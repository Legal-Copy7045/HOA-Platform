"""The sidebar: one definition, filtered per user by the same policy table
the views enforce. Roles are computed once per request here.

Rarely used pages live on hub pages (HUBS) instead of the sidebar: the hub's
entry is shown when at least one of its pages is open to the user (and its
feature is on), counts the hub's waiting items, and is marked current on any
of its pages. Labels match the pages' <h1> (docs/glossary.md).
"""

from __future__ import annotations

from django.urls import NoReverseMatch, reverse

from core import features, policy
from core.my_items import MY_ITEMS_PERMISSIONS

# Hub pages: url name -> [(label, url name, icon, permission, description)].
HUBS = {
    "voting": [
        (
            "Votes and polls",
            "votes",
            "vote",
            "vote.view",
            "Formal votes of the owners and informal polls.",
        ),
        (
            "Elections",
            "elections",
            "users",
            "vote.view",
            "Board elections: nominations, candidates, ballots and proxies.",
        ),
    ],
    "community_info": [
        (
            "Frequently asked questions",
            "faq",
            "info",
            "announcement.view",
            "Quick answers about living here.",
        ),
        (
            "Owner directory",
            "directory",
            "users",
            "directory.view",
            "Neighbors who chose to share their contact details.",
        ),
        (
            "Committees",
            "committees",
            "users",
            "committee.view",
            "Committee members, topics, tasks and files.",
        ),
    ],
    "settings_hub": [
        (
            "Community settings",
            "community_settings",
            "sliders",
            "lot.view_all",
            "Dues, deadlines and voting rules from the Declaration, each with its citation.",
        ),
        (
            "ARC request types",
            "arc_project_types",
            "pencil",
            "arc.configure",
            "Kinds of home improvement project and what each must include.",
        ),
        (
            "Lot neighbors",
            "arc_neighbors",
            "map",
            "arc.configure",
            "Which lots are asked to acknowledge a neighbor's project.",
        ),
        (
            "Letters and certificates",
            "letters_settings",
            "mail",
            "letters.manage",
            "Wording of collection letters and resale certificates, and where to send checks.",
        ),
        (
            "Compliance calendar",
            "compliance",
            "calendar",
            "compliance.manage",
            "Filings and renewals the association must not miss.",
        ),
        (
            "Branding",
            "branding_settings",
            "sliders",
            "branding.manage",
            "The sign-in page picture.",
        ),
        (
            "Features",
            "feature_settings",
            "sliders",
            "features.manage",
            "Switch optional parts of the portal on or off.",
        ),
        (
            "Access review",
            "access_review",
            "shield",
            "access_review.record",
            "Every login's access, reviewed by the board each quarter.",
        ),
        (
            "Contact retention",
            "retention",
            "clock",
            "retention.manage",
            "Former owners' contact details cleared after the retention period.",
        ),
        (
            "Common area booking rules",
            "reservation_settings",
            "calendar",
            "reservation.configure",
            "Which common areas can be booked, and their rules.",
        ),
    ],
}

# (label, url name, icon, permission or tuple of alternatives; None = everyone).
# A hub's own entry takes its visibility from its pages (permission HUB).
HUB = "hub"
SECTIONS = [
    (
        "",
        [
            ("Dashboard", "home", "home", None),
            ("My items", "my_items", "inbox", MY_ITEMS_PERMISSIONS),
        ],
    ),
    (
        "Homeowner",
        [
            ("My home", "my_lot", "pin", "lot.view_own"),
            ("My account", "my_account", "wallet", "ledger.view_own"),
            ("Home improvement approvals (ARC)", "arc_list?mine=1", "pencil", "arc.submit"),
            ("Neighbor acknowledgements", "signoffs_mine", "check-circle", "arc.submit"),
            ("Notices about my home", "violations?mine=1", "flag", "lot.view_own"),
            ("My requests", "requests?mine=1", "mail", "request.view_own"),
            ("Records requests", "records_requests?mine=1", "inbox", "records_request.submit"),
            ("My history", "my_history", "clock", "history.view_own"),
        ],
    ),
    (
        "Community",
        [
            ("Announcements", "announcements", "megaphone", "announcement.view"),
            ("Calendar", "calendar", "calendar", "calendar.view"),
            ("Documents", "documents", "folder", "document.view_owner"),
            ("Meetings", "meetings", "book", "meeting.view"),
            ("Voting", "voting", "vote", HUB),
            ("Resolutions and policies", "resolutions", "file", "resolution.view"),
            ("Budget", "owner_budget", "chart", "budget.view_owner"),
            ("Community info", "community_info", "info", HUB),
            ("Common area bookings", "reservations", "calendar", "reservation.view"),
        ],
    ),
    (
        "Board",
        [
            ("Board overview", "board_home", "sliders", ("person.manage", "task.manage")),
            ("Owner requests", "requests", "mail", "request.manage"),
            ("Approvals", "approvals", "check-circle", "role.sysadmin.approve"),
            ("Written consents", "consents", "file", "resolution.record"),
            ("People", "people", "users", "person.manage"),
            ("Onboarding", "onboarding", "mail", "invite.send"),
            ("Lots", "lot_list", "map", "lot.view_all"),
            ("Roster", "roster", "list", "roster.view"),
            ("Architectural review", "arc_list", "pencil", "arc.view_all"),
            ("Violations", "violations", "flag", "violation.view_all"),
            ("Records requests", "records_requests", "inbox", "records_request.manage"),
            ("Board tasks", "tasks", "list", "task.manage"),
            ("Urgent texts", "urgent_notice", "megaphone", "sms.send_urgent"),
        ],
    ),
    (
        "Finance",
        [
            ("Owner accounts", "ledger_home", "wallet", "finance.view"),
            ("Invoices", "invoices", "receipt", "finance.view"),
            ("Contracts", "contracts", "file", "finance.view"),
            ("Work orders", "work_orders", "wrench", "finance.view"),
            ("Vendors", "vendors", "users", "finance.view"),
            ("Budgets", "budgets", "chart", "finance.view"),
            ("Collections", "collections", "dollar", "finance.view"),
            ("Accounting", "accounting", "receipt", "finance.view"),
            ("Resale certificates", "resales", "file", "resale.manage"),
            ("Reports", "reports", "chart", "reports.view"),
        ],
    ),
    (
        "Admin",
        [
            ("System status", "system_status", "activity", "system.view_status"),
            ("Invitations", "invitations", "mail", "invite.resend"),
            ("Sysadmin activity", "sysadmin_activity", "shield", "audit.view_sysadmin_activity"),
            ("Settings", "settings_hub", "sliders", HUB),
        ],
    ),
]
# Sections open by default; the others open when the current page is inside
# (and the browser remembers what the person last chose, static/js/app.js).
OPEN_BY_DEFAULT = {"Homeowner", "Community"}

