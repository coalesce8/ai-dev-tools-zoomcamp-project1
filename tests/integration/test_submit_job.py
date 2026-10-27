"""POST /api/v1/jobs against a real database."""

import logging
import os
import re
import uuid

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from runwatch.api.app import create_app
from runwatch.models import Attempt, Job
from runwatch.settings import Settings
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VALID = {"name": "nightly-sim", "spec": {"duration_s": 30, "fail_probability": 0.0}}
KEYS = {
    "id", "name", "spec", "state", "max_attempts", "attempt_count",
    "created_at", "started_at", "finished_at", "last_error",
}  # fmt: skip


@pytest.fixture(scope="module", autouse=True)
def _schema():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")


@pytest.fixture(scope="module")
def app():
    return create_app(Settings(database_url=DATABASE_URL))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def session(app):
    with Session(app.state.engine) as s:
        yield s


def _count(session):
    session.rollback()
    return session.scalar(select(func.count()).select_from(Job))


def test_valid_submit(client, session):
    before = _count(session)
    r = client.post("/api/v1/jobs", json=VALID)
    assert r.status_code == 201
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert set(body) == KEYS
    assert r.headers["location"] == f"/api/v1/jobs/{body['id']}"
    uuid.UUID(body["id"])
    assert body["spec"] == {"duration_s": 30, "fail_probability": 0.0, "mode": "normal"}
    assert body["state"] == "QUEUED"
    assert body["max_attempts"] == 3
    assert body["attempt_count"] == 0
    assert re.search(r"(Z|\+00:00)$", body["created_at"])
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert body["last_error"] is None

    assert _count(session) == before + 1
    job = session.get(Job, uuid.UUID(body["id"]))
    assert job.state == "QUEUED"
    assert job.attempt_count == 0
    assert job.last_attempt_number == 0
    assert job.queued_at is not None and job.created_at is not None
    assert set(job.trace_context) == {"traceparent"}  # stored by #21
    assert job.spec == body["spec"]
    assert session.scalar(select(func.count()).where(Attempt.job_id == job.id)) == 0


@pytest.mark.parametrize("m", [1, 10])
def test_explicit_max_attempts(client, session, m):
    r = client.post("/api/v1/jobs", json={**VALID, "max_attempts": m})
    assert r.status_code == 201
    assert r.json()["max_attempts"] == m
    assert session.get(Job, uuid.UUID(r.json()["id"])).max_attempts == m


@pytest.mark.parametrize("d", [1, 600])
@pytest.mark.parametrize("p", [0, 0.0, 1, 1.0, 0.5])
def test_boundaries(client, d, p):
    r = client.post(
        "/api/v1/jobs",
        json={
            "name": "x",
            "spec": {"duration_s": d, "fail_probability": p, "mode": "always_fail"},
        },
    )
    assert r.status_code == 201
    assert r.json()["spec"] == {
        "duration_s": d,
        "fail_probability": float(p),
        "mode": "always_fail",
    }
    assert isinstance(r.json()["spec"]["fail_probability"], float)


@pytest.mark.parametrize("name", ["a" * 100, "a", " a b ", "Modèle-🌊", "🌊" * 100])
def test_name_edge_cases(client, session, name):
    r = client.post("/api/v1/jobs", json={"name": name, "spec": {"duration_s": 1}})
    assert r.status_code == 201
    assert r.json()["name"] == name
    assert session.get(Job, uuid.UUID(r.json()["id"])).name == name


def test_duplicate_submit(client):
    a = client.post("/api/v1/jobs", json=VALID).json()
    b = client.post("/api/v1/jobs", json=VALID).json()
    assert a["id"] != b["id"]


def _spec(**kw):
    return {"name": "n", "spec": {"duration_s": 5, **kw}}


INVALID_JSON = [
    ({"spec": {"duration_s": 5}}, "body.name"),
    ({"name": "n"}, "body.spec"),
]
INVALID = (
    [{"name": v, "spec": {"duration_s": 5}} for v in
     ["", "   ", "a" * 101, 123, None, "a\nb", "a\tb", "a\x00b", "a​b"]]
    + [{"name": "n", "spec": v} for v in [None, "x", []]]
    + [_spec(duration_s=v) for v in [0, 601, -1, 30.5, 30.0, "30", True, None]]
    + [_spec(fail_probability=v) for v in [-0.01, 1.01, "0.5", True, None]]
    + [_spec(mode=v) for v in ["bogus", "NORMAL", "", None]]
    + [{**_spec(), "max_attempts": v} for v in [0, 11, 2.5, "3", True, None]]
    + [{**_spec(), "state": "SUCCEEDED"}, {**_spec(), "id": str(uuid.uuid4())},
       {**_spec(), "attempt_count": 5}, _spec(fail_probabilty=0.5),
       {"name": "n", "spec": {}}]
)  # fmt: skip


def _check_400(r, session, before):
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["message"].startswith("body")
    assert _count(session) == before


@pytest.mark.parametrize("body", INVALID, ids=repr)
def test_invalid(client, session, body):
    before = _count(session)
    _check_400(client.post("/api/v1/jobs", json=body), session, before)


@pytest.mark.parametrize("body, loc", INVALID_JSON, ids=repr)
def test_missing_fields(client, session, body, loc):
    before = _count(session)
    r = client.post("/api/v1/jobs", json=body)
    _check_400(r, session, before)
    assert r.json()["error"]["message"].startswith(loc)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_literals(client, session, literal):
    before = _count(session)
    raw = f'{{"name": "n", "spec": {{"duration_s": 5, "fail_probability": {literal}}}}}'
    r = client.post(
        "/api/v1/jobs", content=raw, headers={"Content-Type": "application/json"}
    )
    _check_400(r, session, before)


@pytest.mark.parametrize(
    "content, ctype",
    [
        ('{"name":', "application/json"),
        ("", "application/json"),
        ("[]", "application/json"),
        ('"x"', "application/json"),
        ('{"name": "n", "spec": {"duration_s": 5}}', "text/plain"),
    ],
)
def test_malformed_body(client, session, content, ctype):
    before = _count(session)
    r = client.post("/api/v1/jobs", content=content, headers={"Content-Type": ctype})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"
    assert _count(session) == before


def test_first_error_only(client):
    r = client.post("/api/v1/jobs", json={"name": "", "spec": {"duration_s": 0}})
    assert r.status_code == 400
    assert list(r.json()) == ["error"]
    assert isinstance(r.json()["error"]["message"], str)


def test_400_does_not_echo_value(client):
    marker = "zq" + uuid.uuid4().hex + "x" * 80
    r = client.post("/api/v1/jobs", json={"name": marker, "spec": {"duration_s": 5}})
    assert r.status_code == 400
    assert marker not in r.text


def test_logs_do_not_contain_name_or_spec(client, caplog):
    marker = "marker-" + uuid.uuid4().hex
    with caplog.at_level(logging.DEBUG):
        r = client.post(
            "/api/v1/jobs", json={"name": marker, "spec": {"duration_s": 7}}
        )
    assert r.status_code == 201
    for record in caplog.records:
        assert marker not in record.getMessage()
        assert marker not in str(record.__dict__)
