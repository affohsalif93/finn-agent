# Refactor plan (temporary)

Cleanup before submission. **Delete this file when the refactor is done**; leftovers go into the note as "known cleanups".

## Why
Finn was built in a few days by several agents in parallel. It works and is well tested, but naming, helpers, comments and docs carry traces of that. Goal: code and docs that read as if one careful person wrote them.

## Goals
1. **Readability first.** A reviewer follows one mail from poller to reply in under 10 minutes; `pipeline.py` reads like the architecture diagram.
2. **One way to do each thing.** One German number formatter, one finding/error mechanism, one way to write the run folder, one settings object. No parallel helpers.
3. **Clear module boundaries** (ARCHITECTURE §12): core logic (plan, calc, contracts) pure; adapters (mail, LLM) do I/O; orchestration (pipeline, workflows, poller) only wires. No imports in the wrong direction.
4. **Smaller units.** No module much over ~300 lines, no function over ~50 lines without a reason (candidates: `reply/render.py`, `plan/reader.py`, `calc/engine.py`).
5. **Remove what isn't needed.** Dead code, unused error codes and settings, leftovers (e.g. `PingWorkflow` if no longer needed as a health check).
6. **Consistent naming and language.** English identifiers and docstrings; German only in user-facing texts. One name per concept everywhere (building block = `ItemKey`).
7. **Tests as documentation.** Clear structure (unit / integration / workflow / live), shared fixtures in one place, test names that read as specifications.
8. **Docs match the code.** Present tense, no "planned" / "as built" / history.
9. **Short comments and docstrings.** Comments explain *why*, not *what*; one-line docstrings for simple functions; no history notes; no design essays in modules (point to D# instead); module headers ≤ 2–3 lines. Keep comments that carry real knowledge: contract clauses, Gmail/IMAP specifics, Temporal determinism rules, reasons for fallbacks.
10. **Clean docs: short, current, no duplication.** One topic per file, link instead of repeat; the code is the reference for details (schemas, error codes, settings).

    | File | Purpose | Budget |
    |---|---|---|
    | `README.md` (DE) | what Finn is, start in a few commands, demo, tests, repo map | ~100 lines |
    | `NOTIZ.md` (DE) | assumptions, changes to the brief, AI usage, next steps | 1 page |
    | `docs/ARCHITECTURE.md` | overview, path of one mail, components → modules, key mechanisms, deployment | ≤ 250 lines (today 919) |
    | `docs/DECISIONS.md` | one short row per decision; merge related ones | ~1,200 words (today 2,350) |
    | `docs/FAILURES.md` | code · situation · reaction · test; reply texts live in code | ~100 lines |
    | `docs/SCOPE.md` | task, answer key + traps, contract comparison, scope | ~60–80 lines |
    | `docs/ROADMAP.md` | product vision, reporting duties, next steps | roughly as is, tidied |

    Out of ARCHITECTURE.md: full JSON profile example (→ short excerpt + link to `models/contract.py` / a gold profile), pseudo-code (→ links to the real modules), failure tables (→ FAILURES.md), contract comparison (→ SCOPE.md), design alternatives (→ DECISIONS.md).
11. **Clean the LLM prompts** (`src/finn/contracts/prompts/`, the intent and polish prompts in `request/` and `reply/`):
    - one prompt per file with a clear structure (role, task, rules, output); no prose essays;
    - split where one prompt does several jobs (e.g. schema explanation, trap list and quote rules as separate, reusable sections), merge where texts are duplicated;
    - no rules the schema or the code already enforce; no contradictions between extraction and review prompt;
    - keep every trap rule that the eval depends on.

    **Prompt changes change the prompt version hash and trigger re-extraction**: after any extraction-prompt change, re-run `finn contracts extract --force` + `finn contracts eval` (must stay 95/95 and reproduce the answer key) and re-approve the profiles; intent/polish prompt changes need the live mail cases green.

## Constraints
- **No behaviour change.** Same answer key, same reply text (golden file), same error codes. Tests change only for renames, never in expectations (exception: prompts, see goal 11).
- **Safety net before every step:** ruff, `ruff format --check`, mypy strict, all unit tests, `pytest -m mailserver` against GreenMail.
- **Small, separate commits**, each green on its own and confirmed by the user.
- **Time box ~2–3 h**, prioritised by impact on readability.
- **No new features, no new dependencies.**

## Out of scope
Architecture changes (Temporal, connectors, profile schema), performance tuning, 100 % coverage as a goal.

## Order
1. Mail-server subagent finishes; open changes committed (baseline).
2. Read-only review against these goals → findings list with effort and risk.
3. User picks findings → refactor in small steps, one commit each.
4. Docs cleanup (goal 10) last, so it describes the final state.
5. Delete this file.

## Done when
- [ ] Review findings fixed or listed as "known cleanups" in the note.
- [ ] All checks green incl. mailserver cases; golden reply and answer key unchanged; eval 95/95 if prompts changed.
- [ ] Docs within budget, consistent with the code, all internal links work.
- [ ] One mail traced through the code in < 10 minutes.
- [ ] `docs/REFACTOR.md` deleted.
