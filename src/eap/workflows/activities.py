"""
The one activity: execute an agent run, exactly as a request would.

All the platform's guarantees are reused, not rebuilt - the activity opens a session as
eap_app, sets the tenant and clearance context, and calls the same execute_agent the API
calls. RLS scopes it, the audit wrappers capture it, the run row records it. Temporal adds
durability AROUND that; it changes nothing inside it.

An activity is Temporal's retry unit. Note what retrying means here: execute_agent records
model failures as FAILED runs and returns normally, so a failed completion is a RESULT,
not a retryable error. What retries is infrastructure - the database being unreachable,
the worker dying mid-run. That is the line between "the platform worked and the answer
was no" and "the platform did not get to work".
"""

from uuid import UUID

from sqlalchemy import text
from temporalio import activity

from eap.agents.runner import execute_agent
from eap.core.db import get_session_factory
from eap.workflows.shared import AgentRunRequest, AgentRunResult


@activity.defn
async def run_agent(request: AgentRunRequest) -> AgentRunResult:
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "SELECT set_config('app.tenant_id', :tid, true), "
                "       set_config('app.clearance', :clr, true)"
            ),
            {"tid": request.tenant_id, "clr": str(request.clearance)},
        )
        run = await execute_agent(
            session=session,
            tenant_id=UUID(request.tenant_id),
            user_id=UUID(request.user_id) if request.user_id else None,
            agent_name=request.agent_name,
            payload=request.payload,
            run_id=UUID(request.run_id) if request.run_id else None,
        )
        await session.commit()
        return AgentRunResult(
            run_id=str(run.id),
            status=run.status.value,
            error=run.error,
            output=run.output,
            latency_ms=run.latency_ms,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
        )
