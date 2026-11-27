"""Own matters (D65): segregation of duty for a board member's own home.

Board members' requests are homeowners' requests: every board member may
read them, their own included. What a board member may not do is advance
their own matter with board or staff powers; another board member does. They
still act on it as the homeowner (reply, withdraw, respond, pay, cancel)."""

import datetime
import html

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from arc import services as arc
from arc.models import ArcState
from audit.models import AuditEvent
from comms import threads
from community import reservations
from community import services as community
from community.models import Account, CommonArea, ContactKind, Role
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from compliance import services as compliance
from core import conflicts
from core.models import FeatureSetting
from core.my_items import items_for
from core.policy import SYSTEM
from core.scoping import system_access
from core.workflow import WorkflowError
from governance import services as governance
from ledger import accounting
from ledger import services as ledger
from operations import services as ops

pytestmark = pytest.mark.django_db

BADGE = "data-board-badge"
NOTE = 'data-testid="own-home-note"'


@pytest.fixture
def w():
    lot, other = LotFactory(), LotFactory()
    bm = make_member(board=True, lots=[lot])  # a board member who owns `lot`
    co_owner = make_member()
    community.record_ownership(
        person=co_owner.person, lot=lot, start_date=days_ago(300), actor=SYSTEM
    )
    return {
        "lot": lot,
        "other": other,
        "bm": bm,
        "co_owner": co_owner,
        "board2": make_member(board=True, lots=[LotFactory()]),
        "manager": make_member(roles=[Role.MANAGER]),
        "owner_b": make_member(lots=[other]),
    }


def _page(client, url) -> str:
    return html.unescape(client.get(url).content.decode())


def _refusals(area=None):
    events = AuditEvent.objects.filter(action=conflicts.AUDIT_ACTION)
    return [e for e in events if area is None or e.metadata.get("area") == area]


def _request(w, who="co_owner", lot="lot"):
    return ops.submit_request(
        lot=w[lot], request_type="maintenance", subject="Gutter", description="Loose", actor=w[who]
    )


# --- 1. The rule ------------------------------------------------------------------------------


def test_own_matter_covers_owner_contact_requester_and_former_era(w):
    req = _request(w)
    assert conflicts.own_matter(w["bm"], req)  # co-owner raised it: still bm's home
    assert conflicts.own_matter(w["bm"], w["lot"])
    assert not conflicts.own_matter(w["board2"], req)
    assert not conflicts.own_matter(w["bm"], _request(w, "owner_b", "other"))
    assert not conflicts.own_matter(SYSTEM, req)
    # An authorized contact of the home (not on the deed).
    contact = make_member(board=True)
    with system_access("t"):
        account = Account.objects.active().get(lot=w["lot"])
    community.add_account_contact(
        account, person=contact.person, kind=ContactKind.AUTHORIZED, actor=SYSTEM
    )
    assert conflicts.own_matter(contact, req)
    # A records request is the requester's own (a person, not a home).
    records = governance.submit_records_request(description="Minutes", actor=w["owner_b"])
    assert conflicts.own_matter(w["owner_b"], records)
    assert not conflicts.own_matter(w["bm"], records)


def test_former_owner_era_counts(w):
    seller = make_member(board=True)
    lot = LotFactory()
    community.record_ownership(
        person=seller.person, lot=lot, start_date=days_ago(400), actor=SYSTEM
    )
    v = compliance.report(lot=lot, category="trash", description="Cans", actor=w["board2"])
    from compliance.models import Violation

    Violation.objects.unscoped("t").filter(pk=v.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=10)
    )
    v.refresh_from_db()
    buyer = make_member()
    community.transfer_lot(lot, new_owners=[buyer.person], transfer_date=days_ago(1), actor=SYSTEM)
    assert conflicts.own_matter(seller, v)  # it arose while they owned the home
    later = compliance.report(lot=lot, category="trash", description="Again", actor=w["board2"])
    assert not conflicts.own_matter(seller, later)


def test_staff_permission_is_the_staff_only_side():
    assert conflicts.staff_permission("request.manage")
    assert conflicts.staff_permission("violation.decide")
    assert not conflicts.staff_permission("request.submit")
    assert not conflicts.staff_permission("arc.submit")
    assert not conflicts.staff_permission("resale.request")  # owners ask too


