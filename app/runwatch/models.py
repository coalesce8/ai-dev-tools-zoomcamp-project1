"""SQLAlchemy models for jobs, attempts and log lines.

The state and cause enums live in ``runwatch.lifecycle``; the CHECK
constraints below are built from their values. Importing this module opens
no database connection.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from runwatch.lifecycle import AttemptState, FailureCause, JobState

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum_type(enum_cls: type[StrEnum], length: int) -> Enum:
    """Store the enum's string value in a varchar; the CHECK is added apart."""
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


def _in_list(column: str, enum_cls: type[StrEnum]) -> str:
    values = ",".join(f"'{m.value}'" for m in enum_cls)
    return f"{column} IN ({values})"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100))
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[JobState] = mapped_column(
        _enum_type(JobState, 16), server_default=JobState.QUEUED.value
    )
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_attempt_number: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    trace_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="job", order_by="Attempt.number", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint(_in_list("state", JobState), name="state"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="max_attempts_range"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonneg"),
        CheckConstraint("last_attempt_number >= 0", name="last_attempt_number_nonneg"),
        Index(
            "ix_jobs_queued",
            "queued_at",
            "id",
            postgresql_where=text("state = 'QUEUED'"),
        ),
        Index("ix_jobs_created_at_id", text("created_at DESC"), text("id DESC")),
        Index(
            "ix_jobs_state_created_at_id",
            "state",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index("ix_jobs_finished_at", "finished_at"),
    )


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[AttemptState] = mapped_column(_enum_type(AttemptState, 16))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(Text)
    failure_cause: Mapped[FailureCause | None] = mapped_column(
        _enum_type(FailureCause, 16)
    )

    job: Mapped[Job] = relationship(back_populates="attempts")

    __table_args__ = (
        CheckConstraint("number >= 1", name="number_positive"),
        CheckConstraint(_in_list("state", AttemptState), name="state"),
        CheckConstraint(_in_list("failure_cause", FailureCause), name="failure_cause"),
        CheckConstraint(
            "(state = 'FAILED') = (failure_cause IS NOT NULL)", name="failed_has_cause"
        ),
        UniqueConstraint("job_id", "number", name="uq_attempts_job_id_number"),
        Index(
            "uq_attempts_one_running",
            "job_id",
            unique=True,
            postgresql_where=text("state = 'RUNNING'"),
        ),
        Index(
            "ix_attempts_running_lease",
            "lease_expires_at",
            postgresql_where=text("state = 'RUNNING'"),
        ),
        Index("ix_attempts_finished_at", "finished_at"),
    )


class LogLine(Base):
    __tablename__ = "log_lines"

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    attempt_number: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    line: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        ForeignKeyConstraint(
            ["job_id", "attempt_number"],
            ["attempts.job_id", "attempts.number"],
            ondelete="CASCADE",
            name="fk_log_lines_job_id_attempt_number_attempts",
        ),
        Index("ix_log_lines_job_id_attempt_number", "job_id", "attempt_number"),
    )
