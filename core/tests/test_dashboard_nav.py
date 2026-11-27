"""Home dashboard and sidebar show only what the person can open."""

import pytest
from django.urls import reverse

from community.models import Role
from community.tests.factories import LotFactory, make_member
from compliance import services as compliance

pytestmark = pytest.mark.django_db


def nav_labels(response):
    return [i["label"] for s in response.context["nav"]["sections"] for i in s["items"]]


def test_owner_sidebar_and_dashboard_stay_in_scope(client):
    board = make_member(board=True)
    mine, theirs = LotFactory(), LotFactory()
    owner = make_member(lots=[mine])
    compliance.report(lot=theirs, category="trash", description="NEIGHBOR-SECRET", actor=board)
    v = compliance.report(lot=mine, category="trash", description="Mine", actor=board)
    compliance.VIOLATION.apply(v, "send_notice", actor=board)
    client.force_login(owner)
    response = client.get(reverse("home"))
    page = response.content.decode()
    assert "NEIGHBOR-SECRET" not in page
    assert "Notice: Trash and recycling" in page
    labels = nav_labels(response)
    assert "My account" in labels and "Invoices" not in labels and "People" not in labels


def test_board_dashboard_lists_invoice_waiting_for_me(client):
    from operations import services as ops
    from operations.models import ExpenseCategory, Vendor

    manager = make_member(roles=[Role.MANAGER])
    board = make_member(board=True)
    inv = ops.create_invoice(
        vendor=Vendor.objects.create(name="V"),
        number="1",
        invoice_date="2026-11-01",
        amount_cents=100,
        category=ExpenseCategory.objects.create(code="1", name="c"),
        actor=manager,
    )
    ops.INVOICE.apply(inv, "submit", actor=manager)
    client.force_login(board)
    assert "Needs your approval" in client.get(reverse("home")).content.decode()
    client.force_login(manager)  # entered it: never asked to approve
    assert "Needs your approval" not in client.get(reverse("home")).content.decode()


@pytest.mark.parametrize(
    ("roles", "present", "absent"),
    [
        (
            [Role.SYSADMIN],
            ["System status", "Invitations", "Branding"],
            ["Owner accounts", "People", "My home", "Settings"],
        ),
        ([Role.AUDITOR], ["Owner accounts", "Reports"], ["Approvals", "People"]),
        ([Role.MANAGER], ["Invoices", "Tasks and deadlines"], ["Approvals", "People"]),
    ],
)
def test_sidebar_by_role(client, roles, present, absent):
    client.force_login(make_member(roles=roles))
    labels = nav_labels(client.get(reverse("home")))
    for label in present:
        assert label in labels
    for label in absent:
        assert label not in labels


def test_current_page_is_marked(client):
    client.force_login(make_member(board=True))
    response = client.get(reverse("people"))
    current = [
        i["label"] for s in response.context["nav"]["sections"] for i in s["items"] if i["current"]
    ]
    assert current == ["People"]
