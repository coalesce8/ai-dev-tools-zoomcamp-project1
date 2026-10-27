"""The FastAPI app factory.

Run with `uvicorn --factory runwatch.api.app:create_app`. Nothing is built at
import time, and the app does not connect to the database at startup.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import (
    FastAPIInstrumentor,
    _get_route_details,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from runwatch.api import health, internal, public
from runwatch.api.errors import install_error_handlers
from runwatch.db import make_engine, make_sessionmaker
from runwatch.metrics import build_metrics, register_db_gauges
from runwatch.settings import Settings, load_settings
from runwatch.sweeper import LeaseSweeper
from runwatch.telemetry import (
    API_SERVICE,
    build_meter_provider,
    build_tracer_provider,
    scrub_server_span,
    set_tracer_provider,
)

METRICS_SCOPE = "runwatch"

# Workers poll the claim route every second and probes run constantly: no
# server span or HTTP metric for them, so idle traffic leaves no trace.
UNTRACED_URLS = "/internal/v1/worker/claim,/healthz,/readyz"


class FullRouteScope:
    """ASGI middleware that restores the full route template for HTTP metrics.

    With routers included in routers (``/api/v1`` around ``/jobs``) the route
    in the ASGI scope holds only the innermost path, so the instrumentation's
    ``http.target`` metric attribute would read ``/jobs`` for every prefix. The
    span already gets the full template from the same helper used here. Add it
    before ``FastAPIInstrumentor.instrument_app`` so it runs inside it.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        finally:
            if scope["type"] == "http":
                try:
                    template = _get_route_details(scope)
                except Exception:
                    template = None
                if template:
                    scope["route"] = SimpleNamespace(
                        path=template, path_format=template
                    )


def create_app(
    settings: Settings | None = None,
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
) -> FastAPI:
    if settings is None:
        settings = load_settings()

    owns_provider = tracer_provider is None
    if tracer_provider is None:
        tracer_provider = build_tracer_provider(API_SERVICE)
    provider = tracer_provider
    set_tracer_provider(provider)

    owns_meter_provider = meter_provider is None
    if meter_provider is None:
        meter_provider = build_meter_provider(API_SERVICE)
    meters = meter_provider

    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sweeper: LeaseSweeper | None = None
        try:
            if settings.sweeper_enabled:
                sweeper = LeaseSweeper(
                    app.state.session_factory, metrics=app.state.metrics
                )
                app.state.sweeper = sweeper
                sweeper.start()
            yield
        finally:
            try:
                if sweeper is not None:
                    sweeper.stop()
            finally:
                try:
                    # Before the engine goes: the last export reads the gauges.
                    if owns_meter_provider:
                        meters.shutdown()
                    else:
                        meters.force_flush()
                finally:
                    try:
                        engine.dispose()
                    finally:
                        if owns_provider:
                            provider.shutdown()
                        else:
                            provider.force_flush()

    app = FastAPI(title="Runwatch", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_sessionmaker(engine)
    meter = meters.get_meter(METRICS_SCOPE)
    app.state.metrics = build_metrics(meter)
    register_db_gauges(meter, app.state.session_factory)

    app.add_middleware(FullRouteScope)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        meter_provider=meters,
        excluded_urls=UNTRACED_URLS,
        server_request_hook=scrub_server_span,
        exclude_spans=["receive", "send"],
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(public.router)
    app.include_router(internal.router)
    return app
