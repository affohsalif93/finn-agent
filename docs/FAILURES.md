# Failure catalogue

Every way a request can go wrong, how Finn detects it, and how it reacts. Each row has an **error code** (used in logs, run folder `checks.json`, reply templates and tests) and a **test fixture**.
Related: [ARCHITECTURE.md](ARCHITECTURE.md) (§4 plan reader, §6 mail handling, §10 resilience) · [DECISIONS.md](DECISIONS.md) (D7 fail closed).

## Conventions

| Severity | Meaning |
|---|---|
| **REJECT** | Mail never enters (gate). Security event logged, **no reply**. |
| **IGNORE** | Mail is skipped silently (loops, duplicates). |
| **ASK** | Input missing or unusable. Finn replies with exactly what is needed and the workflow **waits** for a follow-up (thread case B, timeout 7 days). |
| **BLOCK** | Input contradicts itself or the request. No covenant result is confirmed. Finn explains and waits for a corrected file. |
| **PARTIAL** | One covenant can't be computed; the others are reported, that one is marked "nicht berechenbar" with the reason. |
| **WARN** | Result is computed; the issue is listed at the top of the reply. |
| **INFO** | Noted in the reply header, no impact on the result. |

- Reply texts are **German templates** keyed by error code; the LLM may smooth the wording but code checks that the facts (file names, cells, years) are unchanged.
- "Built" column: ✅ built in the case study · 📐 designed, not built.
- Fixture names refer to `tests/fixtures/`.

## Intent input contracts

Code checks these after the intent classification; anything missing → `ASK`.

| Intent | Required | Optional | Built |
|---|---|---|---|
| `covenant_check` | exactly one plan file (`.xlsx`) in the thread; plan year (from the file; must match the request if the request names one) | contract filter ("nur Landesbank") | ✅ |
| `follow_up_input` | a waiting workflow in the thread + the missing piece | – | ✅ |
| `question_about_result` | an earlier run in the thread | – | 📐 polite "noch nicht unterstützt" |
| `other` | – | – | ✅ short polite reply |

## A. Gate (poller, headers only)

| Code | Failure | Detection | Reaction | Reply (DE, gist) | Built | Fixture |
|---|---|---|---|---|---|---|
| `GATE_SENDER_REJECTED` | sender not allowed | `ALLOWED_SENDERS` rules | REJECT | – | ✅ | `mail_unknown_sender.eml` |
| `GATE_AUTH_FAILED` | SPF/DKIM/DMARC fail (if required) | `Authentication-Results` | REJECT | – | ✅ | `mail_dmarc_fail.eml` |
| `GATE_LOOP` | auto-reply, bulk, own address | `Auto-Submitted`, `Precedence`, `X-Autoreply`, `From` | IGNORE | – | ✅ | `mail_auto_reply.eml` |
| `GATE_NOT_ADDRESSED` | mail in Finn's folder not addressed to `FINN_ADDRESS` (To / Cc / Delivered-To) | headers | REJECT | – | ✅ | gate unit test |
| `GATE_DUPLICATE` | same Message-ID again | Temporal workflow ID | IGNORE | – | ✅ | test only |
| `GATE_LICENCE_EXPIRED` | tenant licence expired | tenant registry | reply once, notify admin, then IGNORE | "Lizenz abgelaufen, bitte Administrator kontaktieren." | 📐 stub | – |
| `MAIL_TOO_LARGE` | mail above size limit (e.g. 25 MB) | size before download | ASK | "Die Nachricht ist zu groß (… MB). Bitte nur den Wirtschaftsplan senden." | ✅ | test only |

## B. Parse

