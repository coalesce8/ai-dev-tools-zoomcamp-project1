"""Structured logs: JSON format, sanitising, configuration, the OTLP leg."""

import io
import json
import logging
import re

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, LogRecordExporter
from opentelemetry.sdk.trace import TracerProvider
from runwatch import logs
from runwatch.logs import JsonFormatter, LogSettingsError, configure_logging
from runwatch.telemetry import build_logger_provider

LEAK_URL = "postgresql+psycopg://user:hunter2@db:5432/x"


@pytest.fixture(autouse=True)
def _restore_root(monkeypatch):
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    names = ("sqlalchemy.engine", "sqlalchemy.pool", "uvicorn.access", "httpx")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for n, lv in saved.items():
        logging.getLogger(n).setLevel(lv)


def make_logger(name="t"):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("svc"))
    log = logging.getLogger(f"unit.{name}")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, stream


def lines(stream):
    out = stream.getvalue().splitlines()
    return [json.loads(line) for line in out]


def test_one_json_line_with_base_keys():
    log, stream = make_logger("base")
    log.info("hello")
    assert len(stream.getvalue().splitlines()) == 1
    rec = lines(stream)[0]
    assert set(rec) == {"timestamp", "level", "logger", "message", "service"}
    assert rec["level"] == "INFO"
    assert rec["service"] == "svc"
    assert rec["logger"] == "unit.base"
    assert rec["timestamp"].endswith("+00:00")


def test_span_ids_inside_recording_span():
    log, stream = make_logger("span")
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("s") as span:
        log.info("in")
        ctx = span.get_span_context()
    log.info("out")
    inside, outside = lines(stream)
    assert re.fullmatch(r"[0-9a-f]{32}", inside["trace_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", inside["span_id"])
    assert inside["trace_id"] == format(ctx.trace_id, "032x")
    assert inside["span_id"] == format(ctx.span_id, "016x")
    assert "trace_id" not in outside and "span_id" not in outside


def test_no_ids_in_non_recording_span():
    from opentelemetry import trace

    log, stream = make_logger("nonrec")
    with trace.use_span(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)):
        log.info("x")
    assert "trace_id" not in lines(stream)[0]


def test_allowed_extras_copied_and_others_dropped():
    log, stream = make_logger("extra")
    log.info(
        "x",
        extra={
            "job_id": "J1",
            "attempt_id": "A1",
            "attempt_number": 2,
            "worker_id": "w",
            "name_of_job": "secret",
        },
    )
    rec = lines(stream)[0]
    assert rec["job_id"] == "J1"
    assert rec["attempt_id"] == "A1"
    assert rec["attempt_number"] == 2
    assert rec["worker_id"] == "w"
    assert "name_of_job" not in rec
    assert "secret" not in stream.getvalue()


def test_message_arguments_and_awkward_text_stay_one_line():
    log, stream = make_logger("msg")
    log.info("claimed %s", "J1")
    log.info('quote " newline \n tab \t ünïcode ✓')
    first, second = lines(stream)
    assert first["message"] == "claimed J1"
    assert second["message"] == 'quote " newline \n tab \t ünïcode ✓'
    assert len(stream.getvalue().splitlines()) == 2


def test_exception_reduced_to_class_name():
    log, stream = make_logger("exc")
    try:
        raise RuntimeError("boom-message-1234")
    except RuntimeError:
        log.exception("failed")
    out = stream.getvalue()
    rec = json.loads(out)
    assert rec["exc_type"] == "RuntimeError"
    assert "exc_info" not in rec
    assert "boom-message-1234" not in out
    assert "Traceback" not in out
    assert len(out.splitlines()) == 1


def test_credentialed_urls_redacted():
    log, stream = make_logger("redact")
    log.info("connect %s", LEAK_URL)
    log.info(f"direct {LEAK_URL}")
    for rec in lines(stream):
        assert "hunter2" not in rec["message"]
        assert "user:" not in rec["message"]
        assert "[redacted]@db:5432/x" in rec["message"]


def configured(**kw):
    exporter = InMemoryLogRecordExporter()
    provider = build_logger_provider("svc", exporter)
    stream = io.StringIO()
    configure_logging("svc", provider, stream=stream)
    return exporter, provider, stream


def ours():
    return [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_runwatch_installed", False)
    ]


def test_configure_installs_two_handlers_and_is_idempotent():
    extra_handler = logging.NullHandler()
    logging.getLogger().addHandler(extra_handler)
    configured()
    configured()
    assert len(ours()) == 2
    assert extra_handler in logging.getLogger().handlers
    stream_handlers = [h for h in ours() if isinstance(h.formatter, JsonFormatter)]
    assert len(stream_handlers) == 1
    for handler in ours():
        assert any(isinstance(f, logs.SanitizingFilter) for f in handler.filters)


