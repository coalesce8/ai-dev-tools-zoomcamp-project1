"""Health, readiness (database down) and settings tests. No database needed."""

import logging
import time

import pytest
from fastapi.testclient import TestClient
from runwatch.api import internal, public
from runwatch.api.app import create_app
from runwatch.settings import Settings, SettingsError

UNREACHABLE_URL = "postgresql+psycopg://dbuser:hunter2pw@127.0.0.1:1/x"


@pytest.fixture
def down_app():
    return create_app(Settings(database_url=UNREACHABLE_URL))


def test_healthz_ok_with_database_down(down_app):
    with TestClient(down_app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_not_ready_when_database_down(down_app, caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(down_app) as client:
        started = time.monotonic()
        response = client.get("/readyz")
        elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "not_ready"
    assert isinstance(body["error"]["message"], str)
    assert elapsed < 2 + 1
    for secret in ("hunter2pw", "dbuser", "127.0.0.1", UNREACHABLE_URL, ":1/x"):
        assert secret not in response.text
        assert secret not in caplog.text
    assert "Readiness check failed" in caplog.text


def test_readyz_unroutable_host_within_timeout():
    # 10.255.255.1 drops packets, so this exercises connect_timeout.
    url = "postgresql+psycopg://u:p@10.255.255.1:5432/x"
    app = create_app(Settings(database_url=url, db_connect_timeout_s=2))
    with TestClient(app) as client:
        started = time.monotonic()
        response = client.get("/readyz")
        elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_ready"
    assert elapsed < 2 + 1


def test_health_routes_are_at_root(down_app):
    with TestClient(down_app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        for prefix in ("/api/v1", "/internal/v1"):
            assert client.get(f"{prefix}/healthz").status_code == 404
            assert client.get(f"{prefix}/readyz").status_code == 404


def test_routers_are_empty_and_prefixed(down_app):
    assert public.router.prefix == "/api/v1"
    assert internal.router.prefix == "/internal/v1"
    assert public.router.routes == []
    assert internal.router.routes == []


def test_create_app_without_database_url_names_it(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SettingsError, match="DATABASE_URL"):
        create_app()


def test_create_app_reads_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_URL)
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_S", "5")
    app = create_app()
    assert app.state.settings.database_url.get_secret_value() == UNREACHABLE_URL
    assert app.state.settings.db_connect_timeout_s == 5


def test_settings_defaults_and_repr_hide_url():
    settings = Settings(database_url=UNREACHABLE_URL)
    assert settings.db_connect_timeout_s == 2
    assert "hunter2pw" not in repr(settings)


def test_one_engine_per_app_disposed_on_shutdown(down_app, monkeypatch):
    engine = down_app.state.engine
    disposed = []
    monkeypatch.setattr(engine, "dispose", lambda: disposed.append(True))
    with TestClient(down_app) as client:
        client.get("/healthz")
        client.get("/readyz")
        assert down_app.state.engine is engine
        assert disposed == []
    assert disposed == [True]


def test_env_example_lists_settings():
    from pathlib import Path

    text = (Path(__file__).parents[2] / ".env.example").read_text()
    lines = text.splitlines()
    for name in Settings.model_fields:
        env = name.upper()
        index = next(i for i, line in enumerate(lines) if line.startswith(f"{env}="))
        assert lines[index - 1].startswith("#"), f"{env} has no comment"
    url_line = next(line for line in lines if line.startswith("DATABASE_URL="))
    assert url_line.startswith("DATABASE_URL=postgresql+psycopg://")
    assert "@localhost:5432/" in url_line
