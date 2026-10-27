"""AC-4 and AC-6 with real processes: two workers, and a killed worker.

Every test starts its own API (sweeper on) and workers through #18's fixtures,
submits jobs through `POST /api/v1/jobs` and reads state through the public API.
Timestamps are compared with other database timestamps or `db_now()`, never
with the test machine's clock. Waits poll with a ceiling; the only fixed waits
are the stated "stays that way for N s" checks.
"""

import os
import signal
import threading
import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

FAST = {"duration_s": 2, "fail_probability": 0, "mode": "normal"}
JOBS = 20
LEASE_BOUND_S = 25


class Client:
    """The public API of one server."""

    def __init__(self, server):
        self.server = server
        self.http = httpx.Client(base_url=server.url, timeout=10)

    def submit(self, spec):
        response = self.http.post("/api/v1/jobs", json={"name": "conc", "spec": spec})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def job(self, job_id):
        response = self.http.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def logs(self, job_id):
        response = self.http.get(f"/api/v1/jobs/{job_id}/logs")
        assert response.status_code == 200, response.text
        return response.json()["items"]

    def jobs(self):
        response = self.http.get("/api/v1/jobs", params={"limit": 100})
        assert response.status_code == 200, response.text
        return response.json()["items"]


@pytest.fixture
def make_client(api_server):
    def make():
        return Client(api_server(sweeper_enabled=True))

    return make


def snapshot(client, job_id):
    """The job row, its attempt rows and its log lines, as the API shows them."""
    return client.job(job_id), client.logs(job_id)


def db_attempts(db_engine, job_id):
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT number, worker_id, state, failure_cause, exit_reason, "
                "started_at, finished_at FROM attempts WHERE job_id = :j "
                "ORDER BY number"
            ),
            {"j": job_id},
        ).mappings()
        return [dict(r) for r in rows]


def count_attempts(db_engine):
    with db_engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM attempts")).scalar_one()


def hold(seconds, check, interval=0.5):
    """Run `check` repeatedly for `seconds` (a bounded negative wait)."""
    deadline = time.monotonic() + seconds
    while True:
        check()
        if time.monotonic() >= deadline:
            return
        time.sleep(interval)


def wait_state(wait_until, client, job_id, state, timeout, forbid=()):
    """Wait for a job state. `forbid` is [(job_id, state)] never to be seen."""

    def check():
        for other, bad in forbid:
            seen = client.job(other)["state"]
            assert seen != bad, f"job {other} was {bad}"
        job = client.job(job_id)
        return job if job["state"] == state else None

    return wait_until(check, timeout, 0.25, what=f"job {job_id} to be {state}")


def wait_running_worker(wait_until, client, job_id, timeout=20):
    """Wait until the job is RUNNING; return the worker_id of its attempt."""

    def check():
        job = client.job(job_id)
        if job["state"] == "RUNNING" and job["attempts"]:
            return job["attempts"][-1]["worker_id"]
        return None

    return wait_until(check, timeout, 0.1, what=f"job {job_id} to be RUNNING")


def wait_first_log(wait_until, client, job_id, timeout=20):
    return wait_until(
        lambda: client.logs(job_id), timeout, 0.1, what=f"job {job_id} first log"
    )


class Sampler(threading.Thread):
    """Every 50 ms, look for a job or a worker with two RUNNING attempts."""

    BY_JOB = (
        "SELECT job_id, count(*) FROM attempts WHERE state = 'RUNNING' "
        "GROUP BY job_id HAVING count(*) > 1"
    )
    BY_WORKER = (
        "SELECT worker_id, count(*) FROM attempts WHERE state = 'RUNNING' "
        "GROUP BY worker_id HAVING count(*) > 1"
    )
    TOTAL = "SELECT count(*) FROM attempts WHERE state = 'RUNNING'"

    def __init__(self, engine):
        super().__init__(daemon=True)
        self.engine = engine
        self.halt = threading.Event()
        self.samples = 0
        self.two_running = 0
        self.violations = []
        self.error = None

    def run(self):
        try:
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as conn:
                while not self.halt.is_set():
                    for query in (self.BY_JOB, self.BY_WORKER):
                        rows = conn.execute(text(query)).all()
                        if rows:
                            self.violations.append((query, rows))
                    if conn.execute(text(self.TOTAL)).scalar_one() == 2:
                        self.two_running += 1
                    self.samples += 1
                    self.halt.wait(0.05)
        except Exception as exc:  # reported by finish()
            self.error = exc

    def finish(self):
        self.halt.set()
        self.join(10)
        assert self.error is None, f"sampler failed: {self.error!r}"
        assert not self.violations, f"two RUNNING at once: {self.violations[:3]}"
        assert self.samples >= 100, f"only {self.samples} samples"
        assert self.two_running >= 1, "the sampler never saw 2 RUNNING attempts"


