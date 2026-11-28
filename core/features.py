"""Optional features the board switches on and off (Milestone 4).

A switched-off feature disappears from the menu, its pages answer 404 for
everyone, and its "my items" entries are hidden. Its records stay in the
database untouched: switching it back on shows them again. Core functions
(lots, people, ARC, the ledger, documents, meetings, audit) cannot be
switched off.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.http import Http404
from django.urls import Resolver404, resolve


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    default: bool
    url_names: frozenset[str]


def _f(key, label, description, url_names, default=True) -> Feature:
    return Feature(key, label, description, default, frozenset(url_names))


FEATURES: list[Feature] = [
    _f(
        "announcements",
        "Announcements",
        "Board news posted to owners.",
        {"announcements", "announcement_detail"},
    ),
    _f(
        "calendar",
        "Calendar",
        "Community events and meeting dates, with RSVPs and volunteer sign-ups.",
        {
            "calendar",
            "event_cancel",
            "event_ics",
            # Event RSVPs and volunteer sign-ups (rsvp)
            "event_detail",
            "event_rsvp",
            "event_rsvp_settings",
            "event_slot_add",
            "event_slot_action",
            "event_rsvp_export",
        },
    ),
    _f("faq", "FAQ", "Frequently asked questions page.", {"faq"}),
    # Owner help (D73): help pages, the welcome page and the getting-started
    # checklist. "Ask the board" (ask_board) stays on: it opens a request.
    _f(
        "owner_help",
        "Help and getting started",
        "Plain-language help pages that read today's rules, a welcome page after the first "
        "sign-in and a getting-started checklist for new owners, contacts and tenants.",
        {"help", "help_topic", "help_text_save", "welcome", "getting_started"},
    ),
    _f(
        "directory",
        "Owner directory",
        "Opt-in directory of owners' contact details.",
        {"directory"},
    ),
    _f(
        "committees",
        "Committees",
        "Committee spaces with members, tasks and files.",
        {
            "committees",
            "committee_detail",
            "committee_action",
            "committee_task_close",
            "committee_topic_detail",
            "committee_member_end",
            "committee_upload",
        },
    ),
    _f(
        "votes",
        "Votes",
        "Formal (binding) votes of the owners. Polls are surveys.",
        {"votes", "vote_detail", "vote_action", "vote_who_voted_csv"},
    ),
    _f(
        "elections",
        "Elections and proxies",
        "Board elections, nominations, proxies.",
        {
            "elections",
            "election_detail",
            "election_action",
            "election_who_voted_csv",
            "proxy_grant",
            "proxy_revoke",
        },
    ),
    # --- Surveys (M5) ---
    _f(
        "surveys",
        "Surveys",
        "Informal multi-question surveys of owners (and, if chosen, tenants).",
        {
            "surveys",
            "survey_detail",
            "survey_action",
            "survey_question_action",
            "survey_results",
            "survey_export",
        },
    ),
    _f(
        "consents",
        "Written consents",
        "Board resolutions adopted by unanimous written consent (on Resolutions and policies).",
        {"consents", "consent_detail"},
    ),
    _f(
        "records_requests",
        "Records requests",
        "Owners' requests to inspect association records.",
        {"records_requests", "records_request_detail", "records_request_action"},
    ),
    # The Budget and reserves page ("owner_budget") is in ANY_OF below: it is
    # on while this or the reserve plan is on, and shows only the parts on.
    _f("owner_budget", "Budget for owners", "The adopted budget shown to owners.", set()),
    _f(
        "violations",
        "Violations",
        "Covenant violation notices, responses and hearings.",
        {
            "violations",
            "violation_detail",
            "violation_action",
            "violation_respond",
            "violation_photo",
            "violation_file",
            "violation_letter",
        },
    ),
    _f(
        "work_orders",
        "Work orders and vendors",
        "Vendors and work orders for maintenance.",
        {
            "work_orders",
            "work_order_detail",
            "work_order_action",
            "vendors",
            "vendor_detail",
            "request_work_order",
        },
    ),
    _f(
        "contracts",
        "Contracts",
        "Vendor contracts and renewal dates.",
        {"contracts", "contract_detail", "contract_action"},
    ),
    # --- Renewals and records retention (D72) ---
    _f(
        "renewals",
        "Renewals",
        "Reminders before the association's insurance policies, vendors' insurance "
        "certificates and contract notice deadlines run out.",
        {
            "renewals",
            "renewal_policy_new",
            "renewal_policy",
            "renewal_vendor",
            "renewal_contract",
            "renewal_contract_decide",
        },
    ),
    _f(
        "records_retention",
        "Records retention",
        "Each kind of document kept for its retention period, then listed for two board "
        "members to dispose of; legal holds stop disposal.",
        {
            "records_retention",
            "records_disposal_propose",
            "records_disposal_decide",
            "records_hold_place",
            "records_hold_release",
        },
    ),
    _f(
        "resale",
        "Resale certificates",
        "Resale certificate requests and issuing.",
        {"resales", "resale_detail", "resale_action", "resale_pdf"},
    ),
    _f(
        "compliance",
        "Compliance calendar",
        "Filing and renewal reminders for the board.",
        {"compliance"},
    ),
    _f(
        "reservations",
        "Common area reservations",
        "Owners book common areas (clubhouse, pavilion). Not needed yet.",
        {
            "reservations",
            "reservation_detail",
            "reservation_action",
            "reservation_settings",
            "reservation_area_save",
        },
        default=False,
    ),
    # Installable app alerts (Milestone 4). The app itself (manifest, offline
    # styles) is always on; these need keys or a provider in the environment.
    _f(
        "push",
        "Push notifications",
        "Owners can get update alerts on their phone or computer (needs VAPID keys).",
        # Turning alerts off is never gated: owners can always stop them and
        # remove what is stored, even while the board has the feature off
        # (review R2-008). Same for "sms_stop" below.
        {"push_subscribe"},
        default=False,
    ),
    _f(
        "sms",
        "Urgent text messages",
        "Owners can opt in to texts; the board sends urgent notices (needs an SMS provider).",
        {"sms_start", "sms_verify", "urgent_notice"},
        default=False,
    ),
    # --- Reserve plan (reserves) ---
    _f(
        "reserves",
        "Reserve plan",
        "An in-house reserve study: what will need replacing, when, and the reserve "
        "contribution that keeps the fund above zero.",
        {
            "reserve_plan",
            "reserve_component",
            "reserve_adopt",
            "reserve_export",
            "reserve_summary",
        },
    ),
    # Bank feed (bankfeeds, D32-bankfeeds): Teller's free tier or statement files.
    _f(
        "bank_feeds",
        "Bank feed",
        "Bank lines from Teller (free tier) or statement files, matched for reconciliation.",
        {
            "bank_feed",
            "bank_connect",
            "bank_sync",
            "bank_disconnect",
            "bank_import",
            "bank_account_add",
            "bank_match",
        },
        default=False,
    ),
    # Online payments (D32-payments): also needs Stripe keys on the server.
    _f(
        "online_payments",
        "Online payments",
        "Owners pay by bank account, debit card or credit card on Stripe's hosted page "
        "(needs Stripe keys on the server).",
        {
            "pay_start",
            "pay_online",
            "pay_checkout",
            "pay_return",
            "payment_receipt",
            "online_payments",
            "payment_alert_resolve",
        },
        default=False,
    ),
    # Disbursements (D52): surplus distributions and refunds of owners' credit.
    _f(
        "disbursements",
        "Disbursements",
        "Money paid back to owners: surplus distributions and refunds of a former owner's "
        "credit, by statement credit or check, with board approval.",
        {
            "disbursements",
            "disbursement_new",
            "disbursement_detail",
            "disbursement_action",
            "disbursement_register",
            "disbursement_checks_pdf",
            "check_print_settings",
            "check_alignment_pdf",
        },
    ),
    # Books and close (D69, D70): the association's own books, for a
    # self-managed association (on by default).
    _f(
        "books",
        "Books and month-end close",
        "The association's own books: bank accounts by fund (operating, reserve), bank lines "
        "coded to categories by rules, and the month and year close that runs by itself when "
        "everything balances.",
        {
            "books_close",
            "books_action",
            "books_accounts",
            "books_coding",
            "accounting_quickbooks",
        },
    ),
    # Financial statements and the year-end package (D71).
    _f(
        "financial_statements",
        "Financial statements",
        "Balance sheet by fund, income and expenses against the budget, reserve activity, "
        "the annual accounting and the year-end package for the treasurer and the tax "
        "preparer.",
        {
            "financial_statements",
            "financial_statements_csv",
            "financial_statements_pdf",
            "year_end_package",
            "tax_worksheet",
            "tax_download",
        },
    ),
    # --- Finance gaps (D75-D78) ---
    _f(
        "special_assessments",
        "Special assessments",
        "One-off assessments for a purpose such as a capital project, levied under a board "
        "resolution (and the owners' vote where required), in one sum or installments posted "
        "on their due dates.",
        {
            "special_assessments",
            "special_assessment_new",
            "special_assessment_detail",
            "special_assessment_action",
        },
    ),
    _f(
        "payment_plans",
        "Payment plans",
        "Installment plans for owners behind on their account: prepared, approved by a board "
        "member, tracked installment by installment, ending as completed or defaulted.",
        {
            "payment_plans",
            "payment_plan_new",
            "payment_plan_detail",
            "payment_plan_action",
            "payment_plan_letter",
            "payment_plan_acknowledge",
        },
    ),
    _f(
        "reimbursements",
        "Expense reimbursements",
        "Board members and committee members claim back association expenses they paid, "
        "with the receipt; another board member approves and the treasurer pays.",
        {
            "reimbursements",
            "reimbursement_new",
            "reimbursement_detail",
            "reimbursement_action",
            "reimbursement_file",
        },
    ),
    _f(
        "vendor_checks",
        "Checks",
        "Print checks for approved invoices and reimbursements on the association's check "
        "stock, with a check register, voids and clearing.",
        {"vendor_checks", "vendor_checks_pdf", "vendor_check_action"},
    ),
    # Automatic payments of the monthly assessment (D46): also needs
    # "online_payments" on and Stripe keys on the server.
    _f(
        "autopay",
        "Automatic payments",
        "Owners can have each monthly assessment, or twelve months once a year, paid "
        "automatically from a saved bank account or card (Stripe; needs Online payments on).",
        {
            "autopay",
            "autopay_start",
            "autopay_return",
            "autopay_cancel",
            "autopay_schedule",
            "autopay_review",
        },
        default=False,
    ),
    # USPS tracking of paper mail (D60): also needs the USPS login on the
    # server (infra/poc/set-usps.sh) and the tracking barcode on.
    _f(
        "usps_tracking",
        "USPS tracking",
        "Reads USPS scans of barcoded paper mail a few times a day and flags letters with no "
        "scan after 5 days (needs a USPS login on the server).",
        {"paper_mail_piece_checked"},
        default=False,
    ),
    # Bounced email (D74): also needs the bounce mailbox's login on the server
    # (infra/poc/set-bounce-mailbox.sh).
    _f(
        "bounce_tracking",
        "Bounced email",
        "Marks addresses that do not accept mail and asks for a paper copy when an official "
        "notice bounces. The bounces come from SMTP2Go's delivery reports when email goes "
        "through SMTP2Go, otherwise from the association's bounce mailbox every 20 minutes "
        "(needs the mailbox's login on the server).",
        {"person_email_bounce_clear", "bounce_paper_done"},
        default=False,
    ),
    # --- Finance options (D80-D85), all off by default ---
    _f(
        "overdue_interest",
        "Interest on overdue balances",
        "Simple interest on past-due assessments, posted once a month at the Declaration's "
        "rate (only if the Declaration allows it; set the rate with its citation).",
        {"interest"},
        default=False,
    ),
    _f(
        "cashflow_forecast",
        "Cash-flow forecast",
        "The next 12 months of operating and reserve cash, with what-if changes and a warning "
        "when a month runs low.",
        {"cashflow", "cashflow_export"},
        default=False,
    ),
    _f(
        "collections_legal",
        "Liens and legal collection",
        "On a collection case: attorney referral, demand letters, liens, lawsuits, costs, money "
        "recovered and the lien release (board only).",
        {"collection_legal", "collection_legal_file"},
        default=False,
    ),
    _f(
        "capital_projects",
        "Capital projects",
        "Big projects with their budget, approvals, change orders, contracts, invoices and "
        "progress; owners see a short summary of the ones the board shares.",
        {
            "capital_projects",
            "capital_project_detail",
            "capital_project_file",
            "community_projects",
        },
        default=False,
    ),
    # Budget and reserves (owner_budget, in ANY_OF): with this on, owners see
    # only operating vs reserve and budget vs actual there (D84).
    _f(
        "owner_finance_dashboard",
        "Owners' finance summary",
        "Budget and reserves shows owners just two things: operating vs reserve money, and "
        "the budget against what was actually spent this year.",
        set(),
        default=False,
    ),
    _f(
        "insurance_claims",
        "Insurance claims",
        "Claims on the association's insurance: dates, adjuster, deadlines, settlement "
        "approval and money received (board and manager).",
        {"insurance_claims", "insurance_claim_detail", "insurance_claim_file"},
        default=False,
    ),
    _f(
        "vendor_onboarding",
        "Vendor onboarding",
        "New vendors fill in an emailed form (W-9, insurance, licences, conflicts) and two board "
        "members approve them before they can be paid; documents are encrypted in the portal "
        "(needs the vendor document key on the server).",
        {
            # The public form (D79): the only pages besides sign-in without sign-in.
            "vendor_onboarding_start",
            "vendor_onboarding_form",
            "vendor_onboarding_upload",
            "vendor_onboarding_submit",
            "vendor_onboarding_done",
            # Board and manager pages.
            "vendor_onboarding_list",
            "vendor_onboarding_invite",
            "vendor_onboarding_review",
            "vendor_onboarding_action",
            "vendor_onboarding_file",
            "vendor_onboarding_vendor",
            "vendor_onboarding_vendor_action",
            "vendor_file_summary",
            "vendor_requirements",
            "vendor_w9_retention",
            "vendor_w9_disposal",
        },
        default=False,
    ),
]
BY_KEY = {f.key: f for f in FEATURES}
# Shared endpoints that act on another feature's records, by their "kind"
# URL argument (SEC-011): off with that feature.
SHARED_BY_KIND = {
    "thread_post": {
        "violation": "violations",
        "work_order": "work_orders",
        "committee_topic": "committees",
    },
    "follow_up_set": {"violation": "violations", "work_order": "work_orders"},
}
BY_URL = {name: f.key for f in FEATURES for name in f.url_names}
# Pages that show several features' records: on while any of them is on
# (the page itself leaves out the parts that are off).
ANY_OF = {
    "voting": frozenset({"votes", "elections", "surveys"}),
    # Budget and reserves (streamlining): the budget and the reserve summary.
    "owner_budget": frozenset({"owner_budget", "reserves", "owner_finance_dashboard"}),
}


def _load() -> dict[str, bool]:
    from core.models import FeatureSetting

    stored = dict(FeatureSetting.objects.values_list("key", "enabled"))
    return {f.key: stored.get(f.key, f.default) for f in FEATURES}


def states(request=None) -> dict[str, bool]:
    """Every feature's on/off; read once per request."""
    if request is not None:
        cached = getattr(request, "_feature_states", None)
        if cached is None:
            cached = request._feature_states = _load()
        return cached
    return _load()


