"""Structured JSON logs for every Runwatch process.

One pipeline, two outputs: JSON lines on stdout and, when a logger provider is
given, OTLP records through the OpenTelemetry ``LoggingHandler``. Both handlers
carry the same sanitising filter, so the two outputs have the same fields.

Privacy (AGENTS.md): only an allow-list of fields can leave the process.
Unknown ``extra`` attributes are dropped, an exception is reduced to its class
name (``exc_type``; messages can hold a connection string and tracebacks hold
source text) and credentials inside URLs are redacted.

This module imports no FastAPI, SQLAlchemy or other Runwatch module.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
import warnings
from datetime import UTC, datetime
from typing import Any, TextIO

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from pydantic_settings import BaseSettings, SettingsConfigDict

# The only attributes copied from ``extra`` (and kept on the OTLP record).
EXTRA_KEYS = (
    "job_id",
    "attempt_id",
    "attempt_number",
    "worker_id",
    # The responder agent's ids (alertname is an alert rule name, not job data).
    "incident_id",
    "fingerprint",
    "alertname",
    "action",
    "outcome",
    "exc_type",
)

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

# Every attribute a plain LogRecord has; anything else came from ``extra``.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}

_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^\s/@]*:[^\s/@]*@")
_REDACTED = r"\1[redacted]@"

_INSTALLED = "_runwatch_installed"


class LogSettingsError(ValueError):
    """``LOG_LEVEL`` holds an unknown value."""


class _LogSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    log_level: str = "INFO"


def redact(text: str) -> str:
    """Replace ``user:password@`` in any URL inside `text`."""
    return _CREDENTIALS.sub(_REDACTED, text)


def _message(record: logging.LogRecord) -> str:
    try:
        return redact(record.getMessage())
    except Exception:
        return redact(str(record.msg))


def _exc_type(record: logging.LogRecord) -> str | None:
    value = getattr(record, "exc_type", None)
    if isinstance(value, str) and value:
        return value
    if record.exc_info and record.exc_info[0] is not None:
        return record.exc_info[0].__name__
    return None


def _trace_ids() -> tuple[str, str] | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


class JsonFormatter(logging.Formatter):
    """One JSON object per record, allow-listed fields only."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _message(record),
            "service": self.service_name,
        }
        for key in EXTRA_KEYS[:-1]:
            value = getattr(record, key, None)
            if value is not None:
                out[key] = _clean(value)
        ids = _trace_ids()
        if ids is not None:
            out["trace_id"], out["span_id"] = ids
        exc_type = _exc_type(record)
        if exc_type is not None:
            out["exc_type"] = redact(exc_type)
        return json.dumps(out, ensure_ascii=False, default=str)


def _clean(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return redact(str(value))
    return redact(value) if isinstance(value, str) else value


class SanitizingFilter(logging.Filter):
    """Reduce a record to the allow-listed fields (on a copy).

    The copy keeps the original record intact for handlers that are not ours
    (pytest's ``caplog``).
    """

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:  # type: ignore[override]
        clean = copy.copy(record)
        clean.msg = _message(record)
        clean.args = None
        exc_type = _exc_type(record)
        clean.exc_info = None
        clean.exc_text = None
        clean.stack_info = None
        for key in list(vars(clean)):
            if key not in _STANDARD_ATTRS and key not in EXTRA_KEYS:
                delattr(clean, key)
        for key in EXTRA_KEYS[:-1]:
            if hasattr(clean, key):
                setattr(clean, key, _clean(getattr(clean, key)))
        if exc_type is not None:
            clean.exc_type = redact(exc_type)
        elif hasattr(clean, "exc_type"):
            delattr(clean, "exc_type")
        return clean


class _StdoutHandler(logging.StreamHandler):
    """A stream handler that follows ``sys.stdout`` unless given a stream."""

    def __init__(self, stream: TextIO | None) -> None:
        self._fixed = stream
        super().__init__(stream)

    @property
    def stream(self) -> TextIO:  # type: ignore[override]
        return self._fixed if self._fixed is not None else sys.stdout

    @stream.setter
    def stream(self, value: TextIO) -> None:
        pass


class _SafeOtlpHandler(LoggingHandler):
    """The SDK handler; an exporter failure never reaches the caller."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:
            pass


def _level() -> int:
    raw = _LogSettings().log_level.strip()
    try:
        return _LEVELS[raw.upper()]
    except KeyError:
        raise LogSettingsError(
            f"LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR (got {raw!r})"
        ) from None


def configure_logging(
    service_name: str,
    logger_provider: LoggerProvider | None = None,
    stream: TextIO | None = None,
) -> None:
    """Install the JSON stdout handler (and the OTLP one) on the root logger.

    Calling it again replaces the handlers installed before and no others.
    """
    level = _level()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _INSTALLED, False):
            root.removeHandler(handler)

    handlers: list[logging.Handler] = []
    stdout = _StdoutHandler(stream)
    stdout.setFormatter(JsonFormatter(service_name))
    handlers.append(stdout)
    if logger_provider is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            handlers.append(_SafeOtlpHandler(logger_provider=logger_provider))
    for handler in handlers:
        handler.addFilter(SanitizingFilter())
        setattr(handler, _INSTALLED, True)
        root.addHandler(handler)
    root.setLevel(level)

    # INFO on sqlalchemy.engine logs SQL parameters, which include job names.
    for name in ("sqlalchemy.engine", "sqlalchemy.pool"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        target.handlers = []
        target.propagate = True
