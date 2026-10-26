"""Requeue and cancel endpoints.

Single-job routes (public) lock the job row, then read its current attempt
(jobs, then attempts: the same lock order as the worker report routes). Bulk
routes (internal) lock all candidate job rows in one statement ordered by
``jobs.id``, so two calls cannot deadlock. State rules come only from
``runwatch.lifecycle``. Time is the database clock, read once per request
after the locks are held, as in ``worker_report``.

Nothing here logs a job name, spec or ``last_error``. Single routes log
``job_id``; bulk routes log the count and the window.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runwatch.api.errors import ConflictError, NotFoundError
from runwatch.api.job_schemas import JobOut
from runwatch.api.worker_report import _attempt_snapshot, _job_snapshot
from runwatch.db import get_session
from runwatch.lifecycle import (
    AttemptState,
    FailureCause,
    JobSnapshot,
    JobState,
    TransitionConflict,
    cancel,
    requeue,
)
from runwatch.models import Attempt, Job

logger = logging.getLogger(__name__)

public_router = APIRouter()
internal_router = APIRouter()


class RequeueWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since: AwareDatetime
    until: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> RequeueWindow:
        if self.since > self.until:
            raise ValueError("since must not be after until")
        return self


class RequeueCount(BaseModel):
    requeued: int


def _clock(session: Session) -> datetime:
    return session.execute(select(func.clock_timestamp())).scalar_one()


def _lock_job(session: Session, job_id: uuid.UUID) -> Job:
    job = session.execute(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError(f"job {job_id} not found")
    return job


def _apply_requeue(job: Job, snap: JobSnapshot, now: datetime) -> None:
    job.state = snap.state
    job.attempt_count = snap.attempt_count
    job.finished_at = snap.finished_at
    job.queued_at = now


def requeue_job(session: Session, job_id: uuid.UUID) -> Job:
    """Requeue one FAILED or STALLED job and commit."""
    job = _lock_job(session, job_id)
    now = _clock(session)
    try:
        snap = requeue(_job_snapshot(job))
    except TransitionConflict as exc:
        raise ConflictError(exc.message) from None
    _apply_requeue(job, snap, now)
    session.commit()
    return job


def cancel_job(session: Session, job_id: uuid.UUID) -> Job:
    """Cancel one unfinished job (and its running attempt) and commit."""
    job = _lock_job(session, job_id)
    now = _clock(session)
    attempt: Attempt | None = None
    if job.state is JobState.RUNNING:
        attempt = session.execute(
            select(Attempt)
            .where(
                Attempt.job_id == job.id,
                Attempt.number == job.last_attempt_number,
                Attempt.state == AttemptState.RUNNING,
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
    try:
        new_job, new_attempt = cancel(
            _job_snapshot(job),
            _attempt_snapshot(attempt) if attempt is not None else None,
            now,
        )
    except TransitionConflict as exc:
        raise ConflictError(exc.message) from None
    job.state = new_job.state
    job.finished_at = new_job.finished_at
    if attempt is not None and new_attempt is not None:
        attempt.state = new_attempt.state
        attempt.finished_at = new_attempt.finished_at
        attempt.exit_reason = new_attempt.exit_reason
        attempt.failure_cause = new_attempt.failure_cause
    session.commit()
    return job


def _requeue_locked(session: Session, jobs: list[Job], expected: JobState) -> int:
    """Requeue already-locked rows that are still in ``expected``; commit."""
    now = _clock(session)
    count = 0
    for job in jobs:
        if job.state is not expected:
            continue
        try:
            snap = requeue(_job_snapshot(job))
        except TransitionConflict:
            continue
        _apply_requeue(job, snap, now)
        count += 1
    session.commit()
    return count


def requeue_stalled(session: Session) -> int:
    """Requeue every STALLED job; return how many were requeued."""
    jobs = (
        session.execute(
            select(Job)
            .where(Job.state == JobState.STALLED)
            .order_by(Job.id)
            .with_for_update(of=Job)
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    return _requeue_locked(session, list(jobs), JobState.STALLED)


def requeue_failed(session: Session, since: datetime, until: datetime) -> int:
    """Requeue FAILED jobs finished in [since, until] whose last attempt was infra."""
    jobs = (
        session.execute(
            select(Job)
            .join(
                Attempt,
                (Attempt.job_id == Job.id)
                & (Attempt.number == Job.last_attempt_number)
                & (Attempt.failure_cause == FailureCause.INFRA),
            )
            .where(
                Job.state == JobState.FAILED,
                Job.finished_at.between(since, until),
            )
            .order_by(Job.id)
            .with_for_update(of=Job)
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    return _requeue_locked(session, list(jobs), JobState.FAILED)


def _parse_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError:
        raise NotFoundError(f"job {job_id} not found") from None


@public_router.post("/jobs/{job_id}/requeue")
def post_requeue(
    job_id: str, session: Annotated[Session, Depends(get_session)]
) -> JobOut:
    job = requeue_job(session, _parse_id(job_id))
    logger.info("requeued job_id=%s", job.id)
    return JobOut.model_validate(job)


@public_router.post("/jobs/{job_id}/cancel")
def post_cancel(
    job_id: str, session: Annotated[Session, Depends(get_session)]
) -> JobOut:
    job = cancel_job(session, _parse_id(job_id))
    logger.info("cancelled job_id=%s", job.id)
    return JobOut.model_validate(job)


@internal_router.post("/jobs/requeue-stalled")
def post_requeue_stalled(
    session: Annotated[Session, Depends(get_session)],
) -> RequeueCount:
    count = requeue_stalled(session)
    logger.info("requeue-stalled requeued=%d", count)
    return RequeueCount(requeued=count)


@internal_router.post("/jobs/requeue-failed")
def post_requeue_failed(
    window: RequeueWindow, session: Annotated[Session, Depends(get_session)]
) -> RequeueCount:
    count = requeue_failed(session, window.since, window.until)
    logger.info(
        "requeue-failed requeued=%d since=%s until=%s",
        count,
        window.since.isoformat(),
        window.until.isoformat(),
    )
    return RequeueCount(requeued=count)
