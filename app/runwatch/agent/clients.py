"""Thin HTTP clients for the agent's tools: Runwatch API, Loki, Kubernetes.

Each client has its own allow-list of methods and paths and checks it itself, as
a second layer under the rules in `tools.py`. A request outside the list raises
`ForbiddenRequest` before anything is sent. Failures of an allowed request raise
`ToolError` with a short reason that holds no URL, token or request body.
"""

from __future__ import annotations

import os
import re
import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

BODY_REASON_CHARS = 200

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
MERGE_PATCH = "application/merge-patch+json"
JSON_PATCH = "application/json-patch+json"


class ForbiddenRequest(Exception):  # noqa: N818 - a refusal, not an error state
    """The client refused to send a request outside its allow-list."""


class ToolError(Exception):
    """An allowed request failed. `reason` is safe to show the model."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _Client:
    """Shared plumbing: allow-list check, timeouts, error mapping."""

    service = "service"
    allowed: tuple[tuple[str, re.Pattern[str]], ...] = ()

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def _check(self, method: str, path: str) -> None:
        for allowed_method, pattern in self.allowed:
            if method == allowed_method and pattern.fullmatch(path):
                return
        raise ForbiddenRequest(f"{self.service} request not allowed")

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self._check(method, path)
        try:
            response = self.http.request(
                method,
                path,
                params=params,
                json=json,
                content=content,
                headers=headers,
            )
        except httpx.TimeoutException:
            raise ToolError(f"request to {self.service} timed out") from None
        except httpx.HTTPError:
            raise ToolError(f"could not reach {self.service}") from None
        if not 200 <= response.status_code < 300:
            body = " ".join(response.text.split())[:BODY_REASON_CHARS]
            raise ToolError(
                f"{self.service} returned {response.status_code}: {body}",
                response.status_code,
            )
        try:
            return response.json()
        except ValueError:
            raise ToolError(f"invalid JSON from {self.service}") from None


_API_ALLOWED = (
    ("GET", re.compile(r"/api/v1/stats")),
    ("GET", re.compile(r"/api/v1/jobs")),
    ("GET", re.compile(rf"/api/v1/jobs/{UUID_PATTERN}/logs")),
    ("POST", re.compile(r"/internal/v1/jobs/requeue-stalled")),
    ("POST", re.compile(r"/internal/v1/jobs/requeue-failed")),
)


class RunwatchApiClient(_Client):
    """The Runwatch API. It has no auth, so no credential header is sent."""

    service = "API"
    allowed = _API_ALLOWED

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            httpx.Client(
                base_url=base_url.rstrip("/"),
                timeout=httpx.Timeout(timeout_s),
                transport=transport,
            )
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._send(method, path, **kwargs)

    def get_stats(self) -> Any:
        return self._send("GET", "/api/v1/stats")

    def list_jobs(self, state: str, limit: int) -> Any:
        return self._send(
            "GET", "/api/v1/jobs", params={"state": state, "limit": limit}
        )

    def job_logs(self, job_id: str, after: int) -> Any:
        return self._send("GET", f"/api/v1/jobs/{job_id}/logs", params={"after": after})

    def requeue_stalled(self) -> Any:
        return self._send("POST", "/internal/v1/jobs/requeue-stalled")

    def requeue_failed(self, since: str, until: str) -> Any:
        return self._send(
            "POST",
            "/internal/v1/jobs/requeue-failed",
            json={"since": since, "until": until},
        )


class LokiClient(_Client):
    service = "Loki"
    allowed = (("GET", re.compile(r"/loki/api/v1/query_range")),)

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            httpx.Client(
                base_url=base_url.rstrip("/"),
                timeout=httpx.Timeout(timeout_s),
                transport=transport,
            )
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._send(method, path, **kwargs)

    def query_range(self, query: str, start_ns: int, end_ns: int, limit: int) -> Any:
        return self._send(
            "GET",
            "/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": limit,
                "direction": "backward",
                "start": start_ns,
                "end": end_ns,
            },
        )


def _ssl_context(ca_path: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca_path)


class _BearerFromFile(httpx.Auth):
    """Reads the token file on every request: the kubelet rotates the token."""

    def __init__(self, token_path: str | Path) -> None:
        self.token_path = Path(token_path)

    def auth_flow(self, request: httpx.Request):
        token = self.token_path.read_text().strip()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class KubeClient(_Client):
    """The Kubernetes REST API, in one namespace, for the verbs the Role allows."""

    service = "Kubernetes"

    def __init__(
        self,
        base_url: str,
        namespace: str,
        token_path: str | Path,
        ca_path: str | Path,
        timeout_s: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.namespace = namespace
        ns = re.escape(namespace)
        apps = rf"/apis/apps/v1/namespaces/{ns}"
        core = rf"/api/v1/namespaces/{ns}"
        self.allowed = (
            ("GET", re.compile(rf"{apps}/deployments/(api|worker|loadgen|frontend)")),
            ("GET", re.compile(rf"{apps}/replicasets")),
            ("GET", re.compile(rf"{core}/pods")),
            ("GET", re.compile(rf"{core}/events")),
            ("PATCH", re.compile(rf"{apps}/deployments/(api|worker)")),
            ("PATCH", re.compile(rf"{apps}/deployments/worker/scale")),
        )
        verify = _ssl_context(str(ca_path)) if transport is None else True
        super().__init__(
            httpx.Client(
                base_url=base_url,
                timeout=httpx.Timeout(timeout_s),
                transport=transport,
                verify=verify,
                auth=_BearerFromFile(token_path),
            )
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        token_path: str | Path = SERVICE_ACCOUNT_DIR / "token",
        ca_path: str | Path = SERVICE_ACCOUNT_DIR / "ca.crt",
        namespace_path: str | Path = SERVICE_ACCOUNT_DIR / "namespace",
        timeout_s: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> KubeClient | None:
        """The client for the pod's service account, or None (k8s tools off)."""
        env = os.environ if environ is None else environ
        host = env.get("KUBERNETES_SERVICE_HOST", "").strip()
        if not host or not Path(token_path).is_file():
            return None
        namespace = env.get("AGENT_K8S_NAMESPACE", "").strip()
        if not namespace:
            try:
                namespace = Path(namespace_path).read_text().strip()
            except OSError:
                namespace = ""
        if not namespace:
            return None
        port = env.get("KUBERNETES_SERVICE_PORT", "").strip() or "443"
        shown = f"[{host}]" if ":" in host else host
        return cls(
            f"https://{shown}:{port}",
            namespace,
            token_path,
            ca_path,
            timeout_s,
            transport,
        )

    def _apps(self) -> str:
        return f"/apis/apps/v1/namespaces/{self.namespace}"

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._send(method, path, **kwargs)

    def get_deployment(self, name: str) -> Any:
        return self._send("GET", f"{self._apps()}/deployments/{quote(name, safe='')}")

    def list_replicasets(self) -> Any:
        return self._send("GET", f"{self._apps()}/replicasets")

    def list_pods(self, label_selector: str) -> Any:
        return self._send(
            "GET",
            f"/api/v1/namespaces/{self.namespace}/pods",
            params={"labelSelector": label_selector},
        )

    def list_events(self) -> Any:
        return self._send("GET", f"/api/v1/namespaces/{self.namespace}/events")

    def scale(self, name: str, replicas: int) -> Any:
        return self._send(
            "PATCH",
            f"{self._apps()}/deployments/{quote(name, safe='')}/scale",
            json={"spec": {"replicas": replicas}},
            headers={"Content-Type": MERGE_PATCH},
        )

    def merge_patch_deployment(self, name: str, body: Any) -> Any:
        return self._send(
            "PATCH",
            f"{self._apps()}/deployments/{quote(name, safe='')}",
            json=body,
            headers={"Content-Type": MERGE_PATCH},
        )

    def json_patch_deployment(self, name: str, operations: list[dict]) -> Any:
        return self._send(
            "PATCH",
            f"{self._apps()}/deployments/{quote(name, safe='')}",
            json=operations,
            headers={"Content-Type": JSON_PATCH},
        )
