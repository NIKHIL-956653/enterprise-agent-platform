"""
Agent endpoints.

POST /v1/agents/{name}/run is the whole platform in one request: bearer token -> tenant
context -> RLS-scoped session -> registry dispatch -> recorded run.

v1 executes synchronously (ADR pending). The response is a run envelope rather than the
raw agent output, because the run id, status, latency and token counts are the part that
makes cost and failure visible - and because M4 will return the same envelope from a queue
with status "queued", so callers need no change.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eap.agents import AgentError, registered_names
from eap.agents.runner import AgentNotEnabled, InvalidAgentInput, execute_agent
from eap.core.deps import get_claims, get_current_tenant_id, get_tenant_session
from eap.models.agent import Agent

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentSummary(BaseModel):
    name: str
    display_name: str
    is_enabled: bool
    has_implementation: bool


class RunResponse(BaseModel):
    run_id: UUID
    agent: str
    status: str
    output: dict[str, Any] | None
    error: str | None
    latency_ms: int | None
    prompt_tokens: int
    completion_tokens: int


@router.get("", response_model=list[AgentSummary])
async def list_agents(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> list[AgentSummary]:
    """
    Agents this tenant has enabled.

    has_implementation surfaces drift: a row can outlive the code that served it, e.g.
    after a rollback. Better to show it plainly than to fail at dispatch time.
    """
    rows = (await session.execute(select(Agent))).scalars().all()
    available = set(registered_names())
    return [
        AgentSummary(
            name=r.name,
            display_name=r.display_name,
            is_enabled=r.is_enabled,
            has_implementation=r.name in available,
        )
        for r in rows
    ]


@router.post("/{name}/run", response_model=RunResponse)
async def run_agent(
    name: str,
    payload: dict[str, Any],
    claims: Annotated[dict[str, Any], Depends(get_claims)],
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> RunResponse:
    user_id = UUID(claims["sub"]) if "sub" in claims else None

    try:
        run = await execute_agent(
            session=session,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_name=name,
            payload=payload,
        )
    except AgentNotEnabled as e:
        # 404, not 403: whether another tenant enabled this agent is none of our caller's
        # business, and "forbidden" would confirm it exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"agent '{name}' not found") from e
    except InvalidAgentInput as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.errors) from e
    except AgentError as e:
        # Row exists but no code is registered under that name - a deployment problem.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e

    # 200 even when the run failed: creating the run succeeded, and its outcome is in the
    # body. The caller always gets a run_id it can look up later.
    return RunResponse(
        run_id=run.id,
        agent=name,
        status=run.status.value,
        output=run.output,
        error=run.error,
        latency_ms=run.latency_ms,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
    )
