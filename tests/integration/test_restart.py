"""AC-9 with real processes: data survives restarting the API, the worker and
the database, and a job running through a short database outage finishes.

A database outage is made two ways. `TcpProxy` sits between the API and the
worktree's database and is cut and restored by the test, so the outage has an
exact length. `PrivatePg` is a Postgres container that belongs to this
worktree: it is created once, then only started, stopped, restarted and
killed, and never removed. Its data lives in the image's anonymous volume, so
it survives all of those.

The API in these tests is started here (`launch_api`) because the `api_server`
fixture always uses the `DATABASE_URL` fixture value. Data checks read the
database directly through `db_engine` (or the container's own engine), not
through the proxy.
"""

from __future__ import annotations

import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]

SPEC_30 = {"duration_s": 30, "fail_probability": 0, "mode": "normal"}
STALL_LIMIT_S = 25  # 15 s lease plus one 5 s sweeper tick, with margin
CONTAINER_DB = "runwatch_restart"
PG_IMAGE = "postgres:16"
PG_READY_S = 60


# -- small helpers --------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hold(seconds: float, check: Callable[[], object] | None = None) -> None:
    """Wait "for N s", running `check` every 0.5 s (a bounded negative wait)."""
    deadline = time.monotonic() + seconds
    while True:
        if check is not None:
            check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


def status_of(base_url: str, path: str, timeout: float = 3) -> int | None:
    """HTTP status of a GET, or None when there was no answer."""
    try:
        return httpx.get(f"{base_url}{path}", timeout=timeout).status_code
    except httpx.HTTPError:
        return None


def url_for(database_url: str, **parts) -> str:
    return make_url(database_url).set(**parts).render_as_string(hide_password=False)


# -- direct database reads (never through the proxy) ----------------------------


def db_job(engine, job_id: str) -> dict:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT state, attempt_count, last_error FROM jobs WHERE id = :i"),
                {"i": job_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def db_attempts(engine, job_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT number, state, failure_cause, exit_reason, finished_at, "
                "worker_id FROM attempts WHERE job_id = :i ORDER BY number"
            ),
            {"i": job_id},
        ).mappings()
        return [dict(r) for r in rows]


def db_log_lines(engine, job_id: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT line FROM log_lines WHERE job_id = :i ORDER BY seq"),
            {"i": job_id},
        )
        return [r[0] for r in rows]


def row_counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("jobs", "attempts", "log_lines")
        }


def wait_db_state(
    engine,
    wait_until,
    job_id: str,
    state: str,
    timeout: float,
    forbidden: tuple[str, ...] = (),
):
    """Poll the job's state in the database until it is `state`.

    Fails at once if the state is one of `forbidden` at any poll.
    """

    def check():
        current = db_job(engine, job_id)["state"]
        if current in forbidden:
            pytest.fail(f"job {job_id} was {current} while waiting for {state}")
        return current == state

    return wait_until(check, timeout, 0.25, what=f"job {job_id} to be {state}")


def wait_first_log(engine, wait_until, job_id: str, timeout: float = 15):
    wait_until(
        lambda: db_log_lines(engine, job_id),
        timeout,
        0.25,
        what=f"job {job_id} first log line",
    )


# -- the public API as a client ---------------------------------------------------


class Client:
    def __init__(self, url: str) -> None:
        self.http = httpx.Client(base_url=url, timeout=10)

    def submit(self, spec=None, max_attempts=None) -> str:
        body: dict = {"name": "restart", "spec": spec or SPEC_30}
        if max_attempts is not None:
            body["max_attempts"] = max_attempts
        response = self.http.post("/api/v1/jobs", json=body)
        assert response.status_code == 201, response.status_code
        return response.json()["id"]

    def job(self, job_id: str) -> dict:
        response = self.http.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.status_code
        return response.json()

    def logs(self, job_id: str) -> list[dict]:
        response = self.http.get(f"/api/v1/jobs/{job_id}/logs")
        assert response.status_code == 200, response.status_code
        return response.json()["items"]

    def post(self, path: str) -> httpx.Response:
        return self.http.post(path)

    def list_ids(self, limit: int) -> list[list[str]]:
        """Walk the whole job list page by page; returns the ids of each page."""
        pages: list[list[str]] = []
        cursor = None
        while True:
            params: dict = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            response = self.http.get("/api/v1/jobs", params=params)
            assert response.status_code == 200, response.status_code
            body = response.json()
            pages.append([j["id"] for j in body["items"]])
            cursor = body["next_cursor"]
            if not cursor:
                return pages


