"""Lease sweeper against the database in DATABASE_URL.

Jobs and attempts are inserted directly through the ORM.
"""

import os
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from runwatch.api.app import create_app
from runwatch.lifecycle import (
    LEASE_EXPIRED_REASON,
    AttemptSnapshot,
    AttemptState,
    FailureCause,
    JobSnapshot,
    JobState,
    expire_lease,
)
from runwatch.models import Attempt, Job
from runwatch.settings import Settings
from runwatch.sweeper import LeaseSweeper, SweptLease, sweep_expired_leases
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC = {"duration_s": 5, "fail_probability": 0.0, "mode": "normal"}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")
    engine = create_engine(DATABASE_URL, pool_size=10)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as s:
        yield s
        s.rollback()


def add_job(
    session,
    *,
    state=JobState.RUNNING,
    attempts=((1, AttemptState.RUNNING, "-1 second"),),
    attempt_count=None,
    max_attempts=3,
    last=None,
):
    """Insert a job and attempts as (number, state, lease offset from DB now)."""
    nums = [n for n, _, _ in attempts]
    job = Job(
        id=uuid.uuid4(),
        name="secret-name",
        spec=SPEC,
        state=state,
        max_attempts=max_attempts,
        attempt_count=attempt_count if attempt_count is not None else len(attempts),
        last_attempt_number=last if last is not None else max(nums, default=0),
        queued_at=T0,
        created_at=T0,
        started_at=T0 if attempts else None,
        trace_context={"traceparent": "00-aa-bb-01"},
    )
    session.add(job)
    session.flush()
    ids = {}
    for number, a_state, offset in attempts:
        cause = FailureCause.WORKLOAD if a_state is AttemptState.FAILED else None
        a = Attempt(
            id=uuid.uuid4(),
            job_id=job.id,
            number=number,
            worker_id="w1",
            state=a_state,
            started_at=T0,
            lease_expires_at=text(f"now() + interval '{offset}'"),
            failure_cause=cause,
        )
        session.add(a)
        ids[number] = a.id
    session.flush()
    session.commit()
    return job.id, ids


def dump(session):
    session.rollback()
    jobs = session.execute(text("SELECT * FROM jobs ORDER BY id")).mappings().all()
    attempts = (
        session.execute(text("SELECT * FROM attempts ORDER BY id")).mappings().all()
    )
    lines = session.execute(text("SELECT * FROM log_lines")).mappings().all()
    out = [dict(r) for r in jobs], [dict(r) for r in attempts], [dict(r) for r in lines]
    session.rollback()
    return out


def row(session, table, id_):
    session.rollback()
    r = session.execute(text(f"SELECT * FROM {table} WHERE id=:i"), {"i": id_})
    out = dict(r.mappings().one())
    session.rollback()
    return out


def sweep(factory):
    with factory() as s:
        out = sweep_expired_leases(s)
        s.commit()
        return out


def test_sweeps_expired_current_attempt(session, factory):
    job_id, ids = add_job(session)
    before_j, before_a = row(session, "jobs", job_id), row(session, "attempts", ids[1])
    with factory() as s:
        swept = sweep_expired_leases(s)
        now = s.execute(text("SELECT now()")).scalar_one()
        s.commit()
    assert swept == [SweptLease(job_id, ids[1], 1)]
    j, a = row(session, "jobs", job_id), row(session, "attempts", ids[1])
    assert j["state"] == "STALLED" and j["last_error"] == LEASE_EXPIRED_REASON
    assert j["finished_at"] is None
    assert a["state"] == "FAILED" and a["failure_cause"] == "infra"
    assert a["exit_reason"] == LEASE_EXPIRED_REASON
    assert a["finished_at"] == now
    for k in before_j:
        if k not in ("state", "last_error"):
            assert j[k] == before_j[k], k
    for k in before_a:
        if k not in ("state", "failure_cause", "exit_reason", "finished_at"):
            assert a[k] == before_a[k], k
    assert dump(session)[2] == []


def test_never_requeues(session, factory):
    job_id, _ = add_job(session, attempt_count=1, max_attempts=3)
    queued_at = row(session, "jobs", job_id)["queued_at"]
    sweep(factory)
    j = row(session, "jobs", job_id)
    assert j["state"] == "STALLED" and j["queued_at"] == queued_at
    assert j["attempt_count"] == 1


FIXTURES = {
    "first_of_3": dict(
        attempts=[(1, AttemptState.RUNNING, "-1 second")], max_attempts=3
    ),
    "third_of_3": dict(
        attempts=[
            (1, AttemptState.FAILED, "-1 hour"),
            (2, AttemptState.FAILED, "-1 hour"),
            (3, AttemptState.RUNNING, "-1 second"),
        ],
        max_attempts=3,
    ),
    "fourth_after_requeue": dict(
        attempts=[
            (1, AttemptState.FAILED, "-1 hour"),
            (2, AttemptState.FAILED, "-1 hour"),
            (3, AttemptState.FAILED, "-1 hour"),
            (4, AttemptState.RUNNING, "-1 second"),
        ],
        max_attempts=3,
        attempt_count=1,
    ),
}


