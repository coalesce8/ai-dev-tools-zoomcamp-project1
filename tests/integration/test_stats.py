"""GET /api/v1/stats and compute_stats against a real database."""

import logging
import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from runwatch.api.app import create_app
from runwatch.lifecycle import AttemptState as A
from runwatch.lifecycle import FailureCause as C
from runwatch.lifecycle import JobState as J
from runwatch.models import Attempt, Job
from runwatch.queue_stats import compute_stats
from runwatch.settings import Settings
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEYS = {
    "as_of", "window_seconds", "counts", "oldest_queued_age_seconds",
    "jobs_finished", "jobs_failed", "job_failure_rate", "attempts_finished",
    "attempts_failed_infra", "infra_attempt_failure_rate",
}  # fmt: skip
STATES = {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "STALLED", "CANCELLED"}


@pytest.fixture(scope="module", autouse=True)
def _schema():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")


@pytest.fixture(scope="module")
def app():
    return create_app(Settings(database_url=DATABASE_URL, sweeper_enabled=False))


@pytest.fixture
def session(app):
    with Session(app.state.engine) as s:
        s.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))
        s.commit()
        yield s


@pytest.fixture
def client(app, session):
    # Depends on `session` so every test starts from emptied tables.
    with TestClient(app) as c:
        yield c


def db_now(session):
    n = session.scalar(select(func.now()))
    session.rollback()
    return n


def sec(n, s):
    return n - timedelta(seconds=s)


def add_job(session, n, state, created, queued, finished=None, attempts=(), name="j"):
    """attempts: (number, state, cause, finished_s_ago or None)."""
    job = Job(
        name=name,
        spec={},
        state=state,
        created_at=sec(n, created),
        queued_at=sec(n, queued),
        finished_at=None if finished is None else sec(n, finished),
    )
    session.add(job)
    session.flush()
    for number, a_state, cause, fin in attempts:
        session.add(
            Attempt(
                job_id=job.id,
                number=number,
                state=a_state,
                failure_cause=cause,
                lease_expires_at=sec(n, 0),
                started_at=sec(n, 1000),
                finished_at=None if fin is None else sec(n, fin),
            )
        )
    session.flush()
    return job


def seed_ac11(session, n):
    F, IN, W = A.FAILED, C.INFRA, C.WORKLOAD
    add_job(session, n, J.QUEUED, 1000, 300)
    add_job(session, n, J.QUEUED, 150, 100, None, [(1, F, IN, 100)])
    add_job(
        session,
        n,
        J.RUNNING,
        500,
        400,
        None,
        [(1, F, W, 400), (2, A.RUNNING, None, None)],
    )
    add_job(session, n, J.RUNNING, 50, 50, None, [(1, A.RUNNING, None, None)])
    add_job(session, n, J.STALLED, 80, 80, None, [(1, F, IN, 20)])
    for created, fin in [(90, 60), (330, 300), (620, 590), (640, 610)]:
        add_job(
            session,
            n,
            J.SUCCEEDED,
            created,
            created,
            fin,
            [(1, A.SUCCEEDED, None, fin)],
        )
    add_job(
        session,
        n,
        J.FAILED,
        200,
        150,
        120,
        [(1, F, IN, 180), (2, F, IN, 150), (3, F, IN, 120)],
    )
    add_job(session, n, J.FAILED, 230, 230, 200, [(1, F, W, 200)])
    add_job(session, n, J.FAILED, 800, 760, 700, [(1, F, IN, 760), (2, F, IN, 700)])
    add_job(session, n, J.CANCELLED, 40, 40, 30, [(1, A.CANCELLED, None, 30)])
    session.commit()