def assert_clean_job(client: Client, job_id: str, steps: int) -> None:
    """SUCCEEDED, one attempt, and exactly `step 1/N` ... `step N/N` once each."""
    job = client.job(job_id)
    assert job["state"] == "SUCCEEDED"
    assert job["attempt_count"] == 1
    (attempt,) = job["attempts"]
    assert attempt["number"] == 1
    assert attempt["state"] == "SUCCEEDED"
    assert attempt["failure_cause"] is None
    lines = client.logs(job_id)
    assert [(x["seq"], x["line"]) for x in lines] == [
        (i, f"step {i}/{steps}") for i in range(1, steps + 1)
    ]


def snapshot(client: Client, ids: list[str]) -> dict:
    """The raw text of the list, every detail and every logs response."""
    listing = client.http.get("/api/v1/jobs", params={"limit": 100})
    assert listing.status_code == 200
    snap = {"list": listing.text}
    for job_id in ids:
        detail = client.http.get(f"/api/v1/jobs/{job_id}")
        logs = client.http.get(f"/api/v1/jobs/{job_id}/logs")
        assert detail.status_code == 200
        assert logs.status_code == 200
        snap[f"job {job_id}"] = detail.text
        snap[f"logs {job_id}"] = logs.text
    return snap


def assert_same_snapshot(before: dict, after: dict) -> None:
    changed = [k for k in before if before[k] != after.get(k)]
    assert not changed, f"responses changed: {changed}"
    assert before.keys() == after.keys()


# -- the TCP proxy -------------------------------------------------------------


class _Conn:
    """One proxied connection: a client socket, an upstream socket, two pumps."""

    def __init__(self, client: socket.socket, upstream: socket.socket) -> None:
        self.client = client
        self.upstream = upstream
        self.stop = threading.Event()
        for sock in (client, upstream):
            sock.settimeout(0.2)
        self.pumps = [
            threading.Thread(target=self._pump, args=(client, upstream), daemon=True),
            threading.Thread(target=self._pump, args=(upstream, client), daemon=True),
        ]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while not self.stop.is_set():
                try:
                    data = src.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            self.stop.set()

    def _serve(self) -> None:
        for pump in self.pumps:
            pump.start()
        for pump in self.pumps:
            pump.join()
        for sock in (self.client, self.upstream):
            sock.close()

    def abort(self) -> None:
        """Reset the client side (RST) and end both pumps."""
        try:
            self.client.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
        except OSError:
            pass
        self.stop.set()


class TcpProxy:
    """Forwards 127.0.0.1:<port> to the database named by `target_url`.

    `cut()` closes the listening socket and resets every open connection, the
    way a stopped database breaks them; `restore()` listens again on the same
    port. `close()` ends every thread and socket.
    """

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.port = _free_port()
        self._lock = threading.Lock()
        self._conns: list[_Conn] = []
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self.listen()

    @property
    def listening(self) -> bool:
        return self._thread is not None

    def listen(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        deadline = time.monotonic() + 5
        while True:
            try:
                listener.bind(("127.0.0.1", self.port))
                break
            except OSError:
                if time.monotonic() >= deadline:
                    listener.close()
                    raise
                time.sleep(0.1)
        listener.listen(128)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._accept_loop, args=(listener, self._stop), daemon=True
        )
        self._thread.start()

    restore = listen

    def _accept_loop(self, listener: socket.socket, stop: threading.Event) -> None:
        listener.settimeout(0.1)
        try:
            while not stop.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                try:
                    upstream = socket.create_connection(self.target, timeout=3)
                except OSError:
                    client.close()
                    continue
                with self._lock:
                    self._conns = [c for c in self._conns if c.thread.is_alive()]
                    self._conns.append(_Conn(client, upstream))
        finally:
            listener.close()

    def cut(self) -> None:
        if self._thread is not None:
            assert self._stop is not None
            self._stop.set()
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            conn.abort()
        for conn in conns:
            conn.thread.join(timeout=5)

    def close(self) -> None:
        self.cut()

    def url(self, database_url: str) -> str:
        return url_for(database_url, host="127.0.0.1", port=self.port)


