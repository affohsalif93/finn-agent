# Failure catalogue

Every way a request can go wrong, how Finn reacts, and which test proves it. Codes are `ErrorCode` in [`src/finn/errors.py`](../src/finn/errors.py); the German reply texts live there too (`MESSAGES_DE`). Principle: fail closed (D7).

**Tests:** `module::test` = `tests/**/test_<module>.py`; **live** = a case in [`tests/live/cases.py`](../tests/live/cases.py) (`pytest -m mailserver`).

| Severity | Reaction |
|---|---|
| REJECT | gate: security event in the log, **no reply**, nothing parsed |
| IGNORE | skipped silently (loops, duplicates) |
| ASK | reply with exactly what is missing; the workflow **waits** for a follow-up (timeout `WAIT_FOR_INPUT_DAYS`) |
| BLOCK | input contradicts itself or the request: no result is confirmed; explain, wait for a corrected file |
| PARTIAL | that covenant is "nicht berechenbar" with the reason, the others are reported |
| WARN / INFO | result computed; noted in the reply |

## Gate and mail

| Code | Situation | Reaction | Test |
|---|---|---|---|
| `GATE_SENDER_REJECTED` | sender not allowed by `ALLOWED_SENDERS` | REJECT | `mail_gate::test_unknown_sender_with_allowlist`; live `blocked_sender`, `unknown_sender` |
| `GATE_AUTH_FAILED` | DMARC fails and `REQUIRE_DMARC_PASS` is on | REJECT | `mail_gate::test_dmarc_failure_rejected_only_when_required` |
| `GATE_LOOP` | auto-reply, bulk, own mail | IGNORE | `mail_gate::test_loop_headers`; live `auto_reply` |
| `GATE_NOT_ADDRESSED` | mail in Finn's folder not addressed to `FINN_ADDRESS` | REJECT | `mail_gate::test_recipient_must_be_finn` |
| `GATE_DUPLICATE` | same Message-ID again | IGNORE (workflow ID = Message-ID) | `covenant_workflow::test_duplicate_start_is_ignored`; live `duplicate` |
| `MAIL_TOO_LARGE` | mail above `MAX_MAIL_MB` (only the header is fetched) | ASK | `intent::test_too_large_mail`; live `too_large` |
| `MAIL_UNPARSEABLE` | broken MIME | raw `.eml` kept, polite reply | – |
| `ATTACHMENT_TNEF` / `_ARCHIVE` / `_LINK_ONLY` | `winmail.dat`, `.zip`, only a link | ASK | `intent::test_fallback_without_anything_asks` |
| `ATTACHMENT_MISSING` | no plan in the thread | ASK, wait, continue on the follow-up | `covenant_workflow::test_missing_attachment_ask_then_follow_up`; live `no_attachment_then_plan` |
| `ATTACHMENT_WRONG_TYPE` | `.pdf`, `.docx`, image | ASK | live `pdf` |
| `ATTACHMENT_MULTIPLE` | several workbooks in one mail | ASK which one | live `two_xlsx` |
| `ATTACHMENT_XLS_LEGACY` | old `.xls` | ASK for `.xlsx` | `plan_fixtures::test_variant_findings`; live `xls_legacy` |
| `ATTACHMENT_MACROS` | `.xlsm` | data read, macros never run; INFO | `plan_fixtures::test_xlsm_macros_info` |

## Intent

| Code | Situation | Reaction | Test |
|---|---|---|---|
| – | LLM unavailable | rule fallback (workbook / keywords → covenant check) | `intent::test_fallback_without_anything_asks` |
| `INTENT_AMBIGUOUS` | vague request with a workbook | proceed, state the assumption; INFO | `intent::test_fallback_without_anything_asks` |
| `INTENT_PARTLY_UNSUPPORTED` | several requests, some unsupported | check, list the rest; INFO | `intent::test_llm_answer_used_and_prompt_pseudonymised` |
| `INTENT_CONTRACT_FILTER` | only some contracts requested | filter, name the others; INFO | `intent::test_llm_answer_used_and_prompt_pseudonymised` |
| – | off-topic or question about an earlier result | short polite reply, no calculation | live `off_topic` |

## Workbook and plan content