def stats_sql(session, now):
    """Independent plain-SQL computation of the same numbers."""
    q = lambda sql: session.execute(text(sql), {"now": now}).scalar()  # noqa: E731
    w = "finished_at > :now - interval '600 seconds' AND finished_at <= :now"
    session.rollback()
    return {
        "counts": {
            s: q(f"SELECT count(*) FROM jobs WHERE state = '{s}'") for s in STATES
        },
        "oldest": q(
            "SELECT extract(epoch FROM :now - min(queued_at)) FROM jobs "
            "WHERE state = 'QUEUED'"
        ),
        "jobs_finished": q(
            f"SELECT count(*) FROM jobs WHERE state IN ('SUCCEEDED','FAILED') AND {w}"
        ),
        "jobs_failed": q(f"SELECT count(*) FROM jobs WHERE state = 'FAILED' AND {w}"),
        "attempts_finished": q(
            "SELECT count(*) FROM attempts "
            f"WHERE state IN ('SUCCEEDED','FAILED') AND {w}"
        ),
        "attempts_failed_infra": q(
            "SELECT count(*) FROM attempts WHERE state = 'FAILED' "
            f"AND failure_cause = 'infra' AND {w}"
        ),
    }


def get(client):
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    return r.json()


def test_empty_database(client):
    body = get(client)
    assert set(body) == KEYS
    assert body["window_seconds"] == 600
    assert re.search(r"(Z|\+00:00)$", body["as_of"])
    assert body["counts"] == dict.fromkeys(STATES, 0)
    assert body["oldest_queued_age_seconds"] is None
    for k in (
        "jobs_finished",
        "jobs_failed",
        "attempts_finished",
        "attempts_failed_infra",
    ):
        assert body[k] == 0
    assert body["job_failure_rate"] is None
    assert body["infra_attempt_failure_rate"] is None


def test_ac11_seeded(client, session):
    n = db_now(session)
    seed_ac11(session, n)
    body = get(client)
    assert set(body) == KEYS
    assert body["counts"] == {
        "QUEUED": 2, "RUNNING": 2, "SUCCEEDED": 4,
        "FAILED": 3, "STALLED": 1, "CANCELLED": 1,
    }  # fmt: skip
    assert body["oldest_queued_age_seconds"] == pytest.approx(300, abs=5)
    assert (body["jobs_finished"], body["jobs_failed"]) == (5, 2)
    assert body["job_failure_rate"] == 0.4
    assert (body["attempts_finished"], body["attempts_failed_infra"]) == (10, 5)
    assert body["infra_attempt_failure_rate"] == 0.5

    as_of = datetime.fromisoformat(body["as_of"])
    exp = stats_sql(session, as_of)
    assert body["counts"] == exp["counts"]
    assert body["oldest_queued_age_seconds"] == pytest.approx(
        float(exp["oldest"]), abs=1e-3
    )
    for k in (
        "jobs_finished",
        "jobs_failed",
        "attempts_finished",
        "attempts_failed_infra",
    ):
        assert body[k] == exp[k]
    assert body["job_failure_rate"] == exp["jobs_failed"] / exp["jobs_finished"]
    assert body["infra_attempt_failure_rate"] == (
        exp["attempts_failed_infra"] / exp["attempts_finished"]
    )


def fixed_now():
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "offset,inside",
    [
        (timedelta(seconds=-600), False),
        (timedelta(seconds=-600, microseconds=1), True),
        (timedelta(0), True),
        (timedelta(seconds=1), False),
    ],
)
def test_window_boundaries(session, offset, inside):
    t = fixed_now()
    fin = t + offset
    job = Job(
        name="b", spec={}, state=J.FAILED, created_at=t - timedelta(hours=1),
        queued_at=t - timedelta(hours=1), finished_at=fin,
    )  # fmt: skip
    session.add(job)
    session.flush()
    session.add(
        Attempt(
            job_id=job.id, number=1, state=A.FAILED, failure_cause=C.INFRA,
            lease_expires_at=t, started_at=t - timedelta(hours=1), finished_at=fin,
        )
    )  # fmt: skip
    session.commit()
    s = compute_stats(session, t)
    expect = 1 if inside else 0
    assert (s.jobs_finished, s.jobs_failed) == (expect, expect)
    assert (s.attempts_finished, s.attempts_failed_infra) == (expect, expect)


def test_queued_after_now_gives_zero_age(session):
    t = fixed_now()
    session.add(
        Job(
            name="f",
            spec={},
            state=J.QUEUED,
            created_at=t,
            queued_at=t + timedelta(seconds=30),
        )
    )
    session.commit()
    assert compute_stats(session, t).oldest_queued_age_seconds == 0.0