# -- an API started by this file -----------------------------------------------


class ExternalApi:
    """A uvicorn process on a free port, with its output in a file."""

    def __init__(self, database_url: str, log_path: Path) -> None:
        self.database_url = database_url
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def output_tail(self, lines: int = 50) -> str:
        try:
            return "\n".join(
                self.log_path.read_text(errors="replace").splitlines()[-lines:]
            )
        except OSError:
            return "(no output)"

    def start(self) -> None:
        env = dict(os.environ)
        env["DATABASE_URL"] = self.database_url
        env["SWEEPER_ENABLED"] = "true"
        for _ in range(3):
            self.port = _free_port()
            args = [
                sys.executable,
                "-m",
                "uvicorn",
                "--factory",
                "runwatch.api.app:create_app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ]
            with open(self.log_path, "ab") as log:
                self.proc = subprocess.Popen(
                    args, env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
                )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                code = self.proc.poll()
                if code is not None:
                    if "address already in use" in self.output_tail().lower():
                        break  # the port was taken meanwhile: pick another
                    pytest.fail(f"API exited with {code}\n{self.output_tail()}")
                if status_of(self.url, "/healthz", 1) == 200:
                    return
                time.sleep(0.1)
            else:
                self.stop()
                pytest.fail(f"API not healthy within 15 s\n{self.output_tail()}")
        pytest.fail("API port was taken three times in a row")

    def stop(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def launch_api(tmp_path):
    started: list[ExternalApi] = []

    def launch(database_url: str) -> ExternalApi:
        api = ExternalApi(database_url, tmp_path / f"api-{len(started) + 1}.log")
        started.append(api)
        api.start()
        return api

    yield launch
    for api in reversed(started):
        try:
            api.stop()
        finally:
            print(f"--- last 50 lines of API {api.log_path.name} ---")
            print(api.output_tail())


@pytest.fixture
def make_client():
    clients: list[Client] = []

    def make(url: str) -> Client:
        client = Client(url)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.http.close()


# -- the outage environment (proxy) ---------------------------------------------


class Outage:
    def __init__(self, proxy, api, client, engine, database_url) -> None:
        self.proxy = proxy
        self.api = api
        self.client = client
        self.engine = engine
        self.database_url = database_url


@pytest.fixture
def proxy(database_url):
    url = make_url(database_url)
    proxy = TcpProxy(url.host or "127.0.0.1", url.port or 5432)
    try:
        yield proxy
    finally:
        proxy.close()


@pytest.fixture
def outage(proxy, database_url, clean_db, db_engine, launch_api, make_client):
    api = launch_api(proxy.url(database_url))
    return Outage(proxy, api, make_client(api.url), db_engine, database_url)


def cut_and_restore(o: Outage, seconds: float, check=None) -> None:
    o.proxy.cut()
    hold(seconds, check)
    o.proxy.restore()


# -- the proxy itself -------------------------------------------------------------


def test_proxy_forwards_then_cuts_then_restores(proxy, database_url):
    engine = create_engine(
        proxy.url(database_url), pool_size=1, connect_args={"connect_timeout": 3}
    )
    try:
        pooled = engine.connect()
        assert pooled.execute(text("SELECT 1")).scalar_one() == 1

        proxy.cut()
        assert not proxy.listening
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
        with pytest.raises(OperationalError):
            pooled.execute(text("SELECT 1"))
        pooled.close()
        fresh = create_engine(
            proxy.url(database_url),
            poolclass=NullPool,
            connect_args={"connect_timeout": 3},
        )
        with pytest.raises(OperationalError):
            fresh.connect()

        proxy.restore()
        with fresh.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
        fresh.dispose()
    finally:
        engine.dispose()


# -- outage through the proxy: job survival ---------------------------------------


def test_outage_of_8s_under_a_running_job(outage, worker, wait_until):
    o = outage
    w = worker(o.api)
    job_id = o.client.submit(SPEC_30)
    wait_first_log(o.engine, wait_until, job_id)
    pids = (o.api.pid, w.pid)

    def not_stalled_or_failed():
        assert db_job(o.engine, job_id)["state"] not in ("STALLED", "FAILED")

    cut_and_restore(o, 8, not_stalled_or_failed)
    wait_db_state(
        o.engine, wait_until, job_id, "SUCCEEDED", 60, forbidden=("STALLED", "FAILED")
    )
    assert_clean_job(o.client, job_id, 30)
    assert (o.api.pid, w.pid) == pids
    assert o.api.running()
    assert w.running()


def test_api_fails_fast_during_an_outage_and_recovers(
    outage, worker, wait_until, database_url
):
    o = outage
    w = worker(o.api)
    password = make_url(database_url).password
    secrets = ["127.0.0.1", str(o.proxy.port)] + ([password] if password else [])
    pid = o.api.pid
    created = 0

    o.proxy.cut()
    try:
        assert status_of(o.api.url, "/healthz") == 200

        started = time.monotonic()
        ready = httpx.get(f"{o.api.url}/readyz", timeout=3)
        assert ready.status_code == 503
        assert time.monotonic() - started < 3

        body = {"name": "restart", "spec": {"duration_s": 1}}
        for method, kwargs in (("GET", {}), ("POST", {"json": body})):
            started = time.monotonic()
            response = httpx.request(
                method, f"{o.api.url}/api/v1/jobs", timeout=5, **kwargs
            )
            assert time.monotonic() - started < 5, method
            created += response.status_code == 201
            assert 500 <= response.status_code < 600, (method, response.status_code)
            error = response.json()["error"]
            assert set(error) >= {"code", "message"}, method
            for secret in secrets:
                assert secret not in response.text, method
    finally:
        o.proxy.restore()

    with o.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() == created

    wait_until(
        lambda: (
            status_of(o.api.url, "/readyz") == 200
            and status_of(o.api.url, "/api/v1/jobs") == 200
        ),
        5,
        0.25,
        what="the same API to be ready after the restore",
    )
    assert o.api.pid == pid
    job_id = o.client.submit({"duration_s": 2})
    wait_db_state(o.engine, wait_until, job_id, "SUCCEEDED", 30)
    assert_clean_job(o.client, job_id, 2)
    assert db_attempts(o.engine, job_id)[0]["worker_id"] == w.worker_id


def test_sweeper_survives_an_outage(outage, worker, wait_until, db_now):
    o = outage
    first = worker(o.api)
    cut_and_restore(o, 8)
    wait_until(
        lambda: status_of(o.api.url, "/readyz") == 200, 5, 0.25, what="API ready"
    )
    job_id = o.client.submit(SPEC_30)
    wait_first_log(o.engine, wait_until, job_id)
    killed_at = db_now()
    first.kill()
    worker(o.api)
    wait_db_state(o.engine, wait_until, job_id, "STALLED", STALL_LIMIT_S + 5)
    job = db_job(o.engine, job_id)
    assert job["last_error"] == "lease expired"
    (attempt,) = db_attempts(o.engine, job_id)
    assert attempt["state"] == "FAILED"
    assert attempt["failure_cause"] == "infra"
    assert (attempt["finished_at"] - killed_at).total_seconds() <= STALL_LIMIT_S


def test_outage_spanning_the_end_of_a_job(outage, worker, wait_until):
    o = outage
    worker(o.api)
    job_id = o.client.submit({"duration_s": 6})
    wait_until(
        lambda: "step 5/6" in db_log_lines(o.engine, job_id),
        15,
        0.1,
        what="step 5/6",
    )
    cut_and_restore(o, 8)
    wait_db_state(
        o.engine, wait_until, job_id, "SUCCEEDED", 60, forbidden=("STALLED", "FAILED")
    )
    assert_clean_job(o.client, job_id, 6)


def test_outage_of_20s_makes_the_worker_give_up(outage, worker, wait_until, db_now):
    o = outage
    w = worker(o.api)
    job_id = o.client.submit({"duration_s": 60})
    wait_first_log(o.engine, wait_until, job_id)
    pids = (o.api.pid, w.pid)
    cut_and_restore(o, 20)
    restored_at = db_now()
    wait_db_state(
        o.engine, wait_until, job_id, "STALLED", 30, forbidden=("SUCCEEDED", "QUEUED")
    )
    (attempt,) = db_attempts(o.engine, job_id)
    assert attempt["number"] == 1
    assert attempt["state"] == "FAILED"
    assert attempt["failure_cause"] == "infra"
    assert attempt["exit_reason"] == "lease expired"
    assert (attempt["finished_at"] - restored_at).total_seconds() <= 30

    def still_stalled():
        assert db_job(o.engine, job_id)["state"] == "STALLED"
        assert len(db_attempts(o.engine, job_id)) == 1

    hold(3, still_stalled)
    assert (o.api.pid, w.pid) == pids
    assert w.running()

    next_id = o.client.submit({"duration_s": 2})
    wait_db_state(o.engine, wait_until, next_id, "SUCCEEDED", 30)
    assert_clean_job(o.client, next_id, 2)
    assert db_attempts(o.engine, next_id)[0]["worker_id"] == w.worker_id


# -- restarting the API -------------------------------------------------------------


def build_five_jobs(client, engine, worker_start, api, wait_until) -> list[str]:
    """SUCCEEDED, FAILED, CANCELLED, STALLED and QUEUED jobs, in that order."""
    cancelled = client.submit({"duration_s": 5})
    assert client.post(f"/api/v1/jobs/{cancelled}/cancel").status_code == 200
    w = worker_start(api)
    succeeded = client.submit({"duration_s": 2})
    failed = client.submit({"duration_s": 1, "mode": "always_fail"}, max_attempts=1)
    wait_db_state(engine, wait_until, succeeded, "SUCCEEDED", 30)
    wait_db_state(engine, wait_until, failed, "FAILED", 30)
    stalled = client.submit(SPEC_30)
    wait_first_log(engine, wait_until, stalled)
    w.kill()
    wait_db_state(engine, wait_until, stalled, "STALLED", STALL_LIMIT_S + 5)
    queued = client.submit({"duration_s": 2})  # no worker is running
    assert db_job(engine, queued)["state"] == "QUEUED"
    expected = {
        succeeded: "SUCCEEDED",
        failed: "FAILED",
        cancelled: "CANCELLED",
        stalled: "STALLED",
        queued: "QUEUED",
    }
    for job_id, state in expected.items():
        assert db_job(engine, job_id)["state"] == state
    assert db_log_lines(engine, succeeded) == ["step 1/2", "step 2/2"]
    return [succeeded, failed, cancelled, stalled, queued]


def test_data_survives_an_api_restart(api_server, worker, db_engine, wait_until):
    api = api_server()
    client = Client(api.url)
    try:
        ids = build_five_jobs(client, db_engine, worker, api, wait_until)
        before = snapshot(client, ids)
        counts = row_counts(db_engine)
        api.stop()
        api.start()
        assert_same_snapshot(before, snapshot(client, ids))
        assert row_counts(db_engine) == counts
    finally:
        client.http.close()


def test_running_job_survives_the_api_being_down_for_5s(
    api_server, worker, db_engine, wait_until
):
    api = api_server()
    client = Client(api.url)
    try:
        w = worker(api)
        job_id = client.submit(SPEC_30)
        wait_first_log(db_engine, wait_until, job_id)
        pid = w.pid
        api.stop()
        hold(5)
        api.start()
        wait_db_state(
            db_engine,
            wait_until,
            job_id,
            "SUCCEEDED",
            60,
            forbidden=("STALLED", "FAILED"),
        )
        assert_clean_job(client, job_id, 30)
        assert w.pid == pid
        assert w.running()
    finally:
        client.http.close()


def test_queue_works_after_an_api_restart(api_server, worker, db_engine, wait_until):
    api = api_server()
    client = Client(api.url)
    try:
        ids = [client.submit({"duration_s": 2}) for _ in range(3)]
        api.stop()
        api.start()
        for job_id in ids:
            assert db_job(db_engine, job_id)["state"] == "QUEUED"
        pages = client.list_ids(2)
        assert [len(p) for p in pages] == [2, 1]
        assert sorted(sum(pages, [])) == sorted(ids)
        worker(api)
        for job_id in ids:
            wait_db_state(db_engine, wait_until, job_id, "SUCCEEDED", 30)
            assert_clean_job(client, job_id, 2)
    finally:
        client.http.close()


def test_restarted_api_sweeps_a_lease_that_expired_while_down(
    api_server, worker, db_engine, wait_until
):
    api = api_server()
    client = Client(api.url)
    try:
        w = worker(api)
        job_id = client.submit(SPEC_30)
        wait_first_log(db_engine, wait_until, job_id)
        w.kill()
        api.stop()
        hold(3)
        api.start()
        wait_db_state(db_engine, wait_until, job_id, "STALLED", 30)
        (attempt,) = db_attempts(db_engine, job_id)
        assert attempt["state"] == "FAILED"
        assert attempt["failure_cause"] == "infra"
        assert db_job(db_engine, job_id)["last_error"] == "lease expired"
    finally:
        client.http.close()


# -- restarting the worker -----------------------------------------------------------


def test_data_survives_a_worker_restart_and_the_stopped_workers_job_stalls(
    api_server, worker, db_engine, db_now, wait_until
):
    api = api_server()
    client = Client(api.url)
    try:
        first = worker(api)
        done = [client.submit({"duration_s": 2}) for _ in range(2)]
        for job_id in done:
            wait_db_state(db_engine, wait_until, job_id, "SUCCEEDED", 30)
        before = snapshot(client, done)

        job_id = client.submit(SPEC_30)
        wait_first_log(db_engine, wait_until, job_id)
        stopped_at = db_now()
        assert first.stop() == 0
        wait_db_state(db_engine, wait_until, job_id, "STALLED", STALL_LIMIT_S + 5)
        (attempt,) = db_attempts(db_engine, job_id)
        assert attempt["state"] == "FAILED"
        assert attempt["failure_cause"] == "infra"
        assert (attempt["finished_at"] - stopped_at).total_seconds() <= STALL_LIMIT_S

        second = worker(api)

        def still_stalled():
            assert db_job(db_engine, job_id)["state"] == "STALLED"

        hold(10, still_stalled)
        response = client.post(f"/api/v1/jobs/{job_id}/requeue")
        assert response.status_code == 200
        assert response.json()["state"] == "QUEUED"
        assert response.json()["attempt_count"] == 0

        wait_db_state(db_engine, wait_until, job_id, "SUCCEEDED", 60)
        one, two = client.job(job_id)["attempts"]
        assert (one["number"], one["state"], one["failure_cause"]) == (
            1,
            "FAILED",
            "infra",
        )
        assert (two["number"], two["state"]) == (2, "SUCCEEDED")
        assert two["worker_id"] == second.worker_id
        after = snapshot(client, done)
        for key in before:
            if key != "list":
                assert before[key] == after[key], key
    finally:
        client.http.close()


def test_a_killed_worker_loses_nothing_else(api_server, worker, db_engine, wait_until):
    api = api_server()
    client = Client(api.url)
    try:
        ids = [client.submit({"duration_s": 3}) for _ in range(3)]
        first = worker(api)

        def running_job():
            for job_id in ids:
                if db_job(db_engine, job_id)["state"] == "RUNNING":
                    return job_id
            return None

        killed_job = wait_until(running_job, 15, 0.1, what="a job to be RUNNING")
        first.kill()
        worker(api)
        others = [j for j in ids if j != killed_job]
        for job_id in others:
            wait_db_state(db_engine, wait_until, job_id, "SUCCEEDED", 30)
            assert_clean_job(client, job_id, 3)
        wait_db_state(db_engine, wait_until, killed_job, "STALLED", STALL_LIMIT_S + 5)
        assert db_job(db_engine, killed_job)["attempt_count"] == 1
        with db_engine.connect() as conn:
            running = conn.execute(
                text("SELECT count(*) FROM attempts WHERE state = 'RUNNING'")
            ).scalar_one()
        assert running == 0
    finally:
        client.http.close()


# -- restarting the Postgres container (private container) ----------------------------


def _docker(*args: str, timeout: float = 120, check: bool = True):
    """Run docker in argument-list form (no shell)."""
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        pytest.fail(f"docker {args[0]} failed: {result.stderr.strip()[-300:]}")
    return result


class PrivatePg:
    """This worktree's own Postgres container: created once, never removed."""

    def __init__(self, database_url: str) -> None:
        dbname = make_url(database_url).database or "runwatch"
        self.name = "runwatch-it-pg-" + re.sub(r"[^a-zA-Z0-9_.-]", "_", dbname)
        self.database_url = database_url
        self.port = 0
        self.engine = None
        # Responses saved by the first survival test, shared by the second.
        self.baseline: tuple[list[str], dict, dict] | None = None

    def exists(self) -> bool:
        result = _docker("inspect", self.name, check=False)
        if result.returncode == 0:
            return True
        if "no such" not in result.stderr.lower():
            pytest.fail(f"docker inspect failed: {result.stderr.strip()[-300:]}")
        return False

    def running(self) -> bool:
        out = _docker("inspect", "-f", "{{.State.Running}}", self.name).stdout
        return out.strip() == "true"

    def container_id(self) -> str:
        return _docker("inspect", "-f", "{{.Id}}", self.name).stdout.strip()

    def create(self) -> None:
        image = _docker("image", "inspect", PG_IMAGE, check=False)
        if image.returncode != 0:
            pytest.fail(
                f"image {PG_IMAGE} is not present locally; the test never pulls"
            )
        _docker(
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            self.name,
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-p",
            f"127.0.0.1:{_free_port()}:5432",
            PG_IMAGE,
        )

    def start(self) -> None:
        result = _docker("start", self.name, check=False)
        if result.returncode != 0:
            err = result.stderr.lower()
            if "port is already allocated" in err or "address already in use" in err:
                pytest.fail(
                    f"the host port of container {self.name} is taken by another "
                    "program; the test did not remove or recreate the container "
                    "(that is your call)"
                )
            pytest.fail(f"docker start failed: {result.stderr.strip()[-300:]}")

    def read_port(self) -> None:
        out = _docker("port", self.name, "5432").stdout.splitlines()[0]
        self.port = int(out.rsplit(":", 1)[1])

    def url(self, database: str = CONTAINER_DB) -> str:
        base = make_url(self.database_url)
        return base.set(
            host="127.0.0.1",
            port=self.port,
            username="postgres",
            password=None,
            database=database,
        ).render_as_string(hide_password=False)

    def wait_sql(self) -> None:
        deadline = time.monotonic() + PG_READY_S
        engine = create_engine(
            self.url("postgres"),
            poolclass=NullPool,
            connect_args={"connect_timeout": 2},
        )
        try:
            while True:
                try:
                    with engine.connect() as conn:
                        conn.execute(text("SELECT 1"))
                    return
                except OperationalError:
                    if time.monotonic() >= deadline:
                        pytest.fail(f"{self.name} not accepting SQL in {PG_READY_S} s")
                    time.sleep(0.25)
        finally:
            engine.dispose()

    def ensure_running(self) -> None:
        if not self.running():
            self.start()
        self.wait_sql()

    def setup(self) -> None:
        if not self.exists():
            self.create()
        elif not self.running():
            self.start()
        self.read_port()
        self.wait_sql()
        admin = create_engine(
            self.url("postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            with admin.connect() as conn:
                found = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"),
                    {"n": CONTAINER_DB},
                ).scalar()
                if not found:
                    conn.execute(text(f"CREATE DATABASE {CONTAINER_DB}"))
        finally:
            admin.dispose()
        env = dict(os.environ)
        env["DATABASE_URL"] = self.url()
        migrate = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if migrate.returncode != 0:
            pytest.fail(f"alembic upgrade failed:\n{migrate.stderr[-1500:]}")
        self.engine = create_engine(
            self.url(), pool_pre_ping=True, connect_args={"connect_timeout": 3}
        )
        self.truncate()

    def truncate(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE log_lines, attempts, jobs CASCADE"))
        self.baseline = None

    def teardown(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
        result = _docker("stop", "-t", "5", self.name, check=False)
        if result.returncode != 0:
            print(f"docker stop failed: {result.stderr.strip()[-300:]}")


@pytest.fixture(scope="module")
def private_pg(database_url):
    pg = PrivatePg(database_url)
    try:
        pg.setup()
        yield pg
    finally:
        pg.teardown()


@pytest.fixture
def pg(private_pg):
    """The container, running (an earlier failed test may have left it down)."""
    private_pg.ensure_running()
    return private_pg


def _readyz_ok(api) -> bool:
    return status_of(api.url, "/readyz", 2) == 200


def _survival(pg, launch_api, make_client, worker, wait_until, act) -> None:
    api = launch_api(pg.url())
    client = make_client(api.url)
    if pg.baseline is None:
        ids = build_five_jobs(client, pg.engine, worker, api, wait_until)
        pg.baseline = (ids, snapshot(client, ids), row_counts(pg.engine))
    ids, before, counts = pg.baseline
    pid = api.pid
    act()
    wait_until(lambda: _readyz_ok(api), PG_READY_S, 0.5, what="/readyz 200 again")
    assert api.pid == pid
    assert api.running()
    assert_same_snapshot(before, snapshot(client, ids))
    assert row_counts(pg.engine) == counts


def test_data_survives_docker_restart(pg, launch_api, make_client, worker, wait_until):
    _survival(
        pg,
        launch_api,
        make_client,
        worker,
        wait_until,
        lambda: _docker("restart", "-t", "2", pg.name),
    )


def test_data_survives_a_crash(pg, launch_api, make_client, worker, wait_until):
    def crash():
        _docker("kill", pg.name)
        _docker("start", pg.name)

    _survival(pg, launch_api, make_client, worker, wait_until, crash)


def test_job_running_through_a_container_restart_succeeds(
    pg, launch_api, make_client, worker, wait_until
):
    pg.truncate()
    api = launch_api(pg.url())
    client = make_client(api.url)
    w = worker(api)
    job_id = client.submit({"duration_s": 40})
    wait_first_log(pg.engine, wait_until, job_id)
    pids = (api.pid, w.pid)

    samples: list[tuple[float, bool]] = []
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            samples.append((time.monotonic(), _readyz_ok(api)))
            time.sleep(0.2)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        _docker("restart", "-t", "2", pg.name)

        def check():
            try:
                state = db_job(pg.engine, job_id)["state"]
            except OperationalError:
                return False
            if state in ("STALLED", "FAILED"):
                pytest.fail(f"job was {state} during the restart")
            return state == "SUCCEEDED"

        wait_until(check, 90, 0.25, what="the job to be SUCCEEDED")
    finally:
        stop.set()
        watcher.join(timeout=5)

    down = [t for t, ok in samples if not ok]
    outage_s = (max(down) - min(down) + 0.2) if down else 0.0
    print(f"/readyz was not 200 for about {outage_s:.1f} s")
    assert outage_s < 15, (
        "restart took longer than the lease; the premise of the test is broken"
    )
    assert_clean_job(client, job_id, 40)
    assert (api.pid, w.pid) == pids
    assert api.running()
    assert w.running()
