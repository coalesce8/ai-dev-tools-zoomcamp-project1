"""AC-18: no job name, job log text, API key or connection string in telemetry.

One scenario touches every telemetry-emitting path of the API, the sweeper and
the worker, with in-memory exporters for spans, metrics and logs (plus the
stdout JSON lines). The captured text of each signal is scanned for canary
strings that no Runwatch code can produce by itself. Negative controls prove
the scanner reports a leak when there is one.

In-process TestClient: no port, no collector, no network.
"""

import contextvars
import io
import json
import logging
import os
import random
import re
import threading
from datetime import UTC, datetime, timedelta

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from runwatch import metrics as m
from runwatch import telemetry
from runwatch.api import worker_claim
from runwatch.api.app import create_app
from runwatch.logs import configure_logging
from runwatch.settings import Settings
from runwatch.sweeper import LeaseSweeper
from runwatch.telemetry import (
    build_logger_provider,
    build_meter_provider,
    build_tracer_provider,
    set_tracer_provider,
)
from runwatch.worker.client import HttpWorkerApi
from runwatch.worker.runner import Worker
from sqlalchemy import create_engine, text

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CANARY_NAME = "CANARY-NAME-8f31c"
CANARY_LINE = "CANARY-LOGLINE-5d2a7"
CANARY_REASON = "CANARY-REASON-91be4"
CANARY_KEY = "sk-ant-CANARY-KEY-3c9e0"
CANARY_CONN = "postgresql://leakuser:CANARY-PW-77ab1@db.invalid/leakdb"
CANARY_PW = "CANARY-PW-77ab1"

CLAIM = "/internal/v1/worker/claim"
ATTEMPTS = "/internal/v1/worker/attempts"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UUID_IN_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

# Metric attribute keys that may appear: the job metrics and the HTTP server
# metrics the instrumentation emits (see telemetry._HTTP_METRIC_ATTRIBUTES).
ALLOWED_METRIC_KEYS = {
    "outcome",
    "cause",
    "http.method",
    "http.target",
    "http.status_code",
    "http.request.method",
    "http.route",
    "http.response.status_code",
}
METRIC_NAMES = {
    m.JOBS_SUBMITTED,
    m.JOBS_FINISHED,
    m.ATTEMPTS_FINISHED,
    m.QUEUE_DEPTH,
    m.OLDEST_QUEUED_AGE,
    m.JOBS_STALLED,
    m.QUEUE_WAIT,
    m.JOB_RUN,
}


@pytest.fixture(scope="module", autouse=True)
def _schema():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def _clean_tables(_schema):
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))
    engine.dispose()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    set_tracer_provider(None)


class IsolatedClient(TestClient):
    """Each request starts from an empty contextvars context, like a real server."""

    def request(self, *args, **kwargs):
        return contextvars.Context().run(TestClient.request, self, *args, **kwargs)


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def monotonic(self):
        return self.t

    def utcnow(self):
        return datetime(2026, 1, 1, tzinfo=UTC)


# -- the scanner -------------------------------------------------------------


def find_leaks(blob: str, canaries: dict[str, str]) -> list[str]:
    """Labels of the canaries found in `blob`. Never returns the text itself."""
    return [label for label, value in canaries.items() if value in blob]


def span_text(spans) -> str:
    parts = []
    for s in spans:
        parts.append(s.to_json())
        parts.append(str(s.instrumentation_scope))
        parts.append(json.dumps([dict(link.attributes or {}) for link in s.links]))
    return "\n".join(parts)


def metric_text(reader) -> str:
    data = reader.get_metrics_data()
    return data.to_json() if data else ""


def log_text(records) -> str:
    parts = []
    for r in records:
        parts.append(r.to_json())
        parts.append(str(r.instrumentation_scope))
    return "\n".join(parts)


def metric_points(reader):
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    yield metric.name, dict(point.attributes)


# -- the scenario ------------------------------------------------------------


class Capture:
    def __init__(self):
        self.span_exporter = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        self.log_exporter = InMemoryLogRecordExporter()
        self.stdout = io.StringIO()
        self.tracer_provider = build_tracer_provider("runwatch-api", self.span_exporter)
        self.meter_provider = build_meter_provider("runwatch-api", self.reader)
        self.logger_provider = build_logger_provider("runwatch-api", self.log_exporter)

    def flush(self):
        self.tracer_provider.force_flush()
        self.meter_provider.force_flush()
        self.logger_provider.force_flush()

    @property
    def spans(self):
        return self.span_exporter.get_finished_spans()

    @property
    def records(self):
        return self.log_exporter.get_finished_logs()