| Code | Failure | Detection | Reaction | Reply (DE, gist) | Built | Fixture |
|---|---|---|---|---|---|---|
| `MAIL_UNPARSEABLE` | broken MIME / encoding | parser exception | raw `.eml` kept; reply with reference ID | "Ihre Nachricht konnte nicht gelesen werden (Referenz …)." | ✅ | `mail_broken_mime.eml` |
| `ATTACHMENT_TNEF` | Outlook `winmail.dat` | content type `application/ms-tnef` | ASK | "Der Anhang kam im Outlook-Sonderformat an. Bitte als normale Datei senden." | ✅ detect | `mail_tnef.eml` |
| `ATTACHMENT_ARCHIVE` | plan inside `.zip`/`.7z` | extension / magic bytes | ASK (📐 product: unpack) | "Bitte die Datei entpackt anhängen." | ✅ detect | `mail_zip.eml` |
| `ATTACHMENT_LINK_ONLY` | SharePoint/OneDrive link instead of a file | URL pattern in body, no attachment | ASK | "Ich kann keine Links öffnen. Bitte die Datei direkt anhängen." | ✅ detect | `mail_link.eml` |

## C. Intent

| Code | Failure | Detection | Reaction | Reply (DE, gist) | Built | Fixture |
|---|---|---|---|---|---|---|
| – (no code) | LLM down after retries | activity failure | rule fallback: `.xlsx` attached + keywords → `covenant_check`; else ASK | (normal reply) | ✅ | test with mocked LLM |
| `INTENT_AMBIGUOUS` | vague request + `.xlsx` | intent `covenant_check` with low confidence | proceed, state the assumption | "Ich habe Ihre Nachricht als Covenant-Prüfung verstanden." | ✅ | `mail_vague.eml` |
| `INTENT_PARTLY_UNSUPPORTED` | several requests, some unsupported | intent notes | do the check, list what isn't supported | "Fristenprüfung kann ich noch nicht; hier die Covenant-Prüfung." | ✅ | `mail_two_requests.eml` |
| `INTENT_CONTRACT_FILTER` | only some contracts requested | intent parameter | filter; mention the others | "Geprüft: Landesbank. Weitere Verträge mit Covenants: …" | ✅ | `mail_only_landesbank.eml` |
| – (no code) | unrelated request | intent `other` | short reply | "Ich prüfe Covenants anhand von Wirtschaftsplänen. …" | ✅ | `mail_off_topic.eml` |
| – (no code) | question about an earlier result | intent `question_about_result` | polite "not yet" | "Rückfragen zu Ergebnissen kann ich noch nicht beantworten." | 📐 | – |

## D. Attachment (does the file fit the intent?)

| Code | Failure | Detection | Reaction | Reply (DE, gist) | Built | Fixture |
|---|---|---|---|---|---|---|
| `ATTACHMENT_MISSING` | no attachment | none in thread | ASK | "Bitte den Wirtschaftsplan als Excel-Datei (.xlsx) anhängen." | ✅ | `mail_no_attachment.eml` |
| `ATTACHMENT_WRONG_TYPE` | `.pdf`, `.docx`, `.csv`, image | extension + magic bytes | ASK | "Erhalten: Plan.pdf. Ich benötige die Excel-Datei (.xlsx)." | ✅ | `mail_pdf.eml` |
| `ATTACHMENT_XLS_LEGACY` | old `.xls` | extension / OLE header | ASK (📐 product: convert) | "Bitte im Format .xlsx speichern." | ✅ | `plan.xls` |
| `ATTACHMENT_MACROS` | `.xlsm` | extension | read data only, **macros never run**; INFO | "Makros wurden nicht ausgeführt." | ✅ | `plan.xlsm` |
| `ATTACHMENT_MULTIPLE` | several `.xlsx` | count | pick the one that looks like a plan; 2+ candidates → ASK | "Welche Datei soll ich prüfen: A.xlsx oder B.xlsx?" | ✅ | `mail_two_xlsx.eml` |
| `ATTACHMENT_SAME_AS_BEFORE` | same file hash as an earlier run | hash lookup in run folders | compute; INFO | "Identisch mit dem Plan vom …" | ✅ | test only |

## E. Workbook (can we open it?)

