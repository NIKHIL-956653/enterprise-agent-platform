"""
Agent endpoints.

POST /v1/agents/{name}/run is the whole platform in one request: bearer token -> tenant
context -> RLS-scoped session -> registry dispatch -> recorded run.

Since EAP-44 every run executes on Temporal. The default request waits for the result, so
the envelope looks exactly as it did when execution was in-request; ?async=true returns at
once with status "queued" - the shape this envelope was designed for since EAP-20.
"""

import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eap.agents import AgentError, get_agent, registered_names
from eap.core.config import get_settings
from eap.core.deps import (
    get_claims,
    get_current_clearance,
    get_current_tenant_id,
    get_tenant_session,
)
from eap.core.ratelimit import enforce_run_rate_limit
from eap.core.temporal import get_temporal_client
from eap.models.agent import Agent, AgentRun, RunStatus
from eap.workflows import AgentRunRequest, AgentRunWorkflow

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentSummary(BaseModel):
    name: str
    display_name: str
    is_enabled: bool
    has_implementation: bool
    # The agent's InputModel as JSON Schema (D5). The admin UI renders its run form from
    # this, so field names, required-ness and limits come from the one place the API also
    # validates against - a form cannot drift from the model it submits to. None when no
    # code serves the agent (has_implementation is False).
    input_schema: dict[str, Any] | None = None


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
            input_schema=get_agent(r.name).InputModel.model_json_schema() if r.name in available else None,
        )
        for r in rows
    ]


def _envelope_from_row(run: AgentRun, agent: str) -> RunResponse:
    return RunResponse(
        run_id=run.id,
        agent=agent,
        status=run.status.value,
        output=run.output,
        error=run.error,
        latency_ms=run.latency_ms,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
    )


async def _await_workflow(handle, timeout_seconds: int, fallback: RunResponse) -> RunResponse:
    """
    Wait at the pass. On a timeout or a workflow-level failure the caller still gets a
    truthful envelope: the run exists durably and can be polled at /v1/runs/{id} - that is
    the point of the cutover, so the degraded answer is "queued", never a 500.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await handle.result()
    except Exception:  # noqa: BLE001 - timeout, worker down, retries exhausted: poll later
        return fallback
    return RunResponse(
        run_id=UUID(result.run_id),
        agent=fallback.agent,
        status=result.status,
        output=result.output,
        error=result.error,
        latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )


@router.post(
    "/{name}/run",
    response_model=RunResponse,
    dependencies=[Depends(enforce_run_rate_limit)],
)
async def run_agent(
    name: str,
    payload: dict[str, Any],
    claims: Annotated[dict[str, Any], Depends(get_claims)],
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
    clearance: Annotated[int, Depends(get_current_clearance)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)] = None,
    no_wait: Annotated[bool, Query(alias="async")] = False,
) -> RunResponse:
    """
    Validation stays synchronous ON PURPOSE: a 404 or 422 must cost one database read,
    not a queue round-trip - and garbage must never enter the queue at all.
    """
    user_id = UUID(claims["sub"]) if "sub" in claims else None

    agent_row = (await session.execute(select(Agent).where(Agent.name == name))).scalar_one_or_none()
    if agent_row is None or not agent_row.is_enabled:
        # 404, not 403: whether another tenant enabled this agent is none of our caller's
        # business, and "forbidden" would confirm it exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"agent '{name}' not found")

    try:
        impl_cls = get_agent(name)
    except AgentError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    try:
        impl_cls.InputModel.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.errors()) from e

    settings = get_settings()
    wait_budget = settings.agent_run_timeout_seconds + 30

    # A replayed request gets the run it already started - attaching to the workflow
    # mid-flight if it is still cooking. The SELECT covers every honest retry; the partial
    # unique index (migration 0010) is the backstop for the race two simultaneous retries
    # can create, and losing that race is a 409 whose own retry lands back here.
    if idempotency_key is not None:
        existing = (
            await session.execute(select(AgentRun).where(AgentRun.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if existing is not None:
            envelope = _envelope_from_row(existing, name)
            if no_wait or existing.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                return envelope
            client = await get_temporal_client()
            handle = client.get_workflow_handle_for(AgentRunWorkflow.run, str(existing.id))
            return await _await_workflow(handle, wait_budget, envelope)

    run = AgentRun(
        tenant_id=tenant_id,
        agent_id=agent_row.id,
        triggered_by=user_id,
        status=RunStatus.QUEUED,
        input=payload,
        idempotency_key=idempotency_key,
    )
    session.add(run)
    try:
        await session.flush()
        # Captured BEFORE the commit: committing may expire the ORM object, and a refresh
        # afterwards would run in a fresh transaction carrying no tenant context - RLS
        # would hide our own row from us.
        queued = _envelope_from_row(run, name)
        # Commit NOW, mid-request: the worker reads this row from its own session, and an
        # uncommitted row would make the first activity attempt fail for no reason.
        await session.commit()
    except IntegrityError as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "another request with this Idempotency-Key is in flight; retry",
        ) from e

    client = await get_temporal_client()
    handle = await client.start_workflow(
        AgentRunWorkflow.run,
        AgentRunRequest(
            tenant_id=str(tenant_id),
            user_id=str(user_id) if user_id else None,
            clearance=clearance,
            agent_name=name,
            payload=payload,
            run_id=str(queued.run_id),
        ),
        id=str(queued.run_id),
        task_queue=settings.temporal_task_queue,
    )

    if no_wait:
        return queued
    return await _await_workflow(handle, wait_budget, queued)
