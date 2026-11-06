"""Command line entry point: `finn worker`, `finn poller`, `finn ping`."""

import asyncio
import uuid
from datetime import timedelta

import typer

from finn.config import get_settings
from finn.log import configure_logging

app = typer.Typer(help="Finn: covenant check agent.", no_args_is_help=True)


@app.callback()
def main() -> None:
    configure_logging(get_settings())


@app.command()
def worker() -> None:
    """Run the Temporal worker until SIGTERM / Ctrl+C."""
    from finn.workflows.worker import run_worker

    asyncio.run(run_worker(get_settings()))


@app.command()
def poller() -> None:
    """Run the mailbox poller loop until SIGTERM / Ctrl+C."""
    from finn.poller import run_poller

    asyncio.run(run_poller(get_settings()))


@app.command()
def ping(timeout: float = typer.Option(30, help="Seconds to wait for the result.")) -> None:
    """Run PingWorkflow on the worker and print the result (end-to-end check)."""
    from finn.runtime import connect
    from finn.workflows.ping import PingWorkflow

    settings = get_settings()

    async def _run() -> str:
        client = await connect(settings)
        assert client is not None
        return await client.execute_workflow(
            PingWorkflow.run,
            id=f"ping-{uuid.uuid4()}",
            task_queue=settings.temporal_task_queue,
            execution_timeout=timedelta(seconds=timeout),
        )

    typer.echo(asyncio.run(_run()))
