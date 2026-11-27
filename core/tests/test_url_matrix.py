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
    # Can't sign in? (D62): the guided help and its steps are sign-in steps.
    # They show no portal data and nothing about any account (tested below);
    # the setup-link form answers every address the same way.
    "signin_help",
    "signin_help_device",
    "signin_help_setup",
    "signin_help_email",
    "signin_help_locked",
    # Self-service account recovery (D58): the email form, the emailed link
    # and its confirm/finish page are sign-in steps too.
    "recovery_request",
    "recovery_link",
    "recovery_continue",
    # Vendor onboarding (D79, owner-approved exception): the public vendor
    # form. Only a valid emailed link opens a session, bound to one round;
    # that session reaches nothing else (operations/tests/
    # test_vendor_onboarding_public.py iterates every URL).
    "vendor_onboarding_start",
    "vendor_onboarding_form",
    "vendor_onboarding_upload",
    "vendor_onboarding_submit",
    "vendor_onboarding_done",
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
    # D44: the official notices step acts on the signed-in person only (no id).
    "notice_choice",
    # Installable app and alerts (Milestone 4): the manifest and service
    # worker hold nothing personal; the rest act on the signed-in login only.
    "pwa_manifest",
    "pwa_service_worker",
    "push_subscribe",
    "push_unsubscribe",
    "sms_start",
    "sms_verify",
    "sms_stop",
    # The statement's "Pay online" QR code (D43): opens the signed-in login's
    # own home only; a hint for another home is ignored.
    "pay_start",
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
    b"Draft survey: special assessment",
    # Reserve plan (reserves): component notes are board only.
    b"Board note on reserve pond liner",
)
# Pages any member may open; tests check board-only content never shows to owners.
MEMBER_PAGES = {
    "announcements",
    "calendar",
    "directory",
    "documents",
    "meetings",
    "resolutions",
    "records_requests",
    "arc_list",
    "violations",
    "my_account",
    "resales",
    "requests",
    "faq",
    "signoffs_mine",
    "owner_budget",
    "reservations",
    # Navigation and UX review (ux_nav): hub pages.
    "voting",
    "community_info",
    # Portal search (core.search): see also test_search_never_shows_board_only_content.
    "search",
    # Reserve plan (reserves): the adopted plan's summary (now a redirect to
    # Budget and reserves, streamlining).
    "reserve_summary",
    # Owner help (D73): Help, Welcome, Getting started (the signed-in login's
    # own steps only) and Ask the board (opens the login's own new request).
    "help",
    "welcome",
    "getting_started",
    "ask_board",
    # Capital projects shared with owners (D83): title, status, budget and
    # spent only (ledger/tests/test_finance_options.py checks the rest).
    "community_projects",
    # Expense reimbursements (D77): a volunteer owner sees only their own claims.
    "reimbursements",
    "reimbursement_new",
}

# Governance streamlining (2026-11-26): old list pages that now redirect to
# the one Voting page with their type preselected (bookmarks, emailed links).
VOTING_REDIRECTS = {
    "votes": "/voting/?type=votes",
    "elections": "/voting/?type=elections",
    "surveys": "/voting/?type=surveys",
}

# Detail pages every member may open by design (community-wide, not lot
# data): checked for sign-in and that nothing board-only shows.
COMMUNITY_DETAIL = {
    "election_detail": ("GET", "election"),
    "election_action": ("POST", "election"),
    # Owner help (D73): a "How it works" page and the board's text on it.
    "help_topic": ("GET", None),
    "help_text_save": ("POST", None),
}

# Board and committees (streamlining, owner decision 2026-11-26): every
# resident sees board members' names and offices and each committee's name
# and members by design; topics, tasks, files and contact details never.
NAMES_AND_OFFICES = {"board_and_committees"}

