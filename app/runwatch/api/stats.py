"""GET /api/v1/stats: queue health read from the database at request time."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runwatch.db import get_session
from runwatch.queue_stats import QueueStats, compute_stats

router = APIRouter()


class StatsOut(BaseModel):
    as_of: datetime
    window_seconds: int
    counts: dict[str, int]
    oldest_queued_age_seconds: float | None
    jobs_finished: int
    jobs_failed: int
    job_failure_rate: float | None
    attempts_finished: int
    attempts_failed_infra: int
    infra_attempt_failure_rate: float | None

    @classmethod
    def from_stats(cls, stats: QueueStats) -> "StatsOut":
        return cls(**stats.__dict__)


@router.get("/stats", response_model=StatsOut)
def get_stats(session: Annotated[Session, Depends(get_session)]) -> StatsOut:
    now = session.scalar(select(func.now()))
    return StatsOut.from_stats(compute_stats(session, now))
