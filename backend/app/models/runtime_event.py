from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class RuntimeEventRecord(Base):
    __tablename__ = "runtime_events"
    __table_args__ = (
        CheckConstraint("byte_size > 0 AND byte_size <= 8192", name="ck_runtime_event_size"),
        Index("ix_runtime_events_occurred_at", "occurred_at", "id"),
        Index("ix_runtime_events_request_id", "request_id", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    module: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details_json: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(Integer)
