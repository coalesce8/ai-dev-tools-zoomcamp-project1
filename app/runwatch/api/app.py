"""The FastAPI app factory.

Run with `uvicorn --factory runwatch.api.app:create_app`. Nothing is built at
import time, and the app does not connect to the database at startup.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from runwatch.api import health, internal, public
from runwatch.api.errors import install_error_handlers
from runwatch.db import make_engine, make_sessionmaker
from runwatch.settings import Settings, load_settings
from runwatch.sweeper import LeaseSweeper


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings()

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
                engine.dispose()

    app = FastAPI(title="Runwatch", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_sessionmaker(engine)

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(public.router)
    app.include_router(internal.router)
    return app
