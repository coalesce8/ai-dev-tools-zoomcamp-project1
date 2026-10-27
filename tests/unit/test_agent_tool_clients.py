import json
import re
from datetime import UTC, datetime

import httpx
import pytest
from runwatch.agent import clients
from runwatch.agent.clients import (
    ForbiddenRequest,
    KubeClient,
    LokiClient,
    RunwatchApiClient,
    ToolError,
)
from runwatch.agent.tools import ToolConfig, ToolRunner

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
JOB_ID = "0b9f6d52-3c1c-4a46-9a53-8d2f6d0f7a11"


class Server:
    """An httpx.MockTransport handler that records requests."""

    def __init__(self, routes=None, status=200, body=None):
        self.requests = []
        self.routes = routes or {}
        self.status = status
        self.body = body if body is not None else {}

    def __call__(self, request):
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.routes:
            value = self.routes[key]
            if callable(value):
                value = value(request)
            return httpx.Response(200, json=value)
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self):
        return httpx.MockTransport(self)


def api_client(server, **kw):
    return RunwatchApiClient("http://api:8000", transport=server.transport, **kw)


# --- API client ---


def test_api_client_sends_only_the_five_requests():
    server = Server(
        {
            ("GET", "/api/v1/stats"): {"a": 1},
            ("GET", "/api/v1/jobs"): {"items": []},
            ("GET", f"/api/v1/jobs/{JOB_ID}/logs"): {"items": []},
            ("POST", "/internal/v1/jobs/requeue-stalled"): {"requeued": 1},
            ("POST", "/internal/v1/jobs/requeue-failed"): {"requeued": 2},
        }
    )
    client = api_client(server)
    assert client.get_stats() == {"a": 1}
    client.list_jobs("FAILED", 20)
    client.job_logs(JOB_ID, 7)
    client.requeue_stalled()
    client.requeue_failed("2026-03-01T11:00:00+00:00", "2026-03-01T11:10:00+00:00")
    r = server.requests
    assert [(x.method, x.url.path) for x in r] == [
        ("GET", "/api/v1/stats"),
        ("GET", "/api/v1/jobs"),
        ("GET", f"/api/v1/jobs/{JOB_ID}/logs"),
        ("POST", "/internal/v1/jobs/requeue-stalled"),
        ("POST", "/internal/v1/jobs/requeue-failed"),
    ]
    assert dict(r[1].url.params) == {"state": "FAILED", "limit": "20"}
    assert dict(r[2].url.params) == {"after": "7"}
    assert r[3].content == b""
    assert json.loads(r[4].content) == {
        "since": "2026-03-01T11:00:00+00:00",
        "until": "2026-03-01T11:10:00+00:00",
    }
    for request in r:
        assert "authorization" not in request.headers


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", f"/api/v1/jobs/{JOB_ID}/cancel"),
        ("POST", f"/api/v1/jobs/{JOB_ID}/requeue"),
        ("POST", "/api/v1/jobs"),
        ("DELETE", f"/api/v1/jobs/{JOB_ID}"),
        ("GET", f"/api/v1/jobs/{JOB_ID}"),
        ("GET", "/api/v1/jobs/../stats"),
        ("GET", "/api/v1/jobs/x/logs"),
        ("POST", "/api/v1/stats"),
        ("GET", "/internal/v1/jobs/requeue-stalled"),
        ("GET", "//evil/api/v1/stats"),
    ],
)
def test_api_client_refuses_other_requests(method, path):
    server = Server()
    with pytest.raises(ForbiddenRequest):
        api_client(server).request(method, path)
    assert server.requests == []


def test_api_client_timeout_comes_from_config():
    client = api_client(Server(), timeout_s=3.5)
    assert client.http.timeout == httpx.Timeout(3.5)
    config = ToolConfig.from_environment({"AGENT_TOOL_TIMEOUT_S": "4"})
    runner = ToolRunner.from_environment({"AGENT_TOOL_TIMEOUT_S": "4"})
    assert runner.api.http.timeout == httpx.Timeout(config.timeout_s)
    assert runner.loki.http.timeout == httpx.Timeout(4)