| Code | Failure | Detection | Reaction | Reply (DE, gist) | Built | Fixture |
|---|---|---|---|---|---|---|
| `XLSX_ENCRYPTED` | password to open | file is an OLE container (`D0 CF 11 E0`) with `EncryptionInfo`, not a ZIP | ASK | "Die Datei ist passwortgeschützt. Bitte ohne Kennwort senden." | ✅ | `plan_password.xlsx` |
| `XLSX_SHEET_PROTECTED` | sheet/workbook protection only | flags in the file | read normally; no message | – | ✅ | `plan_sheet_protected.xlsx` |
| `XLSX_CORRUPT` | corrupt or renamed file | not a valid ZIP / openpyxl error | ASK | "Die Datei ist beschädigt oder keine Excel-Datei." | ✅ | `plan_corrupt.xlsx` |
| `XLSX_NO_CACHED_VALUES` | formulas without cached results | value `None` on a formula cell | recompute sub-totals from inputs (D6); if a needed **input** is uncomputed → PARTIAL | "Zelle Bilanz!D22 enthält eine Formel ohne berechneten Wert. Bitte in Excel speichern." | ✅ | `plan_no_cache.xlsx` |
| `XLSX_ERROR_CELL` | `#REF!`, `#DIV/0!`, `#NV` in a needed item | cell value | PARTIAL for the affected covenants | "Zelle GuV!D9 enthält #REF!." | ✅ | `plan_ref_error.xlsx` |
| `XLSX_EXTERNAL_LINK` | references to other workbooks | formula text / `externalLinks` part | use cached value; WARN | "Externer Bezug in …; verwendet: gespeicherter Wert." | ✅ | `plan_external_link.xlsx` |
| `XLSX_HIDDEN` | hidden sheets/rows with needed data | sheet/row state | read; INFO | "Ausgeblendete Zeilen berücksichtigt." | ✅ | `plan_hidden.xlsx` |

## F. Plan content (is it the right plan, and does it tie out?)

