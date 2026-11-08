# Known cleanups

Left open after the refactor: no behaviour depends on them.

**Code size and structure**
- Modules over ~300 lines: `plan/reader.py` (627), `reply/calc_path.py` (559), `reply/render.py` (511), `contracts/draft.py` (507), `calc/engine.py` (480), `models/contract.py` (382), `plan/labels.py` (364), `contracts/eval.py` (362), `pipeline.py` (359), `cli.py` (355), `mail/imap_smtp.py` (346), `plan/formulas.py` (345), `errors.py` (333).
- Some functions in `plan/reader.py`, `reply/calc_path.py` and `calc/engine.py` are well over 50 lines.
- Code comments still cite merged decision IDs (D4, D11, D19, D20, D22, D24); they resolve through the pointers in [DECISIONS.md](DECISIONS.md).
- `errors.SEVERITY` still lists the log-only codes (gate, send, wait, infrastructure), which `finding()` never uses.

**Stubs and unused parts**
- Gate: tenant and licence checks are stubs (one tenant, always active).
- `gate.AuthSummary` parses SPF and DKIM, but only DMARC is used.
- Three LLM stand-ins with overlapping jobs: `llm/fake.py` (live tests, demo), `mailkit.llm_unavailable` and `mailkit.llm_returning` (unit tests).

**Prompts**
- The extraction and review prompts (`contracts/prompts/`) repeat schema explanations and trap rules. Merging them changes the prompt hash, so it needs a re-extraction, a 95/95 eval and a re-approval of the profiles.
- The intent and polish prompts are not versioned like the contract prompts.

**Tests**
- Older test modules define their own `ROOT` / `SAMPLE` constants instead of using `mailkit`.

**Deployment**
- `/var/log/finn-bootstrap.log` holds the lines of the first deploy twice: the bootstrap and `finn-deploy` both `tee` into it. Fixing `finn-deploy` changes the user data and would replace the instance, so it waits for the next change that needs a new instance anyway.
- `aws/cdk.context.json` (pinned AMI) is git-ignored; the CDK guidance is to commit it.

**Repository**
- `skills-lock.json` (agent tooling) sits in the repo root; the task sheet is named `Aufgabenblatt_Case_Study_Finn (1).pdf`.