def test_non_2xx_becomes_tool_error_with_cut_body():
    server = Server(status=503, body={"detail": "x" * 1000})
    with pytest.raises(ToolError) as caught:
        api_client(server).get_stats()
    reason = caught.value.reason
    assert reason.startswith("API returned 503: ")
    assert len(reason) <= len("API returned 503: ") + 200
    assert caught.value.status == 503


def test_timeout_and_connection_errors_become_tool_errors():
    def raise_timeout(request):
        raise httpx.ReadTimeout("secret-url-http://u:p@h", request=request)

    def raise_connect(request):
        raise httpx.ConnectError("secret-url-http://u:p@h", request=request)

    client = LokiClient(
        "http://loki:3100", transport=httpx.MockTransport(raise_timeout)
    )
    with pytest.raises(ToolError, match="request to Loki timed out") as caught:
        client.query_range('{a="b"}', 1, 2, 10)
    assert "secret" not in caught.value.reason
    client = LokiClient(
        "http://loki:3100", transport=httpx.MockTransport(raise_connect)
    )
    with pytest.raises(ToolError, match="could not reach Loki") as caught:
        client.query_range('{a="b"}', 1, 2, 10)
    assert "secret" not in caught.value.reason


def test_invalid_json_is_a_tool_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ToolError, match="invalid JSON"):
        RunwatchApiClient("http://api", transport=transport).get_stats()


# --- Loki client ---


def test_loki_client_request_and_allow_list():
    server = Server({("GET", "/loki/api/v1/query_range"): {"data": {"result": []}}})
    client = LokiClient("http://loki:3100", transport=server.transport)
    client.query_range('{a="b"}', 10, 20, 100)
    params = dict(server.requests[0].url.params)
    assert params == {
        "query": '{a="b"}',
        "limit": "100",
        "direction": "backward",
        "start": "10",
        "end": "20",
    }
    for method, path in [
        ("POST", "/loki/api/v1/query_range"),
        ("GET", "/loki/api/v1/push"),
        ("GET", "/loki/api/v1/delete"),
        ("GET", "/loki/api/v1/query"),
    ]:
        with pytest.raises(ForbiddenRequest):
            client.request(method, path)
    assert len(server.requests) == 1


# --- Kubernetes client ---


def kube_client(server, tmp_path, namespace="runwatch-dev", token="tok-1"):
    token_file = tmp_path / "token"
    token_file.write_text(token)
    return KubeClient(
        "https://10.0.0.1:443",
        namespace,
        token_file,
        tmp_path / "ca.crt",
        transport=server.transport,
    )


def test_kube_client_reads_the_token_on_every_request(tmp_path):
    server = Server({("GET", "/apis/apps/v1/namespaces/runwatch-dev/replicasets"): {}})
    client = kube_client(server, tmp_path)
    client.list_replicasets()
    (tmp_path / "token").write_text("tok-2\n")
    client.list_replicasets()
    seen = [r.headers["authorization"] for r in server.requests]
    assert seen == ["Bearer tok-1", "Bearer tok-2"]


def test_kube_client_verifies_against_the_service_account_ca(tmp_path, monkeypatch):
    used = []
    monkeypatch.setattr(clients, "_ssl_context", lambda path: used.append(path) or True)
    token_file = tmp_path / "token"
    token_file.write_text("t")
    KubeClient("https://10.0.0.1:443", "ns", token_file, tmp_path / "ca.crt")
    assert used == [str(tmp_path / "ca.crt")]


