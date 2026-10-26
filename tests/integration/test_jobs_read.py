"""GET /api/v1/jobs, /jobs/{id} and /jobs/{id}/logs against a real database."""

import base64
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from runwatch.api.app import create_app
from runwatch.lifecycle import AttemptState, FailureCause, JobState
from runwatch.models import Attempt, Job, LogLine
from runwatch.settings import Settings
from sqlalchemy import text
from sqlalchemy.orm import Session

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SPEC = {"duration_s": 5, "fail_probability": 0.0, "mode": "normal"}
JOB_KEYS = {
    "id", "name", "spec", "state", "max_attempts", "attempt_count",
    "created_at", "started_at", "finished_at", "last_error",
}  # fmt: skip
ATTEMPT_KEYS = {
    "id", "number", "worker_id", "state", "lease_expires_at", "started_at",
    "finished_at", "exit_reason", "failure_cause",
}  # fmt: skip


@pytest.fixture(scope="module", autouse=True)
def _schema():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")


@pytest.fixture(scope="module")
def app():
    return create_app(Settings(database_url=DATABASE_URL, sweeper_enabled=False))


@pytest.fixture
def session(app):
    with Session(app.state.engine) as s:
        s.execute(text("TRUNCATE jobs, attempts, log_lines CASCADE"))
        s.commit()
        yield s


@pytest.fixture
def client(app, session):  # session first: tables are emptied before each test
    with TestClient(app) as c:
        yield c


def make_jobs(session, n, *, offsets=None, state=JobState.QUEUED, name="job"):
    """Seed n jobs; offsets[i] is the created_at offset in seconds."""
    jobs = []
    for i in range(n):
        off = offsets[i] if offsets else i
        st = state[i % len(state)] if isinstance(state, list) else state
        jobs.append(
            Job(
                name=f"{name}-{i}",
                spec=SPEC,
                state=st,
                created_at=T0 + timedelta(seconds=off),
            )
        )
    session.add_all(jobs)
    session.commit()
    return jobs


def expected_order(jobs):
    return [
        str(j.id)
        for j in sorted(jobs, key=lambda j: (j.created_at, j.id), reverse=True)
    ]


def walk(client, limit, **params):
    ids, cursor, pages = [], None, []
    while True:
        q = {"limit": limit, **params}
        if cursor:
            q["cursor"] = cursor
        r = client.get("/api/v1/jobs", params=q)
        assert r.status_code == 200
        body = r.json()
        pages.append(body)
        ids += [i["id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return ids, pages


# ---- list ----


def test_empty_list(client):
    r = client.get("/api/v1/jobs")
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}


def test_post_then_list_equal(client):
    posted = client.post(
        "/api/v1/jobs", json={"name": "a", "spec": {"duration_s": 3}}
    ).json()
    body = client.get("/api/v1/jobs").json()
    assert set(body) == {"items", "next_cursor"}
    assert body["items"] == [posted]
    assert posted["state"] == "QUEUED"
    assert set(posted) == JOB_KEYS


def test_order_with_ties(client, session):
    jobs = make_jobs(session, 8, offsets=[0, 1, 1, 1, 2, 2, 3, 3])
    body = client.get("/api/v1/jobs").json()
    assert [i["id"] for i in body["items"]] == expected_order(jobs)
    assert all(set(i) == JOB_KEYS for i in body["items"])


def test_default_limit_and_exact_fit(client, session):
    make_jobs(session, 50)
    body = client.get("/api/v1/jobs").json()
    assert len(body["items"]) == 50
    assert body["next_cursor"] is None
    make_jobs(session, 1, offsets=[-5])
    body = client.get("/api/v1/jobs").json()
    assert len(body["items"]) == 50
    assert body["next_cursor"] is not None


def test_limit_bounds(client, session):
    make_jobs(session, 3)
    assert len(client.get("/api/v1/jobs", params={"limit": 1}).json()["items"]) == 1
    assert client.get("/api/v1/jobs", params={"limit": 100}).status_code == 200
    for bad in ("0", "101", "-1", "abc", ""):
        r = client.get("/api/v1/jobs", params={"limit": bad})
        assert r.status_code == 400, bad
        assert r.json()["error"]["code"] == "validation_error"


def test_paging_125_with_ties(client, session):
    offsets = [0] * 12 + list(range(1, 114))
    jobs = make_jobs(session, 125, offsets=offsets)
    ids, pages = walk(client, 50)
    assert [len(p["items"]) for p in pages] == [50, 50, 25]
    assert pages[-1]["next_cursor"] is None
    assert ids == expected_order(jobs)
    assert len(set(ids)) == 125


def test_limit_one_walk(client, session):
    jobs = make_jobs(session, 7, offsets=[0, 0, 0, 1, 2, 3, 4])
    ids, pages = walk(client, 1)
    assert len(pages) == 7
    assert ids == expected_order(jobs)


def test_inserts_while_paging(client, session):
    jobs = make_jobs(session, 20, offsets=[0] * 5 + list(range(1, 16)))
    page1 = client.get("/api/v1/jobs", params={"limit": 8}).json()
    new = make_jobs(session, 5, offsets=[1000 + i for i in range(5)], name="new")
    cursor = page1["next_cursor"]
    ids = [i["id"] for i in page1["items"]]
    while cursor:
        p = client.get("/api/v1/jobs", params={"limit": 8, "cursor": cursor}).json()
        ids += [i["id"] for i in p["items"]]
        cursor = p["next_cursor"]
    assert not set(ids) & {str(j.id) for j in new}
    assert ids == expected_order(jobs)
    fresh = client.get("/api/v1/jobs", params={"limit": 5}).json()
    assert {i["id"] for i in fresh["items"]} == {str(j.id) for j in new}


def test_state_filter(client, session):
    states = [JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED, JobState.FAILED]
    jobs = make_jobs(session, 24, state=states)
    for st in JobState:
        want = [j for j in jobs if j.state == st]
        r = client.get("/api/v1/jobs", params={"state": st.value})
        assert r.status_code == 200
        assert [i["id"] for i in r.json()["items"]] == expected_order(want)
        assert all(i["state"] == st.value for i in r.json()["items"])
        ids, _ = walk(client, 2, state=st.value)
        assert ids == expected_order(want)


@pytest.mark.parametrize("value", ["queued", "BOGUS", ""])
def test_bad_state(client, value):
    r = client.get("/api/v1/jobs", params={"state": value})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "cursor",
    [
        "abc",
        "",
        _b64(os.urandom(24)),
        _b64(b'{"created_at":"x","id":"y"}'),
        _b64(json.dumps({"created_at": "2026-01-01T00:00:00+00:00", "id": 1}).encode()),
    ],
)
def test_bad_cursor(client, cursor):
    r = client.get("/api/v1/jobs", params={"cursor": cursor})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["message"].startswith("cursor:")


