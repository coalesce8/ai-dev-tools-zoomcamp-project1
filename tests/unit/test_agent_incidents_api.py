import json
import logging
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from runwatch.agent.reports import REVERT_NOTE

from tests.unit.agent_support import (
    AUTH,
    FakeClock,
    GateResponder,
    RaisingResponder,
    alert,
    body,
    make_app,
)

FIXTURES = Path(__file__).parent / "fixtures"


def post(client, *alerts):
    response = client.post("/webhook/alertmanager", json=body(*alerts), headers=AUTH)
    assert response.status_code == 202
    return response.json()


def incident_file(tmp_path, incident_id, name):
    return tmp_path / "incidents" / incident_id / name


def test_report_written_when_a_run_ends_without_a_model(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    iid = post(client, alert())[0]["incident_id"]
    app.state.runs.wait_idle()
    text = incident_file(tmp_path, iid, "response.md").read_text()
    assert text.startswith(f"# Incident {iid}\n")
    assert "skipped_no_model" in text and "ANTHROPIC_API_KEY" in text
    for section in ("## Evidence", "## Actions taken", "## Repeated webhooks"):
        assert section in text
    assert client.get(f"/incidents/{iid}").text == text


def test_report_written_after_a_responder_exception(tmp_path):
    app = make_app(tmp_path, responder=RaisingResponder())
    client = TestClient(app)
    iid = post(client, alert())[0]["incident_id"]
    app.state.runs.wait_idle()
    text = incident_file(tmp_path, iid, "response.md").read_text()
    assert "- outcome: escalated" in text
    assert "MARKER-IN-EXC" not in text


def test_report_secrets_are_redacted(tmp_path):
    secret = "gh" + "p_" + "B" * 24
    app = make_app(
        tmp_path,
        github_token=secret,
        anthropic_api_key="key-" + "plain-secret",
    )
    client = TestClient(app)
    iid = post(
        client, alert(annotations={"summary": f"x key-plain-secret {secret} tok-unit"})
    )[0]["incident_id"]
    app.state.runs.wait_idle()
    text = incident_file(tmp_path, iid, "response.md").read_text()
    assert "key-plain-secret" not in text and secret not in text
    assert "tok-unit" not in text and "[redacted]" in text


def test_running_incident_is_rendered_on_demand(tmp_path):
    responder = GateResponder()
    app = make_app(tmp_path, responder=responder)
    client = TestClient(app)
    iid = post(client, alert())[0]["incident_id"]
    assert incident_file(tmp_path, iid, "response.md").read_text() == ""
    response = client.get(f"/incidents/{iid}")
    assert response.status_code == 200
    assert "Partial report" in response.text and "In progress" in response.text
    responder.release()
    app.state.runs.wait_idle()
    assert "Partial report" not in client.get(f"/incidents/{iid}").text


def test_repeated_webhook_regenerates_a_finished_report(tmp_path):
    clock = FakeClock()
    app = make_app(tmp_path, clock=clock)
    client = TestClient(app)
    iid = post(client, alert())[0]["incident_id"]
    app.state.runs.wait_idle()
    assert "- dispositions: none" in client.get(f"/incidents/{iid}").text
    clock.advance(10)
    assert post(client, alert())[0]["action"] == "recorded_cooldown"
    text = client.get(f"/incidents/{iid}").text
    assert "- webhook_count: 2" in text
    assert "- dispositions: 1 recorded_cooldown" in text
    assert "webhook firing: recorded_cooldown" in text
    clock.advance(10)
    post(client, alert(status="resolved"))
    assert (
        "1 recorded_cooldown, 1 recorded_resolved"
        in client.get(f"/incidents/{iid}").text
    )


def test_recovery_writes_the_report(tmp_path):
    responder = GateResponder()
    first = make_app(tmp_path, responder=responder)
    with TestClient(first) as client:
        iid = post(client, alert())[0]["incident_id"]
        responder.release()
        first.state.runs.wait_idle()
    # Simulate a crash: put the incident back to running with an empty report.
    status_path = incident_file(tmp_path, iid, "status.json")
    status = json.loads(status_path.read_text())
    status.update(state="running", outcome=None, ended_at=None)
    status_path.write_text(json.dumps(status))
    incident_file(tmp_path, iid, "response.md").write_text("")
    with TestClient(make_app(tmp_path)) as client:
        text = client.get(f"/incidents/{iid}").text
    assert "interrupted by an agent restart" in text
    assert "- outcome: escalated" in text and "Partial report" not in text


def test_render_crash_writes_a_fallback_and_logs_no_report(tmp_path, caplog):
    app = make_app(tmp_path)
    client = TestClient(app)
    controller = app.state.runs
    iid = post(client, alert())[0]["incident_id"]
    controller.wait_idle()
    incident_file(tmp_path, iid, "status.json").write_text("{")
    with caplog.at_level(logging.ERROR):
        controller._write_report(iid)
    assert (
        "report generation failed"
        in incident_file(tmp_path, iid, "response.md").read_text()
    )
    record = next(r for r in caplog.records if r.msg == "report generation failed")
    assert record.incident_id == iid and record.exc_type == "ValueError"


def make_finished(tmp_path, count):
    """`count` fixture-based incidents with distinct start times."""
    ids = []
    for i in range(count):
        name = f"20260301T{10 + i}0000Z-fp{i}"
        directory = tmp_path / "incidents" / name
        shutil.copytree(FIXTURES / "incident_bad_release", directory)
        status = json.loads((directory / "status.json").read_text())
        status.update(
            incident_id=name,
            fingerprint=f"fp{i}",
            started_at=f"2026-03-01T{10 + i}:00:00Z",
            ended_at=f"2026-03-01T{10 + i}:05:00Z",
        )
        (directory / "status.json").write_text(json.dumps(status))
        (directory / "response.md").write_text(f"report {i}\n")
        ids.append(name)
    return ids


def test_list_newest_first_with_limit(tmp_path):
    make_finished(tmp_path, 3)
    client = TestClient(make_app(tmp_path))
    items = client.get("/incidents").json()["incidents"]
    assert [i["incident_id"] for i in items] == [
        "20260301T120000Z-fp2",
        "20260301T110000Z-fp1",
        "20260301T100000Z-fp0",
    ]
    first = items[0]
    assert set(first) == {
        "incident_id",
        "alertname",
        "env",
        "state",
        "outcome",
        "started_at",
        "ended_at",
        "webhook_count",
        "report_url",
    }
    assert first["report_url"] == f"/incidents/{first['incident_id']}"
    assert len(client.get("/incidents?limit=2").json()["incidents"]) == 2
    assert client.get("/incidents?limit=200").status_code == 200
    assert client.get("/incidents?limit=0").status_code == 422
    assert client.get("/incidents?limit=201").status_code == 422


def test_list_skips_bad_directories_and_handles_empty(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert client.get("/incidents").json() == {"incidents": []}
    ids = make_finished(tmp_path, 2)
    root = tmp_path / "incidents"
    (root / "20260301T000000Z-nostatus").mkdir()
    corrupt = root / "20260301T000001Z-corrupt"
    corrupt.mkdir()
    (corrupt / "status.json").write_text("{")
    (root / "stray.txt").write_text("x")
    got = [i["incident_id"] for i in client.get("/incidents").json()["incidents"]]
    assert got == sorted(ids, reverse=True)


def test_get_returns_response_md_as_markdown(tmp_path):
    ids = make_finished(tmp_path, 1)
    client = TestClient(make_app(tmp_path))
    response = client.get(f"/incidents/{ids[0]}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.text == "report 0\n"


def test_unknown_and_unsafe_ids_return_404(tmp_path):
    make_finished(tmp_path, 1)
    (tmp_path / "secret.txt").write_text("OUTSIDE")
    (tmp_path / "incidents" / "response.md").write_text("OUTSIDE")
    client = TestClient(make_app(tmp_path))
    for bad in (
        "20260301T120000Z-unknown",
        "..",
        "%2e%2e",
        "%2e%2e%2fsecret.txt",
        "..%2fsecret.txt",
        "%2Ftmp",
        "20260301T120000Z-",
        "20260301T120000Z-" + "a" * 65,
        "x",
    ):
        response = client.get(f"/incidents/{bad}")
        assert response.status_code == 404, bad
        assert "OUTSIDE" not in response.text
        assert response.json()["error"]["code"] == "not_found"


def test_endpoints_need_no_token_and_are_read_only(tmp_path):
    ids = make_finished(tmp_path, 1)
    client = TestClient(make_app(tmp_path))
    assert client.get("/incidents").status_code == 200
    assert client.get(f"/incidents/{ids[0]}").status_code == 200
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/incidents").status_code == 405
        assert getattr(client, method)(f"/incidents/{ids[0]}").status_code == 405
    # The webhook keeps its token.
    assert client.post("/webhook/alertmanager", json=body(alert())).status_code == 401


def test_revert_note_reaches_the_served_report(tmp_path):
    ids = make_finished(tmp_path, 1)
    shutil.copy(
        FIXTURES / "incident_bad_release" / "expected_report.md",
        tmp_path / "incidents" / ids[0] / "response.md",
    )
    client = TestClient(make_app(tmp_path))
    assert REVERT_NOTE in client.get(f"/incidents/{ids[0]}").text
