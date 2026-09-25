"""
Agent execution.

One function, because every run must be recorded the same way whatever happens. A run that
fails silently is worse than one that fails loudly: the tenant is charged for tokens with
nothing to show for it, and nobody can tell whether the platform or the model misbehaved.

Failures are RECORDED, not raised. The run row is the result - its status says how it went.
"""

import asyncio
import time
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eap.agents import AgentContext, AgentError, get_agent
from eap.core.config import get_settings
from eap.core.logging import get_logger
from eap.models.agent import Agent, AgentRun, RunStatus

log = get_logger("eap.agents")


class AgentNotEnabled(Exception):
    """This tenant has no enabled agent by that name."""


class InvalidAgentInput(Exception):
    """The payload does not match the agent's declared input model."""

    def __init__(self, errors: Any) -> None:
        super().__init__("invalid input")
        self.errors = errors


async def execute_agent(
    *,
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID | None,
    agent_name: str,
    payload: dict[str, Any],
) -> AgentRun:
    # No tenant filter: the session already carries app.tenant_id, so RLS scopes this to
    # the caller's tenant. Another tenant's agent row simply does not exist here.
    agent_row = (await session.execute(select(Agent).where(Agent.name == agent_name))).scalar_one_or_none()
    if agent_row is None or not agent_row.is_enabled:
        raise AgentNotEnabled(agent_name)

    impl_cls = get_agent(agent_name)  # raises AgentError if no code is registered

    # Validate BEFORE creating a run row. A malformed request is the caller's bug, not a
    # failed execution - recording it as one would pollute the tenant's failure rate.
    try:
        typed_input = impl_cls.InputModel.model_validate(payload)
    except ValidationError as e:
        raise InvalidAgentInput(e.errors()) from e

    run = AgentRun(
        tenant_id=tenant_id,
        agent_id=agent_row.id,
        triggered_by=user_id,
        status=RunStatus.RUNNING,
        input=payload,
    )
    session.add(run)
    await session.flush()  # assigns run.id without ending the transaction

    ctx = AgentContext(tenant_id=tenant_id, user_id=user_id, config=agent_row.config or {})
    timeout = get_settings().agent_run_timeout_seconds
    started = time.perf_counter()

    try:
        async with asyncio.timeout(timeout):
            result = await impl_cls().run(typed_input, ctx)
        run.output = result.model_dump(mode="json")
        run.status = RunStatus.SUCCEEDED
    except TimeoutError:
        run.status = RunStatus.TIMED_OUT
        run.error = f"agent exceeded {timeout}s"
    except AgentError as e:
        # An expected failure the agent chose to report. Safe to show the tenant.
        run.status = RunStatus.FAILED
        run.error = str(e)
    except Exception as e:  # noqa: BLE001
        # Unexpected. The tenant gets the exception TYPE only - a message or traceback can
        # carry prompt content, internal paths or credentials.
        run.status = RunStatus.FAILED
        run.error = f"internal error: {type(e).__name__}"
        log.exception("agent_run_failed", agent=agent_name, run_id=str(run.id))

    run.latency_ms = int((time.perf_counter() - started) * 1000)
    await session.flush()
    return run
