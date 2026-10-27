"""Load generator against a fake submitter and fake clock."""

import logging
import os
import subprocess
import sys
import threading

import httpx
import pytest
from runwatch.api.job_schemas import JobCreate
from runwatch.loadgen import (
    HTTP_TIMEOUT_S,
    HttpJobSubmitter,
    Loadgen,
    LoadgenSettingsError,
    SubmitFailed,
    load_loadgen_settings,
)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.on_sleep = None

    def sleep(self, s):
        self.t += s
        if self.on_sleep:
            self.on_sleep(self.t)

    def monotonic(self):
        return self.t


class Submitter:
    def __init__(self, clock, takes=0.0, results=None):
        self.clock = clock
        self.takes = takes
        self.times = []
        self.results = list(results or [])

    def submit(self):
        self.times.append(self.clock.t)
        self.clock.t += self.takes
        if self.results:
            r = self.results.pop(0)
            if r is not None:
                raise r
        return "job-1"


def run_until(interval, until, takes=0.0, results=None):
    clock = Clock()
    stop = threading.Event()
    sub = Submitter(clock, takes, results)
    clock.on_sleep = lambda t: stop.set() if t >= until else None
    Loadgen(sub, interval, stop, clock.sleep, clock.monotonic).run()
    return sub


def test_schedule_default_interval():
    assert run_until(10, 29.5).times == [0, 10, 20]
    assert run_until(10, 30.5).times == [0, 10, 20, 30]


def test_schedule_fractional_interval():
    assert run_until(2.5, 8).times == [0, 2.5, 5, 7.5]


def test_fixed_rate_with_slow_submit():
    assert run_until(10, 25, takes=3).times == [0, 10, 20]


def test_missed_ticks_are_skipped():
    assert run_until(10, 45, takes=25).times == [0, 30]


def test_failures_warn_and_schedule_holds(caplog):
    caplog.set_level(logging.INFO)
    errs = [SubmitFailed("status 500"), None, SubmitFailed("ConnectError"), None]
    sub = run_until(10, 35, results=errs)
    assert sub.times == [0, 10, 20, 30]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert sum("job_id=job-1" in r.getMessage() for r in caplog.records) == 2


def test_stop_returns_within_a_second():
    clock = Clock()
    stop = threading.Event()
    clock.on_sleep = lambda t: stop.set() if t >= 3 else None
    Loadgen(Submitter(clock), 10, stop, clock.sleep, clock.monotonic).run()
    assert clock.t <= 4


def _submitter(handler):
    client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=HTTP_TIMEOUT_S
    )
    return HttpJobSubmitter("http://h:8000/", client=client)


def test_http_body_is_valid_job_create():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"id": "abc"})

    assert _submitter(handler).submit() == "abc"
    import json

    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "http://h:8000/api/v1/jobs"
    assert body == {
        "name": "loadgen",
        "spec": {"duration_s": 10, "fail_probability": 0.0, "mode": "normal"},
    }
    assert JobCreate.model_validate(body).max_attempts == 3


@pytest.mark.parametrize("status", [400, 422, 500, 503])
def test_http_error_statuses(status):
    with pytest.raises(SubmitFailed, match=str(status)):
        _submitter(lambda r: httpx.Response(status, text="secret body")).submit()


def test_http_connection_error_names_class_only():
    def boom(request):
        raise httpx.ConnectError("secret detail", request=request)

    with pytest.raises(SubmitFailed) as info:
        _submitter(boom).submit()
    assert str(info.value) == "ConnectError"


def test_timeout_is_5s():
    assert HTTP_TIMEOUT_S == 5
    assert HttpJobSubmitter("http://h")._client.timeout == httpx.Timeout(5)


def test_settings(monkeypatch):
    monkeypatch.setenv("RUNWATCH_API_URL", "http://x")
    monkeypatch.delenv("LOADGEN_INTERVAL_S", raising=False)
    assert load_loadgen_settings().loadgen_interval_s == 10
    monkeypatch.setenv("LOADGEN_INTERVAL_S", "2.5")
    assert load_loadgen_settings().loadgen_interval_s == 2.5
    for bad in ["0", "-1", "abc"]:
        monkeypatch.setenv("LOADGEN_INTERVAL_S", bad)
        with pytest.raises(LoadgenSettingsError, match="LOADGEN_INTERVAL_S"):
            load_loadgen_settings()
    monkeypatch.delenv("LOADGEN_INTERVAL_S")
    monkeypatch.delenv("RUNWATCH_API_URL")
    with pytest.raises(LoadgenSettingsError, match="RUNWATCH_API_URL"):
        load_loadgen_settings()


def test_missing_url_exits_nonzero_without_database_url():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("RUNWATCH_API_URL", "DATABASE_URL")
    }
    out = subprocess.run(
        [sys.executable, "-m", "runwatch.loadgen"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode != 0
    assert "RUNWATCH_API_URL" in out.stderr
