"""Job and attempt state machine.

Every transition in the spec's "Job states" section lives here, as pure
functions over immutable snapshots. Nothing in this module touches the
database, the network or the clock: time comes in as a timezone-aware
``now`` argument, and every function returns new snapshots built with
``dataclasses.replace`` instead of mutating its input.

Callers persist the returned snapshots (claim, heartbeat, result, requeue,
cancel) or copy the rule into SQL (the sweeper, using
``LEASE_EXPIRED_REASON``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum, StrEnum


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STALLED = "STALLED"
    CANCELLED = "CANCELLED"


class AttemptState(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FailureCause(StrEnum):
    WORKLOAD = "workload"
    INFRA = "infra"


class FailureKind(Enum):
    """Why an attempt failed, before it is classified into a cause."""

    ALWAYS_FAIL = "always_fail"
    PROBABILITY_ROLL = "probability_roll"
    WORKER_EXCEPTION = "worker_exception"
    INJECTED_FAULT = "injected_fault"
    LEASE_EXPIRED = "lease_expired"


TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)
LEASE_SECONDS = 15
LEASE_EXPIRED_REASON = "lease expired"
CANCELLED_REASON = "cancelled"

_LEASE = timedelta(seconds=LEASE_SECONDS)

_FAILURE_CAUSES: dict[FailureKind, FailureCause] = {
    FailureKind.ALWAYS_FAIL: FailureCause.WORKLOAD,
    FailureKind.PROBABILITY_ROLL: FailureCause.WORKLOAD,
    FailureKind.WORKER_EXCEPTION: FailureCause.INFRA,
    FailureKind.INJECTED_FAULT: FailureCause.INFRA,
    FailureKind.LEASE_EXPIRED: FailureCause.INFRA,
}


@dataclass(frozen=True)
class JobSnapshot:
    state: JobState
    attempt_count: int
    max_attempts: int
    last_attempt_number: int
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class AttemptSnapshot:
    number: int
    state: AttemptState
    started_at: datetime
    lease_expires_at: datetime
    finished_at: datetime | None
    exit_reason: str | None
    failure_cause: FailureCause | None


class TransitionConflict(Exception):
    """A transition that the current state does not allow.

    ``code`` is one of ``invalid_transition``, ``attempt_not_current`` or
    ``lease_not_expired``. Mapping it to an HTTP status is the API's job.
    """

    INVALID_TRANSITION = "invalid_transition"
    ATTEMPT_NOT_CURRENT = "attempt_not_current"
    LEASE_NOT_EXPIRED = "lease_not_expired"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")


def _invalid(action: str, job: JobSnapshot) -> TransitionConflict:
    return TransitionConflict(
        TransitionConflict.INVALID_TRANSITION,
        f"cannot {action} a job in state {job.state}",
    )


def is_current(job: JobSnapshot, attempt: AttemptSnapshot) -> bool:
    """True when ``attempt`` is the job's current running attempt."""
    return (
        job.state is JobState.RUNNING
        and attempt.state is AttemptState.RUNNING
        and attempt.number == job.last_attempt_number
    )


def _require_current(job: JobSnapshot, attempt: AttemptSnapshot) -> None:
    if not is_current(job, attempt):
        raise TransitionConflict(
            TransitionConflict.ATTEMPT_NOT_CURRENT,
            f"attempt {attempt.number} is not the job's current running attempt",
        )


def claim(job: JobSnapshot, now: datetime) -> tuple[JobSnapshot, AttemptSnapshot]:
    """QUEUED -> RUNNING, creating the next attempt with a fresh lease."""
    _require_aware(now)
    if job.state is not JobState.QUEUED:
        raise _invalid("claim", job)
    number = job.last_attempt_number + 1
    attempt = AttemptSnapshot(
        number=number,
        state=AttemptState.RUNNING,
        started_at=now,
        lease_expires_at=now + _LEASE,
        finished_at=None,
        exit_reason=None,
        failure_cause=None,
    )
    new_job = replace(
        job,
        state=JobState.RUNNING,
        attempt_count=job.attempt_count + 1,
        last_attempt_number=number,
        started_at=job.started_at if job.started_at is not None else now,
    )
    return new_job, attempt


def heartbeat(
    job: JobSnapshot, attempt: AttemptSnapshot, now: datetime
) -> AttemptSnapshot:
    """Extend the current attempt's lease to ``now + LEASE_SECONDS``.

    Accepted even after the lease has passed, as long as the sweeper has
    not yet moved the job: the attempt is still current.
    """
    _require_aware(now)
    _require_current(job, attempt)
    return replace(attempt, lease_expires_at=now + _LEASE)