| Code | Situation | Reaction | Test |
|---|---|---|---|
| `XLSX_ENCRYPTED` / `XLSX_CORRUPT` | password or broken file | ASK | `plan_fixtures::test_variant_findings`; live `password`, `corrupt` |
| `XLSX_NO_CACHED_VALUES` | formula without cached value, not recomputable (D22) | PARTIAL | `plan_fixtures::test_variant_findings` |
| `XLSX_ERROR_CELL` | `#REF!` etc. in a needed cell | PARTIAL | `plan_fixtures::test_ref_error_items`; live `ref_error` |
| `XLSX_EXTERNAL_LINK` | link to another workbook | cached value used; WARN | – |
| `XLSX_HIDDEN` | needed data on hidden rows / sheets | read; INFO | `plan_fixtures::test_variant_findings` |
| `PLAN_YEAR_MISMATCH` | year in the request ≠ year in the file | BLOCK | `plan_checks::test_plan_year_mismatch`; `pipeline_eml::test_plan_year_mismatch_blocks`; live `year_mismatch` |
| `PLAN_YEAR_FILENAME` | file name names another year | content wins; WARN | `plan_fixtures::test_filename_year_differs_from_content` |
| `PLAN_NO_PLAN_COLUMN` | actuals only | BLOCK | `plan_fixtures::test_variant_findings` |
| `PLAN_COMPANY_MISMATCH` | company ≠ borrower | BLOCK | `plan_checks::test_company_mismatch_blocks` |
| `PLAN_UNITS_UNCLEAR` | unit not detectable | BLOCK | `plan_fixtures::test_no_units_one_block_per_sheet` |
| `PLAN_UNITS_CONVERTED` | sheet in € / Mio. € | normalised to T€; INFO | `plan_fixtures::test_eur_sheet_converted`; live `in_eur` |
| `PLAN_SCALE_IMPLAUSIBLE` | bank debt ~×1000 off vs. contract amounts | WARN | `plan_checks::test_bank_debt_off_by_factor_1000_warns` |
| `PLAN_ITEM_MISSING` | needed building block not found | PARTIAL for the covenants using it | `plan_fixtures::test_no_financing_is_partial`; live `no_financing` |
| `PLAN_SUBITEM_NOT_SHOWN` | no "davon Aufzinsung" line | per recipe PARTIAL or 0 + WARN | `plan_fixtures::test_no_unwinding_not_invented` |
| `PLAN_ITEM_NOT_IN_PLAN` | contract term without plan line (leasing, bonds) | 0 + WARN | `answer_key::test_findings_and_notes` |
| `PLAN_FACT_AMBIGUOUS` | subordination / cash availability unclear | conservative value + alternative result; WARN | `answer_key::test_plan_no_subordination_note_conservative_with_alternative`; live `no_subordination_note` |
| `PLAN_LABEL_AMBIGUOUS` | same label twice | ASK | – |
| `PLAN_UNBALANCED` | assets ≠ liabilities (recomputed) | BLOCK | `plan_fixtures::test_unbalanced_message`; live `unbalanced` |
| `PLAN_LOANS_MISMATCH` | loan schedule ≠ bank liabilities | BLOCK | `plan_fixtures::test_variant_findings` |
| `PLAN_INTEREST_TIEOUT` / `PLAN_OPENING_BALANCE` | interest or opening balances don't tie out | WARN | `plan_fixtures::test_variant_findings` |
| `PLAN_LOAN_NO_CONTRACT` / `PLAN_CONTRACT_NO_LOAN` | loan without contract / contract without loan | INFO / WARN | `loan_checks::test_repayment_differs_and_contract_missing` |
| `PLAN_REPAYMENT_DIFFERS` | planned repayment ≠ contract schedule | WARN | `loan_checks::test_repayment_differs_and_contract_missing` |
| `PLAN_LOAN_AMBIGUOUS` | a loan line matches several contracts | WARN, repayment check skipped | `loan_checks::test_loan_matching_without_numbers_and_ambiguity` |
| `PLAN_BULLET_IN_PLAN_YEAR` | bullet repayment in the plan year (V3 excludes refinanced bullets) | WARN with the alternative result | `answer_key::test_plan_bullet_states_refinancing_alternative` |
| `PLAN_SIGNS` | mixed sign conventions | normalised; WARN | `plan_fixtures::test_signs_normalised_values_unchanged` |
| `PLAN_IMPLAUSIBLE` | negative equity, EBITDA ≤ 0 | WARN; leverage not meaningful (PARTIAL) | `rounding_and_edges::test_ebitda_not_positive_leverage_not_meaningful` |
| `PLAN_STATUS` / `PLAN_STATUS_REQUIRED` | draft plan; V2 requires the approved plan | INFO | `answer_key::test_findings_and_notes` |

