"""`POST /jobs`: submit a job."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from runwatch.api.job_schemas import JobCreate, JobOut
from runwatch.db import get_session
from runwatch.lifecycle import JobState
from runwatch.models import Job
from runwatch.telemetry import (
    annotate_current_span,
    inject_context,
    job_attributes,
    start_span,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/jobs", status_code=201, response_model=JobOut)
def submit_job(
    body: JobCreate,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> Job:
    job_id = uuid.uuid4()
    annotate_current_span(job_id)
    with start_span("submit", attributes=job_attributes(job_id)):
        job = Job(
            id=job_id,
            name=body.name,
            spec=body.spec.model_dump(mode="json"),
            state=JobState.QUEUED,
            max_attempts=body.max_attempts,
            trace_context=inject_context(),
        )
        session.add(job)
        session.commit()
    request.app.state.metrics.submitted()
    session.refresh(job)
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    logger.info("job submitted job_id=%s", job.id, extra={"job_id": str(job.id)})
    return job