def run_scenario(monkeypatch):
    cap = Capture()
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY_KEY)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app = create_app(
        Settings(database_url=DATABASE_URL, sweeper_enabled=False),
        tracer_provider=cap.tracer_provider,
        meter_provider=cap.meter_provider,
        logger_provider=cap.logger_provider,
    )
    # create_app installed the handlers; capture stdout through the same pipeline.
    configure_logging("runwatch-api", cap.logger_provider, stream=cap.stdout)
    ids = {}
    with IsolatedClient(app, raise_server_exceptions=False) as client:

        def submit(name, **extra):
            body = {"name": name, "spec": {"duration_s": 2, "fail_probability": 0.0}}
            body.update(extra)
            r = client.post("/api/v1/jobs", json=body)
            assert r.status_code == 201
            return r.json()["id"]

        # Job A: failed with a workload cause, retried, then succeeds.
        ids["a"] = submit(CANARY_NAME, max_attempts=2)
        first = client.post(CLAIM, json={"worker_id": "w-1"}).json()
        r = client.post(
            f"{ATTEMPTS}/{first['attempt_id']}/heartbeat",
            json={"lines": [{"ts": "2026-01-01T00:00:00Z", "line": CANARY_LINE}]},
        )
        assert r.status_code == 200
        r = client.post(
            f"{ATTEMPTS}/{first['attempt_id']}/result",
            json={"outcome": "failed", "reason": CANARY_REASON, "cause": "workload"},
        )
        assert r.status_code == 200 and r.json()["job_state"] == "QUEUED"
        second = client.post(CLAIM, json={"worker_id": "w-1"}).json()
        assert second["job_id"] == ids["a"]
        r = client.post(
            f"{ATTEMPTS}/{second['attempt_id']}/result", json={"outcome": "succeeded"}
        )
        assert r.status_code == 200

        # Job W: run by a real Worker over HttpWorkerApi (attempt, run, result).
        ids["w"] = submit(CANARY_NAME)
        stop = threading.Event()
        api = HttpWorkerApi("http://testserver", client=client)
        real_claim = api.claim

        def claim_then_stop(worker_id):
            result = real_claim(worker_id)
            if result is None:
                stop.set()
            return result

        api.claim = claim_then_stop
        clock = Clock()
        Worker(
            api,
            "w-2",
            stop=stop,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            utcnow=clock.utcnow,
            rng=random.Random(1),
            tracer=telemetry.get_tracer(),
        ).run()

        # Job C: cancelled.
        ids["c"] = submit(CANARY_NAME)
        assert client.post(f"/api/v1/jobs/{ids['c']}/cancel").status_code == 200

        # Job S: claimed, then its lease expires and the sweeper stalls it.
        ids["s"] = submit(CANARY_NAME)
        assert client.post(CLAIM, json={"worker_id": "w-3"}).status_code == 200
        engine = app.state.engine
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE attempts SET lease_expires_at = :t WHERE job_id = :j"),
                {"t": datetime.now(UTC) - timedelta(seconds=60), "j": ids["s"]},
            )
        swept = LeaseSweeper(
            app.state.session_factory, metrics=app.state.metrics
        ).run_once()
        assert [str(s.job_id) for s in swept] == [ids["s"]]

        # Reads.
        assert client.get("/api/v1/jobs").status_code == 200
        assert client.get(f"/api/v1/jobs/{ids['a']}").status_code == 200
        r = client.get(f"/api/v1/jobs/{ids['a']}/logs")
        assert r.status_code == 200
        assert client.get("/api/v1/stats").status_code == 200

        # A 400 whose body carries the canary name.
        r = client.post(
            "/api/v1/jobs", json={"name": CANARY_NAME, "spec": {"duration_s": -1}}
        )
        assert r.status_code == 400

        # A 500 whose exception message is a connection string.
        def boom(*args, **kwargs):
            raise RuntimeError(CANARY_CONN)

        monkeypatch.setattr(worker_claim, "claim_next", boom)
        assert client.post(CLAIM, json={"worker_id": "w-4"}).status_code == 500
        monkeypatch.undo()
        monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY_KEY)
    cap.flush()
    return cap, ids


@pytest.fixture
def scenario(monkeypatch):
    return run_scenario(monkeypatch)


def canaries():
    return {
        "job name": CANARY_NAME,
        "job log line": CANARY_LINE,
        "result reason": CANARY_REASON,
        "API key": CANARY_KEY,
        "connection string": CANARY_CONN,
        "connection string password": CANARY_PW,
        "DATABASE_URL": DATABASE_URL,
    }


# -- AC-18 -------------------------------------------------------------------


def test_no_canary_in_any_signal(scenario):
    cap, _ = scenario
    signals = {
        "spans": span_text(cap.spans),
        "metrics": metric_text(cap.reader),
        "logs": log_text(cap.records),
        "stdout": cap.stdout.getvalue(),
    }
    found = {name: find_leaks(blob, canaries()) for name, blob in signals.items()}
    leaks = [f"{name}: {', '.join(labels)}" for name, labels in found.items() if labels]
    assert not leaks, "canary found in telemetry: " + "; ".join(leaks)


