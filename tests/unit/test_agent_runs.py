import json
import threading

from fastapi.testclient import TestClient
from runwatch.agent.app import create_app

from tests.unit.agent_support import (
    AUTH,
    FakeClock,
    GateResponder,
    RaisingResponder,
    alert,
    body,
    make_app,
    make_settings,
)


def post(client, *alerts):
    response = client.post("/webhook/alertmanager", json=body(*alerts), headers=AUTH)
    assert response.status_code == 202
    return response.json()


def actions(client, *alerts):
    return [e["action"] for e in post(client, *alerts)]


def incident_dirs(tmp_path):
    return sorted(p for p in (tmp_path / "incidents").iterdir() if p.is_dir())


def status_of(tmp_path, incident_id):
    return json.loads(
        (tmp_path / "incidents" / incident_id / "status.json").read_text()
    )


def test_started_creates_the_directory_layout(tmp_path):
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder)
    client = TestClient(app)
    first = alert("fp1")
    entry = post(client, first)[0]
    assert entry["incident_id"] == "20260102T030405Z-fp1"
    directory = tmp_path / "incidents" / entry["incident_id"]
    assert json.loads((directory / "alert.json").read_text()) == first
    context = (directory / "context.md").read_text()
    assert "HighFailureRate" in context and "demo" in context
    assert "2026-01-02T03:04:05" in context
    assert (directory / "response.md").read_text() == ""
    status = status_of(tmp_path, entry["incident_id"])
    assert status["state"] == "running" and status["outcome"] is None
    assert status["ended_at"] is None and status["webhook_count"] == 1
    assert status["alertname"] == "HighFailureRate" and status["env"] == "demo"
    assert status["fingerprint"] == "fp1"
    lines = (directory / "webhooks.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["disposition"] == "started" and record["kind"] == "firing"
    assert record["startsAt"] == first["startsAt"] and "received_at" in record
    responder.release()
    app.state.runs.wait_idle()


def test_request_returns_before_the_run_finishes(tmp_path):
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder)
    client = TestClient(app)
    entry = post(client, alert())[0]
    assert status_of(tmp_path, entry["incident_id"])["state"] == "running"
    responder.release()
    app.state.runs.wait_idle()
    status = status_of(tmp_path, entry["incident_id"])
    assert status["state"] == "finished" and status["outcome"] == "fixed"
    assert status["ended_at"] is not None


def test_repeat_while_running_is_recorded_not_restarted(tmp_path):
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder)
    client = TestClient(app)
    first = post(client, alert())[0]
    second_alert = alert(startsAt="2026-01-02T03:10:00Z", labels={"alertname": "X"})
    second = post(client, second_alert)[0]
    assert second["action"] == "recorded_in_progress"
    assert second["incident_id"] == first["incident_id"]
    directory = tmp_path / "incidents" / first["incident_id"]
    assert len(incident_dirs(tmp_path)) == 1
    assert json.loads((directory / "alert.json").read_text()) == alert()
    lines = (directory / "webhooks.jsonl").read_text().splitlines()
    assert [json.loads(x)["disposition"] for x in lines] == [
        "started",
        "recorded_in_progress",
    ]
    assert status_of(tmp_path, first["incident_id"])["webhook_count"] == 2
    assert len(responder.seen) == 1
    responder.release()
    app.state.runs.wait_idle()


def test_cooldown_boundary_599_and_600_seconds(tmp_path):
    clock = FakeClock()
    app = make_app(tmp_path, clock=clock)  # no key: ends at once
    client = TestClient(app)
    first = post(client, alert())[0]
    app.state.runs.wait_idle()
    assert status_of(tmp_path, first["incident_id"])["outcome"] == "skipped_no_model"
    clock.advance(599)
    again = post(client, alert())[0]
    assert again == {
        "fingerprint": "fp1",
        "incident_id": first["incident_id"],
        "action": "recorded_cooldown",
    }
    clock.advance(1)
    third = post(client, alert())[0]
    assert third["action"] == "started"
    assert third["incident_id"] != first["incident_id"]
    assert len(incident_dirs(tmp_path)) == 2
    app.state.runs.wait_idle()


