"""Application metrics against a real database, read from an in-memory reader.

In-process TestClient: no port, no collector, no network.
"""

import json
import logging
import os
import uuid

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    PeriodicExportingMetricReader,
)
from runwatch import metrics as m
from runwatch.api import app as app_module
from runwatch.api.app import create_app
from runwatch.api.worker_claim import ClaimResult
from runwatch.settings import Settings
from runwatch.sweeper import LeaseSweeper
from runwatch.telemetry import build_meter_provider
from sqlalchemy import create_engine, text

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET_NAME = "secret-job-name-xyz"
SECRET_LINE = "distinctive-log-line-qrs"
SECRET_REASON = "distinctive-failure-reason-tuv"
BODY = {"name": SECRET_NAME, "spec": {"duration_s": 2, "fail_probability": 0.0}}
CLAIM = "/internal/v1/worker/claim"
ATTEMPTS = "/internal/v1/worker/attempts"
GAUGES = {m.QUEUE_DEPTH, m.OLDEST_QUEUED_AGE, m.JOBS_STALLED}


@pytest.fixture(scope="module")
def engine():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")
    engine = create_engine(DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))


def read(reader):
    """{(name, frozenset of attributes): data point} over everything exported."""
    data = reader.get_metrics_data()
    found = {}
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    found[(metric.name, frozenset(point.attributes.items()))] = point
    return found


def get(found, name, **attrs):
    point = found.get((name, frozenset(attrs.items())))
    if point is None:
        return 0
    return point.count if hasattr(point, "count") else point.value


def total(found, name, **attrs):
    point = found.get((name, frozenset(attrs.items())))
    return 0 if point is None else point.sum


