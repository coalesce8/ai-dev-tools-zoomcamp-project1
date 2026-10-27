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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.sdk._logs import LoggerProvider
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from runwatch.agent.incidents import FINGERPRINT_PATTERN, IncidentStore
from runwatch.agent.runs import (
    Clock,
    NoModelResponder,
    PlaceholderResponder,
    Responder,
    RunController,
    utc_now,
)
from runwatch.agent.settings import AgentSettings, load_agent_settings
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
    app.state.settings = settings
    app.state.runs = controller

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
