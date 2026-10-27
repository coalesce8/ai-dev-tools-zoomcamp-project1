"""Shared helpers for the agent tests (not a test module)."""

import threading
from datetime import UTC, datetime, timedelta

from runwatch.agent.app import create_app
from runwatch.agent.incidents import Incident
from runwatch.agent.settings import AgentSettings

TOKEN = "tok-" + "unit"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class GateResponder:
    """Blocks each run until `release()`; records the incidents it saw."""

    def __init__(self, outcome: str = "fixed") -> None:
        self.gate = threading.Event()
        self.outcome = outcome
        self.seen: list[Incident] = []

    def respond(self, incident: Incident) -> str:
        self.seen.append(incident)
        assert self.gate.wait(10)
        return self.outcome

    def release(self) -> None:
        self.gate.set()


class RaisingResponder:
    def respond(self, incident: Incident) -> str:
        raise RuntimeError("boom " + "MARKER-IN-EXC")


def make_settings(tmp_path, **overrides) -> AgentSettings:
    values = {
        "agent_webhook_token": TOKEN,
        "agent_incidents_dir": str(tmp_path / "incidents"),
        # Explicit, so the developer's environment cannot leak in.
        "agent_cooldown_s": 600,
        "anthropic_api_key": None,
    }
    values.update(overrides)
    return AgentSettings(_env_file=None, **values)


def make_app(tmp_path, responder=None, clock=None, **overrides):
    return create_app(
        make_settings(tmp_path, **overrides),
        responder=responder,
        clock=clock or FakeClock(),
    )


def alert(fingerprint="fp1", status="firing", **extra):
    body = {
        "status": status,
        "labels": {"alertname": "HighFailureRate", "env": "demo"},
        "annotations": {"summary": "jobs are failing"},
        "startsAt": "2026-01-02T03:00:00Z",
        "fingerprint": fingerprint,
    }
    body.update(extra)
    return body


def body(*alerts):
    return {"version": "4", "status": "firing", "alerts": list(alerts)}
