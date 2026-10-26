"""Liveness and readiness endpoints, at the root of the app."""

import logging

from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from runwatch.api.errors import ApiError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request) -> dict[str, str]:
    engine = request.app.state.engine
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # The driver's message names the host, so log only the error type.
        cause = exc.orig if getattr(exc, "orig", None) is not None else exc
        logger.warning("Readiness check failed: %s", type(cause).__name__)
        raise ApiError(503, "not_ready", "Database is not reachable") from None
    return {"status": "ready"}
