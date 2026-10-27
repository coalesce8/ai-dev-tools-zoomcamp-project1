"""Submit, claim, attempt and sweeper traces against a real database.

In-process TestClient, in-memory exporter: no port, no collector, no network.
"""

import contextvars
import os
import random
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode
from runwatch import telemetry
from runwatch.api import worker_claim, worker_report
from runwatch.api.app import create_app
from runwatch.models import Attempt, Job
from runwatch.settings import Settings
from runwatch.sweeper import LeaseSweeper
from runwatch.telemetry import build_tracer_provider, set_tracer_provider
from runwatch.worker.client import HttpWorkerApi
from runwatch.worker.runner import Worker
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET_NAME = "secret-job-name-xyz"
SECRET_LINE = "distinctive-log-line-qrs"
BODY = {"name": SECRET_NAME, "spec": {"duration_s": 2, "fail_probability": 0.0}}
TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
CLAIM = "/internal/v1/worker/claim"


@pytest.fixture(scope="module", autouse=True)
def _schema():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def _clean_tables(_schema):
    from sqlalchemy import create_engine

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))
    engine.dispose()
    yield
    set_tracer_provider(None)


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


class IsolatedClient(TestClient):
    """A TestClient that sends each request from an empty contextvars context.

    The in-process app would otherwise inherit the caller's active span, so the
    server span would be a child of it without reading any traceparent header.
    A real server never sees the caller's context: the header is all it has.
    """

    def request(self, *args, **kwargs):
        return contextvars.Context().run(TestClient.request, self, *args, **kwargs)


def make_client(provider=None, exporter=None):
    if provider is None:
        provider = build_tracer_provider("runwatch-api", exporter)
    app = create_app(
        Settings(database_url=DATABASE_URL, sweeper_enabled=False),
        tracer_provider=provider,
    )
    return IsolatedClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(exporter):
    with make_client(exporter=exporter) as c:
        yield c


@pytest.fixture
def session(client):
    with Session(client.app.state.engine) as s:
        yield s


def spans(exporter, name=None):
    found = exporter.get_finished_spans()
    return [s for s in found if name is None or s.name == name]


def server_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]


def all_text(exporter):
    return "".join(s.to_json() for s in exporter.get_finished_spans())


def submit(client, body=BODY):
    r = client.post("/api/v1/jobs", json=body)
    assert r.status_code == 201
    return r.json()


def claim(client):
    return client.post(CLAIM, json={"worker_id": "w-1"})


# -- submit ---------------------------------------------------------------


def test_submit_trace(client, exporter, session):
    updates = []
    engine = client.app.state.engine

    def capture(conn, cursor, statement, *args):
        updates.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        body = submit(client)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not [s for s in updates if s.lstrip().upper().startswith("UPDATE")]

    (submit_span,) = spans(exporter, "submit")
    (server,) = server_spans(exporter)
    assert server.name == "POST /api/v1/jobs"
    assert submit_span.parent.span_id == server.context.span_id
    assert submit_span.attributes["job.id"] == body["id"]
    assert server.attributes["job.id"] == body["id"]

    job = session.get(Job, uuid.UUID(body["id"]))
    match = TRACEPARENT_RE.match(job.trace_context["traceparent"])
    assert set(job.trace_context) == {"traceparent"}
    assert int(match[1], 16) == submit_span.context.trace_id
    assert int(match[2], 16) == submit_span.context.span_id

    assert "trace_context" not in body
    got = client.get(f"/api/v1/jobs/{body['id']}").json()
    assert "trace_context" not in got
    assert "trace_context" not in client.get("/api/v1/jobs").text


def test_rejected_submit_has_no_submit_span(client, exporter, session):
    r = client.post("/api/v1/jobs", json={"name": ""})
    assert r.status_code == 400
    assert spans(exporter, "submit") == []
    assert session.query(Job).count() == 0


def test_trace_context_stored_without_exporter():
    provider = build_tracer_provider("runwatch-api")
    with make_client(provider=provider) as client:
        body = submit(client)
        with Session(client.app.state.engine) as s:
            job = s.get(Job, uuid.UUID(body["id"]))
            assert TRACEPARENT_RE.match(job.trace_context["traceparent"])


