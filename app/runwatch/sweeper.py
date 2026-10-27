"""The lease sweeper: moves RUNNING jobs with an expired lease to STALLED.

One SQL statement, run every ``SWEEP_INTERVAL_S`` by a daemon thread inside
each API process. Any number of replicas can run it: the statement skips job
rows another transaction holds, so each expired job is swept exactly once.
It locks jobs first, then attempts, like the heartbeat, result and cancel
paths. Time is the database clock (``now()``); this module never reads the
app clock. The rule is the one in ``runwatch.lifecycle.expire_lease``.

Logs carry ``job_id`` and ``attempt_number`` only, and failed ticks log the
exception class name only, so connection details never reach the log.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry.context import Context
from sqlalchemy import text
from sqlalchemy.orm import Session

from runwatch.lifecycle import (
    LEASE_EXPIRED_REASON,
    AttemptState,
    FailureCause,
    JobState,
)
from runwatch.metrics import Metrics
from runwatch.telemetry import job_attributes, start_span

logger = logging.getLogger("runwatch.sweeper")

SWEEP_INTERVAL_S = 5

_SWEEP_SQL = text(
    """
    WITH expired AS (
        SELECT j.id AS job_id, a.id AS attempt_id, a.number AS attempt_number
        FROM jobs j
        JOIN attempts a ON a.job_id = j.id AND a.number = j.last_attempt_number
        WHERE j.state = :running AND a.state = :running
          AND a.lease_expires_at < now()
        FOR UPDATE OF j SKIP LOCKED
    ),
    stalled AS (
        UPDATE jobs SET state = :stalled, last_error = :reason
        FROM expired WHERE jobs.id = expired.job_id
        RETURNING jobs.id
    )
    UPDATE attempts
    SET state = :failed, failure_cause = :infra, exit_reason = :reason,
        finished_at = now()
    FROM expired
    WHERE attempts.id = expired.attempt_id AND attempts.state = :running
    RETURNING attempts.job_id, attempts.id, attempts.number
    """
)


@dataclass(frozen=True)
class SweptLease:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int


def sweep_expired_leases(session: Session) -> list[SweptLease]:
    """Sweep every expired current attempt in one statement. Does not commit."""
    rows = session.execute(
        _SWEEP_SQL,
        {
            "running": JobState.RUNNING.value,
            "stalled": JobState.STALLED.value,
            "failed": AttemptState.FAILED.value,
            "infra": FailureCause.INFRA.value,
            "reason": LEASE_EXPIRED_REASON,
        },
    ).all()
    return [SweptLease(r[0], r[1], r[2]) for r in rows]


class LeaseSweeper:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        interval_s: float = SWEEP_INTERVAL_S,
        *,
        metrics: Metrics | None = None,
    ) -> None:
        self._metrics = metrics
        self._session_factory = session_factory
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="lease-sweeper", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
            if thread is not None:
                thread.join()
            self._thread = None

    def run_once(self) -> list[SweptLease]:
        session = self._session_factory()
        try:
            swept = sweep_expired_leases(session)
            session.commit()
        finally:
            session.close()
        if self._metrics is not None:
            self._metrics.swept(len(swept))
        for lease in swept:
            with start_span(
                "lease.expire",
                context=Context(),
                attributes=job_attributes(lease.job_id, lease.attempt_number),
            ):
                pass
        return swept

    def _loop(self) -> None:
        while True:
            try:
                for lease in self.run_once():
                    logger.info(
                        "swept job_id=%s attempt_number=%d",
                        lease.job_id,
                        lease.attempt_number,
                        extra={
                            "job_id": str(lease.job_id),
                            "attempt_number": lease.attempt_number,
                        },
                    )
            except Exception as exc:
                # Class name only: driver messages hold the host and port.
                logger.error("lease sweep failed: %s", type(exc).__name__)
            if self._stop.wait(self._interval_s):
                return