def test_oldest_uses_queued_jobs_only(session):
    t = fixed_now()
    old = t - timedelta(hours=1)
    for st in (J.RUNNING, J.STALLED):
        session.add(Job(name="o", spec={}, state=st, created_at=old, queued_at=old))
    session.add(
        Job(
            name="q",
            spec={},
            state=J.QUEUED,
            created_at=old,
            queued_at=t - timedelta(seconds=40),
        )
    )
    session.commit()
    assert compute_stats(session, t).oldest_queued_age_seconds == 40.0


def test_only_queued_jobs(client, session):
    n = db_now(session)
    add_job(session, n, J.QUEUED, 100, 60)
    session.commit()
    body = get(client)
    assert body["counts"]["QUEUED"] == 1
    assert body["oldest_queued_age_seconds"] == pytest.approx(60, abs=5)
    assert body["job_failure_rate"] is None
    assert body["infra_attempt_failure_rate"] is None


def test_only_cancelled(client, session):
    n = db_now(session)
    add_job(session, n, J.CANCELLED, 50, 50, 20, [(1, A.CANCELLED, None, 20)])
    session.commit()
    body = get(client)
    assert body["jobs_finished"] == 0 and body["attempts_finished"] == 0
    assert body["job_failure_rate"] is None
    assert body["infra_attempt_failure_rate"] is None


def test_all_workload_failures_rate_zero(client, session):
    n = db_now(session)
    add_job(session, n, J.FAILED, 50, 50, 20, [(1, A.FAILED, C.WORKLOAD, 20)])
    session.commit()
    body = get(client)
    assert body["attempts_finished"] == 1
    assert body["attempts_failed_infra"] == 0
    assert body["infra_attempt_failure_rate"] == 0.0
    assert body["job_failure_rate"] == 1.0


def test_stalled_attempt_counts_job_does_not(client, session):
    n = db_now(session)
    add_job(session, n, J.STALLED, 80, 80, None, [(1, A.FAILED, C.INFRA, 20)])
    session.commit()
    body = get(client)
    assert body["jobs_finished"] == 0
    assert (body["attempts_finished"], body["attempts_failed_infra"]) == (1, 1)
    assert body["infra_attempt_failure_rate"] == 1.0


def test_requeued_failed_job(client, session):
    n = db_now(session)
    add_job(session, n, J.QUEUED, 200, 10, None, [(1, A.FAILED, C.INFRA, 60)])
    session.commit()
    body = get(client)
    assert body["jobs_failed"] == 0 and body["jobs_finished"] == 0
    assert body["job_failure_rate"] is None
    assert (body["attempts_finished"], body["attempts_failed_infra"]) == (1, 1)


def test_queue_wait_drill_age_uses_queued_at(client, session):
    n = db_now(session)
    add_job(session, n, J.QUEUED, 1800, 10, None, [(1, A.FAILED, C.INFRA, 600)])
    session.commit()
    assert get(client)["oldest_queued_age_seconds"] == pytest.approx(10, abs=5)


def test_route_does_not_write(client, session):
    n = db_now(session)
    seed_ac11(session, n)

    def counts():
        session.rollback()
        return [
            session.scalar(text(f"SELECT count(*) FROM {t}"))
            for t in ("jobs", "attempts", "log_lines")
        ]

    before = counts()
    get(client)
    assert counts() == before


def test_repeatable(client, session):
    n = db_now(session)
    seed_ac11(session, n)
    a, b = get(client), get(client)
    for k in KEYS - {"as_of", "oldest_queued_age_seconds"}:
        assert a[k] == b[k]
    assert b["oldest_queued_age_seconds"] >= a["oldest_queued_age_seconds"]


def test_no_job_name_in_logs(client, session, caplog):
    n = db_now(session)
    add_job(session, n, J.QUEUED, 10, 10, name="secret-name-xyz")
    session.commit()
    with caplog.at_level(logging.DEBUG):
        get(client)
    assert all("secret-name-xyz" not in r.getMessage() for r in caplog.records)
    assert "secret-name-xyz" not in caplog.text


def test_openapi_lists_stats(client):
    spec = client.get("/openapi.json").json()
    schema = spec["paths"]["/api/v1/stats"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    name = schema["$ref"].rsplit("/", 1)[1]
    assert set(spec["components"]["schemas"][name]["properties"]) == KEYS


def test_post_not_allowed(client):
    r = client.post("/api/v1/stats")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"
