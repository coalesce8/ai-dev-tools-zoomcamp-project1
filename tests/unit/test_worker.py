"""Worker loop against a fake API, fake clock and fake sleep."""

import logging
import os
import random
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime

import pytest
from runwatch.api.job_schemas import WorkloadSpec
from runwatch.lifecycle import FailureCause
from runwatch.worker.client import (
    ApiUnavailable,
    AttemptNotCurrent,
    Claim,
)
from runwatch.worker.runner import Worker
from runwatch.worker.settings import (
    WorkerSettings,
    WorkerSettingsError,
    load_worker_settings,
)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []
        self.on_sleep = None

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        if self.on_sleep:
            self.on_sleep(self.t)

    def monotonic(self):
        return self.t

    def utcnow(self):
        return datetime(2026, 1, 1, tzinfo=UTC)


class FakeApi:
    """claims: Claim|None|Exception items; once empty, claim stops the worker."""

    def __init__(self, clock, stop, claims=()):
        self.clock = clock
        self.stop = stop
        self.claims = list(claims)
        self.calls = []  # (t, kind, payload)
        self.heartbeat_fn = lambda n, t, lines: None  # raise to fail
        self.result_fn = lambda n, t: None
        self.heartbeats = 0
        self.results = 0

    def claim(self, worker_id):
        self.calls.append((self.clock.t, "claim", worker_id))
        if not self.claims:
            self.stop.set()
            return None
        item = self.claims.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def heartbeat(self, attempt_id, lines):
        self.heartbeats += 1
        self.calls.append((self.clock.t, "heartbeat", (attempt_id, list(lines))))
        self.heartbeat_fn(self.heartbeats, self.clock.t, lines)

    def result(self, attempt_id, outcome, reason=None, cause=None):
        self.results += 1
        self.calls.append((self.clock.t, "result", (outcome, reason, cause)))
        self.result_fn(self.results, self.clock.t)

    def kinds(self):
        return [c[1] for c in self.calls]

    def of(self, kind):
        return [c for c in self.calls if c[1] == kind]


def make_claim(duration_s=5, mode="normal", fail_probability=0.0, spec=None):
    return Claim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        attempt_number=1,
        spec=spec
        if spec is not None
        else WorkloadSpec(
            duration_s=duration_s, mode=mode, fail_probability=fail_probability
        ),
        trace_context=None,
    )


class FakeRng:
    def __init__(self, value=0.5):
        self.value = value
        self.calls = 0

    def random(self):
        self.calls += 1
        return self.value


def build(claims=(), fault=None, rng=None):
    clock = Clock()
    stop = threading.Event()
    api = FakeApi(clock, stop, claims)
    worker = Worker(
        api,
        "w-1",
        worker_fault=fault,
        stop=stop,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
        rng=rng or FakeRng(),
    )
    return worker, api, clock, stop


# -- idle polling ---------------------------------------------------------


def test_idle_polls_once_per_second():
    worker, api, clock, stop = build()
    clock.on_sleep = lambda t: stop.set() if t >= 10.5 else None
    api.stop = threading.Event()  # keep the fake from stopping the worker itself
    worker.run()
    claims = api.of("claim")
    assert [c[0] for c in claims[:11]] == [float(i) for i in range(11)]
    assert {c[2] for c in claims} == {"w-1"}


def test_claim_outage_logs_once_and_recovers(caplog):
    caplog.set_level(logging.INFO)
    errs = [ApiUnavailable("x")] * 10
    worker, api, clock, stop = build(claims=[*errs, None])
    worker.run()
    assert [c[0] for c in api.of("claim")] == [float(i) for i in range(12)][:12]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if "working again" in r.getMessage()]
    assert len(warnings) == 1
    assert len(infos) == 1


def test_claims_again_right_away_after_an_attempt():
    worker, api, clock, stop = build(claims=[make_claim(1)])
    worker.run()
    claims = api.of("claim")
    assert claims[1][0] == 1.0
    assert api.kinds() == ["claim", "heartbeat", "result", "claim"]


# -- running an attempt ---------------------------------------------------


