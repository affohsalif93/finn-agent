"""Pseudonymisation before the intent call (D21)."""

import re

from finn.llm.redact import Pseudonyms, clean_body


def _restore(p: Pseudonyms, text: str) -> str:
    """Map placeholders back via the stored mapping (round-trip check)."""
    return re.sub(r"\[[A-Z]+_\d+\]", lambda m: p.mapping.get(m.group(0), m.group(0)), text)


BODY = """Hallo Finn,

anbei der Wirtschaftsplan 2027 (Bilanzsumme 313.200 T€, Stichtag 31.12.2027).
Bei Fragen: Max Mustermann, max.mustermann@stadtwerke-musterstadt.de, Tel. +49 421 123 4567
oder 0421/1234567. Erstattung bitte auf DE89 3704 0044 0532 0130 00.
Darlehen Nr. 4471 203 118, FD-2022-08817.

Mit freundlichen Grüßen
Dr. Max Mustermann
Kaufmännischer Geschäftsführer
Stadtwerke Musterstadt GmbH · Tel. +49 421 123 4567

Am Mo., 2. Nov. 2026 um 09:15 Uhr schrieb Finn <finn@example.com>:
> alte Nachricht mit Zahlen 3,37
"""


def test_clean_body_strips_quote_and_signature() -> None:
    text = clean_body(BODY)
    assert "alte Nachricht" not in text
    assert "Kaufmännischer Geschäftsführer" not in text
    assert "anbei der Wirtschaftsplan 2027" in text


def test_outlook_header_block_is_quote() -> None:
    body = "Bitte prüfen.\n\nVon: Finn <finn@example.com>\nGesendet: Montag\nAn: x\n\nalt"
    assert clean_body(body) == "Bitte prüfen."


def test_forward_keeps_forwarded_text() -> None:
    body = (
        "Bitte prüfen.\n\n---------- Forwarded message ---------\nVon: A <a@b.de>\nDatum: x\n\nPlan"
    )
    assert "Plan" in clean_body(body, forwarded=True)


def test_mobile_footer_and_delimiter() -> None:
    assert clean_body("Plan anbei\n\nVon meinem iPhone gesendet") == "Plan anbei"
    assert clean_body("Plan anbei\n-- \nMax") == "Plan anbei"


def test_pseudonymise_and_restore() -> None:
    p = Pseudonyms()
    text = clean_body(BODY)
    out = p.pseudonymise(text, names=["Dr. Max Mustermann"])
    assert "max.mustermann@" not in out
    assert "Mustermann" not in out
    assert "Max" not in out
    assert "+49 421" not in out
    assert "0421/1234567" not in out
    assert "DE89" not in out
    assert "[EMAIL_1]" in out
    assert "[IBAN_1]" in out
    assert "[PHONE_1]" in out
    assert "[PERSON_1]" in out
    # business content stays: numbers, dates, loan numbers, company
    for keep in ("313.200", "31.12.2027", "2027", "4471 203 118", "FD-2022-08817"):
        assert keep in out
    assert _restore(p, out) == text


def test_same_original_same_placeholder() -> None:
    p = Pseudonyms()
    a = p.pseudonymise("a@b.de und a@b.de und c@d.de")
    assert a == "[EMAIL_1] und [EMAIL_1] und [EMAIL_2]"
    again = Pseudonyms(p.mapping).pseudonymise("c@d.de")  # mapping reloaded from the run folder
    assert again == "[EMAIL_2]"


def test_name_variants() -> None:
    p = Pseudonyms()
    out = p.pseudonymise("Herr Mustermann und Max Mustermann", names=["Mustermann, Max"])
    assert "Mustermann" not in out
    assert _restore(p, out) == "Herr Mustermann und Max Mustermann"
