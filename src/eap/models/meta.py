"""Reusable column mixins: UUID primary keys and created/updated timestamps."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Timestamps:
    # timezone=True -> Postgres "timestamptz". Without it Postgres stores a wall-clock
    # reading with no zone attached, so the same instant recorded in Dubai, in CI (UTC)
    # and in Lagos becomes three different unreconcilable values.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