# --- 2. Owner requests ------------------------------------------------------------------------


def test_owner_request_staff_steps_refused_on_own_home(w):
    req = _request(w)
    for action in ("start", "ask_owner", "resolve"):
        with pytest.raises(WorkflowError, match="your own home's request"):
            ops.OWNER_REQUEST.apply(req, action, actor=w["bm"], note="x")
    with pytest.raises(ops.OperationsError, match="another board member"):
        ops.assign_request(req, w["bm"].person, actor=w["bm"])
    with pytest.raises(ops.OperationsError, match="another board member"):
        ops.request_to_work_order(req, actor=w["bm"])
    assert not ops.can_add_request_photos(w["bm"], req)  # not the requester: no owner path
    req.refresh_from_db()
    assert req.state == "submitted" and req.assignee_id is None
    refusals = _refusals("request")
    assert {e.metadata["action"] for e in refusals} >= {"start", "assign", "work_order"}
    assert all(e.metadata["object_id"] == str(req.pk) for e in refusals)
    assert all(e.outcome == "denied" and e.actor_id == w["bm"].pk for e in refusals)
    # Another board member acts; the manager too.
    ops.assign_request(req, w["board2"].person, actor=w["board2"])
    ops.OWNER_REQUEST.apply(req, "start", actor=w["manager"])
    req.refresh_from_db()
    assert req.state == "in_progress"
    ops.OWNER_REQUEST.apply(req, "resolve", actor=w["board2"], note="Fixed")
    with pytest.raises(WorkflowError, match="your own home's request"):
        ops.OWNER_REQUEST.apply(req, "close", actor=w["bm"])


def test_board_member_still_acts_as_homeowner_on_own_request(w):
    req = _request(w, "bm")
    assert ops.can_add_request_photos(w["bm"], req)
    threads.post("request", req, body="More detail", actor=w["bm"])
    with pytest.raises(threads.ThreadError):
        threads.post("request", req, body="Note", internal=True, actor=w["bm"])
    ops.OWNER_REQUEST.apply(req, "withdraw", actor=w["bm"])
    req.refresh_from_db()
    assert req.state == "withdrawn"


def test_own_request_page_is_the_homeowner_view(client, w):
    req = _request(w)
    client.force_login(w["bm"])
    page = _page(client, reverse("request_detail", args=[req.pk]))
    assert BADGE not in page and "board-page" not in page
    assert NOTE in page and "This is your own home's request" in page
    assert reverse("request_assign", args=[req.pk]) not in page
    assert reverse("request_work_order", args=[req.pk]) not in page
    assert reverse("request_action", args=[req.pk, "start"]) not in page
    assert "disabled" in page and "Working on it" in page  # shown, not offered
    # A crafted post is refused, and audited.
    client.post(reverse("request_action", args=[req.pk, "start"]), {"note": ""})
    req.refresh_from_db()
    assert req.state == "submitted"
    # Another home's request: the board's page, with the badge.
    theirs = _request(w, "owner_b", "other")
    page = _page(client, reverse("request_detail", args=[theirs.pk]))
    assert BADGE in page and NOTE not in page
    assert reverse("request_action", args=[theirs.pk, "start"]) in page
    # The other board member sees the board's page on bm's home.
    client.force_login(w["board2"])
    page = _page(client, reverse("request_detail", args=[req.pk]))
    assert BADGE in page and NOTE not in page


def test_board_members_see_every_owners_requests_including_board_members(client, w):
    mine = _request(w)
    lot2 = LotFactory()
    community.record_ownership(
        person=w["board2"].person, lot=lot2, start_date=days_ago(30), actor=SYSTEM
    )
    board2s = ops.submit_request(
        lot=lot2, request_type="maintenance", subject="Fence", description="Down", actor=w["board2"]
    )
    client.force_login(w["bm"])
    page = _page(client, reverse("requests"))
    assert mine.subject in page
    assert client.get(reverse("request_detail", args=[board2s.pk])).status_code == 200


# --- 3. Internal notes are hidden on one's own matter ------------------------------------------


