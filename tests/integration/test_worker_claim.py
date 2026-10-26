"""Worker claim against a real database, taken from DATABASE_URL."""

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from runwatch.api import worker_claim
from runwatch.api.app import create_app
from runwatch.lifecycle import AttemptState, FailureCause, JobState
from runwatch.models import Attempt, Job
from runwatch.settings import Settings
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLAIM = "/internal/v1/worker/claim"
SPEC = {"duration_s": 5, "fail_probability": 0.25, "mode": "normal"}
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")
    engine = create_engine(DATABASE_URL, pool_size=12)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as session:
        yield session


@pytest.fixture
def client(engine, factory):
    app = create_app(Settings(database_url=DATABASE_URL))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def add_job(session, queued_at=T0, **kwargs):
    kwargs.setdefault("name", "job")
    kwargs.setdefault("spec", SPEC)
    kwargs.setdefault("state", JobState.QUEUED)
    job = Job(queued_at=queued_at, **kwargs)
    session.add(job)
    session.commit()
    return job.id


def reload(factory, job_id):
    with factory() as s:
        job = s.get(Job, job_id)
        attempts = list(
            s.scalars(
                select(Attempt).where(Attempt.job_id == job_id).order_by(Attempt.number)
            )
        )
        return job, attempts


def test_empty_queue_is_204_with_empty_body(client):
    response = client.post(CLAIM, json={"worker_id": "w"})
    assert response.status_code == 204
    assert response.content == b""


def test_non_queued_jobs_are_not_claimed(client, session, factory):
    ids = [
        add_job(session, state=state, name=state.value)
        for state in JobState
        if state is not JobState.QUEUED
    ]
    before = {i: reload(factory, i) for i in ids}
    response = client.post(CLAIM, json={"worker_id": "w"})
    assert response.status_code == 204
    for i in ids:
        job, attempts = reload(factory, i)
        assert job.state == before[i][0].state
        assert attempts == [] and before[i][1] == []
        assert job.attempt_count == 0 and job.last_attempt_number == 0


