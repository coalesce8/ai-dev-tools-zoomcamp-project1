"""One-line summaries of tool results for `actions.jsonl`.

Built from the parsed result by a per-tool template. They hold counts, ids and
fixed words only and never copy free text from a result.
"""

from __future__ import annotations

import json
import re
from typing import Any

from runwatch.agent.tools import ToolResult

MAX_SUMMARY_CHARS = 200
REFUSED_SUMMARY = "refused by tool rules"
NOT_PARSED = "result not parsed"
ARG_KEYS = ("deployment", "replicas", "since", "until", "state", "app", "id")

_SAFE_ARG = re.compile(r"[A-Za-z0-9_.:+\-]{1,64}")
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?")
_TAG = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_STATUS = re.compile(r"returned (\d{3})")
_DEPLOYMENTS = ("worker", "api")


def clean_args(name: str, args: Any) -> dict[str, Any]:
    """The whitelisted arguments of a call; LogQL is reduced to its length."""
    if not isinstance(args, dict):
        return {}
    if name == "query_loki":
        query = args.get("query")
        return {"query_chars": len(query) if isinstance(query, str) else 0}
    out: dict[str, Any] = {}
    for key in ARG_KEYS:
        value = args.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and abs(value) < 10**6:
            out[key] = value
        elif isinstance(value, str) and _SAFE_ARG.fullmatch(value):
            out[key] = value
    return out


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _list(data: Any, key: str) -> list | None:
    items = data.get(key) if isinstance(data, dict) else None
    return items if isinstance(items, list) else None


def _deployment(args: dict) -> str:
    value = args.get("deployment")
    return value if value in _DEPLOYMENTS else "deployment"


def _ready(pod: Any) -> bool:
    text = pod.get("ready") if isinstance(pod, dict) else None
    match = re.fullmatch(r"(\d+)/(\d+)", text) if isinstance(text, str) else None
    return bool(match) and match[1] == match[2] and match[2] != "0"


def _stats(data: Any, _: dict) -> str | None:
    counts = data.get("counts") if isinstance(data, dict) else None
    if not isinstance(counts, dict):
        return None
    parts = [
        f"{state} {n}"
        for state in ("QUEUED", "RUNNING", "STALLED")
        if (n := _int(counts.get(state))) is not None
    ]
    return ", ".join(parts) or None


def _jobs(data: Any, _: dict) -> str | None:
    jobs = _list(data, "jobs")
    return None if jobs is None else f"{len(jobs)} jobs listed"


def _logs(data: Any, _: dict) -> str | None:
    lines = _list(data, "lines")
    return None if lines is None else f"{len(lines)} log lines"


def _pods(data: Any, _: dict) -> str | None:
    pods = _list(data, "pods")
    if pods is None:
        return None
    return f"{len(pods)} pods, {sum(1 for p in pods if _ready(p))} ready"


def _events(data: Any, _: dict) -> str | None:
    events = _list(data, "events")
    return None if events is None else f"{len(events)} events"


def _history(data: Any, _: dict) -> str | None:
    revisions = _list(data, "revisions")
    if revisions is None:
        return None
    if not revisions or not isinstance(revisions[0], dict):
        return f"{len(revisions)} revisions"
    top = revisions[0]
    number = _int(top.get("revision"))
    if number is None:
        return f"{len(revisions)} revisions"
    text = f"revision {number}"
    created = top.get("created_at")
    if isinstance(created, str) and _TIME.fullmatch(created):
        text += f" created {created[11:19]}Z" if created[10] == "T" else ""
    tag = top.get("image_tag")
    if isinstance(tag, str) and _TAG.fullmatch(tag):
        text += f", image tag {tag}"
    return text


def _loki(data: Any, _: dict) -> str | None:
    entries = _list(data, "entries")
    return None if entries is None else f"{len(entries)} log entries"


def _scale(data: Any, args: dict) -> str | None:
    replicas = _int(data.get("replicas")) if isinstance(data, dict) else None
    return None if replicas is None else f"scaled {_deployment(args)} to {replicas}"


def _restart(data: Any, args: dict) -> str | None:
    return f"restarted {_deployment(args)}" if isinstance(data, dict) else None


def _undo(data: Any, args: dict) -> str | None:
    revision = (
        _int(data.get("rolled_back_to_revision")) if isinstance(data, dict) else None
    )
    if revision is None:
        return None
    return f"rolled back {_deployment(args)} to revision {revision}"


def _requeued(data: Any, _: dict) -> str | None:
    count = _int(data.get("requeued")) if isinstance(data, dict) else None
    return None if count is None else f"{count} jobs requeued"


_TEMPLATES = {
    "get_stats": _stats,
    "list_jobs": _jobs,
    "get_job_logs": _logs,
    "k8s_get_pods": _pods,
    "k8s_get_events": _events,
    "k8s_rollout_history": _history,
    "query_loki": _loki,
    "k8s_scale": _scale,
    "k8s_rollout_restart": _restart,
    "k8s_rollout_undo": _undo,
    "requeue_stalled_jobs": _requeued,
    "requeue_failed_jobs": _requeued,
}


def parse(result: ToolResult) -> Any:
    """The JSON body of a result, or None. A truncated result is text."""
    if result.truncated:
        return None
    try:
        return json.loads(result.content)
    except ValueError:
        return None


def summarize(name: str, args: dict[str, Any], result: ToolResult) -> str:
    if result.refused:
        return REFUSED_SUMMARY
    if not result.ok:
        match = _STATUS.search(result.content)
        return f"failed: API returned {match[1]}" if match else "failed: tool error"
    template = _TEMPLATES.get(name)
    text = template(parse(result), args) if template else None
    return (text or NOT_PARSED)[:MAX_SUMMARY_CHARS]