def succeed(
    job: JobSnapshot, attempt: AttemptSnapshot, now: datetime
) -> tuple[JobSnapshot, AttemptSnapshot]:
    """RUNNING -> SUCCEEDED."""
    _require_aware(now)
    _require_current(job, attempt)
    new_attempt = replace(
        attempt,
        state=AttemptState.SUCCEEDED,
        finished_at=now,
        exit_reason=None,
        failure_cause=None,
    )
    new_job = replace(job, state=JobState.SUCCEEDED, finished_at=now)
    return new_job, new_attempt


def fail(
    job: JobSnapshot,
    attempt: AttemptSnapshot,
    now: datetime,
    reason: str,
    cause: FailureCause,
) -> tuple[JobSnapshot, AttemptSnapshot]:
    """RUNNING -> QUEUED when attempts are left (immediate retry), else FAILED."""
    _require_aware(now)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a failure needs a non-empty reason")
    if not isinstance(cause, FailureCause):
        raise TypeError("cause must be a FailureCause")
    _require_current(job, attempt)
    new_attempt = replace(
        attempt,
        state=AttemptState.FAILED,
        finished_at=now,
        exit_reason=reason,
        failure_cause=cause,
    )
    if job.attempt_count < job.max_attempts:
        new_job = replace(job, state=JobState.QUEUED, last_error=reason)
    else:
        new_job = replace(
            job, state=JobState.FAILED, finished_at=now, last_error=reason
        )
    return new_job, new_attempt


def expire_lease(
    job: JobSnapshot, attempt: AttemptSnapshot, now: datetime
) -> tuple[JobSnapshot, AttemptSnapshot]:
    """RUNNING -> STALLED when ``attempt.lease_expires_at < now``.

    Never requeues. STALLED is not terminal, so ``finished_at`` stays unset.
    The sweeper's SQL applies this same rule.
    """
    _require_aware(now)
    _require_current(job, attempt)
    if not attempt.lease_expires_at < now:
        raise TransitionConflict(
            TransitionConflict.LEASE_NOT_EXPIRED,
            f"attempt {attempt.number} lease has not expired",
        )
    new_attempt = replace(
        attempt,
        state=AttemptState.FAILED,
        finished_at=now,
        exit_reason=LEASE_EXPIRED_REASON,
        failure_cause=failure_cause_for(FailureKind.LEASE_EXPIRED),
    )
    new_job = replace(job, state=JobState.STALLED, last_error=LEASE_EXPIRED_REASON)
    return new_job, new_attempt


def requeue(job: JobSnapshot) -> JobSnapshot:
    """FAILED / STALLED -> QUEUED, resetting ``attempt_count``.

    Attempt numbers keep counting from ``last_attempt_number``.
    """
    if job.state not in (JobState.FAILED, JobState.STALLED):
        raise _invalid("requeue", job)
    return replace(job, state=JobState.QUEUED, attempt_count=0, finished_at=None)


def cancel(
    job: JobSnapshot, attempt: AttemptSnapshot | None, now: datetime
) -> tuple[JobSnapshot, AttemptSnapshot | None]:
    """QUEUED / RUNNING / STALLED -> CANCELLED.

    For a RUNNING job, pass its current attempt: it becomes CANCELLED with no
    failure cause, since a cancel is not a failure. For other states the
    attempt, if given, is returned unchanged.
    """
    _require_aware(now)
    if job.state not in (JobState.QUEUED, JobState.RUNNING, JobState.STALLED):
        raise _invalid("cancel", job)
    new_job = replace(job, state=JobState.CANCELLED, finished_at=now)
    if job.state is not JobState.RUNNING:
        return new_job, attempt
    if attempt is None:
        raise ValueError("cancelling a RUNNING job needs its current attempt")
    if not is_current(job, attempt):
        raise ValueError("the attempt passed is not the job's current attempt")
    new_attempt = replace(
        attempt,
        state=AttemptState.CANCELLED,
        finished_at=now,
        exit_reason=CANCELLED_REASON,
        failure_cause=None,
    )
    return new_job, new_attempt


def failure_cause_for(kind: FailureKind) -> FailureCause:
    """Classify a failure: ``workload`` if the user asked for it, else ``infra``."""
    return _FAILURE_CAUSES[kind]
