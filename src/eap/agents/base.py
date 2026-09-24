"""
The agent contract.

Every agent declares typed input and output as pydantic models. That is not ceremony:
- the API validates a caller's payload before any LLM call is made or paid for
- the platform can publish a JSON schema per agent without the agent doing anything
- a malformed LLM response fails at the boundary, not three layers later

AgentContext is deliberately narrow. An agent gets its tenant, its config and a logger -
NOT a database session. If agents could query freely, tenant isolation would depend on
every agent author remembering the rules, which is exactly the failure ADR-002 avoids.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel


@dataclass(frozen=True)
class AgentContext:
    """Everything an agent is allowed to know about who it is running for."""

    tenant_id: UUID
    user_id: UUID | None
    config: dict[str, Any]


class AgentError(Exception):
    """Raised by an agent for an expected failure. The message is shown to the tenant."""


class BaseAgent(ABC):
    # Registry key. Must match the `name` column on the tenant's agents row.
    name: ClassVar[str]
    display_name: ClassVar[str]

    # Typed boundary. Subclasses override both.
    InputModel: ClassVar[type[BaseModel]]
    OutputModel: ClassVar[type[BaseModel]]

    @abstractmethod
    async def run(self, payload: BaseModel, ctx: AgentContext) -> BaseModel:
        """Execute once. Return an OutputModel instance, or raise AgentError."""

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        """JSON schema for this agent's input - served to callers by the API."""
        return cls.InputModel.model_json_schema()
