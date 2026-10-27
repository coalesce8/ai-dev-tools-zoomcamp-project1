"""HttpWorkerApi against httpx.MockTransport."""

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from runwatch.api.job_schemas import WorkloadSpec
from runwatch.lifecycle import FailureCause
from runwatch.worker.client import (
    HTTP_TIMEOUT_S,
    ApiUnavailable,
    AttemptNotCurrent,
    HttpWorkerApi,
    LogLine,
)

AID = uuid.uuid4()


def api_with(handler, base="http://h:8000"):
    requests = []

    def wrapped(request):
        requests.append(request)
        return handler(request)

    client = httpx.Client(
        transport=httpx.MockTransport(wrapped), timeout=HTTP_TIMEOUT_S
    )
    return HttpWorkerApi(base, client=client), requests


def test_claim_200_and_204():
    jid = uuid.uuid4()
    body = {
        "job_id": str(jid),
        "attempt_id": str(AID),
        "attempt_number": 2,
        "spec": {"duration_s": 5, "fail_probability": 0.5, "mode": "normal"},
        "lease_expires_at": "2026-01-01T00:00:00Z",
        "trace_context": {"a": "b"},
    }
    api, reqs = api_with(lambda r: httpx.Response(200, json=body))
    claim = api.claim("w")
    assert reqs[0].url == "http://h:8000/internal/v1/worker/claim"
    assert json.loads(reqs[0].content) == {"worker_id": "w"}
    assert claim.job_id == jid and claim.attempt_id == AID
    assert claim.attempt_number == 2
    assert claim.spec == WorkloadSpec(duration_s=5, fail_probability=0.5)
    assert claim.trace_context == {"a": "b"}
    api, _ = api_with(lambda r: httpx.Response(204))
    assert api.claim("w") is None


def test_claim_errors_are_unavailable():
    api, _ = api_with(lambda r: httpx.Response(503))
    with pytest.raises(ApiUnavailable):
        api.claim("w")


def test_heartbeat_body_and_statuses():
    api, reqs = api_with(lambda r: httpx.Response(200, json={}))
    ts = datetime(2026, 1, 1, 12, tzinfo=UTC)
    api.heartbeat(AID, [LogLine(ts, "step 1/1")])
    assert str(reqs[0].url).endswith(f"/internal/v1/worker/attempts/{AID}/heartbeat")
    assert json.loads(reqs[0].content) == {
        "lines": [{"ts": "2026-01-01T12:00:00+00:00", "line": "step 1/1"}]
    }
    api, _ = api_with(lambda r: httpx.Response(409))
    with pytest.raises(AttemptNotCurrent):
        api.heartbeat(AID, [])
    for status in (404, 400, 500, 503):
        api, _ = api_with(lambda r, s=status: httpx.Response(s))
        with pytest.raises(ApiUnavailable):
            api.heartbeat(AID, [])


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_errors_are_unavailable(exc):
    def boom(request):
        raise exc("x", request=request)

    api, _ = api_with(boom)
    with pytest.raises(ApiUnavailable):
        api.heartbeat(AID, [])
    with pytest.raises(ApiUnavailable):
        api.result(AID, "succeeded")


def test_result_bodies_and_statuses():
    api, reqs = api_with(lambda r: httpx.Response(200, json={}))
    api.result(AID, "succeeded")
    api.result(AID, "failed", "why", FailureCause.INFRA)
    assert str(reqs[0].url).endswith(f"/attempts/{AID}/result")
    assert json.loads(reqs[0].content) == {"outcome": "succeeded"}
    assert json.loads(reqs[1].content) == {
        "outcome": "failed",
        "reason": "why",
        "cause": "infra",
    }
    api, _ = api_with(lambda r: httpx.Response(409))
    with pytest.raises(AttemptNotCurrent):
        api.result(AID, "succeeded")
    api, _ = api_with(lambda r: httpx.Response(500))
    with pytest.raises(ApiUnavailable):
        api.result(AID, "succeeded")


def test_trailing_slash_gives_same_urls():
    urls = []
    for base in ("http://h:8000", "http://h:8000/"):
        api, reqs = api_with(lambda r: httpx.Response(204), base)
        api.claim("w")
        urls.append(str(reqs[0].url))
    assert urls[0] == urls[1]


def test_timeout_is_2s():
    assert HTTP_TIMEOUT_S == 2
    assert HttpWorkerApi("http://h")._client.timeout == httpx.Timeout(2)
