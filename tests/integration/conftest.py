"""Shared fixtures for the integration tests.

The database comes from `DATABASE_URL` (the worktree's own database); the API
and worker run as real subprocesses on ports the fixtures pick. Nothing here is
`autouse`, so test modules with their own fixtures are not affected.

Processes are started through our own `Popen` handles and stopped through the
same handles, never by name.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from runwatch.api.app import create_app
from runwatch.settings import Settings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from alembic import command

ROOT = Path(__file__).resolve().parents[2]
START_TIMEOUT_S = 15
STOP_TIMEOUT_S = 5
API_START_ATTEMPTS = 3
OUTPUT_TAIL_LINES = 50


# -- helpers ------------------------------------------------------------------


def free_port() -> int:
    """Bind 127.0.0.1:0, read the port and close the socket."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until(predicate: Callable, timeout: float, interval: float = 0.1, what=""):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout} s waiting for {what or predicate}")
        time.sleep(interval)


def _tail(path: Path, lines: int = OUTPUT_TAIL_LINES) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no output)"


class _Process:
    """One subprocess with its output in a file under the test's tmp_path."""

    name = "process"

    def __init__(self, args: list[str], env: dict[str, str], log_path: Path) -> None:
        self.args = args
        self.env = env
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None

    def _spawn(self) -> None:
        # Append, so a restart keeps the output of the earlier run.
        with open(self.log_path, "ab") as log:
            self.proc = subprocess.Popen(
                [sys.executable, *self.args],
                env=self.env,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.pid = self.proc.pid

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def output_tail(self) -> str:
        return _tail(self.log_path)

    def _terminate(self) -> None:
        """SIGTERM, wait up to 5 s, then SIGKILL, and wait for exit."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def stop(self) -> int | None:
        self._terminate()
        return self.proc.returncode if self.proc else None

    def wait_exit(self, timeout: float) -> int:
        """Wait for the process to exit on its own; return the exit code."""
        assert self.proc is not None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"{self.name} did not exit within {timeout} s\n{self.output_tail()}"
            )

    def report(self) -> None:
        print(f"--- last {OUTPUT_TAIL_LINES} lines of {self.name} output ---")
        print(self.output_tail())


class ApiServer(_Process):
    def __init__(self, args, env, log_path, port: int) -> None:
        super().__init__(args, env, log_path)
        self.port = port
        self.name = f"api:{port}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _healthy(self) -> bool:
        try:
            return httpx.get(f"{self.url}/healthz", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def _start_once(self) -> str | None:
        """Start and wait for /healthz. Returns "in_use" if the port was taken."""
        self._spawn()
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            code = self.proc.poll()
            if code is not None:
                tail = self.output_tail()
                if "address already in use" in tail.lower():
                    return "in_use"
                pytest.fail(f"API exited with code {code} before healthy\n{tail}")
            if self._healthy():
                return None
            time.sleep(0.1)
        self._terminate()
        pytest.fail(
            f"API not healthy within {START_TIMEOUT_S} s (stopped it)\n"
            f"{self.output_tail()}"
        )

    def start(self) -> None:
        """Start again on the same port and wait for /healthz."""
        if self.running():
            raise RuntimeError("the API is already running")
        if self._start_once() == "in_use":
            pytest.fail(f"port {self.port} is in use\n{self.output_tail()}")


class WorkerProcess(_Process):
    def __init__(self, args, env, log_path, worker_id: str) -> None:
        super().__init__(args, env, log_path)
        self.worker_id = worker_id
        self.name = f"worker:{worker_id}"

    def start(self) -> None:
        self._spawn()
        try:
            code = self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return
        pytest.fail(f"worker exited with code {code} within 1 s\n{self.output_tail()}")

    def kill(self) -> None:
        """SIGKILL at once, then wait."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


# -- database -----------------------------------------------------------------


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; integration tests need a PostgreSQL")
    return url


@pytest.fixture(scope="session")
def db_engine(database_url):
    shown = make_url(database_url).render_as_string(hide_password=True)
    engine = create_engine(database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        # The driver's message is left out: only the type and the hidden URL.
        pytest.fail(
            f"cannot reach the database named by DATABASE_URL ({shown}): "
            f"{type(exc).__name__}",
            pytrace=False,
        )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def migrated_db(db_engine, database_url):
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


@pytest.fixture
def clean_db(db_engine, migrated_db):
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))


@pytest.fixture
def db_session(db_engine, clean_db):
    with Session(db_engine) as session:
        yield session


@pytest.fixture
def db_now(db_engine) -> Callable[[], datetime]:
    """A function returning the database's `SELECT now()` as an aware datetime."""

    def now() -> datetime:
        with db_engine.connect() as conn:
            return conn.execute(text("SELECT now()")).scalar_one()

    return now


# -- in-process app -----------------------------------------------------------


@pytest.fixture
def api_client(database_url, clean_db):
    settings = Settings(database_url=database_url, sweeper_enabled=False)
    with TestClient(create_app(settings)) as client:
        yield client


# -- processes ----------------------------------------------------------------


@pytest.fixture(name="free_port")
def free_port_fixture() -> Callable[[], int]:
    return free_port


@pytest.fixture
def wait_until():
    def wait(predicate: Callable, timeout: float, interval: float = 0.1, what=""):
        return _wait_until(predicate, timeout, interval, what)

    return wait


@pytest.fixture
def _processes(tmp_path):
    """Every process a fixture starts; stopped at teardown, output reported."""
    started: list[_Process] = []
    yield started
    for process in reversed(started):
        try:
            process.stop()
        finally:
            process.report()


@pytest.fixture
def api_server(database_url, clean_db, tmp_path, _processes):
    counter = iter(range(1, 1000))

    def start(sweeper_enabled: bool = True, port: int | None = None) -> ApiServer:
        env = dict(os.environ)
        env["DATABASE_URL"] = database_url
        env["SWEEPER_ENABLED"] = "true" if sweeper_enabled else "false"
        n = next(counter)
        for _ in range(API_START_ATTEMPTS):
            port = port if port is not None else free_port()
            args = [
                "-m",
                "uvicorn",
                "--factory",
                "runwatch.api.app:create_app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ]
            server = ApiServer(args, env, tmp_path / f"api-{n}-{port}.log", port)
            _processes.append(server)
            if server._start_once() is None:
                return server
            port = None  # taken in the meantime: pick a new one
        pytest.fail(f"API port was taken {API_START_ATTEMPTS} times in a row")

    return start


@pytest.fixture
def worker(tmp_path, _processes):
    counter = iter(range(1, 1000))

    def start(api, fault: str | None = None, worker_id: str | None = None):
        n = next(counter)
        if worker_id is None:
            # Unique within the session: pid plus a session-wide counter.
            worker_id = f"it-{os.getpid()}-{next(_WORKER_SEQ)}"
        env = dict(os.environ)
        env["RUNWATCH_API_URL"] = api.url
        env["WORKER_ID"] = worker_id
        env["WORKER_FAULT"] = "fail_all" if fault == "fail_all" else ""
        handle = WorkerProcess(
            ["-m", "runwatch.worker"], env, tmp_path / f"worker-{n}.log", worker_id
        )
        _processes.append(handle)
        handle.start()
        return handle

    return start


def _sequence():
    n = 0
    while True:
        n += 1
        yield n


_WORKER_SEQ = _sequence()
