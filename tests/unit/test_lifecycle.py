import copy
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from runwatch import lifecycle
from runwatch.lifecycle import (
    CANCELLED_REASON,
    LEASE_EXPIRED_REASON,
    LEASE_SECONDS,
    TERMINAL_STATES,
    AttemptSnapshot,
    AttemptState,
    FailureCause,
    FailureKind,
    JobSnapshot,
    JobState,
    TransitionConflict,
    cancel,
    claim,
    expire_lease,
    fail,
    failure_cause_for,
    heartbeat,
    is_current,
    requeue,
    succeed,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
LEASE = timedelta(seconds=LEASE_SECONDS)


def sec(n: float) -> datetime:
    return T0 + timedelta(seconds=n)


def call(fn, *args, **kwargs):
    """Call a transition and assert that none of its inputs changed."""
    before = copy.deepcopy((args, kwargs))
    result = fn(*args, **kwargs)
    assert (args, kwargs) == before
    return result


def rejects(exc: type[Exception], fn, *args, **kwargs) -> Exception:
    """Assert that a transition raises ``exc`` and leaves its inputs unchanged."""
    before = copy.deepcopy((args, kwargs))
    with pytest.raises(exc) as info:
        fn(*args, **kwargs)
    assert (args, kwargs) == before
    return info.value


def raises(code: str, fn, *args, **kwargs) -> TransitionConflict:
    error = rejects(TransitionConflict, fn, *args, **kwargs)
    assert error.code == code
    assert str(error)
    return error


def new_job(max_attempts: int = 3, **overrides) -> JobSnapshot:
    fields = dict(
        state=JobState.QUEUED,
        attempt_count=0,
        max_attempts=max_attempts,
        last_attempt_number=0,
        started_at=None,
        finished_at=None,
        last_error=None,
    )
    fields.update(overrides)
    return JobSnapshot(**fields)


def running(max_attempts: int = 3, at: datetime = T0):
    return call(claim, new_job(max_attempts), at)


def stalled():
    job, attempt = running()
    return call(expire_lease, job, attempt, sec(LEASE_SECONDS + 1))


def failed_job(max_attempts: int = 3):
    job = new_job(max_attempts)
    attempts = []
    for i in range(max_attempts):
        job, attempt = call(claim, job, sec(i * 10))
        job, attempt = call(
            fail, job, attempt, sec(i * 10 + 1), "exit 1", FailureCause.WORKLOAD
        )
        attempts.append(attempt)
    return job, attempts


def running_attempt(number: int = 1) -> AttemptSnapshot:
    return AttemptSnapshot(
        number=number,
        state=AttemptState.RUNNING,
        started_at=T0,
        lease_expires_at=T0 + LEASE,
        finished_at=None,
        exit_reason=None,
        failure_cause=None,
    )


# Enums and constants


def test_job_state_members():
    assert [m.name for m in JobState] == [
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "STALLED",
        "CANCELLED",
    ]
    for member in JobState:
        assert member == member.name
        assert member.value == member.name
    assert JobState.QUEUED == "QUEUED"


def test_attempt_state_members():
    assert [m.name for m in AttemptState] == [
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    ]
    for member in AttemptState:
        assert member == member.name


def test_failure_cause_members():
    assert {m.value for m in FailureCause} == {"workload", "infra"}
    assert len(FailureCause) == 2
    assert FailureCause("workload") == "workload"
    assert FailureCause("infra") == "infra"


def test_constants():
    assert TERMINAL_STATES == {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
    }
    assert isinstance(TERMINAL_STATES, frozenset)
    assert LEASE_SECONDS == 15
    assert LEASE_EXPIRED_REASON == "lease expired"
    assert CANCELLED_REASON == "cancelled"


# Claim


def test_first_claim():
    job, attempt = call(claim, new_job(), T0)
    assert job.state is JobState.RUNNING
    assert job.attempt_count == 1
    assert job.last_attempt_number == 1
    assert job.started_at == T0
    assert attempt == AttemptSnapshot(
        number=1,
        state=AttemptState.RUNNING,
        started_at=T0,
        lease_expires_at=T0 + timedelta(seconds=15),
        finished_at=None,
        exit_reason=None,
        failure_cause=None,
    )


def test_job_started_at_set_once():
    job, attempt = call(claim, new_job(), T0)
    job, attempt = call(fail, job, attempt, sec(1), "boom", FailureCause.WORKLOAD)
    assert job.started_at == T0
    job, attempt = call(claim, job, sec(2))
    assert job.started_at == T0
    job, attempt = call(expire_lease, job, attempt, sec(30))
    job = call(requeue, job)
    assert job.started_at == T0
    job, attempt = call(claim, job, sec(40))
    assert job.started_at == T0
    assert attempt.started_at == sec(40)


def test_attempt_numbers_never_reset():
    job = new_job(state=JobState.QUEUED, attempt_count=0, last_attempt_number=4)
    job, attempt = call(claim, job, T0)
    assert attempt.number == 5
    assert job.last_attempt_number == 5
    assert job.attempt_count == 1


def test_attempt_number_continues_after_requeue():
    job, _ = failed_job()
    job = call(requeue, job)
    assert job.attempt_count == 0
    assert job.last_attempt_number == 3
    job, attempt = call(claim, job, sec(100))
    assert attempt.number == 4


@pytest.mark.parametrize("state", [s for s in JobState if s is not JobState.QUEUED])
def test_claim_rejects_non_queued(state):
    job = new_job(state=state, attempt_count=1, last_attempt_number=1)
    raises("invalid_transition", claim, job, T0)


# Heartbeat


def test_heartbeat_extends_from_now():
    job, attempt = running()
    hb = call(heartbeat, job, attempt, sec(3))
    assert hb == replace(attempt, lease_expires_at=sec(3) + LEASE)


def test_heartbeats_do_not_accumulate():
    job, attempt = running()
    for i in range(1, 11):
        attempt = call(heartbeat, job, attempt, sec(i))
    assert attempt.lease_expires_at == sec(10) + LEASE
    assert attempt.lease_expires_at != T0 + 10 * LEASE


def test_heartbeat_after_lease_passed_is_accepted():
    job, attempt = running()
    late = sec(LEASE_SECONDS + 5)
    assert attempt.lease_expires_at < late
    hb = call(heartbeat, job, attempt, late)
    assert hb.lease_expires_at == late + LEASE
    assert hb.state is AttemptState.RUNNING


def not_current_cases():
    """(job, attempt) pairs where the attempt is no longer current."""
    # Stalled: job STALLED, attempt FAILED.
    stalled_job, stalled_attempt = stalled()
    # Cancelled: job CANCELLED, attempt CANCELLED.
    job, attempt = running()
    cancelled_job, cancelled_attempt = call(cancel, job, attempt, sec(1))
    # Superseded: attempt 1 stalled, job requeued and claimed as attempt 2.
    # The old attempt is passed as if still RUNNING, so only its number
    # tells it apart from the current one.
    job, old = running()
    job, _ = call(expire_lease, job, old, sec(20))
    job = call(requeue, job)
    superseded_job, current = call(claim, job, sec(30))
    assert current.number == 2
    return {
        "stalled": (stalled_job, stalled_attempt),
        "cancelled": (cancelled_job, cancelled_attempt),
        "superseded": (superseded_job, old),
    }


NOT_CURRENT = ["stalled", "cancelled", "superseded"]


@pytest.mark.parametrize("case", NOT_CURRENT)
def test_heartbeat_rejects_not_current(case):
    job, attempt = not_current_cases()[case]
    assert not is_current(job, attempt)
    raises("attempt_not_current", heartbeat, job, attempt, sec(50))


def test_not_current_cases_cover_each_condition():
    cases = not_current_cases()
    job, attempt = cases["stalled"]
    assert job.state is not JobState.RUNNING
    job, attempt = cases["cancelled"]
    assert attempt.state is not AttemptState.RUNNING
    job, attempt = cases["superseded"]
    assert job.state is JobState.RUNNING
    assert attempt.state is AttemptState.RUNNING
    assert attempt.number != job.last_attempt_number


def test_is_current_conditions():
    job, attempt = running()
    assert is_current(job, attempt)
    assert not is_current(replace(job, state=JobState.STALLED), attempt)
    assert not is_current(job, replace(attempt, state=AttemptState.FAILED))
    assert not is_current(job, replace(attempt, number=2))


# Success


def test_succeed():
    job, attempt = running()
    job2, attempt2 = call(succeed, job, attempt, sec(5))
    assert job2 == replace(job, state=JobState.SUCCEEDED, finished_at=sec(5))
    assert attempt2 == replace(
        attempt,
        state=AttemptState.SUCCEEDED,
        finished_at=sec(5),
        failure_cause=None,
        exit_reason=None,
    )


@pytest.mark.parametrize("case", NOT_CURRENT)
def test_succeed_rejects_not_current(case):
    job, attempt = not_current_cases()[case]
    raises("attempt_not_current", succeed, job, attempt, sec(50))


# Failure


def test_fail_with_attempts_left_requeues_immediately():
    job, attempt = running(max_attempts=3)
    job2, attempt2 = call(fail, job, attempt, sec(5), "exit 1", FailureCause.INFRA)
    assert job2 == replace(job, state=JobState.QUEUED, last_error="exit 1")
    assert job2.finished_at is None
    assert attempt2 == replace(
        attempt,
        state=AttemptState.FAILED,
        finished_at=sec(5),
        exit_reason="exit 1",
        failure_cause=FailureCause.INFRA,
    )


def test_fail_with_no_attempts_left():
    job, attempt = running(max_attempts=2)
    job, attempt = call(fail, job, attempt, sec(1), "a", FailureCause.WORKLOAD)
    job, attempt = call(claim, job, sec(2))
    assert job.attempt_count == job.max_attempts
    job2, attempt2 = call(fail, job, attempt, sec(3), "b", FailureCause.WORKLOAD)
    assert job2 == replace(
        job, state=JobState.FAILED, finished_at=sec(3), last_error="b"
    )
    assert attempt2 == replace(
        attempt,
        state=AttemptState.FAILED,
        finished_at=sec(3),
        exit_reason="b",
        failure_cause=FailureCause.WORKLOAD,
    )


def test_ac5_three_workload_failures_end_failed():
    job, attempts = failed_job(max_attempts=3)
    assert job.state is JobState.FAILED
    assert job.attempt_count == 3
    assert job.last_error == "exit 1"
    assert job.finished_at is not None
    assert [a.number for a in attempts] == [1, 2, 3]
    assert all(a.state is AttemptState.FAILED for a in attempts)
    assert all(a.failure_cause is FailureCause.WORKLOAD for a in attempts)


def test_max_attempts_one_fails_straight_away():
    job, attempt = running(max_attempts=1)
    job, _ = call(fail, job, attempt, sec(1), "nope", FailureCause.WORKLOAD)
    assert job.state is JobState.FAILED


@pytest.mark.parametrize("reason", ["", "   "])
def test_fail_rejects_empty_reason(reason):
    job, attempt = running()
    rejects(ValueError, fail, job, attempt, sec(1), reason, FailureCause.WORKLOAD)


def test_fail_rejects_non_cause():
    job, attempt = running()
    rejects(TypeError, fail, job, attempt, sec(1), "x", "workload")


@pytest.mark.parametrize("case", NOT_CURRENT)
def test_fail_rejects_not_current(case):
    job, attempt = not_current_cases()[case]
    raises("attempt_not_current", fail, job, attempt, sec(50), "x", FailureCause.INFRA)


# Lease expiry


def test_expire_lease():
    job, attempt = running()
    now = attempt.lease_expires_at + timedelta(microseconds=1)
    job2, attempt2 = call(expire_lease, job, attempt, now)
    assert job2 == replace(job, state=JobState.STALLED, last_error="lease expired")
    assert job2.finished_at is None
    assert JobState.STALLED not in TERMINAL_STATES
    assert attempt2 == replace(
        attempt,
        state=AttemptState.FAILED,
        failure_cause=FailureCause.INFRA,
        exit_reason="lease expired",
        finished_at=now,
    )


def test_expire_lease_never_requeues():
    job, attempt = running(max_attempts=3)
    assert job.attempt_count == 1
    job2, _ = call(expire_lease, job, attempt, sec(60))
    assert job2.state is JobState.STALLED
    assert job2.attempt_count == 1


@pytest.mark.parametrize("offset", [0, -1, -15])
def test_expire_lease_requires_strictly_expired(offset):
    job, attempt = running()
    now = attempt.lease_expires_at + timedelta(seconds=offset)
    raises("lease_not_expired", expire_lease, job, attempt, now)


@pytest.mark.parametrize("case", NOT_CURRENT)
def test_expire_lease_rejects_not_current(case):
    job, attempt = not_current_cases()[case]
    raises("attempt_not_current", expire_lease, job, attempt, sec(500))


# Requeue


@pytest.mark.parametrize("source", ["failed", "stalled"])
def test_requeue(source):
    job = failed_job()[0] if source == "failed" else stalled()[0]
    job2 = call(requeue, job)
    assert job2 == replace(
        job, state=JobState.QUEUED, attempt_count=0, finished_at=None
    )
    assert job2.last_error is not None


def test_ac7_requeue_after_failure_then_succeed():
    job, _ = failed_job(max_attempts=3)
    job = call(requeue, job)
    job, attempt = call(claim, job, sec(100))
    assert attempt.number == 4
    assert job.attempt_count == 1
    job, attempt = call(succeed, job, attempt, sec(101))
    assert job.state is JobState.SUCCEEDED
    assert attempt.state is AttemptState.SUCCEEDED


@pytest.mark.parametrize(
    "state",
    [JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED, JobState.CANCELLED],
)
def test_requeue_rejects(state):
    job = new_job(state=state, attempt_count=1, last_attempt_number=1)
    raises("invalid_transition", requeue, job)


# Cancel


def test_cancel_queued():
    job = new_job()
    job2, attempt2 = call(cancel, job, None, sec(1))
    assert job2 == replace(job, state=JobState.CANCELLED, finished_at=sec(1))
    assert attempt2 is None


def test_cancel_stalled_leaves_attempt():
    job, attempt = stalled()
    job2, attempt2 = call(cancel, job, attempt, sec(100))
    assert job2 == replace(job, state=JobState.CANCELLED, finished_at=sec(100))
    assert attempt2 == attempt
    assert attempt2.state is AttemptState.FAILED


def test_cancel_running():
    job, attempt = running()
    job2, attempt2 = call(cancel, job, attempt, sec(4))
    assert job2 == replace(job, state=JobState.CANCELLED, finished_at=sec(4))
    assert attempt2 == replace(
        attempt,
        state=AttemptState.CANCELLED,
        finished_at=sec(4),
        exit_reason="cancelled",
        failure_cause=None,
    )


def test_cancel_running_needs_attempt():
    job, _ = running()
    rejects(ValueError, cancel, job, None, sec(1))


def test_cancel_running_rejects_wrong_attempt():
    job, attempt = running()
    rejects(ValueError, cancel, job, replace(attempt, number=7), sec(1))


def test_ac8_cancelled_attempt_cannot_report():
    job, attempt = running()
    job, cancelled_attempt = call(cancel, job, attempt, sec(2))
    # The worker still holds the attempt as it was before the cancel.
    for held in (attempt, cancelled_attempt):
        raises("attempt_not_current", heartbeat, job, held, sec(3))
        raises("attempt_not_current", succeed, job, held, sec(3))
        raises("attempt_not_current", fail, job, held, sec(3), "x", FailureCause.INFRA)


@pytest.mark.parametrize(
    "state", [JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED]
)
def test_cancel_rejects_terminal(state):
    job = new_job(state=state, attempt_count=1, last_attempt_number=1)
    raises("invalid_transition", cancel, job, None, sec(1))


# Failure cause assignment

EXPECTED_CAUSES = {
    FailureKind.ALWAYS_FAIL: FailureCause.WORKLOAD,
    FailureKind.PROBABILITY_ROLL: FailureCause.WORKLOAD,
    FailureKind.WORKER_EXCEPTION: FailureCause.INFRA,
    FailureKind.INJECTED_FAULT: FailureCause.INFRA,
    FailureKind.LEASE_EXPIRED: FailureCause.INFRA,
}


@pytest.mark.parametrize("kind", list(FailureKind))
def test_failure_cause_for(kind):
    assert kind in EXPECTED_CAUSES, f"no expected cause for {kind}"
    assert failure_cause_for(kind) is EXPECTED_CAUSES[kind]


def test_failure_kind_members_all_mapped():
    assert set(FailureKind) == set(EXPECTED_CAUSES)


# Whole table: 6 states x 6 transitions

TRANSITIONS = ["claim", "succeed", "fail", "expire_lease", "requeue", "cancel"]

# The arrows in the spec diagram: (state, transition) -> resulting job state.
# fail from RUNNING goes to QUEUED here because the job has attempts left.
ARROWS = {
    (JobState.QUEUED, "claim"): JobState.RUNNING,
    (JobState.RUNNING, "succeed"): JobState.SUCCEEDED,
    (JobState.RUNNING, "fail"): JobState.QUEUED,
    (JobState.RUNNING, "expire_lease"): JobState.STALLED,
    (JobState.FAILED, "requeue"): JobState.QUEUED,
    (JobState.STALLED, "requeue"): JobState.QUEUED,
    (JobState.QUEUED, "cancel"): JobState.CANCELLED,
    (JobState.RUNNING, "cancel"): JobState.CANCELLED,
    (JobState.STALLED, "cancel"): JobState.CANCELLED,
}


def table_inputs(state: JobState):
    """A job in ``state`` with attempts left, and an attempt to pass.

    The attempt is RUNNING and numbered as the job's last attempt, so for a
    non-RUNNING job only the job's state can cause a rejection.
    """
    job = new_job(
        max_attempts=3,
        state=state,
        attempt_count=1,
        last_attempt_number=1,
        started_at=T0,
    )
    return job, running_attempt(1)


def apply(transition: str, job: JobSnapshot, attempt: AttemptSnapshot):
    now = sec(LEASE_SECONDS + 1)
    if transition == "claim":
        return call(claim, job, now)[0]
    if transition == "succeed":
        return call(succeed, job, attempt, now)[0]
    if transition == "fail":
        return call(fail, job, attempt, now, "x", FailureCause.WORKLOAD)[0]
    if transition == "expire_lease":
        return call(expire_lease, job, attempt, now)[0]
    if transition == "requeue":
        return call(requeue, job)
    if transition == "cancel":
        passed = attempt if job.state is JobState.RUNNING else None
        return call(cancel, job, passed, now)[0]
    raise AssertionError(transition)


@pytest.mark.parametrize("transition", TRANSITIONS)
@pytest.mark.parametrize("state", list(JobState))
def test_whole_table(state, transition):
    job, attempt = table_inputs(state)
    before = copy.deepcopy((job, attempt))
    expected = ARROWS.get((state, transition))
    if expected is None:
        with pytest.raises(TransitionConflict):
            apply(transition, job, attempt)
    else:
        assert apply(transition, job, attempt).state is expected
    assert (job, attempt) == before


def test_whole_table_size():
    assert len(list(JobState)) * len(TRANSITIONS) == 36
    assert len(ARROWS) == 9


# Purity

FORBIDDEN = re.compile(
    r"^\s*(import|from) +(sqlalchemy|fastapi|starlette|httpx|opentelemetry"
    r"|random|secrets)|\.now\(|\.utcnow\(|time\.time\(",
    re.MULTILINE,
)


def test_module_is_pure():
    source = Path(lifecycle.__file__).read_text()
    assert FORBIDDEN.search(source) is None
    assert not re.search(r"^\s*(import|from) +runwatch\b", source, re.MULTILINE)
    assert not re.search(r"^\s*from +\.", source, re.MULTILINE)


def test_naive_now_rejected():
    rejects(ValueError, claim, new_job(), datetime(2026, 1, 1, 12, 0, 0))
