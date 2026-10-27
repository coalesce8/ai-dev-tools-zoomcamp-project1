"""The responder's tool-use loop.

Hand-written, so the call and write budgets, the data wrapping, the evidence
recording and the handling of refusals sit between each tool call. The loop
depends on the `ModelClient` protocol and the `ToolRunner`, nothing else.

Evidence goes to the incident directory as it is gathered: `context.md` (the
loop's own notes, never copied into the report), `actions.jsonl` and
`summary.md` (the contract in `docs/incident-files.md`).
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runwatch.agent.incidents import Incident, atomic_write
from runwatch.agent.model import ModelApiError, ModelClient, ModelResponse
from runwatch.agent.prompt import build_system_prompt, new_nonce, wrap_data
from runwatch.agent.runs import OUTCOME_ESCALATED
from runwatch.agent.summaries import clean_args, summarize
from runwatch.agent.tools import ToolResult, ToolRunner, is_write
from runwatch.agent.verify import Evaluation, verify

logger = logging.getLogger(__name__)

OUTCOME_RESOLVED = "resolved"
MAX_RESPONSE_TOKENS = 4096
API_FAILURES_IN_A_ROW = 3
SUMMARY_CHARS = 8000
ARGS_CONTEXT_CHARS = 4000
_CACHE = {"type": "ephemeral"}
_NAME = re.compile(r"[^A-Za-z0-9_-]")
_STOP = re.compile(r"[^a-z_]")

REASON_TOOL_LIMIT = "tool-call limit"
REASON_WRITE_LIMIT = "write limit"
REASON_TOKENS = "token budget"
REASON_API = "model API failed 3 times in a row"


@dataclass(frozen=True)
class Limits:
    max_tool_calls: int = 20
    max_writes: int = 3
    max_tokens: int = 200_000


@dataclass
class _Run:
    incident: Incident
    nonce: str
    calls: int = 0
    writes: int = 0
    tokens: int = 0


class _Stop(Exception):
    """The run ends here with this outcome and reason."""

    def __init__(self, outcome: str, reason: str) -> None:
        self.outcome = outcome
        self.reason = reason


def fence(text: str) -> str:
    """`text` in a Markdown fence longer than any run of backticks inside it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text}\n{ticks}\n"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_name(name: Any) -> str:
    return _NAME.sub("_", name)[:64] if isinstance(name, str) and name else "unknown"


