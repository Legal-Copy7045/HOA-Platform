"""Decision D32-payments (replacing the D21 fees): a surcharge on credit
cards only, at most 3%; bank and debit card payments carry no fee."""

import pytest

from community import services as community
from community.models import GoverningRule
from community.rules import validate_value
from core.policy import SYSTEM
from payments.services import (
    MAX_SURCHARGE_BP,
    disclosure,
    payment_options,
    surcharge,
    surcharge_bp,
)

pytestmark = pytest.mark.django_db


def _set(value):
    community.set_rule(
        key="payments.credit_card_surcharge_bp",
        value=value,
        citation="Board resolution (test)",
        actor=SYSTEM,
    )


def test_default_is_three_percent_on_credit_cards_only():
    assert surcharge_bp() == 300
    assert surcharge(10_000, "credit") == 300
    assert surcharge(10_000, "debit") == 0
    assert surcharge(10_000, "bank") == 0


def test_surcharge_rounds_up_and_options_show_each_total():
    _set("295")
    assert surcharge(4_501, "credit") == 133  # 132.78 rounds up
    options = {o["method"]: o for o in payment_options(4_500)}
    assert options["credit"]["total_cents"] == 4_500 + 133
    assert options["credit"]["label"] == "Credit card (+2.95% surcharge)"
    assert options["bank"]["fee_cents"] == options["debit"]["fee_cents"] == 0
    assert options["credit"]["citation"] == "Board resolution (test)"


def test_the_cap_holds_in_the_rule_and_in_code():
    with pytest.raises(ValueError, match="at most 3%"):
        validate_value("payments.credit_card_surcharge_bp", "301")
    GoverningRule.objects.update_or_create(
        key="payments.credit_card_surcharge_bp", defaults={"value": "900", "citation": "x"}
    )
    assert surcharge_bp() == MAX_SURCHARGE_BP
    assert surcharge(10_000, "credit") == 300


def test_disclosure_names_the_percentage_and_no_fee_methods():
    assert disclosure() == (
        "A 3% surcharge applies to credit card payments; no surcharge on debit cards or bank "
        "payments. The surcharge is not greater than our cost of accepting credit cards."
    )
    _set("0")
    assert surcharge(10_000, "credit") == 0
    assert "No surcharge" in disclosure()


def test_old_fee_rules_stay_in_the_history_but_do_nothing():
    GoverningRule.objects.create(key="payments.bank_fee_cents", value="100", citation="old")
    assert surcharge(10_000, "bank") == 0
    assert GoverningRule.objects.filter(key="payments.bank_fee_cents").exists()


def test_unknown_method_is_refused():
    with pytest.raises(ValueError):
        surcharge(100, "crypto")
