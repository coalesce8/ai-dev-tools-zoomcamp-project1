"""Checking the alert condition after the model is done.

The condition is read from `get_stats` through the tool runner, not asked of the
model. Each rule mirrors its alert expression. Time is injected so tests never
sleep.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from runwatch.agent.summaries import NOT_PARSED, parse
from runwatch.agent.tools import ToolResult

QUEUE_WAIT_SECONDS = 120
FAILURE_RATE = 0.5
MIN_ATTEMPTS = 5

REASON_NO_RULE = "no verification rule for this alert"
REASON_STILL_TRUE = "condition still true after verification"

State = Literal["clear", "active", "failed", "unknown"]


@dataclass(frozen=True)
class Evaluation:
    state: State
    summary: str

    @property
    def clear(self) -> bool:
        return self.state == "clear"


@dataclass(frozen=True)
class Verdict:
    resolved: bool
    reason: str


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _jobs_stalled(body: dict) -> Evaluation:
    counts = body.get("counts")
    stalled = counts.get("STALLED") if isinstance(counts, dict) else None
    if isinstance(stalled, bool) or not isinstance(stalled, int):
        return Evaluation("unknown", NOT_PARSED)
    return Evaluation("active" if stalled > 0 else "clear", f"STALLED {stalled}")


def _queue_wait(body: dict) -> Evaluation:
    if "oldest_queued_age_seconds" not in body:
        return Evaluation("unknown", NOT_PARSED)
    age = body["oldest_queued_age_seconds"]
    if age is None:
        return Evaluation("clear", "oldest queued none")
    value = _number(age)
    if value is None:
        return Evaluation("unknown", NOT_PARSED)
    text = f"oldest queued {value:g} s"
    return Evaluation("active" if value > QUEUE_WAIT_SECONDS else "clear", text)


def _failure_rate(body: dict) -> Evaluation:
    attempts = body.get("attempts_finished")
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        return Evaluation("unknown", NOT_PARSED)
    if "infra_attempt_failure_rate" not in body:
        return Evaluation("unknown", NOT_PARSED)
    rate = body["infra_attempt_failure_rate"]
    value = _number(rate)
    if rate is not None and value is None:
        return Evaluation("unknown", NOT_PARSED)
    text = f"{attempts} attempts, infra failure rate " + (
        "none" if value is None else f"{value:g}"
    )
    active = attempts >= MIN_ATTEMPTS and value is not None and value > FAILURE_RATE
    return Evaluation("active" if active else "clear", text)


def _api_up(_: dict) -> Evaluation:
    return Evaluation("clear", "stats read")


RULES: dict[str, Callable[[dict], Evaluation]] = {
    "JobsStalled": _jobs_stalled,
    "QueueWaitHigh": _queue_wait,
    "JobFailureRateHigh": _failure_rate,
    "RunwatchApiDown": _api_up,
}


def evaluate(alertname: str, result: ToolResult) -> Evaluation:
    """Judge one `get_stats` result against the rule of `alertname`."""
    rule = RULES.get(alertname)
    if rule is None:
        return Evaluation("unknown", "no verification rule")
    if not result.ok:
        return Evaluation("failed", "get_stats failed")
    if alertname == "RunwatchApiDown":
        return _api_up({})
    body = parse(result)
    if not isinstance(body, dict):
        # Truncated or not JSON: cannot say it is clear.
        return Evaluation("failed", NOT_PARSED)
    return rule(body)


def verify(
    run_stats: Callable[[], ToolResult],
    alertname: str,
    wrote: bool,
    *,
    interval_s: float,
    timeout_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    record: Callable[[ToolResult, Evaluation], None],
) -> Verdict:
    """Poll the condition. `record` sees every poll."""
    if alertname not in RULES:
        return Verdict(False, REASON_NO_RULE)

    def poll() -> Verdict | None:
        result = run_stats()
        evaluation = evaluate(alertname, result)
        record(result, evaluation)
        if evaluation.clear:
            return Verdict(True, "alert condition clear")
        if evaluation.state == "unknown":
            return Verdict(False, REASON_NO_RULE)
        return None

    if not wrote:
        return poll() or Verdict(False, REASON_STILL_TRUE)
    start = clock()
    while True:
        # The next poll must fall at or before the timeout.
        if clock() - start + interval_s > timeout_s:
            return Verdict(False, REASON_STILL_TRUE)
        sleep(interval_s)
        verdict = poll()
        if verdict is not None:
            return verdict
