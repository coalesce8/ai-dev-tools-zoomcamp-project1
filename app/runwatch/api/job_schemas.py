"""Request and response schemas for jobs, shared by the job endpoints."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from runwatch.lifecycle import JobState

NAME_MAX_LENGTH = 100
DURATION_MIN_S = 1
DURATION_MAX_S = 600
MAX_ATTEMPTS_MIN = 1
MAX_ATTEMPTS_MAX = 10
MAX_ATTEMPTS_DEFAULT = 3


class WorkloadMode(StrEnum):
    NORMAL = "normal"
    ALWAYS_FAIL = "always_fail"


def _check_name(value: str) -> str:
    if not value.isprintable():
        raise ValueError("must contain only printable characters")
    if not value.strip():
        raise ValueError("must not be blank")
    return value


Name = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=NAME_MAX_LENGTH),
    AfterValidator(_check_name),
]


class WorkloadSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_s: int = Field(strict=True, ge=DURATION_MIN_S, le=DURATION_MAX_S)
    fail_probability: float = Field(
        default=0.0, strict=True, ge=0.0, le=1.0, allow_inf_nan=False
    )
    mode: WorkloadMode = WorkloadMode.NORMAL


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    spec: WorkloadSpec
    max_attempts: int = Field(
        default=MAX_ATTEMPTS_DEFAULT,
        strict=True,
        ge=MAX_ATTEMPTS_MIN,
        le=MAX_ATTEMPTS_MAX,
    )


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    spec: WorkloadSpec
    state: JobState
    max_attempts: int
    attempt_count: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
