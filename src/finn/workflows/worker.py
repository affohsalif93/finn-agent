"""Worker process: registers workflows and activities on the task queue and runs until stopped."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import structlog
from temporalio.worker import Worker

from finn.config import Settings
from finn.runtime import connect, install_stop_handlers
from finn.workflows.activities import ping
from finn.workflows.ping import PingWorkflow

log = structlog.get_logger(__name__)


async def run_worker(settings: Settings) -> None:
    stop = install_stop_handlers()
    client = await connect(settings, stop)
    if client is None:
        return

    with ThreadPoolExecutor(max_workers=10) as activity_executor:  # activities are sync
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[PingWorkflow],
            activities=[ping],
            activity_executor=activity_executor,
            graceful_shutdown_timeout=timedelta(seconds=10),
        )
        run_task = asyncio.create_task(worker.run())
        stop_task = asyncio.create_task(stop.wait())
        log.info("worker started", task_queue=settings.temporal_task_queue)

        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if run_task.done():
            # Fatal worker error: re-raise so the process exits non-zero and compose restarts it.
            stop_task.cancel()
            await run_task
            return
        log.info("stopping worker")
        await worker.shutdown()
        await run_task
    log.info("worker stopped")
