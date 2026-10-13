"""
The agent contract.

Every agent declares typed input and output as pydantic models. That is not ceremony:
- the API validates a caller's payload before any LLM call is made or paid for
- the platform can publish a JSON schema per agent without the agent doing anything
- a malformed LLM response fails at the boundary, not three layers later

AgentContext is deliberately narrow. An agent gets its tenant, its config and a logger -
NOT a database session. If agents could query freely, tenant isolation would depend on
every agent author remembering the rules, which is exactly the failure ADR-002 avoids.

The retriever is the one database-backed capability an agent gets, and it is narrow on
purpose: a question in, this tenant's nearest chunks out. The runner binds it to the
request's session, so RLS has scoped it before the agent ever calls it. An agent can ask
"what do my documents say about X" - it cannot name a table or a tenant, and cannot write.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from eap.llm.base import LLM
from eap.rag.retrieval import RetrievedChunk


@runtime_checkable
class Retriever(Protocol):
    """Tenant-scoped similarity search. Nearest first; empty when nothing clears the floor."""

    async def __call__(self, query: str, *, top_k: int, min_similarity: float) -> list[RetrievedChunk]: ...


@dataclass(frozen=True)
class AgentContext:
    """Everything an agent is allowed to know about who it is running for."""

    tenant_id: UUID
    user_id: UUID | None
    config: dict[str, Any]
    llm: LLM | None = None
    retriever: Retriever | None = None


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
