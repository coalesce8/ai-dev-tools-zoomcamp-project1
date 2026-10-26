"""LeaseSweeper loop behaviour without a database."""

import logging
import threading
import time
import uuid

from runwatch.sweeper import SWEEP_INTERVAL_S, LeaseSweeper, SweptLease
from sqlalchemy.exc import OperationalError


def alive():
    return [t for t in threading.enumerate() if t.name == "lease-sweeper"]


class Ticking(LeaseSweeper):
    def __init__(self, interval_s, behaviour):
        super().__init__(lambda: None, interval_s=interval_s)
        self.calls = 0
        self.behaviour = behaviour

    def run_once(self):
        self.calls += 1
        return self.behaviour(self.calls)


def wait_for(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_interval_constant():
    assert SWEEP_INTERVAL_S == 5


def test_start_runs_immediately_and_stop_is_prompt():
    s = Ticking(5, lambda n: [])
    s.start()
    try:
        assert wait_for(lambda: s.calls >= 1, 1)
        assert s.is_running
        assert len(alive()) == 1
        assert alive()[0].daemon
    finally:
        began = time.monotonic()
        s.stop()
    assert time.monotonic() - began < 1
    assert not s.is_running
    assert alive() == []


def test_start_twice_starts_one_thread():
    s = Ticking(5, lambda n: [])
    s.start()
    s.start()
    try:
        assert len(alive()) == 1
    finally:
        s.stop()


def test_stop_without_start_is_harmless():
    LeaseSweeper(lambda: None).stop()


def test_failing_ticks_do_not_end_the_thread():
    def behaviour(n):
        if n <= 2:
            raise OperationalError("SELECT 1", {}, Exception("boom"))
        return []

    s = Ticking(0.05, behaviour)
    began = time.monotonic()
    s.start()
    try:
        assert wait_for(lambda: s.calls >= 3, 3 * 0.05 + 1)
    finally:
        s.stop()
    assert time.monotonic() - began < 3 * 0.05 + 1
    assert alive() == []


def test_error_log_has_class_name_only(caplog):
    url = "postgresql+psycopg://u:secret@db.example:5432/x"

    def behaviour(n):
        raise RuntimeError(f"could not connect to {url}")

    s = Ticking(0.05, behaviour)
    with caplog.at_level(logging.DEBUG):
        s.start()
        try:
            assert wait_for(lambda: s.calls >= 2, 1)
        finally:
            s.stop()
    records = [r for r in caplog.records if r.name == "runwatch.sweeper"]
    assert records
    for r in records:
        text = r.getMessage() + str(r.exc_info) + str(r.args)
        assert "RuntimeError" in r.getMessage()
        for banned in ("secret", "db.example", "5432"):
            assert banned not in text
    assert all(r.exc_info is None for r in records)


def test_logs_job_id_and_attempt_number_only(caplog):
    lease = SweptLease(uuid.uuid4(), uuid.uuid4(), 2)
    s = Ticking(5, lambda n: [lease] if n == 1 else [])
    with caplog.at_level(logging.INFO):
        s.start()
        try:
            assert wait_for(lambda: s.calls >= 1, 1)
        finally:
            s.stop()
    infos = [
        r
        for r in caplog.records
        if r.name == "runwatch.sweeper" and r.levelno == logging.INFO
    ]
    assert len(infos) == 1
    msg = infos[0].getMessage()
    assert str(lease.job_id) in msg and "2" in msg
    assert str(lease.attempt_id) not in msg