def _with_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A copy of the history with one cache breakpoint, on the last block."""
    out = copy.deepcopy(messages)
    for message in out:
        for block in message["content"]:
            block.pop("cache_control", None)
    out[-1]["content"][-1]["cache_control"] = dict(_CACHE)
    return out


class ClaudeResponder:
    """Implements the `Responder` protocol of `runs.py`."""

    def __init__(
        self,
        client: ModelClient,
        runner: ToolRunner,
        *,
        model: str,
        retry_delay_s: float = 2,
        verify_interval_s: float = 30,
        verify_timeout_s: float = 600,
        limits: Limits | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        nonce_factory: Callable[[], str] = new_nonce,
    ) -> None:
        self.client = client
        self.runner = runner
        self.model = model
        self.retry_delay_s = retry_delay_s
        self.verify_interval_s = verify_interval_s
        self.verify_timeout_s = verify_timeout_s
        self.limits = limits or Limits()
        self.sleep = sleep
        self.clock = clock
        self.nonce_factory = nonce_factory

    # -- entry point --

    def respond(self, incident: Incident) -> str:
        run = _Run(incident, self.nonce_factory())
        try:
            outcome, reason = self._drive(run)
        except _Stop as stop:
            outcome, reason = stop.outcome, stop.reason
        self._context(
            run, f"\n## Outcome\n\n- outcome: {outcome}\n- reason: {reason}\n"
        )
        logger.info(
            "responder finished",
            extra={
                "incident_id": incident.incident_id,
                "fingerprint": incident.fingerprint,
                "alertname": incident.alertname,
                "outcome": outcome,
                "reason": reason,
                "tool_calls": run.calls,
                "writes": run.writes,
                "tokens": run.tokens,
            },
        )
        return outcome

    # -- the turn loop --

    def _drive(self, run: _Run) -> tuple[str, str]:
        kube = self.runner.kube_enabled
        system = [
            {
                "type": "text",
                "text": build_system_prompt(
                    run.nonce,
                    kube,
                    self.limits.max_tool_calls,
                    self.limits.max_writes,
                ),
                "cache_control": dict(_CACHE),
            }
        ]
        tools = self.runner.definitions()
        alert = wrap_data(
            run.nonce, "alert", json.dumps(run.incident.alert, ensure_ascii=True)
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "A Runwatch alert fired. Its details are in the "
                        "data block below. Diagnose it and fix it.\n\n" + alert,
                    }
                ],
            }
        ]
        failures = 0
        while True:
            response, failures = self._ask(run, system, tools, messages, failures)
            run.tokens = response.usage.total
            if run.tokens >= self.limits.max_tokens:
                raise _Stop(OUTCOME_ESCALATED, REASON_TOKENS)
            text = "\n".join(
                b["text"] for b in response.content if b.get("type") == "text"
            ).strip()
            if text:
                self._context(run, "\n## Model notes\n\n" + fence(text))
            messages.append({"role": "assistant", "content": response.content})
            stop = response.stop_reason
            if stop == "end_turn":
                return self._finish(run, text)
            if stop != "tool_use":
                name = _STOP.sub("_", str(stop))[:40]
                raise _Stop(OUTCOME_ESCALATED, f"unexpected stop reason {name}")
            uses = [b for b in response.content if b.get("type") == "tool_use"]
            if not uses:
                raise _Stop(OUTCOME_ESCALATED, "unexpected stop reason tool_use")
            messages.append({"role": "user", "content": self._run_tools(run, uses)})
            if run.calls >= self.limits.max_tool_calls:
                raise _Stop(OUTCOME_ESCALATED, REASON_TOOL_LIMIT)

    def _ask(
        self,
        run: _Run,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        failures: int,
    ) -> tuple[ModelResponse, int]:
        while True:
            try:
                response = self.client.create(
                    model=self.model,
                    system=system,
                    tools=tools,
                    messages=_with_breakpoint(messages),
                    max_tokens=MAX_RESPONSE_TOKENS,
                )
                return response, 0
            except ModelApiError as exc:
                failures += 1
                logger.warning(
                    "model request failed",
                    extra={
                        "incident_id": run.incident.incident_id,
                        "kind": exc.kind,
                        "status": exc.status,
                    },
                )
                if failures >= API_FAILURES_IN_A_ROW:
                    raise _Stop(OUTCOME_ESCALATED, REASON_API) from None
                self.sleep(self.retry_delay_s)

    # -- tools --

    def _run_tools(self, run: _Run, uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for block in uses:
            name = block.get("name")
            args = block.get("input")
            if run.calls >= self.limits.max_tool_calls:
                raise _Stop(OUTCOME_ESCALATED, REASON_TOOL_LIMIT)
            run.calls += 1
            write = False
            if isinstance(name, str):
                try:
                    write = is_write(name)
                except KeyError:  # an unknown tool is a non-write call
                    write = False
            if write and run.writes >= self.limits.max_writes:
                self._record(
                    run,
                    name,
                    args,
                    ToolResult(False, "not executed: write limit"),
                    write,
                    note="not executed: write limit",
                )
                raise _Stop(OUTCOME_ESCALATED, REASON_WRITE_LIMIT)
            result = self.runner.run(name, args)
            if write and result.executed:
                run.writes += 1
            self._record(run, name, args, result, write)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": wrap_data(run.nonce, _safe_name(name), result.content),
                    "is_error": not result.ok,
                }
            )
        return results

    def _record(
        self,
        run: _Run,
        name: Any,
        args: Any,
        result: ToolResult,
        write: bool,
        note: str = "",
    ) -> None:
        tool = _safe_name(name)
        shown = clean_args(tool, args)
        summary = note or summarize(tool, shown, result)
        status = "ok" if result.ok else "refused" if result.refused else "failed"
        self._action(
            run,
            {
                "ts": _now(),
                "kind": "tool",
                "tool": tool,
                "args": shown,
                "write": write,
                "ok": result.ok,
                "summary": summary,
            },
        )
        try:
            raw_args = json.dumps(args, ensure_ascii=True, default=str)
        except (TypeError, ValueError):
            raw_args = "null"
        self._context(
            run,
            f"\n## Tool call {run.calls}: {tool}\n\n"
            f"- status: {status}\n- arguments:\n\n"
            + fence(raw_args[:ARGS_CONTEXT_CHARS])
            + "\n- result:\n\n"
            + fence(result.content),
        )
        logger.info(
            "tool call finished",
            extra={
                "incident_id": run.incident.incident_id,
                "tool": tool,
                "ok": result.ok,
                "refused": result.refused,
                "tool_calls": run.calls,
                "writes": run.writes,
            },
        )

    # -- the end of the run --

    def _finish(self, run: _Run, text: str) -> tuple[str, str]:
        if text:
            atomic_write(run.incident.path / "summary.md", text[:SUMMARY_CHARS] + "\n")

        def record(result: ToolResult, evaluation: Evaluation) -> None:
            self._action(
                run,
                {
                    "ts": _now(),
                    "kind": "verification",
                    "tool": "get_stats",
                    "args": {},
                    "write": False,
                    "ok": evaluation.clear,
                    "summary": evaluation.summary[:200],
                },
            )
            self._context(
                run,
                f"\n## Verification\n\n- condition: {evaluation.state}\n"
                f"- {evaluation.summary}\n",
            )

        verdict = verify(
            lambda: self.runner.run("get_stats", {}),
            run.incident.alertname,
            run.writes > 0,
            interval_s=self.verify_interval_s,
            timeout_s=self.verify_timeout_s,
            clock=self.clock,
            sleep=self.sleep,
            record=record,
        )
        return (
            OUTCOME_RESOLVED if verdict.resolved else OUTCOME_ESCALATED,
            verdict.reason,
        )

    # -- evidence files --

    def _context(self, run: _Run, text: str) -> None:
        with run.incident.context_file.open("a") as handle:
            handle.write(text)

    def _action(self, run: _Run, entry: dict[str, Any]) -> None:
        path: Path = run.incident.path / "actions.jsonl"
        with path.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
