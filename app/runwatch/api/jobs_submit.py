"""`POST /jobs`: submit a job."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from runwatch.api.job_schemas import JobCreate, JobOut
from runwatch.db import get_session
from runwatch.lifecycle import JobState
from runwatch.models import Job

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/jobs", status_code=201, response_model=JobOut)
def submit_job(
    body: JobCreate,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> Job:
    job = Job(
        name=body.name,
        spec=body.spec.model_dump(mode="json"),
        state=JobState.QUEUED,
        max_attempts=body.max_attempts,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    logger.info("job submitted job_id=%s", job.id)
    return job
