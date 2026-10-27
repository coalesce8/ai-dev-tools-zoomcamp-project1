"""Run control: one run at a time per fingerprint, then a cooldown.

State is the incident directories plus a small index in memory, guarded by one
lock. The responder runs on a worker thread, outside the lock.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Protocol

from runwatch.agent.incidents import (
    FINISHED,
    RUNNING,
    Incident,
    IncidentStore,
    format_time,
    parse_time,
)
from runwatch.agent.reports import write_report

logger = logging.getLogger(__name__)

STARTED = "started"
RECORDED_IN_PROGRESS = "recorded_in_progress"
RECORDED_COOLDOWN = "recorded_cooldown"
RECORDED_RESOLVED = "recorded_resolved"
IGNORED_RESOLVED = "ignored_resolved"

OUTCOME_ESCALATED = "escalated"
OUTCOME_SKIPPED = "skipped_no_model"


class Responder(Protocol):
    """Diagnose and fix one incident; return its outcome (the loop fills this)."""

    def respond(self, incident: Incident) -> str: ...


class NoModelResponder:
    """Used without ANTHROPIC_API_KEY: the run ends at once."""

    def respond(self, incident: Incident) -> str:
        return OUTCOME_SKIPPED


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunController:
    def __init__(
        self,
        store: IncidentStore,
        responder: Responder,
        cooldown_s: float,
        clock: Clock = utc_now,
        report_secrets: Sequence[str] = (),
    ) -> None:
        self.report_secrets = tuple(report_secrets)
        self.store = store
        self.responder = responder
        self.cooldown_s = cooldown_s
        self.clock = clock
        # Reentrant: a finished future runs its done-callback in the submitter.
        self._lock = threading.RLock()
        # fingerprint -> status of its most recent incident
        self._latest: dict[str, dict[str, Any]] = {}
        self._futures: set[Future] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="agent-run"
        )

    def recover(self) -> None:
        """Rebuild the index from disk; close runs a dead agent left running."""
        with self._lock:
            for incident_id in self.store.list_ids():
                try:
                    status = self.store.read_status(incident_id)
                except ValueError:
                    logger.warning("skipping incident directory without valid status")
                    continue
                if status["state"] == RUNNING:
                    status["state"] = FINISHED
                    status["outcome"] = OUTCOME_ESCALATED
                    status["ended_at"] = format_time(self.clock())
                    status["interrupted"] = True
                    try:
                        self.store.append_context(
                            incident_id,
                            "\nEscalated: the agent stopped while this run was "
                            "in progress\n",
                        )
                        self.store.write_status(incident_id, status)
                    except OSError:
                        logger.warning("could not close an interrupted incident")
                        continue
                    self._write_report(incident_id)
                    logger.warning(
                        "closed interrupted incident",
                        extra={
                            "incident_id": incident_id,
                            "fingerprint": status["fingerprint"],
                            "outcome": OUTCOME_ESCALATED,
                        },
                    )
                self._remember(status)

    def _remember(self, status: dict[str, Any]) -> None:
        current = self._latest.get(status["fingerprint"])
        key = (status["started_at"], status["incident_id"])
        if current is None or key >= (current["started_at"], current["incident_id"]):
            self._latest[status["fingerprint"]] = status

    def _in_cooldown(self, status: dict[str, Any], now: datetime) -> bool:
        ended = parse_time(status["ended_at"])
        return (now - ended).total_seconds() < self.cooldown_s

    def handle(self, alert: dict[str, Any]) -> tuple[str | None, str]:
        """Apply one alert. Returns (incident_id or None, action)."""
        fingerprint = alert["fingerprint"]
        kind = alert["status"]
        with self._lock:
            now = self.clock()
            latest = self._latest.get(fingerprint)
            active = latest is not None and (
                latest["state"] == RUNNING or self._in_cooldown(latest, now)
            )
            if kind == "resolved":
                if not active:
                    return None, IGNORED_RESOLVED
                self._record(latest, alert, now, RECORDED_RESOLVED)
                return latest["incident_id"], RECORDED_RESOLVED
            if active:
                action = (
                    RECORDED_IN_PROGRESS
                    if latest["state"] == RUNNING
                    else RECORDED_COOLDOWN
                )
                self._record(latest, alert, now, action)
                return latest["incident_id"], action
            incident, status = self.store.create(alert, now)
            self._latest[fingerprint] = status
            self._record(status, alert, now, STARTED)
            self._submit(incident)
            return incident.incident_id, STARTED

    def _record(
        self, status: dict[str, Any], alert: dict[str, Any], now: datetime, action: str
    ) -> None:
        self.store.append_webhook(
            status["incident_id"],
            {
                "received_at": format_time(now),
                "kind": alert["status"],
                "disposition": action,
                "status": alert["status"],
                "startsAt": alert.get("startsAt"),
            },
        )
        status["webhook_count"] += 1
        self.store.write_status(status["incident_id"], status)
        if status["state"] == FINISHED:
            self._write_report(status["incident_id"])
        logger.info(
            "webhook handled",
            extra={
                "incident_id": status["incident_id"],
                "fingerprint": status["fingerprint"],
                "alertname": status["alertname"],
                "action": action,
            },
        )

    def _write_report(self, incident_id: str) -> None:
        write_report(self.store, incident_id, self.clock(), self.report_secrets)

    def _submit(self, incident: Incident) -> None:
        future = self._executor.submit(self._run, incident)
        self._futures.add(future)
        future.add_done_callback(self._discard)

    def _discard(self, future: Future) -> None:
        with self._lock:
            self._futures.discard(future)

    def _run(self, incident: Incident) -> None:
        try:
            outcome = self.responder.respond(incident)
            if not isinstance(outcome, str) or not outcome:
                outcome = OUTCOME_ESCALATED
        except Exception as exc:
            outcome = OUTCOME_ESCALATED
            logger.error(
                "responder failed",
                extra={
                    "incident_id": incident.incident_id,
                    "exc_type": type(exc).__name__,
                },
            )
        with self._lock:
            status = self._latest[incident.fingerprint]
            status["state"] = FINISHED
            status["outcome"] = outcome
            status["ended_at"] = format_time(self.clock())
            self.store.write_status(incident.incident_id, status)
            self._write_report(incident.incident_id)
        logger.info(
            "run finished",
            extra={
                "incident_id": incident.incident_id,
                "fingerprint": incident.fingerprint,
                "alertname": incident.alertname,
                "outcome": outcome,
            },
        )

    def wait_idle(self, timeout: float = 10) -> None:
        """Block until the runs in flight have finished (tests)."""
        with self._lock:
            pending = list(self._futures)
        for future in pending:
            future.result(timeout=timeout)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