class Env:
    def __init__(self, engine, **kwargs):
        self.engine = engine
        self.reader = InMemoryMetricReader()
        self.provider = build_meter_provider("runwatch-api", self.reader)
        self.app = create_app(
            Settings(database_url=DATABASE_URL, sweeper_enabled=False),
            meter_provider=self.provider,
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def snap(self):
        return read(self.reader)

    def sql(self, sql, **params):
        with self.engine.begin() as conn:
            res = conn.execute(text(sql), params)
            return res.mappings().all() if res.returns_rows else []

    def seed(
        self,
        state="QUEUED",
        *,
        queued_ago=0,
        created_ago=None,
        attempt_count=0,
        max_attempts=3,
        attempt_state=None,
        started_ago=30,
        lease_s=60,
    ):
        """Insert a job (and one attempt when `attempt_state` is given)."""
        job_id = uuid.uuid4()
        last = attempt_count
        self.sql(
            """INSERT INTO jobs (id, name, spec, state, max_attempts, attempt_count,
                last_attempt_number, created_at, queued_at)
               VALUES (:id, :name, CAST(:spec AS jsonb), :state, :max, :n, :n,
                now() - make_interval(secs => :created),
                now() - make_interval(secs => :queued))""",
            id=job_id,
            name=SECRET_NAME,
            spec=json.dumps(BODY["spec"]),
            state=state,
            max=max_attempts,
            n=last,
            created=created_ago if created_ago is not None else queued_ago,
            queued=queued_ago,
        )
        attempt_id = None
        if attempt_state:
            attempt_id = uuid.uuid4()
            self.sql(
                """INSERT INTO attempts (id, job_id, number, worker_id, state,
                    lease_expires_at, started_at)
                   VALUES (:id, :job, :n, 'w1', :state,
                    now() + make_interval(secs => :lease),
                    now() - make_interval(secs => :started))""",
                id=attempt_id,
                job=job_id,
                n=max(last, 1),
                state=attempt_state,
                lease=lease_s,
                started=started_ago,
            )
        return job_id, attempt_id

    def running(self, **kwargs):
        kwargs.setdefault("attempt_count", 1)
        return self.seed("RUNNING", attempt_state="RUNNING", **kwargs)

    def result(self, attempt_id, body):
        return self.client.post(f"{ATTEMPTS}/{attempt_id}/result", json=body)

    def fail(self, attempt_id, cause="workload"):
        return self.result(
            attempt_id, {"outcome": "failed", "reason": SECRET_REASON, "cause": cause}
        )


@pytest.fixture
def env(engine):
    e = Env(engine)
    with e.client:
        yield e


def counters(found):
    return {
        k: (p.count, p.sum) if hasattr(p, "count") else p.value
        for k, p in found.items()
        if k[0].startswith("runwatch_") and k[0] not in GAUGES
    }


# --- startup -----------------------------------------------------------------


def test_series_exist_at_zero_before_any_event(env):
    found = env.snap()
    assert get(found, m.JOBS_SUBMITTED) == 0
    assert (m.JOBS_SUBMITTED, frozenset()) in found
    for outcome in ("succeeded", "failed", "cancelled"):
        assert (m.JOBS_FINISHED, frozenset({("outcome", outcome)})) in found
    for pair in (("succeeded", "none"), ("failed", "workload"), ("failed", "infra")):
        key = frozenset({("outcome", pair[0]), ("cause", pair[1])})
        assert found[(m.ATTEMPTS_FINISHED, key)].value == 0


def test_create_app_without_arguments_works(engine, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app = create_app(Settings(database_url=DATABASE_URL, sweeper_enabled=False))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/api/v1/jobs", json=BODY).status_code == 201


# --- submit and claim ----------------------------------------------------------


def test_submit_counts_only_accepted_jobs(env):
    before = env.snap()
    assert env.client.post("/api/v1/jobs", json=BODY).status_code == 201
    mid = env.snap()
    assert get(mid, m.JOBS_SUBMITTED) == get(before, m.JOBS_SUBMITTED) + 1
    assert env.client.post("/api/v1/jobs", json={"name": ""}).status_code == 400
    assert counters(env.snap()) == counters(mid)


def test_claim_observes_queue_wait_from_queued_at(env):
    env.seed(queued_ago=90)
    before = env.snap()
    r = env.client.post(CLAIM, json={"worker_id": "w1"})
    assert r.status_code == 200
    assert set(r.json()) == set(ClaimResult.model_fields)
    after = env.snap()
    assert get(after, m.QUEUE_WAIT) == get(before, m.QUEUE_WAIT) + 1
    assert total(after, m.QUEUE_WAIT) - total(before, m.QUEUE_WAIT) == pytest.approx(
        90, abs=0.5
    )


def test_claim_of_requeued_job_measures_from_the_reset(env):
    env.seed(queued_ago=10, created_ago=3600)
    before = env.snap()
    assert env.client.post(CLAIM, json={"worker_id": "w1"}).status_code == 200
    delta = total(env.snap(), m.QUEUE_WAIT) - total(before, m.QUEUE_WAIT)
    assert delta == pytest.approx(10, abs=0.5)


def test_empty_claim_observes_nothing(env):
    before = counters(env.snap())
    assert env.client.post(CLAIM, json={"worker_id": "w1"}).status_code == 204
    assert counters(env.snap()) == before


# --- result ------------------------------------------------------------------


def attempts(found, outcome, cause):
    return get(found, m.ATTEMPTS_FINISHED, outcome=outcome, cause=cause)


def test_result_succeeded(env):
    _, attempt = env.running(started_ago=30)
    before = env.snap()
    assert env.result(attempt, {"outcome": "succeeded"}).status_code == 200
    after = env.snap()
    assert (
        attempts(after, "succeeded", "none")
        == attempts(before, "succeeded", "none") + 1
    )
    assert get(after, m.JOBS_FINISHED, outcome="succeeded") == (
        get(before, m.JOBS_FINISHED, outcome="succeeded") + 1
    )
    assert get(after, m.JOB_RUN, outcome="succeeded") == (
        get(before, m.JOB_RUN, outcome="succeeded") + 1
    )
    run = total(after, m.JOB_RUN, outcome="succeeded") - total(
        before, m.JOB_RUN, outcome="succeeded"
    )
    assert run == pytest.approx(30, abs=0.5)


def test_failed_with_attempts_left_is_not_a_finished_job(env):
    _, attempt = env.running(attempt_count=1, max_attempts=3)
    before = env.snap()
    r = env.fail(attempt)
    assert r.status_code == 200 and r.json()["job_state"] == "QUEUED"
    after = env.snap()
    assert (
        attempts(after, "failed", "workload")
        == attempts(before, "failed", "workload") + 1
    )
    assert (
        get(after, m.JOB_RUN, outcome="failed")
        == get(before, m.JOB_RUN, outcome="failed") + 1
    )
    for outcome in ("succeeded", "failed", "cancelled"):
        assert get(after, m.JOBS_FINISHED, outcome=outcome) == get(
            before, m.JOBS_FINISHED, outcome=outcome
        )


@pytest.mark.parametrize(
    ("attempt_count", "max_attempts", "cause"),
    [(1, 1, "workload"), (3, 3, "infra"), (3, 3, "workload")],
)
def test_failed_without_attempts_left_finishes_the_job_once(
    env, attempt_count, max_attempts, cause
):
    _, attempt = env.running(attempt_count=attempt_count, max_attempts=max_attempts)
    before = env.snap()
    r = env.fail(attempt, cause)
    assert r.json()["job_state"] == "FAILED"
    after = env.snap()
    assert attempts(after, "failed", cause) == attempts(before, "failed", cause) + 1
    other = "infra" if cause == "workload" else "workload"
    assert attempts(after, "failed", other) == attempts(before, "failed", other)
    assert get(after, m.JOBS_FINISHED, outcome="failed") == (
        get(before, m.JOBS_FINISHED, outcome="failed") + 1
    )


def test_rejected_result_and_heartbeat_change_nothing(env):
    _, attempt = env.running()
    assert env.result(attempt, {"outcome": "succeeded"}).status_code == 200
    before = counters(env.snap())
    assert env.result(attempt, {"outcome": "succeeded"}).status_code == 409
    assert env.fail(attempt).status_code == 409
    r = env.client.post(f"{ATTEMPTS}/{attempt}/heartbeat", json={})
    assert r.status_code == 409
    job, attempt2 = env.running()
    assert env.client.post(f"/api/v1/jobs/{job}/cancel").status_code == 200
    before = counters(env.snap())
    assert env.result(attempt2, {"outcome": "succeeded"}).status_code == 409
    assert counters(env.snap()) == before


def test_rolled_back_result_changes_nothing(env, monkeypatch):
    _, attempt = env.running()
    before = counters(env.snap())

    def boom(self):
        raise RuntimeError("commit failed")

    monkeypatch.setattr("sqlalchemy.orm.Session.commit", boom)
    assert env.result(attempt, {"outcome": "succeeded"}).status_code == 500
    monkeypatch.undo()
    assert counters(env.snap()) == before


def test_demo_incident_ten_workload_failures(env):
    before = env.snap()
    for _ in range(10):
        _, attempt = env.running(attempt_count=1, max_attempts=1)
        assert env.fail(attempt).status_code == 200
    after = env.snap()
    assert (
        attempts(after, "failed", "workload") - attempts(before, "failed", "workload")
        == 10
    )
    assert attempts(after, "failed", "infra") == attempts(before, "failed", "infra")
    assert (
        get(after, m.JOBS_FINISHED, outcome="failed")
        - get(before, m.JOBS_FINISHED, outcome="failed")
        == 10
    )


# --- sweep, cancel, requeue --------------------------------------------------


def test_sweep_counts_each_attempt_once(env):
    for _ in range(2):
        env.running(lease_s=-5)
    env.running(lease_s=60)
    sweeper = LeaseSweeper(env.app.state.session_factory, metrics=env.app.state.metrics)
    before = env.snap()
    assert len(sweeper.run_once()) == 2
    after = env.snap()
    assert attempts(after, "failed", "infra") == attempts(before, "failed", "infra") + 2
    assert sweeper.run_once() == []
    again = env.snap()
    assert counters(again) == counters(after)
    for outcome in ("succeeded", "failed", "cancelled"):
        assert get(after, m.JOBS_FINISHED, outcome=outcome) == get(
            before, m.JOBS_FINISHED, outcome=outcome
        )
    assert get(after, m.JOB_RUN, outcome="failed") == get(
        before, m.JOB_RUN, outcome="failed"
    )


def test_sweeper_without_metrics_records_nothing(env):
    env.running(lease_s=-5)
    before = counters(env.snap())
    assert len(LeaseSweeper(env.app.state.session_factory).run_once()) == 1
    assert counters(env.snap()) == before


@pytest.mark.parametrize("kind", ["queued", "stalled", "running"])
def test_cancel_counts_a_cancelled_job_only(env, kind):
    if kind == "running":
        job, _ = env.running()
    else:
        job, _ = env.seed(kind.upper(), attempt_count=1 if kind == "stalled" else 0)
    before = env.snap()
    assert env.client.post(f"/api/v1/jobs/{job}/cancel").status_code == 200
    after = env.snap()
    assert get(after, m.JOBS_FINISHED, outcome="cancelled") == (
        get(before, m.JOBS_FINISHED, outcome="cancelled") + 1
    )
    assert {
        k: v for k, v in counters(after).items() if k[0] == m.ATTEMPTS_FINISHED
    } == {k: v for k, v in counters(before).items() if k[0] == m.ATTEMPTS_FINISHED}
    assert env.client.post(f"/api/v1/jobs/{job}/cancel").status_code == 409
    assert counters(env.snap()) == counters(after)


def test_requeue_changes_no_metric_and_refinishing_counts_again(env):
    job, attempt = env.running(attempt_count=1, max_attempts=1)
    assert env.fail(attempt, "infra").status_code == 200
    stalled, _ = env.seed("STALLED", attempt_count=1)
    before = counters(env.snap())
    assert env.client.post(f"/api/v1/jobs/{job}/requeue").status_code == 200
    r = env.client.post("/internal/v1/jobs/requeue-stalled")
    assert r.status_code == 200 and r.json()["requeued"] == 1
    window = {
        "since": (env.sql("SELECT now() - interval '1 hour' AS t")[0]["t"]).isoformat(),
        "until": (env.sql("SELECT now() + interval '1 hour' AS t")[0]["t"]).isoformat(),
    }
    assert (
        env.client.post("/internal/v1/jobs/requeue-failed", json=window).status_code
        == 200
    )
    assert counters(env.snap()) == before


# --- gauges ------------------------------------------------------------------


def test_gauges_read_the_database_at_each_collection(env):
    for ago in (200, 50, 5):
        env.seed("QUEUED", queued_ago=ago)
    for _ in range(2):
        env.seed("STALLED", attempt_count=1)
    env.running()
    env.seed("SUCCEEDED", attempt_count=1)
    found = env.snap()
    assert get(found, m.QUEUE_DEPTH) == 3
    assert get(found, m.JOBS_STALLED) == 2
    assert get(found, m.OLDEST_QUEUED_AGE) == pytest.approx(200, abs=5)
    env.seed("QUEUED")
    assert get(env.snap(), m.QUEUE_DEPTH) == 4


def test_age_uses_queued_at_and_agrees_with_stats(env):
    env.seed("QUEUED", queued_ago=10, created_ago=3600)
    gauge = get(env.snap(), m.OLDEST_QUEUED_AGE)
    assert gauge == pytest.approx(10, abs=5)
    stats = env.client.get("/api/v1/stats").json()["oldest_queued_age_seconds"]
    assert gauge == pytest.approx(stats, abs=5)


def test_empty_queue_exports_zero_gauges(env):
    found = env.snap()
    for name in GAUGES:
        assert (name, frozenset()) in found
        assert found[(name, frozenset())].value == 0
    assert env.client.get("/api/v1/stats").json()["oldest_queued_age_seconds"] is None


def test_unreachable_database_leaves_gauges_without_data(caplog):
    url = "postgresql+psycopg://user:hunter2@127.0.0.1:1/nodb?connect_timeout=1"
    reader = InMemoryMetricReader()
    app = create_app(
        Settings(database_url=url, sweeper_enabled=False),
        meter_provider=build_meter_provider("runwatch-api", reader),
    )
    with caplog.at_level(logging.ERROR):
        found = read(reader)
    assert not {name for name, _ in found} & GAUGES
    assert (m.JOBS_SUBMITTED, frozenset()) in found
    assert "hunter2" not in caplog.text and "postgresql" not in caplog.text
    assert "OperationalError" in caplog.text
    app.state.engine.dispose()


# --- HTTP server metrics -----------------------------------------------------


def http_series(found):
    return {k: p for k, p in found.items() if k[0].startswith("http.server")}


def test_http_metrics_have_route_and_status_without_job_data(env):
    assert env.client.get("/api/v1/jobs").status_code == 200
    for _ in range(2):
        assert env.client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404
    assert env.client.get(f"/nowhere/{uuid.uuid4()}").status_code == 404
    found = env.snap()
    duration = [
        dict(attrs)
        for (name, attrs) in found
        if name in ("http.server.duration", "http.server.request.duration")
    ]
    assert {
        "http.method": "GET",
        "http.target": "/api/v1/jobs",
        "http.status_code": 200,
    } in duration
    per_status = [a for a in duration if a.get("http.target") == "/api/v1/jobs/{id}"]
    assert per_status == [
        {
            "http.method": "GET",
            "http.target": "/api/v1/jobs/{id}",
            "http.status_code": 404,
        }
    ]
    for _, attrs in http_series(found):
        for _, value in attrs:
            assert not isinstance(value, str) or not any(
                part in value for part in ("nowhere", "-")
            )


def test_probe_and_claim_routes_produce_no_http_metric(env):
    assert env.client.get("/healthz").status_code == 200
    assert env.client.get("/readyz").status_code == 200
    assert env.client.post(CLAIM, json={"worker_id": "w"}).status_code == 204
    assert http_series(env.snap()) == {}


# --- privacy -----------------------------------------------------------------


def test_attribute_keys_and_no_job_strings(env):
    job = env.client.post("/api/v1/jobs", json=BODY).json()["id"]
    claimed = env.client.post(CLAIM, json={"worker_id": "worker-secret-id"}).json()
    attempt = claimed["attempt_id"]
    env.client.post(
        f"{ATTEMPTS}/{attempt}/heartbeat",
        json={"lines": [{"ts": "2026-01-01T00:00:00+00:00", "line": SECRET_LINE}]},
    )
    env.fail(attempt)
    env.client.get(f"/api/v1/jobs/{job}")
    _, other = env.running()
    env.result(other, {"outcome": "succeeded"})
    data = env.reader.get_metrics_data()
    keys = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name.startswith("runwatch_"):
                    for p in metric.data.data_points:
                        keys |= set(p.attributes)
    assert keys <= {"outcome", "cause"}
    found = env.snap()
    for name in (m.JOBS_SUBMITTED, m.QUEUE_DEPTH, m.JOBS_STALLED, m.QUEUE_WAIT):
        assert all(attrs == frozenset() for (n, attrs) in found if n == name)
    blob = data.to_json()
    for secret in (
        SECRET_NAME,
        SECRET_LINE,
        SECRET_REASON,
        "worker-secret-id",
        job,
        attempt,
    ):
        assert secret not in blob
    resource = data.resource_metrics[0].resource.attributes
    assert resource["service.name"] == "runwatch-api"
    assert set(resource) <= {
        "service.name",
        "telemetry.sdk.language",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
    }


# --- export failure and lifecycle ---------------------------------------------


class BrokenExporter(MetricExporter):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        self.calls += 1
        raise RuntimeError("collector down")

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs):
        pass


def test_export_failure_never_breaks_requests(engine):
    exporter = BrokenExporter()
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=50, export_timeout_millis=1000
    )
    provider = build_meter_provider("runwatch-api", reader)
    app = create_app(
        Settings(database_url=DATABASE_URL, sweeper_enabled=False),
        meter_provider=provider,
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/jobs", json=BODY).status_code == 201
        claimed = client.post(CLAIM, json={"worker_id": "w"})
        assert claimed.status_code == 200
        attempt = claimed.json()["attempt_id"]
        provider.force_flush()
        r = client.post(f"{ATTEMPTS}/{attempt}/heartbeat", json={})
        assert r.status_code == 200
        r = client.post(f"{ATTEMPTS}/{attempt}/result", json={"outcome": "succeeded"})
        assert r.status_code == 200
        provider.force_flush()
    provider.shutdown()
    assert exporter.calls >= 1


class SpyProvider:
    """Wraps a real provider and records shutdown and force_flush calls."""

    def __init__(self, real):
        self.real = real
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.real, name)

    def shutdown(self, *a, **kw):
        self.calls.append("shutdown")
        return self.real.shutdown(*a, **kw)

    def force_flush(self, *a, **kw):
        self.calls.append("force_flush")
        return self.real.force_flush(*a, **kw)


