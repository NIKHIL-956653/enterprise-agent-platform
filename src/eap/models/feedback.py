"""
Answer feedback (EAP-63): one thumb per person per run.

A student says whether an answer helped. That is the pilot's only direct quality signal, so
it is a table of its own rather than a key in agent_runs.output: a run's output is what the
agent produced and must stay exactly that; a vote is what a reader thought of it, written
later, by someone else, and changeable.

Three shapes, all enforced by the database, not by the handler:

- One vote per (run, user). The unique constraint is the upsert target: changing your mind
  rewrites your row, it does not add one.
- A verdict is 'up' or 'down'. A CHECK, created by name in migration 0013, because a
  SQLAlchemy Enum with native_enum=False emits none - see models/meta.py.
- A vote belongs to the run's tenant. The foreign key is COMPOSITE, (tenant_id, run_id) to
  agent_runs (tenant_id, id): RLS pins tenant_id to the caller's tenant, so a run_id from
  another tenant has no matching parent and the insert fails before any policy has to decide
  anything. The handler's RLS-scoped lookup turns that into a 404; the key is the backstop.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from eap.models.base import Base
from eap.models.meta import UUIDPk

VERDICTS = ("up", "down")


class Feedback(UUIDPk, Base):
    __tablename__ = "feedback"

    __table_args__ = (
        UniqueConstraint("run_id", "user_id", name="uq_feedback_run_id_user_id"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.id"],
            name="fk_feedback_tenant_id_agent_runs",
            ondelete="CASCADE",
        ),
        CheckConstraint("verdict IN ('up', 'down')", name="verdict"),
    )

    # Also a plain FK to tenants, like every other tenant table, so deleting a tenant
    # removes its votes even if its runs were removed first.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # NOT NULL: an anonymous vote would make the unique constraint meaningless (NULLs are
    # distinct). CASCADE rather than SET NULL for the same reason - a vote with no voter
    # cannot be told apart from the next one.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    verdict: Mapped[str] = mapped_column(String(8))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The time of the LATEST vote: the upsert resets it, so "when did they say this" is
    # always true of what the row says now.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    def __repr__(self) -> str:
        return f"<Feedback {self.verdict} run={self.run_id} user={self.user_id}>"