def test_kube_client_allow_list(tmp_path):
    server = Server()
    client = kube_client(server, tmp_path)
    ns = "/namespaces/runwatch-dev"
    bad = [
        ("DELETE", f"/apis/apps/v1{ns}/deployments/worker"),
        ("POST", f"/apis/apps/v1{ns}/deployments"),
        ("POST", f"/api/v1{ns}/pods"),
        ("GET", "/apis/apps/v1/namespaces/runwatch-prod/deployments/worker"),
        ("PATCH", "/apis/apps/v1/namespaces/runwatch-prod/deployments/worker"),
        ("GET", "/api/v1/namespaces/runwatch-dev/secrets"),
        ("GET", "/api/v1/namespaces"),
        ("GET", f"/api/v1{ns}/pods/p/exec"),
        ("GET", f"/api/v1{ns}/pods/p/log"),
        ("PATCH", f"/apis/apps/v1{ns}/deployments/loadgen"),
        ("PATCH", f"/apis/apps/v1{ns}/deployments/api/scale"),
        ("PATCH", f"/apis/apps/v1{ns}/deployments/frontend/scale"),
        ("PUT", f"/apis/apps/v1{ns}/deployments/worker/scale"),
        ("GET", f"/apis/apps/v1{ns}/deployments/postgres"),
        ("GET", f"/apis/apps/v1{ns}x/replicasets"),
    ]
    for method, path in bad:
        with pytest.raises(ForbiddenRequest):
            client.request(method, path)
    assert server.requests == []


def test_kube_from_environment(tmp_path):
    token = tmp_path / "token"
    ns_file = tmp_path / "namespace"
    ca = tmp_path / "ca.crt"
    ns_file.write_text("from-file\n")

    def build(env, **kw):
        return KubeClient.from_environment(
            env, token_path=token, ca_path=ca, namespace_path=ns_file,
            transport=Server().transport, **kw,
        )  # fmt: skip

    env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "6443"}
    assert build(env) is None  # no token file
    token.write_text("t")
    assert build({}) is None  # no host
    client = build(env)
    assert client.namespace == "from-file"
    assert str(client.http.base_url) == "https://10.0.0.1:6443"
    assert build({**env, "AGENT_K8S_NAMESPACE": "chosen"}).namespace == "chosen"
    ns_file.write_text("\n")
    assert build(env) is None  # neither source gives a namespace
    assert build({**env, "AGENT_K8S_NAMESPACE": "  "}) is None
    missing = KubeClient.from_environment(
        env, token_path=token, ca_path=ca, namespace_path=tmp_path / "none"
    )
    assert missing is None
    assert build({**env, "AGENT_K8S_NAMESPACE": "chosen"}) is not None


# --- the runner with the real clients over mock transports ---


def make_runner(api, kube):
    loki = LokiClient("http://l", transport=Server().transport)
    return ToolRunner(ToolConfig(), api, loki, kube, clock=lambda: NOW)


def pod_server():
    base = "/apis/apps/v1/namespaces/runwatch-dev"
    rs = lambda owner, rev, tag: {  # noqa: E731
        "metadata": {
            "creationTimestamp": f"2026-03-01T1{rev}:00:00Z",
            "annotations": {"deployment.kubernetes.io/revision": str(rev)},
            "ownerReferences": [{"kind": "Deployment", "name": owner}],
        },
        "spec": {
            "replicas": 0,
            "template": {
                "metadata": {"labels": {"pod-template-hash": "h"}},
                "spec": {"containers": [{"image": f"r/{owner}:{tag}"}]},
            },
        },
    }
    return Server(
        {
            ("GET", f"{base}/deployments/worker"): {
                "metadata": {"annotations": {"deployment.kubernetes.io/revision": "2"}},
                "spec": {"selector": {"matchLabels": {"app": "worker"}}},
            },
            ("GET", f"{base}/replicasets"): {
                "items": [
                    rs("worker", 1, "v1"),
                    rs("worker", 2, "v2"),
                    rs("api", 1, "a"),
                ]
            },
            ("GET", "/api/v1/namespaces/runwatch-dev/pods"): {"items": []},
            ("GET", "/api/v1/namespaces/runwatch-dev/events"): {"items": []},
            ("PATCH", f"{base}/deployments/worker/scale"): {"spec": {"replicas": 2}},
            ("PATCH", f"{base}/deployments/worker"): {},
            ("PATCH", f"{base}/deployments/api"): {},
        }
    )


def test_every_k8s_request_stays_in_the_configured_namespace(tmp_path):
    server = pod_server()
    kube = kube_client(server, tmp_path)
    runner = make_runner(api_client(Server()), kube)
    runs = [
        ("k8s_get_pods", {"app": "worker"}),
        ("k8s_get_events", {}),
        ("k8s_rollout_history", {"deployment": "worker"}),
        ("k8s_scale", {"deployment": "worker", "replicas": 2}),
        ("k8s_rollout_restart", {"deployment": "api"}),
        ("k8s_rollout_undo", {"deployment": "worker"}),
    ]
    for name, args in runs:
        result = runner.run(name, args)
        assert result.ok, (name, result.content)
    assert server.requests
    allowed = re.compile(
        r"/apis?/(apps/)?v1/namespaces/runwatch-dev/"
        r"(deployments(/(worker|api))?(/scale)?|replicasets|pods|events)"
    )
    for request in server.requests:
        assert request.method in ("GET", "PATCH")
        assert allowed.fullmatch(request.url.path), request.url.path
        assert "runwatch-prod" not in str(request.url)
    patches = {
        (r.url.path.rsplit("/", 1)[-1], r.headers.get("content-type", ""))
        for r in server.requests
        if r.method == "PATCH"
    }
    assert patches == {
        ("scale", "application/merge-patch+json"),
        ("api", "application/merge-patch+json"),
        ("worker", "application/json-patch+json"),
    }
    scale = next(r for r in server.requests if r.url.path.endswith("/scale"))
    assert json.loads(scale.content) == {"spec": {"replicas": 2}}
    undo = [
        r
        for r in server.requests
        if r.headers.get("content-type", "").endswith("json-patch+json")
    ][0]
    assert (
        json.loads(undo.content)[0]["value"]["spec"]["containers"][0]["image"]
        == "r/worker:v1"
    )


def test_kube_error_statuses_are_results(tmp_path):
    server = Server(status=409, body={"message": "conflict"})
    runner = make_runner(api_client(Server()), kube_client(server, tmp_path))
    result = runner.run("k8s_scale", {"deployment": "worker", "replicas": 2})
    assert (result.ok, result.refused, result.executed) == (False, False, True)
    assert "409" in result.content
    assert len(server.requests) == 1  # no retry


def test_huge_loki_response_is_cut():
    values = [["1772366400000000000", "y" * 5000] for _ in range(100)]
    body = {"data": {"result": [{"values": values}]}}
    loki_server = Server({("GET", "/loki/api/v1/query_range"): body})
    runner = ToolRunner(
        ToolConfig(env="dev"),
        api_client(Server()),
        LokiClient("http://loki:3100", transport=loki_server.transport),
        None,
        clock=lambda: NOW,
    )
    result = runner.run("query_loki", {"query": '{service_name="a"}'})
    assert result.ok and result.truncated
    assert len(result.content) == 8000 and result.content.endswith("...[truncated]")
    sent = loki_server.requests[0].url.params["query"]
    assert sent == '{service_name="a"} | deployment_environment="dev"'


def test_api_end_to_end_through_the_runner():
    server = Server({("POST", "/internal/v1/jobs/requeue-stalled"): {"requeued": 4}})
    runner = make_runner(api_client(server), None)
    result = runner.run("requeue_stalled_jobs", {})
    assert json.loads(result.content) == {"requeued": 4}
    missing = make_runner(api_client(Server(status=404)), None)
    result = missing.run("get_job_logs", {"id": JOB_ID})
    assert "job not found" in result.content
