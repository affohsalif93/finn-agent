# Finn – task, answer key and scope

Related: [DECISIONS.md](DECISIONS.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [FAILURES.md](FAILURES.md) · [ROADMAP.md](ROADMAP.md)

## 1. The task

"Finn" is a digital finance officer at a municipal utility. The head of finance emails Finn a business plan (`Wirtschaftsplan_2027.xlsx`); Finn finds every **financial covenant** in four loan contracts (metric, definition *as written in that contract*, threshold, source), maps the plan values onto each definition, has **code** compute every ratio for Plan 2027 (reporting date 31.12.2027) with its headroom, and replies in the same thread with a summary, a table and the source of every number. **The LLM reads, structures and writes; it never calculates.** Live demo: reviewers mail Finn from their own address.

Business context: the plan (draft of 15.10.2026) is checked **before** it goes to the banks. A plan-based shortfall is **not a default** in any contract (V2 § 13(3), V3 § 13(3), V4 § 14(3)); the binding test uses the audited accounts. Finn words results as an early warning.

## 2. Answer key (regression test)

Base figures (T€): EBITDA 40,200 (JÜ 10,700 + taxes 3,200 + interest 5,100 − interest income 300 + depreciation 21,500) · total assets 313,200 · equity 96,100 · Sonderposten 22,800 · bank liabilities 148,200 · shareholder loan 8,000 (qualified subordination, note 2) · cash 12,600 (freely available, note 3).

| Contract | Covenant | Calculation | Plan 2027 | Threshold | Headroom |
|---|---|---|---|---|---|
| V1 Sparkasse | – none – | preamble ratios describe 2019 (trap) | | | |
| V2 Förderbank | economic equity ratio | (96,100 + 50 % × 22,800) / 313,200; no rounding rule → 2 dp | 34.32 % | ≥ 27.5 % | 6.82 pp |
| V3 Landesbank | DSCR | (40,200 − 3,200) / (4,930 interest excl. unwinding + 18,750 repayments incl. shareholder loan, excl. special repayment) | 1.56 | ≥ 1.30 | 0.26 |
| V4 Syndicated | equity ratio | 96,100 / 313,200, 1 dp | 30.7 % (tight) | ≥ 30.0 % | 0.7 pp |
| V4 Syndicated | dynamic leverage | (148,200 − 12,600) / 40,200; shareholder loan **out** (else 3.57, a false breach) | 3.37 (tightest) | ≤ 3.50 | 0.13 |

**The test behind the task:** "the same" metric differs per contract (Sonderposten 50 % or 0 %; shareholder loan in or out; pension unwinding in or out). A generic "equity" or "net debt" gets at least one covenant wrong. Further traps: V4's margin grid (§ 6(3)) is pricing, not a covenant; V3's printed example (Anlage 3 (8)) must give 1.54.

## 3. How the contracts differ

| | V1 Sparkasse | V2 Förderbank | V3 Landesbank | V4 Syndicated |
|---|---|---|---|---|
| Financial covenants | none | economic equity ratio ≥ 27.5 % | DSCR ≥ 1.30 | equity ratio ≥ 30.0 %; leverage ≤ 3.50 |
| Definitions in | – | § 1 | Anlage 3 | § 1 |
| Rounding | – | none → 2 dp, flagged | 2 dp half-up | 1 dp / 2 dp half-up |
| Plan basis / status | – | Planbilanz; **approved** plan by 31 Jan | Erfolgsplan + Darlehensspiegel; draft OK by 15 Dec | Planbilanz + Erfolgsplan; by 31 Dec |
| Plan shortfall → | – | talks; notify the bank (§ 12(3)) | measures within 30 days (§ 13(3)) | talks on request (§ 14(3)) |
| Binding breach → | – | collateral, termination after 60 days | termination; **cure** by equity injection (≤ 2×, not consecutive) | event of default, **no cure** |
| Loan match | Nr. 4471 203 118 | Nr. FD-2022-08817 | Nr. LBM-INV-2025-0412 (tranches A, B) | "Konsortialkredit" (Sparkasse is also a member) |
| Dismissed numerics | preamble ratios; disposals 10 % | disposals 10 % | disposals 10 %; tranche B condition | margin grid; disposals 7.5 %; cross default EUR 5 M |
| Plan repayment 2027 | 1,250 ✓ | 1,500 ✓ | 3,000 (tranche A) ✓ | 3,000 ✓ |

## 4. Deliverables

A running agent (real mailbox, external senders) · README (German) · note (German, ≤ 1 page: assumptions, changes to the brief, AI usage, next steps) · meaningful commit history · private repo. Deadline: Mon 9 Nov 2026, 09:00.

## 5. Scope

**In scope:** dedicated mailbox with IMAP polling and replies in the thread; one-time LLM extraction into reviewed profiles with quotes; label-based plan reader with cell references and fail-closed input checks; deterministic calculation with contract rounding; German reply with table, sources and calculation path; thread handling (ask → follow-up, revised plan → new run); Temporal orchestration plus a direct `.eml` runner; run folder, structured logs, Temporal UI; sender allowlist; Docker Compose locally and on AWS.

**Out of scope (brief: "nicht verlangt"):** dashboard, PDF report, UI, plausibility checks against prior years, contract search.

**Product scope (documented, not built):** reporting-duty calendar, reminders, drafts with CFO approval, customer UI, multi-tenancy and licensing, Microsoft Graph connector, webhooks, provider failover. See [ROADMAP.md](ROADMAP.md).

Open point to flag, not decide: V4 requires the plan within 30 days of drawing it up; if the 15.10.2026 draft counts, that deadline has passed.
