# ruff: noqa: F811  (the scenario fixtures are imported, then used as arguments)
"""The statement explains moved entries in plain words (D43), using the
backdated-sale scenario of the fixmoney tests: the buyer's statement shows
the moved lines once, with a note saying where they came from, and nothing
of the seller's own."""

import pytest

from ledger import letters
from ledger.tests.test_fixmoney import (  # noqa: F401  (fixtures)
    DUES,
    _accounts,
    _charge,
    _check,
    _pay,
    _sell,
    ago,
    books,
    w,
)

pytestmark = pytest.mark.django_db


def test_moved_entries_carry_a_plain_note(w, books):
    _charge(w, 3000, ago(40), "Seller fine")
    _charge(w, DUES, ago(15), "December dues")
    _pay(w, DUES, ago(10), "5005")
    _sell(w, ago(20))
    seller_account, _buyer_account = _accounts(w["lot"])
    paras, balance = letters.statement_paragraphs(w["lot"])
    text = "\n".join(t for _s, t in paras)
    assert balance == 0
    assert "Seller fine" not in text
    assert text.count("December dues") == 1 and text.count("#5005") == 1
    assert f"Note: this line was moved from account {seller_account.number} on" in text
    _check(books)
