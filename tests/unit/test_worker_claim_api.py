"""Claim request validation and routing. No database needed: every request here
is rejected before the route touches a session."""

import pytest
from fastapi.testclient import TestClient
from runwatch.api.app import create_app
from runwatch.settings import Settings

URL = "postgresql+psycopg://dbuser:hunter2pw@127.0.0.1:1/x"
CLAIM = "/internal/v1/worker/claim"


@pytest.fixture
def client():
    with TestClient(create_app(Settings(database_url=URL))) as client:
        yield client


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"json": {}},
        {"json": {"worker_id": ""}},
        {"json": {"worker_id": "x" * 256}},
        {"json": {"worker_id": 5}},
        {"json": {"worker_id": None}},
        {"json": {"worker_id": ["a"]}},
        {"content": "not json", "headers": {"content-type": "application/json"}},
    ],
)
def test_invalid_body_is_400(client, kwargs):
    response = client.post(CLAIM, **kwargs)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_get_is_405(client):
    response = client.get(CLAIM)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_not_under_public_api(client):
    assert (
        client.post("/api/v1/worker/claim", json={"worker_id": "w"}).status_code == 404
    )
