"""Public API routes, under `/api/v1`."""

from fastapi import APIRouter

from runwatch.api import jobs_control, jobs_submit

router = APIRouter(prefix="/api/v1")
router.include_router(jobs_submit.router)
router.include_router(jobs_control.public_router)
