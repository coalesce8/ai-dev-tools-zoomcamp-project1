"""`GET /jobs`, `GET /jobs/{id}` and `GET /jobs/{id}/logs`: read jobs."""

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from runwatch.api.errors import ApiError, NotFoundError
from runwatch.api.job_schemas import JobOut
from runwatch.db import get_session
from runwatch.lifecycle import AttemptState, FailureCause, JobState
from runwatch.models import Job, LogLine

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
LOG_PAGE_SIZE = 500
MAX_SEQ = 2**63 - 1  # LogLine.seq is a bigint

router = APIRouter()


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: int
    worker_id: str | None
    state: AttemptState
    lease_expires_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_reason: str | None
    failure_cause: FailureCause | None


class JobDetail(JobOut):
    attempts: list[AttemptOut]


class JobPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None


class LogLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    attempt_number: int
    ts: datetime
    line: str


class LogPage(BaseModel):
    items: list[LogLineOut]
    next_after: int
    has_more: bool


def encode_cursor(created_at: datetime, job_id: uuid.UUID) -> str:
    payload = {
        "created_at": created_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "id": str(job_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None:
            raise ValueError("timestamp has no UTC offset")
        return created_at, uuid.UUID(payload["id"])
    except (
        ValueError,  # includes binascii.Error, JSON and UTF-8 decode errors
        binascii.Error,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        raise ApiError(400, "validation_error", "cursor: invalid cursor") from exc


@router.get("/jobs", response_model=JobPage)
def list_jobs(
    session: Annotated[Session, Depends(get_session)],
    state: JobState | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> JobPage:
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
    if state is not None:
        stmt = stmt.where(Job.state == state)
    if cursor is not None:
        created_at, job_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Job.created_at, Job.id) < tuple_(created_at, job_id))
    rows = list(session.scalars(stmt.limit(limit + 1)))
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id)
    return JobPage(
        items=[JobOut.model_validate(j) for j in page], next_cursor=next_cursor
    )


def _parse_job_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise NotFoundError("Job not found") from None


@router.get("/jobs/{id}", response_model=JobDetail)
def get_job(id: str, session: Annotated[Session, Depends(get_session)]) -> Job:
    job = session.get(Job, _parse_job_id(id))
    if job is None:
        raise NotFoundError("Job not found")
    return job


@router.get("/jobs/{id}/logs", response_model=LogPage)
def get_job_logs(
    id: str,
    session: Annotated[Session, Depends(get_session)],
    after: Annotated[int, Query(ge=0, le=MAX_SEQ)] = 0,
) -> LogPage:
    jid = _parse_job_id(id)
    if session.get(Job, jid) is None:
        raise NotFoundError("Job not found")
    stmt = (
        select(LogLine)
        .where(LogLine.job_id == jid, LogLine.seq > after)
        .order_by(LogLine.seq)
        .limit(LOG_PAGE_SIZE + 1)
    )
    rows = list(session.scalars(stmt))
    page = rows[:LOG_PAGE_SIZE]
    return LogPage(
        items=[LogLineOut.model_validate(r) for r in page],
        next_after=page[-1].seq if page else after,
        has_more=len(rows) > LOG_PAGE_SIZE,
    )