def enabled(key: str, request=None) -> bool:
    return states(request).get(key, True)


def url_name_enabled(url_name: str | None, request=None) -> bool:
    if url_name in ANY_OF:
        return any(enabled(k, request) for k in ANY_OF[url_name])
    key = BY_URL.get(url_name or "")
    return key is None or enabled(key, request)


def path_enabled(path: str, request=None, states_: dict | None = None) -> bool:
    try:
        match = resolve(path.split("?", 1)[0])
    except Resolver404:
        return True
    key = BY_URL.get(match.url_name or "")
    if key is None:
        return True
    return (states_ if states_ is not None else states(request)).get(key, True)


class FeatureGateMiddleware:
    """Pages of a switched-off feature answer 404."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, "resolver_match", None)
        name = match.url_name if match else None
        if (name in BY_URL or name in ANY_OF) and not url_name_enabled(name, request):
            raise Http404
        feature = SHARED_BY_KIND.get(name, {}).get(view_kwargs.get("kind"))
        if feature and not enabled(feature, request):
            raise Http404


def set_feature(key: str, enabled_: bool, *, actor, request=None) -> None:
    from django.utils import timezone

    from audit.services import record_event
    from core.models import FeatureSetting
    from core.policy import SYSTEM, require

    require(actor, "features.manage")
    if key not in BY_KEY:
        raise ValueError("Unknown feature.")
    FeatureSetting.objects.update_or_create(
        key=key,
        defaults={
            "enabled": bool(enabled_),
            "updated_by": None if actor is SYSTEM else actor,
            "updated_at": timezone.now(),
        },
    )
    record_event(
        "feature.switched",
        actor=None if actor is SYSTEM else actor,
        request=request,
        metadata={"feature": key, "enabled": bool(enabled_)},
    )
    if request is not None and hasattr(request, "_feature_states"):
        del request._feature_states
