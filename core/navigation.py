"""The sidebar: one definition, filtered per user by the same policy table
the views enforce. Roles are computed once per request here."""

from __future__ import annotations

from django.urls import NoReverseMatch, reverse

from core import features, policy
from core.my_items import MY_ITEMS_PERMISSIONS

# (label, url name, icon, permission or tuple of alternatives; None = everyone)
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
            ("My architectural requests", "arc_list?mine=1", "pencil", "arc.submit"),
            ("Neighbor acknowledgements", "signoffs_mine", "check-circle", "arc.submit"),
            ("Notices about my home", "violations?mine=1", "flag", "lot.view_own"),
            ("My requests", "requests?mine=1", "mail", "request.view_own"),
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
            ("Votes and polls", "votes", "vote", "vote.view"),
            ("Elections", "elections", "users", "vote.view"),
            ("Policies", "resolutions", "file", "resolution.view"),
            ("Budget", "owner_budget", "chart", "budget.view_owner"),
            ("FAQ", "faq", "info", "announcement.view"),
            ("Directory", "directory", "users", "directory.view"),
            ("Committees", "committees", "users", "committee.view"),
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
            ("All lots", "lot_list", "map", "lot.view_all"),
            ("Roster", "roster", "list", "roster.view"),
            ("Architectural review", "arc_list", "pencil", "arc.view_all"),
            ("Violations", "violations", "flag", "violation.view_all"),
            ("Records requests", "records_requests", "inbox", "records_request.manage"),
            ("Tasks", "tasks", "list", "task.manage"),
            ("Urgent texts", "urgent_notice", "megaphone", "sms.send_urgent"),
            ("Compliance calendar", "compliance", "calendar", "compliance.manage"),
            ("Community settings", "community_settings", "sliders", "lot.view_all"),
            ("ARC request types", "arc_project_types", "pencil", "arc.configure"),
            ("Lot neighbors", "arc_neighbors", "map", "arc.configure"),
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
            ("Letters and certificates", "letters_settings", "mail", "letters.manage"),
            ("Accounting", "accounting", "receipt", "finance.view"),
            ("Resale certificates", "resales", "file", "resale.manage"),
            ("Reports", "reports", "chart", "reports.view"),
        ],
    ),
    (
        "Admin",
        [
            ("System status", "system_status", "activity", "system.view_status"),
            ("Features", "feature_settings", "sliders", "features.manage"),
            ("Branding", "branding_settings", "sliders", "branding.manage"),
            ("Invitations", "invitations", "mail", "invite.resend"),
            ("Sysadmin activity", "sysadmin_activity", "shield", "audit.view_sysadmin_activity"),
        ],
    ),
]

ROLE_LABELS = {
    policy.OWNER: "Owner",
    policy.BOARD: "Board",
    policy.MANAGER: "Manager",
    policy.ARC_REVIEWER: "ARC",
    policy.AUDITOR: "Auditor",
    policy.SYSADMIN: "Sysadmin",
}


def _allowed(groups: frozenset[str], permission) -> bool:
    if permission is None:
        return True
    options = permission if isinstance(permission, tuple) else (permission,)
    return any(policy.PERMISSIONS[p] & groups for p in options)


def build(request) -> dict:
    user = request.user
    roles = policy.roles_for(user)
    path = request.path
    seen: set[str] = set()
    sections = []
    from core.my_items import nav_counts

    try:
        counts = nav_counts(user) if user.is_authenticated else {}
    except Exception:  # a count must never break the page
        counts = {}
    best = ("", None)  # longest matching URL wins the "current page" mark
    for title, items in SECTIONS:
        visible = []
        for label, name, icon, permission in items:
            if name in seen or not _allowed(roles.groups, permission):
                continue
            url_name, _, query = name.partition("?")
            if not features.url_name_enabled(url_name, request):
                continue
            try:
                url = reverse(url_name) + (f"?{query}" if query else "")
            except NoReverseMatch:
                continue
            seen.add(name)
            item = {
                "label": label,
                "url": url,
                "icon": icon,
                "current": False,
                "count": counts.get(name, 0),
            }
            base, _, query = url.partition("?")
            here_mine = request.GET.get("mine") == "1"
            matches = (base == "/" and path == "/") or (
                base != "/" and path.startswith(base) and (("mine=1" in query) == here_mine)
            )
            if matches and len(base) > len(best[0]):
                best = (base, item)
            visible.append(item)
        if visible:
            sections.append({"title": title, "items": visible})
    if best[1]:
        best[1]["current"] = True
    return {
        "sections": sections,
        "roles": [ROLE_LABELS[g] for g in ROLE_LABELS if g in roles.groups],
        "person_id": roles.person_id,
    }
