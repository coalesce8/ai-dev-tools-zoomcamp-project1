"""Validation of the heartbeat and result routes. No database is reached."""

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from runwatch.api.app import create_app
from runwatch.api.worker_report import HeartbeatIn, ResultIn
from runwatch.settings import Settings

UNREACHABLE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:1/x"
TS = "2026-01-01T00:00:00+00:00"
ATTEMPT = uuid.uuid4()
HB = f"/internal/v1/worker/attempts/{ATTEMPT}/heartbeat"
RES = f"/internal/v1/worker/attempts/{ATTEMPT}/result"


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(Settings(database_url=UNREACHABLE_URL)))


def ok(line="hello"):
    return {"ts": TS, "line": line}


HEARTBEAT_BAD = {
    "too_many_lines": {"lines": [ok()] * 101},
    "line_too_long": {"lines": [ok("x" * 4097)]},
    "ts_missing": {"lines": [{"line": "a"}]},
    "ts_not_datetime": {"lines": [{"ts": "nope", "line": "a"}]},
    "ts_number": {"lines": [{"ts": 1700000000, "line": "a"}]},
    "ts_naive": {"lines": [{"ts": "2026-01-01T00:00:00", "line": "a"}]},
    "line_missing": {"lines": [{"ts": TS}]},
    "line_not_string": {"lines": [{"ts": TS, "line": 5}]},
    "line_nul": {"lines": [ok("a\x00b")]},
    "unknown_top": {"lines": [], "extra": 1},
    "unknown_line_field": {"lines": [{"ts": TS, "line": "a", "level": "x"}]},
    "lines_not_list": {"lines": "abc"},
}


@pytest.mark.parametrize("body", HEARTBEAT_BAD.values(), ids=HEARTBEAT_BAD.keys())
def test_heartbeat_validation(client, body):
    response = client.post(HB, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


RESULT_BAD = {
    "missing_outcome": {},
    "bad_outcome": {"outcome": "done"},
    "failed_no_reason": {"outcome": "failed", "cause": "workload"},
    "failed_no_cause": {"outcome": "failed", "reason": "x"},
    "bad_cause": {"outcome": "failed", "reason": "x", "cause": "other"},
    "empty_reason": {"outcome": "failed", "reason": "", "cause": "infra"},
    "blank_reason": {"outcome": "failed", "reason": " \n\t", "cause": "infra"},
    "nul_reason": {"outcome": "failed", "reason": "a\x00", "cause": "infra"},
    "succeeded_reason": {"outcome": "succeeded", "reason": "x"},
    "succeeded_cause": {"outcome": "succeeded", "cause": "infra"},
    "unknown_field": {"outcome": "succeeded", "lines": []},
    "failed_unknown_field": {
        "outcome": "failed",
        "reason": "x",
        "cause": "infra",
        "x": 1,
    },
}


@pytest.mark.parametrize("body", RESULT_BAD.values(), ids=RESULT_BAD.keys())
def test_result_validation(client, body):
    response = client.post(RES, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("suffix", ["heartbeat", "result"])
def test_attempt_id_must_be_uuid(client, suffix):
    response = client.post(
        f"/internal/v1/worker/attempts/not-a-uuid/{suffix}",
        json={"outcome": "succeeded"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_validation_message_does_not_echo_values(client):
    response = client.post(RES, json={"outcome": "failed", "reason": "", "cause": "x"})
    assert "secret" not in response.text
    response = client.post(HB, json={"lines": [ok("secret-line\x00")]})
    assert "secret-line" not in response.text


def test_public_prefix_has_no_worker_routes(client):
    response = client.post(f"/api/v1/worker/attempts/{ATTEMPT}/heartbeat", json={})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_valid_bodies_parse():
    assert HeartbeatIn.model_validate({}).lines == []
    parsed = HeartbeatIn.model_validate({"lines": [ok("")] * 100})
    assert len(parsed.lines) == 100
    assert HeartbeatIn.model_validate({"lines": [ok("x" * 4096)]})
    adapter = TypeAdapter(ResultIn)
    assert adapter.validate_python({"outcome": "succeeded"})
    long = adapter.validate_python(
        {"outcome": "failed", "reason": "r" * 1500, "cause": "workload"}
    )
    assert long.reason == "r" * 1000


def test_naive_ts_rejected_by_model():
    with pytest.raises(ValidationError):
        HeartbeatIn.model_validate(
            {"lines": [{"ts": "2026-01-01T00:00:00", "line": "a"}]}
        )
