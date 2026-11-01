"""
Run history.

GET /v1/runs         newest first, keyset-paginated, envelope only (no payloads)
GET /v1/runs/{id}    one run, with input and output
GET /v1/runs/{id}/audit   the run's audit events, hash chain re-verified (EAP-53)

No query here filters by tenant, and none needs to: the session from get_tenant_session
carries app.tenant_id and RLS does the rest. Another tenant's run id is simply not found.
"""

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.api import pagination
from eap.core.deps import get_tenant_session
from eap.models.agent import Agent, AgentRun, RunStatus

router = APIRouter(prefix="/runs", tags=["runs"])


class RunSummary(BaseModel):
    run_id: UUID
    agent: str
    status: str
    error: str | None
    latency_ms: int | None
    prompt_tokens: int
    completion_tokens: int
    created_at: datetime


class RunDetail(RunSummary):
    input: dict[str, Any]
    output: dict[str, Any] | None


class RunPage(BaseModel):
    items: list[RunSummary]
    # Send this back as ?before=... for the next page, unchanged - it is opaque. None means
    # there is no next page. It names (created_at, id), so two runs sharing a timestamp on a
    # page boundary are neither skipped nor repeated (EAP-37, eap.api.pagination).
    next_before: str | None


def _summary(run: AgentRun, agent_name: str) -> RunSummary:
    return RunSummary(
        run_id=run.id,
        agent=agent_name,
        status=run.status.value,
        error=run.error,
        latency_ms=run.latency_ms,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        created_at=run.created_at,
    )


@router.get("", response_model=RunPage)
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
) -> RunPage:
    """
    Keyset pagination: "everything older than the last thing you saw". Every page hits the
    (tenant_id, created_at DESC) index the same way, so page 500 costs what page 1 costs.
    OFFSET would make Postgres read and discard 10,000 rows to show you page 500.
    """
    cursor = pagination.parse(before)

    stmt = (
        select(AgentRun, Agent.name)
        .join(Agent, Agent.id == AgentRun.agent_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(limit + 1)  # one extra row tells us whether a next page exists
    )
    if cursor is not None:
        stmt = stmt.where(pagination.older_than(cursor, AgentRun.created_at, AgentRun.id))
    if agent is not None:
        stmt = stmt.where(Agent.name == agent)
    if run_status is not None:
        stmt = stmt.where(AgentRun.status == run_status)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    items = [_summary(run, name) for run, name in rows[:limit]]
    last = items[-1] if has_more else None
    return RunPage(
        items=items,
        next_before=pagination.encode(last.created_at, last.run_id) if last is not None else None,
    )


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> RunDetail:
    row = (
        await session.execute(
            select(AgentRun, Agent.name)
            .join(Agent, Agent.id == AgentRun.agent_id)
            .where(AgentRun.id == run_id)
        )
    ).first()
    if row is None:
        # Covers both "never existed" and "belongs to another tenant" - RLS makes them
        # indistinguishable, which is exactly what we want the caller to see.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    run, agent_name = row
    return RunDetail(**_summary(run, agent_name).model_dump(), input=run.input, output=run.output)


class AuditEventOut(BaseModel):
    seq: int
    event_type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    created_at: datetime
    # This event, re-verified: its stored hash recomputes from its stored fields AND its
    # prev_hash equals the previous tenant event's hash. False means tampered or spliced.
    intact: bool


class RunAuditResponse(BaseModel):
    run_id: UUID
    # True only when every event below is intact. "We keep an audit log" is a claim;
    # this field is the receipt.
    chain_ok: bool
    events: list[AuditEventOut]


# Verification runs IN Postgres, with the exact expression the chain trigger used to write
# the hashes (migration 0009) - recomputing in Python would have to reproduce JSONB's and
# timestamptz's text forms byte for byte, which is a lie waiting for a locale change. The
# join to seq-1 checks linkage: a deleted event leaves the next one pointing at a hash that
# no longer exists, and COALESCE turns that hole into a mismatch instead of a NULL pass.
_AUDIT_VERIFY = text(
    """
    SELECT a.seq, a.event_type, a.payload, a.prev_hash, a.hash, a.created_at,
           (
             a.hash = encode(digest(
                 a.prev_hash || a.tenant_id::text || a.seq::text || a.event_type
                 || a.payload::text || COALESCE(a.run_id::text, '') || a.created_at::text,
                 'sha256'), 'hex')
             AND a.prev_hash = COALESCE(p.hash, repeat('0', 64))
           ) AS intact
      FROM audit_log a
      LEFT JOIN audit_log p ON p.tenant_id = a.tenant_id AND p.seq = a.seq - 1
     WHERE a.run_id = :run_id
     ORDER BY a.seq
    """
)


@router.get("/{run_id}/audit", response_model=RunAuditResponse)
async def get_run_audit(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> RunAuditResponse:
    """
    The run's audit trail, re-verified on every read.

    RLS scopes both queries, so another tenant's run - and its events - simply do not
    exist here; 404, same as get_run. eap_app cannot UPDATE or DELETE audit rows, so a
    false `intact` means someone with owner access edited history - which is exactly the
    event this table exists to make detectable.
    """
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    rows = (await session.execute(_AUDIT_VERIFY, {"run_id": run_id})).all()
    events = [
        AuditEventOut(
            seq=r.seq,
            event_type=r.event_type,
            payload=r.payload if isinstance(r.payload, dict) else json.loads(r.payload),
            prev_hash=r.prev_hash,
            hash=r.hash,
            created_at=r.created_at,
            intact=r.intact,
        )
        for r in rows
    ]
    return RunAuditResponse(run_id=run_id, chain_ok=all(e.intact for e in events), events=events)
