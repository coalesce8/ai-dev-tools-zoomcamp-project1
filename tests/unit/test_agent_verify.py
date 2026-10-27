import json

import pytest
from runwatch.agent.summaries import summarize
from runwatch.agent.tools import ToolResult
from runwatch.agent.verify import (
    REASON_NO_RULE,
    REASON_STILL_TRUE,
    evaluate,
    verify,
)

from tests.unit.agent_loop_support import FakeTime, no_network  # noqa: F401


def stats(**fields):
    body = {
        "counts": {"QUEUED": 0, "RUNNING": 0, "STALLED": 0},
        "oldest_queued_age_seconds": None,
        "attempts_finished": 0,
        "infra_attempt_failure_rate": None,
    }
    body.update(fields)
    return ToolResult(True, json.dumps(body), executed=True)


def state(alertname, **fields):
    return evaluate(alertname, stats(**fields)).state


def test_jobs_stalled_boundary():
    stalled = lambda n: {"counts": {"STALLED": n}}  # noqa: E731
    assert state("JobsStalled", **stalled(0)) == "clear"
    assert state("JobsStalled", **stalled(1)) == "active"
    assert evaluate("JobsStalled", stats(**stalled(0))).summary == "STALLED 0"


def test_queue_wait_boundary():
    age = lambda v: {"oldest_queued_age_seconds": v}  # noqa: E731
    assert state("QueueWaitHigh", **age(None)) == "clear"
    assert state("QueueWaitHigh", **age(120)) == "clear"
    assert state("QueueWaitHigh", **age(120.1)) == "active"
    assert evaluate("QueueWaitHigh", stats(**age(340))).summary == "oldest queued 340 s"


def test_failure_rate_boundaries():
    def rule(attempts, rate):
        return state(
            "JobFailureRateHigh",
            attempts_finished=attempts,
            infra_attempt_failure_rate=rate,
        )

    assert rule(5, 0.5) == "clear"
    assert rule(5, 0.51) == "active"
    assert rule(4, 0.9) == "clear"
    assert rule(5, None) == "clear"


def test_api_down_is_clear_as_soon_as_stats_succeeds():
    assert evaluate("RunwatchApiDown", ToolResult(True, "not json")).clear
    assert evaluate(
        "RunwatchApiDown", ToolResult(True, "x...[truncated]", truncated=True)
    ).clear
    assert evaluate("RunwatchApiDown", ToolResult(False, "{}")).state == "failed"


def test_failed_refused_or_truncated_stats_is_not_clear():
    for result in (
        ToolResult(False, '{"error": "x"}', executed=True),
        ToolResult(False, '{"error": "x"}', refused=True),
        ToolResult(True, '{"counts": {"STAL...[truncated]', truncated=True),
        ToolResult(True, "not json"),
    ):
        assert evaluate("JobsStalled", result).state == "failed"


def test_missing_fields_and_other_alerts_are_unknown():
    assert evaluate("JobsStalled", ToolResult(True, "{}")).state == "unknown"
    assert evaluate("QueueWaitHigh", ToolResult(True, "{}")).state == "unknown"
    assert evaluate("JobFailureRateHigh", ToolResult(True, "{}")).state == "unknown"
    assert evaluate("SomethingElse", stats()).state == "unknown"


def run_verify(alertname, wrote, answers, interval=30, timeout=600):
    """`answers(t)` gives the stats result at fake time t."""
    t = FakeTime()
    polls = []

    def run_stats():
        polls.append(t.now)
        return answers(t.now)

    seen = []
    verdict = verify(
        run_stats,
        alertname,
        wrote,
        interval_s=interval,
        timeout_s=timeout,
        clock=t.clock,
        sleep=t.sleep,
        record=lambda result, ev: seen.append(ev),
    )
    return verdict, polls, seen


STALLED = lambda n: stats(counts={"STALLED": n})  # noqa: E731


