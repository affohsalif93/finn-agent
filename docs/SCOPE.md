# Finn – Covenant check by email: analysis & scope

Working document. It records what the task is, what the material contains, the answer we expect, and what we deliver.
Related: [DECISIONS.md](DECISIONS.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [FAILURES.md](FAILURES.md) · [ROADMAP.md](ROADMAP.md)

## 1. The task in one paragraph

"Finn" is a digital finance officer (*digitaler Referent Finanzierung*) at a municipal utility.
1. The head of finance emails Finn a business plan (`Wirtschaftsplan_2027.xlsx`).
2. Finn reads four loan contracts from a fixed folder and finds every **financial covenant**: metric, definition *as written in that contract*, threshold, source location.
3. Finn maps the plan values onto each definition.
4. **Code** computes every ratio for Plan 2027 (reporting date 31.12.2027) and the headroom to the threshold.
5. Finn replies in the same mail thread with a summary, a table (value, threshold, headroom, met yes/no) and the source of every number (contract location + spreadsheet cell).

**Core rule:** the LLM reads, structures and writes. It never calculates.
**Live demo:** the reviewers email Finn from *their own* address and wait for the reply.

## 2. Business context

- **The plan is written before the plan year starts.** The workbook is dated 15.10.2026, a draft not yet approved by the supervisory board. The check answers: *"if 2027 goes as planned, will we meet every covenant on 31.12.2027?"*
- The CFO runs this check **before sending the plan to the banks.** Each contract requires the plan, and most require a covenant forecast along with it (see [ROADMAP.md](ROADMAP.md) §1). If a covenant is tight, the plan may be revised or the bank briefed in advance.
- **A plan-based shortfall is not a default** in any of the contracts (V2 § 13(3), V3 § 13(3), V4 § 14(3)). It is an early warning. The binding test uses the audited annual accounts (~mid-2028). Finn's mail must word results that way.

## 3. Material and answer key

All values in T€. Base figures for Plan 2027:

| Figure | Value | Source |
|---|---|---|
| EBITDA | 40,200 | GuV: JÜ 10,700 + taxes 3,200 + interest expense 5,100 − interest income 300 + depreciation 21,500 |
| Total assets | 313,200 | Bilanz D15 |
| Equity | 96,100 | Bilanz D22 |
| Sonderposten (investment/construction grants) | 22,800 | Bilanz D23 |
| Bank liabilities | 148,200 | Bilanz D28 = Finanzierung F10 |
| Shareholder loan (qualified subordination) | 8,000 | Bilanz D29 |
| Cash (freely available, note 3) | 12,600 | Bilanz D12 |

### Contract 1 – Sparkasse, investment loan: **no financial covenants**
- The preamble mentions "equity ratio 28.4 %" and "dynamic leverage 2.9". These describe the **2019 situation**; they are not obligations.
- § 14 contains only general covenants. § 10(4) / AGB Nr. 22 is a right to request more collateral, not a ratio.
- **Trap:** a careless extractor reports two covenants here.

### Contract 2 – Förderbank, promotional loan: 1 covenant
- **Economic equity ratio ≥ 27.5 %**: § 13(1), p. 6; definitions § 1, pp. 1–2.
- Economic equity = equity + **50 %** of Sonderposten. Shareholder loans are explicitly excluded, subordinated or not.
- (96,100 + 11,400) / 313,200 = **34.32 %**, headroom **6.82 pp** ✅ (no rounding rule in the contract → we report 2 decimals and say so).

### Contract 3 – Landesbank, two tranches: 1 covenant
- **DSCR ≥ 1.30**: § 13(1), pp. 5–6; definition **Anlage 3**, p. 13; plan basis per Anlage 3 (7).
- DSCR = (EBITDA − income taxes) / debt service, rounded half-up to 2 decimals.
- Debt service = interest on financial liabilities + scheduled repayments:
  - shareholder loan **included** ("unabhängig davon, ob … Rangrücktritt")
  - pension interest unwinding (170) **excluded** → interest 4,930 (= 5,100 − 170 = Finanzierung G15)
  - voluntary special repayment (3,000) **excluded**
  - repayments 1,250 + 1,500 + 3,000 + 3,000 + 9,000 + 1,000 = 18,750
- 37,000 / 23,680 = **1.56**, headroom **0.26** ✅

### Contract 4 – Syndicated loan: 2 covenants
- **Equity ratio ≥ 30.0 %**: § 14(1)a, p. 6; definition § 1, p. 2. Balance-sheet equity only (no Sonderposten, no shareholder loan). Rounded to 1 decimal.
  - 96,100 / 313,200 = **30.7 %**, headroom **0.7 pp** ✅ ← tight
- **Dynamic leverage ≤ 3.50**: § 14(1)b, p. 7; definitions § 1, pp. 2–3. Net financial debt / EBITDA, rounded to 2 decimals.
  - Financial liabilities **exclude** the subordinated shareholder loan. It meets the definition: qualified subordination under § 39(2) InsO, and it may only be serviced from free assets (spreadsheet note 2).
  - (148,200 − 12,600) / 40,200 = **3.37**, headroom **0.13** ✅ ← tightest
  - Trap 1: including the shareholder loan gives 3.57, a **false breach**.
  - Trap 2: the margin grid in § 6(3) is pricing only (§ 6(4)), not a covenant. As information: 3.37 > 3.00 → highest margin step (1.25 %).

### Answer key (used as the regression test)

| Contract | Covenant | Plan 2027 | Threshold | Headroom | Met |
|---|---|---|---|---|---|
| 1 Sparkasse | – none – | | | | |
| 2 Förderbank | Economic equity ratio | 34.32 % | ≥ 27.5 % | 6.82 pp | yes |
| 3 Landesbank | DSCR | 1.56 | ≥ 1.30 | 0.26 | yes |
| 4 Syndicated | Equity ratio | 30.7 % | ≥ 30.0 % | 0.7 pp | yes (tight) |
| 4 Syndicated | Dynamic leverage | 3.37 | ≤ 3.50 | 0.13 | yes (tight) |

**The test behind the task:** "the same" metric is defined differently in every contract (Sonderposten counted 50 % or 0 %; shareholder loan in or out; pension unwinding in or out). A single generic "equity" or "net debt" gets at least one covenant wrong.

## 4. Deliverables (from the brief)
1. A **running agent**: real mailbox → processing → reply; accepts external senders.
2. **README (German)**: start the agent with a few commands.
3. **Note (German, max. 1 page)**: assumptions, changes to the brief and why, AI usage, next steps.
4. **Time log / meaningful commit history.**
5. Git repo (private, read access for the reviewers). **Deadline: Mon 9 Nov 2026, 09:00.**

## 5. Scope

### In scope (case study)
- **Mail:** dedicated new mailbox, IMAP polling + SMTP reply in the same thread, to the verified sender.
- **Contracts:** one-time LLM extraction into a fixed schema (with quotes, pages, §), validated and reviewed, stored as JSON profiles.
- **Plan:** label-based reader (no fixed cells) with cell references, plus extensive input checks ("fail closed").
- **Calculation:** deterministic, parameterised per contract, exact decimals, contract rounding rules.
- **Reply:** German mail with summary, table, sources, calculation path per covenant, notes.
- **Thread handling:** missing input → ask; follow-up in the thread completes the request; revised plan → new run from scratch.
- **Orchestration:** Temporal (durable steps, retries, waiting for follow-ups), plus a direct `.eml` runner.
- **Observability:** console log, run folder, Temporal Web UI, Langfuse traces for LLM calls.
- **Security:** sender allowlist (open in demo mode), SPF/DKIM/DMARC result logged, no attachment parsing for rejected mail.
- **Deployment:** `docker compose up`, runnable on a laptop or a small cloud VM.

### Out of scope (brief: "nicht verlangt")
Dashboard, PDF report, UI, plausibility checks against prior years, contract search.

### Product scope (documented, not built)
Reporting-duty calendar, reminders, drafts + CFO approval, customer UI, multi-tenancy, licensing, Microsoft Graph connector, webhooks, provider failover. See [ROADMAP.md](ROADMAP.md).

## 6. Stack (D25, D26)

Python 3.12. Versions checked on PyPI on 6 Nov 2026; lower bounds in `pyproject.toml`, exact versions locked in `uv.lock`.

| Purpose | Choice |
|---|---|
| Orchestration | `temporalio` 1.35 (incl. pydantic data converter, test environment) |
| LLM / tracing | `openai` 3.x (structured outputs) · `langfuse` 5.x (optional), API details checked before coding |
| Excel / PDF | `openpyxl` 3.1 · `pdfplumber` 0.11 |
| Models / settings | `pydantic` 2.14 · `pydantic-settings` 2.16 |
| Mail | `imap-tools` 1.16 · stdlib `email` + `smtplib` |
| Templates / CLI / logs | `jinja2` 3.1 · `typer` 0.28 · `structlog` 26.1 |
| Dev | `uv` · `ruff` (lint + format) · `mypy` (strict on core modules) · `pytest` + `pytest-asyncio` · GitHub Actions CI |

| Service | Case study |
|---|---|
| Temporal | dev server in Docker (SQLite on a volume, UI :8233) |
| OpenAI | API key from the user's account, model via `OPENAI_MODEL` |
| Langfuse | Langfuse Cloud, EU region, free tier; off if no keys |
| Mailbox | new **Gmail** account, IMAP/SMTP with an app password (2-step verification) |
| Git | private GitHub repo, read access for the reviewers |
| VM (optional) | e.g. Hetzner CX22 |

Not used on purpose: `tenacity` (Temporal + OpenAI SDK retry), `msoffcrypto-tool` (magic-byte check suffices), formula parsers, `pint`, `babel` (D23).

## 7. Increments (≈ one commit each, every commit confirmed by the user)
1. ✅ Project setup, material, scope
2. Docs: scope update, decisions, architecture, roadmap
3. Project skeleton: dependencies, package layout, config + `.env.example`
4. Plan reader (label-based, cell references) + input checks + tests
5. Calculation engine (recipe evaluator) with hand-written profiles + answer-key, rounding, unit and trap tests (no LLM yet)
6. Contract text extraction (page cache) → LLM extraction → quote validation + completeness check → reviewed profiles; eval against the hand-written profiles
7. Mail parsing, intent, reply rendering (LLM + template fallback), number check, direct runner `finn process mail.eml`
8. Temporal workflow + activities, IMAP poller, idempotent SMTP send, thread handling
9. Langfuse tracing, run folder, structured logs
10. Docker Compose, README (DE), note (DE), time log; end-to-end test from an external address; optional VM deploy

## 8. Open items
- ~~Mailbox provider~~ → Gmail with app password (decided). To do: create the account, enable 2-step verification, create the app password; test before the demo that replies don't land in spam.
- ~~Langfuse~~ → Langfuse Cloud EU (decided). To do: create the account and keys.
- OpenAI model choice: configurable; pick when implementing (check current models).
- Check the APIs of `openai` 3.x and `langfuse` 5.x (major versions newer than prior experience) and the Temporal Docker image tag before coding.
- V4 plan deadline: "within 30 days of drawing up the plan" may already have passed if the 15.10.2026 draft counts. Flag it, don't decide it.
