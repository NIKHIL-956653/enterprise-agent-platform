"""
The audit log: append-only, per-tenant hash chain.

This is a different kind of table from everything else in models/. agent_runs is a record
the application maintains; audit_log is evidence the application cannot quietly change.
Three properties, all enforced below the application:

- eap_app holds INSERT and SELECT but not UPDATE or DELETE (revoked in migration 0009).
- seq, prev_hash and hash are computed by a database trigger, never by Python. Each hash
  covers the previous one, so an edit or a deletion breaks every link after it.
- The chain is per tenant, because a tenant must be able to verify their own trail under
  RLS without seeing anyone else's rows. A global chain would be unverifiable here.

Writes go through eap.audit.record(), reads and verification through eap.audit. The ORM
model exists for Alembic's metadata comparison and typed reads - not for inserts.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from eap.models.base import Base


class AuditEvent(Base):
    __tablename__ = "audit_log"

    __table_args__ = (UniqueConstraint("tenant_id", "seq", name="uq_audit_log_tenant_id_seq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    # RESTRICT, not CASCADE: deleting a tenant must not silently shred their audit trail.
    # Offboarding handles the trail deliberately or not at all.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    seq: Mapped[int] = mapped_column(BigInteger)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    def __repr__(self) -> str:
        return f"<AuditEvent {self.event_type} tenant={self.tenant_id} seq={self.seq}>"