def test_internal_notes_hidden_from_the_board_member_on_own_matter(client, w):
    req = _request(w)
    threads.post("request", req, body="Owner seems upset", internal=True, actor=w["board2"])
    threads.post("request", req, body="We're on it", actor=w["board2"])
    bodies = [m.body for m in threads.messages_for("request", req, w["bm"])]
    assert bodies == ["We're on it"]
    assert not threads.access("request", req, w["bm"]).read_internal
    assert "Owner seems upset" in [
        m.body for m in threads.messages_for("request", req, w["manager"])
    ]
    client.force_login(w["bm"])
    page = _page(client, reverse("request_detail", args=[req.pk]))
    assert "Owner seems upset" not in page and "We're on it" in page
    v = compliance.report(lot=w["lot"], category="trash", description="Cans", actor=w["board2"])
    threads.post("violation", v, body="Discuss at hearing", internal=True, actor=w["board2"])
    assert threads.messages_for("violation", v, w["bm"]) == []


# --- 4. ARC -----------------------------------------------------------------------------------


def test_arc_staff_steps_refused_for_an_account_contact_of_the_home(w):
    """The ARC recusal rule now also covers contacts and the home's era."""
    contact = make_member(board=True, roles=[Role.ARC_REVIEWER])
    with system_access("t"):
        account = Account.objects.active().get(lot=w["lot"])
    community.add_account_contact(
        account, person=contact.person, kind=ContactKind.AUTHORIZED, actor=SYSTEM
    )
    req = arc.create_request(lot=w["lot"], title="Shed", description="8x10", actor=w["co_owner"])
    arc.ARC.apply(req, "submit", actor=w["co_owner"])
    assert arc.is_recused(contact, req)
    with pytest.raises(WorkflowError, match="recused"):
        arc.ARC.apply(req, "accept", actor=contact)
    with pytest.raises(WorkflowError, match="recused"):
        arc.ARC.apply(req, "accept", actor=w["bm"])
    with pytest.raises(arc.ArcError, match="recused"):
        arc.set_affected_lots(req, [], actor=w["bm"])
    req = arc.ARC.apply(req, "accept", actor=w["board2"])
    with pytest.raises(WorkflowError, match="recused"):
        arc.decide(req, outcome=ArcState.APPROVED, citation="x", actor=contact)
    with pytest.raises(arc.ArcError, match="recused"):
        arc.record_position(req, position="approve", actor=contact)
    assert {e.metadata["action"] for e in _refusals("arc")} >= {"approve", "position"}
    assert arc.deliberation(req, contact) is None


def test_own_arc_page_has_no_badge_and_the_note(client, w):
    req = arc.create_request(lot=w["lot"], title="Shed", description="8x10", actor=w["co_owner"])
    arc.ARC.apply(req, "submit", actor=w["co_owner"])
    client.force_login(w["bm"])
    page = _page(client, reverse("arc_detail", args=[req.pk]))
    assert BADGE not in page
    assert reverse("arc_action", args=[req.pk, "accept"]) not in page
    client.force_login(w["board2"])
    page = _page(client, reverse("arc_detail", args=[req.pk]))
    assert BADGE in page and reverse("arc_action", args=[req.pk, "accept"]) in page


# --- 5. Violations ----------------------------------------------------------------------------


def test_violation_steps_refused_on_own_home_owner_can_respond(client, w):
    v = compliance.report(lot=w["lot"], category="trash", description="Cans", actor=w["board2"])
    with pytest.raises(WorkflowError, match="your own home's violation"):
        compliance.VIOLATION.apply(v, "send_notice", actor=w["bm"])
    with pytest.raises(WorkflowError, match="your own home's violation"):
        compliance.VIOLATION.apply(v, "dismiss", actor=w["bm"], note="Nothing to see")
    with pytest.raises(compliance.ComplianceError, match="another board member"):
        compliance.set_follow_up(v, timezone.localdate(), actor=w["bm"])
    compliance.VIOLATION.apply(v, "send_notice", actor=w["board2"])
    v.refresh_from_db()
    compliance.respond(v, text="Moved the cans", actor=w["bm"])  # as the owner
    v.refresh_from_db()
    assert v.state == "responded"
    for action, note in (("schedule_hearing", "Mon 7pm"), ("uphold", "x"), ("mark_cured", "")):
        with pytest.raises(WorkflowError):
            compliance.VIOLATION.apply(v, action, actor=w["bm"], note=note)
    client.force_login(w["bm"])
    page = _page(client, reverse("violation_detail", args=[v.pk]))
    assert BADGE not in page and NOTE in page
    assert reverse("violation_action", args=[v.pk, "uphold"]) not in page
    assert reverse("follow_up_set", args=["violation", v.pk]) not in page
    client.force_login(w["board2"])
    page = _page(client, reverse("violation_detail", args=[v.pk]))
    assert BADGE in page and reverse("violation_action", args=[v.pk, "uphold"]) in page