def test_five_step_success():
    claim = make_claim(5)
    worker, api, clock, _ = build(claims=[claim])
    worker.run()
    beats = api.of("heartbeat")
    assert clock.sleeps == [1] * 5
    assert len(beats) == 5
    for i, (_, _, (aid, lines)) in enumerate(beats, start=1):
        assert aid == claim.attempt_id
        assert [item.line for item in lines] == [f"step {i}/5"]
        assert lines[0].ts.utcoffset().total_seconds() == 0
    assert api.kinds()[-2:] == ["result", "claim"]
    assert api.of("result")[0][2] == ("succeeded", None, None)
    assert len(api.of("result")) == 1


@pytest.mark.parametrize("n", [1, 600])
def test_step_counts(n):
    worker, api, clock, _ = build(claims=[make_claim(n)])
    worker.run()
    assert len(api.of("heartbeat")) == n
    assert clock.sleeps == [1] * n


def test_always_fail_runs_all_steps_without_roll():
    rng = FakeRng()
    worker, api, _, _ = build(claims=[make_claim(3, mode="always_fail")], rng=rng)
    worker.run()
    assert len(api.of("heartbeat")) == 3
    assert api.of("result")[0][2] == (
        "failed",
        "always_fail mode",
        FailureCause.WORKLOAD,
    )
    assert rng.calls == 0


@pytest.mark.parametrize(
    ("p", "roll", "fails"),
    [(0.0, 0.0, False), (1.0, 0.999, True), (0.3, 0.29, True), (0.3, 0.3, False)],
)
def test_probability_roll(p, roll, fails):
    rng = FakeRng(roll)
    worker, api, _, _ = build(claims=[make_claim(2, fail_probability=p)], rng=rng)
    worker.run()
    assert rng.calls == 1
    outcome = api.of("result")[0][2]
    if fails:
        assert outcome == (
            "failed",
            f"random failure (fail_probability={p})",
            FailureCause.WORKLOAD,
        )
    else:
        assert outcome[0] == "succeeded"


def test_no_literal_causes_in_worker_package():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "app", "runwatch")
    out = subprocess.run(
        ["grep", "-rnE", '"(workload|infra)"', os.path.join(root, "worker")],
        capture_output=True,
        text=True,
    )
    assert out.stdout == ""


def test_logs_never_contain_lines_or_reasons(caplog):
    caplog.set_level(logging.DEBUG)
    worker, api, _, _ = build(claims=[make_claim(2, mode="always_fail")])
    worker.run()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "step 1/2" not in text
    assert "always_fail mode" not in text
    assert "duration_s" not in text


# -- fault injection ------------------------------------------------------


def test_fail_all_fault():
    rng = FakeRng()
    worker, api, clock, _ = build(
        claims=[make_claim(30, mode="always_fail", fail_probability=0.5)],
        fault="fail_all",
        rng=rng,
    )
    worker.run()
    assert clock.sleeps == [1]
    beats = api.of("heartbeat")
    assert len(beats) == 1
    assert [x.line for x in beats[0][2][1]] == ["injected fault: fail_all"]
    assert api.of("result")[0][2] == (
        "failed",
        "injected fault: fail_all",
        FailureCause.INFRA,
    )
    assert rng.calls == 0


@pytest.mark.parametrize("value", [None, "", "FAIL_ALL", "fail-all", "bogus"])
def test_other_fault_values_run_normally(value, caplog):
    worker, api, _, _ = build(claims=[make_claim(2)], fault=value)
    worker.run()
    assert api.of("result")[0][2][0] == "succeeded"
    warned = [r for r in caplog.records if "WORKER_FAULT" in r.getMessage()]
    assert len(warned) == (1 if value else 0)


def test_fault_read_once_from_settings(monkeypatch):
    monkeypatch.setenv("RUNWATCH_API_URL", "http://x")
    monkeypatch.setenv("WORKER_FAULT", "fail_all")
    settings = load_worker_settings()
    worker, api, _, _ = build(
        claims=[make_claim(3), make_claim(3)], fault=settings.worker_fault
    )
    monkeypatch.delenv("WORKER_FAULT")
    worker.run()
    assert [c[2][0] for c in api.of("result")] == ["failed", "failed"]


# -- cancellation ---------------------------------------------------------


def test_409_on_heartbeat_stops_attempt():
    worker, api, clock, _ = build(claims=[make_claim(10)])

    def hb(n, t, lines):
        if n == 3:
            raise AttemptNotCurrent

    api.heartbeat_fn = hb
    worker.run()
    assert clock.sleeps == [1, 1, 1]
    assert api.of("result") == []
    assert api.calls[-2][1] == "heartbeat"
    assert api.calls[-1][1] == "claim"
    assert api.calls[-1][0] == api.calls[-2][0]


