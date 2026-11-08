"""Shared fixture for the contract-text tests."""

import pytest

from finn.contracts.pdf_text import ContractText, load_contract_text
from mailkit import MATERIAL


@pytest.fixture(scope="session")
def texts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ContractText]:
    """Page texts of the four contracts (own cache dir, read once per session)."""
    cache = tmp_path_factory.mktemp("cache")
    return {
        f"V{pdf.name.split('_')[1]}": load_contract_text(pdf, cache)
        for pdf in sorted(MATERIAL.glob("Vertrag_*.pdf"))
    }