## Contracts and calculation

| Code | Situation | Reaction | Test |
|---|---|---|---|
| `CONTRACT_CHANGED` / `CONTRACT_NEW_FILE` | PDF hash ≠ profile, PDF without profile | WARN | `store::test_sha_mismatch_and_draft_warn`, `store::test_contract_without_profile_and_missing_file` |
| `PROFILE_MISSING` / `PROFILE_INVARIANT` | profile unreadable or contradictory | that contract not checked | `store::test_broken_json_is_profile_missing`, `store::test_invariant_and_unknown_source` |
| `PROFILE_INVALID_RECIPE` | recipe outside the whitelist, bad ref, cycle | that covenant `manual_review` | `store::test_bad_ref_makes_only_that_covenant_manual_review` |
| `PROFILE_UNMAPPED` | contract part the plan cannot show | `manual_review` or stated assumption | `store::test_unmapped_manual_review_blocks_the_covenant` |
| `PROFILE_NOT_REVIEWED` | profile still `draft` | WARN | `store::test_sha_mismatch_and_draft_warn` |
| `PROFILE_EXAMPLE_MISMATCH` | contract's own example not reproduced | onboarding fails | `store::test_example_mismatch`, `cli::test_cli_contracts_check_fails_on_mismatch` |
| `PROFILE_QUOTE_MISMATCH` / `PROFILE_INCOMPLETE` | quote not verbatim; covenant signal not covered | onboarding fails / hint | `validate::test_quote_near_misses_and_missing`, `completeness::test_missing_dismissal_is_reported` |
| `CALC_DENOMINATOR` | denominator ≤ 0 | PARTIAL | `rounding_and_edges::test_denominator_zero_is_not_computable` |
| `CALC_AT_THRESHOLD` | rounded value = limit | met, no headroom; WARN | `rounding_and_edges::test_equity_ratio_rounding_edges` |
| `CALC_NO_ROUNDING_RULE` | contract names no rounding (V2) | 2 decimals; INFO | `answer_key::test_findings_and_notes` |

## Reply, waiting, infrastructure (logged)

| Code | Situation | Reaction | Test |
|---|---|---|---|
| `REPLY_LLM_UNAVAILABLE` | polish LLM down | template text | `reply_render::test_polish_llm_unavailable` |
| `REPLY_NUMBER_MISMATCH` | polished text changed a number or placeholder | template text | `reply_render::test_polish_with_new_number_falls_back` |
| – | SMTP down | `send_reply` retried (~1 h) | `covenant_workflow::test_send_retried_once_and_sent_exactly_once` |
| `SEND_ALREADY_DONE` | retry after a successful send | skipped (sent marker / Sent folder) | `covenant_workflow::test_already_in_sent_folder_is_not_sent_again`, `pipeline_eml::test_send_is_idempotent` |
| `WAIT_TIMEOUT` | no follow-up in time | run ends, no mail | `covenant_workflow::test_wait_timeout_ends_the_run` |
| `INFRA_IMAP_DOWN` / `INFRA_MAILBOX_AUTH` | IMAP unreachable / login fails | back off, next cycle / loud log, long pause | – |
| `INFRA_IMAP_FOLDER_MISSING` | `IMAP_FOLDER` does not exist | log with the fix, back off; never INBOX | `covenant_workflow::test_folder_missing_propagates_to_the_loop` |
| `INFRA_UNEXPECTED` | any other exception | `error.txt` + polite mail with the reference | `pipeline_eml::test_unexpected_error_sends_polite_mail` |

## Designed, not built
Licence check per tenant (gate stub), bounce handling, "same file as last time" notice, LLM plan-mapping fallback (D6), reminder before the wait timeout, answers to questions about an earlier result. See [ROADMAP.md](ROADMAP.md).