| Code | Failure | Detection | Reaction | Reply (DE, gist) | Built | Fixture |
|---|---|---|---|---|---|---|
| `PLAN_YEAR_MISMATCH` | year in the request ≠ year in the file | intent `plan_year` vs. header | BLOCK | "Angefragt: 2027, die Datei enthält Plan 2026." | ✅ | `plan_2026.xlsx` + `mail_asks_2027.eml` |
| `PLAN_YEAR_FILENAME` | year in the file name ≠ content | regex on file name | trust content; WARN | "Dateiname nennt 2026, Inhalt ist Plan 2027." | ✅ | `Wirtschaftsplan_2026.xlsx` (content 2027) |
| `PLAN_NO_PLAN_COLUMN` | only actuals, no plan column | header search | BLOCK | "Keine Planspalte gefunden (nur Ist-Werte)." | ✅ | `plan_actuals_only.xlsx` |
| `PLAN_COMPANY_MISMATCH` | company ≠ borrower in the contracts | cover sheet vs. profiles | BLOCK | "Der Plan gehört zu … , die Verträge zu Stadtwerke Musterstadt GmbH." | ✅ | `plan_other_company.xlsx` |
| `PLAN_UNITS_UNCLEAR` | T€ / € / Mio. € not detectable | unit labels | BLOCK | "Einheit nicht erkennbar. Bitte angeben (T€ / €)." | ✅ | `plan_no_units.xlsx` |
| `PLAN_UNITS_CONVERTED` | declared € / Mio. € instead of T€, also per sheet (e.g. Finanzierung in €) | unit label per sheet | normalise to T€; INFO | "Finanzierung in € angegeben, in T€ umgerechnet." | ✅ | `plan_in_eur.xlsx`, `plan_mixed_units.xlsx` |
| `PLAN_SCALE_IMPLAUSIBLE` | values ~×1000 off vs. contract loan amounts | total assets / bank debt vs. profile amounts | WARN | "Größenordnung auffällig: Bankverbindlichkeiten 148 vs. Darlehen 64 Mio. €. Einheit prüfen." | ✅ | `plan_wrong_unit_label.xlsx` |
| `PLAN_MAPPING_LLM` | items found only via step 2 (LLM mapping) | mapping source | INFO | "Zuordnung durch KI vorgeschlagen, durch Abstimmungsprüfungen bestätigt: …" | ✅ stretch | `plan_renamed_labels.xlsx`, `plan_one_sheet.xlsx` |
| `PLAN_ITEM_MISSING` | needed item not found after steps 1+2 | mapping result | PARTIAL | "V3 nicht berechenbar: Darlehensspiegel (Tilgungen) fehlt." | ✅ | `plan_no_financing.xlsx` |
| `PLAN_SUBITEM_NOT_SHOWN` | recipe excludes a sub-item the plan doesn't show separately (no "davon Aufzinsung") | building block missing, parent present | per recipe: PARTIAL or WARN with 0 | "Aufzinsung Pensionsrückstellungen nicht separat ausgewiesen; mit 0 angesetzt." | ✅ | `plan_no_unwinding.xlsx` |
| `PLAN_ITEM_NOT_IN_PLAN` | contract term with no plan counterpart (leasing, guarantees) | profile marks the term; no block found | WARN, 0 | "Leasingverbindlichkeiten im Plan nicht ausgewiesen, mit 0 angesetzt." | ✅ | test profile |
| `PLAN_FACT_AMBIGUOUS` | plan fact unclear (subordination note missing or vague) | pattern rules on note text | WARN, stated assumption; the result with the alternative assumption is shown too | "Rangrücktritt nicht eindeutig; Ergebnis ohne / mit Gesellschafterdarlehen: 3,37 / 3,57." | ✅ | `plan_no_subordination_note.xlsx` |
| `PLAN_LABEL_AMBIGUOUS` | same label twice (`Zinsaufwand`) | duplicate matches | choose by section; still ambiguous → step 2 / ASK | "Zinsaufwand kommt zweimal vor (GuV!D9, GuV!D20). Welcher Wert gilt?" | ✅ | `plan_duplicate_label.xlsx` |
| `PLAN_UNBALANCED` | assets ≠ liabilities | recomputed totals | BLOCK | "Bilanz nicht ausgeglichen: Aktiva 313.200, Passiva 312.900 T€." | ✅ | `plan_unbalanced.xlsx` |
| `PLAN_LOANS_MISMATCH` | loan schedule ≠ bank liabilities | Finanzierung total vs. Bilanz | BLOCK | "Darlehensspiegel 148.200 ≠ Bilanz 150.000 T€." | ✅ | `plan_loans_mismatch.xlsx` |
| `PLAN_INTEREST_TIEOUT` | loan interest + unwinding ≠ GuV interest | sums | WARN | "Zinsen laut Darlehensspiegel weichen von der GuV ab." | ✅ | `plan_interest_off.xlsx` |
| `PLAN_OPENING_BALANCE` | opening ≠ previous closing | sums | WARN | – | ✅ | `plan_opening_off.xlsx` |
| `PLAN_LOAN_NO_CONTRACT` | loan in plan without a contract | loan IDs vs. profiles | INFO | "Darlehen X ohne Vertrag im Vertragsordner." | ✅ | `plan_extra_loan.xlsx` |
| `PLAN_CONTRACT_NO_LOAN` | contract without loan in the plan | profiles vs. loan IDs | WARN | "Vertrag 3 fehlt im Darlehensspiegel." | ✅ | `plan_missing_loan.xlsx` |
| `PLAN_REPAYMENT_DIFFERS` | scheduled repayment ≠ contract schedule | profile vs. plan | WARN | – | ✅ | `plan_repayment_off.xlsx` |
| `PLAN_BULLET_IN_PLAN_YEAR` | a loan is due in full (bullet) in the plan year; the contract excludes refinanced bullets (V3 Anlage 3 (4)) | closing balance 0 with a large repayment; no plan note ruling out refinancing | include as scheduled; WARN with the alternative result | "Darlehen X endfällig 2027; falls refiniert, ist die Tilgung nicht einzubeziehen (DSCR dann …)." | ✅ | `plan_bullet.xlsx` |
| `PLAN_SIGNS` | expenses positive / mixed signs | section convention | normalise; WARN | "Vorzeichen vereinheitlicht." | ✅ | `plan_signs.xlsx` |
| `PLAN_IMPLAUSIBLE` | negative equity, EBITDA ≤ 0 | value checks | compute; flag; EBITDA ≤ 0 → leverage not meaningful (PARTIAL) | "EBITDA ≤ 0: Verschuldungsgrad nicht sinnvoll definiert." | ✅ | `plan_negative_ebitda.xlsx` |
| `PLAN_STATUS` | draft vs. approved | cover sheet | INFO | "Entwurf vom 15.10.2026, nicht vom Aufsichtsrat genehmigt." | ✅ | sample plan |
| `PLAN_STATUS_REQUIRED` | the contract requires an approved plan, the file is a draft (V2 § 12 (1) c) | profile `plan_status_required` vs. cover sheet | compute; INFO per contract | "Hinweis: Die Förderbank verlangt den beschlossenen Wirtschaftsplan; der vorliegende Plan ist ein Entwurf." | ✅ | sample plan |
| `PLAN_LOAN_AMBIGUOUS` | a loan-schedule row matches several contracts (e.g. lender name "Sparkasse" in V1 and the V4 syndicate) | `loans[].match`, loan number first | match by loan number; still ambiguous → WARN, repayment check skipped for that loan | "Darlehenszeile … nicht eindeutig einem Vertrag zuzuordnen." | ✅ | `plan_no_loan_numbers.xlsx` |