# --- 6. Records requests ----------------------------------------------------------------------


def test_records_request_one_filed(client, w):
    obj = governance.submit_records_request(description="Minutes 2025", actor=w["bm"])
    with pytest.raises(WorkflowError, match="your own records request"):
        governance.RECORDS.apply(obj, "acknowledge", actor=w["bm"])
    client.force_login(w["bm"])
    page = _page(client, reverse("records_request_detail", args=[obj.pk]))
    assert BADGE not in page and NOTE in page
    governance.RECORDS.apply(obj, "acknowledge", actor=w["board2"])
    governance.RECORDS.apply(obj, "withdraw", actor=w["bm"])  # the requester's own step


# --- 7. Resale certificate --------------------------------------------------------------------


def test_resale_certificate_for_own_home(client, w):
    obj = ledger.request_resale(lot=w["lot"], requester_name="Seller", actor=w["bm"])
    for action in ("start", "issue"):
        with pytest.raises(WorkflowError, match="your own home's resale certificate"):
            ledger.RESALE.apply(obj, action, actor=w["bm"])
    with pytest.raises(ledger.LedgerError, match="another board member"):
        ledger.save_disclosures(obj, "None", actor=w["bm"])
    client.force_login(w["bm"])
    page = _page(client, reverse("resale_detail", args=[obj.pk]))
    assert NOTE in page and 'name="other_disclosures"' not in page
    ledger.RESALE.apply(obj, "start", actor=w["manager"])


# --- 8. Reservations --------------------------------------------------------------------------


def _at(days, hour):
    day = timezone.localdate() + datetime.timedelta(days=days)
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour)))


def test_own_booking_is_another_board_members_to_confirm(client, w):
    FeatureSetting.objects.create(key="reservations", enabled=True)
    area = CommonArea.objects.create(name="Pavilion", kind="amenity")
    reservations.save_area_settings(
        area, bookable=True, needs_approval=True, citation="Rules §4", actor=SYSTEM
    )
    res = reservations.request_reservation(
        area=area, lot_id=w["lot"].pk, starts_at=_at(3, 10), ends_at=_at(3, 12), actor=w["bm"]
    )
    with pytest.raises(reservations.ReservationError, match="your own home's booking"):
        reservations.approve(res, actor=w["bm"])
    with pytest.raises(reservations.ReservationError, match="your own home's booking"):
        reservations.decline(res, actor=w["bm"])
    assert not [i for i in items_for(w["bm"]) if i.area == "Common area bookings"]
    client.force_login(w["bm"])
    page = _page(client, reverse("reservation_detail", args=[res.pk]))
    assert NOTE in page and "Confirm booking" not in page and "Cancel booking" in page
    reservations.approve(res, actor=w["board2"])
    reservations.cancel(res, actor=w["bm"])  # as the owner


# --- 9. Collections ---------------------------------------------------------------------------


def _charge(w, lot="lot"):
    return ledger.post_charge(
        lot=w[lot], kind="other_charge", amount_cents=5000, description="Gate", actor=w["board2"]
    )


