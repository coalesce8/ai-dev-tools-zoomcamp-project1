"""The worker's view of the internal API."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from runwatch.api.job_schemas import WorkloadSpec
from runwatch.lifecycle import FailureCause

HTTP_TIMEOUT_S = 2


class ApiUnavailable(Exception):
    """The API did not give a usable answer; the call may be retried."""


class AttemptNotCurrent(Exception):
    """The API answered 409: the attempt is no longer the running one."""


@dataclass(frozen=True)
class LogLine:
    ts: datetime
    line: str


@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int
    # A spec that fails validation stays a raw dict, so the worker can report
    # a "worker error" for the attempt instead of losing it.
    spec: WorkloadSpec | Any
    trace_context: dict[str, Any] | None


class WorkerApi(Protocol):
    def claim(self, worker_id: str) -> Claim | None: ...

    def heartbeat(self, attempt_id: uuid.UUID, lines: list[LogLine]) -> None: ...

    def result(
        self,
        attempt_id: uuid.UUID,
        outcome: str,
        reason: str | None = None,
        cause: FailureCause | None = None,
    ) -> None: ...


class HttpWorkerApi:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=HTTP_TIMEOUT_S)

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        try:
            response = self._client.post(self._base + path, json=body)
        except httpx.HTTPError as exc:
            raise ApiUnavailable(type(exc).__name__) from None
        return response

    def _check(self, response: httpx.Response) -> None:
        if response.status_code == 409:
            raise AttemptNotCurrent
        if response.status_code != 200:
            raise ApiUnavailable(f"status {response.status_code}")

    def claim(self, worker_id: str) -> Claim | None:
        response = self._post("/internal/v1/worker/claim", {"worker_id": worker_id})
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise ApiUnavailable(f"status {response.status_code}")
        data = response.json()
        raw_spec = data["spec"]
        try:
            spec: Any = WorkloadSpec.model_validate(raw_spec)
        except ValidationError:
            spec = raw_spec
        return Claim(
            job_id=uuid.UUID(data["job_id"]),
            attempt_id=uuid.UUID(data["attempt_id"]),
            attempt_number=int(data["attempt_number"]),
            spec=spec,
            trace_context=data.get("trace_context"),
        )

    def heartbeat(self, attempt_id: uuid.UUID, lines: list[LogLine]) -> None:
        body = {
            "lines": [{"ts": item.ts.isoformat(), "line": item.line} for item in lines]
        }
        path = f"/internal/v1/worker/attempts/{attempt_id}/heartbeat"
        self._check(self._post(path, body))

    def result(
        self,
        attempt_id: uuid.UUID,
        outcome: str,
        reason: str | None = None,
        cause: FailureCause | None = None,
    ) -> None:
        body: dict[str, Any] = {"outcome": outcome}
        if outcome == "failed":
            body["reason"] = reason
            body["cause"] = str(cause.value) if cause is not None else None
        path = f"/internal/v1/worker/attempts/{attempt_id}/result"
        self._check(self._post(path, body))
