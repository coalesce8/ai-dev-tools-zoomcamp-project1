"""Worker heartbeat and result endpoints, under `/internal/v1/worker`.

Both routes lock the attempt's job row first (jobs, then attempts), so they
serialize with each other, with cancel and with the sweeper for one job.
State rules come from ``runwatch.lifecycle``. Time is the database clock.

Nothing here logs or raises with a job name, a log line or a result reason.
Only ``job_id`` and ``attempt_id`` are logged.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runwatch.api.errors import ConflictError, NotFoundError
from runwatch.db import get_session
from runwatch.lifecycle import (
    AttemptSnapshot,
    FailureCause,
    JobSnapshot,
    TransitionConflict,
    fail,
    heartbeat,
    succeed,
)
from runwatch.models import Attempt, Job, LogLine

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_LINES_PER_REQUEST = 100
MAX_LINE_CHARS = 4096
MAX_LINES_PER_ATTEMPT = 1000
MAX_REASON_CHARS = 1000


def _reject_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LogLineIn(_Strict):
    ts: AwareDatetime
    line: Annotated[StrictStr, Field(max_length=MAX_LINE_CHARS)]

    _no_nul = field_validator("line")(_reject_nul)

    @field_validator("ts", mode="before")
    @classmethod
    def _ts_is_text(cls, value: object) -> object:
        # FastAPI validates parsed JSON, so strict mode would reject ISO strings.
        # Reject numbers (unix timestamps) here instead.
        if not isinstance(value, str):
            raise ValueError("must be an ISO 8601 string with a UTC offset")
        return value


class HeartbeatIn(_Strict):
    lines: list[LogLineIn] = Field(
        default_factory=list, max_length=MAX_LINES_PER_REQUEST
    )


class HeartbeatOut(BaseModel):
    attempt_id: uuid.UUID
    lease_expires_at: datetime
    lines_accepted: int
    lines_dropped: int


class ResultSucceeded(_Strict):
    outcome: Literal["succeeded"]


class ResultFailed(_Strict):
    outcome: Literal["failed"]
    reason: StrictStr
    cause: FailureCause

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, value: str) -> str:
        _reject_nul(value)
        if not value.strip():
            raise ValueError("must not be empty")
        return value[:MAX_REASON_CHARS]


ResultIn = Annotated[ResultSucceeded | ResultFailed, Field(discriminator="outcome")]


class ResultOut(BaseModel):
    attempt_id: uuid.UUID
    attempt_state: Literal["SUCCEEDED", "FAILED"]
    job_id: uuid.UUID
    job_state: Literal["SUCCEEDED", "QUEUED", "FAILED"]


def _job_snapshot(job: Job) -> JobSnapshot:
    return JobSnapshot(
        state=job.state,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        last_attempt_number=job.last_attempt_number,
        started_at=job.started_at,
        finished_at=job.finished_at,
        last_error=job.last_error,
    )


def _attempt_snapshot(attempt: Attempt) -> AttemptSnapshot:
    return AttemptSnapshot(
        number=attempt.number,
        state=attempt.state,
        started_at=attempt.started_at,
        lease_expires_at=attempt.lease_expires_at,
        finished_at=attempt.finished_at,
        exit_reason=attempt.exit_reason,
        failure_cause=attempt.failure_cause,
    )


def _apply_job(job: Job, snap: JobSnapshot) -> None:
    job.state = snap.state
    job.finished_at = snap.finished_at
    job.last_error = snap.last_error


def _apply_attempt(attempt: Attempt, snap: AttemptSnapshot) -> None:
    attempt.state = snap.state
    attempt.lease_expires_at = snap.lease_expires_at
    attempt.finished_at = snap.finished_at
    attempt.exit_reason = snap.exit_reason
    attempt.failure_cause = snap.failure_cause


def _lock(session: Session, attempt_id: uuid.UUID) -> tuple[Job, Attempt, datetime]:
    """Lock the attempt's job row, reload the attempt, read the database clock."""
    found = session.get(Attempt, attempt_id)
    if found is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    job = session.execute(
        select(Job).where(Job.id == found.job_id).with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    session.refresh(job)
    session.refresh(found)
    # Read after the lock, so the clock never runs backwards across requests.
    now = session.execute(select(func.clock_timestamp())).scalar_one()
    return job, found, now


def _not_current(attempt_id: uuid.UUID) -> ConflictError:
    return ConflictError(f"attempt {attempt_id} is not the current running attempt")


@router.post("/worker/attempts/{attempt_id}/heartbeat")
def post_heartbeat(
    attempt_id: uuid.UUID,
    body: HeartbeatIn,
    session: Annotated[Session, Depends(get_session)],
) -> HeartbeatOut:
    job, attempt, now = _lock(session, attempt_id)
    try:
        new_attempt = heartbeat(_job_snapshot(job), _attempt_snapshot(attempt), now)
    except TransitionConflict:
        raise _not_current(attempt_id) from None
    attempt.lease_expires_at = new_attempt.lease_expires_at

    accepted = 0
    if body.lines:
        stored = session.execute(
            select(func.count())
            .select_from(LogLine)
            .where(LogLine.job_id == job.id, LogLine.attempt_number == attempt.number)
        ).scalar_one()
        room = max(0, MAX_LINES_PER_ATTEMPT - stored)
        accepted_lines = body.lines[:room]
        accepted = len(accepted_lines)
        if accepted:
            last_seq = session.execute(
                select(func.coalesce(func.max(LogLine.seq), 0)).where(
                    LogLine.job_id == job.id
                )
            ).scalar_one()
            session.add_all(
                LogLine(
                    job_id=job.id,
                    seq=last_seq + i,
                    attempt_number=attempt.number,
                    ts=item.ts,
                    line=item.line,
                )
                for i, item in enumerate(accepted_lines, start=1)
            )
    dropped = len(body.lines) - accepted
    session.commit()
    logger.info(
        "heartbeat job_id=%s attempt_id=%s accepted=%d dropped=%d",
        job.id,
        attempt_id,
        accepted,
        dropped,
    )
    return HeartbeatOut(
        attempt_id=attempt_id,
        lease_expires_at=new_attempt.lease_expires_at,
        lines_accepted=accepted,
        lines_dropped=dropped,
    )


@router.post("/worker/attempts/{attempt_id}/result")
def post_result(
    attempt_id: uuid.UUID,
    body: ResultIn,
    session: Annotated[Session, Depends(get_session)],
) -> ResultOut:
    job, attempt, now = _lock(session, attempt_id)
    job_snap = _job_snapshot(job)
    attempt_snap = _attempt_snapshot(attempt)
    try:
        if isinstance(body, ResultSucceeded):
            new_job, new_attempt = succeed(job_snap, attempt_snap, now)
        else:
            new_job, new_attempt = fail(
                job_snap, attempt_snap, now, body.reason, body.cause
            )
    except TransitionConflict:
        raise _not_current(attempt_id) from None
    _apply_job(job, new_job)
    _apply_attempt(attempt, new_attempt)
    if new_job.state.value == "QUEUED":
        job.queued_at = now
    session.commit()
    logger.info(
        "result job_id=%s attempt_id=%s attempt_state=%s job_state=%s",
        job.id,
        attempt_id,
        new_attempt.state.value,
        new_job.state.value,
    )
    return ResultOut(
        attempt_id=attempt_id,
        attempt_state=new_attempt.state.value,
        job_id=job.id,
        job_state=new_job.state.value,
    )
