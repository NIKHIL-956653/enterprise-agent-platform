"""
Run history.

GET /v1/runs         newest first, keyset-paginated, envelope only (no payloads)
GET /v1/runs/{id}    one run, with input and output

No query here filters by tenant, and none needs to: the session from get_tenant_session
carries app.tenant_id and RLS does the rest. Another tenant's run id is simply not found.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    # Send this back as ?before=... for the next page. None means there is no next page.
    next_before: datetime | None


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
    before: Annotated[datetime | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
) -> RunPage:
    """
    Keyset pagination: "everything older than the last thing you saw". Every page hits the
    (tenant_id, created_at DESC) index the same way, so page 500 costs what page 1 costs.
    OFFSET would make Postgres read and discard 10,000 rows to show you page 500.
    """
    if before is not None and before.tzinfo is None:
        # created_at is timestamptz. Comparing it to a naive datetime raises inside asyncpg
        # as a 500; better to tell the caller what is wrong.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "before must include a timezone")

    stmt = (
        select(AgentRun, Agent.name)
        .join(Agent, Agent.id == AgentRun.agent_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(limit + 1)  # one extra row tells us whether a next page exists
    )
    if before is not None:
        stmt = stmt.where(AgentRun.created_at < before)
    if agent is not None:
        stmt = stmt.where(Agent.name == agent)
    if run_status is not None:
        stmt = stmt.where(AgentRun.status == run_status)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    items = [_summary(run, name) for run, name in rows[:limit]]
    return RunPage(items=items, next_before=items[-1].created_at if has_more else None)


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
