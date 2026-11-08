"""`finn demo send-cases`: the case catalogue into GreenMail, answered by the compose test Finn.

Starts / updates the compose profile `testmail` (GreenMail, Roundcube, finn-*-testmail; the core
services are never touched), sends every case from its test sender to finn@finn.test, waits for
Finn's replies (multi-step cases need them), checks them like the live tests and writes the
gallery `out/replies/index.html`. Afterwards the threads can be read in Roundcube
(http://localhost:8080, login cfo@stadtwerke.test / cfo-test).
"""

import asyncio
import importlib.util
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import gallery
from cases import CASES, FINN, PASSWORDS
from driver import (
    COMPOSE_SERVICES,
    ROOT,
    CaseResult,
    Env,
    compose_gate_codes,
    compose_up,
    run_cases,
)

Echo = Callable[[str], None]


def _plans(out: Path) -> dict[str, Path]:
    path = ROOT / "scripts" / "make_plan_fixtures.py"
    spec = importlib.util.spec_from_file_location("make_plan_fixtures", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built: dict[str, Path] = module.build_all(out)
    return built


def send_cases(only: list[str], real_llm_key: str | None, echo: Echo = print) -> int:
    unknown = [n for n in only if n not in {c.name for c in CASES}]
    if unknown:
        names = ", ".join(c.name for c in CASES)
        echo(f"Unbekannte Fälle: {', '.join(unknown)}. Verfügbar: {names}")
        return 2
    cases = [c for c in CASES if not only or c.name in only]
    echo("Starte Compose-Profil testmail (GreenMail, Roundcube, finn-*-testmail) …")
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    compose_up(COMPOSE_SERVICES, real_llm_key=real_llm_key)
    llm = "OpenAI" if real_llm_key else "Stand-in (FINN_LLM_FAKE)"
    echo(f"Sende {len(cases)} Fälle an {FINN} (LLM: {llm}) und warte auf Finns Antworten …")
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(
            finn=FINN,
            runs_dir=ROOT / "runs-testmail",
            plans=_plans(Path(tmp)),
            gate_codes=compose_gate_codes(since),
            timeout=400 if real_llm_key else 120,
            settle=6,
            poll=1,
        )
        results: list[CaseResult] = asyncio.run(run_cases(cases, env))
    path = gallery.write(results, mode=f"demo (compose), LLM: {llm}")
    for r in results:
        kinds = " → ".join(x.kind for x in r.replies) or "keine Antwort"
        mark = "ok    " if r.passed else "FEHLER"
        echo(f"{mark} {r.case.name:24} {kinds:20} {'; '.join(r.problems)}")
    passed = sum(r.passed for r in results)
    echo(f"\n{passed}/{len(results)} Fälle wie erwartet. Galerie: {path}")
    echo(
        "Roundcube: http://localhost:8080 · Login cfo@stadtwerke.test / "
        f"{PASSWORDS['cfo@stadtwerke.test']} (abgelehnte Fälle: blocked@stadtwerke.test, "
        "eve@unbekannt.test, Passwörter in tests/live/cases.py)"
    )
    return 0 if passed == len(results) else 1
