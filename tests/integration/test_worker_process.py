"""Worker and load generator as real processes against a real API and database."""

import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest
from alembic.config import Config
from runwatch.models import Attempt, Job, LogLine
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from alembic import command

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; needs a reachable PostgreSQL"
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class Procs:
    """Starts processes through our own Popen handles and stops only those."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.procs = []

    def start(self, args, env_extra, name):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("RUNWATCH_API_URL", "WORKER_FAULT", "WORKER_ID")
        }
        env.update(env_extra)
        log = open(self.tmp_path / f"{name}-{len(self.procs)}.log", "w+")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, *args],
            env=env,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        proc.log = log
        self.procs.append(proc)
        return proc

    def output(self, proc):
        proc.log.flush()
        proc.log.seek(0)
        return proc.log.read()

    def stop_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.log.close()


@pytest.fixture(scope="module")
def engine():
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")
    engine = create_engine(DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))


@pytest.fixture
def procs(tmp_path):
    p = Procs(tmp_path)
    yield p
    p.stop_all()


@pytest.fixture
def api_url(procs):
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    procs.start(
        [
            "-m",
            "uvicorn",
            "--factory",
            "runwatch.api.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        {"DATABASE_URL": DATABASE_URL},
        "api",
    )

    def healthy():
        try:
            return httpx.get(url + "/healthz", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    assert wait_for(healthy, 15), "API did not become healthy"
    return url


def start_worker(procs, url, worker_id, **extra):
    env = {"RUNWATCH_API_URL": url, "WORKER_ID": worker_id, **extra}
    return procs.start(["-m", "runwatch.worker"], env, "worker")


def job_state(engine, job_id):
    with Session(engine) as session:
        return session.get(Job, job_id).state.value


def test_job_runs_to_success(engine, procs, api_url):
    start_worker(procs, api_url, "proc-test-worker")
    response = httpx.post(
        api_url + "/api/v1/jobs",
        json={"name": "t", "spec": {"duration_s": 2, "fail_probability": 0}},
    )
    assert response.status_code == 201
    job_id = response.json()["id"]
    assert wait_for(lambda: job_state(engine, job_id) == "SUCCEEDED", 12)
    with Session(engine) as session:
        attempts = session.scalars(
            select(Attempt).where(Attempt.job_id == job_id)
        ).all()
        assert len(attempts) == 1
        assert attempts[0].state.value == "SUCCEEDED"
        assert attempts[0].worker_id == "proc-test-worker"
        lines = session.scalars(
            select(LogLine).where(LogLine.job_id == job_id).order_by(LogLine.seq)
        ).all()
        assert [(item.seq, item.line) for item in lines] == [
            (1, "step 1/2"),
            (2, "step 2/2"),
        ]


def test_fail_all_fault(engine, procs, api_url):
    start_worker(procs, api_url, "proc-fault-worker", WORKER_FAULT="fail_all")
    response = httpx.post(
        api_url + "/api/v1/jobs",
        json={"name": "t", "spec": {"duration_s": 5}, "max_attempts": 1},
    )
    job_id = response.json()["id"]
    assert wait_for(lambda: job_state(engine, job_id) == "FAILED", 10)
    with Session(engine) as session:
        attempt = session.scalars(select(Attempt).where(Attempt.job_id == job_id)).one()
        assert attempt.failure_cause.value == "infra"
        assert attempt.exit_reason == "injected fault: fail_all"


def test_sigterm_exits_zero(procs, api_url):
    worker = start_worker(procs, api_url, "proc-idle-worker")
    loadgen = procs.start(
        ["-m", "runwatch.loadgen"],
        {"RUNWATCH_API_URL": api_url, "LOADGEN_INTERVAL_S": "5"},
        "loadgen",
    )
    time.sleep(1.5)
    assert worker.poll() is None and loadgen.poll() is None
    worker.send_signal(signal.SIGTERM)
    loadgen.send_signal(signal.SIGTERM)
    assert worker.wait(timeout=3) == 0
    assert loadgen.wait(timeout=3) == 0


def test_worker_without_api_url_exits_nonzero(procs):
    proc = procs.start(["-m", "runwatch.worker"], {}, "worker")
    assert proc.wait(timeout=2) != 0
    assert "RUNWATCH_API_URL" in procs.output(proc)


def test_loadgen_submits_jobs(engine, procs, api_url):
    procs.start(
        ["-m", "runwatch.loadgen"],
        {"RUNWATCH_API_URL": api_url, "LOADGEN_INTERVAL_S": "1"},
        "loadgen",
    )

    def rows():
        with Session(engine) as session:
            return session.scalars(select(Job).where(Job.name == "loadgen")).all()

    assert wait_for(lambda: len(rows()) >= 3, 4)
    for job in rows():
        assert job.spec == {"duration_s": 10, "fail_probability": 0.0, "mode": "normal"}


def test_loadgen_survives_dead_api(procs):
    dead = f"http://127.0.0.1:{free_port()}"
    proc = procs.start(
        ["-m", "runwatch.loadgen"],
        {"RUNWATCH_API_URL": dead, "LOADGEN_INTERVAL_S": "1"},
        "loadgen",
    )
    time.sleep(3)
    assert proc.poll() is None
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=3) == 0
    assert procs.output(proc).count("WARNING") >= 2