def snaps(session, job_id, number):
    j, a = row(session, "jobs", job_id), None
    session.rollback()
    a = dict(
        session.execute(
            text("SELECT * FROM attempts WHERE job_id=:j AND number=:n"),
            {"j": job_id, "n": number},
        )
        .mappings()
        .one()
    )
    session.rollback()
    return (
        JobSnapshot(
            state=JobState(j["state"]),
            attempt_count=j["attempt_count"],
            max_attempts=j["max_attempts"],
            last_attempt_number=j["last_attempt_number"],
            started_at=j["started_at"],
            finished_at=j["finished_at"],
            last_error=j["last_error"],
        ),
        AttemptSnapshot(
            number=a["number"],
            state=AttemptState(a["state"]),
            started_at=a["started_at"],
            lease_expires_at=a["lease_expires_at"],
            finished_at=a["finished_at"],
            exit_reason=a["exit_reason"],
            failure_cause=FailureCause(a["failure_cause"])
            if a["failure_cause"]
            else None,
        ),
    )


@pytest.mark.parametrize("name", FIXTURES)
def test_matches_lifecycle_expire_lease(session, factory, name):
    kw = FIXTURES[name]
    job_id, _ = add_job(session, **kw)
    number = max(n for n, _, _ in kw["attempts"])
    job0, att0 = snaps(session, job_id, number)
    sweep(factory)
    job1, att1 = snaps(session, job_id, number)
    expected_job, expected_att = expire_lease(job0, att0, att1.finished_at)
    assert (job1, att1) == (expected_job, expected_att)


def test_boundary_is_strict(session, factory):
    jid_eq, ids_eq = add_job(session, attempts=[(1, AttemptState.RUNNING, "1 hour")])
    jid_lt, ids_lt = add_job(session, attempts=[(1, AttemptState.RUNNING, "1 hour")])
    with factory() as s:
        s.execute(
            text("UPDATE attempts SET lease_expires_at = now() WHERE id = :i"),
            {"i": ids_eq[1]},
        )
        s.execute(
            text(
                "UPDATE attempts SET lease_expires_at = now() - interval "
                "'1 microsecond' WHERE id = :i"
            ),
            {"i": ids_lt[1]},
        )
        swept = sweep_expired_leases(s)
        s.rollback()
    assert [w.job_id for w in swept] == [jid_lt]


def test_future_lease_not_swept(session, factory):
    job_id, _ = add_job(session, attempts=[(1, AttemptState.RUNNING, "10 seconds")])
    before = dump(session)
    assert sweep(factory) == []
    assert dump(session) == before


UNTOUCHED = {
    "queued": (JobState.QUEUED, AttemptState.FAILED),
    "succeeded": (JobState.SUCCEEDED, AttemptState.SUCCEEDED),
    "failed": (JobState.FAILED, AttemptState.FAILED),
    "stalled": (JobState.STALLED, AttemptState.FAILED),
    "cancelled": (JobState.CANCELLED, AttemptState.CANCELLED),
}


@pytest.mark.parametrize("name", UNTOUCHED)
def test_other_states_untouched(session, factory, name):
    jstate, astate = UNTOUCHED[name]
    add_job(session, state=jstate, attempts=[(1, astate, "-1 hour")])
    before = dump(session)
    assert sweep(factory) == []
    assert dump(session) == before


def test_old_attempt_expired_but_current_alive_untouched(session, factory):
    add_job(
        session,
        attempts=[
            (1, AttemptState.FAILED, "-1 hour"),
            (2, AttemptState.RUNNING, "1 hour"),
        ],
    )
    before = dump(session)
    assert sweep(factory) == []
    assert dump(session) == before


def test_returns_only_expired(session, factory):
    expired = {add_job(session)[0] for _ in range(3)}
    for _ in range(2):
        add_job(session, attempts=[(1, AttemptState.RUNNING, "1 hour")])
    swept = sweep(factory)
    assert {w.job_id for w in swept} == expired and len(swept) == 3


def test_idempotent(session, factory):
    add_job(session)
    assert len(sweep(factory)) == 1
    after_first = dump(session)
    assert sweep(factory) == []
    assert dump(session) == after_first


def test_one_statement_and_no_literals(session, engine):
    add_job(session)
    seen = []

    def count(conn, cursor, statement, *a):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        s = Session(engine)
        seen.clear()
        sweep_expired_leases(s)
        s.rollback()
        s.close()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(seen) == 1
    src = open(os.path.join(ROOT, "app/runwatch/sweeper.py")).read()
    for line in src.splitlines():
        assert "lease expired" not in line or line.lstrip().startswith("#")
    for banned in ("datetime.now", "utcnow", "time.time("):
        assert banned not in src