ROLE_LABELS = {
    policy.OWNER: "Owner",
    policy.BOARD: "Board",
    policy.MANAGER: "Manager",
    policy.ARC_REVIEWER: "ARC",
    policy.AUDITOR: "Auditor",
    policy.SYSADMIN: "Sysadmin",
}


def allowed(groups: frozenset[str], permission) -> bool:
    if permission is None:
        return True
    options = permission if isinstance(permission, tuple) else (permission,)
    return any(policy.PERMISSIONS[p] & groups for p in options)


def _url(name: str) -> str | None:
    url_name, _, query = name.partition("?")
    try:
        return reverse(url_name) + (f"?{query}" if query else "")
    except NoReverseMatch:
        return None


def hub_links(request, hub: str, groups: frozenset[str] | None = None) -> list[dict]:
    """The pages of a hub this person may open, with their features on."""
    if groups is None:
        groups = policy.roles_for(request.user).groups
    out = []
    for label, name, icon, permission, description in HUBS[hub]:
        if not allowed(groups, permission) or not features.url_name_enabled(name, request):
            continue
        url = _url(name)
        if url:
            out.append(
                {"label": label, "name": name, "url": url, "icon": icon, "text": description}
            )
    return out


def _matches(url: str, path: str, here_mine: bool) -> bool:
    base, _, query = url.partition("?")
    if base == "/":
        return path == "/"
    return path.startswith(base) and (("mine=1" in query) == here_mine)


def build(request) -> dict:
    user = request.user
    roles = policy.roles_for(user)
    path = request.path
    here_mine = request.GET.get("mine") == "1"
    seen: set[str] = set()
    sections = []
    from core.my_items import nav_counts

    try:
        counts = nav_counts(user, request) if user.is_authenticated else {}
    except Exception:  # a count must never break the page
        counts = {}
    best = ("", None, None)  # longest matching URL wins the "current page" mark
    for title, items in SECTIONS:
        visible = []
        for label, name, icon, permission in items:
            if name in seen:
                continue
            if permission == HUB:
                children = hub_links(request, name, roles.groups)
                if not children:
                    continue
            else:
                children = []
                if not allowed(roles.groups, permission):
                    continue
            if not features.url_name_enabled(name.partition("?")[0], request):
                continue
            url = _url(name)
            if url is None:
                continue
            seen.add(name)
            item = {
                "label": label,
                "url": url,
                "icon": icon,
                "current": False,
                "count": counts.get(name, 0) + sum(counts.get(c["name"], 0) for c in children),
            }
            for candidate in [url] + [c["url"] for c in children]:
                base = candidate.partition("?")[0]
                if _matches(candidate, path, here_mine) and len(base) > len(best[0]):
                    best = (base, item, len(sections))
            visible.append(item)
        if visible:
            sections.append(
                {
                    "title": title,
                    "key": title.lower(),
                    "items": visible,
                    "open": title in OPEN_BY_DEFAULT,
                    "count": sum(i["count"] for i in visible),
                }
            )
    if best[1]:
        best[1]["current"] = True
        sections[best[2]]["open"] = True
        sections[best[2]]["current"] = True
    return {
        "sections": sections,
        "roles": [ROLE_LABELS[g] for g in ROLE_LABELS if g in roles.groups],
        "person_id": roles.person_id,
    }