## G. Contracts and profiles

| Code | Failure | Detection | Reaction | Reply (DE, gist) | Built | Fixture |
|---|---|---|---|---|---|---|
| `CONTRACT_CHANGED` | contract file hash ≠ profile hash | hash at start | compute with the old profile; WARN (also when the PDF is missing); `finn contracts check` fails | "Vertrag 4 wurde geändert; Profil nicht neu geprüft." | ✅ | test only |
| `PROFILE_MISSING` | no / invalid profile | load + schema validation | that contract "nicht geprüft"; others continue | "Vertrag X konnte nicht geprüft werden." | ✅ | test only |
| `PROFILE_INVALID_RECIPE` | recipe uses an item / condition / operation outside the whitelist | validation at onboarding and at load | that covenant `manual_review` | "Covenant … kann nicht automatisch berechnet werden." | ✅ | test profile |
| `PROFILE_EXAMPLE_MISMATCH` | the recipe applied to the contract's own sample calculation doesn't give the printed result (V3 Anlage 3 (8): 1.54) | `finn contracts check` | profile stays `draft`; review required | (onboarding only, no mail) | ✅ | test profile |
| `PROFILE_INVARIANT` | `has_financial_covenants` disagrees with `covenants[]`, or `no_covenant_reason` missing | schema validator | profile rejected | (onboarding only) | ✅ | test profile |
| `PROFILE_NOT_REVIEWED` | profile `review.status = draft` used at runtime | load | compute; WARN | "Vertragsprofil V… ist noch nicht fachlich geprüft." | ✅ | test profile |
| `PROFILE_UNMAPPED` | `unmapped_components` non-empty | profile | `effect: manual_review` → that covenant "manuelle Prüfung"; `effect: assumption` (the plan cannot show it either, V4 (e)) → computed, INFO with the handling text | "Covenant … enthält Bestandteile, die ich nicht automatisch abbilde: …" | ✅ | test profile |
| `CONTRACT_NEW_FILE` | contract in the folder without a profile | folder scan | WARN; product: start onboarding | "Neuer Vertrag ohne geprüftes Profil: …" | ✅ detect | test only |
| `PROFILE_QUOTE_MISMATCH` | a `sources[]` quote is not verbatim on its stated page (whitespace / typographic quotes normalised) | `finn contracts check` / `extract` / `approve` (`contracts/validate.py`) | near misses (wrong page, punctuation only) are repaired at extraction and noted; anything else: `finn contracts check` fails, `approve` refuses | (onboarding only) | ✅ | unit test |
| `PROFILE_INCOMPLETE` | completeness scan: a clause naming a ratio (Eigenkapitalquote, Verschuldungsgrad, DSCR, EBITDA, Kennzahl, Covenant) or a %/ratio bound is covered neither by a source of the profile nor by restating known covenants | `finn contracts check` / `extract` (`contracts/completeness.py`) | hint; the reviewer adds the covenant or a dismissed mention | (onboarding only) | ✅ | mutated gold profiles |

