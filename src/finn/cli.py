"""Command line entry point: worker, poller, check, process, plan, contracts and demo commands."""

import asyncio
import io
import sys
from pathlib import Path
from typing import Annotated

import typer

from finn.config import get_settings
from finn.errors import ErrorCode
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
            "`finn contracts extract`. Zum Testen: "
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
    from finn.calc.report_text import render_report
    from finn.contracts.store import load_profiles
    from finn.plan import read_plan

    settings = get_settings()
    directory = _profiles_dir(profiles)
    loaded, profile_findings = load_profiles(directory, settings.contracts_dir)
    result = read_plan(path)
    report = calculate(
        result.plan, loaded, result.findings, tight_ratio=settings.tight_headroom_ratio,
        profile_findings=profile_findings, file=path.name,
    )  # fmt: skip
    typer.echo(report.model_dump_json(indent=2) if as_json else render_report(report))


@app.command()
def process(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Mail (.eml).")],
    send: Annotated[
        bool, typer.Option("--send", help="Really send the reply via SMTP (default: print only).")
    ] = False,
    no_gate: Annotated[
        bool, typer.Option("--no-gate", help="Skip the gate (sender / recipient rules).")
    ] = False,
    html: Annotated[bool, typer.Option("--html", help="Print the HTML part instead.")] = False,
) -> None:
    """Direct runner (no Temporal): parse → intent → plan → calculate → reply; prints the reply."""
    from finn.pipeline import process_eml

    settings = get_settings()
    connector = None
    if send:
        from finn.mail.imap_smtp import ImapSmtpConnector

        connector = ImapSmtpConnector(settings)
    result = process_eml(path, settings, send=send, connector=connector, check_gate=not no_gate)
    if result.rejected is not None:
        typer.echo(
            f"Vom Gate abgelehnt ({result.rejected.event}, {result.rejected.code}): "
            f"{result.rejected.reason}. Mit --no-gate trotzdem verarbeiten.",
            err=True,
        )
        raise typer.Exit(code=3)
    for d in result.drafts:
        typer.echo(f"An: {d.to}\nBetreff: {d.subject}\nMessage-ID: <{d.message_id}>\n")
        typer.echo(d.html if html else d.text)
    for s in result.sent:
        state = "bereits gesendet, übersprungen" if s.skipped_already_sent else "gesendet"
        typer.echo(f"[{state}] <{s.message_id}>", err=True)
    typer.echo(f"Laufordner: {result.run_dir}", err=True)


demo_app = typer.Typer(
    help="Demo against the local test mail server (compose profile testmail).",
    no_args_is_help=True,
)
app.add_typer(demo_app, name="demo")


@demo_app.command("send-cases")
def demo_send_cases(
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Case name(s), e.g. --only happy.")
    ] = None,
    real_llm: Annotated[
        bool, typer.Option("--real-llm", help="Test containers use OpenAI (key from .env).")
    ] = False,
) -> None:
    """Send the failure-case catalogue (tests/live/cases.py) into GreenMail, let the test Finn
    answer, write out/replies/index.html; then read the threads in Roundcube (localhost:8080).
    Needs the repository checkout (tests/) and Docker; never touches the real mailbox."""
    root = Path.cwd()
    if not (root / "tests" / "live" / "demo.py").is_file():
        typer.echo("Bitte im Repository-Wurzelverzeichnis ausführen (tests/live fehlt).", err=True)
        raise typer.Exit(code=2)
    for sub in ("tests", "tests/live"):
        if str(root / sub) not in sys.path:
            sys.path.insert(0, str(root / sub))
    import importlib

    demo = importlib.import_module("demo")  # tests/live/demo.py (not part of the package)

    key = None
    if real_llm:
        secret = get_settings().openai_api_key
        if secret is None:
            typer.echo("--real-llm braucht OPENAI_API_KEY (Umgebung oder .env).", err=True)
            raise typer.Exit(code=2)
        key = secret.get_secret_value()
    code = demo.send_cases(list(only or []), key, echo=typer.echo)
    raise typer.Exit(code=int(code))


_PROFILE_ERRORS = {
    ErrorCode.PROFILE_MISSING,
    ErrorCode.PROFILE_INVALID_RECIPE,
    ErrorCode.PROFILE_INVARIANT,
    ErrorCode.PROFILE_EXAMPLE_MISMATCH,
    ErrorCode.PROFILE_QUOTE_MISMATCH,
    ErrorCode.CONTRACT_CHANGED,
}


@contracts_app.command("check")
def contracts_check(profiles: ProfilesOption = None) -> None:
    """Validate the profiles (schema, sources, recipes, contract hash, quotes, completeness scan)
    and run their examples."""
    from finn.contracts.pdf_text import load_contract_text
    from finn.contracts.selftest import check_examples, example_value
    from finn.contracts.store import load_profiles
    from finn.contracts.validate import text_findings
    from finn.numbers import format_de

    settings = get_settings()
    directory = _profiles_dir(profiles)
    loaded, findings = load_profiles(directory, settings.contracts_dir)
    for profile in loaded:
        findings += check_examples(profile)
        pdf = settings.contracts_dir / profile.document.file
        if pdf.is_file():  # missing PDF: CONTRACT_CHANGED.missing from the store
            findings += text_findings(profile, load_contract_text(pdf))
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