@pytest.mark.parametrize("round_", range(5))
def test_two_replicas_sweep_each_job_once(session, factory, round_):
    expected = {add_job(session)[0] for _ in range(50)}
    barrier = threading.Barrier(2)
    results = [None, None]

    def replica(i):
        with factory() as s:
            barrier.wait()
            results[i] = sweep_expired_leases(s)
            s.commit()

    threads = [threading.Thread(target=replica, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    a, b = ({w.job_id for w in r} for r in results)
    assert not (a & b)
    assert len(results[0]) + len(results[1]) == 50
    assert a | b == expected
    states = {r["state"] for r in dump(session)[0]}
    assert states == {"STALLED"}


def test_skips_locked_jobs(session, factory):
    x, _ = add_job(session)
    y, _ = add_job(session)
    holder = factory()
    holder.execute(text("SELECT * FROM jobs WHERE id = :x FOR UPDATE"), {"x": x})
    try:
        began = time.monotonic()
        swept = sweep(factory)
        assert time.monotonic() - began < 1
        assert [w.job_id for w in swept] == [y]
        assert row(session, "jobs", x)["state"] == "RUNNING"
    finally:
        holder.rollback()
        holder.close()
    assert [w.job_id for w in sweep(factory)] == [x]


@pytest.mark.parametrize("round_", range(20))
def test_no_deadlock_with_heartbeat_lock_order(session, factory, round_):
    job_id, ids = add_job(session)
    barrier = threading.Barrier(2)
    errors = []

    def heartbeat():
        try:
            with factory() as s:
                barrier.wait()
                s.execute(
                    text("SELECT * FROM jobs WHERE id = :j FOR UPDATE"), {"j": job_id}
                )
                s.execute(
                    text(
                        "UPDATE attempts SET lease_expires_at = now() + "
                        "interval '15 seconds' WHERE id = :a"
                    ),
                    {"a": ids[1]},
                )
                s.commit()
        except Exception as exc:
            errors.append(exc)

    def sweeper():
        try:
            with factory() as s:
                barrier.wait()
                sweep_expired_leases(s)
                s.commit()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=heartbeat), threading.Thread(target=sweeper)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    j, a = row(session, "jobs", job_id), row(session, "attempts", ids[1])
    assert (j["state"], a["state"]) in {
        ("RUNNING", "RUNNING"),
        ("STALLED", "FAILED"),
    }
    if a["state"] == "FAILED":
        assert a["failure_cause"] == "infra"


def wait_for(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def job_state(session, job_id):
    return row(session, "jobs", job_id)["state"]


def test_loop_sweeps_within_a_second(session, factory):
    job_id, _ = add_job(session)
    sweeper = LeaseSweeper(factory, interval_s=0.05)
    sweeper.start()
    try:
        assert wait_for(lambda: job_state(session, job_id) == "STALLED", 1)
    finally:
        sweeper.stop()


def alive():
    return [t for t in threading.enumerate() if t.name == "lease-sweeper"]


def test_lifespan_starts_and_stops_sweeper(session):
    job_id, _ = add_job(session)
    app = create_app(Settings(database_url=DATABASE_URL, sweeper_enabled=True))
    with TestClient(app):
        assert wait_for(lambda: job_state(session, job_id) == "STALLED", 2)
        assert len(alive()) == 1
    assert wait_for(lambda: not alive(), 2)


def test_lifespan_disabled(session):
    job_id, _ = add_job(session)
    app = create_app(Settings(database_url=DATABASE_URL, sweeper_enabled=False))
    with TestClient(app):
        time.sleep(2)
        assert alive() == []
        assert job_state(session, job_id) == "RUNNING"
    assert not hasattr(app.state, "sweeper")


def test_lifespan_with_unreachable_database():
    url = "postgresql+psycopg://postgres:postgres@127.0.0.1:1/x"
    app = create_app(Settings(database_url=url, sweeper_enabled=True))
    client = TestClient(app)
    client.__enter__()
    assert client.get("/healthz").status_code == 200
    began = time.monotonic()
    client.__exit__(None, None, None)
    assert time.monotonic() - began < 2 + 2
    assert alive() == []


def test_sweeper_stopped_before_engine_disposed():
    order = []
    app = create_app(Settings(database_url=DATABASE_URL, sweeper_enabled=True))
    real_dispose = app.state.engine.dispose
    with TestClient(app):
        sweeper = app.state.sweeper
        real_stop = sweeper.stop

        def stop():
            order.append("stop")
            real_stop()

        def dispose(*a, **kw):
            order.append("dispose")
            return real_dispose(*a, **kw)

        sweeper.stop = stop
        app.state.engine.dispose = dispose
    assert order == ["stop", "dispose"]
