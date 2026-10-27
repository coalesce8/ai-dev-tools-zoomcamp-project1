"""The FastAPI app factory.

Run with `uvicorn --factory runwatch.api.app:create_app`. Nothing is built at
import time, and the app does not connect to the database at startup.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider

from runwatch.api import health, internal, public
from runwatch.api.errors import install_error_handlers
from runwatch.db import make_engine, make_sessionmaker
from runwatch.settings import Settings, load_settings
from runwatch.sweeper import LeaseSweeper
from runwatch.telemetry import API_SERVICE, build_tracer_provider, set_tracer_provider

# Workers poll the claim route every second and probes run constantly: no
# server span for them, so idle traffic leaves no traces.
UNTRACED_URLS = "/internal/v1/worker/claim,/healthz,/readyz"


def create_app(
    settings: Settings | None = None,
    tracer_provider: TracerProvider | None = None,
) -> FastAPI:
    if settings is None:
        settings = load_settings()

    owns_provider = tracer_provider is None
    if tracer_provider is None:
        tracer_provider = build_tracer_provider(API_SERVICE)
    provider = tracer_provider
    set_tracer_provider(provider)

    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sweeper: LeaseSweeper | None = None
        try:
            if settings.sweeper_enabled:
                sweeper = LeaseSweeper(app.state.session_factory)
                app.state.sweeper = sweeper
                sweeper.start()
            yield
        finally:
            try:
                if sweeper is not None:
                    sweeper.stop()
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

    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls=UNTRACED_URLS
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(public.router)
    app.include_router(internal.router)
    return app