# name -> (method, kwargs builder key). Every one must refuse owner A when
# pointed at owner B's data, and send anonymous visitors to sign in.
PROTECTED = {
    "my_lot": ("GET", None),
    "lot_list": ("GET", None),
    "lot_detail": ("GET", "lot"),
    "lot_transfer": ("POST", "lot"),
    "my_items": ("GET", None),
    "settings_hub": ("GET", None),
    "finance_more": ("GET", None),
    "finance_overview": ("GET", None),
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
    "account_change_deed": ("GET", "owner_change_b"),
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
    "request_attach": ("POST", "request_b"),
    "request_file": ("GET", "request_b"),
    "request_action": ("POST", "request_b"),
    "request_assign": ("POST", "request_b"),
    "request_work_order": ("POST", "request_b"),
    "thread_post": ("POST", "request_b"),
    "follow_up_set": ("POST", "violation_b"),
    "lot_account_number": ("POST", "account_b"),
    "lot_account_contact_add": ("POST", "account_b"),
    "lot_account_contact_end": ("POST", "account_contact_b"),
    "contact_method_remove": ("POST", "contact_method_b"),
    "contact_method_notices": ("POST", "contact_method_b"),
    "my_history": ("GET", None),
    # A former owner's own period by its index in their roles (WT-010).
    "my_history_statement_pdf": ("GET", None),
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
    # D58: a board page about one recovery (owners get 403).
    "recovery_board": ("GET", "recovery_b"),
    "sysadmin_activity": ("GET", None),
    "system_status": ("GET", None),
    "server_tasks": ("GET", None),
    # D74: the incident timeline (sysadmins); bounced email actions (board,
    # and board or manager for the paper copy).
    "ops_timeline": ("GET", None),
    "person_email_bounce_clear": ("POST", "person"),
    "bounce_paper_done": ("POST", "lot"),
    "demo_data": ("POST", None),  # 404 unless demo data is allowed; sysadmins only (D53)
    "community_settings": ("GET", None),
    # Community setup wizard (D51): board only.
    "setup_home": ("GET", None),
    "setup_start": ("POST", None),
    "setup_step": ("GET", None),
    # Import homes and owners (D54): board only.
    "register_import": ("GET", None),
    "register_template": ("GET", None),
    "register_import_detail": ("GET", None),
    "register_import_run": ("POST", None),
    "register_import_rescan": ("POST", None),
    "register_import_remove": ("POST", None),
    "register_import_file": ("GET", None),
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
    # Who has voted (D61): board and manager only.
    "vote_who_voted_csv": ("GET", "draft_vote"),
    "election_who_voted_csv": ("GET", "election"),
    # Surveys (M5): a draft is invisible to owners; board steps are refused.
    "survey_detail": ("GET", "draft_survey"),
    "survey_action": ("POST", "draft_survey"),
    "survey_question_action": ("POST", "draft_survey_question"),
    "survey_results": ("GET", "draft_survey"),
    "survey_export": ("GET", "draft_survey"),
    "arc_detail": ("GET", "arc_b"),
    "arc_edit": ("POST", "arc_b"),
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
    "record_payments": ("GET", None),
    "lot_ledger": ("GET", "lot"),
    "lot_ledger_action": ("POST", "lot"),
    "lot_statement_pdf": ("GET", "lot"),
    "lot_notice_pdf": ("GET", "lot"),
    "letters_settings": ("GET", None),
    "collections": ("GET", None),
    "collection_detail": ("GET", "collection"),
    "collection_action": ("POST", "collection"),
    "collection_letter": ("POST", "collection"),
    "collection_paper_copy": ("POST", "collection"),
    "collection_notice_pdf": ("GET", "collection"),
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
    # Text alerts (Milestone 4; the feature is switched on in world()).
    "urgent_notice": ("GET", None),
    # Reservations, access review, retention (Milestone 4)
    "reservation_detail": ("GET", "reservation_b"),
    "reservation_action": ("POST", "reservation_b"),
    "reservation_settings": ("GET", None),
    "reservation_area_save": ("POST", "area"),
    "access_review": ("GET", None),
    "access_review_record": ("POST", None),
    "retention": ("GET", None),
    "retention_run": ("POST", None),
    # Meeting packets (M5): the build page is for builders; the board packet's
    # .zip lives on a meeting owners can see, and must still refuse them.
    "meeting_packet": ("GET", "executive_meeting"),
    "meeting_packet_zip": ("GET", "board_packet"),
    # Event RSVPs and volunteer sign-ups (rsvp): a board-only event's pages
    # are 404 for owners; the board side is 403.
    "event_detail": ("GET", "event"),
    "event_rsvp": ("POST", "event"),
    "event_rsvp_settings": ("POST", "event"),
    "event_slot_add": ("POST", "event"),
    "event_slot_action": ("POST", "event_slot"),
    "event_rsvp_export": ("GET", "event"),
    # Add to calendar (D49): the .ics file is scoped like its page.
    "event_ics": ("GET", "event"),
    "meeting_ics": ("GET", "executive_meeting"),
    # Reserve plan (reserves)
    "reserve_plan": ("GET", None),
    "reserve_component": ("GET", "reserve_component"),
    "reserve_adopt": ("POST", None),
    "reserve_export": ("GET", None),
    # Bank feed (bankfeeds; the feature is switched on in world()).
    "bank_feed": ("GET", None),
    "bank_connect": ("GET", None),
    "bank_account_add": ("POST", None),
    "bank_import": ("POST", None),
    "bank_match": ("POST", None),
    "bank_sync": ("POST", "bank_connection"),
    "bank_disconnect": ("POST", "bank_connection"),
    # Online payments (D32-payments; the feature is switched on in world()).
    "pay_online": ("GET", "lot_id_b"),
    "pay_checkout": ("POST", "lot_id_b"),
    "pay_return": ("GET", "checkout_b"),
    "payment_receipt": ("GET", "payment_b"),
    "online_payments": ("GET", None),
    "payment_alert_resolve": ("POST", "alert_b"),
    # Automatic payments (D46; the feature is switched on in world()).
    "autopay": ("GET", "lot_id_b"),
    "autopay_start": ("POST", "lot_id_b"),
    "autopay_cancel": ("POST", "lot_id_b"),
    "autopay_return": ("GET", "autopay_setup_b"),
    # D47: the payer's schedule change and the board's review decision.
    "autopay_schedule": ("POST", "lot_id_b"),
    "autopay_review": ("POST", "autopay_review_b"),
    # Disbursements (D52): board, manager and auditor pages only.
    "disbursements": ("GET", None),
    "disbursement_new": ("GET", None),
    "disbursement_detail": ("GET", "disbursement"),
    "disbursement_action": ("POST", "disbursement"),
    "disbursement_register": ("GET", "disbursement"),
    "disbursement_checks_pdf": ("GET", "disbursement"),
    "check_print_settings": ("GET", None),
    "check_alignment_pdf": ("GET", None),
    # Finance gaps (D75-D78): board, manager and auditor pages; lot B's plan,
    # owner B's claim and its receipt are 404 for owner A.
    "special_assessments": ("GET", None),
    "special_assessment_new": ("GET", None),
    "special_assessment_detail": ("GET", "special_assessment"),
    "special_assessment_action": ("POST", "special_assessment"),
    "payment_plans": ("GET", None),
    "payment_plan_new": ("GET", "collection"),
    "payment_plan_detail": ("GET", "plan_b"),
    "payment_plan_action": ("POST", "plan_b"),
    "payment_plan_letter": ("GET", "plan_b"),
    "payment_plan_acknowledge": ("POST", "plan_b"),
    "reimbursement_detail": ("GET", "claim_b"),
    "reimbursement_action": ("POST", "claim_b"),
    "reimbursement_file": ("GET", "claim_b"),
    "vendor_checks": ("GET", None),
    "vendor_checks_pdf": ("GET", None),
    "vendor_check_action": ("POST", "vendor_check"),
    "budget_prepare": ("POST", None),
    "budget_compare": ("GET", "budget"),
    # Paper mail (D55): board and manager only; owners get 403 everywhere.
    "paper_mail": ("GET", None),
    "paper_mail_new": ("POST", None),
    "paper_mail_settings": ("POST", None),
    "paper_mail_test": ("GET", None),
    "paper_mail_detail": ("GET", "mailing"),
    "paper_mail_letters": ("GET", "mailing"),
    "paper_mail_envelopes": ("GET", "mailing"),
    "paper_mail_labels": ("GET", "mailing"),
    "paper_mail_mark": ("POST", "mailing"),
    "paper_mail_piece_checked": ("POST", "mailing"),
    # Renewals and records retention (D72): board, manager and auditor pages
    # (disposal and holds board only); a document's date, access history and
    # version comparison are scoped like the document (lot B's is 404).
    "document_set_date": ("POST", "lot_b_document"),
    "document_access": ("GET", "lot_b_document"),
    "document_compare": ("GET", "lot_b_document"),
    "renewals": ("GET", None),
    "renewal_policy_new": ("GET", None),
    "renewal_policy": ("GET", None),
    "renewal_vendor": ("GET", "vendor"),
    "renewal_contract": ("GET", "contract"),
    "renewal_contract_decide": ("POST", "contract"),
    "records_retention": ("GET", None),
    "records_disposal_propose": ("POST", None),
    "records_disposal_decide": ("POST", None),
    "records_hold_place": ("POST", None),
    "records_hold_release": ("POST", None),
    # Books and close, statements and tax (D69-D71): board, manager, auditor.
    "books_close": ("GET", None),
    "books_action": ("POST", None),
    "books_accounts": ("GET", None),
    "books_coding": ("GET", None),
    "accounting_quickbooks": ("GET", None),
    "financial_statements": ("GET", None),
    "financial_statements_csv": ("GET", None),
    "financial_statements_pdf": ("GET", None),
    "year_end_package": ("GET", None),
    "tax_worksheet": ("GET", None),
    "tax_download": ("GET", None),
    # Finance options (D80-D85): board, manager and auditor pages only (each
    # feature on or off; owners get 403/404 either way). Feature-on cross-owner
    # tests: ledger/tests/test_finance_options.py.
    "interest": ("POST", None),
    "cashflow": ("GET", None),
    "cashflow_export": ("GET", None),
    "collection_legal": ("POST", "collection"),
    "collection_legal_file": ("GET", "collection"),
    "capital_projects": ("POST", None),
    "capital_project_detail": ("GET", "lot"),
    "capital_project_file": ("GET", "lot"),
    "insurance_claims": ("POST", None),
    "insurance_claim_detail": ("GET", "lot"),
    "insurance_claim_file": ("GET", "lot"),
    # Vendor onboarding (D79): board and manager pages; owners get 403.
    "vendor_onboarding_list": ("GET", None),
    "vendor_onboarding_invite": ("POST", None),
    "vendor_onboarding_review": ("GET", "vendor_round"),
    "vendor_onboarding_action": ("POST", "vendor_round"),
    "vendor_onboarding_file": ("GET", "vendor_round"),
    "vendor_onboarding_vendor": ("GET", "vendor"),
    "vendor_onboarding_vendor_action": ("POST", "vendor"),
    "vendor_file_summary": ("GET", "vendor"),
    "vendor_requirements": ("GET", None),
    "vendor_w9_retention": ("GET", None),
    "vendor_w9_disposal": ("POST", None),
}
# Pages refused before their record is looked up: any id will do (D72).
PK_ONE = {"renewal_policy", "records_disposal_decide", "records_hold_release"}


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
        | set(VOTING_REDIRECTS)
        | set(PROTECTED)
        | set(COMMUNITY_DETAIL)
        | NAMES_AND_OFFICES
        and not name.startswith(OWN_ACCOUNT_PREFIXES)
    }
    assert unclassified == set(), f"Classify these URLs in {__name__}: {sorted(unclassified)}"