def test_capture_is_not_vacuous(scenario):
    cap, ids = scenario
    names = {s.name for s in cap.spans}
    assert {"submit", "claim", "attempt", "run", "result", "lease.expire"} <= names
    metric_names = {name for name, _ in metric_points(cap.reader)}
    assert METRIC_NAMES <= metric_names
    assert any(n.startswith("http.server") for n in metric_names)

    with_job = [r for r in cap.records if "job_id" in r.log_record.attributes]
    without_job = [r for r in cap.records if "job_id" not in r.log_record.attributes]
    assert with_job and without_job
    for r in with_job:
        assert UUID_RE.match(r.log_record.attributes["job_id"])

    job_id = ids["a"]
    assert any(s.attributes.get("job.id") == job_id for s in cap.spans)
    assert any(r.log_record.attributes.get("job_id") == job_id for r in cap.records)
    assert job_id not in metric_text(cap.reader)
    stdout = [json.loads(line) for line in cap.stdout.getvalue().splitlines()]
    assert any(line.get("job_id") == job_id for line in stdout)
    assert len(stdout) >= len(cap.records) - 5


def test_logs_correlate_with_spans(scenario):
    cap, _ = scenario
    by_span = {(s.context.trace_id, s.context.span_id) for s in cap.spans}
    correlated = [
        r
        for r in cap.records
        if r.log_record.trace_id
        and r.log_record.span_id
        and (r.log_record.trace_id, r.log_record.span_id) in by_span
    ]
    assert correlated
    # And on stdout, the same ids in hex.
    hexes = {(format(t, "032x"), format(s, "016x")) for t, s in by_span}
    lines = [json.loads(line) for line in scenario[0].stdout.getvalue().splitlines()]
    assert any((x.get("trace_id"), x.get("span_id")) in hexes for x in lines)


def test_metric_attribute_keys_are_allow_listed(scenario):
    cap, _ = scenario
    for name, attrs in metric_points(cap.reader):
        for key, value in attrs.items():
            assert key in ALLOWED_METRIC_KEYS, f"metric {name} has label {key!r}"
            if key in ("http.route", "http.target"):
                assert not UUID_IN_TEXT.search(str(value)), (
                    f"metric {name} label {key} holds a concrete path"
                )


def test_api_log_records_carry_their_span_ids(scenario):
    cap, ids = scenario
    # The claim route has no server span (idle polling is untraced); its record
    # is written inside the `claim` span instead.
    server = {
        (s.context.trace_id, s.context.span_id): s
        for s in cap.spans
        if s.kind.name == "SERVER" or s.name == "claim"
    }
    for message in ("claimed job_id", "heartbeat job_id", "result job_id"):
        found = [
            r
            for r in cap.records
            if str(r.log_record.body).startswith(message)
            and r.log_record.attributes.get("job_id") == ids["a"]
        ]
        assert found, message
        for r in found:
            span = server[(r.log_record.trace_id, r.log_record.span_id)]
            assert span.attributes["job.id"] == ids["a"]


def test_worker_records_carry_the_attempt_trace(scenario):
    cap, ids = scenario
    attempt = next(
        s
        for s in cap.spans
        if s.name == "attempt" and s.attributes["job.id"] == ids["w"]
    )
    mine = [
        r
        for r in cap.records
        if r.log_record.attributes.get("job_id") == ids["w"]
        and r.log_record.attributes.get("worker_id") == "w-2"
        and str(r.log_record.body).startswith(("attempt started", "result sent"))
    ]
    assert len(mine) == 2
    assert {r.log_record.trace_id for r in mine} <= {attempt.context.trace_id} | {0}
    sent = next(r for r in mine if str(r.log_record.body).startswith("result sent"))
    assert sent.log_record.trace_id == attempt.context.trace_id


# -- negative controls: the scanner reports a leak --------------------------


def test_control_log_record_with_job_name_is_reported():
    exporter = InMemoryLogRecordExporter()
    provider = build_logger_provider("runwatch-api", exporter)
    stream = io.StringIO()
    configure_logging("runwatch-api", provider, stream=stream)
    logging.getLogger("control").info("job %s", CANARY_NAME)
    provider.force_flush()
    assert find_leaks(log_text(exporter.get_finished_logs()), canaries()) == [
        "job name"
    ]
    assert find_leaks(stream.getvalue(), canaries()) == ["job name"]


def test_control_span_with_canary_attribute_is_reported():
    exporter = InMemorySpanExporter()
    provider = build_tracer_provider("runwatch-api", exporter)
    with provider.get_tracer("control").start_as_current_span(
        "leaky", attributes={"job.name": CANARY_NAME}
    ):
        pass
    assert find_leaks(span_text(exporter.get_finished_spans()), canaries()) == [
        "job name"
    ]


def test_control_metric_label_outside_the_allow_list_is_reported():
    reader = InMemoryMetricReader()
    provider = build_meter_provider("runwatch-api", reader)
    provider.get_meter("control").create_counter("leaky").add(
        1, {"job_name": CANARY_NAME}
    )
    points = list(metric_points(reader))
    assert any(key not in ALLOWED_METRIC_KEYS for _, a in points for key in a)
    assert find_leaks(metric_text(reader), canaries()) == ["job name"]
