"""Cursor encoding for the job list: no database."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from runwatch.api.errors import ApiError
from runwatch.api.jobs_read import decode_cursor, encode_cursor


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_round_trip_keeps_microseconds():
    ts = datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC)
    jid = uuid.uuid4()
    cursor = encode_cursor(ts, jid)
    assert decode_cursor(cursor) == (ts, jid)
    assert all(c.isalnum() or c in "-_" for c in cursor)


def test_round_trip_other_offset_is_same_instant():
    ts = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=2)))
    jid = uuid.uuid4()
    got, _ = decode_cursor(encode_cursor(ts, jid))
    assert got == ts


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "abc",
        "!!!!",
        _b64(b"\xff\xfe\x00random"),
        _b64(b"not json"),
        _b64(b"[]"),
        _b64(b"null"),
        _b64(b"{}"),
        _b64(json.dumps({"created_at": "nope", "id": str(uuid.uuid4())}).encode()),
        _b64(
            json.dumps(
                {"created_at": "2026-01-01T00:00:00", "id": str(uuid.uuid4())}
            ).encode()
        ),
        _b64(
            json.dumps({"created_at": "2026-01-01T00:00:00+00:00", "id": "x"}).encode()
        ),
        _b64(json.dumps({"created_at": 5, "id": 7}).encode()),
    ],
)
def test_malformed_cursor(cursor):
    with pytest.raises(ApiError) as e:
        decode_cursor(cursor)
    assert e.value.status_code == 400
    assert e.value.code == "validation_error"
    assert e.value.message.startswith("cursor:")