def test_collection_case_on_own_account(client, w):
    _charge(w)
    with pytest.raises(ledger.LedgerError, match="your own home's account"):
        ledger.open_case(lot=w["lot"], actor=w["bm"])
    case = ledger.open_case(lot=w["lot"], actor=w["board2"])
    for action in ("send_reminder", "payment_plan", "close"):
        with pytest.raises(WorkflowError, match="collection case"):
            ledger.COLLECTION.apply(case, action, actor=w["bm"], note="x")
    with pytest.raises(ledger.LedgerError):
        ledger.set_case_follow_up(case, timezone.localdate(), actor=w["bm"])
    with pytest.raises(accounting.AccountingError):
        accounting.generate_case_letter(case, actor=w["bm"])
    with pytest.raises(PermissionDenied):
        ledger.generate_notice(w["lot"], "first", actor=w["bm"])
    client.force_login(w["bm"])
    page = _page(client, reverse("collection_detail", args=[case.pk]))
    assert BADGE not in page and "your own home's collection case" in page
    assert reverse("collection_letter", args=[case.pk]) not in page
    assert reverse("collection_notice_pdf", args=[case.pk, "first"]) not in page
    ledger.COLLECTION.apply(case, "send_reminder", actor=w["board2"], note="First reminder")


# --- 10. Ledger entries -----------------------------------------------------------------------


def test_ledger_entries_on_own_account_refused(client, w):
    charge = _charge(w)
    for call in (
        lambda: ledger.post_charge(
            lot=w["lot"], kind="other_charge", amount_cents=100, description="x", actor=w["bm"]
        ),
        lambda: ledger.post_credit(lot=w["lot"], amount_cents=100, description="x", actor=w["bm"]),
        lambda: ledger.record_payment(
            lot=w["lot"], amount_cents=100, received_on=days_ago(1), actor=w["bm"]
        ),
        lambda: ledger.reverse_entry(charge, reason="waive it", actor=w["bm"]),
    ):
        with pytest.raises(ledger.LedgerError, match="your own home's account"):
            call()
    assert {e.metadata["action"] for e in _refusals("ledger")} == {
        "post_charge",
        "post_credit",
        "record_payment",
        "reverse",
    }
    # Another home: fine. Another board member on bm's home: fine.
    ledger.post_credit(lot=w["other"], amount_cents=100, description="x", actor=w["bm"])
    ledger.reverse_entry(charge, reason="posted twice", actor=w["board2"])
    client.force_login(w["bm"])
    page = _page(client, reverse("lot_ledger", args=[w["lot"].pk]))
    assert BADGE not in page and NOTE in page
    assert reverse("lot_ledger_action", args=[w["lot"].pk, "charge"]) not in page
    assert reverse("lot_ledger_action", args=[w["lot"].pk, "collection"]) not in page
    page = _page(client, reverse("lot_ledger", args=[w["other"].pk]))
    assert BADGE in page and reverse("lot_ledger_action", args=[w["other"].pk, "charge"]) in page
    response = client.post(
        reverse("lot_ledger_action", args=[w["lot"].pk, "credit"]),
        {"amount": "1.00", "description": "x"},
    )
    assert response.status_code == 302
    with system_access("t"):
        from ledger.models import LedgerEntry

        assert not LedgerEntry.objects.filter(lot=w["lot"], kind="credit").exists()


# --- 11. Account changes ----------------------------------------------------------------------


def test_account_change_to_own_home_needs_another_board_member(w):
    with system_access("t"):
        account = Account.objects.active().get(lot=w["lot"])
    change = community.request_account_change(
        account, action="add", kind="authorized", name="Pat", email="pat@example.com",
        actor=w["co_owner"],
    )  # fmt: skip
    assert community.own_change_refusal(change, w["bm"])
    with pytest.raises(community.CommunityError, match="another board member"):
        community.decide_account_change(change, approve=True, actor=w["bm"])
    assert _refusals("account_change")
    community.decide_account_change(change, approve=False, note="No", actor=w["board2"])


