"""Requeue and cancel endpoints against the database in DATABASE_URL.

Jobs and attempts are seeded directly through the ORM models.
"""

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from runwatch import lifecycle
from runwatch.api.app import create_app
from runwatch.api.jobs_control import requeue_failed, requeue_stalled
from runwatch.lifecycle import AttemptState, FailureCause, JobState
from runwatch.models import Attempt, Job
from runwatch.settings import Settings
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOUR = timedelta(hours=1)
MARKER = "zq-secret-marker-7731"


@pytest.fixture(scope="module")
def engine():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")
    engine = create_engine(DATABASE_URL, pool_size=10)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))


@pytest.fixture
def client(clean):
    app = create_app(Settings(database_url=DATABASE_URL))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def sf(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def db_now(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT now()")).scalar_one()


def seed(
    sf,
    *,
    state,
    attempts=(),
    attempt_count=0,
    last=None,
    queued_ago=HOUR,
    finished_at=None,
    name="job",
    last_error=None,
):
    """attempts: tuples (state, cause, exit_reason)."""
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    with sf() as s:
        s.add(
            Job(
                id=job_id,
                name=name,
                spec={"duration_s": 5},
                state=state,
                attempt_count=attempt_count,
                last_attempt_number=len(attempts) if last is None else last,
                queued_at=now - queued_ago,
                started_at=now - 2 * HOUR if attempts else None,
                finished_at=finished_at,
                last_error=last_error,
            )
        )
        s.flush()
        for i, (a_state, cause, reason) in enumerate(attempts, start=1):
            s.add(
                Attempt(
                    job_id=job_id,
                    number=i,
                    state=a_state,
                    lease_expires_at=now + timedelta(seconds=15),
                    started_at=now - 2 * HOUR,
                    finished_at=None if a_state is AttemptState.RUNNING else now - HOUR,
                    exit_reason=reason,
                    failure_cause=cause,
                )
            )
        s.commit()
    return job_id


def rows(sf, job_id):
    with sf() as s:
        job = s.get(Job, job_id)
        cols = {c.name: getattr(job, c.name) for c in Job.__table__.columns}
        atts = [
            {c.name: getattr(a, c.name) for c in Attempt.__table__.columns}
            for a in s.execute(
                select(Attempt).where(Attempt.job_id == job_id).order_by(Attempt.number)
            ).scalars()
        ]
    return cols, atts


F, W, IN = AttemptState.FAILED, FailureCause.WORKLOAD, FailureCause.INFRA


def failed_job(sf, **kw):
    return seed(
        sf,
        state=JobState.FAILED,
        attempts=[(F, W, "x"), (F, W, "y"), (F, W, "z")],
        attempt_count=3,
        finished_at=datetime.now(UTC) - HOUR,
        last_error="boom",
        **kw,
    )


def stalled_job(sf, **kw):
    return seed(
        sf,
        state=JobState.STALLED,
        attempts=[(F, IN, "lease expired")],
        attempt_count=1,
        last_error="lease expired",
        **kw,
    )


# ---------------------------------------------------------------- requeue


@pytest.mark.parametrize("make", [failed_job, stalled_job])
def test_requeue_ok(client, sf, engine, make):
    jid = make(sf)
    before_job, before_atts = rows(sf, jid)
    r = client.post(f"/api/v1/jobs/{jid}/requeue")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "QUEUED"
    assert body["attempt_count"] == 0
    assert body["finished_at"] is None
    assert body["last_error"] == before_job["last_error"]
    assert body["started_at"] is not None
    after_job, after_atts = rows(sf, jid)
    assert after_job["state"] is JobState.QUEUED
    assert after_job["attempt_count"] == 0
    assert after_job["finished_at"] is None
    assert after_job["last_attempt_number"] == before_job["last_attempt_number"]
    assert after_job["max_attempts"] == before_job["max_attempts"]
    assert after_job["started_at"] == before_job["started_at"]
    assert after_atts == before_atts
    # queued_at reset to the database clock
    assert after_job["queued_at"] > before_job["queued_at"]
    assert abs((db_now(engine) - after_job["queued_at"]).total_seconds()) < 5


def test_requeue_goes_behind_earlier_queued(client, sf):
    earlier = seed(sf, state=JobState.QUEUED, queued_ago=timedelta(minutes=5))
    jid = failed_job(sf)
    assert client.post(f"/api/v1/jobs/{jid}/requeue").status_code == 200
    assert rows(sf, earlier)[0]["queued_at"] < rows(sf, jid)[0]["queued_at"]


def test_requeued_row_claims_next_attempt_number(client, sf):
    jid = failed_job(sf)
    client.post(f"/api/v1/jobs/{jid}/requeue")
    job = rows(sf, jid)[0]
    snap = lifecycle.JobSnapshot(
        state=job["state"],
        attempt_count=job["attempt_count"],
        max_attempts=job["max_attempts"],
        last_attempt_number=job["last_attempt_number"],
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        last_error=job["last_error"],
    )
    new_job, attempt = lifecycle.claim(snap, datetime.now(UTC))
    assert attempt.number == 4
    assert new_job.attempt_count == 1


@pytest.mark.parametrize(
    "state", [JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED, JobState.CANCELLED]
)
def test_requeue_conflict(client, sf, state):
    atts = [(AttemptState.RUNNING, None, None)] if state is JobState.RUNNING else []
    jid = seed(
        sf,
        state=state,
        attempts=atts,
        attempt_count=len(atts),
        finished_at=(
            datetime.now(UTC)
            if state in (JobState.SUCCEEDED, JobState.CANCELLED)
            else None
        ),
    )
    before = rows(sf, jid)
    r = client.post(f"/api/v1/jobs/{jid}/requeue")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert rows(sf, jid) == before


def test_requeue_twice(client, sf):
    jid = failed_job(sf)
    assert client.post(f"/api/v1/jobs/{jid}/requeue").status_code == 200
    assert client.post(f"/api/v1/jobs/{jid}/requeue").status_code == 409


# ----------------------------------------------------------------- cancel


def test_cancel_queued(client, sf, engine):
    jid = seed(sf, state=JobState.QUEUED)
    r = client.post(f"/api/v1/jobs/{jid}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "CANCELLED"
    assert r.json()["finished_at"] is not None
    job, atts = rows(sf, jid)
    assert job["state"] is JobState.CANCELLED
    assert abs((db_now(engine) - job["finished_at"]).total_seconds()) < 5
    assert atts == []


def test_cancel_stalled(client, sf):
    jid = stalled_job(sf)
    _, before = rows(sf, jid)
    r = client.post(f"/api/v1/jobs/{jid}/cancel")
    assert r.status_code == 200
    job, atts = rows(sf, jid)
    assert job["state"] is JobState.CANCELLED and job["finished_at"] is not None
    assert atts == before
    assert atts[0]["state"] is F and atts[0]["failure_cause"] is IN
    assert atts[0]["exit_reason"] == "lease expired"


def _snap(job, att):
    return (
        lifecycle.JobSnapshot(
            state=job["state"],
            attempt_count=job["attempt_count"],
            max_attempts=job["max_attempts"],
            last_attempt_number=job["last_attempt_number"],
            started_at=job["started_at"],
            finished_at=job["finished_at"],
            last_error=job["last_error"],
        ),
        lifecycle.AttemptSnapshot(
            number=att["number"],
            state=att["state"],
            started_at=att["started_at"],
            lease_expires_at=att["lease_expires_at"],
            finished_at=att["finished_at"],
            exit_reason=att["exit_reason"],
            failure_cause=att["failure_cause"],
        ),
    )


@pytest.mark.parametrize("expired", [False, True])
def test_cancel_running(client, sf, expired):
    jid = seed(
        sf,
        state=JobState.RUNNING,
        attempts=[(F, W, "x"), (AttemptState.RUNNING, None, None)],
        attempt_count=2,
    )
    if expired:
        with sf() as s:
            s.execute(
                text(
                    "UPDATE attempts SET lease_expires_at = now() - interval '1 minute'"
                    " WHERE job_id = :j AND number = 2"
                ),
                {"j": jid},
            )
            s.commit()
    _, (a1, _a2) = rows(sf, jid)
    r = client.post(f"/api/v1/jobs/{jid}/cancel")
    assert r.status_code == 200, r.text
    job, (b1, b2) = rows(sf, jid)
    assert job["state"] is JobState.CANCELLED
    assert job["finished_at"] is not None
    assert b1 == a1
    assert b2["state"] is AttemptState.CANCELLED
    assert b2["finished_at"] == job["finished_at"]
    assert b2["exit_reason"] == "cancelled"
    assert b2["failure_cause"] is None
    js, as_ = _snap(job, b2)
    assert not lifecycle.is_current(js, as_)


@pytest.mark.parametrize(
    "state", [JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED]
)
def test_cancel_conflict(client, sf, state):
    jid = seed(
        sf,
        state=state,
        attempts=[
            (
                AttemptState.SUCCEEDED if state is JobState.SUCCEEDED else F,
                None if state is JobState.SUCCEEDED else W,
                None if state is JobState.SUCCEEDED else "x",
            )
        ],
        attempt_count=1,
        finished_at=datetime.now(UTC),
    )
    before = rows(sf, jid)
    r = client.post(f"/api/v1/jobs/{jid}/cancel")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert rows(sf, jid) == before


def test_cancel_twice(client, sf):
    jid = seed(sf, state=JobState.QUEUED)
    assert client.post(f"/api/v1/jobs/{jid}/cancel").status_code == 200
    assert client.post(f"/api/v1/jobs/{jid}/cancel").status_code == 409


def test_cancel_waits_for_row_lock(client, sf, engine):
    jid = seed(
        sf,
        state=JobState.RUNNING,
        attempts=[(AttemptState.RUNNING, None, None)],
        attempt_count=1,
    )
    holder = engine.connect()
    tx = holder.begin()
    holder.execute(text("SELECT 1 FROM jobs WHERE id = :j FOR UPDATE"), {"j": jid})
    result = {}
    t = threading.Thread(
        target=lambda: result.setdefault("r", client.post(f"/api/v1/jobs/{jid}/cancel"))
    )
    t.start()
    time.sleep(1)
    assert "r" not in result, "cancel returned while the row was locked"
    holder.execute(
        text("UPDATE jobs SET state='SUCCEEDED', finished_at=now() WHERE id=:j"),
        {"j": jid},
    )
    holder.execute(
        text(
            "UPDATE attempts SET state='SUCCEEDED', finished_at=now() WHERE job_id=:j"
        ),
        {"j": jid},
    )
    tx.commit()
    holder.close()
    t.join(10)
    assert result["r"].status_code == 409
    job, (att,) = rows(sf, jid)
    assert job["state"] is JobState.SUCCEEDED
    assert att["state"] is AttemptState.SUCCEEDED


# -------------------------------------------------------- both single routes


@pytest.mark.parametrize("action", ["requeue", "cancel"])
@pytest.mark.parametrize("bad", [str(uuid.uuid4()), "not-a-uuid", "123"])
def test_unknown_or_malformed_id(client, action, bad):
    r = client.post(f"/api/v1/jobs/{bad}/{action}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("action", ["requeue", "cancel"])
def test_get_is_405(client, sf, action):
    jid = failed_job(sf)
    r = client.get(f"/api/v1/jobs/{jid}/{action}")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"


def test_json_body_ignored(client, sf):
    a = failed_job(sf)
    b = seed(sf, state=JobState.QUEUED)
    assert client.post(f"/api/v1/jobs/{a}/requeue", json={"x": 1}).status_code == 200
    assert client.post(f"/api/v1/jobs/{b}/cancel", json={"x": 1}).status_code == 200


# --------------------------------------------------------- requeue-stalled


def test_requeue_stalled(client, sf, engine):
    stalled = [stalled_job(sf) for _ in range(3)]
    others = [
        seed(sf, state=JobState.QUEUED),
        seed(
            sf,
            state=JobState.RUNNING,
            attempts=[(AttemptState.RUNNING, None, None)],
            attempt_count=1,
        ),
        seed(sf, state=JobState.SUCCEEDED, finished_at=datetime.now(UTC)),
        failed_job(sf),
        seed(sf, state=JobState.CANCELLED, finished_at=datetime.now(UTC)),
    ]
    before_others = [rows(sf, j) for j in others]
    before_atts = [rows(sf, j)[1] for j in stalled]
    r = client.post("/internal/v1/jobs/requeue-stalled")
    assert r.status_code == 200
    assert r.json() == {"requeued": 3}
    queued_ats = set()
    for j, atts in zip(stalled, before_atts, strict=True):
        job, a = rows(sf, j)
        assert job["state"] is JobState.QUEUED
        assert job["attempt_count"] == 0 and job["finished_at"] is None
        assert a == atts
        queued_ats.add(job["queued_at"])
    assert len(queued_ats) == 1
    assert abs((db_now(engine) - queued_ats.pop()).total_seconds()) < 5
    assert [rows(sf, j) for j in others] == before_others
    again = client.post("/internal/v1/jobs/requeue-stalled", json={"x": 1})
    assert again.json() == {"requeued": 0}


def test_requeue_stalled_none(client, sf):
    jid = seed(sf, state=JobState.QUEUED)
    before = rows(sf, jid)
    assert client.post("/internal/v1/jobs/requeue-stalled").json() == {"requeued": 0}
    assert rows(sf, jid) == before


def test_requeue_stalled_concurrent(sf, engine):
    ids = [stalled_job(sf) for _ in range(10)]
    barrier = threading.Barrier(2)

    def run():
        with sf() as s:
            barrier.wait()
            return requeue_stalled(s)

    with ThreadPoolExecutor(2) as pool:
        counts = [f.result(20) for f in [pool.submit(run), pool.submit(run)]]
    assert sum(counts) == 10
    assert all(rows(sf, j)[0]["state"] is JobState.QUEUED for j in ids)


# ---------------------------------------------------------- requeue-failed

T0 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 26, 11, 0, tzinfo=UTC)
MID = T0 + timedelta(minutes=30)


def fj(sf, finished, causes, state=JobState.FAILED):
    atts = [(F, c, "x") for c in causes]
    return seed(
        sf,
        state=state,
        attempts=atts,
        attempt_count=len(atts),
        finished_at=finished,
        last_error="e",
    )


def window_seed(sf):
    ids = {
        "A": fj(sf, MID, [IN]),
        "B": fj(sf, MID, [W]),
        "C": fj(sf, MID, [IN, IN, W]),
        "D": fj(sf, MID, [W, W, IN]),
        "E": fj(sf, T0 - timedelta(seconds=1), [IN]),
        "F": fj(sf, T1 + timedelta(seconds=1), [IN]),
        "G1": fj(sf, T0, [IN]),
        "G2": fj(sf, T1, [IN]),
        "H": fj(sf, None, [IN], state=JobState.STALLED),
        "I1": seed(
            sf,
            state=JobState.SUCCEEDED,
            attempts=[(AttemptState.SUCCEEDED, None, None)],
            attempt_count=1,
            finished_at=MID,
        ),
        "I2": seed(sf, state=JobState.CANCELLED, finished_at=MID),
    }
    return ids


def test_requeue_failed_window(client, sf, engine):
    ids = window_seed(sf)
    before = {k: rows(sf, v) for k, v in ids.items()}
    r = client.post(
        "/internal/v1/jobs/requeue-failed",
        json={"since": T0.isoformat(), "until": T1.isoformat()},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"requeued": 4}
    changed = {"A", "D", "G1", "G2"}
    stamps = set()
    for k, v in ids.items():
        job, atts = rows(sf, v)
        if k in changed:
            assert job["state"] is JobState.QUEUED
            assert job["attempt_count"] == 0 and job["finished_at"] is None
            assert job["last_attempt_number"] == before[k][0]["last_attempt_number"]
            assert atts == before[k][1]
            stamps.add(job["queued_at"])
        else:
            assert (job, atts) == before[k], k
    assert len(stamps) == 1
    assert abs((db_now(engine) - stamps.pop()).total_seconds()) < 5
    again = client.post(
        "/internal/v1/jobs/requeue-failed",
        json={"since": T0.isoformat(), "until": T1.isoformat()},
    )
    assert again.json() == {"requeued": 0}


def test_requeue_failed_last_attempt_is_by_number(client, sf):
    # attempt rows: number 1 infra, number 2 workload; last_attempt_number = 1
    jid = seed(
        sf,
        state=JobState.FAILED,
        attempts=[(F, IN, "x"), (F, W, "y")],
        last=1,
        attempt_count=1,
        finished_at=MID,
    )
    r = client.post(
        "/internal/v1/jobs/requeue-failed",
        json={"since": T0.isoformat(), "until": T1.isoformat()},
    )
    assert r.json() == {"requeued": 1}
    assert rows(sf, jid)[0]["state"] is JobState.QUEUED


def test_requeue_failed_offset_window(client, sf):
    window_seed(sf)
    r = client.post(
        "/internal/v1/jobs/requeue-failed",
        json={
            "since": "2026-10-26T12:00:00+02:00",
            "until": "2026-10-26T13:00:00+02:00",
        },
    )
    assert r.json() == {"requeued": 4}


def test_requeue_failed_point_window(client, sf):
    window_seed(sf)
    r = client.post(
        "/internal/v1/jobs/requeue-failed",
        json={"since": T0.isoformat(), "until": T0.isoformat()},
    )
    assert r.json() == {"requeued": 1}


@pytest.mark.parametrize(
    "body",
    [
        {"until": "2026-10-26T11:00:00+00:00"},
        {"since": "2026-10-26T10:00:00+00:00"},
        {"since": "2026-10-26T11:00:00+00:00", "until": "2026-10-26T10:00:00+00:00"},
        {"since": "2026-10-26T10:00:00", "until": "2026-10-26T11:00:00+00:00"},
        {"since": "nope", "until": "2026-10-26T11:00:00+00:00"},
        {"since": "2026-10-26T10:00:00Z", "until": "2026-10-26T11:00:00Z", "x": 1},
        None,
    ],
)
def test_requeue_failed_invalid(client, sf, body):
    ids = window_seed(sf)
    before = {k: rows(sf, v) for k, v in ids.items()}
    r = client.post("/internal/v1/jobs/requeue-failed", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"
    assert {k: rows(sf, v) for k, v in ids.items()} == before


def test_requeue_failed_concurrent(sf):
    for _ in range(10):
        fj(sf, MID, [IN])
    barrier = threading.Barrier(2)

    def run():
        with sf() as s:
            barrier.wait()
            return requeue_failed(s, T0, T1)

    with ThreadPoolExecutor(2) as pool:
        counts = [f.result(20) for f in [pool.submit(run), pool.submit(run)]]
    assert sum(counts) == 10


# ----------------------------------------------------------------- privacy


def test_logs_do_not_contain_job_name(client, sf, caplog):
    caplog.set_level(logging.DEBUG)
    a = failed_job(sf, name=MARKER)
    b = seed(sf, state=JobState.QUEUED, name=MARKER)
    fj(sf, MID, [IN])
    client.post(f"/api/v1/jobs/{a}/requeue")
    client.post(f"/api/v1/jobs/{b}/cancel")
    client.post("/internal/v1/jobs/requeue-stalled")
    client.post(
        "/internal/v1/jobs/requeue-failed",
        json={"since": T0.isoformat(), "until": T1.isoformat()},
    )
    assert caplog.records
    assert not any(MARKER in r.getMessage() for r in caplog.records)
    assert not any("boom" in r.getMessage() for r in caplog.records)
