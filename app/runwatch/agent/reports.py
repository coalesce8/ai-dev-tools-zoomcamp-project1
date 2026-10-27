"""Incident reports: render, redact and write `response.md`.

The report is built from structured fields only (ids, counts, states,
timestamps, deployment names, alert labels and annotations). It never embeds
`context.md`, tool results, job names or log lines, because it is copied into
the repository and may be opened as a public GitHub issue. Rendering is pure:
it reads the files of one incident directory and nothing else.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runwatch.agent.incidents import IncidentStore

logger = logging.getLogger(__name__)

REVERT_NOTE = (
    "The cluster now differs from `main`: `main` needs a revert PR before the "
    "next deploy, because CD would otherwise roll the bad release out again."
)
REFUSED_SUMMARY = "refused by tool rules"
SUMMARY_LIMIT = 2000
NONE_RECORDED = "None recorded."

# Argument keys that may reach the report, in display order.
ARG_KEYS = ("deployment", "replicas", "since", "until", "state", "app", "id")
_SAFE_ARG = re.compile(r"[A-Za-z0-9_.:+/=-]{1,64}")

_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"gh[pous]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"[a-z+]+://[^\s:@/]+:[^\s@/]+@\S+"),
)

_HUMAN_CHECK = {
    "resolved": (
        "The alert condition was clear in a verification poll. Confirm the alert "
        "is resolved and the queue is draining."
    ),
    "escalated": (
        "The alert condition may still be true. Look at the alert, the "
        "dashboard and the agent log."
    ),
    "skipped_no_model": (
        "No `ANTHROPIC_API_KEY` was set, so nobody handled this alert. Handle "
        "it by hand."
    ),
}
_HUMAN_CHECK_INTERRUPTED = (
    "The run was interrupted by an agent restart. The alert condition may still "
    "be true: look at the alert, the dashboard and the agent log."
)
_HUMAN_CHECK_OTHER = (
    "Check the alert, the dashboard and the agent log for what the run left behind."
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace secret values and secret-shaped strings with `[redacted]`."""
    for value in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _line(value: Any, limit: int = 200) -> str:
    """One-line, bounded text for a value that came from outside."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> tuple[list[Any], int]:
    """Parsed lines and the number of lines that were not JSON."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return [], 0
    rows, bad = [], 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except ValueError:
            bad += 1
    return rows, bad


