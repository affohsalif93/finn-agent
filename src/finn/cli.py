"""Command line entry point: `finn worker`, `finn poller`, `finn ping`, `finn plan read`."""

import asyncio
import io
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer

from finn.config import get_settings
from finn.log import configure_logging

app = typer.Typer(help="Finn: covenant check agent.", no_args_is_help=True)
plan_app = typer.Typer(help="Business plan workbooks.", no_args_is_help=True)
app.add_typer(plan_app, name="plan")


def _utf8_console() -> None:
    """Write UTF-8 so umlauts and "€" render on Windows consoles and pipes (cp1252 default)."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


@app.callback()
def main() -> None:
    _utf8_console()
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


@plan_app.command("read")
def plan_read(
    path: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Plan workbook (.xlsx).")
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Dump the full result as JSON.")] = False,
) -> None:
    """Read a plan: building blocks with cell and label, loans, facts and findings."""
    from finn.plan import read_plan
    from finn.plan.render import render_text

    result = read_plan(path)
    typer.echo(result.model_dump_json(indent=2) if as_json else render_text(result))
