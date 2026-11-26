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
        "Votes and polls",
        "Informal polls and formal votes of the owners.",
        {"votes", "vote_detail", "vote_action"},
    ),
    _f(
        "elections",
        "Elections and proxies",
        "Board elections, nominations, proxies.",
        {"elections", "election_detail", "election_action", "proxy_grant", "proxy_revoke"},
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
        "Board resolutions adopted by unanimous written consent.",
        {"consents", "consent_detail"},
    ),
    _f(
        "records_requests",
        "Records requests",
        "Owners' requests to inspect association records.",
        {"records_requests", "records_request_detail", "records_request_action"},
    ),
    _f(
        "owner_budget", "Budget for owners", "The adopted budget shown to owners.", {"owner_budget"}
    ),
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
            "pay_online",
            "pay_checkout",
            "pay_return",
            "payment_receipt",
            "online_payments",
            "payment_alert_resolve",
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
ANY_OF = {"voting": frozenset({"votes", "elections", "surveys"})}


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
