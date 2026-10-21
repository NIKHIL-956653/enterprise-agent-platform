"""
The worker process: `python -m eap.worker`, run beside uvicorn.

This is the second half of the split the API was shaped for since EAP-20 - the run
endpoint already returns an id and a status so it can one day answer "queued". EAP-44
makes the API enqueue onto this task queue; until then the worker runs real workflows
started by tests or by hand, and production traffic still executes in-request.
"""

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from eap.core.config import get_settings
from eap.core.logging import get_logger
from eap.workflows.activities import run_agent
from eap.workflows.agent_run import AgentRunWorkflow

log = get_logger("eap.worker")


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address)
    log.info("worker_started", address=settings.temporal_address, queue=settings.temporal_task_queue)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[AgentRunWorkflow],
        activities=[run_agent],
        # The global ceiling; the per-tenant gate shares it out fairly.
        max_concurrent_activities=settings.worker_max_concurrent_activities,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
