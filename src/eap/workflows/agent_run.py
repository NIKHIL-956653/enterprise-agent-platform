"""
The workflow is thin ON PURPOSE. Temporal replays workflow code from history to recover
state, so it must be deterministic - no I/O, no clocks, no randomness. Everything real
happens in the activity; the workflow's job is to say what runs, with what timeout, and
what retrying looks like. When EAP-44 adds multi-step checkpointing, each step becomes
its own activity here and recovery between steps comes from history replay, free.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from eap.workflows.activities import run_agent
    from eap.workflows.shared import AgentRunRequest, AgentRunResult


@workflow.defn
class AgentRunWorkflow:
    @workflow.run
    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        return await workflow.execute_activity(
            run_agent,
            request,
            start_to_close_timeout=timedelta(seconds=120),
            # Three attempts, short first interval: infra blips retry fast, and anything
            # still failing after three tries needs a human, not a fourth try.
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=200),
                backoff_coefficient=2.0,
                maximum_attempts=3,
            ),
        )
