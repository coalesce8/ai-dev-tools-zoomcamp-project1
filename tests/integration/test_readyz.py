"""Readiness against a real database, taken from DATABASE_URL."""

import os

import pytest
from fastapi.testclient import TestClient
from runwatch.api.app import create_app
from runwatch.settings import Settings

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)


def test_readyz_ready_with_reachable_database():
    app = create_app(Settings(database_url=DATABASE_URL))
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_recovers_without_restart():
    app = create_app(Settings(database_url=DATABASE_URL))
    good_engine = app.state.engine
    down = create_app(
        Settings(database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:1/x")
    )
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        # Simulate an outage by pointing the app at an unreachable database,
        # then bring the real one back.
        app.state.engine = down.state.engine
        assert client.get("/readyz").status_code == 503
        app.state.engine = good_engine
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
