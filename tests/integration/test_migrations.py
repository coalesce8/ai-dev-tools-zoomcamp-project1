"""Migration and schema checks against the database in DATABASE_URL."""

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from runwatch.lifecycle import AttemptState, FailureCause, JobState
from runwatch.models import Attempt, Base, Job, LogLine
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _config() -> Config:
    return Config(os.path.join(ROOT, "alembic.ini"))


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(DATABASE_URL)
    cfg = _config()
    try:
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        yield engine
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session
        session.rollback()
        session.execute(text("DELETE FROM jobs"))
        session.commit()


def _job(**kw) -> Job:
    return Job(id=uuid.uuid4(), name="j", spec={"x": 1}, **kw)


def _attempt(job, number=1, state=AttemptState.RUNNING, cause=None) -> Attempt:
    return Attempt(
        id=uuid.uuid4(),
        job_id=job.id,
        number=number,
        state=state,
        failure_cause=cause,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=15),
    )


def test_tables_exist(engine):
    names = set(inspect(engine).get_table_names())
    assert names == {"jobs", "attempts", "log_lines", "alembic_version"}


def test_single_head():
    from alembic.script import ScriptDirectory

    assert ScriptDirectory.from_config(_config()).get_heads() == ["0001"]


def test_columns(engine):
    insp = inspect(engine)
    jobs = {c["name"]: c for c in insp.get_columns("jobs")}
    assert set(jobs) == {
        "id", "name", "spec", "state", "max_attempts", "attempt_count",
        "last_attempt_number", "created_at", "queued_at", "started_at",
        "finished_at", "last_error", "trace_context",
    }  # fmt: skip
    nullable = {n for n, c in jobs.items() if c["nullable"]}
    assert nullable == {"started_at", "finished_at", "last_error", "trace_context"}
    assert jobs["spec"]["type"].__class__.__name__ == "JSONB"
    assert jobs["created_at"]["type"].timezone is True
    assert jobs["name"]["type"].length == 100
    assert jobs["state"]["type"].length == 16

    attempts = {c["name"]: c for c in insp.get_columns("attempts")}
    assert {n for n, c in attempts.items() if c["nullable"]} == {
        "worker_id", "finished_at", "exit_reason", "failure_cause",
    }  # fmt: skip
    assert attempts["worker_id"]["type"].length == 255
    assert attempts["lease_expires_at"]["type"].timezone is True

    logs = {c["name"]: c for c in insp.get_columns("log_lines")}
    assert set(logs) == {"job_id", "seq", "attempt_number", "ts", "line"}
    assert not any(c["nullable"] for c in logs.values())
    assert logs["seq"]["type"].__class__.__name__ == "BIGINT"


def test_keys_and_indexes(engine):
    insp = inspect(engine)
    assert insp.get_pk_constraint("jobs")["constrained_columns"] == ["id"]
    assert insp.get_pk_constraint("attempts")["constrained_columns"] == ["id"]
    assert insp.get_pk_constraint("log_lines")["constrained_columns"] == [
        "job_id",
        "seq",
    ]

    fks = insp.get_foreign_keys("attempts")
    assert [(f["constrained_columns"], f["referred_table"]) for f in fks] == [
        (["job_id"], "jobs")
    ]
    assert fks[0]["options"]["ondelete"] == "CASCADE"
    fks = insp.get_foreign_keys("log_lines")
    assert len(fks) == 1
    assert fks[0]["constrained_columns"] == ["job_id", "attempt_number"]
    assert fks[0]["referred_table"] == "attempts"
    assert fks[0]["referred_columns"] == ["job_id", "number"]
    assert fks[0]["options"]["ondelete"] == "CASCADE"

    uqs = {
        u["name"]: u["column_names"] for u in insp.get_unique_constraints("attempts")
    }
    assert uqs == {"uq_attempts_job_id_number": ["job_id", "number"]}

    def idx(table):
        return {i["name"]: i for i in insp.get_indexes(table)}

    jobs = idx("jobs")
    assert set(jobs) == {
        "ix_jobs_queued",
        "ix_jobs_created_at_id",
        "ix_jobs_state_created_at_id",
        "ix_jobs_finished_at",
    }
    assert jobs["ix_jobs_queued"]["column_names"] == ["queued_at", "id"]
    assert "QUEUED" in jobs["ix_jobs_queued"]["dialect_options"]["postgresql_where"]
    assert jobs["ix_jobs_finished_at"]["column_names"] == ["finished_at"]
    assert jobs["ix_jobs_state_created_at_id"]["column_names"][0] == "state"
    with engine.connect() as conn:
        defs = dict(
            conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='jobs'"
                )
            ).all()
        )
    assert "created_at DESC, id DESC" in defs["ix_jobs_created_at_id"]
    assert "state, created_at DESC, id DESC" in defs["ix_jobs_state_created_at_id"]

    att = idx("attempts")
    assert set(att) - {"uq_attempts_job_id_number"} == {
        "ix_attempts_running_lease",
        "ix_attempts_finished_at",
        "uq_attempts_one_running",
    }
    assert att["uq_attempts_one_running"]["unique"]
    assert att["uq_attempts_one_running"]["column_names"] == ["job_id"]
    assert (
        "RUNNING"
        in att["uq_attempts_one_running"]["dialect_options"]["postgresql_where"]
    )
    assert att["ix_attempts_running_lease"]["column_names"] == ["lease_expires_at"]
    assert (
        "RUNNING"
        in att["ix_attempts_running_lease"]["dialect_options"]["postgresql_where"]
    )

    logs = idx("log_lines")
    assert logs["ix_log_lines_job_id_attempt_number"]["column_names"] == [
        "job_id",
        "attempt_number",
    ]