def test_people_on_own_account_change_only_through_another_board_member(client, w):
    with system_access("t"):
        account = Account.objects.active().get(lot=w["lot"])
        ownership = w["co_owner"].person.ownerships.first()
    tenant = PersonFactory()
    for call in (
        lambda: community.add_account_contact(
            account, person=tenant, kind=ContactKind.TENANT, actor=w["bm"]
        ),
        lambda: community.record_ownership(
            person=tenant, lot=w["lot"], start_date=days_ago(1), actor=w["bm"]
        ),
        lambda: community.end_ownership(ownership, actor=w["bm"]),
        lambda: community.transfer_lot(w["lot"], new_owners=[tenant], actor=w["bm"]),
    ):
        with pytest.raises(community.CommunityError, match="your own home's account"):
            call()
    assert len(_refusals("account")) == 4
    client.force_login(w["bm"])
    page = _page(client, reverse("lot_detail", args=[w["lot"].pk]))
    assert BADGE not in page and NOTE in page
    assert reverse("lot_transfer", args=[w["lot"].pk]) not in page
    assert reverse("lot_account_contact_add", args=[account.pk]) not in page
    client.force_login(w["board2"])
    page = _page(client, reverse("lot_detail", args=[w["lot"].pk]))
    assert BADGE in page and reverse("lot_transfer", args=[w["lot"].pk]) in page
    community.add_account_contact(
        account, person=tenant, kind=ContactKind.TENANT, actor=w["board2"]
    )


# --- 12. Refunds ------------------------------------------------------------------------------


def test_refund_to_own_account_cannot_be_proposed_by_the_board_member(w):
    from ledger import disbursements as disb

    FeatureSetting.objects.create(key="disbursements", enabled=True)
    ledger.post_credit(lot=w["lot"], amount_cents=2000, description="Overpaid", actor=w["board2"])
    with system_access("t"):
        account = Account.objects.active().get(lot=w["lot"])
    with pytest.raises(disb.DisbursementError):
        disb.propose_refund(account=account, amount_cents=1000, actor=w["bm"])
    batch = disb.propose_refund(account=account, amount_cents=1000, actor=w["manager"])
    with pytest.raises(disb.DisbursementError, match="owner on the account"):
        disb.approve(batch, actor=w["bm"])
    with pytest.raises(disb.DisbursementError, match="owner on the account"):
        disb.decline(batch, note="No", actor=w["bm"])
    assert disb.approve(batch, actor=w["board2"]).status == "approved"


# --- 13. Two-person coverage ------------------------------------------------------------------


def _coverage_titles(user):
    return [i.title for i in items_for(user) if i.title.startswith("Only you can act on")]


def test_coverage_warning_when_no_other_board_member_can_act():
    lot = LotFactory()
    solo = make_member(board=True, lots=[lot])
    req = ops.submit_request(
        lot=lot, request_type="maintenance", subject="Gutter", description="Loose", actor=solo
    )
    (title,) = _coverage_titles(solo)
    assert title == (
        f"Only you can act on request #{req.pk} and it's your own — another board member is needed"
    )
    # Another board member with two-step sign-in: no warning.
    make_member(board=True)
    assert not _coverage_titles(solo)


def test_coverage_counts_only_people_with_two_step_sign_in_and_without_a_conflict():
    lot = LotFactory()
    solo = make_member(board=True, lots=[lot])
    ops.submit_request(
        lot=lot, request_type="maintenance", subject="Gutter", description="Loose", actor=solo
    )
    make_member(board=True, mfa=False)  # cannot sign in safely yet
    co_owner_on_board = make_member(board=True)
    community.record_ownership(
        person=co_owner_on_board.person, lot=lot, start_date=days_ago(10), actor=SYSTEM
    )
    assert _coverage_titles(solo)
    make_member(roles=[Role.MANAGER])  # the manager can work requests
    assert not _coverage_titles(solo)


def test_own_matters_leave_the_staff_queue(w):
    req = _request(w)
    titles = [i.title for i in items_for(w["bm"]) if i.area == "Owner requests"]
    assert not any(f"#{req.pk} " in t for t in titles)
    titles = [i.title for i in items_for(w["board2"]) if i.area == "Owner requests"]
    assert any(f"#{req.pk} " in t for t in titles)


# --- 14. Cross-owner access is unchanged -----------------------------------------------------


def test_owner_still_cannot_reach_another_homes_matters(client, w):
    req = _request(w)
    client.force_login(w["owner_b"])
    assert client.get(reverse("request_detail", args=[req.pk])).status_code == 404
    assert client.get(reverse("lot_ledger", args=[w["lot"].pk])).status_code in (403, 404)
    stranger = PersonFactory()
    assert not conflicts.own_matter(w["owner_b"], stranger)