ContractsDirOption = Annotated[
    Path | None,
    typer.Option("--contracts", file_okay=False, help="Contract PDFs; default: CONTRACTS_DIR."),
]


@contracts_app.command("extract")
def contracts_extract(
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Contract id(s), e.g. --only V4.")
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", file_okay=False, help="Output folder; default: PROFILES_DIR."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-extract even if PDF and prompt are unchanged.")
    ] = False,
    no_review: Annotated[
        bool, typer.Option("--no-review", help="Skip the second LLM pass (review).")
    ] = False,
    contracts: ContractsDirOption = None,
) -> None:
    """LLM extraction of the contract PDFs into draft profiles (+ <id>.review.json)."""
    from finn.contracts.extract import run_extract
    from finn.contracts.report import render_extract_runs
    from finn.llm.client import usage_log

    settings = get_settings()
    runs = run_extract(
        contracts or settings.contracts_dir,
        out or settings.profiles_dir,
        only=only,
        force=force,
        with_review=not no_review,
    )
    typer.echo(render_extract_runs(runs, usage_log()))
    if any(r.status == "failed" for r in runs):
        raise typer.Exit(code=1)


@contracts_app.command("eval")
def contracts_eval(
    profiles: ProfilesOption = None,
    gold: Annotated[
        Path, typer.Option("--gold", file_okay=False, help="Hand-written gold profiles.")
    ] = Path("tests/fixtures/profiles_gold"),
    plan: Annotated[
        Path, typer.Option("--plan", dir_okay=False, help="Plan for the answer-key run.")
    ] = Path("material/Wirtschaftsplan_2027.xlsx"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show passed checks.")] = False,
) -> None:
    """Compare extracted profiles with the gold profiles and run the answer key with them."""
    from finn.contracts.eval import answer_key_checks, evaluate_dirs
    from finn.contracts.store import load_profiles

    directory = _profiles_dir(profiles)
    extracted, problems = load_profiles(directory)
    gold_profiles, _ = load_profiles(gold)
    for f in problems:
        if f.code != "PROFILE_NOT_REVIEWED":
            typer.echo(f"[{f.severity}] {f.code}: {f.message_de}")
    results = evaluate_dirs(extracted, gold_profiles)
    for ev in results:
        typer.echo("\n".join(ev.lines(verbose=verbose)))
    passed = sum(e.passed for e in results)
    total = sum(len(e.checks) for e in results)
    typer.echo(f"Gesamt: {passed}/{total} Prüfpunkte ({passed / total:.0%})" if total else "")
    key = answer_key_checks(extracted, plan, tight_ratio=get_settings().tight_headroom_ratio)
    typer.echo("\n".join(c.line() for c in key))
    if not all(c.ok for c in key):
        raise typer.Exit(code=1)


@contracts_app.command("approve")
def contracts_approve(
    contract_id: Annotated[str, typer.Argument(help="Contract id, e.g. V4.")],
    by: Annotated[str, typer.Option("--by", help="Name of the reviewer.")],
    profiles: ProfilesOption = None,
    note: Annotated[str | None, typer.Option("--note", help="Review note.")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Approve despite quote / recipe errors.")
    ] = False,
) -> None:
    """Human review step: set review.status = reviewed (+ reviewed_by / reviewed_at)."""
    import datetime as dt

    from finn.contracts.extract import write_profile
    from finn.contracts.pdf_text import load_contract_text
    from finn.contracts.store import load_profile
    from finn.contracts.validate import validate_profile

    settings = get_settings()
    directory = profiles or settings.profiles_dir
    path = directory / f"{contract_id}.json"
    profile, problems = load_profile(path) if path.is_file() else (None, [])
    if profile is None:
        detail = "; ".join(f.message_de for f in problems) or "nicht gefunden"
        typer.echo(f"Profil {path}: {detail}", err=True)
        raise typer.Exit(code=2)
    pdf = settings.contracts_dir / profile.document.file
    if pdf.is_file():
        report = validate_profile(profile, load_contract_text(pdf))
        if not report.ok:
            typer.echo("\n".join(report.lines()), err=True)
            if not force:
                typer.echo("Nicht freigegeben (--force übergeht die Prüfung).", err=True)
                raise typer.Exit(code=1)
    notes = "\n".join(filter(None, [profile.review.notes, note]))
    review = profile.review.model_copy(
        update={
            "status": "reviewed",
            "reviewed_by": by,
            "reviewed_at": dt.datetime.now(dt.UTC).date(),
            "notes": notes or None,
        }
    )
    write_profile(path, profile.model_copy(update={"review": review}))
    typer.echo(f"{contract_id}: freigegeben von {by}.")
