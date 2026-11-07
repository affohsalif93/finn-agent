"""Command line entry point: `finn worker | poller | ping | check`, `finn plan read`,
`finn contracts check`."""

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
contracts_app = typer.Typer(help="Contract profiles.", no_args_is_help=True)
app.add_typer(contracts_app, name="contracts")

ProfilesOption = Annotated[
    Path | None,
    typer.Option(
        "--profiles",
        file_okay=False,
        help="Folder with contract profiles (*.json); default: PROFILES_DIR.",
    ),
]


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


def _profiles_dir(profiles: Path | None) -> Path:
    directory = profiles or get_settings().profiles_dir
    if not directory.is_dir() or not any(directory.glob("*.json")):
        typer.echo(
            f"Keine Vertragsprofile in {directory}. Die geprüften Profile entstehen erst mit "
            "`finn contracts extract` (Inkrement 6). Zum Testen: "
            "--profiles tests/fixtures/profiles_gold",
            err=True,
        )
        raise typer.Exit(code=2)
    return directory


@app.command()
def check(
    path: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Plan workbook (.xlsx).")
    ],
    profiles: ProfilesOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Dump the report as JSON.")] = False,
) -> None:
    """Covenant check: read the plan, load the profiles, calculate, print the result."""
    from finn.calc.engine import calculate
    from finn.calc.render import render_report
    from finn.contracts.store import load_profiles
    from finn.plan import read_plan

    settings = get_settings()
    directory = _profiles_dir(profiles)
    loaded, profile_findings = load_profiles(directory, settings.contracts_dir)
    result = read_plan(path)
    report = calculate(
        result.plan, loaded, result.findings, settings,
        profile_findings=profile_findings, file=path.name,
    )  # fmt: skip
    typer.echo(report.model_dump_json(indent=2) if as_json else render_report(report))


_PROFILE_ERRORS = {
    "PROFILE_MISSING",
    "PROFILE_INVALID_RECIPE",
    "PROFILE_INVARIANT",
    "PROFILE_EXAMPLE_MISMATCH",
    "CONTRACT_CHANGED",
}


@contracts_app.command("check")
def contracts_check(profiles: ProfilesOption = None) -> None:
    """Validate the profiles (schema, sources, recipes, contract hash) and run their examples."""
    from finn.contracts.selftest import check_examples, example_value
    from finn.contracts.store import load_profiles
    from finn.numbers import format_de

    settings = get_settings()
    directory = _profiles_dir(profiles)
    loaded, findings = load_profiles(directory, settings.contracts_dir)
    for profile in loaded:
        findings += check_examples(profile)
        covenants = ", ".join(c.covenant_id for c in profile.covenants) or "keine Covenants"
        typer.echo(f"{profile.contract_id} ({profile.review.status}): {covenants}")
        for example in profile.examples:
            value = example_value(profile, example)
            shown = format_de(value, 2) if value is not None else "nicht berechenbar"
            typer.echo(
                f"  Beispiel {example.covenant_id}: erwartet {format_de(example.expected, 2)}, "
                f"berechnet {shown}"
            )
    for f in findings:
        typer.echo(f"[{f.severity}] {f.code}: {f.message_de}")
    errors = [f for f in findings if f.code in _PROFILE_ERRORS]
    typer.echo(f"{len(loaded)} Profile geladen, {len(errors)} Fehler, "
               f"{len(findings) - len(errors)} Hinweise.")  # fmt: skip
    if errors:
        raise typer.Exit(code=1)