def test_409_during_outage_retry():
    worker, api, clock, _ = build(claims=[make_claim(10)])

    def hb(n, t, lines):
        if n == 5:
            raise AttemptNotCurrent
        if n >= 3:
            raise ApiUnavailable("x")

    api.heartbeat_fn = hb
    worker.run()
    assert api.of("result") == []
    assert clock.sleeps == [1] * 5


# -- API unreachable during an attempt -----------------------------------


def test_outage_buffers_lines_in_order():
    worker, api, clock, _ = build(claims=[make_claim(10)])

    def hb(n, t, lines):
        if 3 <= t < 8:
            raise ApiUnavailable("x")

    api.heartbeat_fn = hb
    worker.run()
    assert clock.sleeps == [1] * 10
    beats = api.of("heartbeat")
    at8 = [b for b in beats if b[0] == 8 and len(b[2][1]) > 1][0]
    assert [x.line for x in at8[2][1]] == [f"step {i}/10" for i in range(3, 9)]
    sent = [x.line for b in beats if b[0] >= 8 for x in b[2][1]]
    assert sent[-3:] == ["step 8/10", "step 9/10", "step 10/10"]
    assert api.of("result")[0][2][0] == "succeeded"


def test_abandon_after_15s_since_last_success():
    worker, api, clock, _ = build(claims=[make_claim(100)])
    api.heartbeat_fn = lambda n, t, lines: (
        (_ for _ in ()).throw(ApiUnavailable("x")) if t > 4 else None
    )
    worker.run()
    assert api.of("result") == []
    last_hb = api.of("heartbeat")[-1][0]
    assert last_hb == 19  # last success at 4 plus 15
    assert api.calls[-1][1] == "claim"


def test_abandon_after_15s_when_never_succeeded():
    worker, api, clock, _ = build(claims=[make_claim(100)])

    def hb(n, t, lines):
        raise ApiUnavailable("x")

    api.heartbeat_fn = hb
    worker.run()
    assert api.of("result") == []
    assert api.of("heartbeat")[-1][0] == 15


def test_abandon_logs_attempt_id(caplog):
    claim = make_claim(100)
    worker, api, clock, _ = build(claims=[claim])
    api.heartbeat_fn = lambda n, t, lines: (_ for _ in ()).throw(ApiUnavailable("x"))
    worker.run()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(claim.attempt_id) in r.getMessage() for r in warnings)


def test_outage_at_end_retries_then_sends_result():
    worker, api, clock, _ = build(claims=[make_claim(3)])
    api.heartbeat_fn = lambda n, t, lines: (
        (_ for _ in ()).throw(ApiUnavailable("x")) if t in (3, 4, 5) else None
    )
    worker.run()
    result_t = api.of("result")[0][0]
    assert result_t == 6
    assert [x.line for x in api.of("heartbeat")[-1][2][1]] == ["step 3/3"]


def test_outage_at_end_abandons_after_15s():
    worker, api, clock, _ = build(claims=[make_claim(3)])
    api.heartbeat_fn = lambda n, t, lines: (
        (_ for _ in ()).throw(ApiUnavailable("x")) if t >= 3 else None
    )
    worker.run()
    assert api.of("result") == []
    assert api.of("heartbeat")[-1][0] == 17  # last success at 2 plus 15


def test_heartbeat_chunks_of_100():
    worker, api, clock, _ = build(claims=[make_claim(1)])
    from runwatch.worker.client import LogLine

    orig = worker._flush
    pending = [LogLine(clock.utcnow(), f"x{i}") for i in range(129)]

    def flush(claim, buffer, state):
        buffer[:0] = pending
        pending.clear()
        orig(claim, buffer, state)

    worker._flush = flush
    worker.run()
    sizes = [len(b[2][1]) for b in api.of("heartbeat")]
    assert sizes == [100, 30]
    assert api.of("heartbeat")[0][2][1][0].line == "x0"


@pytest.mark.parametrize("exc", [ApiUnavailable("404"), ApiUnavailable("500")])
def test_any_non_409_error_is_retried(exc):
    worker, api, clock, _ = build(claims=[make_claim(2)])
    api.heartbeat_fn = lambda n, t, lines: (
        (_ for _ in ()).throw(exc) if n == 1 else None
    )
    worker.run()
    assert api.of("result")[0][2][0] == "succeeded"