def run_two_workers(
    db_engine, wait_until, clients, workers_of, *, timeout=70, fast=FAST
):
    """Submit JOBS jobs to clients[0] (no worker yet), start two workers, wait.

    `workers_of` starts the two workers and returns their handles. Returns
    (job ids, worker handles). The sampler's checks are made before returning.
    """
    submit_client = clients[0]
    ids = [submit_client.submit(fast) for _ in range(JOBS)]
    assert {j["state"] for j in submit_client.jobs()} == {"QUEUED"}
    sampler = Sampler(db_engine)
    sampler.start()
    try:
        handles = workers_of()

        def check():
            jobs = submit_client.jobs()
            states = [j["state"] for j in jobs]
            assert "STALLED" not in states, "a job was STALLED"
            assert "FAILED" not in states, "a job FAILED"
            return len(jobs) == JOBS and set(states) == {"SUCCEEDED"}

        wait_until(check, timeout, 0.25, what=f"all {JOBS} jobs SUCCEEDED")
    finally:
        sampler.halt.set()
    sampler.finish()
    return ids, handles


def check_one_attempt_each(db_engine, client, ids, handles):
    ran = {h.worker_id: 0 for h in handles}
    for job_id in ids:
        job = client.job(job_id)
        assert job["state"] == "SUCCEEDED"
        assert job["attempt_count"] == 1
        (attempt,) = job["attempts"]
        assert attempt["number"] == 1
        assert attempt["state"] == "SUCCEEDED"
        assert attempt["failure_cause"] is None
        assert attempt["worker_id"] in ran
        ran[attempt["worker_id"]] += 1
        lines = client.logs(job_id)
        assert [(x["seq"], x["attempt_number"], x["line"]) for x in lines] == [
            (1, 1, "step 1/2"),
            (2, 1, "step 2/2"),
        ]
    assert set(ran) == {h.worker_id for h in handles}
    assert all(n >= 5 for n in ran.values()), ran

    # Intervals, from database timestamps only.
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT worker_id, state, started_at, finished_at FROM attempts "
                "ORDER BY started_at"
            )
        ).all()
    assert len(rows) == JOBS
    assert all(r.state == "SUCCEEDED" for r in rows), "an attempt is not SUCCEEDED"
    by_worker = {}
    for r in rows:
        by_worker.setdefault(r.worker_id, []).append(r)
    for worker_id, attempts in by_worker.items():
        for prev, nxt in zip(attempts, attempts[1:], strict=False):
            assert nxt.started_at >= prev.finished_at, (
                f"worker {worker_id} ran two attempts at once"
            )
    first, second = by_worker.values()
    assert any(
        a.started_at < b.finished_at and b.started_at < a.finished_at
        for a in first
        for b in second
    ), "the two workers never overlapped"
    assert len(client.jobs()) == JOBS


def test_two_workers_twenty_jobs(db_engine, wait_until, make_client, worker):
    client = make_client()

    def start():
        return [worker(client.server), worker(client.server)]

    ids, handles = run_two_workers(db_engine, wait_until, [client], start)
    check_one_attempt_each(db_engine, client, ids, handles)


def test_two_api_replicas(db_engine, wait_until, make_client, worker):
    first, second = make_client(), make_client()

    def start():
        return [worker(first.server), worker(second.server)]

    ids, handles = run_two_workers(db_engine, wait_until, [first, second], start)
    check_one_attempt_each(db_engine, first, ids, handles)
    # Both replicas show the same jobs.
    assert {j["id"] for j in second.jobs()} == set(ids)


# -- AC-6: a killed worker ----------------------------------------------------


def assert_infra_stall(db_engine, db_now, client, job_id, worker_id, t_kill):
    job = client.job(job_id)
    assert job["state"] == "STALLED"
    assert job["attempt_count"] == 1
    assert job["last_error"] == "lease expired"
    (attempt,) = db_attempts(db_engine, job_id)
    assert attempt["number"] == 1
    assert attempt["state"] == "FAILED"
    assert attempt["failure_cause"] == "infra"
    assert attempt["exit_reason"] == "lease expired"
    assert attempt["worker_id"] == worker_id
    assert attempt["finished_at"] <= t_kill + timedelta(seconds=LEASE_BOUND_S)


