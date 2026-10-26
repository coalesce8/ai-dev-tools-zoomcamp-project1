"""Queue health queries behind ``GET /api/v1/stats``.

No FastAPI import: the DB-derived gauges (#22) reuse these functions so the
endpoint and the alerts measure the same thing. Nothing here writes or commits.

Window = ``now - 600 s < finished_at <= now``: the lower bound is excluded and
the upper bound included, so a row on the boundary is counted in exactly one of
two back-to-back windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runwatch.lifecycle import AttemptState, FailureCause, JobState
from runwatch.models import Attempt, Job

WINDOW_SECONDS = 600


def rate(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator``, or None when nothing was measured."""
    if numerator < 0 or numerator > denominator:
        raise ValueError(f"invalid rate {numerator}/{denominator}")
    if denominator == 0:
        return None
    return numerator / denominator


@dataclass(frozen=True)
class QueueStats:
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


def count_by_state(session: Session) -> dict[str, int]:
    """Jobs per state; a state with no jobs is 0."""
    rows = session.execute(select(Job.state, func.count()).group_by(Job.state)).all()
    found = {str(state): n for state, n in rows}
    return {state.value: found.get(state.value, 0) for state in JobState}


def oldest_queued_age_seconds(session: Session, now: datetime) -> float | None:
    # Age = now - min(queued_at) over QUEUED jobs only, clamped at 0, None if
    # nothing is queued. queued_at (not created_at) is reset on every return
    # to QUEUED, so a requeued job does not look as old as its first submit.
    oldest = session.scalar(
        select(func.min(Job.queued_at)).where(Job.state == JobState.QUEUED)
    )
    if oldest is None:
        return None
    return max(0.0, (now - oldest).total_seconds())


def _window(now: datetime) -> tuple[datetime, datetime]:
    return now - timedelta(seconds=WINDOW_SECONDS), now


def job_finish_counts(session: Session, now: datetime) -> tuple[int, int]:
    """(jobs_finished, jobs_failed) in the window.

    finished = SUCCEEDED or FAILED jobs (CANCELLED is the user's choice and is
    left out); failed = FAILED jobs, whatever the cause. One statement, so
    failed <= finished even under concurrent writes.
    """
    low, high = _window(now)
    failed = func.count().filter(Job.state == JobState.FAILED)
    finished = func.count().filter(Job.state.in_([JobState.SUCCEEDED, JobState.FAILED]))
    row = session.execute(
        select(finished, failed).where(Job.finished_at > low, Job.finished_at <= high)
    ).one()
    return int(row[0]), int(row[1])


def attempt_finish_counts(session: Session, now: datetime) -> tuple[int, int]:
    """(attempts_finished, attempts_failed_infra) in the window.

    finished = SUCCEEDED or FAILED attempts (the JobFailureRateHigh
    denominator; CANCELLED and RUNNING are left out); failed_infra = FAILED
    with cause infra. One statement.
    """
    low, high = _window(now)
    finished = func.count().filter(
        Attempt.state.in_([AttemptState.SUCCEEDED, AttemptState.FAILED])
    )
    failed_infra = func.count().filter(
        Attempt.state == AttemptState.FAILED,
        Attempt.failure_cause == FailureCause.INFRA,
    )
    row = session.execute(
        select(finished, failed_infra).where(
            Attempt.finished_at > low, Attempt.finished_at <= high
        )
    ).one()
    return int(row[0]), int(row[1])


def compute_stats(session: Session, now: datetime) -> QueueStats:
    jobs_finished, jobs_failed = job_finish_counts(session, now)
    attempts_finished, attempts_failed_infra = attempt_finish_counts(session, now)
    return QueueStats(
        as_of=now,
        window_seconds=WINDOW_SECONDS,
        counts=count_by_state(session),
        oldest_queued_age_seconds=oldest_queued_age_seconds(session, now),
        jobs_finished=jobs_finished,
        jobs_failed=jobs_failed,
        job_failure_rate=rate(jobs_failed, jobs_finished),
        attempts_finished=attempts_finished,
        attempts_failed_infra=attempts_failed_infra,
        infra_attempt_failure_rate=rate(attempts_failed_infra, attempts_finished),
    )
