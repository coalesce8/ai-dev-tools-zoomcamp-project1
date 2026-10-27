"""The incident directory store.

Layout under the incidents directory::

    <incident_id>/alert.json      the first alert, as received
                  context.md      header; later issues append evidence
                  response.md     the Markdown report (empty while running)
                  status.json     state of the run (written atomically)
                  webhooks.jsonl  one line per webhook that touched the incident

The store does no locking; `runs.RunController` serialises access.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FINGERPRINT_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_FINGERPRINT = re.compile(FINGERPRINT_PATTERN)
_STATUS_KEYS = (
    "incident_id",
    "fingerprint",
    "alertname",
    "env",
    "state",
    "outcome",
    "started_at",
    "ended_at",
    "webhook_count",
)

RUNNING = "running"
FINISHED = "finished"


@dataclass(frozen=True)
class Incident:
    """What a `Responder` gets: where the incident lives and what fired."""

    incident_id: str
    fingerprint: str
    alertname: str
    env: str
    path: Path
    alert: dict[str, Any]

    @property
    def context_file(self) -> Path:
        return self.path / "context.md"

    @property
    def response_file(self) -> Path:
        return self.path / "response.md"


def format_time(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def make_incident_id(moment: datetime, fingerprint: str) -> str:
    return f"{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}-{fingerprint}"


def label(alert: dict[str, Any], name: str) -> str:
    labels = alert.get("labels")
    value = labels.get(name) if isinstance(labels, dict) else None
    return value if isinstance(value, str) and value else "unknown"


def atomic_write(target: Path, text: str) -> None:
    """Write `text` to `target` through a temp file in the same directory."""
    fd, tmp = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


INCIDENT_ID_PATTERN = r"^\d{8}T\d{6}Z-[A-Za-z0-9_-]{1,64}$"
_INCIDENT_ID = re.compile(INCIDENT_ID_PATTERN)


def valid_incident_id(incident_id: str) -> bool:
    return _INCIDENT_ID.fullmatch(incident_id) is not None


class IncidentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, incident_id: str) -> Path:
        return self.root / incident_id

    def create(self, alert: dict[str, Any], now: datetime) -> tuple[Incident, dict]:
        """Make a new incident directory for `alert`; returns it and its status."""
        fingerprint = alert["fingerprint"]
        if not _FINGERPRINT.match(fingerprint):
            raise ValueError("unsafe fingerprint")
        self.root.mkdir(parents=True, exist_ok=True)
        # Two incidents of one fingerprint in the same second (cooldown 0) must
        # not share a directory: move the id forward until it is free.
        stamp = now
        while True:
            incident_id = make_incident_id(stamp, fingerprint)
            directory = self.path(incident_id)
            try:
                directory.mkdir()
                break
            except FileExistsError:
                stamp += timedelta(seconds=1)
        alertname = label(alert, "alertname")
        env = label(alert, "env")
        (directory / "alert.json").write_text(json.dumps(alert, indent=2) + "\n")
        (directory / "context.md").write_text(
            f"# Incident {incident_id}\n\n"
            f"- alertname: {alertname}\n"
            f"- env: {env}\n"
            f"- started: {format_time(now)}\n"
        )
        (directory / "response.md").write_text("")
        (directory / "webhooks.jsonl").write_text("")
        status = {
            "incident_id": incident_id,
            "fingerprint": fingerprint,
            "alertname": alertname,
            "env": env,
            "state": RUNNING,
            "outcome": None,
            "started_at": format_time(now),
            "ended_at": None,
            "webhook_count": 0,
        }
        # Last: a directory without status.json is not an incident yet.
        self.write_status(incident_id, status)
        incident = Incident(incident_id, fingerprint, alertname, env, directory, alert)
        return incident, status

    def write_status(self, incident_id: str, status: dict[str, Any]) -> None:
        atomic_write(
            self.path(incident_id) / "status.json", json.dumps(status, indent=2) + "\n"
        )

    def write_response(self, incident_id: str, text: str) -> None:
        atomic_write(self.path(incident_id) / "response.md", text)

    def append_webhook(self, incident_id: str, entry: dict[str, Any]) -> None:
        with (self.path(incident_id) / "webhooks.jsonl").open("a") as handle:
            handle.write(json.dumps(entry) + "\n")

    def append_context(self, incident_id: str, text: str) -> None:
        with (self.path(incident_id) / "context.md").open("a") as handle:
            handle.write(text)

    def read_status(self, incident_id: str) -> dict[str, Any]:
        """Read and validate a status.json; raises ValueError when unusable."""
        try:
            data = json.loads((self.path(incident_id) / "status.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("status.json unreadable") from exc
        if not isinstance(data, dict) or any(k not in data for k in _STATUS_KEYS):
            raise ValueError("status.json incomplete")
        if (
            data["state"] not in (RUNNING, FINISHED)
            or not isinstance(data["fingerprint"], str)
            or not _FINGERPRINT.match(data["fingerprint"])
            or not isinstance(data["webhook_count"], int)
        ):
            raise ValueError("status.json invalid")
        try:
            parse_time(data["started_at"])
            if data["ended_at"] is not None:
                parse_time(data["ended_at"])
        except (TypeError, ValueError) as exc:
            raise ValueError("status.json invalid time") from exc
        return data

    def list_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def list_statuses(self) -> list[dict[str, Any]]:
        """Valid statuses of every incident, newest `started_at` first."""
        found = []
        for incident_id in self.list_ids():
            if not valid_incident_id(incident_id):
                continue
            try:
                found.append(self.read_status(incident_id))
            except ValueError:
                continue
        found.sort(
            key=lambda s: (parse_time(s["started_at"]), s["incident_id"]), reverse=True
        )
        return found

    def read_response(self, incident_id: str) -> str | None:
        """Contents of response.md; None for an unknown or invalid id."""
        if not valid_incident_id(incident_id):
            return None
        try:
            self.read_status(incident_id)
            return (self.path(incident_id) / "response.md").read_text()
        except (OSError, ValueError):
            return None
