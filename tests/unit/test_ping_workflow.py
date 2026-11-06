"""PingWorkflow end to end against a local Temporal dev server (see `temporal_env`)."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from finn.workflows.activities import ping
from finn.workflows.ping import PingWorkflow

pytestmark = pytest.mark.asyncio(loop_scope="session")  # same loop as the session fixture


async def test_ping_workflow_returns_pong(temporal_env: WorkflowEnvironment) -> None:
    task_queue = f"test-{uuid.uuid4()}"
    with ThreadPoolExecutor(max_workers=1) as executor:  # `ping` is a sync activity
        async with Worker(
            temporal_env.client,
            task_queue=task_queue,
            workflows=[PingWorkflow],
            activities=[ping],
            activity_executor=executor,
        ):
            result = await temporal_env.client.execute_workflow(
                PingWorkflow.run,
                id=f"ping-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    assert result == "pong"
