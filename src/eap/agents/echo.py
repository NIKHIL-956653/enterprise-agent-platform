"""
Echo agent - the smallest possible real agent.

It exists to prove the whole path works end to end without spending an LLM call: request,
auth, tenant context, dispatch, typed validation, run record. When something breaks later,
running this first tells you whether the problem is the platform or the LLM.
"""

from pydantic import BaseModel, Field

from eap.agents.base import AgentContext, BaseAgent
from eap.agents.registry import register


class EchoInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class EchoOutput(BaseModel):
    message: str
    tenant_id: str


@register
class EchoAgent(BaseAgent):
    name = "echo"
    display_name = "Echo"
    InputModel = EchoInput
    OutputModel = EchoOutput

    async def run(self, payload: EchoInput, ctx: AgentContext) -> EchoOutput:
        # Returning the tenant id proves the context actually reached the agent.
        return EchoOutput(message=payload.message, tenant_id=str(ctx.tenant_id))
