"""Reusable column pieces: UUID primary keys, created/updated timestamps, the language type."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from eap.i18n import Language


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


def language_type() -> Enum:
    """Column type for a Language: a VARCHAR(8) holding the code ('en', 'ar').

    One definition shared by every language column - tenants, users, agent_runs,
    document_chunks - so they cannot drift apart. Three arguments carry the weight:

    - native_enum=False: a VARCHAR, never a Postgres ENUM type, for the same reason as every
      other enum column here - widening a CHECK is trivial, altering a PG enum is not.
    - length=8: must equal LENGTH in migration 0011. Left alone, SQLAlchemy sizes the VARCHAR
      to the longest current value (2), and `alembic check` reports every language column
      as drifted from the database.
    - values_callable: store the code 'en', not the member name 'EN'. The CHECKs in 0011
      accept codes only, so storing names would fail every insert.

    The CHECK constraints live in migration 0011, created by name, and not here: SQLAlchemy
    2.x Enum(native_enum=False) emits none unless create_constraint=True, and declaring the
    rule in both places would give it two copies that can disagree.
    """
    return Enum(
        Language,
        native_enum=False,
        length=8,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )
