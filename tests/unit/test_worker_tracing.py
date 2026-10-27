"""Worker attempt traces and trace-context headers (no database, no network)."""

import random
import threading
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from runwatch.api.job_schemas import WorkloadSpec
from runwatch.telemetry import build_tracer_provider, set_tracer_provider
from runwatch.worker.client import (
    HTTP_TIMEOUT_S,
    ApiUnavailable,
    AttemptNotCurrent,
    Claim,
    HttpWorkerApi,
    LogLine,
)
from runwatch.worker.runner import Worker

T = "0af7651916cd43dd8448eb211c80319c"
S = "b7ad6b7169203331"
TRACEPARENT = f"00-{T}-{S}-01"


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def monotonic(self):
        return self.t

    def utcnow(self):
        return datetime(2026, 1, 1, tzinfo=UTC)


class FakeApi:
    def __init__(self, stop, claims=()):
        self.stop = stop
        self.claims = list(claims)
        self.heartbeat_fn = lambda n: None
        self.result_fn = lambda n: None
        self.heartbeats = 0
        self.results = 0
        self.claim_calls = 0

    def claim(self, worker_id):
        self.claim_calls += 1
        if not self.claims:
            self.stop.set()
            return None
        return self.claims.pop(0)

    def heartbeat(self, attempt_id, lines):
        self.heartbeats += 1
        self.heartbeat_fn(self.heartbeats)

    def result(self, attempt_id, outcome, reason=None, cause=None):
        self.results += 1
        self.result_fn(self.results)


def make_claim(trace_context, number=1, job_id=None, duration_s=2):
    return Claim(
        job_id=job_id or uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        attempt_number=number,
        spec=WorkloadSpec(duration_s=duration_s, mode="normal", fail_probability=0.0),
        trace_context=trace_context,
    )


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = build_tracer_provider("runwatch-worker", exporter)
    set_tracer_provider(provider)
    yield exporter
    set_tracer_provider(None)


def build(exporter, claims=()):
    clock = Clock()
    stop = threading.Event()
    api = FakeApi(stop, claims)
    provider = build_tracer_provider("runwatch-worker", exporter)
    worker = Worker(
        api,
        "w-1",
        stop=stop,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
        rng=random.Random(1),
        tracer=provider.get_tracer("test"),
    )
    return worker, api, clock


