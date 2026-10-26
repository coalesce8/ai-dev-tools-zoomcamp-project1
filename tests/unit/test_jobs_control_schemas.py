"""RequeueWindow validation, no database."""

import pytest
from pydantic import ValidationError
from runwatch.api.jobs_control import RequeueCount, RequeueWindow

T0 = "2026-10-26T10:00:00+00:00"
T1 = "2026-10-26T11:00:00Z"


def test_valid_window():
    w = RequeueWindow.model_validate({"since": T0, "until": T1})
    assert w.since < w.until


def test_equal_bounds_allowed():
    RequeueWindow.model_validate({"since": T0, "until": T0})


def test_offset_window_is_same_instant():
    a = RequeueWindow.model_validate({"since": T0, "until": T1})
    b = RequeueWindow.model_validate(
        {"since": "2026-10-26T12:00:00+02:00", "until": "2026-10-26T13:00:00+02:00"}
    )
    assert a.since == b.since and a.until == b.until


@pytest.mark.parametrize(
    "body",
    [
        {"until": T1},
        {"since": T0},
        {},
        {"since": T1, "until": T0},
        {"since": "2026-10-26T10:00:00", "until": T1},
        {"since": T0, "until": "2026-10-26T11:00:00"},
        {"since": "yesterday", "until": T1},
        {"since": T0, "until": T1, "extra": 1},
    ],
)
def test_invalid_window(body):
    with pytest.raises(ValidationError):
        RequeueWindow.model_validate(body)


def test_count_shape():
    assert RequeueCount(requeued=3).model_dump() == {"requeued": 3}
