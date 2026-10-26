"""Internal routes for workers and the agent, under `/internal/v1`."""

from fastapi import APIRouter

from runwatch.api import jobs_control, worker_report

router = APIRouter(prefix="/internal/v1")
router.include_router(worker_report.router)
router.include_router(jobs_control.internal_router)
