# Finn – Covenant check by email: analysis & scope

Working document. Written before any code, to pin down what we build and what we expect the result to be.

## 1. What the task really is

An email agent ("Finn", a digital finance officer at a municipal utility):

1. A colleague emails Finn's address, subject like *"Covenant-Prüfung Wirtschaftsplan 2027"*, with `Wirtschaftsplan_2027.xlsx` attached.
2. Finn reads the four loan contracts (fixed folder), extracts every **financial covenant**: metric, definition *as written in that contract*, threshold and source location (page, §, Anlage).
3. Finn maps plan values from the spreadsheet onto each definition.
4. **Code** computes each metric for Plan 2027 (as of 31.12.2027) and the headroom to the threshold.
5. Finn replies to the original mail: short summary, a table per contract/covenant (value, threshold, headroom, met yes/no), and the source for every number (contract location + spreadsheet cell).

**The core rule:** the LLM reads, structures and writes. It never calculates. Every number comes from deterministic code.

**Live demo:** the reviewers send the mail from *their own* address and wait for the reply. So we need a real mailbox that accepts external senders and replies to them.

## 2. What the material contains (and where the traps are)

All values in T€. Plan 2027 base figures (computed from the spreadsheet):

| Figure | Value | Source |
|---|---|---|
| EBITDA | 40,200 | GuV: JÜ 10,700 + taxes 3,200 + interest expense 5,100 − interest income 300 + depreciation 21,500 |
| Total assets | 313,200 | Bilanz D15 |
| Equity | 96,100 | Bilanz D22 |
| Special item for grants (Sonderposten) | 22,800 | Bilanz D23 |
| Bank liabilities | 148,200 | Bilanz D28 = Finanzierung F10 |
| Shareholder loan (subordinated) | 8,000 | Bilanz D29 |
| Cash | 12,600 | Bilanz D12 |

### Contract 1 – Sparkasse, investment loan: **no financial covenants**
- The preamble says "equity ratio 28.4 %" and "dynamic leverage 2.9". These are **background facts about 2019**, not obligations.
- § 14 has only general covenants (negative pledge, pari passu, …). § 10(4) / AGB Nr. 22 is a right to ask for more collateral, not a ratio.
- **Trap:** a careless extractor reports two covenants here. The correct answer is "keine Financial Covenants".

### Contract 2 – Förderbank, promotional loan: 1 covenant
- **Economic equity ratio ≥ 27.5 %** (§ 13(1), p. 6; definitions in § 1, pp. 1–2).
- Economic equity = equity + **50 %** of the Sonderposten. Shareholder loans are explicitly **not** added, even with subordination.
- (96,100 + 11,400) / 313,200 = **34.32 %**, headroom **6.82 pp** ✅
- A plan-based figure is only a forecast (§ 12(1)c, § 13(3)). It is never a reason for termination.

### Contract 3 – Landesbank, two tranches: 1 covenant
- **DSCR ≥ 1.30** (§ 13(1), pp. 5–6; definition in **Anlage 3**, p. 13; plan basis per Anlage 3 (7)).
- DSCR = (EBITDA − income taxes) / debt service, rounded to 2 decimals.
- Debt service = interest on financial liabilities + scheduled repayments.
  - The shareholder loan **is included** ("unabhängig davon, ob … Rangrücktritt").
  - **Exclude** the 170 from unwinding the discount on pension provisions → interest 4,930 (= 5,100 − 170, cross-checks with Finanzierung G15).
  - **Exclude** the voluntary 3,000 special repayment on the syndicated loan.
  - Repayments: 1,250 + 1,500 + 3,000 + 3,000 + 9,000 + 1,000 = 18,750.
- 37,000 / 23,680 = **1.56**, headroom **0.26** ✅

### Contract 4 – Syndicated loan: 2 covenants
- **Equity ratio ≥ 30.0 %** (§ 14(1)a, p. 6; definition § 1, p. 2). Balance-sheet equity only: **no** Sonderposten and **no** shareholder loan. Rounded to 1 decimal.
  - 96,100 / 313,200 = **30.7 %**, headroom **0.7 pp** ✅ ← **tight**
