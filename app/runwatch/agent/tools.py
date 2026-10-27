"""The responder agent's twelve tools and the rules every call must pass.

Each tool is declared once (`TOOLS`); the Anthropic tool definitions the model
sees and the validator that checks every call are both built from that one
declaration, so they cannot drift. `ToolRunner.run` validates first and makes
no client call for a refused call. Refusals and failures are results, never
exceptions: the loop (#28) hands them back to the model.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from runwatch.agent.clients import (
    KubeClient,
    LokiClient,
    RunwatchApiClient,
    ToolError,
)
from runwatch.agent.runs import Clock, utc_now
from runwatch.settings import SettingsError

logger = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 8000
TRUNCATED_MARKER = "...[truncated]"
MAX_JOBS = 20
MAX_LOG_LINES = 50
MAX_LOG_PAGES = 5
MAX_LOKI_ENTRIES = 100
MAX_EVENTS = 50
MAX_REVISIONS = 10
LAST_ERROR_CHARS = 200
LOG_LINE_CHARS = 200
LOKI_LINE_CHARS = 300
EVENT_MESSAGE_CHARS = 300
MAX_QUERY_CHARS = 500
MAX_WINDOW = timedelta(hours=2)
FUTURE_SKEW = timedelta(seconds=60)
LOKI_LOOKBACK = timedelta(minutes=15)

JOB_STATES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "STALLED", "CANCELLED")
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE_ANNOTATION = "kubernetes.io/change-cause"
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
POD_TEMPLATE_HASH = "pod-template-hash"


# --- declarations ---------------------------------------------------------


@dataclass(frozen=True)
class Param:
    """One tool argument. `kind` picks both the schema and the check."""

    kind: str  # enum | int | uuid | timestamp | selector
    description: str
    values: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int = 0
    free_text: bool = False  # never logged

    def schema(self) -> dict[str, Any]:
        if self.kind == "enum":
            out: dict[str, Any] = {"type": "string", "enum": list(self.values)}
        elif self.kind == "int":
            out = {"type": "integer", "minimum": self.minimum, "maximum": self.maximum}
        elif self.kind == "uuid":
            out = {"type": "string", "format": "uuid"}
        elif self.kind == "timestamp":
            out = {"type": "string", "format": "date-time"}
        else:
            out = {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS}
        out["description"] = self.description
        return out

    def check(self, value: Any) -> Any:
        """Return the accepted (normalised) value or raise `Refusal`."""
        if self.kind == "enum":
            if not isinstance(value, str) or value not in self.values:
                raise Refusal("value is not one of the allowed values")
            return value
        if self.kind == "int":
            if type(value) is not int or not self.minimum <= value <= self.maximum:
                raise Refusal(
                    f"must be an integer from {self.minimum} to {self.maximum}"
                )
            return value
        if self.kind == "uuid":
            if not isinstance(value, str):
                raise Refusal("must be a UUID string")
            try:
                parsed = uuid.UUID(value)
            except ValueError:
                raise Refusal("must be a UUID string") from None
            if str(parsed) != value.lower():
                raise Refusal("must be a canonical UUID string")
            return value
        if self.kind == "timestamp":
            return parse_timestamp(value)
        if not isinstance(value, str) or not value.startswith("{"):
            raise Refusal("must be a log stream selector starting with '{'")
        if len(value) > MAX_QUERY_CHARS:
            raise Refusal(f"must be at most {MAX_QUERY_CHARS} characters")
        if selector_end(value) is None:
            raise Refusal("selector has no closing '}'")
        return value


class Refusal(Exception):  # noqa: N818
    """A call broke a rule. The message names the rule, never the value."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    write: bool = False
    kube: bool = False
    params: Mapping[str, Param] = field(default_factory=dict)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": {k: p.schema() for k, p in self.params.items()},
                "required": list(self.params),
                "additionalProperties": False,
            },
        }


