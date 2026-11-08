# Decision log

One row per decision; the German note summarises them. IDs are stable (code and docs cite them); merged decisions point to the row that now holds them.

| # | Decision | Why | Not chosen |
|---|---|---|---|
| D1 | **The LLM never calculates.** It extracts, classifies and writes; code computes every number. | Rule of the brief; numbers must be reproducible and auditable. | – |
| D2 | **Extract each contract once into a reviewed JSON profile**, with the full text in the call (no RAG) and verbatim quotes + pages for every parameter. A second LLM pass and a deterministic completeness scan look for omissions; re-extract only when the PDF or the prompt changes. | The interpretation is written down once, reviewed and frozen, so runs can't read a contract differently (3.37 vs. 3.57). 13 pages fit into context; chunking would break cross-references (§ 13 → § 1 → Anlage 3). | Full text on every request (nothing reviewable); RAG (later, for Q&A across many contracts). |
| D3 | → see D2 | | |
| D4 | → see D2 | | |
| D5 | **Covenants as recipes over building blocks**, with the contract's own definitions, weights (0.5 × Sonderposten) and whitelisted conditions (shareholder loan out if subordinated); anything else → `manual_review`. | The ratio shape is standard; the wording decides what goes in, and that difference is the test. New contracts need no new code; the recipe also generates the formula in the mail. | Fixed per-type parameters; formula strings written by the LLM (not auditable, needs a parser). |
| D6 | **Read the plan by labels, normalise, check what we use.** Building blocks found by row label × plan-year column with their cells; units normalised per sheet to T€ (unclear → BLOCK); the figures we use are tied out, the rest of the workbook is not audited; simple formulas without cached values are recomputed, anything else is reported. | Templates differ and rows move; a wrong mapping or unit almost always breaks a tie-out. The brief asks for covenants, not a plan audit. | Fixed cells; sending the sheet to the LLM; evaluating every formula (`pycel`); a unit library. |
| D7 | **Fail closed on input.** BLOCK / ASK findings stop the result; WARN / INFO are reported. | A wrong "alles eingehalten" to a bank is worse than no answer. | Best effort. |
| D8 | **Recompute from scratch on every run**; the thread only supplies the request and the latest plan. | Deterministic; revised plans need no conversation memory. | LLM context across messages. |
| D9 | **Temporal for orchestration** plus a direct `.eml` runner over the same functions. | Durable steps, per-step retries, workflow ID = Message-ID, waiting for follow-ups, Web UI as operator console. | Own checkpointing; n8n; LangGraph (our flow is a fixed pipeline). |
| D10 | **Own mail connectors behind one interface; IMAP polling by a stateless poller outside Temporal, with the gate in it.** Case: a dedicated Gmail mailbox; product: Graph / Gmail API in the customer's domain. | Only outbound connections. The poll loop has no state to keep (the folder is the state; the next cycle is the retry) and unauthorised mail never creates workflow data. Low volume needs no mail-sync service. | Webhooks (product option); Nylas / EmailEngine (mail leaves us / licence); a poll workflow (~1,400 runs per mailbox and day). |
| D11 | → see D10 | | |
| D12 | **Access control via sender rules, later tenants**, not keys in mails. Demo: `ALLOWED_SENDERS` open (the brief requires unknown senders); rejections are security events, attachments never parsed. | Keys in mails leak. Product: licences, verified domains, roles. | Product key in the subject. |
| D13 | **Observability = run folder + Temporal UI + structured logs.** | The run folder answers every per-request question (what came in, what the LLM saw, why 3.37). A tracing backend pays off only at scale. | Langfuse / OpenTelemetry (roadmap): extra containers and demo risk. |
| D14 | **Calculation path in the mail itself.** | No UI asked for; the mail must be self-contained and auditable. | Link to a web view. |
| D15 | **Results worded as an early warning**, plus tight flag, rounding-aware sensitivity, V4 margin step and "no covenants" reasons. | A plan shortfall is not a default in any contract; cheap to compute, useful to a finance reader. | Plain pass / fail table. |
| D16 | **Resilience over features:** retries, template reply without the LLM, idempotent send, partial results. | Numbers never depend on the LLM at runtime, so Finn answers correctly without it. | Provider failover (documented, not built). |
| D17 | **German for README, note and mails; English for code and internal docs.** | Reviewers are German; docs are working material. | – |
| D18 | (dropped: a process rule, not an architecture decision) | | |
| D19 | → see D10 | | |
| D20 | → see D10 | | |
| D21 | **Data minimisation instead of an anonymisation engine.** Plan values never go to the LLM; the polish sees placeholders; the thread is pseudonymised before the intent call; person names are masked before extraction; the mapping stays in the run folder. | The sensitive data is business data, which code already keeps away from the LLM; each call gets only what it needs. | NER anonymisation (Presidio): false positives, and extraction needs the real text. |
| D22 | → see D6 | | |
| D23 | **Own small evaluator on stdlib `decimal`**: weighted sums, one division, contract rounding, one comparison. | Exact decimals and `ROUND_HALF_UP` built in; nothing to parse or inject; small enough to test exhaustively. | `sympy` / formula strings; `pint`; `babel`. |
| D24 | → see D6 | | |
| D25 | **Python 3.12 and a small dependency set**: openpyxl, pdfplumber, Temporal, OpenAI SDK, Jinja2, Typer, structlog; versions locked in `uv.lock`; ruff, mypy strict, pytest. | Best libraries for Excel, PDF text and exact decimals; few moving parts in a live demo. | TypeScript (floats, weaker PDF / Excel tooling); heavier stacks. |
| D26 | → see D25 | | |
| D27 | **Layered package with one shared `pipeline.py`; onboarding as a CLI command** with human review in git. | Core testable without Temporal, mail or LLM; contracts don't change during the demo. | Onboarding service (product: yes, with an approval step). |
| D28 | **Contract profile schema v2, derived from all four contracts**: sources by ID, definitions with exclusions, conditions with criteria, `if_missing`, `loan_filters`, loans with schedules, typed plan check / breach / cure / pricing grid, printed examples, dismissed mentions, review metadata. | Every key exists because a contract needs it (V2/V4 exclusions are the traps; V3 includes shareholder loans; V3 prints an example that tests the extraction). | v1 (global sub-recipes, loose sources, an LLM confidence). |
