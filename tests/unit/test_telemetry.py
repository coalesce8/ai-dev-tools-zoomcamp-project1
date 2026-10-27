"""runwatch.telemetry: provider, context propagation and span helpers."""

import pytest
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Link, StatusCode
from runwatch.telemetry import (
    build_tracer_provider,
    extract_context,
    get_tracer,
    inject_context,
    link_from,
    set_tracer_provider,
    start_span,
)

TP = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@pytest.fixture(autouse=True)
def _env_and_provider(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    yield
    set_tracer_provider(None)


def processors(provider):
    return provider._active_span_processor._span_processors


def memory_provider():
    exporter = InMemorySpanExporter()
    provider = build_tracer_provider("runwatch-api", exporter)
    set_tracer_provider(provider)
    return provider, exporter


def test_provider_resource_and_no_processor_without_endpoint():
    provider = build_tracer_provider("runwatch-api")
    assert provider.resource.attributes["service.name"] == "runwatch-api"
    assert processors(provider) == ()
    set_tracer_provider(provider)
    with get_tracer().start_as_current_span("x") as span:
        assert span.get_span_context().is_valid
    provider.shutdown()


@pytest.mark.parametrize("value", ["http://collector:4318", "http://collector:4318/"])
def test_endpoint_builds_batch_processor(monkeypatch, value):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", value)
    provider = build_tracer_provider("runwatch-worker")
    (proc,) = processors(provider)
    assert isinstance(proc, BatchSpanProcessor)
    assert proc.span_exporter._endpoint == "http://collector:4318/v1/traces"
    assert provider.resource.attributes["service.name"] == "runwatch-worker"
    provider.shutdown()


def test_explicit_exporter_wins_over_environment(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    exporter = InMemorySpanExporter()
    provider = build_tracer_provider("runwatch-api", exporter)
    (proc,) = processors(provider)
    assert isinstance(proc, SimpleSpanProcessor)
    set_tracer_provider(provider)
    with get_tracer().start_as_current_span("x"):
        pass
    assert [s.name for s in exporter.get_finished_spans()] == ["x"]


def test_inject_context_in_and_out_of_span():
    provider, _ = memory_provider()
    assert inject_context() is None
    with get_tracer().start_as_current_span("x") as span:
        carrier = inject_context()
        ctx = span.get_span_context()
    version, trace_id, span_id, flags = carrier["traceparent"].split("-")
    assert (version, int(trace_id, 16), int(span_id, 16)) == (
        "00",
        ctx.trace_id,
        ctx.span_id,
    )
    assert len(flags) == 2
    assert "tracestate" not in carrier


def test_link_from_valid_carrier():
    (link,) = link_from({"traceparent": TP})
    assert isinstance(link, Link)
    assert format(link.context.trace_id, "032x") == TP.split("-")[1]
    assert format(link.context.span_id, "016x") == TP.split("-")[2]


@pytest.mark.parametrize(
    "carrier",
    [
        None,
        {},
        {"tracestate": "a=b"},
        {"traceparent": "garbage"},
        {"traceparent": 5},
        {"traceparent": "00-00000000000000000000000000000000-0000000000000000-01"},
        "traceparent",
        ["x"],
    ],
)
def test_link_from_bad_carrier(carrier):
    assert link_from(carrier) == []
    assert extract_context(carrier) is None


def test_get_tracer_follows_last_provider():
    assert get_tracer() is not None
    with get_tracer().start_as_current_span("noop") as span:
        assert not span.is_recording()
    _, first = memory_provider()
    with get_tracer().start_as_current_span("a"):
        pass
    _, second = memory_provider()
    with get_tracer().start_as_current_span("b"):
        pass
    assert [s.name for s in first.get_finished_spans()] == ["a"]
    assert [s.name for s in second.get_finished_spans()] == ["b"]


def test_start_span_hides_exception_messages():
    _, exporter = memory_provider()
    with pytest.raises(RuntimeError):
        with start_span("boom"):
            raise RuntimeError("postgresql://user:pw@host/db")
    with pytest.raises(KeyError):
        with start_span("fine", expected=(KeyError,)):
            raise KeyError("x")
    boom, fine = exporter.get_finished_spans()
    assert boom.status.status_code == StatusCode.ERROR
    assert boom.status.description is None
    assert boom.events == ()
    assert fine.status.status_code == StatusCode.UNSET
    assert "pw@host" not in boom.to_json()
