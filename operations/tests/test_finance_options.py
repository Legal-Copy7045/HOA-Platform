"""Capital projects (D83) and insurance claims (D85): approvals and their
thresholds, change orders, spent from linked paid invoices, the owners'
summary, claim states and settlement approvals, the feature switches (404,
menu, My items), cross-owner access, append-only records and audit."""

from __future__ import annotations

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import features
from core.append_only import AppendOnlyError
from core.my_items import items_for
from core.policy import SYSTEM
from operations import capital_projects as cp
from operations import insurance_claims as ic
from operations.models import ExpenseCategory, Invoice, InvoicePayment, Vendor
from operations.models_options import CapitalProjectApproval, ChangeOrder

pytestmark = pytest.mark.django_db


def _on(*keys):
    for key in keys:
        features.set_feature(key, True, actor=SYSTEM)


@pytest.fixture
def w():
    lot_a = LotFactory(street_address="1 Alpha Way")
    lot_b = LotFactory(street_address="2 Bravo Way")
    for key in ("capital.second_approval_cents", "insurance.second_approval_cents"):
        community.set_rule(key=key, value="500000", citation="Resolution (test)", actor=SYSTEM)
    vendor = Vendor.objects.create(name="Pond Pros (test)")
    category = ExpenseCategory.objects.create(code="5100", name="Pond")
    return {
        "owner_a": make_member(lots=[lot_a]),
        "owner_b": make_member(lots=[lot_b]),
        "board": make_member(board=True),
        "board2": make_member(board=True),
        "board3": make_member(board=True),
        "manager": make_member(roles=[Role.MANAGER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "vendor": vendor,
        "category": category,
    }


def _project(w, budget=100000, **kw):
    return cp.create(
        title=kw.pop("title", "Entrance sign"),
        budget_cents=budget,
        funding=kw.pop("funding", "reserve"),
        actor=kw.pop("actor", w["manager"]),
        **kw,
    )


def _paid_invoice(w, number, cents, *, paid=True):
    invoice = Invoice.objects.create(
        vendor=w["vendor"],
        number=number,
        invoice_date=timezone.localdate(),
        amount_cents=cents,
        category=w["category"],
        state="paid" if paid else "approved",
        entered_by=w["manager"],
    )
    if paid:
        InvoicePayment.objects.create(
            invoice=invoice,
            paid_on=timezone.localdate(),
            method="check",
            amount_cents=cents,
            recorded_by=w["manager"],
        )
    return invoice


# --- Capital projects (D83) ----------------------------------------------------------------


def test_manager_records_a_project_the_board_approves_with_a_resolution(w):
    project = _project(w)
    with pytest.raises(cp.ProjectError):
        cp.approve(project, actor=w["board"])  # not sent for approval yet
    with pytest.raises(cp.ProjectError):
        cp.ask_approval(project, resolution_ref=" ", actor=w["manager"])
    cp.ask_approval(project, resolution_ref="Resolution 2027-03", actor=w["manager"])
    with pytest.raises(PermissionDenied):
        cp.approve(project, actor=w["manager"])
    project = cp.approve(project, actor=w["board"])
    assert project.state == "approved" and project.required_approvals == 1
    with pytest.raises(cp.ProjectError):
        cp.update(project, budget_cents=200000, actor=w["manager"])  # change orders only


def test_large_projects_need_two_different_board_members(w):
    project = _project(w, budget=600000)
    cp.ask_approval(project, resolution_ref="Resolution 2027-04", actor=w["board"])
    project = cp.approve(project, actor=w["board"])
    assert project.state == "planned"
    with pytest.raises(cp.ProjectError):
        cp.approve(project, actor=w["board"])
    titles = [i.title for i in items_for(w["board2"])]
    assert "Approve capital project: Entrance sign" not in titles  # feature off
    _on("capital_projects")
    assert "Approve capital project: Entrance sign" in [i.title for i in items_for(w["board2"])]
    assert "Approve capital project: Entrance sign" not in [i.title for i in items_for(w["board"])]
    project = cp.approve(project, actor=w["board2"])
    assert project.state == "approved"
    assert CapitalProjectApproval.objects.filter(project=project).count() == 2
    event = AuditEvent.objects.filter(action="capital.approved").order_by("pk").last()
    assert set(event.metadata) == {"project_id", "approvals", "required"}


def test_change_orders_thresholds_and_budget_figures(w):
    project = _project(w, budget=100000)
    cp.ask_approval(project, resolution_ref="R-1", actor=w["manager"])
    cp.approve(project, actor=w["board"])
    cp.change_state(project, "start", actor=w["manager"])
    small = cp.propose_change(project, amount_cents=20000, reason="More stone", actor=w["board"])
    assert small.required_approvals == 1
    with pytest.raises(cp.ProjectError):
        cp.decide_change(small, decision="approve", actor=w["board"])  # own proposal
    with pytest.raises(PermissionDenied):
        cp.decide_change(small, decision="approve", actor=w["manager"])
    assert cp.decide_change(small, decision="approve", actor=w["board2"]) == "approved"
    big = cp.propose_change(project, amount_cents=600000, reason="Redesign", actor=w["manager"])
    assert big.required_approvals == 2
    assert cp.decide_change(big, decision="approve", actor=w["board"]) == "pending"
    with pytest.raises(cp.ProjectError):
        cp.decide_change(big, decision="decline", actor=w["board2"])  # needs a reason
    assert cp.decide_change(big, decision="decline", note="Too much", actor=w["board2"]) == (
        "declined"
    )
    cut = cp.propose_change(project, amount_cents=-5000, reason="Smaller sign", actor=w["manager"])
    cp.decide_change(cut, decision="approve", actor=w["board3"])
    f = cp.figures(project)
    assert f["budget"] == 100000 + 20000 - 5000
    with pytest.raises(cp.ProjectError):
        cp.propose_change(project, amount_cents=-1000000, reason="x", actor=w["manager"])
    with pytest.raises(AppendOnlyError):
        ChangeOrder.objects.filter(pk=small.pk).update(amount_cents=1)


def test_spent_is_payments_on_linked_invoices_and_committed_is_typed(w):
    project = _project(w, budget=100000)
    paid = _paid_invoice(w, "PP-1", 30000)
    unpaid = _paid_invoice(w, "PP-2", 50000, paid=False)
    cp.link(project, kind="invoice", object_id=paid.pk, actor=w["manager"])
    cp.link(project, kind="invoice", object_id=unpaid.pk, actor=w["manager"])
    cp.link(
        project, kind="vendor", object_id=w["vendor"].pk, committed_cents=90000, actor=w["manager"]
    )
    with pytest.raises(cp.ProjectError):
        cp.link(project, kind="invoice", object_id=paid.pk, actor=w["manager"])
    other = _project(w, title="Other")
    with pytest.raises(cp.ProjectError):
        cp.link(other, kind="invoice", object_id=paid.pk, actor=w["manager"])
    f = cp.figures(project)
    assert f["spent"] == 30000 and f["committed"] == 90000 and f["spent_percent"] == 30
    link = next(lk for lk in cp.active_links(project) if lk.invoice_id == paid.pk)
    cp.unlink(link, actor=w["manager"])
    assert cp.figures(project)["spent"] == 0
    with pytest.raises(cp.ProjectError):
        cp.unlink(link, actor=w["manager"])
    assert cp.spend_by_project()[0]["fund"] in ("reserve",)


def test_cancel_is_the_boards_with_a_note(w):
    project = _project(w)
    with pytest.raises(PermissionDenied):
        cp.change_state(project, "cancel", actor=w["manager"], note="x")
    with pytest.raises(cp.ProjectError):
        cp.change_state(project, "cancel", actor=w["board"])
    project = cp.change_state(project, "cancel", actor=w["board"], note="Not needed")
    assert project.state == "cancelled"
    with pytest.raises(cp.ProjectError):
        cp.change_state(project, "start", actor=w["manager"])


def test_owners_see_only_shared_projects_and_four_facts(client, w):
    shown = _project(w, title="Pond fence (shared)")
    _project(w, title="Board only: litigation reserve")
    cp.update(shown, owner_visible=True, actor=w["manager"])
    cp.link(
        shown, kind="vendor", object_id=w["vendor"].pk, committed_cents=1234500, actor=w["manager"]
    )
    client.force_login(w["owner_a"])
    assert client.get(reverse("community_projects")).status_code == 404  # feature off
    _on("capital_projects")
    page = client.get(reverse("community_projects")).content.decode()
    assert "Pond fence (shared)" in page and "$1,000.00" in page
    assert "Board only: litigation" not in page
    assert "Pond Pros" not in page and "12,345.00" not in page
    for name in ("capital_projects",):
        assert client.get(reverse(name)).status_code == 403
    for name in ("capital_project_detail",):
        assert client.get(reverse(name, args=[shown.pk])).status_code == 403
    assert client.get(reverse("capital_project_file", args=[shown.pk, 1])).status_code == 403


def test_capital_pages_for_board_manager_auditor(client, w):
    _on("capital_projects")
    client.force_login(w["manager"])
    response = client.post(
        reverse("capital_projects"),
        {"title": "New sign", "budget_cents": "2500.00", "funding": "operating"},
    )
    assert response.status_code == 302
    project = cp.CapitalProject.objects.get(title="New sign")
    assert project.budget_cents == 250000
    assert client.get(reverse("capital_project_detail", args=[project.pk])).status_code == 200
    client.force_login(w["auditor"])
    assert client.get(reverse("capital_projects")).status_code == 200
    assert client.post(reverse("capital_projects"), {"title": "x"}).status_code == 403
    client.force_login(w["board"])
    home = client.get(reverse("home")).content.decode()
    assert reverse("capital_projects") in home and reverse("insurance_claims") not in home


# --- Insurance claims (D85) --------------------------------------------------------------


def _claim(w, **kw):
    values = {
        "carrier": "Fictional Mutual",
        "policy_number": "GL-1",
        "incident_on": timezone.localdate() - datetime.timedelta(days=5),
        "description": "Storm damage to the sign",
    }
    values.update(kw)
    return ic.create(actor=kw.pop("actor", w["manager"]), **values)


def test_claim_states_and_who_takes_each_step(w):
    claim = _claim(w)
    with pytest.raises(ic.ClaimError):
        _claim(w, incident_on=timezone.localdate() + datetime.timedelta(days=1))
    claim = ic.change_state(claim, "review", actor=w["manager"])
    assert claim.state == "under_review"
    with pytest.raises(ic.ClaimError):
        ic.change_state(claim, "deny", actor=w["manager"])  # note required
    claim = ic.change_state(claim, "deny", actor=w["manager"], note="Wear and tear")
    with pytest.raises(PermissionDenied):
        ic.change_state(claim, "reopen", actor=w["manager"], note="Appeal")
    claim = ic.change_state(claim, "reopen", actor=w["board"], note="Appeal with photos")
    assert claim.state == "under_review"
    with pytest.raises(PermissionDenied):
        ic.create(
            actor=w["owner_a"], carrier="x", incident_on=timezone.localdate(), description="x"
        )


def test_settlement_needs_board_approval_two_above_the_threshold(w):
    claim = _claim(w)
    small = ic.record_offer(claim, amount_cents=120000, actor=w["manager"])
    assert small.required_approvals == 1
    with pytest.raises(PermissionDenied):
        ic.approve_offer(small, actor=w["manager"])
    big = ic.record_offer(claim, amount_cents=900000, actor=w["manager"])
    with pytest.raises(ic.ClaimError):
        ic.approve_offer(small, actor=w["board"])  # a newer offer exists
    assert ic.approve_offer(big, actor=w["board"]) == "pending"
    with pytest.raises(ic.ClaimError):
        ic.approve_offer(big, actor=w["board"])
    claim.refresh_from_db()
    assert claim.state == "reported"
    assert ic.approve_offer(big, actor=w["board2"]) == "accepted"
    claim.refresh_from_db()
    assert claim.state == "approved"
    assert ic.money_totals(claim) == {"expected": 900000, "received": 0}
    ic.record_money(
        claim, kind="received", amount_cents=900000, on=timezone.localdate(), actor=w["manager"]
    )
    claim.refresh_from_db()
    assert claim.state == "paid"
    with pytest.raises(PermissionDenied):
        ic.change_state(claim, "close", actor=w["manager"], note="Done")
    claim = ic.change_state(claim, "close", actor=w["board"], note="Repaired and paid")
    with pytest.raises(ic.ClaimError):
        ic.record_money(
            claim, kind="received", amount_cents=1, on=timezone.localdate(), actor=w["manager"]
        )
    event = AuditEvent.objects.filter(action="insurance.money_recorded").first()
    assert set(event.metadata) == {"claim_id", "kind", "amount_cents"}


def test_claim_deadlines_and_settlements_reach_my_items_only_while_on(w):
    claim = _claim(w, proof_of_loss_due=timezone.localdate() + datetime.timedelta(days=7))
    ic.record_offer(claim, amount_cents=100000, actor=w["manager"])
    assert not [i for i in items_for(w["manager"]) if "Proof of loss" in i.title]
    _on("insurance_claims")
    assert [i for i in items_for(w["manager"]) if "Proof of loss" in i.title]
    assert [i for i in items_for(w["board"]) if "Accept insurance settlement" in i.title]
    assert not [i for i in items_for(w["manager"]) if "Accept insurance" in i.title]
    assert not [i for i in items_for(w["owner_a"]) if "claim" in i.title.lower()]


def test_claim_pages_are_staff_only(client, w):
    claim = _claim(w)
    client.force_login(w["board"])
    assert client.get(reverse("insurance_claims")).status_code == 404
    _on("insurance_claims")
    assert client.get(reverse("insurance_claims")).status_code == 200
    assert client.get(reverse("insurance_claim_detail", args=[claim.pk])).status_code == 200
    client.force_login(w["manager"])
    response = client.post(
        reverse("insurance_claim_detail", args=[claim.pk]), {"do": "offer", "amount": "250.00"}
    )
    assert response.status_code == 302 and claim.offers.count() == 1
    for user in (w["owner_a"], w["owner_b"]):
        client.force_login(user)
        assert client.get(reverse("insurance_claims")).status_code == 403
        assert client.get(reverse("insurance_claim_detail", args=[claim.pk])).status_code == 403
        url = reverse("insurance_claim_file", args=[claim.pk, 1])
        assert client.get(url).status_code == 403
        home = client.get(reverse("home")).content.decode()
        assert reverse("insurance_claims") not in home


def test_every_finance_option_is_off_by_default_and_on_the_features_list():
    keys = {
        "overdue_interest",
        "cashflow_forecast",
        "collections_legal",
        "capital_projects",
        "owner_finance_dashboard",
        "insurance_claims",
    }
    assert keys <= set(features.BY_KEY)
    assert not any(features.BY_KEY[k].default for k in keys)
    assert all(features.BY_KEY[k].description for k in keys)
