"""The integration fixtures themselves."""

import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import httpx
import pytest
from runwatch.models import Job
from sqlalchemy import func, select, text

ROOT = Path(__file__).resolve().parents[2]
SPEC = {"duration_s": 1, "fail_probability": 0.0, "mode": "normal"}


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_db_tests_skip_without_database_url():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-rs",
            "-p",
            "no:cacheprovider",
            "tests/integration/test_harness.py::test_clean_db_empties_tables_first",
            "tests/integration/test_harness.py::test_api_server_is_healthy_and_ready",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 skipped" in result.stdout
    assert "DATABASE_URL is not set" in result.stdout


def test_free_port_returns_bindable_ports(free_port):
    for _ in range(20):
        port = free_port()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))


def test_db_engine_failure_names_database_url_and_hides_password():
    env = dict(os.environ, DATABASE_URL="postgresql+psycopg://u:s3cretpw@127.0.0.1:1/x")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "tests/integration/test_harness.py::test_clean_db_empties_tables_first",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
    )
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "DATABASE_URL" in out
    assert "127.0.0.1:1" in out
    assert "s3cretpw" not in out


def test_clean_db_empties_tables_first(db_session):
    db_session.add(Job(id=uuid.uuid4(), name="j", spec=SPEC))
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(Job)) == 1


def test_clean_db_empties_tables_second(db_session):
    assert db_session.scalar(select(func.count()).select_from(Job)) == 0


def test_migrated_db_is_idempotent(db_engine, migrated_db):
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() >= 0


def test_db_now_is_aware(db_now):
    assert db_now().tzinfo is not None


def test_api_client_runs_without_sweeper(api_client):
    assert api_client.get("/healthz").status_code == 200
    names = [t.name for t in threading.enumerate()]
    assert "lease-sweeper" not in names


def test_api_server_is_healthy_and_ready(api_server):
    api = api_server()
    assert api.url == f"http://127.0.0.1:{api.port}"
    assert httpx.get(f"{api.url}/healthz").status_code == 200
    assert httpx.get(f"{api.url}/readyz").status_code == 200


def test_api_server_stop_then_start_keeps_the_port(api_server):
    api = api_server()
    port, pid = api.port, api.pid
    api.stop()
    assert _gone(pid) or not api.running()
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{api.url}/healthz", timeout=1)
    api.start()
    assert api.port == port
    assert httpx.get(f"{api.url}/healthz").status_code == 200


def test_api_server_retries_when_the_port_is_taken(api_server):
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        taken = held.getsockname()[1]
        api = api_server(port=taken)
        assert api.port != taken
        assert httpx.get(f"{api.url}/healthz").status_code == 200


def test_api_server_without_sweeper_has_the_flag_in_its_environment(api_server):
    api = api_server(sweeper_enabled=False)
    assert api.env["SWEEPER_ENABLED"] == "false"
    assert api.env["DATABASE_URL"] == os.environ["DATABASE_URL"]


def test_worker_exits_with_code_zero_on_sigterm(api_server, worker, wait_until):
    api = api_server()
    w = worker(api)
    other = worker(api)
    assert w.worker_id != other.worker_id
    assert w.running()
    assert w.stop() == 0
    assert other.stop() == 0
    assert not w.running()


def test_worker_that_exits_at_once_fails_the_test(api_server, worker):
    api = api_server()
    api.stop()
    # Without RUNWATCH_API_URL being valid the worker still runs, so use a bad id.
    with pytest.raises(pytest.fail.Exception) as info:
        worker(api, worker_id="x" * 300)
    assert "exited with code" in str(info.value)


def test_wait_until_times_out_with_a_message(wait_until):
    assert wait_until(lambda: "yes", 1) == "yes"
    with pytest.raises(pytest.fail.Exception) as info:
        wait_until(lambda: False, 0.3, what="the moon")
    assert "the moon" in str(info.value)
    assert "0.3" in str(info.value)


def test_processes_start_and_record_pids(api_server, worker, tmp_path):
    api = api_server()
    w = worker(api)
    assert api.running() and w.running()
    target = os.environ.get("HARNESS_PIDS_FILE")
    if target:
        Path(target).write_text(f"{api.pid} {w.pid}")


def test_no_pid_left_after_teardown(db_engine, tmp_path):
    pids_file = tmp_path / "pids.txt"
    env = dict(os.environ, HARNESS_PIDS_FILE=str(pids_file))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/integration/test_harness.py::test_processes_start_and_record_pids",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    pids = [int(p) for p in pids_file.read_text().split()]
    assert len(pids) == 2
    assert all(_gone(pid) for pid in pids)