def test_provider_shutdown_only_when_owned(engine, monkeypatch):
    settings = Settings(database_url=DATABASE_URL, sweeper_enabled=False)
    owned = []

    def build(name, reader=None):
        spy = SpyProvider(build_meter_provider(name, InMemoryMetricReader()))
        owned.append(spy)
        return spy

    monkeypatch.setattr(app_module, "build_meter_provider", build)
    with TestClient(create_app(settings)):
        pass
    assert owned[0].calls == ["shutdown"]

    passed = SpyProvider(build_meter_provider("runwatch-api", InMemoryMetricReader()))
    with TestClient(create_app(settings, meter_provider=passed)):
        pass
    assert "shutdown" not in passed.calls and "force_flush" in passed.calls


def test_two_apps_count_separately(engine):
    a, b = Env(engine), Env(engine)
    with a.client, b.client:
        before_b = counters(b.snap())
        assert a.client.post("/api/v1/jobs", json=BODY).status_code == 201
        assert get(a.snap(), m.JOBS_SUBMITTED) == 1
        assert get(b.snap(), m.JOBS_SUBMITTED) == 0
        assert {
            k: v for k, v in counters(b.snap()).items() if k[0] == m.JOBS_SUBMITTED
        } == {k: v for k, v in before_b.items() if k[0] == m.JOBS_SUBMITTED}
