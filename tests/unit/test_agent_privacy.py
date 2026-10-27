import io
import json
import logging

from fastapi.testclient import TestClient
from opentelemetry.sdk._logs import LoggerProvider
from runwatch.agent.app import AGENT_SERVICE, create_app
from runwatch.logs import configure_logging

from tests.unit.agent_support import (
    AUTH,
    TOKEN,
    FakeClock,
    RaisingResponder,
    alert,
    body,
    make_settings,
)

MARKER = "MARKER-" + "7f3a9c"
KEY = "sk-" + "unit-secret"


def test_webhook_content_and_secrets_reach_no_log(tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    settings = make_settings(tmp_path, anthropic_api_key=KEY)
    app = create_app(
        settings,
        responder=RaisingResponder(),
        clock=FakeClock(),
        logger_provider=LoggerProvider(),
    )
    secret_alert = alert(
        annotations={"summary": MARKER, "description": MARKER},
        labels={"alertname": "A", "env": "demo", "job_name": MARKER},
    )
    with TestClient(app) as client:
        for _ in range(2):
            client.post("/webhook/alertmanager", json=body(secret_alert), headers=AUTH)
        client.post("/webhook/alertmanager", json=body(secret_alert))
        client.post(
            "/webhook/alertmanager",
            content=json.dumps({"alerts": [{"x": MARKER}]}),
            headers=AUTH,
        )
        app.state.runs.wait_idle()
    assert caplog.records
    out = capsys.readouterr()
    haystacks = [out.out, out.err]
    for record in caplog.records:
        haystacks.append(json.dumps(vars(record), default=str))
        haystacks.append(record.getMessage())
    text = "\n".join(haystacks)
    for secret in (MARKER, TOKEN, KEY, "MARKER-IN-EXC"):
        assert secret not in text


def test_agent_log_fields_survive_the_privacy_filter(tmp_path):
    stream = io.StringIO()
    configure_logging(AGENT_SERVICE, None, stream)
    app = create_app(make_settings(tmp_path), clock=FakeClock())
    # The lifespan reconfigures logging to stdout, so use the controller alone.
    client = TestClient(app)
    client.post("/webhook/alertmanager", json=body(alert()), headers=AUTH)
    app.state.runs.wait_idle()
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    handled = [x for x in lines if x["message"] == "webhook handled"]
    assert handled and handled[0]["service"] == AGENT_SERVICE
    assert handled[0]["action"] == "started"
    assert handled[0]["fingerprint"] == "fp1"
    assert handled[0]["alertname"] == "HighFailureRate"
    assert handled[0]["incident_id"].endswith("-fp1")
    finished = [x for x in lines if x["message"] == "run finished"]
    assert finished[0]["outcome"] == "skipped_no_model"
    assert set(handled[0]) <= {
        "timestamp",
        "level",
        "logger",
        "message",
        "service",
        "incident_id",
        "fingerprint",
        "alertname",
        "action",
    }
