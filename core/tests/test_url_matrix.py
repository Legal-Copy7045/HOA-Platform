"""Every URL is classified and cross-owner access is refused everywhere.

CLAUDE.md: "Every feature ships with cross-owner access tests: owner A must
get 403/404 for owner B's lot ... on every URL and download path."

Adding a URL without classifying it here fails
test_every_named_url_is_classified - by design.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.urls import get_resolver, reverse
from django.utils import timezone

from accounts import mfa_reset
from accounts import services as account_services
from accounts.models import Invitation, MfaReset, VerificationMethod
from community import services
from community.models import (
    Account,
    BoardSeat,
    Office,
    OfficeHolding,
    Ownership,
    Person,
    Role,
    RoleAssignment,
    SysadminNomination,
)
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db

PUBLIC = {
    "home",
    "healthz",
    "invite_landing",
    "invite_accept",
    "account_setup_expired",
    "login_background_image",
}
# Links emailed before sign-in became mandatory (D24): sign-in first, then
# only the neighboring lot's owner is sent on to their review page.
OLD_EMAIL_LINKS = {"signoff_link", "signoff_link_file", "ballot_link"}
# Signed-in account pages provided by allauth (each acts only on the signed-in
# user's own account) plus our enrollment-code page.
OWN_ACCOUNT_PREFIXES = ("account_", "mfa_")
OWN_ACCOUNT = {
    "enroll_with_code",
    "my_profile",
    "export_my_data",
    "owner_preview_stop",
    "contact_method_add",
}
# Text from board-only or other-lot fixtures that must never reach owner A.
BOARD_ONLY_MARKERS = (
    b"Board only: legal matter",
    b"Board executive session",
    b"pond contractor",
    b"counsel letter",
    b"Lot B drainage survey",
    b"Executive session: litigation",
    b"Lot B wants the 2019 audit",
    b"Draft: special assessment",
    b"Lot B hot tub",
    b"Lot B boat in driveway",
    b"Lot B Buyer Agent",
    b"Lot B leaking gutter",
    b"Board note on lot B",
    b"Lot B pond",
    b"Lot B nephew",
    b"Lot B pavilion party",
)
# Pages any member may open; tests check board-only content never shows to owners.
MEMBER_PAGES = {
    "announcements",
    "calendar",
    "directory",
    "documents",
    "meetings",
    "resolutions",
    "votes",
    "records_requests",
    "arc_list",
    "violations",
    "my_account",
    "resales",
    "requests",
    "faq",
    "signoffs_mine",
    "elections",
    "owner_budget",
    "reservations",
}

# Detail pages every member may open by design (community-wide, not lot
# data): checked for sign-in and that nothing board-only shows.
COMMUNITY_DETAIL = {
    "election_detail": ("GET", "election"),
    "election_action": ("POST", "election"),
}

# name -> (method, kwargs builder key). Every one must refuse owner A when
# pointed at owner B's data, and send anonymous visitors to sign in.
PROTECTED = {
    "my_lot": ("GET", None),
    "lot_list": ("GET", None),
    "lot_detail": ("GET", "lot"),
    "lot_transfer": ("POST", "lot"),
    "my_items": ("GET", None),
    "proxy_grant": ("POST", "executive_meeting"),
    "proxy_revoke": ("POST", "proxy_b"),
    "consents": ("GET", None),
    "consent_detail": ("GET", "consent"),
    "compliance": ("GET", None),
    "onboarding": ("GET", None),
    "arc_signoff_action": ("POST", "arc_b"),
    "arc_signoff_form": ("GET", "arc_b"),
    "arc_neighbors": ("GET", None),
    "signoff_detail": ("GET", "signoff_b"),
    "signoff_file": ("GET", "signoff_b"),
    "seat_nomination_decide": ("POST", "seat_proposal"),
    "roster": ("GET", None),
    "contracts": ("GET", None),
    "contract_detail": ("GET", "contract"),
    "contract_action": ("POST", "contract"),
    "account_change_request": ("POST", "account_b"),
    "account_change_withdraw": ("POST", "change_b"),
    "account_change_decide": ("POST", "change_b"),
    "arc_project_types": ("GET", None),
    "committees": ("GET", None),
    "committee_detail": ("GET", "committee_b"),
    "committee_action": ("POST", "committee_b"),
    "committee_task_close": ("POST", "committee_b"),
    "committee_topic_detail": ("GET", "topic_b"),
    "committee_member_end": ("POST", "membership_b"),
    "committee_upload": ("POST", "committee_b"),
    "folder_create": ("POST", None),
    "document_move": ("POST", "board_document"),
    "request_detail": ("GET", "request_b"),
    "request_action": ("POST", "request_b"),
    "request_assign": ("POST", "request_b"),
    "request_work_order": ("POST", "request_b"),
    "thread_post": ("POST", "request_b"),
    "follow_up_set": ("POST", "violation_b"),
    "lot_account_number": ("POST", "account_b"),
    "lot_account_contact_add": ("POST", "account_b"),
    "lot_account_contact_end": ("POST", "account_contact_b"),
    "contact_method_remove": ("POST", "contact_method_b"),
    "my_history": ("GET", None),
    "board_home": ("GET", None),
    "owner_preview_start": ("POST", None),
    "branding_settings": ("GET", None),
    "feature_settings": ("GET", None),
    "approvals": ("GET", None),
    "people": ("GET", None),
    "person_detail": ("GET", "person"),
    "person_add_ownership": ("POST", "person"),
    "person_add_seat": ("POST", "person"),
    "person_add_office": ("POST", "person"),
    "person_add_role": ("POST", "person"),
    "sysadmin_nominate": ("POST", "person"),
    "ownership_end": ("POST", "ownership"),
    "seat_end": ("POST", "seat"),
    "office_end": ("POST", "office"),
    "role_end": ("POST", "role"),
    "nomination_decide": ("POST", "nomination"),
    "invitations": ("GET", None),
    "invite_person": ("POST", "person"),
    "invitation_action": ("POST", "invitation"),
    "reset_start": ("POST", "person"),
    "reset_decide": ("POST", "reset"),
    "sysadmin_activity": ("GET", None),
    "system_status": ("GET", None),
    "community_settings": ("GET", None),
    "community_rule_save": ("POST", None),
    "community_profile_save": ("POST", None),
    "common_area_save": ("POST", None),
    "announcement_detail": ("GET", "board_announcement"),
    "event_cancel": ("POST", "event"),
    "tasks": ("GET", None),
    "task_close": ("POST", "task"),
    "document_detail": ("GET", "lot_b_document"),
    "document_download": ("GET", "lot_b_document"),
    "document_version_download": ("GET", "board_document"),
    "document_add_version": ("POST", "board_document"),
    "document_archive": ("POST", "board_document"),
    "meeting_detail": ("GET", "executive_meeting"),
    "meeting_action": ("POST", "executive_meeting"),
    "records_request_detail": ("GET", "records_request_b"),
    "records_request_action": ("POST", "records_request_b"),
    "vote_detail": ("GET", "draft_vote"),
    "vote_action": ("POST", "draft_vote"),
    "arc_detail": ("GET", "arc_b"),
    "arc_action": ("POST", "arc_b"),
    "arc_decide": ("POST", "arc_b"),
    "arc_position": ("POST", "arc_b"),
    "arc_attach": ("POST", "arc_b"),
    "arc_file": ("GET", "arc_b"),
    "arc_letter": ("GET", "arc_b"),
    "arc_affected": ("POST", "arc_b"),
    "violation_detail": ("GET", "violation_b"),
    "violation_action": ("POST", "violation_b"),
    "violation_respond": ("POST", "violation_b"),
    "violation_photo": ("POST", "violation_b"),
    "violation_file": ("GET", "violation_b"),
    "violation_letter": ("GET", "violation_b"),
    "vendors": ("GET", None),
    "vendor_detail": ("GET", "vendor"),
    "work_orders": ("GET", None),
    "work_order_detail": ("GET", "work_order"),
    "work_order_action": ("POST", "work_order"),
    "invoices": ("GET", None),
    "invoice_detail": ("GET", "invoice"),
    "invoice_action": ("POST", "invoice"),
    "invoice_file": ("GET", "invoice"),
    "my_statement_pdf": ("GET", "lot_id_b"),
    "ledger_home": ("GET", None),
    "lot_ledger": ("GET", "lot"),
    "lot_ledger_action": ("POST", "lot"),
    "lot_statement_pdf": ("GET", "lot"),
    "lot_notice_pdf": ("GET", "lot"),
    "letters_settings": ("GET", None),
    "collections": ("GET", None),
    "collection_detail": ("GET", "collection"),
    "collection_action": ("POST", "collection"),
    "collection_letter": ("POST", "collection"),
    "accounting": ("GET", None),
    "accounting_export": ("GET", None),
    "accounting_1099": ("GET", None),
    "reconcile": ("GET", None),
    "budgets": ("GET", None),
    "budget_detail": ("GET", "budget"),
    "resale_detail": ("GET", "resale_b"),
    "resale_action": ("POST", "resale_b"),
    "resale_pdf": ("GET", "resale_b"),
    "reports": ("GET", None),
    "report_run": ("GET", "report"),
    # Reservations, access review, retention (Milestone 4)
    "reservation_detail": ("GET", "reservation_b"),
    "reservation_action": ("POST", "reservation_b"),
    "reservation_settings": ("GET", None),
    "reservation_area_save": ("POST", "area"),
    "access_review": ("GET", None),
    "access_review_record": ("POST", None),
    "retention": ("GET", None),
    "retention_run": ("POST", None),
}


def _named_urls() -> set[str]:
    return {k for k in get_resolver().reverse_dict if isinstance(k, str)}


def test_every_named_url_is_classified():
    unclassified = {
        name
        for name in _named_urls()
        if name
        not in PUBLIC
        | OLD_EMAIL_LINKS
        | OWN_ACCOUNT
        | MEMBER_PAGES
        | set(PROTECTED)
        | set(COMMUNITY_DETAIL)
        and not name.startswith(OWN_ACCOUNT_PREFIXES)
    }
    assert unclassified == set(), f"Classify these URLs in {__name__}: {sorted(unclassified)}"


@pytest.fixture
def world():
    with (
        mock.patch("accounts.tasks.send_invitation.defer"),
        mock.patch("community.tasks.notify_board.defer"),
    ):
        lot_a, lot_b = (
            LotFactory(street_address="1 Alpha Way"),
            LotFactory(street_address="2 Bravo Way"),
        )
        owner_a = make_member(lots=[lot_a])
        owner_b = make_member(lots=[lot_b], board=True, roles=[Role.ARC_REVIEWER])
        board = make_member(board=True)
        board2 = make_member(board=True)
        person_b = Person.objects.unscoped("t").get(user=owner_b)
        services.record_office(
            person=person_b, office=Office.TREASURER, start_date=days_ago(5), actor=SYSTEM
        )
        nomination = services.nominate_sysadmin(person=person_b, actor=board)
        uninvited = PersonFactory()
        services.record_ownership(person=uninvited, lot=lot_b, start_date=days_ago(2), actor=SYSTEM)
        invitation = account_services.create_invitation(person=uninvited, actor=board)
        reset = mfa_reset.start_reset(
            subject=owner_b, actor=board, verification_method=VerificationMethod.IN_PERSON
        )
        from comms import services as comms
        from operations.services import create_task

        board_note = comms.publish_announcement(
            title="Board only: legal matter", body="Confidential", audience="board", actor=board
        )
        board_event = comms.create_event(
            title="Board executive session",
            starts_at=timezone.now() + timedelta(days=3),
            audience="board",
            actor=board,
        )
        task = create_task(title="Chase the pond contractor", actor=board)
        from documents import services as documents
        from documents.tests.helpers import pdf

        board_document = documents.create_document(
            title="Board only: counsel letter",
            category="other",
            visibility="board",
            file=pdf(),
            actor=board,
        )
        lot_b_document = documents.create_document(
            title="Lot B drainage survey",
            category="lot",
            visibility="lot",
            lot=lot_b,
            file=pdf(),
            actor=board,
        )
        from governance import services as governance

        executive_meeting = governance.create_meeting(
            title="Executive session: litigation",
            kind="executive",
            starts_at=timezone.now() + timedelta(days=5),
            actor=board,
        )
        records_request_b = governance.submit_records_request(
            description="Lot B wants the 2019 audit", actor=owner_b
        )
        draft_vote = governance.create_vote(
            title="Draft: special assessment", kind="informal", options=["Yes", "No"], actor=board
        )
        from arc import services as arc
        from documents.attachments import attach

        arc_b = arc.create_request(
            lot=lot_b, title="Lot B hot tub", description="Rear patio", actor=owner_b
        )
        arc_b_file = attach(arc_b, pdf("hot-tub.pdf"), actor=owner_b)
        from compliance import services as compliance

        violation_b = compliance.report(
            lot=lot_b, category="parking", description="Lot B boat in driveway", actor=board
        )
        violation_b_file = attach(violation_b, pdf("boat.pdf"), actor=board)
        from operations import services as operations
        from operations.models import ExpenseCategory

        vendor = operations.save_vendor(name="Pond Pros (demo)", actor=board)
        work_order = operations.create_work_order(title="Fix pond pump", actor=board)
        contract = operations.create_contract(
            vendor=vendor,
            title="Pond contract",
            category=ExpenseCategory.objects.create(code="9100", name="Pond"),
            schedule="monthly",
            amount_cap_cents=10000,
            starts_on=days_ago(30),
            citation="R-1",
            actor=board,
        )
        invoice = operations.create_invoice(
            vendor=vendor,
            number="INV-1",
            invoice_date=timezone.localdate(),
            amount_cents=12345,
            category=ExpenseCategory.objects.create(code="5100", name="Pond"),
            actor=board,
        )
        invoice_file = attach(invoice, pdf("invoice.pdf"), actor=board)
        from ledger import services as ledger

        ledger.post_charge(
            lot=lot_b, kind="other_charge", amount_cents=5000, description="Lot B fine", actor=board
        )
        collection = ledger.open_case(lot=lot_b, actor=board)
        budget = ledger.create_budget(fiscal_year=2027, actor=board)
        resale_b = ledger.request_resale(
            lot=lot_b, requester_name="Lot B Buyer Agent", actor=owner_b
        )
        from comms import threads
        from operations import services as operations

        request_b = operations.submit_request(
            lot=lot_b,
            request_type="maintenance",
            subject="Lot B leaking gutter",
            description="Water everywhere",
            actor=owner_b,
        )
        threads.post("request", request_b, body="Board note on lot B", actor=board)
        from governance import services as gov
        from governance.models import CommitteeMembership

        committee_b = gov.create_committee(name="Lot B pond committee", actor=board)
        gov.appoint_committee_member(committee_b, person=person_b, actor=board)
        topic_b = gov.start_topic(
            committee_b, title="Lot B pond plans", body="Board note on lot B", actor=owner_b
        )
        task_b = gov.add_committee_task(committee_b, title="Lot B pond task", actor=owner_b)
        membership_b = CommitteeMembership.objects.unscoped("t").get(person=person_b)
        _, seat_proposal = services.propose_board_seat(
            person=services.record_person(display_name="Lot B seat candidate", actor=SYSTEM),
            seat_type="owner_elected",
            term_start=days_ago(1),
            actor=board,
        )
        from arc import signoffs as arc_signoffs
        from arc.models import SignoffRequest

        lot_c = LotFactory(street_address="3 Charlie Way")
        make_member(lots=[lot_c])
        arc_signoffs.add_signoff_lot(arc_b, lot_c, actor=owner_b)
        signoff_b = SignoffRequest.objects.unscoped("t").get(arc_request=arc_b, lot=lot_c)
        from governance import elections as gov_elections

        annual = governance.create_meeting(
            title="Annual meeting",
            kind="annual",
            starts_at=timezone.now() + timedelta(days=30),
            actor=board,
        )
        proxy_b = gov_elections.grant_proxy(
            annual, lot=lot_b, holder=Person.objects.unscoped("t").get(user=board), actor=owner_b
        )
        election = gov_elections.create_election(title="Board election", seats=1, actor=board)
        consent = gov_elections.propose_consent(
            title="Board only: legal matter consent", text="x", actor=board
        )
        account_b = Account.objects.unscoped("t").get(lot=lot_b, closed_on__isnull=True)
        change_b = services.request_account_change(
            account_b, action="add", name="Lot B nephew", kind="view_only", actor=owner_b
        )
        contact_b = services.add_account_contact(
            account_b,
            person=services.record_person(display_name="Lot B tenant", actor=SYSTEM),
            kind="tenant",
            actor=SYSTEM,
        )
        method_b = services.add_contact_method(
            person_b, kind="phone", value="555-0100", actor=SYSTEM
        )
        # Reservations are an optional feature: switched on to check its pages.
        from community import reservations
        from community.models import CommonArea
        from core.models import FeatureSetting

        FeatureSetting.objects.create(key="reservations", enabled=True)
        area = CommonArea.objects.create(name="Pavilion", kind="amenity")
        reservations.save_area_settings(area, bookable=True, actor=SYSTEM)
        start = timezone.now() + timedelta(days=3)
        reservation_b = reservations.request_reservation(
            area=area,
            lot_id=lot_b.pk,
            starts_at=start,
            ends_at=start + timedelta(hours=2),
            purpose="Lot B pavilion party",
            actor=owner_b,
        )
        reservations.approve(reservation_b, actor=board)
    targets = {
        "reservation_b": reservation_b.pk,
        "area": area.pk,
        "account_b": account_b.pk,
        "proxy_b": proxy_b.pk,
        "election": election.pk,
        "consent": consent.pk,
        "signoff_b": signoff_b.pk,
        "seat_proposal": seat_proposal.pk,
        "contract": contract.pk,
        "change_b": change_b.pk,
        "request_b": request_b.pk,
        "committee_b": committee_b.pk,
        "topic_b": topic_b.pk,
        "task_b": task_b.pk,
        "membership_b": membership_b.pk,
        "account_contact_b": contact_b.pk,
        "contact_method_b": method_b.pk,
        "lot": lot_b.pk,
        "person": person_b.pk,
        "ownership": Ownership.objects.unscoped("t").get(person=person_b).pk,
        "seat": BoardSeat.objects.unscoped("t").get(person=person_b).pk,
        "office": OfficeHolding.objects.unscoped("t").get(person=person_b).pk,
        "role": RoleAssignment.objects.unscoped("t").get(person=person_b).pk,
        "nomination": SysadminNomination.objects.unscoped("t").get(pk=nomination.pk).pk,
        "invitation": Invitation.objects.unscoped("t").get(pk=invitation.pk).pk,
        "reset": MfaReset.objects.unscoped("t").get(pk=reset.pk).pk,
        "board_announcement": board_note.pk,
        "event": board_event.pk,
        "task": task.pk,
        "board_document": board_document.pk,
        "lot_b_document": lot_b_document.pk,
        "executive_meeting": executive_meeting.pk,
        "records_request_b": records_request_b.pk,
        "draft_vote": draft_vote.pk,
        "arc_b": arc_b.pk,
        "arc_b_file": arc_b_file.pk,
        "violation_b": violation_b.pk,
        "violation_b_file": violation_b_file.pk,
        "vendor": vendor.pk,
        "work_order": work_order.pk,
        "invoice": invoice.pk,
        "invoice_file": invoice_file.pk,
        "lot_id_b": lot_b.pk,
        "collection": collection.pk,
        "budget": budget.pk,
        "resale_b": resale_b.pk,
        "report": "ar_aging",
    }
    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "board": board,
        "board2": board2,
        "lot_a": lot_a,
        "lot_b": lot_b,
        "targets": targets,
    }


def _url(name, key, targets):
    kwargs = {}
    if key:
        kwargs["pk"] = targets[key]
    if name == "nomination_decide" or name == "reset_decide":
        kwargs["decision"] = "approve"
    elif name == "invitation_action":
        kwargs["action"] = "revoke"
    elif name == "task_close":
        kwargs["status"] = "done"
    elif name == "document_version_download":
        kwargs["version"] = 1
    elif name == "document_archive":
        kwargs["action"] = "archive"
    elif name == "meeting_action":
        kwargs["action"] = "notice"
    elif name == "records_request_action":
        kwargs["action"] = "withdraw"
    elif name == "vote_action":
        kwargs["action"] = "open"
    elif name == "arc_action":
        kwargs["action"] = "withdraw"
    elif name == "arc_decide":
        kwargs["kind"] = "decision"
    elif name == "arc_file":
        kwargs["file_id"] = targets["arc_b_file"]
    elif name == "arc_letter":
        kwargs["decision_id"] = 1
    elif name == "violation_action":
        kwargs["action"] = "send_notice"
    elif name == "violation_file":
        kwargs["file_id"] = targets["violation_b_file"]
    elif name == "request_action":
        kwargs["action"] = "withdraw"
    elif name == "account_change_decide" or name == "seat_nomination_decide":
        kwargs["decision"] = "approve"
    elif name == "arc_signoff_action":
        kwargs["action"] = "prepare"
    elif name == "signoff_file":
        kwargs["file_id"] = targets["arc_b_file"]
    elif name == "election_action":
        kwargs["action"] = "nominate"
    elif name == "contract_action":
        kwargs["action"] = "approve"
    elif name == "committee_action":
        kwargs["action"] = "topic"
    elif name == "committee_task_close":
        kwargs["task_id"] = targets["task_b"]
    elif name == "thread_post":
        kwargs["kind"] = "request"
    elif name == "follow_up_set":
        kwargs["kind"] = "violation"
    elif name == "work_order_action":
        kwargs["action"] = "cancel"
    elif name == "invoice_action":
        kwargs["action"] = "submit"
    elif name == "invoice_file":
        kwargs["file_id"] = targets["invoice_file"]
    elif name == "my_statement_pdf":
        kwargs = {"lot_id": targets["lot_id_b"]}
    elif name == "lot_ledger_action":
        kwargs["action"] = "credit"
    elif name == "lot_statement_pdf":
        kwargs["kind"] = "statement"
    elif name == "lot_notice_pdf":
        kwargs["kind"] = "first"
    elif name == "accounting_1099":
        kwargs = {"year": 2026}
    elif name == "collection_action":
        kwargs["action"] = "send_reminder"
    elif name == "resale_action":
        kwargs["action"] = "issue"
    elif name == "report_run":
        kwargs = {"key": "ar_aging"}
    elif name == "reservation_action":
        kwargs["action"] = "cancel"
    return reverse(name, kwargs=kwargs)


@pytest.mark.parametrize("name", sorted(PROTECTED))
def test_owner_a_cannot_reach_owner_b_data(client, world, name):
    method, key = PROTECTED[name]
    client.force_login(world["owner_a"])
    url = _url(name, key, world["targets"])
    response = client.get(url) if method == "GET" else client.post(url, {})
    if name == "my_lot":  # their own page: allowed, but must not show B's lot
        assert response.status_code == 200
        assert b"2 Bravo Way" not in response.content
        assert b"1 Alpha Way" in response.content
    else:
        assert response.status_code in (403, 404), f"{name} -> {response.status_code}"
    # Nothing changed as a side effect.
    assert Ownership.objects.unscoped("t").filter(end_date__isnull=False).count() == 0
    assert not RoleAssignment.objects.unscoped("t").filter(role=Role.SYSADMIN).exists()


@pytest.mark.parametrize("name", sorted(PROTECTED))
def test_anonymous_visitors_are_sent_to_sign_in(client, world, name):
    method, key = PROTECTED[name]
    url = _url(name, key, world["targets"])
    response = client.get(url) if method == "GET" else client.post(url, {})
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


@pytest.mark.parametrize("name", sorted(OLD_EMAIL_LINKS))
def test_no_anonymous_access_through_emailed_links(client, world, name):
    kwargs = {"token": "x" * 43}
    if name == "signoff_link_file":
        kwargs["file_id"] = 1
    response = client.get(reverse(name, kwargs=kwargs))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


def test_owner_sees_own_lot_detail(client, world):
    client.force_login(world["owner_a"])
    response = client.get(reverse("lot_detail", args=[world["lot_a"].pk]))
    assert response.status_code == 200
    assert b"1 Alpha Way" in response.content
    assert b"Transfer this lot" not in response.content  # board-only controls


@pytest.mark.parametrize(
    ("who", "allowed", "denied"),
    [
        (
            "auditor",
            ["lot_list", "sysadmin_activity"],
            ["board_home", "people", "system_status", "invitations", "approvals"],
        ),
        (
            "sysadmin",
            ["system_status", "invitations"],
            ["lot_list", "board_home", "people", "sysadmin_activity", "my_lot"],
        ),
        ("arc", [], ["lot_list", "board_home", "people", "system_status", "my_lot"]),
    ],
)
def test_role_page_access(client, world, who, allowed, denied):
    user = make_member(
        roles=[{"auditor": Role.AUDITOR, "sysadmin": Role.SYSADMIN, "arc": Role.ARC_REVIEWER}[who]]
    )
    client.force_login(user)
    for name in allowed:
        assert client.get(reverse(name)).status_code == 200, name
    for name in denied:
        assert client.get(reverse(name)).status_code == 403, name


def test_board_pages_load(client, world):
    client.force_login(world["board"])
    for name in (
        "board_home",
        "people",
        "approvals",
        "invitations",
        "lot_list",
        "sysadmin_activity",
    ):
        assert client.get(reverse(name)).status_code == 200, name
    for key, name in (("person", "person_detail"), ("lot", "lot_detail")):
        assert client.get(reverse(name, args=[world["targets"][key]])).status_code == 200


def test_transfer_through_the_page_revokes_the_seller_at_once(client, world):
    buyer = PersonFactory()
    client.force_login(world["board"])
    client.post(
        reverse("lot_transfer", args=[world["lot_a"].pk]),
        {"new_owners": [buyer.pk], "transfer_date": days_ago(0).isoformat()},
    )
    client.force_login(world["owner_a"])
    assert client.get(reverse("lot_detail", args=[world["lot_a"].pk])).status_code == 404
    assert client.get(reverse("my_lot")).status_code == 403


def test_sysadmin_requests_are_audited_and_shown_to_the_board(client, world):
    from audit.models import AuditEvent

    admin = make_member(roles=[Role.SYSADMIN])
    client.force_login(admin)
    client.get(reverse("system_status"))
    event = AuditEvent.objects.filter(action="sysadmin.request", actor=admin).latest("id")
    assert event.metadata["view"] == "system_status"
    client.force_login(world["board"])
    page = client.get(reverse("sysadmin_activity")).content.decode()
    assert "sysadmin.request" in page and admin.email in page


def test_reset_code_is_shown_once_to_the_approver(client, world):
    from audit.models import AuditEvent

    reset_id = world["targets"]["reset"]
    client.force_login(world["board2"])
    with mock.patch("community.tasks.notify_board.defer"):
        response = client.post(reverse("reset_decide", args=[reset_id, "approve"]))
    assert response.status_code == 200
    code = response.context["code"]
    assert code.encode() in response.content
    assert response["Cache-Control"].startswith("max-age=0")
    assert code not in str(list(AuditEvent.objects.values_list("metadata", flat=True)))


@pytest.mark.parametrize("name", sorted(MEMBER_PAGES | {"my_profile", "export_my_data"}))
def test_member_pages_require_sign_in(client, world, name):
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


@pytest.mark.parametrize("name", sorted(MEMBER_PAGES))
def test_member_pages_hide_board_only_content_from_owners(client, world, name):
    client.force_login(world["owner_a"])
    response = client.get(reverse(name))
    assert response.status_code == 200
    for secret in BOARD_ONLY_MARKERS:
        assert secret not in response.content


def test_export_contains_only_my_own_records(client, world):
    import json

    client.force_login(world["owner_a"])
    data = json.loads(client.get(reverse("export_my_data")).content)
    assert [o["lot"] for o in data["community"]["ownerships"]] == [world["lot_a"].number]
    assert "Bravo" not in json.dumps(data)


@pytest.mark.parametrize("name", sorted(COMMUNITY_DETAIL))
def test_community_detail_pages_require_sign_in_and_hide_board_content(client, world, name):
    method, key = COMMUNITY_DETAIL[name]
    url = _url(name, key, world["targets"])
    response = client.get(url) if method == "GET" else client.post(url, {})
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))
    if method == "GET":
        client.force_login(world["owner_a"])
        page = client.get(url)
        assert page.status_code == 200
        for secret in BOARD_ONLY_MARKERS:
            assert secret not in page.content