_DEPLOYMENT = Param("enum", "Deployment name.", ("worker", "api"))

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "get_stats",
        "Read-only, changes nothing. Queue health: job counts by state, age of "
        "the oldest queued job, failure rates over the recent window.",
    ),
    ToolSpec(
        "list_jobs",
        "Read-only, changes nothing. The 20 newest jobs in one state, without "
        "names or specs.",
        params={"state": Param("enum", "Job state to list.", JOB_STATES)},
    ),
    ToolSpec(
        "get_job_logs",
        "Read-only, changes nothing. The last 50 log lines of one job.",
        params={"id": Param("uuid", "Job id.")},
    ),
    ToolSpec(
        "k8s_get_pods",
        "Read-only, changes nothing. Pods of one Deployment: phase, readiness, "
        "restarts, start time, image tags. An empty list means no pods.",
        kube=True,
        params={
            "app": Param(
                "enum",
                "Deployment name.",
                ("api", "worker", "loadgen", "frontend"),
            )
        },
    ),
    ToolSpec(
        "k8s_get_events",
        "Read-only, changes nothing. The 50 newest Kubernetes events.",
        kube=True,
    ),
    ToolSpec(
        "k8s_rollout_history",
        "Read-only, changes nothing. Revisions of a Deployment, newest first, "
        "with creation time, image tag and change cause.",
        kube=True,
        params={"deployment": _DEPLOYMENT},
    ),
    ToolSpec(
        "query_loki",
        "Read-only, changes nothing. Logs of the last 15 minutes from a LogQL "
        'log stream selector such as {service_name="runwatch-worker"}, '
        "optionally followed by line filters. Limited to this environment.",
        params={
            "query": Param(
                "selector",
                "LogQL log stream selector, starting with '{'.",
                free_text=True,
            )
        },
    ),
    ToolSpec(
        "k8s_rollout_restart",
        "WRITE: restarts the pods of a Deployment (rolling restart).",
        write=True,
        kube=True,
        params={"deployment": _DEPLOYMENT},
    ),
    ToolSpec(
        "k8s_scale",
        "WRITE: sets the number of worker pods, from 1 to 3. Only the worker "
        "can be scaled.",
        write=True,
        kube=True,
        params={
            "deployment": Param("enum", "Deployment name.", ("worker",)),
            "replicas": Param("int", "Desired replicas.", minimum=1, maximum=3),
        },
    ),
    ToolSpec(
        "k8s_rollout_undo",
        "WRITE: rolls a Deployment back to its previous revision.",
        write=True,
        kube=True,
        params={"deployment": _DEPLOYMENT},
    ),
    ToolSpec(
        "requeue_stalled_jobs",
        "WRITE: requeues every job in the STALLED state.",
        write=True,
    ),
    ToolSpec(
        "requeue_failed_jobs",
        "WRITE: requeues jobs whose attempts failed from an infrastructure "
        "cause between two times (at most 2 hours apart, not in the future).",
        write=True,
        params={
            "since": Param("timestamp", "Window start, ISO 8601 with UTC offset."),
            "until": Param("timestamp", "Window end, ISO 8601 with UTC offset."),
        },
    ),
)
_BY_NAME = {t.name: t for t in TOOLS}


def tool_definitions(kube_enabled: bool = True) -> list[dict[str, Any]]:
    return [t.definition() for t in TOOLS if kube_enabled or not t.kube]


def is_write(name: str) -> bool:
    return _BY_NAME[name].write


# --- result and config ----------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    refused: bool = False
    executed: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class ToolConfig:
    api_url: str = "http://localhost:8000"
    loki_url: str = "http://localhost:3100"
    env: str = "local"
    k8s_namespace: str | None = None
    timeout_s: float = 10

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ToolConfig:
        env = os.environ if environ is None else environ

        def text(name: str, default: str) -> str:
            return env.get(name, "").strip() or default

        try:
            timeout = float(text("AGENT_TOOL_TIMEOUT_S", "10"))
        except ValueError:
            timeout = 0
        if not timeout > 0:
            raise SettingsError(
                "Invalid environment settings: AGENT_TOOL_TIMEOUT_S: "
                "must be a number greater than 0"
            )
        return cls(
            api_url=text("RUNWATCH_API_URL", cls.api_url),
            loki_url=text("AGENT_LOKI_URL", cls.loki_url),
            env=text("AGENT_ENV", cls.env),
            k8s_namespace=env.get("AGENT_K8S_NAMESPACE", "").strip() or None,
            timeout_s=timeout,
        )


# --- helpers --------------------------------------------------------------


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise Refusal("must be an ISO 8601 string with a UTC offset")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise Refusal("must be an ISO 8601 string with a UTC offset") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise Refusal("must carry a UTC offset")
    return moment.astimezone(UTC)


def selector_end(query: str) -> int | None:
    """Index of the `}` closing the leading stream selector, quotes respected."""
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(query):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in ('"', "`"):
            quote = ch
        elif ch == "}":
            return i
    return None