def test_sigkill_mid_run(db_engine, db_now, wait_until, make_client, worker):
    client = make_client()
    w1 = worker(client.server)
    j1 = client.submit({"duration_s": 30})
    j2 = client.submit({"duration_s": 1})
    wait_first_log(wait_until, client, j1)
    w1.kill()
    t_kill = db_now()
    w2 = worker(client.server)

    # Not early: the lease is 15 s and the last heartbeat was moments ago.
    def not_early():
        assert client.job(j1)["state"] == "RUNNING", "stalled before 5 s"
        return db_now() - t_kill >= timedelta(seconds=5)

    wait_until(not_early, 10, 0.25, what="5 s after the kill")

    wait_state(wait_until, client, j1, "STALLED", LEASE_BOUND_S)
    assert_infra_stall(db_engine, db_now, client, j1, w1.worker_id, t_kill)

    # The queue is not wedged: W2 ran J2.
    done = wait_state(wait_until, client, j2, "SUCCEEDED", 30)
    assert done["attempt_count"] == 1
    (attempt,) = done["attempts"]
    assert attempt["number"] == 1
    assert attempt["worker_id"] == w2.worker_id

    # Not requeued, with an idle worker available.
    before = snapshot(client, j1)
    attempts_before = count_attempts(db_engine)
    assert attempts_before == 2

    def unchanged():
        assert snapshot(client, j1) == before
        assert count_attempts(db_engine) == attempts_before

    hold(10, unchanged)
    assert w2.running()


def test_only_killed_workers_job_stalls(
    db_engine, db_now, wait_until, make_client, worker
):
    client = make_client()
    w1, w2 = worker(client.server), worker(client.server)
    by_id = {w1.worker_id: w1, w2.worker_id: w2}
    spec = {"duration_s": 20, "fail_probability": 0, "mode": "normal"}
    a, b = client.submit(spec), client.submit(spec)
    owner_a = wait_running_worker(wait_until, client, a)
    owner_b = wait_running_worker(wait_until, client, b)
    assert owner_a != owner_b
    by_id[owner_a].kill()
    t_kill = db_now()

    wait_state(wait_until, client, a, "STALLED", LEASE_BOUND_S, forbid=[(b, "STALLED")])
    assert_infra_stall(db_engine, db_now, client, a, owner_a, t_kill)

    done = wait_state(wait_until, client, b, "SUCCEEDED", 30)
    assert done["attempt_count"] == 1
    (attempt,) = done["attempts"]
    assert attempt["number"] == 1
    assert attempt["worker_id"] == owner_b

    before = snapshot(client, a)

    def unchanged():
        assert snapshot(client, a) == before
        assert count_attempts(db_engine) == 2

    hold(3, unchanged)
    assert by_id[owner_b].running()


def test_kill_before_first_heartbeat(
    db_engine, db_now, wait_until, make_client, worker
):
    client = make_client()
    w1 = worker(client.server)
    job = client.submit({"duration_s": 30})
    wait_running_worker(wait_until, client, job)
    w1.kill()
    t_kill = db_now()
    wait_state(wait_until, client, job, "STALLED", LEASE_BOUND_S)
    assert_infra_stall(db_engine, db_now, client, job, w1.worker_id, t_kill)


# -- a frozen worker that comes back ------------------------------------------


def test_sigstop_sweep_sigcont(db_engine, db_now, wait_until, make_client, worker):
    client = make_client()
    w1 = worker(client.server)
    job = client.submit({"duration_s": 40})
    wait_first_log(wait_until, client, job)
    os.kill(w1.pid, signal.SIGSTOP)
    try:
        t_stop = db_now()
        wait_state(wait_until, client, job, "STALLED", LEASE_BOUND_S)
        assert_infra_stall(db_engine, db_now, client, job, w1.worker_id, t_stop)
        dump = snapshot(client, job)
    finally:
        os.kill(w1.pid, signal.SIGCONT)

    def unchanged():
        assert snapshot(client, job) == dump

    hold(8, unchanged)

    second = client.submit({"duration_s": 1})
    done = wait_state(wait_until, client, second, "SUCCEEDED", 15)
    (attempt,) = done["attempts"]
    assert attempt["worker_id"] == w1.worker_id
    assert snapshot(client, job) == dump
