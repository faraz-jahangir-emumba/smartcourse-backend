"""Failed background work. See docs/SCHEMA.md section 8.

Part A lists "Failed Events / Workflow Issues" among the nine analytics metrics
(Core Functional Requirements section 5), so failures need somewhere to live
rather than only a log line that scrolls away.

A skeleton for now. Module 3 fills it in, when Kafka, Celery and Temporal exist
and there is something to fail. It is here already so the metric has a source
and the schema does not have to change later.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from smartcourse.infra.db.base import Base, UUIDMixin

# The three async mechanisms, each of which can fail in its own way.
FAILURE_SOURCES = ("kafka", "celery", "temporal")

FAILURE_STATUSES = ("pending", "retrying", "resolved", "abandoned")


class FailedEvent(UUIDMixin, Base):
    """Something that was supposed to happen in the background and did not."""

    __tablename__ = "failed_events"

    source: Mapped[str] = mapped_column(String(20), index=True)

    # The topic, task or workflow name - whatever identifies the specific piece
    # of work within its mechanism.
    source_name: Mapped[str] = mapped_column(String(200), index=True)

    # JSONB rather than Text because the three sources carry different shapes,
    # and jsonb can still be queried and indexed. Text would force every
    # question about the payload to be answered by reading it in Python.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    error: Mapped[str] = mapped_column(Text)

    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", index=True
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "source IN ('kafka', 'celery', 'temporal')",
            name="ck_failed_events_source_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'retrying', 'resolved', 'abandoned')",
            name="ck_failed_events_status_valid",
        ),
        CheckConstraint(
            "retry_count >= 0", name="ck_failed_events_retry_count_non_negative"
        ),
    )

    def __repr__(self) -> str:
        return f"<FailedEvent {self.source}:{self.source_name} {self.status}>"
