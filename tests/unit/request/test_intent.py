"""Intent: LLM classification (mocked), rule fallback, input contracts (FAILURES §C/§D)."""

from pathlib import Path
from typing import Any

import pytest

from finn.errors import ErrorCode
from finn.llm import client
from finn.mail.parse import inbound_from_bytes
from finn.models.mail import ParsedMessage
from finn.models.run import ClassifyInput, ParseInput
from finn.pipeline import classify_intent, parse_message
from finn.request.intent import ContractInfo, IntentAnswer, check_input, rule_fallback
from mailkit import FINN, build_mail, fixture_mails, llm_returning, settings

pytestmark = pytest.mark.usefixtures("no_llm")

CONTRACTS = [
    ContractInfo("V1", "Sparkasse", False),
    ContractInfo("V2", "Förderbank", True),
    ContractInfo("V3", "Landesbank", True),
    ContractInfo("V4", "Konsortialkredit", True),
]


def _parsed(raw: bytes, tmp_path: Path, name: str = "m") -> ParsedMessage:
    run = tmp_path / name
    run.mkdir(parents=True, exist_ok=True)
    eml = run / "incoming.eml"
    eml.write_bytes(raw)
    inbound = inbound_from_bytes(raw, mailbox=FINN, keep_raw=False).model_copy(
        update={"raw_eml_path": str(eml)}
    )
    return parse_message(ParseInput(inbound=inbound, run_dir=str(run)), settings(tmp_path))


def _classify(thread: list[ParsedMessage], tmp_path: Path):  # type: ignore[no-untyped-def]
    run = thread[0].run_dir
    return classify_intent(
        ClassifyInput(thread=thread, run_dir=run, workflow_id="wf", session_id="s"),
        settings(tmp_path),
    )


def test_fallback_xlsx_and_keywords(tmp_path: Path) -> None:
    c = _classify([_parsed(fixture_mails()["request"], tmp_path)], tmp_path)
    assert c.request.intent == "covenant_check"
    assert c.request.source == "rules"
    assert c.request.plan_year == 2027
    assert c.request.confidence >= 0.9
    assert c.input.status == "ok"
    assert c.input.plan is not None
    assert c.input.plan.filename == "Wirtschaftsplan_2027.xlsx"


def test_fallback_without_anything_asks(tmp_path: Path) -> None:
    raw = build_mail(subject="Hallo", body="Wie geht es?")
    c = _classify([_parsed(raw, tmp_path)], tmp_path)
    assert c.request.intent == "covenant_check"
    assert c.input.status == "ask"
    codes = [f.code for f in c.input.findings]
    assert codes[0] == ErrorCode.ATTACHMENT_MISSING
    assert ErrorCode.INTENT_AMBIGUOUS in codes


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("no_attachment", ErrorCode.ATTACHMENT_MISSING),
        ("two_xlsx", ErrorCode.ATTACHMENT_MULTIPLE),
        ("pdf", ErrorCode.ATTACHMENT_WRONG_TYPE),
        ("tnef", ErrorCode.ATTACHMENT_TNEF),
        ("zip", ErrorCode.ATTACHMENT_ARCHIVE),
        ("link", ErrorCode.ATTACHMENT_LINK_ONLY),
    ],
)
def test_input_contract_asks(tmp_path: Path, fixture: str, code: ErrorCode) -> None:
    check = check_input([_parsed(fixture_mails()[fixture], tmp_path)], 25)
    assert check.status == "ask"
    assert [f.code for f in check.findings] == [code]


def test_two_xlsx_one_named_like_a_plan(tmp_path: Path) -> None:
    from mailkit import plan_attachment

    raw = build_mail(attachments=[plan_attachment("Anlage.xlsx"), plan_attachment()])
    check = check_input([_parsed(raw, tmp_path)], 25)
    assert check.status == "ok"
    assert check.plan is not None
    assert check.plan.filename == "Wirtschaftsplan_2027.xlsx"


def test_follow_up_supplies_the_plan(tmp_path: Path) -> None:
    first = _parsed(fixture_mails()["no_attachment"], tmp_path, "a")
    from mailkit import plan_attachment

    second = _parsed(
        build_mail(subject="AW: x", message_id="f@x", attachments=[plan_attachment()]),
        tmp_path,
        "b",
    )
    assert check_input([first], 25).status == "ask"
    check = check_input([first, second], 25)
    assert check.status == "ok"


def test_too_large_mail(tmp_path: Path) -> None:
    parsed = _parsed(fixture_mails()["request"], tmp_path)
    big = parsed.model_copy(
        update={"inbound": parsed.inbound.model_copy(update={"too_large": True, "size": 40 << 20}),
                "attachments": []}
    )  # fmt: skip
    check = check_input([big], 25)
    assert [f.code for f in check.findings] == [ErrorCode.MAIL_TOO_LARGE]
    assert "40,0 MB" in check.findings[0].message_de


def test_llm_answer_used_and_prompt_pseudonymised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def answer(kwargs: dict[str, Any]) -> IntentAnswer:
        seen.update(kwargs)
        return IntentAnswer(
            intent="covenant_check",
            plan_year=2027,
            contract_ids=["V3", "V99"],
            confidence=0.95,
            assumptions=[],
            unsupported=["Fristenprüfung"],
        )

    monkeypatch.setattr(client, "complete_structured", llm_returning({"intent": answer}))
    c = _classify([_parsed(fixture_mails()["request"], tmp_path)], tmp_path)
    assert c.request.source == "llm"
    assert c.request.contract_filter == ["V3"]  # unknown ids dropped
    codes = [f.code for f in c.input.findings]
    assert ErrorCode.INTENT_PARTLY_UNSUPPORTED in codes
    assert ErrorCode.INTENT_CONTRACT_FILTER in codes
    assert seen["schema"] is IntentAnswer
    assert seen["trace_id"] == "wf"
    user = seen["user"]
    assert "max.mustermann" not in user.lower()
    assert "Mustermann" not in user
    assert "+49 421" not in user
    assert "Wirtschaftsplan_2027.xlsx (xlsx)" in user
    # the call is logged pseudonymised in the run folder
    log = (Path(c.input.plan.path).parents[1] / "llm_calls.jsonl").read_text(encoding="utf-8")
    assert "Mustermann" not in log


def test_llm_off_topic_with_workbook_still_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = IntentAnswer(
        intent="other", plan_year=None, contract_ids=[], confidence=0.8, assumptions=[],
        unsupported=[],
    )  # fmt: skip
    monkeypatch.setattr(client, "complete_structured", llm_returning({"intent": other}))
    c = _classify([_parsed(fixture_mails()["request"], tmp_path)], tmp_path)
    assert c.request.intent == "covenant_check"
    no_plan = _classify([_parsed(fixture_mails()["no_attachment"], tmp_path, "n")], tmp_path)
    assert no_plan.request.intent == "other"


def test_rule_fallback_direct(tmp_path: Path) -> None:
    req = rule_fallback([_parsed(fixture_mails()["no_attachment"], tmp_path)])
    assert req.intent == "covenant_check"
    assert req.source == "rules"
    assert req.plan_year == 2027