@pytest.fixture
def world():
    with (
        mock.patch("accounts.tasks.send_invitation.defer"),
        mock.patch("community.tasks.notify_board.defer"),
    ):
        from core.models import FeatureSetting

        for key in (  # off by default; checked here
            "push",
            "sms",
            "bank_feeds",
            "usps_tracking",
            "bounce_tracking",
            # Finance options (D80-D85)
            "overdue_interest",
            "cashflow_forecast",
            "collections_legal",
            "capital_projects",
            "owner_finance_dashboard",
            "insurance_claims",
            "vendor_onboarding",
        ):
            FeatureSetting.objects.create(key=key, enabled=True)
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
        from comms import rsvp

        rsvp.save_settings(board_event, enabled=True, actor=board)
        event_slot = rsvp.add_slot(board_event, title="Board snacks", capacity=2, actor=board)
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
            location="Clubhouse",
            starts_at=timezone.now() + timedelta(days=5),
            actor=board,
        )
        from governance import packets

        packet_meeting = governance.create_meeting(
            title="Open board meeting",
            kind="board",
            location="Clubhouse",
            starts_at=timezone.now() + timedelta(days=6),
            actor=board,
        )
        board_packet = packets.build_packet(
            packet_meeting, variant="board", attachment_ids=[board_document.pk], actor=board
        )
        records_request_b = governance.submit_records_request(
            description="Lot B wants the 2019 audit", actor=owner_b
        )
        draft_vote = governance.create_vote(
            title="Draft: special assessment", kind="formal", options=["Yes", "No"], actor=board
        )
        from governance import surveys as gov_surveys

        draft_survey = gov_surveys.create_survey(
            title="Draft survey: special assessment",
            opens_on=timezone.localdate(),
            closes_on=timezone.localdate() + timedelta(days=7),
            actor=board,
        )
        draft_survey_question = gov_surveys.add_question(
            draft_survey, kind="text", prompt="Thoughts?", actor=board
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
        from operations.models import ExpenseCategory, OnboardingRound

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
        from ledger import reserve_plan

        reserve_component = reserve_plan.save_component(
            name="Pond liner",
            category="stormwater",
            replacement_cost_cents=4500000,
            useful_life_years=20,
            remaining_life_years=3,
            notes="Board note on reserve pond liner",
            actor=board,
        )
        reserve_plan.adopt(actor=board)
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
        request_b_file = attach(request_b, pdf("gutter.pdf"), actor=owner_b)
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
            location="Clubhouse",
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
            account_b,
            action="add",
            name="Lot B nephew",
            kind="view_only",
            no_login=True,
            actor=owner_b,
        )
        owner_change_b = services.request_account_change(
            account_b,
            action="add_owner",
            name="Lot B spouse",
            email="lot-b-spouse@example.com",
            ownership_type="joint",
            deed_date=timezone.localdate(),
            deed=pdf("lot-b-deed.pdf"),
            actor=owner_b,
        )
        from documents.attachments import attachments_for

        owner_change_b_file = attachments_for(owner_change_b).get()
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
        from ledger.bankfeeds import services as bank_feed

        bank_connection = bank_feed.add_file_account(display_name="Operating", actor=board)
        # Online payments: owner B's checkout, payment and a board item on it.
        import secrets

        from core.scoping import system_access
        from payments.models import PaymentAlert, PendingCheckout, StripePayment

        FeatureSetting.objects.create(key="online_payments", enabled=True)
        with system_access("t"):
            checkout_b = PendingCheckout.objects.create(
                nonce=secrets.token_hex(32),
                lot=lot_b,
                account=account_b,
                person=person_b,
                user=owner_b,
                method="bank",
                base_cents=5000,
                total_cents=5000,
                expires_at=timezone.now() + timedelta(hours=24),
                session_id="cs_test_matrixb",
                state="paid",
            )
            entry_b = ledger.record_online_payment(
                lot=lot_b,
                amount_cents=5000,
                received_on=timezone.localdate(),
                payment_intent_id="pi_test_matrixb",
                apply_to=[],
                actor=SYSTEM,
            )
            payment_b = StripePayment.objects.create(
                checkout=checkout_b,
                lot=lot_b,
                entry=entry_b,
                payment_intent_id="pi_test_matrixb",
                charge_id="py_test_matrixb",
                method="bank",
                funding="bank",
                base_cents=5000,
            )
            alert_b = PaymentAlert.objects.create(
                kind="refund", key="re_test_matrixb", lot=lot_b, detail="Lot B refund"
            )
            from payments.models import AutopaySetup

            FeatureSetting.objects.create(key="autopay", enabled=True)
            autopay_setup_b = AutopaySetup.objects.create(
                nonce=secrets.token_hex(32),
                lot=lot_b,
                account=account_b,
                person=person_b,
                user=owner_b,
                method_type="card",
                customer_id="cus_test_matrixb",
                session_id="cs_test_matrixautopayb",
                expires_at=timezone.now() + timedelta(hours=24),
            )
            from payments.models import AutopayReview

            autopay_review_b = AutopayReview.objects.create(
                lot=lot_b,
                account=account_b,
                schedule="annual",
                period="2027",
                covers_from="2027-01",
                method="bank",
                base_cents=240_000,
                total_cents=240_000,
                limit_cents=50_000,
                expires_at=timezone.now() + timedelta(days=14),
            )
        from comms import papermail

        mailing = papermail.create_mailing(
            actor=board,
            defaults=papermail.source_defaults(),
            subject="Paper letter",
            body="Hello {owner_names}",
            everyone=True,
        )
    from community import services as community_services
    from ledger import disbursements

    # An equal split: the matrix's homes have no assessments paid (D67).
    community_services.set_rule(
        key="disbursement.surplus_by_share_paid", value="false", citation="t", actor=SYSTEM
    )
    disbursement = disbursements.propose_surplus(
        total_cents=4400, year=2026, resolution_ref="Resolution 2026-14", actor=board
    )
    # Finance gaps (D75-D77): a special assessment, lot B's payment plan,
    # owner B's reimbursement claim and a vendor check.
    from ledger import payment_plans
    from ledger import special_assessments as sa_service
    from operations import reimbursements as rb
    from operations import services as operations_services
    from operations import vendor_checks

    special = sa_service.levy(
        title="Pond special levy",
        purpose="Pond",
        total_cents=4400,
        due_dates=[timezone.localdate() + timedelta(days=30)],
        resolution_ref="Resolution 2026-20",
        actor=board,
    )
    ledger.post_charge(
        lot=lot_b, kind="other_charge", amount_cents=5000, description="Lot B fee", actor=board
    )
    plan_total = payment_plans.covered_charges(collection)[1]
    plan_b = payment_plans.prepare(
        collection,
        installments=[(timezone.localdate() + timedelta(days=10), plan_total)],
        actor=board,
    )
    claim_b = rb.submit(
        amount_cents=1200,
        spent_on=timezone.localdate(),
        payee="Lot B pond supplies",
        category=ExpenseCategory.objects.get(code="5100"),
        description="Lot B pond",
        files=[pdf("receipt-b.pdf")],
        actor=owner_b,
    )
    claim_b_file = attachments_for(claim_b).first()
    operations_services.INVOICE.apply(invoice, "submit", actor=board)
    operations_services.approve(invoice, actor=board2)
    vendor_check = vendor_checks.issue(
        [invoice.pk], first_number="9001", issued_on=timezone.localdate(), actor=board
    )[0]
    targets = {
        "special_assessment": special.pk,
        "plan_b": plan_b.pk,
        "claim_b": claim_b.pk,
        "claim_b_file": claim_b_file.pk,
        "vendor_check": vendor_check.pk,
        "disbursement": disbursement.pk,
        "mailing": mailing.pk,
        "checkout_b": checkout_b.pk,
        "payment_b": payment_b.pk,
        "alert_b": alert_b.pk,
        "autopay_setup_b": autopay_setup_b.pk,
        "autopay_review_b": autopay_review_b.pk,
        "bank_connection": bank_connection.pk,
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
        "owner_change_b": owner_change_b.pk,
        "owner_change_b_file": owner_change_b_file.pk,
        "request_b": request_b.pk,
        "request_b_file": request_b_file.pk,
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
        "recovery_b": _recovery_for(owner_b).pk,
        "board_announcement": board_note.pk,
        "event": board_event.pk,
        "event_slot": event_slot.pk,
        "task": task.pk,
        "board_document": board_document.pk,
        "lot_b_document": lot_b_document.pk,
        "executive_meeting": executive_meeting.pk,
        "packet_meeting": packet_meeting.pk,
        "board_packet": board_packet.pk,
        "records_request_b": records_request_b.pk,
        "draft_vote": draft_vote.pk,
        "draft_survey": draft_survey.pk,
        "draft_survey_question": draft_survey_question.pk,
        "arc_b": arc_b.pk,
        "arc_b_file": arc_b_file.pk,
        "violation_b": violation_b.pk,
        "violation_b_file": violation_b_file.pk,
        "vendor": vendor.pk,
        "vendor_round": OnboardingRound.objects.create(vendor=vendor, created_by=board).pk,
        "work_order": work_order.pk,
        "invoice": invoice.pk,
        "invoice_file": invoice_file.pk,
        "lot_id_b": lot_b.pk,
        "collection": collection.pk,
        "budget": budget.pk,
        "reserve_component": reserve_component.pk,
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


def _recovery_for(user):
    from accounts.models import AccountRecovery

    now = timezone.now()
    return AccountRecovery.objects.create(
        subject=user,
        wait_hours=48,
        lock_hours=48,
        ready_at=now + timedelta(hours=48),
        expires_at=now + timedelta(days=9),
    )


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
    elif name == "vote_action" or name == "survey_action":
        kwargs["action"] = "open"
    elif name == "survey_question_action":
        kwargs["action"] = "remove"
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
    elif name == "account_change_deed":
        kwargs["file_id"] = targets["owner_change_b_file"]
    elif name == "request_file":
        kwargs["file_id"] = targets["request_b_file"]
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
    elif name == "my_statement_pdf" or name in (
        "pay_online",
        "pay_checkout",
        "autopay",
        "autopay_start",
        "autopay_cancel",
        "autopay_schedule",
    ):
        kwargs = {"lot_id": targets["lot_id_b"]}
    elif name == "lot_ledger_action":
        kwargs["action"] = "credit"
    elif name == "lot_statement_pdf":
        kwargs["kind"] = "statement"
    elif name == "lot_notice_pdf" or name == "collection_notice_pdf":
        kwargs["kind"] = "first"
    elif name == "accounting_1099":
        kwargs = {"year": 2026}
    elif name == "my_history_statement_pdf":
        kwargs = {"index": 0}
    elif name == "collection_action":
        kwargs["action"] = "send_reminder"
    elif name == "resale_action":
        kwargs["action"] = "issue"
    elif name == "report_run":
        kwargs = {"key": "ar_aging"}
    elif name == "reservation_action":
        kwargs["action"] = "cancel"
    elif name == "meeting_packet_zip":
        kwargs = {"pk": targets["packet_meeting"], "packet_id": targets["board_packet"]}
    elif name == "event_slot_action":
        kwargs["action"] = "join"
    elif name == "setup_step":
        kwargs["step"] = "charge"
    elif name in ("help_topic", "help_text_save"):
        kwargs["slug"] = "paying"
    elif name == "disbursement_action":
        kwargs["action"] = "approve"
    elif name in ("collection_legal_file", "capital_project_file", "insurance_claim_file"):
        kwargs["file_id"] = 1
    elif name in ("special_assessment_action", "payment_plan_action"):
        kwargs["action"] = "cancel" if name == "special_assessment_action" else "approve"
    elif name == "reimbursement_action":
        kwargs["action"] = "approve"
    elif name == "reimbursement_file":
        kwargs["file_id"] = targets["claim_b_file"]
    elif name == "vendor_check_action":
        kwargs["action"] = "void"
    elif name == "register_template":
        kwargs["fmt"] = "xlsx"
    elif name.startswith("register_import_") or name in PK_ONE:
        kwargs["pk"] = 1
    elif name == "vendor_onboarding_action":
        kwargs["action"] = "approve"
    elif name == "vendor_onboarding_file":
        kwargs["file_id"] = 1
    elif name == "vendor_onboarding_vendor_action":
        kwargs["action"] = "update"
    elif name == "vendor_w9_disposal":
        kwargs = {"pk": 1, "step": "propose"}
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


SIGNIN_HELP_PAGES = sorted(n for n in PUBLIC if n.startswith("signin_help"))


@pytest.mark.parametrize("name", SIGNIN_HELP_PAGES)
def test_signin_help_pages_are_open_and_serve_no_portal_data(client, world, name):
    response = client.get(reverse(name))
    assert response.status_code == 200, name
    assert "_auth_user_id" not in client.session
    for marker in (*BOARD_ONLY_MARKERS, b"1 Alpha Way", b"2 Bravo Way", b"@example"):
        assert marker not in response.content, (name, marker)


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
    assert b"Transfer this home" not in response.content  # board-only controls


@pytest.mark.parametrize(
    ("who", "allowed", "denied"),
    [
        (
            "auditor",
            ["lot_list", "sysadmin_activity"],
            ["board_home", "people", "system_status", "server_tasks", "invitations", "approvals"],
        ),
        (
            "sysadmin",
            ["system_status", "server_tasks", "invitations"],
            ["lot_list", "board_home", "people", "sysadmin_activity", "my_lot"],
        ),
        (
            "arc",
            [],
            ["lot_list", "board_home", "people", "system_status", "server_tasks", "my_lot"],
        ),
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
    # The old Board overview sends bookmarks to My items (streamlining).
    assert client.get(reverse("board_home"))["Location"] == reverse("my_items")
    # Sign-ups are a view of Homes and owners for the board (WT-002).
    signups = reverse("lot_list") + "?view=signups"
    assert client.get(reverse("invitations"))["Location"] == signups
    assert client.get(signups).status_code == 200
    for name in (
        "people",
        "approvals",
        "lot_list",
        "sysadmin_activity",
        "server_tasks",
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


@pytest.mark.parametrize("name", sorted(VOTING_REDIRECTS))
def test_old_voting_lists_redirect_signed_in_members_only(client, world, name):
    response = client.get(reverse(name))
    assert response["Location"].startswith(reverse("account_login"))
    client.force_login(world["owner_a"])
    response = client.get(reverse(name))
    assert response.status_code == 302 and response["Location"] == VOTING_REDIRECTS[name]
    page = client.get(response["Location"])
    assert page.status_code == 200
    for secret in BOARD_ONLY_MARKERS:
        assert secret not in page.content


@pytest.mark.parametrize("name", sorted(MEMBER_PAGES | {"my_profile", "export_my_data"}))
def test_member_pages_require_sign_in(client, world, name):
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


@pytest.mark.parametrize("name", sorted(MEMBER_PAGES))
def test_member_pages_hide_board_only_content_from_owners(client, world, name):
    client.force_login(world["owner_a"])
    # follow: old pages that now redirect to their merged page (streamlining).
    response = client.get(reverse(name), follow=True)
    assert response.status_code == 200
    for secret in BOARD_ONLY_MARKERS:
        assert secret not in response.content


@pytest.mark.parametrize(
    "q", ["Lot B", "Board", "pond", "counsel", "Executive", "audit", "Draft", "nephew", "Bravo"]
)
def test_search_never_shows_board_only_content(client, world, q):
    """Searching for words from owner B's and board-only records finds none of
    them for owner A (the <mark> highlight is removed before checking)."""
    client.force_login(world["owner_a"])
    response = client.get(reverse("search"), {"q": q})
    assert response.status_code == 200
    page = response.content.replace(b"<mark>", b"").replace(b"</mark>", b"")
    for secret in BOARD_ONLY_MARKERS:
        assert secret not in page
    assert b"2 Bravo Way" not in page


def test_board_search_finds_the_same_records(client, world):
    """Positive control for the test above: the words do find these records."""
    client.force_login(world["board"])
    page = client.get(reverse("search"), {"q": "Lot B"}).content
    page = page.replace(b"<mark>", b"").replace(b"</mark>", b"")
    for secret in (b"Lot B hot tub", b"Lot B boat in driveway", b"Lot B leaking gutter"):
        assert secret in page


def test_export_contains_only_my_own_records(client, world):
    import json

    client.force_login(world["owner_a"])
    data = json.loads(client.get(reverse("export_my_data")).content)
    assert [o["lot"] for o in data["community"]["ownerships"]] == [world["lot_a"].number]
    assert "Bravo" not in json.dumps(data)


@pytest.mark.parametrize("name", sorted(NAMES_AND_OFFICES))
def test_board_and_committees_shows_names_and_offices_only(client, world, name):
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))
    client.force_login(world["owner_a"])
    page = client.get(reverse(name)).content
    # By design: committee names and members' names (owner B chairs nothing
    # secret here), the board with offices.
    assert b"Lot B pond committee" in page
    # Never: committee topics, tasks, notes, other lots' records or contact details.
    for secret in (*BOARD_ONLY_MARKERS, b"Lot B pond plans", b"Lot B pond task"):
        if secret != b"Lot B pond":
            assert secret not in page
    for detail in (b"2 Bravo Way", b"1 Alpha Way", b"@"):
        assert detail not in page.split(b"<main", 1)[-1]


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
