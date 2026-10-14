"""
EAP-43: durable execution around the existing runner, with nothing rebuilt inside it.

These tests run against the compose Temporal dev cluster (same philosophy as Postgres:
integration over mocks), with the worker running IN this process so monkeypatched fakes
reach the activity.

The pair that matters is the last two. An infrastructure failure - the database gone -
retries and recovers without a human. A MODEL failure does not retry: execute_agent
records it as a failed run and returns normally, so it is a result, not an error. That
line is what stops Temporal from re-billing a tenant three times for a model that said
no the first time.
"""

import asyncio
import time
import uuid

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from eap.agents import runner
from eap.core.config import get_settings
from eap.core.db import get_owner_session_factory, get_session_factory
from eap.workflows import AgentRunRequest, AgentRunWorkflow
from eap.workflows import activities as activities_module
from eap.workflows.activities import run_agent
from tests.test_audit_capture import FakeLLM

TEXT = (
    "The platform runs many agents for many tenants. Each tenant's rows are isolated by "
    "row-level security, and every agent run is recorded whether it succeeds or fails."
)


@pytest.fixture
async def temporal_client() -> Client:
    """Connect with patience: auto-setup takes ~30s to build its schema on first boot."""
    settings = get_settings()
    deadline = time.monotonic() + 30
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await Client.connect(settings.temporal_address)
        except Exception as e:  # noqa: BLE001 - the bridge raises RuntimeError on refusal
            last = e
            await asyncio.sleep(1)
    pytest.fail(
        f"Temporal is not reachable on {settings.temporal_address}. "
        f"Is the compose stack up? Try: docker compose up -d  ({last})"
    )


async def _seed_tenant() -> str:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, :slug) RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, 'summarize', 'Summarize')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return str(tid)


async def _execute(client: Client, tenant_id: str) -> object:
    """One workflow through an in-process worker on a queue no other worker is watching."""
    queue = f"test-{uuid.uuid4().hex[:8]}"
    async with Worker(client, task_queue=queue, workflows=[AgentRunWorkflow], activities=[run_agent]):
        return await client.execute_workflow(
            AgentRunWorkflow.run,
            AgentRunRequest(
                tenant_id=tenant_id,
                user_id=None,
                clearance=0,
                agent_name="summarize",
                payload={"text": TEXT},
            ),
            id=f"test-run-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
        )


async def test_a_workflow_executes_a_real_agent_run(temporal_client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: FakeLLM(answer="Done."))
    tenant_id = await _seed_tenant()

    result = await _execute(temporal_client, tenant_id)

    assert result.status == "succeeded"
    # The run row and its audit trail exist - Temporal wrapped the platform, it did not
    # bypass it. RLS, the run record and capture all fired exactly as in-request.
    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        status = (
            await s.execute(text("SELECT status FROM agent_runs WHERE id = :rid"), {"rid": result.run_id})
        ).scalar_one()
        events = (
            (
                await s.execute(
                    text("SELECT event_type FROM audit_log WHERE run_id = :rid ORDER BY seq"),
                    {"rid": result.run_id},
                )
            )
            .scalars()
            .all()
        )
    assert status == "succeeded"
    assert events[0] == "run.started"
    assert events[-1] == "run.finished"


async def test_an_infrastructure_failure_retries_and_recovers(temporal_client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: FakeLLM(answer="Done."))
    tenant_id = await _seed_tenant()

    real_factory = activities_module.get_session_factory
    attempts: list[int] = []

    def flaky_factory():
        attempts.append(1)
        if len(attempts) == 1:
            # The database is "down" for the first attempt. This raises INSIDE the
            # activity, which is exactly what Temporal's retry policy exists for.
            raise ConnectionError("database unreachable")
        return real_factory()

    monkeypatch.setattr(activities_module, "get_session_factory", flaky_factory)

    result = await _execute(temporal_client, tenant_id)

    assert result.status == "succeeded"
    assert len(attempts) == 2  # failed once, retried once, no human involved


async def test_a_failed_model_is_a_result_not_a_retry(temporal_client, monkeypatch) -> None:
    llm_builds: list[int] = []

    def counting_llm():
        llm_builds.append(1)
        return FakeLLM(fail=True)

    monkeypatch.setattr(runner, "_default_llm", counting_llm)
    tenant_id = await _seed_tenant()

    result = await _execute(temporal_client, tenant_id)

    # The platform worked; the model said no. Retrying would re-bill the tenant for the
    # same no - so the workflow completes, carrying the failure as data.
    assert result.status == "failed"
    assert "model call failed" in result.error
    assert len(llm_builds) == 1
