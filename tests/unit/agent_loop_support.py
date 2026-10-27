"""Fakes and helpers for the responder loop tests (not a test module)."""

import copy
import json
import socket
from datetime import UTC, datetime

import pytest
from runwatch.agent.incidents import IncidentStore
from runwatch.agent.model import ModelResponse, Usage
from runwatch.agent.tools import ToolConfig, ToolRunner

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
JOB_ID = "0b9f6d52-3c1c-4a46-9a53-8d2f6d0f7a11"


def _guard(monkeypatch):
    """Any real connection, or a real API key, fails the test loudly."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def refuse(self, *args, **kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)


no_network = pytest.fixture(autouse=True)(_guard)


class FakeModelClient:
    """Replays a scripted list of responses (or exceptions); records each call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        assert self.script, "the loop asked the model for one call too many"
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Recorder:
    """A fake service client: calls are recorded, answers come from `data`."""

    def __init__(self, **data):
        self.calls = []
        self.data = data
        self.namespace = "runwatch-dev"

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args):
            self.calls.append((name, *args))
            answer = self.data.get(name)
            if isinstance(answer, Exception):
                raise answer
            return answer(*args) if callable(answer) else answer

        return method


STATS_OK = {
    "as_of": "2026-03-01T12:00:00Z",
    "window_seconds": 600,
    "counts": {"QUEUED": 2, "RUNNING": 0, "STALLED": 0},
    "oldest_queued_age_seconds": None,
    "jobs_finished": 0,
    "jobs_failed": 0,
    "job_failure_rate": None,
    "attempts_finished": 0,
    "attempts_failed_infra": 0,
    "infra_attempt_failure_rate": None,
}


def make_runner(api=None, loki=None, kube="default"):
    api = api if api is not None else Recorder(get_stats=STATS_OK)
    loki = loki if loki is not None else Recorder()
    kube = Recorder() if kube == "default" else kube
    runner = ToolRunner(ToolConfig(env="dev"), api, loki, kube, clock=lambda: NOW)
    runner.fakes = (api, loki, kube)
    return runner


def service_calls(runner):
    return [c for fake in runner.fakes if fake for c in fake.calls]


def make_incident(tmp_path, alertname="JobsStalled", fingerprint="fp1"):
    alert = {
        "status": "firing",
        "labels": {"alertname": alertname, "env": "dev"},
        "annotations": {"summary": "alert"},
        "startsAt": "2026-03-01T11:00:00Z",
        "fingerprint": fingerprint,
    }
    incident, _ = IncidentStore(tmp_path / "incidents").create(alert, NOW)
    return incident


def usage(total=100, **kw):
    return Usage(input_tokens=total, output_tokens=0, **kw)


def reply(stop_reason, *blocks, tokens=100):
    return ModelResponse(stop_reason, list(blocks), usage(tokens))


def say(text):
    return {"type": "text", "text": text}


def use(name, args=None, id="tu1"):
    return {"type": "tool_use", "id": id, "name": name, "input": args or {}}


def tool_turn(name, args=None, id="tu1", tokens=100):
    return reply("tool_use", use(name, args, id), tokens=tokens)


def end_turn(text="done", tokens=100):
    return reply("end_turn", say(text), tokens=tokens)


class FakeTime:
    """A fake monotonic clock whose sleep advances it."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def actions_of(incident):
    path = incident.path / "actions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]
