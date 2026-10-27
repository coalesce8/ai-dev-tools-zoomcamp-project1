"""`POST /internal/v1/worker/claim`: hand one QUEUED job to a worker.

The state change itself is `runwatch.lifecycle.claim`; this module locks the
row, applies the result and commits, all in one transaction.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from opentelemetry.context import Context
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runwatch import lifecycle
from runwatch.db import get_session
from runwatch.lifecycle import JobSnapshot, JobState
from runwatch.models import Attempt, Job
from runwatch.telemetry import extract_context, job_attributes, start_span

router = APIRouter()


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    worker_id: StrictStr = Field(min_length=1, max_length=255)


class ClaimResult(BaseModel):
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int
    spec: dict[str, Any]
    lease_expires_at: datetime
    trace_context: dict[str, Any] | None

    # Not part of the response: how long the job waited (database times), for
    # the queue_wait_seconds metric.
    _queue_wait_seconds: float = PrivateAttr(default=0.0)

    @property
    def queue_wait_seconds(self) -> float:
        return self._queue_wait_seconds


def claim_select():
    """The statement that picks and locks the oldest QUEUED job."""
    return (
        select(Job)
        .where(Job.state == JobState.QUEUED)
        .order_by(Job.queued_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def claim_next(session: Session, worker_id: str) -> ClaimResult | None:
    """Claim the oldest QUEUED job for `worker_id`, or return None.

    One transaction: read the database clock, lock one row (skipping rows other
    claims hold), apply the transition, insert the attempt, commit.
    """
    now = session.execute(select(func.now())).scalar_one()
    job = session.execute(claim_select()).scalar_one_or_none()
    if job is None:
        session.rollback()
        return None

    new_job, new_attempt = lifecycle.claim(
        JobSnapshot(
            state=job.state,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            last_attempt_number=job.last_attempt_number,
            started_at=job.started_at,
            finished_at=job.finished_at,
            last_error=job.last_error,
        ),
        now,
    )
    job.state = new_job.state
    job.attempt_count = new_job.attempt_count
    job.last_attempt_number = new_job.last_attempt_number
    job.started_at = new_job.started_at

    attempt = Attempt(
        id=uuid.uuid4(),
        job_id=job.id,
        number=new_attempt.number,
        worker_id=worker_id,
        state=new_attempt.state,
        lease_expires_at=new_attempt.lease_expires_at,
        started_at=new_attempt.started_at,
        finished_at=new_attempt.finished_at,
        exit_reason=new_attempt.exit_reason,
        failure_cause=new_attempt.failure_cause,
    )
    session.add(attempt)
    result = ClaimResult(
        job_id=job.id,
        attempt_id=attempt.id,
        attempt_number=attempt.number,
        spec=job.spec,
        lease_expires_at=attempt.lease_expires_at,
        trace_context=job.trace_context,
    )
    result._queue_wait_seconds = max(
        0.0, (new_attempt.started_at - job.queued_at).total_seconds()
    )
    # A child of the submit span when the job has a usable stored context,
    # otherwise a root span.
    with start_span(
        "claim",
        context=extract_context(job.trace_context) or Context(),
        attributes=job_attributes(job.id, attempt.number),
    ):
        session.commit()
    return result


@router.post(
    "/worker/claim",
    response_model=ClaimResult,
    responses={204: {"description": "Nothing is queued"}},
)
def claim(
    body: ClaimRequest,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
):
    result = claim_next(session, body.worker_id)
    if result is None:
        return Response(status_code=204)
    request.app.state.metrics.claimed(result.queue_wait_seconds)
    return result