# -- claim ------------------------------------------------------------------


def test_claim_span_is_child_of_submit_span(client, exporter):
    body = submit(client)
    (submit_span,) = spans(exporter, "submit")
    r = claim(client)
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {
        "job_id", "attempt_id", "attempt_number", "spec",
        "lease_expires_at", "trace_context",
    }  # fmt: skip
    assert TRACEPARENT_RE.match(data["trace_context"]["traceparent"])
    (span,) = spans(exporter, "claim")
    assert span.attributes == {"job.id": body["id"], "attempt.number": 1}
    assert isinstance(span.attributes["attempt.number"], int)
    assert span.context.trace_id == submit_span.context.trace_id
    assert span.parent.span_id == submit_span.context.span_id
    assert server_spans(exporter)[-1].name != "POST /internal/v1/worker/claim"
    assert len([s for s in server_spans(exporter)]) == 1  # the submit request only


@pytest.mark.parametrize("stored", [None, {"traceparent": "garbage"}, {"x": 1}])
def test_claim_without_usable_context_is_root(client, exporter, session, stored):
    job = Job(name="n", spec=BODY["spec"], trace_context=stored)
    session.add(job)
    session.commit()
    r = claim(client)
    assert r.status_code == 200
    assert r.json()["trace_context"] == stored
    (span,) = spans(exporter, "claim")
    assert span.parent is None
    assert span.status.status_code != StatusCode.ERROR


def test_idle_claim_and_probes_create_no_spans(client, exporter):
    assert claim(client).status_code == 204
    client.get("/healthz")
    client.get("/readyz")
    assert exporter.get_finished_spans() == ()


# -- worker over the API ----------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def monotonic(self):
        return self.t

    def utcnow(self):
        return datetime(2026, 1, 1, tzinfo=UTC)


def run_worker(client, exporter, session, stop_after_idle=True):
    clock = Clock()
    stop = threading.Event()
    api = HttpWorkerApi("http://testserver", client=client)
    real_claim = api.claim

    def claim_then_stop(worker_id):
        result = real_claim(worker_id)
        if result is None:
            stop.set()
        return result

    api.claim = claim_then_stop
    worker = Worker(
        api,
        "w-1",
        stop=stop,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
        rng=random.Random(1),
        tracer=telemetry.get_tracer(),
    )
    worker.run()


def test_worker_propagates_context_to_the_api(client, exporter, session):
    body = submit(client)
    run_worker(client, exporter, session)
    session.rollback()
    assert session.get(Job, uuid.UUID(body["id"])).state == "SUCCEEDED"

    (submit_span,) = spans(exporter, "submit")
    (attempt,) = spans(exporter, "attempt")
    (run,) = spans(exporter, "run")
    (result,) = spans(exporter, "result")
    assert attempt.context.trace_id != submit_span.context.trace_id
    assert attempt.links[0].context.span_id == submit_span.context.span_id

    heartbeats = [s for s in server_spans(exporter) if "heartbeat" in s.name]
    results = [s for s in server_spans(exporter) if s.name.endswith("/result")]
    assert len(heartbeats) >= 2 and len(results) == 1
    for s in heartbeats:
        assert s.context.trace_id == attempt.context.trace_id
        assert s.parent.span_id == run.context.span_id
    (r,) = results
    assert r.context.trace_id == attempt.context.trace_id
    assert r.parent.span_id == result.context.span_id
    for s in heartbeats + results:
        assert s.attributes["job.id"] == body["id"]
        assert s.attributes["attempt.number"] == 1
    # The submit trace holds no worker request.
    in_submit = [
        s
        for s in exporter.get_finished_spans()
        if s.context.trace_id == submit_span.context.trace_id
    ]
    assert not [s for s in in_submit if "heartbeat" in s.name or "result" in s.name]