def add_environment(query: str, env: str) -> str:
    end = selector_end(query)
    if end is None:
        raise Refusal("selector has no closing '}'")
    flt = f" | deployment_environment={json.dumps(env)}"
    return query[: end + 1] + flt + query[end + 1 :]


def image_tag(image: str) -> str | None:
    if "@" in image:
        return None
    name = image.rsplit("/", 1)[-1]
    return name.rsplit(":", 1)[1] if ":" in name else None


def _cut(text: Any, limit: int) -> str | None:
    return None if text is None else str(text)[:limit]


def _ns(moment: datetime) -> int:
    return int(moment.timestamp()) * 10**9 + moment.microsecond * 1000


def _items(body: Any) -> list[dict]:
    items = body.get("items") if isinstance(body, dict) else None
    return [i for i in items or [] if isinstance(i, dict)]


def _annotations(obj: dict) -> dict:
    return (obj.get("metadata") or {}).get("annotations") or {}


def _revision(obj: dict) -> int | None:
    try:
        return int(_annotations(obj).get(REVISION_ANNOTATION))
    except (TypeError, ValueError):
        return None


def _first_image_tag(template: dict) -> str | None:
    containers = (template.get("spec") or {}).get("containers") or []
    if containers and isinstance(containers[0], dict):
        return image_tag(containers[0].get("image") or "")
    return None


# --- the runner -----------------------------------------------------------