def test_cursor_url_safe_in_raw_url(client, session):
    make_jobs(session, 5)
    cursor = client.get("/api/v1/jobs?limit=2").json()["next_cursor"]
    assert cursor == cursor.strip("=") and "+" not in cursor and "/" not in cursor
    r = client.get(f"/api/v1/jobs?cursor={cursor}&limit=10")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 3


# ---- detail ----


def add_attempt(session, job, number, state, cause=None, **kw):
    a = Attempt(
        job_id=job.id,
        number=number,
        worker_id="w1",
        state=state,
        lease_expires_at=T0 + timedelta(minutes=number),
        started_at=T0,
        failure_cause=cause,
        **kw,
    )
    session.add(a)
    session.commit()
    return a


def test_detail_with_five_attempts(client, session):
    [job] = make_jobs(session, 1)
    causes = [FailureCause.WORKLOAD] * 3 + [FailureCause.INFRA]
    for n in (3, 1, 5, 2, 4):  # insertion order differs from number order
        if n == 5:
            add_attempt(session, job, n, AttemptState.RUNNING)
        else:
            add_attempt(
                session, job, n, AttemptState.FAILED, causes[n - 1],
                finished_at=T0, exit_reason="boom",
            )  # fmt: skip
    r = client.get(f"/api/v1/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == JOB_KEYS | {"attempts"}
    att = body["attempts"]
    assert [a["number"] for a in att] == [1, 2, 3, 4, 5]
    assert all(set(a) == ATTEMPT_KEYS for a in att)
    assert [a["failure_cause"] for a in att] == [
        "workload", "workload", "workload", "infra", None,
    ]  # fmt: skip
    assert att[4]["state"] == "RUNNING"
    assert att[4]["finished_at"] is None and att[4]["exit_reason"] is None
    assert att[0]["lease_expires_at"].endswith(("Z", "+00:00"))
    assert att[0]["exit_reason"] == "boom"
    listed = client.get("/api/v1/jobs").json()["items"][0]
    assert {k: v for k, v in body.items() if k != "attempts"} == listed


def test_detail_no_attempts(client, session):
    [job] = make_jobs(session, 1)
    assert client.get(f"/api/v1/jobs/{job.id}").json()["attempts"] == []


@pytest.mark.parametrize("bad", [str(uuid.uuid4()), "abc", "-1", "x" * 300])
def test_detail_not_found(client, bad):
    for suffix in ("", "/logs"):
        r = client.get(f"/api/v1/jobs/{bad}{suffix}")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


# ---- logs ----


def seed_lines(session, job, seqs, attempt_number=1, text_fn=lambda s: f"line {s}"):
    session.add_all(
        LogLine(
            job_id=job.id,
            seq=s,
            attempt_number=attempt_number,
            ts=T0 + timedelta(seconds=s),
            line=text_fn(s),
        )
        for s in seqs
    )
    session.commit()


def job_with_attempts(session, n_attempts=1, **kw):
    [job] = make_jobs(session, 1, **kw)
    for n in range(1, n_attempts + 1):
        add_attempt(session, job, n, AttemptState.SUCCEEDED, **{})
    return job


def logs(client, job, **params):
    r = client.get(f"/api/v1/jobs/{job.id}/logs", params=params)
    assert r.status_code == 200
    return r.json()


def test_logs_after(client, session):
    job = job_with_attempts(session)
    seed_lines(session, job, range(1, 21))
    body = logs(client, job)
    assert set(body) == {"items", "next_after", "has_more"}
    assert all(set(i) == {"seq", "attempt_number", "ts", "line"} for i in body["items"])
    assert [i["seq"] for i in body["items"]] == list(range(1, 21))
    assert logs(client, job, after=0) == body
    part = logs(client, job, after=7)
    assert [i["seq"] for i in part["items"]] == list(range(8, 21))
    assert part["next_after"] == 20 and part["has_more"] is False


def test_logs_multi_attempt_and_other_jobs(client, session):
    job = job_with_attempts(session, 2)
    other = job_with_attempts(session, 1)
    seed_lines(session, job, [1, 2], 1)
    seed_lines(session, job, [3, 4], 2)
    seed_lines(session, other, [1, 2, 3, 4, 5], 1, lambda s: "other")
    items = logs(client, job)["items"]
    assert [(i["seq"], i["attempt_number"]) for i in items] == [
        (1, 1),
        (2, 1),
        (3, 2),
        (4, 2),
    ]
    assert all(i["line"] != "other" for i in items)


def test_logs_paging_1200(client, session):
    job = job_with_attempts(session)
    seed_lines(session, job, range(1, 1201))
    first = logs(client, job, after=0)
    assert len(first["items"]) == 500
    assert (first["items"][0]["seq"], first["items"][-1]["seq"]) == (1, 500)
    assert first["next_after"] == 500 and first["has_more"] is True
    second = logs(client, job, after=first["next_after"])
    assert (second["items"][0]["seq"], second["items"][-1]["seq"]) == (501, 1000)
    assert second["has_more"] is True
    third = logs(client, job, after=second["next_after"])
    assert (third["items"][0]["seq"], third["items"][-1]["seq"]) == (1001, 1200)
    assert third["has_more"] is False and third["next_after"] == 1200


def test_logs_exactly_500(client, session):
    job = job_with_attempts(session)
    seed_lines(session, job, range(1, 501))
    body = logs(client, job)
    assert len(body["items"]) == 500 and body["has_more"] is False


def test_logs_empty_and_beyond(client, session):
    job = job_with_attempts(session)
    assert logs(client, job) == {"items": [], "next_after": 0, "has_more": False}
    seed_lines(session, job, range(1, 21))
    assert logs(client, job, after=999) == {
        "items": [], "next_after": 999, "has_more": False,
    }  # fmt: skip


@pytest.mark.parametrize("bad", ["-1", "abc", "", str(2**70)])
def test_logs_bad_after(client, session, bad):
    job = job_with_attempts(session)
    r = client.get(f"/api/v1/jobs/{job.id}/logs", params={"after": bad})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def test_logs_verbatim(client, session):
    job = job_with_attempts(session)
    lines = ['say "hi"', "back\\slash", "héllo ☃ 日本", "", "  spaced  "]
    seed_lines(session, job, range(1, 6), text_fn=lambda s: lines[s - 1])
    assert [i["line"] for i in logs(client, job)["items"]] == lines


# ---- general ----


def test_no_name_or_line_in_logs(client, session, caplog):
    [job] = make_jobs(session, 1, name="secret-name-xyz")
    job.name = "secret-name-xyz"
    add_attempt(session, job, 1, AttemptState.SUCCEEDED)
    seed_lines(session, job, [1], text_fn=lambda s: "secret-line-xyz")
    with caplog.at_level(logging.DEBUG):
        assert client.get("/api/v1/jobs").status_code == 200
        assert client.get(f"/api/v1/jobs/{job.id}").status_code == 200
        assert client.get(f"/api/v1/jobs/{job.id}/logs").status_code == 200
    assert "secret-name-xyz" not in caplog.text
    assert "secret-line-xyz" not in caplog.text


def test_read_routes_do_not_write(client, session):
    job = job_with_attempts(session)
    seed_lines(session, job, [1])

    def counts():
        session.rollback()
        return [
            session.scalar(text(f"SELECT count(*) FROM {t}"))
            for t in ("jobs", "attempts", "log_lines")
        ]

    before = counts()
    client.get("/api/v1/jobs")
    client.get(f"/api/v1/jobs/{job.id}")
    client.get(f"/api/v1/jobs/{job.id}/logs")
    assert counts() == before
    from runwatch.api import jobs_read

    assert all(r.methods == {"GET"} for r in jobs_read.router.routes)


def test_openapi_lists_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/api/v1/jobs", "/api/v1/jobs/{id}", "/api/v1/jobs/{id}/logs"):
        assert "get" in paths[p]
