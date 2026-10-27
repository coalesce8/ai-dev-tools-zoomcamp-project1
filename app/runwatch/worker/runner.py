"""The worker loop: poll, run one attempt at a time, report.

Single-threaded and synchronous. Time, sleeping and randomness are injected so
tests run on a fake clock. Logs name only job_id, attempt_id, attempt_number
and worker_id: never the spec, a log line or a result reason.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from runwatch.api.job_schemas import WorkloadMode, WorkloadSpec
from runwatch.lifecycle import FailureKind, failure_cause_for
from runwatch.worker.client import (
    ApiUnavailable,
    AttemptNotCurrent,
    Claim,
    LogLine,
    WorkerApi,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1
STEP_S = 1
HEARTBEAT_GIVE_UP_S = 15
RESULT_RETRY_S = 1
RESULT_GIVE_UP_S = 30
MAX_LINES_PER_HEARTBEAT = 100
FAULT_DELAY_S = 1

FAULT_ENV = "WORKER_FAULT"
FAULT_FAIL_ALL = "fail_all"
FAULT_LINE = "injected fault: fail_all"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _Ended(Exception):
    """Internal: the attempt ends without a result (cancelled, abandoned, stopped)."""


class Worker:
    def __init__(
        self,
        api: WorkerApi,
        worker_id: str,
        worker_fault: str | None = None,
        stop: threading.Event | None = None,
        sleep: Callable[[float], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = _utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self.api = api
        self.worker_id = worker_id
        self.stop = stop if stop is not None else threading.Event()
        self.sleep = sleep if sleep is not None else self.stop.wait
        self.monotonic = monotonic
        self.utcnow = utcnow
        self.rng = rng if rng is not None else random.Random()
        self.fail_all = worker_fault == FAULT_FAIL_ALL
        if worker_fault and not self.fail_all:
            logger.warning("%s has an unknown value; running with no fault", FAULT_ENV)
        self._claims_failing = False

    def run(self) -> None:
        while not self.stop.is_set():
            claim = self._claim()
            if claim is None:
                if self.stop.is_set():
                    break
                self.sleep(POLL_INTERVAL_S)
                continue
            self._attempt(claim)

    def _claim(self) -> Claim | None:
        try:
            claim = self.api.claim(self.worker_id)
        except ApiUnavailable:
            if not self._claims_failing:
                self._claims_failing = True
                logger.warning(
                    "claim failing: API unavailable worker_id=%s", self.worker_id
                )
            return None
        except Exception as exc:
            logger.error(
                "claim response unusable error=%s worker_id=%s",
                type(exc).__name__,
                self.worker_id,
            )
            return None
        if self._claims_failing:
            self._claims_failing = False
            logger.info("claim working again worker_id=%s", self.worker_id)
        return claim

    # -- one attempt ------------------------------------------------------

    def _attempt(self, claim: Claim) -> None:
        ctx = (
            f"job_id={claim.job_id} attempt_id={claim.attempt_id} "
            f"attempt_number={claim.attempt_number} worker_id={self.worker_id}"
        )
        logger.info("attempt started %s", ctx)
        try:
            try:
                outcome = self._work(claim)
            except _Ended:
                logger.info("attempt ended without a result %s", ctx)
                return
            except Exception as exc:
                name = type(exc).__name__
                logger.error("attempt raised error=%s %s", name, ctx)
                cause = failure_cause_for(FailureKind.WORKER_EXCEPTION)
                outcome = ("failed", f"worker error: {name}", cause)
            self._send_result(claim, outcome, ctx)
        except _Ended:
            logger.info("attempt ended without a result %s", ctx)
        except Exception as exc:
            logger.error("attempt handling failed error=%s %s", type(exc).__name__, ctx)

    def _work(self, claim: Claim):
        """Run the steps; return the result as (outcome, reason, cause)."""
        spec = claim.spec
        if not isinstance(spec, WorkloadSpec):
            spec = WorkloadSpec.model_validate(spec)
        buffer: list[LogLine] = []
        state = {"last_ok": self.monotonic()}

        if self.fail_all:
            self._sleep_checked(FAULT_DELAY_S)
            buffer.append(LogLine(self.utcnow(), FAULT_LINE))
            self._flush(claim, buffer, state)
            self._drain(claim, buffer, state)
            cause = failure_cause_for(FailureKind.INJECTED_FAULT)
            return ("failed", FAULT_LINE, cause)

        steps = spec.duration_s
        for i in range(1, steps + 1):
            self._sleep_checked(STEP_S)
            buffer.append(LogLine(self.utcnow(), f"step {i}/{steps}"))
            self._flush(claim, buffer, state)
        self._drain(claim, buffer, state)

        if spec.mode == WorkloadMode.ALWAYS_FAIL:
            cause = failure_cause_for(FailureKind.ALWAYS_FAIL)
            return ("failed", "always_fail mode", cause)
        if self.rng.random() < spec.fail_probability:
            cause = failure_cause_for(FailureKind.PROBABILITY_ROLL)
            reason = f"random failure (fail_probability={spec.fail_probability})"
            return ("failed", reason, cause)
        return ("succeeded", None, None)

    def _sleep_checked(self, seconds: float) -> None:
        self.sleep(seconds)
        if self.stop.is_set():
            raise _Ended

    def _flush(self, claim: Claim, buffer: list[LogLine], state: dict) -> None:
        """Send buffered lines (at most 100 per request), oldest first."""
        while buffer:
            chunk = buffer[:MAX_LINES_PER_HEARTBEAT]
            try:
                self.api.heartbeat(claim.attempt_id, chunk)
            except AttemptNotCurrent:
                logger.info(
                    "attempt no longer current job_id=%s attempt_id=%s",
                    claim.job_id,
                    claim.attempt_id,
                )
                raise _Ended from None
            except ApiUnavailable:
                if self.monotonic() - state["last_ok"] >= HEARTBEAT_GIVE_UP_S:
                    logger.warning(
                        "abandoning attempt: no heartbeat for %ss "
                        "job_id=%s attempt_id=%s",
                        HEARTBEAT_GIVE_UP_S,
                        claim.job_id,
                        claim.attempt_id,
                    )
                    raise _Ended from None
                return
            del buffer[: len(chunk)]
            state["last_ok"] = self.monotonic()

    def _drain(self, claim: Claim, buffer: list[LogLine], state: dict) -> None:
        """After the last step, keep retrying until every line is sent."""
        while buffer:
            self._sleep_checked(STEP_S)
            self._flush(claim, buffer, state)

    # -- result -----------------------------------------------------------

    def _send_result(self, claim: Claim, outcome, ctx: str) -> None:
        name, reason, cause = outcome
        first = self.monotonic()
        while True:
            try:
                self.api.result(claim.attempt_id, name, reason, cause)
            except AttemptNotCurrent:
                logger.info("result not accepted: attempt not current %s", ctx)
                return
            except ApiUnavailable:
                if self.monotonic() - first >= RESULT_GIVE_UP_S:
                    logger.warning(
                        "giving up on result after %ss %s", RESULT_GIVE_UP_S, ctx
                    )
                    return
                self._sleep_checked(RESULT_RETRY_S)
            else:
                logger.info("result sent %s", ctx)
                return