def _read_actions(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows, unreadable = _read_jsonl(path)
    entries = []
    for row in rows:
        moment = _parse(row.get("ts")) if isinstance(row, dict) else None
        if moment is None or not isinstance(row.get("tool"), str) or not row["tool"]:
            unreadable += 1
            continue
        entries.append(
            {
                "at": moment,
                "verification": row.get("kind") == "verification",
                "tool": _line(row["tool"], 64),
                "args": row.get("args") if isinstance(row.get("args"), dict) else {},
                "write": row.get("write") is True,
                "ok": row.get("ok") is True,
                "summary": _line(row.get("summary", ""), 200),
            }
        )
    return entries, unreadable


def _args(args: dict[str, Any]) -> str:
    parts = []
    chars = args.get("query_chars")
    if isinstance(chars, int) and not isinstance(chars, bool) and chars >= 0:
        parts.append(f"query, {chars} chars")
    for key in ARG_KEYS:
        value = args.get(key)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            continue
        text = str(value)
        parts.append(f"{key}={text if _SAFE_ARG.fullmatch(text) else '(omitted)'}")
    return ", ".join(parts)


def _result(entry: dict[str, Any]) -> str:
    if entry["ok"]:
        return "ok"
    return "refused" if entry["summary"] == REFUSED_SUMMARY else "failed"


def _call(entry: dict[str, Any]) -> str:
    args = _args(entry["args"])
    return f"`{entry['tool']}`" + (f" ({args})" if args else "")


def _entry_line(entry: dict[str, Any]) -> str:
    text = f"- {_call(entry)}: {_result(entry)}"
    return text + (f" - {entry['summary']}" if entry["summary"] else "")


def _duration(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _section(title: str, lines: list[str]) -> list[str]:
    return [f"## {title}", "", *(lines or [NONE_RECORDED]), ""]


def render_report(incident_dir: str | Path, now: datetime) -> str:
    """Render the Markdown report of one incident directory.

    Raises ValueError when `status.json` is unusable. Everything else a bad
    file can do is tolerated.
    """
    directory = Path(incident_dir)
    status = _read_json(directory / "status.json")
    started = _parse(status.get("started_at")) if isinstance(status, dict) else None
    if started is None:
        raise ValueError("status.json unreadable")
    ended = _parse(status.get("ended_at"))
    running = status.get("state") == "running"
    outcome = status.get("outcome") if isinstance(status.get("outcome"), str) else None
    incident_id = _line(status.get("incident_id") or directory.name, 100)
    alert = _read_json(directory / "alert.json")
    alert = alert if isinstance(alert, dict) else {}
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    notes = (
        alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    )
    actions, unreadable = _read_actions(directory / "actions.jsonl")
    hooks, _ = _read_jsonl(directory / "webhooks.jsonl")
    hooks = [
        (h, t)
        for h in hooks
        if isinstance(h, dict) and (t := _parse(h.get("received_at"))) is not None
    ]
    reverted = any(
        a["tool"] == "k8s_rollout_undo" and a["ok"] and not a["verification"]
        for a in actions
    )
    verifications = [a for a in actions if a["verification"]]
    tools = [a for a in actions if not a["verification"]]

    out = [f"# Incident {incident_id}", ""]
    if running:
        out += [
            f"> Partial report: the run is still in progress (rendered {_stamp(now)}).",
            "",
        ]

    end_time = ended or (now if running else None)
    out += _section(
        "Summary",
        [
            f"- alertname: {_line(status.get('alertname'), 100)}",
            f"- env: {_line(status.get('env'), 100)}",
            f"- fingerprint: {_line(status.get('fingerprint'), 64)}",
            f"- started: {_stamp(started)}",
            f"- ended: {_stamp(ended) if ended else 'not yet'}",
            "- duration: "
            + (_duration(started, end_time) if end_time else "unknown")
            + (" so far" if running else ""),
            f"- state: {_line(status.get('state'), 32)}",
            f"- outcome: {_line(outcome, 64) if outcome else 'In progress'}",
        ],
    )

    name = labels.get("alertname") or status.get("alertname")
    label_text = ", ".join(
        f"{_line(k, 40)}={_line(v, 100)}" for k, v in sorted(labels.items())[:20]
    )
    out += _section(
        "Alert",
        [
            f"- alertname: {_line(name, 100)}",
            f"- severity: {_line(labels.get('severity', 'unknown'), 40)}",
            f"- summary: {_line(notes.get('summary', ''), 300)}",
            f"- description: {_line(notes.get('description', ''), 300)}",
            f"- startsAt: {_line(alert.get('startsAt', 'unknown'), 40)}",
            f"- labels: {label_text or 'none'}",
        ],
    )

    timeline: list[tuple[datetime, int, str]] = [
        (started, 0, f"first firing: {_line(status.get('alertname'), 100)}")
    ]
    for hook, moment in hooks:
        kind = _line(hook.get("kind", "?"), 20)
        disposition = _line(hook.get("disposition", "?"), 40)
        timeline.append((moment, 1, f"webhook {kind}: {disposition}"))
    for entry in actions:
        if entry["verification"]:
            seen = "condition cleared" if entry["ok"] else "condition still true"
            text = f"verification: {seen}"
        else:
            text = f"{_call(entry)}: {_result(entry)}"
        timeline.append((entry["at"], 2, text))
    if ended:
        timeline.append((ended, 3, f"run ended: {_line(outcome or 'unknown', 64)}"))
    timeline.sort(key=lambda row: (row[0], row[1]))
    out += _section("Timeline", [f"- {_stamp(t)} {text}" for t, _, text in timeline])

    evidence = _section("Evidence", [_entry_line(a) for a in tools if not a["write"]])
    if unreadable:
        evidence[-1:] = ["", f"{unreadable} unreadable entries in the action log.", ""]
    out += evidence
    action_lines = [_entry_line(a) for a in tools if a["write"]]
    if reverted:
        action_lines += ["", REVERT_NOTE]
    out += _section("Actions taken", action_lines)

    outcome_lines = [f"- outcome: {_line(outcome, 64)}" if outcome else "In progress"]
    if verifications:
        last = verifications[-1]
        seen = "condition cleared" if last["ok"] else "condition still true"
        outcome_lines.append(f"- last verification: {seen} at {_stamp(last['at'])}")
    if running:
        outcome_lines.append(
            "- This report is partial and is rewritten when the run ends."
        )
    summary_text = _agent_summary(directory / "summary.md")
    if summary_text:
        outcome_lines += ["", "Agent summary (model-written, unverified):", ""]
        outcome_lines += [
            f"> {row}" if row else ">" for row in summary_text.splitlines()
        ]
    out += _section("Outcome", outcome_lines)

    if running:
        check = "The run is in progress. Check again when it has ended."
    elif status.get("interrupted") is True:
        check = _HUMAN_CHECK_INTERRUPTED
    else:
        check = _HUMAN_CHECK.get(outcome or "", _HUMAN_CHECK_OTHER)
    check_lines = [check]
    if reverted:
        check_lines += ["", REVERT_NOTE]
    out += _section("What a human should check", check_lines)

    count = status.get("webhook_count")
    counts = Counter(
        _line(h.get("disposition", "?"), 40)
        for h, _ in hooks
        if h.get("disposition") != "started"
    )
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out += _section(
        "Repeated webhooks",
        [
            f"- webhook_count: {count if isinstance(count, int) else 'unknown'}",
            "- dispositions: "
            + (", ".join(f"{n} {d}" for d, n in ordered) if ordered else "none"),
        ],
    )
    return "\n".join(out).rstrip("\n") + "\n"


def _agent_summary(path: Path) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return text.strip()[:SUMMARY_LIMIT].rstrip()


def fallback_report(incident_id: str) -> str:
    return f"# Incident {incident_id}\n\nIncident report: report generation failed.\n"


def write_report(
    store: IncidentStore,
    incident_id: str,
    now: datetime,
    secrets: Iterable[str] = (),
) -> None:
    """Render, redact and atomically write `response.md`; never raises on render."""
    try:
        text = redact(render_report(store.path(incident_id), now), secrets)
    except Exception as exc:
        logger.error(
            "report generation failed",
            extra={"incident_id": incident_id, "exc_type": type(exc).__name__},
        )
        text = fallback_report(incident_id)
    try:
        store.write_response(incident_id, text)
    except OSError as exc:
        logger.error(
            "report write failed",
            extra={"incident_id": incident_id, "exc_type": type(exc).__name__},
        )