class ToolRunner:
    def __init__(
        self,
        config: ToolConfig,
        api: RunwatchApiClient,
        loki: LokiClient,
        kube: KubeClient | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.config = config
        self.api = api
        self.loki = loki
        self.kube = kube
        self.clock = clock

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, clock: Clock = utc_now
    ) -> ToolRunner:
        config = ToolConfig.from_environment(environ)
        kube = KubeClient.from_environment(environ, timeout_s=config.timeout_s)
        return cls(
            config,
            RunwatchApiClient(config.api_url, config.timeout_s),
            LokiClient(config.loki_url, config.timeout_s),
            kube,
            clock,
        )

    @property
    def kube_enabled(self) -> bool:
        return self.kube is not None

    def definitions(self) -> list[dict[str, Any]]:
        return tool_definitions(self.kube_enabled)

    # -- entry point --

    def run(self, name: Any, args: Any) -> ToolResult:
        spec = _BY_NAME.get(name) if isinstance(name, str) else None
        if spec is None:
            return self._finish(None, None, self._refuse("unknown tool"))
        try:
            clean = self._validate(spec, args)
        except Refusal as exc:
            return self._finish(spec, None, self._refuse(str(exc)))
        try:
            data = getattr(self, f"_do_{spec.name}")(clean)
            result = self._pack(data)
        except ToolError as exc:
            result = self._fail(exc.reason)
        except Exception:  # a client or API that breaks its contract
            result = self._fail("unexpected response from the service")
        return self._finish(spec, clean, result)

    # -- validation --

    def _validate(self, spec: ToolSpec, args: Any) -> dict[str, Any]:
        if spec.kube and not self.kube_enabled:
            raise Refusal("Kubernetes is disabled: no Kubernetes credentials")
        if not isinstance(args, dict):
            raise Refusal("arguments must be a JSON object")
        if any(
            isinstance(k, str) and k.lower() in ("namespace", "namespaces")
            for k in args
        ):
            raise Refusal(
                "namespace rule: the namespace is set by configuration, "
                "never by the caller"
            )
        for key in args:
            if key not in spec.params:
                raise Refusal("an argument is not in the tool's schema")
        clean: dict[str, Any] = {}
        for key, param in spec.params.items():
            if key not in args:
                raise Refusal(f"argument '{key}' is required")
            try:
                clean[key] = param.check(args[key])
            except Refusal as exc:
                raise Refusal(f"argument '{key}' {exc}") from None
        if spec.name == "requeue_failed_jobs":
            since, until = clean["since"], clean["until"]
            if since > until:
                raise Refusal("since must not be after until")
            if until > self.clock() + FUTURE_SKEW:
                raise Refusal("until must not be in the future")
            if until - since > MAX_WINDOW:
                raise Refusal("window must not be longer than 2 hours")
        return clean

    # -- result plumbing --

    @staticmethod
    def _refuse(reason: str) -> ToolResult:
        return ToolResult(False, json.dumps({"error": reason}), refused=True)

    @staticmethod
    def _fail(reason: str) -> ToolResult:
        return ToolResult(False, json.dumps({"error": reason}), executed=True)

    @staticmethod
    def _pack(data: Any) -> ToolResult:
        content = json.dumps(data)
        truncated = len(content) > MAX_CONTENT_CHARS
        if truncated:
            content = content[: MAX_CONTENT_CHARS - len(TRUNCATED_MARKER)]
            content += TRUNCATED_MARKER
        return ToolResult(True, content, executed=True, truncated=truncated)

    @staticmethod
    def _finish(
        spec: ToolSpec | None, clean: dict | None, result: ToolResult
    ) -> ToolResult:
        shown = []
        if spec and clean:
            for key, param in spec.params.items():
                value = clean.get(key)
                if param.free_text:
                    shown.append(f"{key}_length={len(value)}")
                else:
                    text = value.isoformat() if isinstance(value, datetime) else value
                    shown.append(f"{key}={text}")
        logger.info(
            "tool call tool=%s ok=%s refused=%s args=[%s] result_length=%d",
            spec.name if spec else "unknown",
            result.ok,
            result.refused,
            " ".join(shown),
            len(result.content),
        )
        return result

    # -- Runwatch API tools --

    def _do_get_stats(self, _: dict) -> Any:
        return self.api.get_stats()

    def _do_list_jobs(self, args: dict) -> Any:
        body = self.api.list_jobs(args["state"], MAX_JOBS)
        keep = (
            "id",
            "state",
            "attempt_count",
            "max_attempts",
            "created_at",
            "started_at",
            "finished_at",
        )
        jobs = []
        for item in _items(body)[:MAX_JOBS]:
            job = {k: item.get(k) for k in keep}
            job["last_error"] = _cut(item.get("last_error"), LAST_ERROR_CHARS)
            jobs.append(job)
        return {"jobs": jobs, "has_more": bool(body.get("next_cursor"))}

    def _do_get_job_logs(self, args: dict) -> Any:
        lines: list[dict] = []
        dropped = False
        after = 0
        has_more = False
        for _ in range(MAX_LOG_PAGES):
            try:
                body = self.api.job_logs(args["id"], after)
            except ToolError as exc:
                if exc.status == 404:
                    raise ToolError("job not found", 404) from None
                raise
            for item in _items(body):
                lines.append(
                    {
                        "seq": item.get("seq"),
                        "attempt_number": item.get("attempt_number"),
                        "ts": item.get("ts"),
                        "line": _cut(item.get("line"), LOG_LINE_CHARS),
                    }
                )
            if len(lines) > MAX_LOG_LINES:
                dropped = True
                lines = lines[-MAX_LOG_LINES:]
            has_more = bool(body.get("has_more"))
            if not has_more:
                break
            after = body["next_after"]
        return {"lines": lines, "truncated": has_more or dropped}

    def _do_requeue_stalled_jobs(self, _: dict) -> Any:
        return {"requeued": self.api.requeue_stalled()["requeued"]}

    def _do_requeue_failed_jobs(self, args: dict) -> Any:
        body = self.api.requeue_failed(
            args["since"].isoformat(), args["until"].isoformat()
        )
        return {"requeued": body["requeued"]}

    # -- Loki --

    def _do_query_loki(self, args: dict) -> Any:
        now = self.clock()
        query = add_environment(args["query"], self.config.env)
        body = self.loki.query_range(
            query, _ns(now - LOKI_LOOKBACK), _ns(now), MAX_LOKI_ENTRIES
        )
        entries = []
        data = body.get("data") if isinstance(body, dict) else None
        for stream in (data or {}).get("result") or []:
            for ts, line in stream.get("values") or []:
                entries.append((int(ts), line))
        entries.sort(key=lambda e: e[0], reverse=True)
        return {
            "entries": [
                {
                    "ts": datetime.fromtimestamp(ts / 1e9, UTC).isoformat(),
                    "line": line[:LOKI_LINE_CHARS],
                }
                for ts, line in entries[:MAX_LOKI_ENTRIES]
            ]
        }

    # -- Kubernetes tools --

    def _deployment(self, name: str) -> dict:
        assert self.kube is not None
        try:
            return self.kube.get_deployment(name)
        except ToolError as exc:
            if exc.status == 404:
                raise ToolError("deployment not found", 404) from None
            raise

    def _owned_replicasets(self, name: str) -> list[dict]:
        assert self.kube is not None
        owned = []
        for rs in _items(self.kube.list_replicasets()):
            owners = (rs.get("metadata") or {}).get("ownerReferences") or []
            if any(
                o.get("kind") == "Deployment" and o.get("name") == name for o in owners
            ):
                owned.append(rs)
        return owned

    def _do_k8s_get_pods(self, args: dict) -> Any:
        assert self.kube is not None
        deployment = self._deployment(args["app"])
        labels = ((deployment.get("spec") or {}).get("selector") or {}).get(
            "matchLabels"
        )
        if not labels:
            raise ToolError("deployment has no selector")
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        pods = []
        for pod in _items(self.kube.list_pods(selector)):
            status = pod.get("status") or {}
            containers = status.get("containerStatuses") or []
            specs = (pod.get("spec") or {}).get("containers") or []
            pods.append(
                {
                    "name": (pod.get("metadata") or {}).get("name"),
                    "phase": status.get("phase"),
                    "ready": f"{sum(1 for c in containers if c.get('ready'))}"
                    f"/{len(containers) or len(specs)}",
                    "restarts": sum(c.get("restartCount", 0) for c in containers),
                    "started_at": status.get("startTime"),
                    "image_tags": [image_tag(c.get("image") or "") for c in specs],
                }
            )
        return {"pods": pods}

    def _do_k8s_get_events(self, _: dict) -> Any:
        assert self.kube is not None

        def when(event: dict) -> str:
            return (
                event.get("lastTimestamp")
                or event.get("eventTime")
                or event.get("firstTimestamp")
                or (event.get("metadata") or {}).get("creationTimestamp")
                or ""
            )

        events = sorted(_items(self.kube.list_events()), key=when, reverse=True)
        out = []
        for event in events[:MAX_EVENTS]:
            involved = event.get("involvedObject") or {}
            out.append(
                {
                    "time": when(event) or None,
                    "type": event.get("type"),
                    "reason": event.get("reason"),
                    "object": f"{involved.get('kind')}/{involved.get('name')}",
                    "message": _cut(event.get("message"), EVENT_MESSAGE_CHARS),
                    "count": event.get("count"),
                }
            )
        return {"events": out}

    def _do_k8s_rollout_history(self, args: dict) -> Any:
        self._deployment(args["deployment"])
        revisions = []
        for rs in self._owned_replicasets(args["deployment"]):
            revision = _revision(rs)
            if revision is None:
                continue
            template = (rs.get("spec") or {}).get("template") or {}
            revisions.append(
                {
                    "revision": revision,
                    "created_at": (rs.get("metadata") or {}).get("creationTimestamp"),
                    "image_tag": _first_image_tag(template),
                    "change_cause": _annotations(rs).get(CHANGE_CAUSE_ANNOTATION),
                    "replicas": (rs.get("spec") or {}).get("replicas"),
                }
            )
        revisions.sort(key=lambda r: r["revision"], reverse=True)
        return {"revisions": revisions[:MAX_REVISIONS]}

    def _do_k8s_scale(self, args: dict) -> Any:
        assert self.kube is not None
        body = self.kube.scale(args["deployment"], args["replicas"])
        return {
            "deployment": args["deployment"],
            "replicas": (body.get("spec") or {}).get("replicas", args["replicas"]),
        }

    def _do_k8s_rollout_restart(self, args: dict) -> Any:
        assert self.kube is not None
        stamp = self.clock().astimezone(UTC).isoformat()
        self.kube.merge_patch_deployment(
            args["deployment"],
            {
                "spec": {
                    "template": {
                        "metadata": {"annotations": {RESTARTED_AT_ANNOTATION: stamp}}
                    }
                }
            },
        )
        return {"deployment": args["deployment"], "restarted_at": stamp}

    def _do_k8s_rollout_undo(self, args: dict) -> Any:
        assert self.kube is not None
        name = args["deployment"]
        deployment = self._deployment(name)
        sets = [(_revision(rs), rs) for rs in self._owned_replicasets(name)]
        sets = sorted(((r, rs) for r, rs in sets if r is not None), key=lambda p: p[0])
        current = _revision(deployment)
        if current is None and sets:
            current = sets[-1][0]
        previous = [(r, rs) for r, rs in sets if current is not None and r < current]
        if not previous:
            raise ToolError("no previous revision")
        target_revision, target = previous[-1]
        template = copy.deepcopy((target.get("spec") or {}).get("template") or {})
        ((template.get("metadata") or {}).get("labels") or {}).pop(
            POD_TEMPLATE_HASH, None
        )
        self.kube.json_patch_deployment(
            name, [{"op": "replace", "path": "/spec/template", "value": template}]
        )
        return {"deployment": name, "rolled_back_to_revision": target_revision}
