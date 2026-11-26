"""Pay shortcut (streamlining 2026-11-26): the dashboard's balance to-do has
"Pay $X" straight to the ways to pay with every open charge included, and
"Pay only some charges" is a link. Amounts are always worked out again on
the server; the address only picks the step."""

import pytest
from django.urls import reverse

from community.tests.factories import make_member
from core import my_items
from core.models import FeatureSetting
from core.scoping import system_access
from ledger import services as ledger
from payments.models import PendingCheckout

pytestmark = pytest.mark.django_db


def _pay_url(w, query=""):
    return reverse("pay_online", args=[w["lot"].pk]) + query


def test_pay_page_shows_the_methods_first_for_every_open_charge(client, w):
    client.force_login(w["owner"])
    page = client.get(_pay_url(w, "?all=1")).content.decode()
    assert "Choose how to pay $150.00" in page
    assert "$154.50" in page  # credit card total, 3% on $150.00
    assert 'data-testid="paying-all"' in page and "Pay only some charges" in page
    assert _pay_url(w, "?choose=1") in page
    # Each method form carries both charges for the checkout to re-check.
    assert page.count(f'name="charge" value="{w["c1"].pk}"') == 3
    assert page.count(f'name="charge" value="{w["c2"].pk}"') == 3
    # The plain address does the same.
    assert "Choose how to pay $150.00" in client.get(_pay_url(w)).content.decode()


def test_pay_only_some_charges_shows_the_charge_list(client, w):
    client.force_login(w["owner"])
    page = client.get(_pay_url(w, "?choose=1")).content.decode()
    assert "What would you like to pay?" in page and "Choose how to pay" not in page
    assert f'value="{w["c1"].pk}"' in page and 'name="amount"' in page
    # Choosing one charge still gives its own quote.
    page = client.post(_pay_url(w), {"charge": [w["c2"].pk]}).content.decode()
    assert "Choose how to pay $50.00" in page
    assert _pay_url(w, "?choose=1") in page and "Pay only some charges" not in page


def test_query_values_are_never_trusted(client, w):
    with system_access("t"):
        other_charge = ledger.LedgerEntry.objects.filter(lot=w["other_lot"]).first()
    client.force_login(w["owner"])
    page = client.get(
        _pay_url(w, f"?all=1&amount=1.00&charge={other_charge.pk}&base_cents=1")
    ).content.decode()
    assert "Choose how to pay $150.00" in page
    assert f'value="{other_charge.pk}"' not in page
    assert 'name="amount"' not in page  # charges, not a typed amount
    # Nothing is started by opening the page.
    with system_access("t"):
        assert not PendingCheckout.objects.exists()
    assert not w["fake"].posts()


def test_checkout_from_the_shortcut_is_bound_to_the_server_quote(client, w):
    client.force_login(w["owner"])
    response = client.post(
        reverse("pay_checkout", args=[w["lot"].pk]),
        {"method": "bank", "charge": [w["c1"].pk, w["c2"].pk]},
    )
    assert response.status_code == 302
    with system_access("t"):
        checkout = PendingCheckout.objects.get()
    assert checkout.base_cents == 15_000 and checkout.total_cents == 15_000
    assert sorted(checkout.charge_ids) == sorted([w["c1"].pk, w["c2"].pk])


def test_paid_up_account_falls_back_to_the_choice_form(client, w):
    for c in (w["c1"], w["c2"]):
        ledger.record_payment(
            lot=w["lot"],
            amount_cents=c.amount_cents,
            received_on=c.posted_on,
            method="check",
            reference=f"chk-{c.pk}",
            actor=w["board"],
        )
    client.force_login(w["owner"])
    page = client.get(_pay_url(w, "?all=1")).content.decode()
    assert "Choose how to pay" not in page and "Nothing is due on this account." in page


def test_other_people_cannot_use_the_shortcut(client, w):
    for user in (w["owner_b"], make_member(board=True)):
        client.force_login(user)
        assert client.get(_pay_url(w, "?all=1")).status_code in (403, 404)


def test_balance_todo_has_pay_x_straight_to_the_payment_page(client, w):
    [item] = [i for i in my_items.items_for(w["owner"]) if i.nav == "my_account"]
    assert item.action_label == "Pay $150.00"
    assert item.action_url == _pay_url(w, "?all=1")
    assert item.url == reverse("my_account")
    client.force_login(w["owner"])
    page = client.get(reverse("home")).content.decode()
    assert f'href="{_pay_url(w, "?all=1")}">Pay $150.00</a>' in page


def test_no_pay_shortcut_when_online_payments_are_off(w):
    FeatureSetting.objects.filter(key="online_payments").update(enabled=False)
    [item] = [i for i in my_items.items_for(w["owner"]) if i.nav == "my_account"]
    assert item.action_url == "" and item.action_label == ""