## H. Calculation

| Code | Failure | Detection | Reaction | Reply (DE, gist) | Built | Fixture |
|---|---|---|---|---|---|---|
| `CALC_DENOMINATOR` | denominator ≤ 0 | engine | PARTIAL with reason | "Kapitaldienst = 0, DSCR nicht definiert." | ✅ | unit test |
| `CALC_AT_THRESHOLD` | rounded value = threshold | engine | met, headroom 0, highlighted | "Eingehalten, kein Spielraum." | ✅ | unit test |
| `CALC_NO_ROUNDING_RULE` | contract specifies no rounding | profile | 2 decimals; INFO | "Vertrag nennt keine Rundung; 2 Nachkommastellen." | ✅ | V2 |

## I. Reply and send

| Code | Failure | Detection | Reaction | Built | Fixture |
|---|---|---|---|---|---|
| `REPLY_LLM_UNAVAILABLE` | LLM down after retries | activity failure | German template reply | ✅ | mocked LLM |
| `REPLY_NUMBER_MISMATCH` | LLM text changed/added a number | number check | discard LLM text → template | ✅ | mocked LLM |
| – (no code) | SMTP unavailable | exception | Temporal retries | ✅ | integration test |
| `SEND_ALREADY_DONE` | retry after a successful send | `was_sent(message_id)` | skip | ✅ | integration test |
| `SEND_BOUNCED` | recipient bounces | bounce mail in inbox | log; operator alert | ✅ log | `mail_bounce.eml` |

## J. Waiting for input

| Code | Failure | Detection | Reaction | Built |
|---|---|---|---|---|
| `WAIT_TIMEOUT` | no follow-up within 7 days | timer | end workflow (📐 reminder after 2 days) | ✅ end |
| `WAIT_OTHER_SENDER` | follow-up from another person in the thread | `From` ≠ original | accept if they pass the gate | ✅ |
| `WAIT_LATE_FOLLOW_UP` | follow-up after the timeout | no waiting workflow | new workflow; request recovered from the thread | ✅ |

## K. Infrastructure

| Code | Failure | Reaction | Built |
|---|---|---|---|
| `INFRA_IMAP_DOWN` | IMAP unreachable | poller logs, backs off, next cycle; mail stays in the inbox | ✅ |
| `INFRA_MAILBOX_AUTH` | mailbox login fails | non-retryable; operator alert | ✅ |
| `INFRA_WORKER_CRASH` | worker restarts | Temporal resumes at the last completed step | ✅ |
| `INFRA_IMAP_FOLDER_MISSING` | `IMAP_FOLDER` (Gmail label) does not exist | SELECT → NONEXISTENT | log error with the fix (create label + filter), back off, next cycle; never falls back to INBOX | ✅ |
| `INFRA_UNEXPECTED` | any unhandled exception | sender: polite mail with reference ID; operator: `error.txt` + stack trace | ✅ |
