"""Heartbeat and result endpoints against the database in DATABASE_URL.

Jobs and attempts are inserted directly, so no other endpoint is needed.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from runwatch.api.app import create_app
from runwatch.settings import Settings
from sqlalchemy import create_engine, text

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TS = "2026-01-01T00:00:00+00:00"


@pytest.fixture(scope="module")
def engine():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")
    engine = create_engine(DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def client():
    app = create_app(Settings(database_url=DATABASE_URL, sweeper_enabled=False))
    with TestClient(app) as client:
        yield client


def q(engine, sql, **params):
    with engine.begin() as conn:
        res = conn.execute(text(sql), params)
        return res.mappings().all() if res.returns_rows else []


def make_job(
    engine,
    *,
    state="RUNNING",
    attempt_count=1,
    max_attempts=3,
    last=1,
    queued_ago=timedelta(hours=1),
):
    job_id = uuid.uuid4()
    q(
        engine,
        "INSERT INTO jobs (id, name, spec, state, max_attempts, attempt_count,"
        " last_attempt_number, queued_at) VALUES (:id, 'secret-name', '{}',"
        " :state, :m, :c, :l, now() - make_interval(secs => :ago))",
        id=job_id,
        state=state,
        m=max_attempts,
        c=attempt_count,
        l=last,
        ago=queued_ago.total_seconds(),
    )
    return job_id


def make_attempt(
    engine, job_id, number=1, state="RUNNING", cause=None, lease_offset=10
):
    attempt_id = uuid.uuid4()
    q(
        engine,
        "INSERT INTO attempts (id, job_id, number, state, lease_expires_at,"
        " failure_cause) VALUES (:id, :j, :n, :s,"
        " now() + make_interval(secs => :o), :c)",
        id=attempt_id,
        j=job_id,
        n=number,
        s=state,
        o=lease_offset,
        c=cause,
    )
    return attempt_id


def running(engine, **kw):
    job_id = make_job(engine, **kw)
    return job_id, make_attempt(engine, job_id, number=kw.get("last", 1))


def hb(client, attempt_id, lines=None):
    body = {} if lines is None else {"lines": lines}
    return client.post(
        f"/internal/v1/worker/attempts/{attempt_id}/heartbeat", json=body
    )


def result(client, attempt_id, body):
    return client.post(f"/internal/v1/worker/attempts/{attempt_id}/result", json=body)


def fail_body(reason="boom", cause="workload"):
    return {"outcome": "failed", "reason": reason, "cause": cause}


def lines(n, prefix="l"):
    return [{"ts": TS, "line": f"{prefix}{i}"} for i in range(n)]


def snapshot(engine, job_id):
    return (
        [dict(r) for r in q(engine, "SELECT * FROM jobs WHERE id=:i", i=job_id)],
        [
            dict(r)
            for r in q(
                engine,
                "SELECT * FROM attempts WHERE job_id=:i ORDER BY number",
                i=job_id,
            )
        ],
        [
            dict(r)
            for r in q(
                engine, "SELECT * FROM log_lines WHERE job_id=:i ORDER BY seq", i=job_id
            )
        ],
    )


def db_now_plus(engine, secs=0):
    return q(engine, "SELECT now() + make_interval(secs => :s) AS t", s=secs)[0]["t"]


# --- not found -------------------------------------------------------------


def test_unknown_attempt_is_404(client, engine):
    missing = uuid.uuid4()
    for response in (
        hb(client, missing),
        result(client, missing, {"outcome": "succeeded"}),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


# --- heartbeat -------------------------------------------------------------


def test_heartbeat_extends_lease_from_now(client, engine):
    job_id, attempt_id = running(engine)
    before = snapshot(engine, job_id)
    response = hb(client, attempt_id)
    assert response.status_code == 200
    expected = db_now_plus(engine, 15)
    body = response.json()
    assert body["attempt_id"] == str(attempt_id)
    assert body["lines_accepted"] == 0 and body["lines_dropped"] == 0
    stored = q(
        engine, "SELECT lease_expires_at FROM attempts WHERE id=:i", i=attempt_id
    )[0]
    assert abs(stored["lease_expires_at"] - expected) < timedelta(seconds=2)
    assert (
        datetime.fromisoformat(body["lease_expires_at"]) == stored["lease_expires_at"]
    )
    jobs_after, attempts_after, lines_after = snapshot(engine, job_id)
    assert jobs_after == before[0] and lines_after == []
    for key in attempts_after[0]:
        if key != "lease_expires_at":
            assert attempts_after[0][key] == before[1][0][key]

    first = stored["lease_expires_at"]
    assert hb(client, attempt_id).status_code == 200
    second = q(
        engine, "SELECT lease_expires_at FROM attempts WHERE id=:i", i=attempt_id
    )[0]
    assert second["lease_expires_at"] >= first
    assert abs(second["lease_expires_at"] - db_now_plus(engine, 15)) < timedelta(
        seconds=2
    )


def test_heartbeat_after_lease_passed_still_extends(client, engine):
    job_id = make_job(engine)
    attempt_id = make_attempt(engine, job_id, lease_offset=-60)
    assert hb(client, attempt_id).status_code == 200
    stored = q(
        engine, "SELECT lease_expires_at FROM attempts WHERE id=:i", i=attempt_id
    )[0]
    assert abs(stored["lease_expires_at"] - db_now_plus(engine, 15)) < timedelta(
        seconds=2
    )


def test_heartbeat_stores_lines_with_seq(client, engine):
    job_id, attempt_id = running(engine)
    payload = [
        {"ts": "2026-01-01T01:00:00+02:00", "line": "first"},
        {"ts": TS, "line": ""},
        {"ts": TS, "line": "third"},
    ]
    response = hb(client, attempt_id, payload)
    assert response.json()["lines_accepted"] == 3
    hb(client, attempt_id, lines(2))
    rows = q(engine, "SELECT * FROM log_lines WHERE job_id=:i ORDER BY seq", i=job_id)
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["line"] for r in rows[:3]] == ["first", "", "third"]
    assert rows[0]["ts"] == datetime(2026, 1, 1, 1, 0, tzinfo=timezone_plus2())
    assert {r["attempt_number"] for r in rows} == {1}


def timezone_plus2():
    from datetime import timezone

    return timezone(timedelta(hours=2))


def test_seq_continues_across_attempts_and_requeue(client, engine):
    job_id, a1 = running(engine, max_attempts=3)
    hb(client, a1, lines(3))
    assert result(client, a1, fail_body()).json()["job_state"] == "QUEUED"
    # simulate the next claim
    q(
        engine,
        "UPDATE jobs SET state='RUNNING', attempt_count=2, last_attempt_number=2"
        " WHERE id=:i",
        i=job_id,
    )
    a2 = make_attempt(engine, job_id, number=2)
    response = hb(client, a2, lines(2))
    assert response.json()["lines_accepted"] == 2
    rows = q(
        engine,
        "SELECT seq, attempt_number FROM log_lines WHERE job_id=:i ORDER BY seq",
        i=job_id,
    )
    assert [(r["seq"], r["attempt_number"]) for r in rows] == [
        (1, 1),
        (2, 1),
        (3, 1),
        (4, 2),
        (5, 2),
    ]


def test_cap_per_attempt(client, engine):
    job_id, a1 = running(engine)
    q(
        engine,
        "INSERT INTO log_lines (job_id, seq, attempt_number, ts, line)"
        " SELECT :j, g, 1, now(), 'x' FROM generate_series(1, 995) g",
        j=job_id,
    )
    before_lease = q(engine, "SELECT lease_expires_at FROM attempts WHERE id=:i", i=a1)[
        0
    ]
    response = hb(client, a1, lines(10))
    assert response.status_code == 200
    assert response.json()["lines_accepted"] == 5
    assert response.json()["lines_dropped"] == 5
    count = q(
        engine, "SELECT count(*) c, max(seq) m FROM log_lines WHERE job_id=:i", i=job_id
    )[0]
    assert (count["c"], count["m"]) == (1000, 1000)
    assert [
        r["line"]
        for r in q(
            engine,
            "SELECT line FROM log_lines WHERE job_id=:i AND seq>995 ORDER BY seq",
            i=job_id,
        )
    ] == [f"l{i}" for i in range(5)]
    # further lines are dropped but the lease still moves
    response = hb(client, a1, lines(3))
    assert response.json() == {
        **response.json(),
        "lines_accepted": 0,
        "lines_dropped": 3,
    }
    after = q(engine, "SELECT lease_expires_at FROM attempts WHERE id=:i", i=a1)[0]
    assert after["lease_expires_at"] != before_lease["lease_expires_at"]
    # the next attempt starts a fresh cap
    q(
        engine,
        "UPDATE attempts SET state='FAILED', failure_cause='infra' WHERE id=:i",
        i=a1,
    )
    q(
        engine,
        "UPDATE jobs SET attempt_count=2, last_attempt_number=2 WHERE id=:i",
        i=job_id,
    )
    a2 = make_attempt(engine, job_id, number=2)
    response = hb(client, a2, lines(100))
    assert response.json()["lines_accepted"] == 100
    assert (
        q(engine, "SELECT max(seq) m FROM log_lines WHERE job_id=:i", i=job_id)[0]["m"]
        == 1100
    )


def test_concurrent_heartbeats_no_collisions(client, engine):
    job_id, attempt_id = running(engine)
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(
            pool.map(lambda n: hb(client, attempt_id, lines(5, f"t{n}-")), range(10))
        )
    assert [r.status_code for r in responses] == [200] * 10
    seqs = [
        r["seq"]
        for r in q(
            engine, "SELECT seq FROM log_lines WHERE job_id=:i ORDER BY seq", i=job_id
        )
    ]
    assert seqs == list(range(1, 51))


# --- result succeeded ------------------------------------------------------


def test_result_succeeded(client, engine):
    job_id, attempt_id = running(engine)
    before = snapshot(engine, job_id)
    response = result(client, attempt_id, {"outcome": "succeeded"})
    assert response.status_code == 200
    assert response.json() == {
        "attempt_id": str(attempt_id),
        "attempt_state": "SUCCEEDED",
        "job_id": str(job_id),
        "job_state": "SUCCEEDED",
    }
    jobs, attempts, _ = snapshot(engine, job_id)
    job, attempt = jobs[0], attempts[0]
    assert job["state"] == "SUCCEEDED" and job["finished_at"] is not None
    for key in (
        "attempt_count",
        "last_attempt_number",
        "started_at",
        "queued_at",
        "last_error",
    ):
        assert job[key] == before[0][0][key]
    assert attempt["state"] == "SUCCEEDED" and attempt["finished_at"] is not None
    assert attempt["failure_cause"] is None and attempt["exit_reason"] is None


# --- result failed ---------------------------------------------------------


@pytest.mark.parametrize("cause", ["workload", "infra"])
def test_result_failed_with_attempts_left_requeues(client, engine, cause):
    job_id, attempt_id = running(engine)
    old_queued = q(
        engine, "SELECT queued_at, started_at FROM jobs WHERE id=:i", i=job_id
    )[0]
    response = result(client, attempt_id, fail_body("it broke", cause))
    assert response.status_code == 200
    assert response.json()["attempt_state"] == "FAILED"
    assert response.json()["job_state"] == "QUEUED"
    jobs, attempts, _ = snapshot(engine, job_id)
    job, attempt = jobs[0], attempts[0]
    assert attempt["state"] == "FAILED" and attempt["finished_at"] is not None
    assert attempt["exit_reason"] == "it broke" and attempt["failure_cause"] == cause
    assert job["state"] == "QUEUED" and job["finished_at"] is None
    assert job["last_error"] == "it broke"
    assert (job["attempt_count"], job["last_attempt_number"]) == (1, 1)
    assert job["started_at"] == old_queued["started_at"]
    assert job["queued_at"] > old_queued["queued_at"]
    assert abs(job["queued_at"] - db_now_plus(engine)) < timedelta(seconds=2)
    # the running slot is free for the next claim
    make_attempt(engine, job_id, number=2)


def test_result_failed_without_attempts_left(client, engine):
    job_id, attempt_id = running(engine, attempt_count=3, last=3, max_attempts=3)
    old = q(engine, "SELECT queued_at FROM jobs WHERE id=:i", i=job_id)[0]["queued_at"]
    response = result(client, attempt_id, fail_body("final", "infra"))
    assert response.json()["job_state"] == "FAILED"
    job = snapshot(engine, job_id)[0][0]
    assert job["state"] == "FAILED" and job["finished_at"] is not None
    assert job["last_error"] == "final" and job["queued_at"] == old


def test_max_attempts_one(client, engine):
    job_id, attempt_id = running(engine, max_attempts=1)
    assert result(client, attempt_id, fail_body()).json()["job_state"] == "FAILED"


def test_reason_truncated(client, engine):
    job_id, attempt_id = running(engine)
    reason = "é" * 1500
    assert result(client, attempt_id, fail_body(reason)).status_code == 200
    jobs, attempts, _ = snapshot(engine, job_id)
    assert jobs[0]["last_error"] == "é" * 1000
    assert attempts[0]["exit_reason"] == "é" * 1000


def test_ac5_three_workload_failures(client, engine):
    job_id = make_job(engine, state="QUEUED", attempt_count=0, max_attempts=3, last=0)
    states = []
    for n in (1, 2, 3):
        # test-claim
        q(
            engine,
            "UPDATE jobs SET state='RUNNING', attempt_count=:n,"
            " last_attempt_number=:n WHERE id=:i",
            n=n,
            i=job_id,
        )
        attempt_id = make_attempt(engine, job_id, number=n)
        states.append(
            result(client, attempt_id, fail_body("always_fail")).json()["job_state"]
        )
    assert states == ["QUEUED", "QUEUED", "FAILED"]
    jobs, attempts, _ = snapshot(engine, job_id)
    assert jobs[0]["state"] == "FAILED" and jobs[0]["attempt_count"] == 3
    assert jobs[0]["last_error"] == "always_fail"
    assert [a["number"] for a in attempts] == [1, 2, 3]
    assert all(
        a["state"] == "FAILED" and a["failure_cause"] == "workload" for a in attempts
    )


# --- 409 -------------------------------------------------------------------


def _cancelled(engine):
    job_id = make_job(engine, state="CANCELLED")
    return job_id, make_attempt(engine, job_id, state="CANCELLED")


def _stalled(engine):
    job_id = make_job(engine, state="STALLED")
    return job_id, make_attempt(engine, job_id, state="FAILED", cause="infra")


def _succeeded(engine):
    job_id = make_job(engine, state="SUCCEEDED")
    return job_id, make_attempt(engine, job_id, state="SUCCEEDED")


def _failed_requeued(engine):
    job_id = make_job(engine, state="QUEUED")
    return job_id, make_attempt(engine, job_id, state="FAILED", cause="workload")


def _failed_final(engine):
    job_id = make_job(engine, state="FAILED", attempt_count=3, last=1)
    return job_id, make_attempt(engine, job_id, state="FAILED", cause="workload")


def _superseded(engine):
    job_id = make_job(engine, state="RUNNING", attempt_count=2, last=2)
    old = make_attempt(engine, job_id, number=1, state="FAILED", cause="infra")
    make_attempt(engine, job_id, number=2)
    return job_id, old


@pytest.mark.parametrize(
    "setup",
    [_cancelled, _stalled, _succeeded, _failed_requeued, _failed_final, _superseded],
)
@pytest.mark.parametrize("route", ["heartbeat", "result"])
def test_not_current_is_409_and_changes_nothing(client, engine, setup, route):
    job_id, attempt_id = setup(engine)
    q(
        engine,
        "INSERT INTO log_lines (job_id, seq, attempt_number, ts, line)"
        " VALUES (:j, 1, 1, now(), 'old')",
        j=job_id,
    )
    before = snapshot(engine, job_id)
    if route == "heartbeat":
        response = hb(client, attempt_id, [{"ts": TS, "line": "SECRET-LINE"}])
    else:
        response = result(client, attempt_id, fail_body("SECRET-REASON"))
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert str(attempt_id) in error["message"]
    for secret in ("secret-name", "SECRET-LINE", "SECRET-REASON"):
        assert secret not in error["message"]
    assert snapshot(engine, job_id) == before


def test_repeated_result_is_409_and_records_once(client, engine):
    job_id, attempt_id = running(engine)
    assert result(client, attempt_id, fail_body("once")).status_code == 200
    after_first = snapshot(engine, job_id)
    assert result(client, attempt_id, fail_body("twice")).status_code == 409
    assert snapshot(engine, job_id) == after_first