def test_successful_claim(client, session, factory):
    ctx = {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
    job_id = add_job(session, name="distinctive-name-xyz", trace_context=ctx)
    created_at = reload(factory, job_id)[0].created_at

    response = client.post(CLAIM, json={"worker_id": "worker-1"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "job_id",
        "attempt_id",
        "attempt_number",
        "spec",
        "lease_expires_at",
        "trace_context",
    }
    assert "distinctive-name-xyz" not in response.text
    assert body["job_id"] == str(job_id)
    assert body["spec"] == SPEC
    assert body["trace_context"] == ctx
    lease = datetime.fromisoformat(body["lease_expires_at"])
    assert lease.utcoffset() is not None

    job, attempts = reload(factory, job_id)
    assert job.state == JobState.RUNNING
    assert job.attempt_count == 1
    assert job.last_attempt_number == 1
    assert job.queued_at == T0 and job.created_at == created_at
    assert len(attempts) == 1
    a = attempts[0]
    assert job.started_at == a.started_at
    assert a.state == AttemptState.RUNNING
    assert str(a.id) == body["attempt_id"] and a.number == body["attempt_number"] == 1
    assert a.worker_id == "worker-1"
    assert a.lease_expires_at - a.started_at == timedelta(seconds=15)
    assert a.lease_expires_at == lease
    assert a.finished_at is None and a.exit_reason is None
    assert a.failure_cause is None


def test_claim_without_trace_context_returns_null(client, session):
    add_job(session)
    response = client.post(CLAIM, json={"worker_id": "w"})
    assert response.status_code == 200
    assert response.json()["trace_context"] is None


def test_order_by_queued_at_shuffled(client, session):
    times = [T0 + timedelta(seconds=s) for s in (2, 0, 1)]
    ids = {t: add_job(session, queued_at=t) for t in times}
    got = [
        client.post(CLAIM, json={"worker_id": "w"}).json()["job_id"] for _ in range(3)
    ]
    assert got == [str(ids[T0 + timedelta(seconds=s)]) for s in (0, 1, 2)]
    assert client.post(CLAIM, json={"worker_id": "w"}).status_code == 204


def test_order_is_queued_at_not_created_at(client, session):
    first_created = add_job(session, queued_at=T0 + timedelta(seconds=10))
    second_created = add_job(session, queued_at=T0)
    got = [
        client.post(CLAIM, json={"worker_id": "w"}).json()["job_id"] for _ in range(2)
    ]
    assert got == [str(second_created), str(first_created)]


def test_ties_break_by_id(client, session):
    ids = sorted(uuid.uuid4() for _ in range(3))
    for i in (ids[2], ids[0], ids[1]):
        add_job(session, id=i)
    got = [
        client.post(CLAIM, json={"worker_id": "w"}).json()["job_id"] for _ in range(3)
    ]
    assert got == [str(i) for i in ids]


def test_query_uses_partial_index(session):
    stmt = worker_claim.claim_select().compile(
        dialect=session.bind.dialect, compile_kwargs={"literal_binds": True}
    )
    session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(r[0] for r in session.execute(text(f"EXPLAIN {stmt}")).all())
    session.rollback()
    assert "ix_jobs_queued" in plan


def test_retry_gets_next_attempt_and_keeps_started_at(client, session, factory):
    started = T0 - timedelta(minutes=1)
    job_id = add_job(
        session, attempt_count=1, last_attempt_number=1, started_at=started
    )
    session.add(
        Attempt(
            job_id=job_id,
            number=1,
            worker_id="old",
            state=AttemptState.FAILED,
            lease_expires_at=started + timedelta(seconds=15),
            started_at=started,
            finished_at=started + timedelta(seconds=3),
            exit_reason="boom",
            failure_cause=FailureCause.INFRA,
        )
    )
    session.commit()
    _, before = reload(factory, job_id)

    body = client.post(CLAIM, json={"worker_id": "w"}).json()
    assert body["attempt_number"] == 2
    job, attempts = reload(factory, job_id)
    assert (job.attempt_count, job.last_attempt_number) == (2, 2)
    assert job.started_at == started
    assert [a.number for a in attempts] == [1, 2]
    old = attempts[0]
    assert (old.state, old.exit_reason, old.finished_at, old.worker_id) == (
        AttemptState.FAILED,
        "boom",
        before[0].finished_at,
        "old",
    )


def test_after_requeue_numbers_continue_count_resets(client, session, factory):
    job_id = add_job(session, attempt_count=0, last_attempt_number=3)
    for n in (1, 2, 3):
        session.add(
            Attempt(
                job_id=job_id,
                number=n,
                state=AttemptState.FAILED,
                lease_expires_at=T0,
                started_at=T0,
                finished_at=T0,
                exit_reason="x",
                failure_cause=FailureCause.WORKLOAD,
            )
        )
    session.commit()
    body = client.post(CLAIM, json={"worker_id": "w"}).json()
    assert body["attempt_number"] == 4
    job, attempts = reload(factory, job_id)
    assert (job.attempt_count, job.last_attempt_number) == (1, 4)
    assert [a.number for a in attempts] == [1, 2, 3, 4]


@pytest.mark.parametrize("run", range(5))
def test_concurrent_claims_never_hand_out_a_job_twice(factory, session, run):
    total, threads = 20, 10
    for i in range(total):
        add_job(session, queued_at=T0 + timedelta(seconds=i))
    barrier = threading.Barrier(threads)
    results: list[list] = [[] for _ in range(threads)]
    errors: list[BaseException] = []

    def work(idx):
        try:
            with factory() as s:
                barrier.wait()
                while True:
                    r = worker_claim.claim_next(s, f"w{idx}")
                    results[idx].append(r)
                    if r is None:
                        break
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert not errors
    claimed = [r for rs in results for r in rs if r is not None]
    assert len(claimed) == total
    assert all(rs[-1] is None for rs in results)
    assert len({r.job_id for r in claimed}) == total
    assert len({r.attempt_id for r in claimed}) == total
    assert all(r.attempt_number == 1 for r in claimed)
    with factory() as s:
        jobs = list(s.scalars(select(Job)))
        assert len(jobs) == total
        assert all(
            j.state == JobState.RUNNING and j.last_attempt_number == 1 for j in jobs
        )
        assert s.scalar(select(text("count(*)")).select_from(Attempt)) == total


def test_skip_locked_does_not_block(factory, session, engine):
    oldest = add_job(session, queued_at=T0)
    second = add_job(session, queued_at=T0 + timedelta(seconds=1))
    holder = Session(engine)
    try:
        holder.execute(select(Job).where(Job.id == oldest).with_for_update())
        with factory() as s:
            started = datetime.now(UTC)
            r = worker_claim.claim_next(s, "w")
            assert r is not None and r.job_id == second
            r = worker_claim.claim_next(s, "w")
            assert r is None
            assert (datetime.now(UTC) - started).total_seconds() < 1
    finally:
        holder.rollback()
        holder.close()
    with factory() as s:
        r = worker_claim.claim_next(s, "w")
    assert r is not None and r.job_id == oldest


def test_database_failure_is_500_and_leaves_job_queued(
    client, session, factory, monkeypatch
):
    job_id = add_job(session)

    def boom(self):
        raise RuntimeError("db down: postgresql://secret")

    monkeypatch.setattr(Session, "commit", boom)
    response = client.post(CLAIM, json={"worker_id": "w"})
    monkeypatch.undo()
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    job, attempts = reload(factory, job_id)
    assert job.state == JobState.QUEUED
    assert job.attempt_count == 0 and job.started_at is None
    assert attempts == []


def test_invalid_body_leaves_database_unchanged(client, session, factory):
    job_id = add_job(session)
    assert client.post(CLAIM, json={"worker_id": ""}).status_code == 400
    job, attempts = reload(factory, job_id)
    assert job.state == JobState.QUEUED and attempts == []