def test_no_write_checks_once_immediately():
    verdict, polls, _ = run_verify("JobsStalled", False, lambda t: STALLED(0))
    assert verdict.resolved and polls == [0]
    verdict, polls, _ = run_verify("JobsStalled", False, lambda t: STALLED(2))
    assert not verdict.resolved and verdict.reason == REASON_STILL_TRUE
    assert polls == [0]


def test_after_a_write_polls_every_interval_until_clear():
    verdict, polls, seen = run_verify(
        "JobsStalled", True, lambda t: STALLED(0 if t >= 90 else 3)
    )
    assert verdict.resolved
    assert polls == [30, 60, 90]
    assert [e.clear for e in seen] == [False, False, True]


def test_polling_stops_at_the_timeout_with_the_last_poll_at_600():
    verdict, polls, _ = run_verify("JobsStalled", True, lambda t: STALLED(1))
    assert not verdict.resolved and verdict.reason == REASON_STILL_TRUE
    assert polls[0] == 30 and polls[-1] == 600 and len(polls) == 20


def test_a_failed_stats_call_keeps_polling():
    def answers(t):
        if t < 60:
            return ToolResult(False, '{"error": "x"}', executed=True)
        return STALLED(0)

    verdict, polls, _ = run_verify("JobsStalled", True, answers)
    assert verdict.resolved and polls == [30, 60]


def test_unverifiable_alert_escalates_without_polling():
    verdict, polls, _ = run_verify("SomethingElse", True, lambda t: stats())
    assert not verdict.resolved and verdict.reason == REASON_NO_RULE
    assert polls == []


def test_body_without_the_fields_ends_with_no_rule():
    verdict, polls, _ = run_verify(
        "JobsStalled", True, lambda t: ToolResult(True, "{}")
    )
    assert verdict.reason == REASON_NO_RULE and polls == [30]


@pytest.mark.parametrize(
    ("name", "args", "body", "expected"),
    [
        ("requeue_stalled_jobs", {}, {"requeued": 3}, "3 jobs requeued"),
        ("k8s_scale", {"deployment": "worker"}, {"replicas": 1}, "scaled worker to 1"),
        (
            "k8s_get_pods",
            {},
            {"pods": [{"ready": "1/1"}, {"ready": "0/1"}]},
            "2 pods, 1 ready",
        ),
        ("k8s_get_pods", {}, {"pods": []}, "0 pods, 0 ready"),
        (
            "k8s_rollout_history",
            {},
            {
                "revisions": [
                    {
                        "revision": 5,
                        "created_at": "2026-03-01T12:03:11Z",
                        "image_tag": "abc123",
                    }
                ]
            },
            "revision 5 created 12:03:11Z, image tag abc123",
        ),
        (
            "k8s_rollout_undo",
            {"deployment": "worker"},
            {"rolled_back_to_revision": 4},
            "rolled back worker to revision 4",
        ),
        (
            "get_stats",
            {},
            {"counts": {"QUEUED": 1, "STALLED": 2}},
            "QUEUED 1, STALLED 2",
        ),
    ],
)
def test_summary_templates(name, args, body, expected):
    result = ToolResult(True, json.dumps(body), executed=True)
    assert summarize(name, args, result) == expected


def test_summary_of_refusal_failure_and_unparsed_results():
    assert (
        summarize("k8s_scale", {}, ToolResult(False, "{}", refused=True))
        == "refused by tool rules"
    )
    failed = ToolResult(
        False, '{"error": "runwatch API returned 503: SECRET"}', executed=True
    )
    assert summarize("get_stats", {}, failed) == "failed: API returned 503"
    assert summarize("get_stats", {}, ToolResult(True, "nope")) == "result not parsed"
    cut = ToolResult(True, '{"jobs": [{"id": "x"...[truncated]', truncated=True)
    assert summarize("list_jobs", {}, cut) == "result not parsed"
