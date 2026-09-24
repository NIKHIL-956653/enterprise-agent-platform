"""
Agent registration and run history.

Two separate ideas:
- Agent      = a tenant has ENABLED a given agent, with its own config. The agent's actual
               code lives in the repo; this row is the tenant's subscription to it.
- AgentRun   = one execution. Append-only. This table is how cost, latency and failure
               become visible per tenant, which is half the reason the platform exists.
"""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from eap.models.base import Base
from eap.models.meta import Timestamps, UUIDPk


class RunStatus(enum.StrEnum):
    # QUEUED exists from day one even though v1 runs synchronously. When M4 moves runs onto
    # a Redis queue, the state machine is already correct and no migration is needed.
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


def _enum_col(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=20,
        values_callable=lambda e: [m.value for m in e],
    )


class Agent(UUIDPk, Timestamps, Base):
    __tablename__ = "agents"

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_id_name"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Matches a key in the code-side registry, e.g. "doc-writer". Not free text: if no
    # implementation is registered under this name, the run fails fast at dispatch.
    name: Mapped[str] = mapped_column(String(63))

    display_name: Mapped[str] = mapped_column(String(200))

    # Per-tenant settings for this agent - model choice, prompt overrides, destination ids.
    # JSONB rather than columns because every agent wants different keys, and adding an
    # agent must not require a migration.
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    is_enabled: Mapped[bool] = mapped_column(default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<Agent {self.name} tenant={self.tenant_id}>"


class AgentRun(UUIDPk, Timestamps, Base):
    __tablename__ = "agent_runs"

    __table_args__ = (
        # Run history is always "this tenant, newest first". tenant_id must lead the index
        # or RLS filters millions of rows before the sort can help.
        Index("ix_agent_runs_tenant_id_created_at", "tenant_id", text("created_at DESC")),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )

    # Nullable and SET NULL: a run must survive the user who triggered it being deleted,
    # or the audit trail develops holes exactly where it matters.
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[RunStatus] = mapped_column(
        _enum_col(RunStatus, "runstatus"),
        default=RunStatus.QUEUED,
        server_default=RunStatus.QUEUED.value,
    )

    input: Mapped[dict] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # The error MESSAGE, never a traceback - a traceback can carry prompt content and
    # internal paths, and this row is readable by the tenant.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<AgentRun {self.id} {self.status}>"