# -- result retries -------------------------------------------------------


def test_result_retried_until_success():
    worker, api, clock, _ = build(claims=[make_claim(1)])
    api.result_fn = lambda n, t: (
        (_ for _ in ()).throw(ApiUnavailable("x")) if n < 4 else None
    )
    worker.run()
    assert len(api.of("result")) == 4
    assert [c[0] for c in api.of("result")] == [1, 2, 3, 4]


def test_result_409_stops_retries(caplog):
    caplog.set_level(logging.INFO)
    worker, api, clock, _ = build(claims=[make_claim(1)])

    def res(n, t):
        raise AttemptNotCurrent if n == 2 else ApiUnavailable("x")

    api.result_fn = res
    worker.run()
    assert len(api.of("result")) == 2
    assert api.kinds()[-1] == "claim"


def test_result_gives_up_after_30s(caplog):
    claim = make_claim(1)
    worker, api, clock, _ = build(claims=[claim])
    api.result_fn = lambda n, t: (_ for _ in ()).throw(ApiUnavailable("x"))
    worker.run()
    n = len(api.of("result"))
    assert 30 <= n <= 31
    assert any(
        r.levelno == logging.WARNING and str(claim.attempt_id) in r.getMessage()
        for r in caplog.records
    )
    assert api.kinds()[-1] == "claim"


# -- unexpected errors ----------------------------------------------------


def test_bad_spec_reports_worker_error(caplog):
    claim = make_claim(spec={"duration_s": "x", "secret": "hunter2"})
    worker, api, _, _ = build(claims=[claim])
    worker.run()
    assert api.of("result")[0][2] == (
        "failed",
        "worker error: ValidationError",
        FailureCause.INFRA,
    )
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in text
    assert "ValidationError" in text
    assert str(claim.attempt_id) in text


def test_clock_raising_reports_worker_error():
    worker, api, clock, _ = build(claims=[make_claim(3)])
    worker.utcnow = lambda: (_ for _ in ()).throw(RuntimeError("secret detail"))
    worker.run()
    assert api.of("result")[0][2][1] == "worker error: RuntimeError"


def test_unusable_claim_response_keeps_polling(caplog):
    worker, api, _, _ = build(claims=[KeyError("attempt_id"), make_claim(1)])
    worker.run()
    assert len(api.of("result")) == 1
    assert any("unusable" in r.getMessage() for r in caplog.records)


# -- shutdown -------------------------------------------------------------


def test_stop_while_idle():
    worker, api, clock, stop = build()
    api.stop = threading.Event()
    clock.on_sleep = lambda t: stop.set() if t >= 2.5 else None
    worker.run()
    assert clock.t <= 3.5


def test_stop_mid_attempt_sends_nothing_more():
    worker, api, clock, stop = build(claims=[make_claim(10)])
    clock.on_sleep = lambda t: stop.set() if t >= 3 else None
    worker.run()
    assert len(api.of("heartbeat")) == 2
    assert api.of("result") == []
    assert clock.t == 3


# -- settings and imports -------------------------------------------------


def test_settings_require_api_url(monkeypatch):
    monkeypatch.delenv("RUNWATCH_API_URL", raising=False)
    with pytest.raises(WorkerSettingsError, match="RUNWATCH_API_URL"):
        load_worker_settings()


def test_worker_id_default_and_validation(monkeypatch):
    monkeypatch.setenv("RUNWATCH_API_URL", "http://x")
    monkeypatch.delenv("WORKER_ID", raising=False)
    wid = load_worker_settings().worker_id
    assert wid.endswith(f"-{os.getpid()}") and len(wid) <= 255
    for bad in ["", "x" * 256]:
        monkeypatch.setenv("WORKER_ID", bad)
        with pytest.raises(WorkerSettingsError, match="WORKER_ID"):
            load_worker_settings()
    monkeypatch.setenv("WORKER_ID", "x" * 255)
    assert WorkerSettings().worker_id == "x" * 255


def test_worker_does_not_import_database_code():
    code = (
        "import runwatch.worker.__main__, sys;"
        "bad={'runwatch.settings','runwatch.db','runwatch.models','runwatch.api.app'};"
        "assert not bad & set(sys.modules), bad & set(sys.modules)"
    )
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_real_rng_is_default():
    worker = Worker(FakeApi(Clock(), threading.Event()), "w")
    assert isinstance(worker.rng, random.Random)