- **Dynamic leverage ≤ 3.50** (§ 14(1)b, p. 7; definitions § 1, pp. 2–3). Net financial debt / EBITDA, rounded to 2 decimals.
  - Financial liabilities **exclude** the subordinated shareholder loan (it meets the contract's definition: qualified subordination § 39(2) InsO, repayable only from free assets; spreadsheet note 2 confirms both).
  - (148,200 − 12,600) / 40,200 = **3.37**, headroom **0.13** ✅ ← **tightest**
- **Trap 1:** if you include the shareholder loan you get 3.57, a **false breach**.
- **Trap 2:** the margin grid in § 6(3) (2.50 / 3.00) is pricing only, not a covenant (§ 6(4)). It is worth mentioning as information: 3.37 > 3.00 puts the loan in the 1.25 % margin step.

### Expected result (our own answer key)

| Contract | Covenant | Plan 2027 | Threshold | Headroom | Met |
|---|---|---|---|---|---|
| 1 Sparkasse | – none – | | | | |
| 2 Förderbank | Economic equity ratio | 34.32 % | ≥ 27.5 % | 6.82 pp | yes |
| 3 Landesbank | DSCR | 1.56 | ≥ 1.30 | 0.26 | yes |
| 4 Syndicated | Equity ratio | 30.7 % | ≥ 30.0 % | 0.7 pp | yes |
| 4 Syndicated | Dynamic leverage | 3.37 | ≤ 3.50 | 0.13 | yes |

All met, but contract 4 is tight on both covenants. Sensitivity: an EBITDA drop of about **1.5 M€** (to ≈ 38,690) breaks the leverage covenant. That is the kind of sentence the demo mail uses.

**The test behind the task:** "the same" metric means something different in every contract (equity: plain / +50 % Sonderposten; debt: with / without shareholder loan; interest: with / without unwinding). A single generic "net debt" or "equity" gets at least one covenant wrong. **Each contract needs its own definition.**

## 3. Scope

### Deliverables (from the brief)
1. **Running agent**: real mailbox → processing → reply to the sender, works with external senders.
2. **README**: start the agent with a few commands.
3. **Note (max. 1 page)**: assumptions, what we changed in the brief and why, how AI was used, what comes next.
4. **Time log / meaningful commit history.**
5. Git repo (GitHub, private, with read access for the reviewers). **Deadline: Mon 9 Nov 2026, 09:00.**

### In scope
- Mail I/O via IMAP polling + SMTP on a dedicated test Gmail account (app password). Reply with `In-Reply-To`/`References`, to the actual sender.
- Fallback: read an `.eml` file / local run (for dev and as a safety net).
- LLM extraction of covenants into a **fixed schema** (metric, components, operator, threshold, rounding, source location, verbatim quote). An empty list is a valid result.
- Deterministic calculation engine: one explicit formula per covenant type and its parameters (include/exclude flags), with an input → cell-reference trail.
- Spreadsheet reader that keeps cell references (`Bilanz!D22`) for the sources.
- Reply mail (HTML + plain text): summary, table, footnotes with sources, calculation path per covenant.
- **Robustness** (the brief explicitly prefers this over features):
  - Validation: the LLM output must cite a quote that really occurs on the stated page.
  - Cross-checks: balance sheet balances, debt schedule = balance sheet, interest tie-out.
  - Regression test against the answer key above.
  - Clear error mail when something fails (wrong or missing attachment).
  - Do not process the same mail twice.

### Out of scope (explicitly "not required")
Dashboard, PDF report, UI, plausibility checks against prior years. No contract search (fixed folder).

### Design decisions to justify in the note (candidates)
- **The LLM does not choose the formula freely.** It maps the contract text onto a small set of known metric types plus definition flags (e.g. `include_shareholder_loan`, `sonderposten_share = 0.5`, `exclude_provision_unwinding`). The code computes. Why: auditable and testable, and the LLM never does arithmetic.
- **Cache the contract extraction** (contracts rarely change): extract once, a human reviews the result (`covenants.json`), and each mail run reuses it. Faster, cheaper and reproducible during the live demo. Re-extraction is triggered by a file hash change.
- **Plan values count as early warning, not breach.** All contracts say a plan-based breach is not an event of default. The mail says so.
- Mention extras the reviewer will value: margin-step info (contract 4), tight-headroom sensitivity, and the reason contract 1 has none.

## 4. Proposed stack
Python 3.12 · `uv` · `pdfplumber` (text per page) · `openpyxl` (values + formulas, our own evaluation of the formula cells) · Claude API (structured output / tool use) · `imaplib`/`smtplib` (stdlib) · `pytest`.

## 5. Plan of increments (≈ one commit each)
1. Project setup, material, this scope document
2. Spreadsheet reader with cell references + tie-out checks
3. Calculation engine + tests against the answer key (no LLM yet: hard-coded covenant specs)
4. PDF reader (text per page) + LLM covenant extraction into the schema + quote validation
5. Pipeline: `.eml` in → result → reply mail rendered locally
6. Live mailbox: IMAP poll / SMTP reply, deduplication, error mails
7. README, note, time log; end-to-end test from an external address