def test_cooldown_counts_from_the_end_of_the_run(tmp_path):
    clock = FakeClock()
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder, clock=clock)
    client = TestClient(app)
    post(client, alert())
    clock.advance(1000)  # a long run
    responder.release()
    app.state.runs.wait_idle()
    clock.advance(599)
    assert actions(client, alert()) == ["recorded_cooldown"]
    clock.advance(1)
    assert actions(client, alert()) == ["started"]
    app.state.runs.wait_idle()


def test_resolved_is_recorded_without_ending_anything(tmp_path):
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder)
    client = TestClient(app)
    first = post(client, alert())[0]
    resolved = post(client, alert(status="resolved"))[0]
    assert resolved["action"] == "recorded_resolved"
    assert resolved["incident_id"] == first["incident_id"]
    status = status_of(tmp_path, first["incident_id"])
    assert status["state"] == "running" and status["webhook_count"] == 2
    lines = tmp_path / "incidents" / first["incident_id"] / "webhooks.jsonl"
    last = json.loads(lines.read_text().splitlines()[-1])
    assert last["kind"] == "resolved" and last["disposition"] == "recorded_resolved"
    responder.release()
    app.state.runs.wait_idle()
    # In cooldown it is recorded too, and does not restart the run.
    assert actions(client, alert(status="resolved")) == ["recorded_resolved"]
    assert len(incident_dirs(tmp_path)) == 1


def test_resolved_without_incident_is_ignored(tmp_path):
    client = TestClient(make_app(tmp_path))
    entry = post(client, alert(status="resolved"))[0]
    assert entry["action"] == "ignored_resolved" and entry["incident_id"] is None
    assert not (tmp_path / "incidents").exists()


def test_resolved_after_cooldown_is_ignored(tmp_path):
    clock = FakeClock()
    app = make_app(tmp_path, clock=clock)
    client = TestClient(app)
    post(client, alert())
    app.state.runs.wait_idle()
    clock.advance(600)
    assert actions(client, alert(status="resolved")) == ["ignored_resolved"]


def test_fingerprints_are_independent(tmp_path):
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder)
    client = TestClient(app)
    assert actions(client, alert("a")) == ["started"]
    assert actions(client, alert("b")) == ["started"]
    assert actions(client, alert("a"), alert("b")) == [
        "recorded_in_progress",
        "recorded_in_progress",
    ]
    assert len(incident_dirs(tmp_path)) == 2
    responder.release()
    app.state.runs.wait_idle()


def test_same_fingerprint_twice_in_one_body(tmp_path):
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder)
    client = TestClient(app)
    assert actions(client, alert(), alert()) == ["started", "recorded_in_progress"]
    responder.release()
    app.state.runs.wait_idle()


def test_twenty_simultaneous_requests_create_one_incident(tmp_path):
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder)
    barrier = threading.Barrier(20)
    results: list[str] = []
    errors: list[BaseException] = []

    def fire():
        try:
            client = TestClient(app)
            barrier.wait(10)
            results.extend(actions(client, alert()))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=fire) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert sorted(results) == ["recorded_in_progress"] * 19 + ["started"]
    assert len(incident_dirs(tmp_path)) == 1
    only = incident_dirs(tmp_path)[0].name
    assert status_of(tmp_path, only)["webhook_count"] == 20
    responder.release()
    app.state.runs.wait_idle()