def test_configure_without_provider_has_only_stdout_handler():
    configure_logging("svc", stream=io.StringIO())
    assert len(ours()) == 1


def test_otlp_record_is_sanitised():
    exporter, provider, stream = configured()
    log = logging.getLogger("unit.otlp")
    try:
        raise RuntimeError("boom-message-1234")
    except RuntimeError:
        log.error(
            "failed %s",
            LEAK_URL,
            extra={"leak": "secret", "job_id": "J1"},
            exc_info=True,
        )
    provider.force_flush()
    [record] = exporter.get_finished_logs()
    rec = record.log_record
    seen = json.dumps([rec.body, dict(rec.attributes)], default=str) + stream.getvalue()
    for forbidden in ("boom-message-1234", "leak", "secret", "hunter2", "Traceback"):
        assert forbidden not in seen
    assert rec.attributes["job_id"] == "J1"
    assert rec.attributes["exc_type"] == "RuntimeError"
    assert "exception.message" not in rec.attributes
    assert "exception.stacktrace" not in rec.attributes
    assert json.loads(stream.getvalue())["job_id"] == "J1"


def test_caplog_still_sees_original_record(caplog):
    configured()
    with caplog.at_level(logging.INFO):
        try:
            raise ValueError("kept")
        except ValueError:
            logging.getLogger("unit.caplog").exception("oops")
    assert caplog.records[0].exc_info is not None


def test_log_level_default_and_values(monkeypatch):
    configure_logging("svc", stream=io.StringIO())
    assert logging.getLogger().level == logging.INFO
    monkeypatch.setenv("LOG_LEVEL", "warning")
    configure_logging("svc", stream=io.StringIO())
    assert logging.getLogger().level == logging.WARNING


def test_bad_log_level_names_the_variable(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(LogSettingsError, match="LOG_LEVEL"):
        configure_logging("svc", stream=io.StringIO())


def test_sqlalchemy_engine_info_not_emitted(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    stream = io.StringIO()
    configure_logging("svc", stream=stream)
    logging.getLogger("sqlalchemy.engine.Engine").info("params ('CANARY',)")
    logging.getLogger("sqlalchemy.pool").info("pool")
    assert stream.getvalue() == ""
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING


def test_uvicorn_loggers_propagate_to_root():
    logging.getLogger("uvicorn.access").addHandler(logging.NullHandler())
    configure_logging("svc", stream=io.StringIO())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True


def test_uvicorn_extra_dropped():
    stream = io.StringIO()
    configure_logging("svc", stream=stream)
    logging.getLogger("uvicorn.access").info(
        "%s - hit", "1.2.3.4", extra={"color_message": "\x1b[1mhit"}
    )
    assert "color_message" not in stream.getvalue()
    assert "x1b" not in stream.getvalue()


# -- build_logger_provider ---------------------------------------------------


def test_provider_resource_and_no_exporter_without_endpoint():
    provider = build_logger_provider("runwatch-api")
    assert provider.resource.attributes["service.name"] == "runwatch-api"
    assert provider._multi_log_record_processor._log_record_processors == ()


@pytest.mark.parametrize(
    "endpoint", ["http://collector:4318", "http://collector:4318/"]
)
def test_provider_with_endpoint_has_batch_otlp(monkeypatch, endpoint):
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    provider = build_logger_provider("runwatch-api")
    [processor] = provider._multi_log_record_processor._log_record_processors
    assert isinstance(processor, BatchLogRecordProcessor)
    exporter = processor._batch_processor._exporter
    assert exporter._endpoint == "http://collector:4318/v1/logs"
    provider.shutdown()


def test_explicit_exporter_wins_over_environment(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    exporter = InMemoryLogRecordExporter()
    provider = build_logger_provider("svc", exporter)
    stream = io.StringIO()
    configure_logging("svc", provider, stream=stream)
    logging.getLogger("unit.sync").info("now")
    assert len(exporter.get_finished_logs()) == 1


class RaisingExporter(LogRecordExporter):
    def export(self, batch):
        raise RuntimeError("exporter down")

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


def test_failing_exporter_never_breaks_the_caller():
    provider = build_logger_provider("svc", RaisingExporter())
    stream = io.StringIO()
    configure_logging("svc", provider, stream=stream)
    logging.getLogger("unit.raise").info("still works")
    assert json.loads(stream.getvalue().splitlines()[0])["message"] == "still works"