def test_409_server_span_keeps_attributes(client, exporter, session):
    body = submit(client)
    run_worker(client, exporter, session)
    attempt_id = session.query(Attempt).one().id
    exporter.clear()
    r = client.post(
        f"/internal/v1/worker/attempts/{attempt_id}/heartbeat", json={"lines": []}
    )
    assert r.status_code == 409
    r = client.post(
        f"/internal/v1/worker/attempts/{attempt_id}/result",
        json={"outcome": "succeeded"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"]
    stale = server_spans(exporter)
    assert len(stale) == 2
    for s in stale:
        assert s.attributes["job.id"] == body["id"]
        assert s.attributes["attempt.number"] == 1


# -- sweeper ---------------------------------------------------------------


def test_sweeper_emits_lease_expire_spans(client, exporter, session):
    a, b = submit(client), submit(client)
    assert claim(client).status_code == 200
    assert claim(client).status_code == 200
    session.execute(
        text("UPDATE attempts SET lease_expires_at = :t"),
        {"t": datetime.now(UTC) - timedelta(seconds=60)},
    )
    session.commit()
    exporter.clear()
    sweeper = LeaseSweeper(client.app.state.session_factory)
    assert len(sweeper.run_once()) == 2
    found = spans(exporter, "lease.expire")
    assert len(found) == 2
    assert {s.attributes["job.id"] for s in found} == {a["id"], b["id"]}
    assert all(s.attributes["attempt.number"] == 1 for s in found)
    assert all(s.parent is None for s in found)
    exporter.clear()
    assert sweeper.run_once() == []
    assert exporter.get_finished_spans() == ()


# -- privacy and failure handling ---------------------------------------------


def test_no_job_name_or_log_text_in_any_span(client, exporter, session):
    submit(client)
    run_worker(client, exporter, session)
    # A heartbeat with a distinctive line, for a fresh running attempt.
    submit(client)
    data = claim(client).json()
    r = client.post(
        f"/internal/v1/worker/attempts/{data['attempt_id']}/heartbeat",
        json={"lines": [{"ts": "2026-01-01T00:00:00Z", "line": SECRET_LINE}]},
    )
    assert r.status_code == 200
    blob = all_text(exporter)
    assert SECRET_NAME not in blob
    assert SECRET_LINE not in blob
    assert len(spans(exporter)) > 5


def test_exception_message_never_reaches_a_span(client, exporter, monkeypatch):
    submit(client)
    secret = "postgresql://user:pw@host/db"

    def boom(*a, **k):
        raise RuntimeError(secret)

    monkeypatch.setattr(worker_claim, "claim_next", boom)
    assert claim(client).status_code == 500
    monkeypatch.undo()
    data = claim(client).json()
    monkeypatch.setattr(worker_report, "succeed", boom)
    r = client.post(
        f"/internal/v1/worker/attempts/{data['attempt_id']}/result",
        json={"outcome": "succeeded"},
    )
    assert r.status_code == 500
    assert secret not in all_text(exporter)
    assert "pw@host" not in all_text(exporter)


class BrokenExporter(SpanExporter):
    def export(self, spans):
        raise ConnectionError("collector unreachable")

    def shutdown(self):
        pass


def test_failing_exporter_never_breaks_requests_or_worker(session):
    provider = build_tracer_provider("runwatch-api", BrokenExporter())
    with make_client(provider=provider) as client:
        body = submit(client)
        run_worker(client, None, session)
        session.rollback()
        assert session.get(Job, uuid.UUID(body["id"])).state == "SUCCEEDED"


def test_lifespan_shuts_down_only_its_own_provider(exporter, monkeypatch):
    passed = build_tracer_provider("runwatch-api", exporter)
    calls = []
    monkeypatch.setattr(passed, "shutdown", lambda: calls.append("shutdown"))
    monkeypatch.setattr(passed, "force_flush", lambda *a: calls.append("flush"))
    with make_client(provider=passed):
        pass
    assert calls == ["flush"]

    built = build_tracer_provider("runwatch-api")
    monkeypatch.setattr("runwatch.api.app.build_tracer_provider", lambda name: built)
    owned_calls = []
    monkeypatch.setattr(built, "shutdown", lambda: owned_calls.append("shutdown"))
    app = create_app(Settings(database_url=DATABASE_URL, sweeper_enabled=False))
    with TestClient(app):
        assert owned_calls == []
    assert owned_calls == ["shutdown"]
