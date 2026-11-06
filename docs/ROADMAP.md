# Roadmap: from case study to product

Not built in the case study. Source material for the note ("was ich mit mehr Zeit als Nächstes bauen würde") and the interview.

## 1. Reporting duties found in the contracts

### Contract 1 – Sparkasse (§ 12, p. 6)
| Duty | Deadline |
|---|---|
| Audited annual accounts + management report + auditor's opinion | 6 months after year end, **at the latest 14 days after formal approval** (whichever is earlier) |
| Business plan (income, asset and financial plan, staff overview), **approved** by the supervisory board | 1 month after approval, at the latest **31 Jan** |
| § 53 HGrG audit report | if carried out |
| Immediate notices | event of default, legal form / ownership change, loss of a concession, regulatory decisions on revenue caps |

Other dates: special repayment up to €1M per year without penalty, lapses on 31 Dec (§ 8(2)); end of fixed interest **30.03.2031**, bank offer 3 months before (§ 6(3)).

### Contract 2 – Förderbank (§ 12, p. 5)
| Duty | Deadline |
|---|---|
| Annual accounts + management report | 180 days after 31 Dec (≈ 29 Jun) |
| Covenant certificate (economic equity ratio), template **Anlage 2** | with the accounts |
| Business plan + **forecast of the economic equity ratio** | **31 Jan** of the plan year |
| Report on operating the funded assets | yearly by **31 Mar** (10-year purpose lock) |
| Notice when a covenant breach is expected | immediately (§ 12(3)) |

⚠️ Information duties unfulfilled 30 days after a reminder → termination right (§ 15(1)f).

### Contract 3 – Landesbank (§ 12, p. 5)
| Duty | Deadline |
|---|---|
| Annual accounts + management report + auditor's opinion | 180 days after year end |
| Covenant certificate with DSCR and detailed calculation | with the accounts |
| Business plan **+ expected DSCR**, draft accepted | **15 Dec** of the previous year |
| Half-year report | 90 days after 30 Jun (≈ 28 Sep) |
| Planned DSCR < 1.30 | measures within 30 days (§ 13(3)) |
| Cure by equity injection | within 30 days of the certificate; max. twice, not consecutive (§ 13(4)) |

Events: Tranche B (€15M) drawable 1 Jan – 30 Jun 2027 only (§ 2(2)b); requires the 2025 certificate with DSCR ≥ 1.30 (§ 4(3)); drawdown notice 5 bank working days ahead; commitment fee 0.30 % on undrawn amounts from 1 Jan 2027.

### Contract 4 – Syndicated loan (§ 13, p. 6)
| Duty | Deadline |
|---|---|
| Annual accounts + management report + audit report | **150 days** after year end (≈ 30 May) |
| Compliance certificate (equity ratio + leverage), template **Anlage 4**, signed by **two** managing directors | with the accounts; sets the margin from the next interest period |
| Business plan incl. loan schedule + indicative covenant calculation | **within 30 days of drawing up the plan, at the latest 31 Dec** ⚠️ |
| Immediate notices | material litigation, regulatory decisions on revenue caps |

⚠️ Plan dated 15.10.2026: if the draft counts as "drawn up", the deadline was around 14 Nov 2026. Ambiguous; Finn should flag, not decide.
Events: voluntary special repayment (Q3 2027 in the plan) needs 10 bank working days' notice, at the end of an interest period (§ 8(1)); interest swap until 30.11.2028; no equity cure (§ 14(4)).

### Resulting yearly calendar
```
15 Dec   Landesbank       plan (draft OK) + DSCR forecast
31 Dec   Syndicated loan  plan + indicative covenants (possibly earlier)
31 Jan   Sparkasse        approved plan
31 Jan   Förderbank       plan + equity-ratio forecast
31 Mar   Förderbank       operating report
~30 May  Syndicated loan  accounts + compliance certificate (2 signatures) → new margin
~29 Jun  Förderbank       accounts + covenant certificate
~29 Jun  Landesbank       accounts + DSCR certificate
30 Jun   Sparkasse        accounts (or 14 days after approval)
~28 Sep  Landesbank       half-year report
```

## 2. Product stages

| Stage | Finn does | Human role |
|---|---|---|
| 1. Covenant check (case study) | plan by mail → check → reply with sources | requests, reads |
| 2. Duties calendar | extracts reporting duties (what, deadline rule, recipient, signatories, template, source), computes concrete dates, reminds | – |
| 3. Draft and approve | drafts each bank's cover mail, fills certificate templates (Anlage 2 / 4) with calculated figures | **CFO reviews, edits, approves** |
| 4. Status tracking | due → drafted → approved → sent → confirmed, per duty | – |
| 5. Continuous monitoring | quarterly / half-year forecasts against 31.12., sensitivity, early warnings | CFO |
| 6. Event deadlines | drawdown windows, prepayment notice periods, end of fixed interest, swap expiry | Finn reminds |
| 7. Q&A | "why is V4 tight?" from run records + contract text; cross-contract questions (RAG becomes useful here) | anyone authorised |

