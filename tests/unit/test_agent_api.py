import json
import logging

import pytest
from fastapi.testclient import TestClient
from runwatch.agent.settings import load_agent_settings
from runwatch.settings import SettingsError

from tests.unit.agent_support import (
    AUTH,
    TOKEN,
    GateResponder,
    alert,
    body,
    make_app,
)


def test_healthz_needs_no_auth(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"}],
)
def test_bad_auth_is_401_and_creates_nothing(tmp_path, headers):
    client = TestClient(make_app(tmp_path))
    response = client.post("/webhook/alertmanager", json=body(alert()), headers=headers)
    assert response.status_code == 401
    assert not (tmp_path / "incidents").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"status": "firing"}',
        b'{"alerts": "x"}',
        json.dumps(body({"status": "firing", "labels": {}})).encode(),
        json.dumps(body(alert(fingerprint="../x"))).encode(),
        json.dumps(body(alert(fingerprint=""))).encode(),
        json.dumps(body(alert(fingerprint="a" * 65))).encode(),
        json.dumps(body(alert(status="weird"))).encode(),
        json.dumps(body(alert(), "nope")).encode(),
    ],
)
def test_invalid_body_is_422_and_creates_nothing(tmp_path, content):
    client = TestClient(make_app(tmp_path))
    response = client.post("/webhook/alertmanager", content=content, headers=AUTH)
    assert response.status_code == 422
    assert not (tmp_path / "incidents").exists()


def test_invalid_alert_among_valid_creates_nothing(tmp_path):
    client = TestClient(make_app(tmp_path))
    payload = body(alert("good"), alert("../x"))
    response = client.post("/webhook/alertmanager", json=payload, headers=AUTH)
    assert response.status_code == 422
    assert not (tmp_path / "incidents").exists()


def test_valid_request_is_202_with_one_entry_per_alert(tmp_path):
    responder = GateResponder()
    client = TestClient(make_app(tmp_path, responder=responder))
    response = client.post(
        "/webhook/alertmanager", json=body(alert("a"), alert("b")), headers=AUTH
    )
    responder.release()
    assert response.status_code == 202
    entries = response.json()
    assert [e["fingerprint"] for e in entries] == ["a", "b"]
    assert all(e["action"] == "started" for e in entries)
    assert entries[0]["incident_id"].endswith("-a")


def test_settings_require_a_token(monkeypatch):
    monkeypatch.delenv("AGENT_WEBHOOK_TOKEN", raising=False)
    with pytest.raises(SettingsError) as info:
        load_agent_settings()
    assert "AGENT_WEBHOOK_TOKEN" in str(info.value)


def test_settings_reject_an_empty_token(monkeypatch):
    monkeypatch.setenv("AGENT_WEBHOOK_TOKEN", "")
    with pytest.raises(SettingsError) as info:
        load_agent_settings()
    assert "AGENT_WEBHOOK_TOKEN" in str(info.value)


def test_settings_need_no_database_and_have_defaults(monkeypatch):
    monkeypatch.setenv("AGENT_WEBHOOK_TOKEN", TOKEN)
    for name in (
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "AGENT_COOLDOWN_S",
        "AGENT_MODEL",
        "AGENT_INCIDENTS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_agent_settings()
    assert settings.agent_cooldown_s == 600
    assert settings.agent_model == "claude-opus-5-5"
    assert settings.agent_incidents_dir == "incidents"
    assert not settings.has_api_key
    assert TOKEN not in repr(settings)


def test_settings_error_does_not_show_values(monkeypatch):
    monkeypatch.setenv("AGENT_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_COOLDOWN_S", "-5")
    with pytest.raises(SettingsError) as info:
        load_agent_settings()
    assert "AGENT_COOLDOWN_S" in str(info.value)
    assert TOKEN not in str(info.value)


def test_startup_warns_once_without_api_key(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    with TestClient(make_app(tmp_path)):
        pass
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ANTHROPIC_API_KEY" in warnings[0].getMessage()


def test_no_startup_warning_with_api_key(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    with TestClient(make_app(tmp_path, anthropic_api_key="k-" + "unit")):
        pass
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
