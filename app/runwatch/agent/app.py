"""The agent's FastAPI app factory.

Run with `uvicorn --factory runwatch.agent.app:create_app`. Nothing is built at
import time; logging and telemetry are set up when the app starts.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry.sdk._logs import LoggerProvider
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from runwatch.agent.incidents import (
    FINGERPRINT_PATTERN,
    IncidentStore,
    valid_incident_id,
)
from runwatch.agent.reports import redact, render_report
from runwatch.agent.runs import (
    Clock,
    NoModelResponder,
    PlaceholderResponder,
    Responder,
    RunController,
    utc_now,
)
from runwatch.agent.settings import AgentSettings, load_agent_settings
from runwatch.api.errors import error_response, handle_http_exception
from runwatch.logs import configure_logging
from runwatch.telemetry import build_logger_provider

AGENT_SERVICE = "runwatch-agent"

logger = logging.getLogger(__name__)


class _Alert(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["firing", "resolved"]
    fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    startsAt: str | None = None


class _Body(BaseModel):
    model_config = ConfigDict(extra="allow")

    alerts: list[dict[str, Any]]


def _invalid() -> JSONResponse:
    # Never echo the body back: it is untrusted and may hold anything.
    return JSONResponse({"detail": "invalid Alertmanager payload"}, status_code=422)


def _authorized(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(supplied.strip().encode(), token.encode())


def create_app(
    settings: AgentSettings | None = None,
    responder: Responder | None = None,
    clock: Clock = utc_now,
    logger_provider: LoggerProvider | None = None,
) -> FastAPI:
    if settings is None:
        settings = load_agent_settings()
    if responder is None:
        responder = (
            PlaceholderResponder() if settings.has_api_key else NoModelResponder()
        )

    controller = RunController(
        IncidentStore(settings.agent_incidents_dir),
        responder,
        settings.agent_cooldown_s,
        clock,
        settings.secret_values,
    )
    token = settings.agent_webhook_token.get_secret_value()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_provider = logger_provider is None
        logs = logger_provider or build_logger_provider(AGENT_SERVICE)
        configure_logging(AGENT_SERVICE, logs)
        try:
            if not settings.has_api_key:
                logger.warning(
                    "ANTHROPIC_API_KEY is not set: runs end as skipped_no_model"
                )
            controller.recover()
            yield
        finally:
            controller.shutdown()
            if owns_provider:
                logs.shutdown()
            else:
                logs.force_flush()

    app = FastAPI(title="Runwatch agent", lifespan=lifespan)
    # Unknown paths and wrong methods answer in the error envelope; request
    # validation keeps FastAPI's 422.
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.state.settings = settings
    app.state.runs = controller

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/incidents")
    async def list_incidents(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> JSONResponse:
        statuses = await run_in_threadpool(controller.store.list_statuses)
        items = [
            {
                **{k: status[k] for k in _LIST_KEYS},
                "report_url": f"/incidents/{status['incident_id']}",
            }
            for status in statuses[:limit]
        ]
        return JSONResponse({"incidents": items})

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str) -> Response:
        report = await run_in_threadpool(
            _read_report, controller, incident_id, settings.secret_values
        )
        if report is None:
            return error_response(404, "not_found", "Incident not found")
        return Response(report, media_type="text/markdown; charset=utf-8")

    @app.post("/webhook/alertmanager")
    async def webhook(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            body = _Body.model_validate(await request.json())
            alerts = [(raw, _Alert.model_validate(raw)) for raw in body.alerts]
        except (ValueError, ValidationError):
            return _invalid()
        # Blocking file work: off the event loop.
        results = await run_in_threadpool(
            _handle_all, controller, [raw for raw, _ in alerts]
        )
        return JSONResponse(results, status_code=202)

    return app


_LIST_KEYS = (
    "incident_id",
    "alertname",
    "env",
    "state",
    "outcome",
    "started_at",
    "ended_at",
    "webhook_count",
)


def _read_report(
    controller: RunController, incident_id: str, secrets: list[str]
) -> str | None:
    if not valid_incident_id(incident_id):
        return None
    report = controller.store.read_response(incident_id)
    if report:
        return report
    if report is None:
        return None
    # Running: response.md is empty, so render the partial report now.
    try:
        text = render_report(controller.store.path(incident_id), controller.clock())
    except (OSError, ValueError):
        return None
    return redact(text, secrets)


def _handle_all(controller: RunController, alerts: list[dict]) -> list[dict]:
    results = []
    for alert in alerts:
        incident_id, action = controller.handle(alert)
        results.append(
            {
                "fingerprint": alert["fingerprint"],
                "incident_id": incident_id,
                "action": action,
            }
        )
    return results
