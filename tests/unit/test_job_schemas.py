"""Job schemas, without a database."""

import pytest
from pydantic import ValidationError
from runwatch.api.job_schemas import JobCreate, JobOut, WorkloadSpec


def _body(**over):
    body = {"name": "n", "spec": {"duration_s": 30}}
    body.update(over)
    return body


def test_defaults():
    job = JobCreate.model_validate(_body())
    assert job.max_attempts == 3
    assert job.spec.model_dump(mode="json") == {
        "duration_s": 30,
        "fail_probability": 0.0,
        "mode": "normal",
    }


@pytest.mark.parametrize("name", ["a", "a" * 100, " a b ", "Modèle-🌊", "🌊" * 100])
def test_valid_names_unchanged(name):
    assert JobCreate.model_validate(_body(name=name)).name == name


@pytest.mark.parametrize("d", [1, 600])
def test_duration_bounds(d):
    assert WorkloadSpec(duration_s=d).duration_s == d


@pytest.mark.parametrize("p", [0, 0.0, 1, 1.0, 0.5])
def test_probability_valid(p):
    spec = WorkloadSpec(duration_s=1, fail_probability=p)
    assert isinstance(spec.fail_probability, float)
    assert spec.fail_probability == p


@pytest.mark.parametrize("m", [1, 10])
def test_max_attempts_valid(m):
    assert JobCreate.model_validate(_body(max_attempts=m)).max_attempts == m


def test_always_fail_mode():
    spec = WorkloadSpec.model_validate({"duration_s": 1, "mode": "always_fail"})
    assert spec.mode == "always_fail"


INVALID = [
    {"name": None},
    {"name": ""},
    {"name": "   "},
    {"name": "a" * 101},
    {"name": 123},
    {"name": "a\nb"},
    {"name": "a\tb"},
    {"name": "a\x00b"},
    {"name": "a​b"},
    {"spec": None},
    {"spec": "x"},
    {"spec": []},
    {"spec": {}},
    *({"spec": {"duration_s": v}} for v in [0, 601, -1, 30.5, 30.0, "30", True, None]),
    *(
        {"spec": {"duration_s": 5, "fail_probability": v}}
        for v in [-0.01, 1.01, "0.5", True, None, float("nan"), float("inf")]
    ),
    *({"spec": {"duration_s": 5, "mode": v}} for v in ["bogus", "NORMAL", "", None]),
    *({"max_attempts": v} for v in [0, 11, 2.5, "3", True, None]),
    {"state": "SUCCEEDED"},
    {"id": "x"},
    {"attempt_count": 5},
    {"spec": {"duration_s": 5, "fail_probabilty": 0.5}},
]


@pytest.mark.parametrize("over", INVALID, ids=repr)
def test_invalid(over):
    with pytest.raises(ValidationError):
        JobCreate.model_validate(_body(**over))


def test_missing_name():
    with pytest.raises(ValidationError):
        JobCreate.model_validate({"spec": {"duration_s": 1}})


def test_job_out_keys():
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import uuid4

    job = SimpleNamespace(
        id=uuid4(),
        name="n",
        spec={"duration_s": 3, "fail_probability": 0.0, "mode": "normal"},
        state="QUEUED",
        max_attempts=3,
        attempt_count=0,
        created_at=datetime.now(UTC),
        started_at=None,
        finished_at=None,
        last_error=None,
        trace_context=None,
        queued_at=datetime.now(UTC),
        last_attempt_number=0,
    )
    out = JobOut.model_validate(job).model_dump(mode="json")
    assert set(out) == {
        "id", "name", "spec", "state", "max_attempts", "attempt_count",
        "created_at", "started_at", "finished_at", "last_error",
    }  # fmt: skip
