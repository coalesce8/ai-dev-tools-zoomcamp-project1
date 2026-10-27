"""The job metrics of the spec's "Observability" table.

Counters and histograms are updated by the events that cause them, after the
transaction committed. The three queue gauges are observable: the database is
read when a metric reader collects, never on a timer of our own. The only SQL
lives in ``runwatch.queue_stats``, so the gauges, ``GET /stats`` and the
alerts measure the same thing.

Privacy: the only attribute keys are ``outcome`` and ``cause``. A metric never
carries a job id, a name, log content or a failure reason. Label values come
from the lifecycle enums, never from request data.

This module imports no FastAPI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from opentelemetry.metrics import (
    CallbackOptions,
    Counter,
    Histogram,
    Meter,
    Observation,
)
from sqlalchemy.orm import Session

from runwatch import queue_stats
from runwatch.lifecycle import AttemptState, FailureCause, JobState

logger = logging.getLogger("runwatch.metrics")

JOBS_SUBMITTED = "runwatch_jobs_submitted_total"
JOBS_FINISHED = "runwatch_jobs_finished_total"
ATTEMPTS_FINISHED = "runwatch_attempts_finished_total"
QUEUE_DEPTH = "runwatch_queue_depth"
OLDEST_QUEUED_AGE = "runwatch_oldest_queued_age_seconds"
JOBS_STALLED = "runwatch_jobs_stalled"
QUEUE_WAIT = "runwatch_queue_wait_seconds"
JOB_RUN = "runwatch_job_run_seconds"

ATTR_OUTCOME = "outcome"
ATTR_CAUSE = "cause"

OUTCOME_SUCCEEDED = JobState.SUCCEEDED.value.lower()
OUTCOME_FAILED = JobState.FAILED.value.lower()
OUTCOME_CANCELLED = JobState.CANCELLED.value.lower()
CAUSE_NONE = "none"

QUEUE_WAIT_BUCKETS = [0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600]
JOB_RUN_BUCKETS = [1, 2, 5, 10, 20, 30, 60, 120, 300]

# Every (outcome, cause) pair the code ever records.
_ATTEMPT_SERIES = (
    (OUTCOME_SUCCEEDED, CAUSE_NONE),
    (OUTCOME_FAILED, FailureCause.WORKLOAD.value),
    (OUTCOME_FAILED, FailureCause.INFRA.value),
)
_JOB_OUTCOMES = (OUTCOME_SUCCEEDED, OUTCOME_FAILED, OUTCOME_CANCELLED)

_TERMINAL_RESULT_STATES = {JobState.SUCCEEDED, JobState.FAILED}


@dataclass(frozen=True)
class Metrics:
    """The instruments, plus one method per event. None of them raises."""

    jobs_submitted: Counter
    jobs_finished: Counter
    attempts_finished: Counter
    queue_wait: Histogram
    job_run: Histogram

    def submitted(self) -> None:
        self.jobs_submitted.add(1)

    def claimed(self, wait_seconds: float) -> None:
        self.queue_wait.record(max(0.0, wait_seconds))

    def result_reported(
        self,
        attempt_state: AttemptState,
        cause: FailureCause | None,
        job_state: JobState,
        run_seconds: float,
    ) -> None:
        """An attempt finished by a worker's result (succeeded or failed)."""
        if attempt_state is AttemptState.SUCCEEDED:
            outcome, cause_label = OUTCOME_SUCCEEDED, CAUSE_NONE
        else:
            outcome = OUTCOME_FAILED
            cause_label = (cause or FailureCause.WORKLOAD).value
        self.attempts_finished.add(1, {ATTR_OUTCOME: outcome, ATTR_CAUSE: cause_label})
        self.job_run.record(max(0.0, run_seconds), {ATTR_OUTCOME: outcome})
        if job_state in _TERMINAL_RESULT_STATES:
            self.jobs_finished.add(1, {ATTR_OUTCOME: job_state.value.lower()})

    def swept(self, count: int) -> None:
        """`count` attempts failed with cause infra (the job is STALLED)."""
        if count > 0:
            self.attempts_finished.add(
                count,
                {ATTR_OUTCOME: OUTCOME_FAILED, ATTR_CAUSE: FailureCause.INFRA.value},
            )

    def cancelled(self) -> None:
        self.jobs_finished.add(1, {ATTR_OUTCOME: OUTCOME_CANCELLED})


def build_metrics(meter: Meter) -> Metrics:
    """Create the counters and histograms, with every counter series at 0.

    A counter that was never incremented does not exist in Prometheus, so
    ``increase()`` over it would show nothing instead of 0.
    """
    jobs_submitted = meter.create_counter(JOBS_SUBMITTED)
    jobs_finished = meter.create_counter(JOBS_FINISHED)
    attempts_finished = meter.create_counter(ATTEMPTS_FINISHED)
    queue_wait = meter.create_histogram(
        QUEUE_WAIT, explicit_bucket_boundaries_advisory=QUEUE_WAIT_BUCKETS
    )
    job_run = meter.create_histogram(
        JOB_RUN, explicit_bucket_boundaries_advisory=JOB_RUN_BUCKETS
    )
    jobs_submitted.add(0)
    for outcome in _JOB_OUTCOMES:
        jobs_finished.add(0, {ATTR_OUTCOME: outcome})
    for outcome, cause in _ATTEMPT_SERIES:
        attempts_finished.add(0, {ATTR_OUTCOME: outcome, ATTR_CAUSE: cause})
    return Metrics(
        jobs_submitted, jobs_finished, attempts_finished, queue_wait, job_run
    )


def _gauge_callback(
    session_factory: Callable[[], Session],
    read: Callable[[Session], float],
) -> Callable[[CallbackOptions], Iterable[Observation]]:
    def callback(options: CallbackOptions) -> Iterable[Observation]:
        # A fresh session, closed every time: no pooled connection is held
        # between exports. When the database cannot be read the gauge has no
        # data point, because 0 would hide the outage.
        try:
            session = session_factory()
            try:
                value = read(session)
            finally:
                session.close()
        except Exception as exc:
            # Class name only: driver messages hold the host and port.
            logger.error("gauge read failed: %s", type(exc).__name__)
            return []
        return [Observation(value)]

    return callback


def _read_depth(session: Session) -> float:
    return queue_stats.count_by_state(session)[JobState.QUEUED.value]


def _read_stalled(session: Session) -> float:
    return queue_stats.count_by_state(session)[JobState.STALLED.value]


def _read_oldest_age(session: Session) -> float:
    now = queue_stats.database_now(session)
    age = queue_stats.oldest_queued_age_seconds(session, now)
    return 0.0 if age is None else age


def register_db_gauges(meter: Meter, session_factory: Callable[[], Session]) -> None:
    """Create the three queue gauges, read from the database at each collection."""
    for name, read in (
        (QUEUE_DEPTH, _read_depth),
        (OLDEST_QUEUED_AGE, _read_oldest_age),
        (JOBS_STALLED, _read_stalled),
    ):
        meter.create_observable_gauge(
            name, callbacks=[_gauge_callback(session_factory, read)]
        )