def test_no_drift(engine):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_job_defaults(session):
    session.execute(
        text("INSERT INTO jobs (id, name, spec) VALUES (:id, 'n', '{}')"),
        {"id": uuid.uuid4()},
    )
    session.commit()
    job = session.query(Job).one()
    assert job.state is JobState.QUEUED
    assert (job.max_attempts, job.attempt_count, job.last_attempt_number) == (3, 0, 0)
    assert job.created_at.tzinfo is not None and job.queued_at.tzinfo is not None
    assert job.created_at.utcoffset() == timedelta(0)


def test_enum_stored_as_value(session):
    job = _job(state=JobState.STALLED)
    session.add(job)
    session.commit()
    raw = session.execute(
        text("SELECT state FROM jobs WHERE id=:i"), {"i": job.id}
    ).scalar()
    assert raw == "STALLED"
    att = _attempt(job, state=AttemptState.FAILED, cause=FailureCause.INFRA)
    session.add(att)
    session.commit()
    raw = session.execute(
        text("SELECT failure_cause FROM attempts WHERE id=:i"), {"i": att.id}
    ).scalar()
    assert raw == "infra"
    session.expire_all()
    assert session.get(Attempt, att.id).failure_cause is FailureCause.INFRA
    assert job.attempts[0].job is job


def _rejected(session, *objs):
    session.add_all(objs)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_job_constraints(session):
    session.execute(
        text("INSERT INTO jobs (id, name, spec) VALUES (:id, 'n', '{}')"),
        {"id": uuid.uuid4()},
    )
    session.commit()
    for sql in (
        "INSERT INTO jobs (id, name, spec, state) "
        "VALUES (gen_random_uuid(), 'n', '{}', 'BOGUS')",
        "INSERT INTO jobs (id, name, spec, max_attempts) "
        "VALUES (gen_random_uuid(), 'n', '{}', 11)",
        "INSERT INTO jobs (id, name, spec) VALUES (gen_random_uuid(), '', '{}')",
    ):
        with pytest.raises(IntegrityError):
            session.execute(text(sql))
        session.rollback()


def test_attempt_constraints(session):
    job = _job()
    session.add(job)
    session.commit()
    _rejected(session, _attempt(job, state=AttemptState.FAILED))
    _rejected(
        session, _attempt(job, state=AttemptState.SUCCEEDED, cause=FailureCause.INFRA)
    )
    _rejected(session, _attempt(job, 1), _attempt(job, 1, AttemptState.SUCCEEDED))
    _rejected(session, _attempt(job, 1), _attempt(job, 2))


def test_running_and_failed_coexist(session):
    job = _job()
    session.add(job)
    session.commit()
    session.add_all(
        [
            _attempt(job, 1, AttemptState.FAILED, FailureCause.WORKLOAD),
            _attempt(job, 2, AttemptState.RUNNING),
        ]
    )
    session.commit()
    assert [a.number for a in job.attempts] == [1, 2]


def _line(job, seq, attempt_number=1):
    return LogLine(
        job_id=job.id,
        seq=seq,
        attempt_number=attempt_number,
        ts=datetime.now(UTC),
        line="x",
    )


def test_log_line_constraints_and_cascade(session):
    job = _job()
    session.add(job)
    session.commit()
    session.add(_attempt(job, 1))
    session.commit()
    session.add(_line(job, 1))
    session.commit()
    _rejected(session, _line(job, 1))
    _rejected(session, _line(job, 2, attempt_number=7))

    session.execute(text("DELETE FROM jobs WHERE id=:i"), {"i": job.id})
    session.commit()
    assert session.execute(text("SELECT count(*) FROM attempts")).scalar() == 0
    assert session.execute(text("SELECT count(*) FROM log_lines")).scalar() == 0


def test_unset_database_url_fails_naming_it():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr
    assert "Traceback" not in result.stderr