def test_no_key_runs_end_skipped_and_cooldown_applies(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    entry = post(client, alert())[0]
    app.state.runs.wait_idle()
    status = status_of(tmp_path, entry["incident_id"])
    assert status["state"] == "finished" and status["outcome"] == "skipped_no_model"
    assert actions(client, alert()) == ["recorded_cooldown"]


def test_key_set_builds_the_real_responder_not_a_placeholder(tmp_path, monkeypatch):
    # The responder given by the key is the loop; its model client is faked
    # here so nothing reaches the network.
    from runwatch.agent import app as app_module
    from runwatch.agent.loop import ClaudeResponder

    seen = {}

    def fake_build(settings):
        seen["key"] = settings.anthropic_api_key.get_secret_value()
        return GateResponder("escalated")

    monkeypatch.setattr(app_module, "build_responder", fake_build)
    app = make_app(tmp_path, anthropic_api_key="k-" + "unit")
    responder = app.state.runs.responder
    assert seen["key"] == "k-unit" and isinstance(responder, GateResponder)
    assert not hasattr(app_module, "PlaceholderResponder")
    assert ClaudeResponder  # importable
    responder.release()
    client = TestClient(app)
    entry = post(client, alert())[0]
    app.state.runs.wait_idle()
    assert status_of(tmp_path, entry["incident_id"])["outcome"] == "escalated"


def test_responder_exception_escalates_and_service_keeps_serving(tmp_path):
    app = make_app(tmp_path, responder=RaisingResponder())
    client = TestClient(app)
    entry = post(client, alert())[0]
    app.state.runs.wait_idle()
    status = status_of(tmp_path, entry["incident_id"])
    assert status["state"] == "finished" and status["outcome"] == "escalated"
    assert actions(client, alert()) == ["recorded_cooldown"]
    assert actions(client, alert("other")) == ["started"]
    app.state.runs.wait_idle()
    assert client.get("/healthz").status_code == 200


def write_status(tmp_path, name, **fields):
    directory = tmp_path / "incidents" / name
    directory.mkdir(parents=True)
    (directory / "context.md").write_text("# header\n")
    status = {
        "incident_id": name,
        "fingerprint": "fp1",
        "alertname": "A",
        "env": "demo",
        "state": "finished",
        "outcome": "fixed",
        "started_at": "2026-01-02T02:00:00+00:00",
        "ended_at": "2026-01-02T03:00:00+00:00",
        "webhook_count": 1,
    }
    status.update(fields)
    (directory / "status.json").write_text(json.dumps(status))
    return directory


def test_restart_closes_a_running_incident_as_escalated(tmp_path):
    name = "20260102T020000Z-fp1"
    directory = write_status(
        tmp_path, name, state="running", outcome=None, ended_at=None
    )
    clock = FakeClock()
    app = make_app(tmp_path, clock=clock)
    with TestClient(app) as client:
        status = status_of(tmp_path, name)
        assert status["state"] == "finished" and status["outcome"] == "escalated"
        assert status["ended_at"].startswith("2026-01-02T03:04:05")
        assert "stopped" in (directory / "context.md").read_text()
        assert actions(client, alert()) == ["recorded_cooldown"]
        clock.advance(600)
        assert actions(client, alert()) == ["started"]
        app.state.runs.wait_idle()


def test_restart_uses_the_stored_ended_at_for_cooldown(tmp_path):
    name = "20260102T020000Z-fp1"
    write_status(tmp_path, name)  # ended 03:00:00
    clock = FakeClock()  # 03:04:05, 245 s later
    app = make_app(tmp_path, clock=clock)
    with TestClient(app) as client:
        assert actions(client, alert()) == ["recorded_cooldown"]
        clock.advance(355)  # 600 s after the stored end
        assert actions(client, alert()) == ["started"]
        app.state.runs.wait_idle()


def test_restart_skips_corrupt_or_missing_status(tmp_path, caplog):
    root = tmp_path / "incidents"
    (root / "20260102T020000Z-bad").mkdir(parents=True)
    (root / "20260102T020000Z-bad" / "status.json").write_text("{not json")
    (root / "20260102T020000Z-none").mkdir()
    (root / "stray.txt").write_text("x")
    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/healthz").status_code == 200
        assert actions(client, alert("bad")) == ["started"]
    assert len([r for r in caplog.records if "skipping" in r.getMessage()]) == 2


def test_default_app_builds_without_database_url(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_WEBHOOK_TOKEN", "t-" + "env")
    monkeypatch.setenv("AGENT_INCIDENTS_DIR", str(tmp_path / "i"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert TestClient(create_app()).get("/healthz").status_code == 200
    assert make_settings(tmp_path)  # helper stays importable
