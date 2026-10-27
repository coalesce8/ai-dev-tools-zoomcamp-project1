"""The load generator: submits one healthy job every LOADGEN_INTERVAL_S.

Run with `python -m runwatch.loadgen`. Needs only RUNWATCH_API_URL. It never
retries a failed tick, so an outage cannot produce a burst of duplicate jobs.
"""

from __future__ import annotations

import logging
import math
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from runwatch.logs import LogSettingsError, configure_logging
from runwatch.telemetry import LOADGEN_SERVICE, build_logger_provider

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 5
JOB_NAME = "loadgen"
JOB_SPEC = {"duration_s": 10, "fail_probability": 0.0, "mode": "normal"}
MAX_SLEEP_S = 1.0


class LoadgenSettingsError(RuntimeError):
    """Raised when the environment does not provide valid settings."""


class LoadgenSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    runwatch_api_url: str = Field(min_length=1)
    loadgen_interval_s: float = Field(default=20.0, gt=0, allow_inf_nan=False)


def load_loadgen_settings() -> LoadgenSettings:
    try:
        return LoadgenSettings()
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]).upper()
            problems.append(f"{name}: {error['msg']}")
        raise LoadgenSettingsError(
            "Invalid environment settings: " + "; ".join(problems)
        ) from None


class SubmitFailed(Exception):
    """A submit did not produce a 201. The message is a status or class name."""


class JobSubmitter(Protocol):
    def submit(self) -> str:
        """Submit one job and return its job_id; raise SubmitFailed."""
        ...


class HttpJobSubmitter:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._url = base_url.rstrip("/") + "/api/v1/jobs"
        self._client = client or httpx.Client(timeout=HTTP_TIMEOUT_S)

    def submit(self) -> str:
        body: dict[str, Any] = {"name": JOB_NAME, "spec": dict(JOB_SPEC)}
        try:
            response = self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            raise SubmitFailed(type(exc).__name__) from None
        if response.status_code != 201:
            raise SubmitFailed(f"status {response.status_code}")
        try:
            return str(response.json()["id"])
        except Exception:
            raise SubmitFailed("unreadable 201 response") from None


class Loadgen:
    def __init__(
        self,
        submitter: JobSubmitter,
        interval_s: float,
        stop: threading.Event | None = None,
        sleep: Callable[[float], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.submitter = submitter
        self.interval_s = interval_s
        self.stop = stop if stop is not None else threading.Event()
        self.sleep = sleep if sleep is not None else self.stop.wait
        self.monotonic = monotonic

    def run(self) -> None:
        start = self.monotonic()
        tick = 0
        while not self.stop.is_set():
            self._tick()
            # Fixed rate; ticks that were missed while submitting are skipped.
            tick += 1
            now = self.monotonic()
            due = start + tick * self.interval_s
            if due < now:
                tick += math.floor((now - due) / self.interval_s) + 1
                due = start + tick * self.interval_s
            while not self.stop.is_set():
                remaining = due - self.monotonic()
                if remaining <= 0:
                    break
                self.sleep(min(remaining, MAX_SLEEP_S))

    def _tick(self) -> None:
        try:
            job_id = self.submitter.submit()
        except SubmitFailed as exc:
            logger.warning("submit failed: %s", exc)
        except Exception as exc:
            logger.warning("submit failed: %s", type(exc).__name__)
        else:
            logger.info("submitted job_id=%s", job_id, extra={"job_id": str(job_id)})


def main() -> int:
    logs = build_logger_provider(LOADGEN_SERVICE)
    try:
        configure_logging(LOADGEN_SERVICE, logs)
        settings = load_loadgen_settings()
    except (LogSettingsError, LoadgenSettingsError) as exc:
        print(exc, file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    Loadgen(
        HttpJobSubmitter(settings.runwatch_api_url),
        settings.loadgen_interval_s,
        stop=stop,
    ).run()
    logs.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
