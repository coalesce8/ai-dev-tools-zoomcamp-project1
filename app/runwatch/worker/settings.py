"""Worker settings. The worker never reads DATABASE_URL: it talks HTTP only."""

import os
import socket

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

WORKER_ID_MAX = 255


class WorkerSettingsError(RuntimeError):
    """Raised when the environment does not provide valid worker settings."""


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"[:WORKER_ID_MAX]


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    runwatch_api_url: str = Field(min_length=1)
    worker_id: str = Field(
        default_factory=default_worker_id, min_length=1, max_length=WORKER_ID_MAX
    )
    worker_fault: str | None = None


def load_worker_settings() -> WorkerSettings:
    """Build the settings, naming the bad variables (never their values)."""
    try:
        return WorkerSettings()
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]).upper()
            problems.append(f"{name}: {error['msg']}")
        raise WorkerSettingsError(
            "Invalid environment settings: " + "; ".join(problems)
        ) from None