def by_name(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def test_attempt_trace_structure(exporter):
    claim = make_claim({"traceparent": TRACEPARENT})
    worker, api, _ = build(exporter, [claim])
    worker.run()
    (attempt,) = by_name(exporter, "attempt")
    (run,) = by_name(exporter, "run")
    (result,) = by_name(exporter, "result")
    assert len(exporter.get_finished_spans()) == 3
    assert attempt.parent is None
    assert format(attempt.context.trace_id, "032x") != T
    (link,) = attempt.links
    assert format(link.context.trace_id, "032x") == T
    assert format(link.context.span_id, "016x") == S
    for child in (run, result):
        assert child.context.trace_id == attempt.context.trace_id
        assert child.parent.span_id == attempt.context.span_id
        assert child.links == ()
    for span in (attempt, run, result):
        assert span.attributes == {
            "job.id": str(claim.job_id),
            "attempt.number": 1,
        }
    assert run.end_time <= result.start_time


def test_result_span_covers_retries(exporter):
    worker, api, _ = build(exporter, [make_claim({"traceparent": TRACEPARENT})])

    def flaky(n):
        if n <= 2:
            raise ApiUnavailable("x")

    api.result_fn = flaky
    worker.run()
    assert api.results == 3
    assert len(by_name(exporter, "result")) == 1


@pytest.mark.parametrize(
    "context", [None, {}, {"traceparent": "garbage"}, {"traceparent": 7}]
)
def test_missing_or_malformed_context(exporter, context):
    worker, api, _ = build(exporter, [make_claim(context)])
    worker.run()
    (attempt,) = by_name(exporter, "attempt")
    assert attempt.links == ()
    assert api.results == 1


def test_each_attempt_is_its_own_trace(exporter):
    job_id = uuid.uuid4()
    ctx = {"traceparent": TRACEPARENT}
    claims = [make_claim(ctx, 1, job_id), make_claim(ctx, 2, job_id)]
    worker, _, _ = build(exporter, claims)
    worker.run()
    first, second = sorted(
        by_name(exporter, "attempt"), key=lambda s: s.attributes["attempt.number"]
    )
    assert first.context.trace_id != second.context.trace_id
    assert [
        first.attributes["attempt.number"],
        second.attributes["attempt.number"],
    ] == [
        1,
        2,
    ]
    for span in (first, second):
        assert span.parent is None
        assert span.links[0].context.span_id == int(S, 16)


def assert_nothing_open(exporter):
    assert trace.get_current_span().get_span_context().is_valid is False
    for span in exporter.get_finished_spans():
        assert span.end_time is not None


def test_cancelled_attempt_ends_spans_without_result(exporter):
    ctx = {"traceparent": TRACEPARENT}
    worker, api, _ = build(exporter, [make_claim(ctx, 1), make_claim(ctx, 2)])
    seen = []

    def hb(n):
        seen.append(n)
        if n == 1:
            raise AttemptNotCurrent

    api.heartbeat_fn = hb
    worker.run()
    assert api.results == 1  # only the second attempt reports
    assert len(by_name(exporter, "attempt")) == 2
    assert len(by_name(exporter, "result")) == 1
    first = min(by_name(exporter, "attempt"), key=lambda s: s.start_time)
    assert first.status.status_code.name == "UNSET"
    assert all(s.parent is None for s in by_name(exporter, "attempt"))
    assert_nothing_open(exporter)


def test_abandoned_attempt_ends_spans(exporter):
    worker, api, _ = build(
        exporter, [make_claim({"traceparent": TRACEPARENT}, duration_s=100)]
    )

    def hb(n):
        raise ApiUnavailable("x")

    api.heartbeat_fn = hb
    worker.run()
    assert api.results == 0
    assert len(by_name(exporter, "attempt")) == 1
    assert len(by_name(exporter, "run")) == 1
    assert by_name(exporter, "result") == []
    assert_nothing_open(exporter)


def test_idle_polling_creates_no_spans(exporter):
    worker, api, clock = build(exporter, [])
    worker.run()
    assert api.claim_calls == 1
    assert exporter.get_finished_spans() == ()


def test_no_message_in_spans_when_work_raises(exporter):
    claim = make_claim({"traceparent": TRACEPARENT})
    worker, api, _ = build(exporter, [claim])

    def boom(n):
        raise RuntimeError("postgresql://user:pw@host/db")

    api.heartbeat_fn = boom
    worker.run()
    assert "postgresql" not in "".join(
        s.to_json() for s in exporter.get_finished_spans()
    )
    assert api.results == 1


# -- header propagation ----------------------------------------------------


def http_api(provider):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=HTTP_TIMEOUT_S
    )
    return HttpWorkerApi("http://h:8000", client=client), requests


def test_heartbeat_and_result_send_traceparent(exporter):
    provider = build_tracer_provider("runwatch-worker", exporter)
    tracer = provider.get_tracer("test")
    api, requests = http_api(provider)
    aid = uuid.uuid4()
    with tracer.start_as_current_span("run") as span:
        api.heartbeat(aid, [LogLine(datetime(2026, 1, 1, tzinfo=UTC), "x")])
        api.result(aid, "succeeded")
        ctx = span.get_span_context()
    for request in requests:
        _, trace_id, span_id, _ = request.headers["traceparent"].split("-")
        assert int(trace_id, 16) == ctx.trace_id
        assert int(span_id, 16) == ctx.span_id


def test_claim_sends_no_traceparent_even_inside_a_span(exporter):
    provider = build_tracer_provider("runwatch-worker", exporter)
    api, requests = http_api(provider)

    def handler(request):
        requests.append(request)
        return httpx.Response(204)

    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    with provider.get_tracer("test").start_as_current_span("x"):
        assert api.claim("w") is None
    assert "traceparent" not in requests[-1].headers


def test_no_header_without_span(exporter):
    api, requests = http_api(None)
    api.heartbeat(uuid.uuid4(), [])
    api.result(uuid.uuid4(), "succeeded")
    assert all("traceparent" not in r.headers for r in requests)
