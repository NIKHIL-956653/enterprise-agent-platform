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
from eap.agents.base import Retriever
from eap.audit import record
from eap.core.config import get_settings
from eap.core.logging import get_logger
from eap.llm.base import LLM, LLMError
from eap.llm.gemini import get_llm
from eap.models.agent import Agent, AgentRun, RunStatus
from eap.rag import retrieval
from eap.rag.retrieval import RetrievedChunk

log = get_logger("eap.agents")


class AgentNotEnabled(Exception):
    """This tenant has no enabled agent by that name."""


class InvalidAgentInput(Exception):
    """The payload does not match the agent's declared input model."""

    def __init__(self, errors: Any) -> None:
        super().__init__("invalid input")
        self.errors = errors


def _default_llm() -> LLM | None:
    """
    The real model when a key is configured, otherwise None. Dev boxes and CI have no key;
    that must not stop agents that never touch a model (echo) from running.
    """
    try:
        return get_llm()
    except LLMError:
        return None


def _bind_retriever(session: AsyncSession) -> Retriever:
    """
    Retrieval closed over the request's session. The agent receives the function, never the
    session: it can search this tenant's chunks and do nothing else. RLS scoped the session
    before the agent asks, so no input an agent passes can widen its own view.
    """

    async def _retrieve(query: str, *, top_k: int, min_similarity: float) -> list[RetrievedChunk]:
        return await retrieval.retrieve(
            session=session, query=query, top_k=top_k, min_similarity=min_similarity
        )

    return _retrieve


class _AuditingLLM:
    """
    Wraps whatever model the run uses so every call lands in the audit trail. The agent
    receives this, never the bare model - capture is not something an agent can opt out of.
    """

    def __init__(self, inner: LLM, session: AsyncSession, tenant_id: UUID, run_id: UUID) -> None:
        self._inner = inner
        self._session = session
        self._tenant_id = tenant_id
        self._run_id = run_id

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        try:
            completion = await self._inner.complete(prompt, max_output_tokens=max_output_tokens)
        except Exception:
            # The attempted prompt is evidence too - a reviewer reconstructing a failure
            # needs what was ASKED, not only that asking failed.
            await record(
                self._session,
                tenant_id=self._tenant_id,
                run_id=self._run_id,
                event_type="llm.error",
                payload={"prompt": prompt},
            )
            raise
        await record(
            self._session,
            tenant_id=self._tenant_id,
            run_id=self._run_id,
            event_type="llm.call",
            payload={
                "prompt": prompt,
                "completion": completion,
                "max_output_tokens": max_output_tokens,
            },
        )
        return completion


def _auditing_retriever(inner: Retriever, session: AsyncSession, tenant_id: UUID, run_id: UUID) -> Retriever:
    """Same idea for retrieval: which chunks, at what similarity, for which query."""

    async def _retrieve(query: str, *, top_k: int, min_similarity: float) -> list[RetrievedChunk]:
        chunks = await inner(query, top_k=top_k, min_similarity=min_similarity)
        await record(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            event_type="retrieval.performed",
            payload={
                "query": query,
                "top_k": top_k,
                "min_similarity": min_similarity,
                "chunks": [
                    {"chunk_id": str(c.chunk_id), "ordinal": c.ordinal, "similarity": round(c.similarity, 6)}
                    for c in chunks
                ],
            },
        )
        return chunks

    return _retrieve


async def execute_agent(
    *,
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID | None,
    agent_name: str,
    payload: dict[str, Any],
    llm: LLM | None = None,
    retriever: Retriever | None = None,
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

    await record(
        session,
        tenant_id=tenant_id,
        run_id=run.id,
        event_type="run.started",
        payload={"agent": agent_name, "input": payload},
    )

    base_llm = llm if llm is not None else _default_llm()
    base_retriever = retriever if retriever is not None else _bind_retriever(session)
    ctx = AgentContext(
        tenant_id=tenant_id,
        user_id=user_id,
        config=agent_row.config or {},
        # Explicitly injected (tests) beats resolved (production) - and either way the
        # agent gets the AUDITING wrapper, so a test fake is captured like the real model.
        llm=_AuditingLLM(base_llm, session, tenant_id, run.id) if base_llm is not None else None,
        retriever=_auditing_retriever(base_retriever, session, tenant_id, run.id),
    )
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
    except LLMError as e:
        # The model failed, not our code. Expected in the sense that every network call
        # fails sometimes; safe to show the tenant because LLMError carries no detail.
        run.status = RunStatus.FAILED
        run.error = str(e)
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
    await record(
        session,
        tenant_id=tenant_id,
        run_id=run.id,
        event_type="run.finished",
        payload={
            "agent": agent_name,
            "status": run.status.value,
            "error": run.error,
            "latency_ms": run.latency_ms,
        },
    )
    await session.flush()
    return run