Stages 2–6 map onto Temporal primitives already in place: durable timers (reminders), signals (approval), workflows per duty.

## 3. Contract profiles as building blocks

Contracts differ but are composed of a small set of item types:

| Item type | Example |
|---|---|
| Basic loan data | lender, amount, term, loan number |
| Defined terms | Eigenkapital, Finanzverbindlichkeiten, EBITDA |
| Financial covenants | metric, definition parameters, threshold, test date, rounding |
| Reporting duties | document, deadline rule, recipient, signatories, template |
| Event notices | change of control, litigation, regulatory decisions |
| Repayment schedule | dates, amounts, tranches |
| Interest terms / margin grid | fixed / floating, margin steps |
| Drawdown windows and conditions | Tranche B window and DSCR condition |
| Prepayment rights and notice periods | special repayment, notice in bank working days |
| Events of default | cross-default thresholds |

Each type gets its own extractor with the same pattern (full text → schema → quote check → review). Extractors share the contract registry, page-text cache and defined terms. An **intent router** sends each request to the right module; modules fail independently.

## 4. Platform

- **Customer workspace (UI):** upload contracts, review/correct extractions (confidence, quotes, "what we're unsure about"), approve profiles, plan template mapping, duties calendar, run history, exports. Approved profiles are the source of truth.
- **Operator console (UI, run by us):** traces, errors, costs per customer, alerts.
- **Multi-tenancy:** strict separation of mailboxes, profiles, data and traces per utility.
- **Licensing and access:** tenant licence status (active / trial / expired), activation code at onboarding, **verified domains** (DNS TXT), users with roles (requester / approver), seats. Check per mail: tenant (by receiving mailbox) → licence → SPF/DKIM/DMARC → domain → user. Rejections logged; no replies to unknown senders (no information leak, no backscatter); alerts on repeated attempts.
- **Mail connectors** (same interface as the case study's IMAP connector, D19). Preferred setup: **a Finn mailbox in the customer's own domain**.
  - **Microsoft 365 (shared mailbox):** the customer creates a shared mailbox (e.g. `finn@stadtwerke-x.de`); their admin grants consent to our multi-tenant Entra app (`Mail.ReadWrite`, `Mail.Send`, application permissions) and restricts it to that one mailbox (RBAC for Applications / Application Access Policy); they send us their tenant ID. We poll with Graph **delta queries** and reply via `/reply` (threading by M365).
  - **Google Workspace:** service account with domain-wide delegation limited to the Finn mailbox; `history.list` for new mail, send with `threadId`.
  - **Generic IMAP/SMTP:** for everyone else (case-study connector).
  - **Direct inbox (no customer mailbox):** the customer mails our address (e.g. `stadtwerke-x@finn.app`); tenant by recipient address. Needs our own domain + MX. Forwarding from the customer's mailbox breaks SPF, and M365 blocks external auto-forwarding by default.
  - **Scaling ingestion:** push (Graph change notifications, Gmail Pub/Sub) behind a **FastAPI** endpoint as trigger, plus periodic delta reconciliation, plus Message-ID dedup. Push alone can miss events.
  - **Build vs. buy (checked Nov 2026):** Nylas (hosted, mail leaves us), EmailEngine (self-hosted, commercial, ~€1,200/year, stops without a licence), open source (Nylas Sync Engine unmaintained; imapapi IMAP only). Decision: build (D19); revisit at hundreds of mailboxes or real-time needs.
- **Licence expiry policy (designed, not built):** reply once to the sender, notify the tenant admin (contact from company settings), then ignore further mail from that tenant (logged).
- **Plan profiles:** LLM-proposed mapping of a new plan template, verified by accounting checks, confirmed once per customer, reused yearly.

## 5. Operations and resilience

- Temporal in production: self-hosted with Postgres on AWS/Azure, or Temporal Cloud with **client-side payload encryption** (Temporal sees only encrypted data).
- LLM provider failover: retries with backoff first, then a second provider with the same structured schema; validation guards schema drift. Health metrics per provider.
- Observability: Temporal → OpenTelemetry → Langfuse for one end-to-end trace per request; evaluation datasets (extraction vs. reviewed profiles) to catch regressions when models or prompts change.
- Data protection: EU hosting, data-processing agreements (AVV) with the LLM provider and tracing, self-hosted Langfuse option, retention rules for run folders and traces.
  - LLM provider terms to verify before go-live: no training on API data, zero data retention, EU data residency.
  - Alternatives: Azure OpenAI in an EU region; a small self-hosted model for the intent call (simple classification).
  - NER-based anonymisation (e.g. Microsoft Presidio) for free-text mails with names and companies in the body, on top of D21.
- Versioning: contract profile versions (who approved what, when); workflow versioning for long-running waits.
- Webhooks instead of polling for instant processing at scale.
