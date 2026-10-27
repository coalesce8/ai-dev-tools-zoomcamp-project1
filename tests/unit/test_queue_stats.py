"""Pure parts of the stats feature: rate() and the StatsOut model."""

from datetime import UTC, datetime

import pytest
from runwatch.api.stats import StatsOut
from runwatch.queue_stats import WINDOW_SECONDS, rate

KEYS = {
    "as_of", "window_seconds", "counts", "oldest_queued_age_seconds",
    "jobs_finished", "jobs_failed", "job_failure_rate", "attempts_finished",
    "attempts_failed_infra", "infra_attempt_failure_rate",
}  # fmt: skip


def test_rate_zero_denominator_is_none():
    assert rate(0, 0) is None


def test_rate_divides():
    assert rate(2, 5) == 0.4
    assert rate(0, 3) == 0.0
    assert rate(3, 3) == 1.0


@pytest.mark.parametrize("args", [(-1, 5), (6, 5), (1, 0), (-1, 0)])
def test_rate_invalid(args):
    with pytest.raises(ValueError):
        rate(*args)


def test_stats_out_keeps_null_rates():
    out = StatsOut(
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        window_seconds=WINDOW_SECONDS,
        counts={"QUEUED": 0},
        oldest_queued_age_seconds=None,
        jobs_finished=0,
        jobs_failed=0,
        job_failure_rate=None,
        attempts_finished=0,
        attempts_failed_infra=0,
        infra_attempt_failure_rate=None,
    )
    data = out.model_dump(mode="json")
    assert set(data) == KEYS
    assert data["job_failure_rate"] is None
    assert data["infra_attempt_failure_rate"] is None
    assert data["oldest_queued_age_seconds"] is None
