"""Tracing helpers shared by the API, the sweeper and the worker.

Spans are always created; they are exported only when
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (OTLP over HTTP) or an exporter is
passed in (tests). The module keeps its own provider holder instead of
OpenTelemetry's global one, which can be set only once per process.

Privacy: span names are fixed strings and attributes are ``job.id`` and
``attempt.number`` only. Manual spans never record exception messages, which
can hold a connection string; a failure only sets status ERROR.

This module imports no FastAPI, SQLAlchemy or ``runwatch.api``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Link, Span, Status, StatusCode, Tracer
from pydantic_settings import BaseSettings, SettingsConfigDict

API_SERVICE = "runwatch-api"
WORKER_SERVICE = "runwatch-worker"

ATTR_JOB_ID = "job.id"
ATTR_ATTEMPT_NUMBER = "attempt.number"

_TELEMETRY_SCOPE = "runwatch"

# The spec fixes the metric export interval.
METRIC_EXPORT_INTERVAL_MS = 10_000

_provider: TracerProvider | None = None


class _TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    otel_exporter_otlp_endpoint: str | None = None


def build_tracer_provider(
    service_name: str, exporter: SpanExporter | None = None
) -> TracerProvider:
    """Build a provider for one process.

    An explicit exporter is used synchronously. Otherwise the OTLP/HTTP
    exporter is added when the endpoint variable is set; with neither, there
    is no span processor and nothing is exported.
    """
    provider = TracerProvider(resource=Resource({SERVICE_NAME: service_name}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        return provider
    endpoint = (_TelemetrySettings().otel_exporter_otlp_endpoint or "").strip()
    if endpoint:
        url = endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url)))
    return provider


# HTTP server metrics keep method, route template and status code only. The
# instrumentation's defaults also carry the Host header, which a client picks.
_HTTP_METRIC_ATTRIBUTES = {
    "http.method",
    "http.target",
    "http.status_code",
    "http.request.method",
    "http.route",
    "http.response.status_code",
}
_VIEWS = [View(instrument_name="http.server.*", attribute_keys=_HTTP_METRIC_ATTRIBUTES)]


def build_meter_provider(
    service_name: str, reader: MetricReader | None = None
) -> MeterProvider:
    """Build a meter provider for one process.

    An explicit reader is used as it is (tests). Otherwise a periodic reader
    around the OTLP/HTTP exporter is added when the endpoint variable is set;
    with neither there is no reader, nothing is exported and gauge callbacks
    never run.
    """
    resource = Resource({SERVICE_NAME: service_name})
    if reader is not None:
        return MeterProvider(resource=resource, metric_readers=[reader], views=_VIEWS)
    endpoint = (_TelemetrySettings().otel_exporter_otlp_endpoint or "").strip()
    if not endpoint:
        return MeterProvider(resource=resource, views=_VIEWS)
    url = endpoint.rstrip("/") + "/v1/metrics"
    periodic = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=url),
        export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
    )
    return MeterProvider(resource=resource, metric_readers=[periodic], views=_VIEWS)


def set_tracer_provider(provider: TracerProvider | None) -> None:
    global _provider
    _provider = provider


def get_tracer() -> Tracer:
    if _provider is None:
        return trace.NoOpTracer()
    return _provider.get_tracer(_TELEMETRY_SCOPE)


def inject_context() -> dict[str, str] | None:
    """The active span's W3C trace context as a dict, or None without a span."""
    if not trace.get_current_span().get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier if "traceparent" in carrier else None


def extract_context(carrier: Any) -> Context | None:
    """A context holding the remote span in `carrier`, or None. Never raises."""
    try:
        if not isinstance(carrier, Mapping):
            return None
        clean = {k: v for k, v in carrier.items() if isinstance(v, str)}
        if "traceparent" not in clean:
            return None
        context = propagate.extract(clean, context=Context())
        if not trace.get_current_span(context).get_span_context().is_valid:
            return None
        return context
    except Exception:
        return None


def link_from(carrier: Any) -> list[Link]:
    """A one-element link list to the span in `carrier`, or []. Never raises."""
    context = extract_context(carrier)
    if context is None:
        return []
    return [Link(trace.get_current_span(context).get_span_context())]


@contextmanager
def start_span(
    name: str,
    *,
    tracer: Tracer | None = None,
    context: Context | None = None,
    links: list[Link] | None = None,
    attributes: Mapping[str, Any] | None = None,
    expected: tuple[type[BaseException], ...] = (),
) -> Iterator[Span]:
    """Start `name` as the current span.

    No exception message or stack ever reaches the span: a failure sets status
    ERROR without a description. Exceptions in `expected` are control flow and
    leave the status alone.
    """
    with (tracer or get_tracer()).start_as_current_span(
        name,
        context=context,
        links=links,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except expected:
            raise
        except BaseException:
            span.set_status(Status(StatusCode.ERROR))
            raise


def job_attributes(job_id: uuid.UUID | str, attempt_number: int | None = None) -> dict:
    attrs: dict[str, Any] = {ATTR_JOB_ID: str(job_id)}
    if attempt_number is not None:
        attrs[ATTR_ATTEMPT_NUMBER] = int(attempt_number)
    return attrs


def annotate_current_span(
    job_id: uuid.UUID | str, attempt_number: int | None = None
) -> None:
    """Put `job.id` (and `attempt.number`) on the active span, e.g. the server span."""
    span = trace.get_current_span()
    for key, value in job_attributes(job_id, attempt_number).items():
        span.set_attribute(key, value)


def scrub_server_span(span: Span, scope: Any = None) -> None:
    """``server_request_hook`` for the FastAPI instrumentation.

    The instrumentation records an unhandled exception on the server span with
    its message and stack, and puts the message in the status description. An
    exception message can hold a connection string, so on this span an error
    only sets status ERROR.
    """
    if not span.is_recording():
        return
    original = span.set_status

    def set_status(status: Status | StatusCode, description: str | None = None):
        code = status.status_code if isinstance(status, Status) else status
        original(Status(code))

    def record_exception(*args: Any, **kwargs: Any) -> None:
        return None

    span.set_status = set_status  # type: ignore[method-assign]
    span.record_exception = record_exception  # type: ignore[method-assign]
