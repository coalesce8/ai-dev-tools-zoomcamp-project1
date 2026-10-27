"""AC-3, AC-5, AC-7, AC-8 and AC-12 through a real API and real workers.

Each test starts its own API (sweeper on) and workers, submits jobs through
`POST /api/v1/jobs` and reads state through the public API. The database is
read directly only where a test says so.
"""

import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from runwatch.models import Attempt, Job
from sqlalchemy import select

NORMAL_3S = {"duration_s": 3, "fail_probability": 0, "mode": "normal"}


class Api:
    """A small client for the public and internal API of one server."""

    def __init__(self, server, wait_until):
        self.server = server
        self.wait_until = wait_until
        self.http = httpx.Client(base_url=server.url, timeout=10)

    def submit(self, spec=None, max_attempts=None, name="e2e"):
        body = {"name": name, "spec": spec or NORMAL_3S}
        if max_attempts is not None:
            body["max_attempts"] = max_attempts
        response = self.http.post("/api/v1/jobs", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def job(self, job_id):
        response = self.http.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def logs(self, job_id):
        response = self.http.get(f"/api/v1/jobs/{job_id}/logs")
        assert response.status_code == 200, response.text
        return response.json()["items"]

    def post(self, path):
        return self.http.post(path)

    def wait_state(self, job_id, state, timeout):
        return self.wait_until(
            lambda: (j := self.job(job_id))["state"] == state and j,
            timeout,
            what=f"job {job_id} to be {state}",
        )

    def wait_first_log(self, job_id, timeout=10):
        return self.wait_until(
            lambda: self.logs(job_id), timeout, what=f"job {job_id} first log line"
        )


@pytest.fixture
def api(api_server, wait_until):
    return Api(api_server(), wait_until)


def numbers(job):
    return [a["number"] for a in job["attempts"]]


def hold(seconds, check):
    """Check a condition repeatedly for `seconds` (a bounded negative wait)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check()
        time.sleep(0.5)
    check()


def test_ac3_normal_job_succeeds(api, worker):
    w = worker(api.server)
    job = api.submit({"duration_s": 3, "fail_probability": 0, "mode": "normal"})
    done = api.wait_state(job["id"], "SUCCEEDED", 13)
    assert done["attempt_count"] == 1
    assert done["finished_at"] is not None
    (attempt,) = done["attempts"]
    assert attempt["number"] == 1
    assert attempt["state"] == "SUCCEEDED"
    assert attempt["worker_id"] == w.worker_id
    assert attempt["failure_cause"] is None
    lines = api.logs(job["id"])
    assert [(x["seq"], x["attempt_number"], x["line"]) for x in lines] == [
        (1, 1, "step 1/3"),
        (2, 1, "step 2/3"),
        (3, 1, "step 3/3"),
    ]


def test_ac5_always_fail_uses_all_attempts(api, worker):
    worker(api.server)
    spec = {"duration_s": 1, "mode": "always_fail"}
    job = api.submit(spec, max_attempts=3)
    done = api.wait_state(job["id"], "FAILED", 20)

    def check(done):
        assert numbers(done) == [1, 2, 3]
        assert [a["state"] for a in done["attempts"]] == ["FAILED"] * 3
        assert [a["failure_cause"] for a in done["attempts"]] == ["workload"] * 3
        assert {a["exit_reason"] for a in done["attempts"]} == {"always_fail mode"}
        assert done["last_error"] == "always_fail mode"
        assert done["attempt_count"] == 3

    check(done)
    hold(3, lambda: check(api.job(job["id"])) or None)
    assert api.job(job["id"])["state"] == "FAILED"


def test_ac7_requeue_from_failed(api, worker):
    infra = worker(api.server, fault="fail_all")
    spec = {"duration_s": 1, "fail_probability": 0, "mode": "normal"}
    job = api.submit(spec, max_attempts=2)
    failed = api.wait_state(job["id"], "FAILED", 20)
    assert numbers(failed) == [1, 2]
    assert [a["failure_cause"] for a in failed["attempts"]] == ["infra", "infra"]
    infra.stop()
    assert not infra.running()

    response = api.post(f"/api/v1/jobs/{job['id']}/requeue")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "QUEUED"
    assert response.json()["attempt_count"] == 0
    again = api.post(f"/api/v1/jobs/{job['id']}/requeue")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"

    worker(api.server)
    # The requeued job keeps its spec; a 3 s run is checked against a 13 s limit.
    done = api.wait_state(job["id"], "SUCCEEDED", 13)
    assert numbers(done) == [1, 2, 3]
    assert [a["state"] for a in done["attempts"]] == ["FAILED", "FAILED", "SUCCEEDED"]
    assert [a["failure_cause"] for a in done["attempts"][:2]] == ["infra", "infra"]
    assert done["attempt_count"] == 1
    lines = api.logs(job["id"])
    assert [x["seq"] for x in lines] == sorted(x["seq"] for x in lines)
    assert {x["attempt_number"] for x in lines} == {1, 2, 3}
    assert [x["line"] for x in lines if x["attempt_number"] == 3] == ["step 1/1"]

    assert api.post(f"/api/v1/jobs/{job['id']}/requeue").status_code == 409

    running = api.submit(NORMAL_3S)
    api.wait_first_log(running["id"])
    assert api.job(running["id"])["state"] == "RUNNING"
    response = api.post(f"/api/v1/jobs/{running['id']}/requeue")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    final = api.wait_state(running["id"], "SUCCEEDED", 13)
    assert numbers(final) == [1]


def test_ac7_requeue_from_stalled(api, worker):
    first = worker(api.server)
    job = api.submit({"duration_s": 10, "fail_probability": 0, "mode": "normal"})
    api.wait_first_log(job["id"])
    first.stop()
    stalled = api.wait_state(job["id"], "STALLED", 25)
    (attempt,) = stalled["attempts"]
    assert attempt["state"] == "FAILED"
    assert attempt["failure_cause"] == "infra"
    assert attempt["worker_id"] == first.worker_id

    def still_stalled():
        current = api.job(job["id"])
        assert current["state"] == "STALLED"
        assert numbers(current) == [1]

    hold(3, still_stalled)

    response = api.post(f"/api/v1/jobs/{job['id']}/requeue")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "QUEUED"
    assert response.json()["attempt_count"] == 0

    # The new worker runs the job's own 10 s spec, so the limit is 10 s + 10 s.
    second = worker(api.server)
    assert second.worker_id != first.worker_id
    done = api.wait_state(job["id"], "SUCCEEDED", 20)
    one, two = done["attempts"]
    assert (one["number"], one["state"], one["failure_cause"]) == (1, "FAILED", "infra")
    assert one["worker_id"] == first.worker_id
    assert (two["number"], two["state"]) == (2, "SUCCEEDED")
    assert two["worker_id"] == second.worker_id
    assert done["attempt_count"] == 1


def test_ac8_cancel_running_job(api, worker, db_session):
    w = worker(api.server)
    long_job = api.submit({"duration_s": 30, "fail_probability": 0, "mode": "normal"})
    api.wait_first_log(long_job["id"])
    assert api.job(long_job["id"])["state"] == "RUNNING"
    cancelled = api.post(f"/api/v1/jobs/{long_job['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "CANCELLED"
    short = api.submit({"duration_s": 1, "fail_probability": 0, "mode": "normal"})
    api.wait_state(short["id"], "SUCCEEDED", 10)

    db_session.expire_all()
    first_job = db_session.get(Job, uuid.UUID(long_job["id"]))
    second_attempt = db_session.scalars(
        select(Attempt).where(Attempt.job_id == uuid.UUID(short["id"]))
    ).one()
    assert second_attempt.worker_id == w.worker_id
    stopped_after = second_attempt.started_at - first_job.finished_at
    assert stopped_after.total_seconds() <= 2, stopped_after

    detail = api.job(long_job["id"])
    assert detail["state"] == "CANCELLED"
    (attempt,) = detail["attempts"]
    assert attempt["state"] == "CANCELLED"
    assert attempt["exit_reason"] == "cancelled"
    cancel_db = first_job.finished_at
    for line in api.logs(long_job["id"]):
        if line["attempt_number"] == 1:
            ts = datetime.fromisoformat(line["ts"])
            assert (ts - cancel_db).total_seconds() <= 2, line

    before = api.logs(long_job["id"])
    attempt_id = attempt["id"]
    result = api.http.post(
        f"/internal/v1/worker/attempts/{attempt_id}/result",
        json={"outcome": "succeeded"},
    )
    assert result.status_code == 409, result.text
    beat = api.http.post(
        f"/internal/v1/worker/attempts/{attempt_id}/heartbeat",
        json={"lines": [{"ts": datetime.now(UTC).isoformat(), "line": "late"}]},
    )
    assert beat.status_code == 409, beat.text
    after = api.job(long_job["id"])
    assert after["state"] == "CANCELLED"
    assert after["attempts"][0]["state"] == "CANCELLED"
    assert api.logs(long_job["id"]) == before


def test_ac8_cancel_queued_job_is_never_run(api, worker):
    job = api.submit()
    response = api.post(f"/api/v1/jobs/{job['id']}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "CANCELLED"
    worker(api.server)

    def untouched():
        current = api.job(job["id"])
        assert current["state"] == "CANCELLED"
        assert current["attempts"] == []

    hold(3, untouched)


def test_ac12_requeue_failed_window(api, worker, db_now):
    spec = {"duration_s": 1, "fail_probability": 0, "mode": "normal"}
    fail_spec = {"duration_s": 1, "mode": "always_fail"}
    infra = worker(api.server, fault="fail_all")
    j0 = api.submit(spec, max_attempts=1)
    api.wait_state(j0["id"], "FAILED", 10)
    since = db_now()
    j1 = api.submit(spec, max_attempts=1)
    j2 = api.submit(spec, max_attempts=1)
    api.wait_state(j1["id"], "FAILED", 15)
    api.wait_state(j2["id"], "FAILED", 15)
    infra.stop()
    assert not infra.running()

    worker(api.server)
    j3 = api.submit(fail_spec, max_attempts=1)
    api.wait_state(j3["id"], "FAILED", 10)
    j4 = api.submit(spec, max_attempts=1)
    api.wait_state(j4["id"], "SUCCEEDED", 10)
    until = db_now()

    window = {"since": since.isoformat(), "until": until.isoformat()}
    response = api.http.post("/internal/v1/jobs/requeue-failed", json=window)
    assert response.status_code == 200, response.text
    assert response.json() == {"requeued": 2}

    for job in (j1, j2):
        done = api.wait_state(job["id"], "SUCCEEDED", 15)
        first, second = done["attempts"]
        assert (first["number"], first["state"]) == (1, "FAILED")
        assert first["failure_cause"] == "infra"
        assert (second["number"], second["state"]) == (2, "SUCCEEDED")
    for job, state, cause in ((j0, "FAILED", "infra"), (j3, "FAILED", "workload")):
        current = api.job(job["id"])
        assert current["state"] == state
        assert numbers(current) == [1]
        assert current["attempts"][0]["failure_cause"] == cause
    current = api.job(j4["id"])
    assert current["state"] == "SUCCEEDED"
    assert numbers(current) == [1]

    again = api.http.post("/internal/v1/jobs/requeue-failed", json=window)
    assert again.status_code == 200, again.text
    assert again.json() == {"requeued": 0}
