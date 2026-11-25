"""The sidebar: one definition, filtered per user by the same policy table
the views enforce. Roles are computed once per request here."""

from __future__ import annotations

from django.urls import NoReverseMatch, reverse

from core import policy
from core.my_items import MY_ITEMS_PERMISSIONS

# (label, url name, icon, permission or tuple of alternatives; None = everyone)
SECTIONS = [
    (
        "My home",
        [
            ("Home", "home", "home", None),
            (
                "My items",
                "my_items",
                "inbox",
                MY_ITEMS_PERMISSIONS,
            ),
            ("My lot", "my_lot", "pin", "lot.view_own"),
            ("My account", "my_account", "wallet", "ledger.view_own"),
            ("Architectural requests", "arc_list", "pencil", "arc.submit"),
            ("Notices about my lot", "violations", "flag", "lot.view_own"),
            ("My requests", "requests", "mail", "request.view_own"),
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
            ("Policies", "resolutions", "file", "resolution.view"),
            ("Owner directory", "directory", "users", "directory.view"),
        ],
    ),
    (
        "Board",
        [
            ("Board overview", "board_home", "sliders", ("person.manage", "task.manage")),
            ("Owner requests", "requests", "mail", "request.manage"),
            ("Approvals", "approvals", "check-circle", "role.sysadmin.approve"),
            ("People", "people", "users", "person.manage"),
            ("All lots", "lot_list", "map", "lot.view_all"),
            ("Architectural review", "arc_list", "pencil", "arc.view_all"),
            ("Violations", "violations", "flag", "violation.view_all"),
            ("Records requests", "records_requests", "inbox", "records_request.manage"),
            ("Tasks", "tasks", "list", "task.manage"),
            ("Community settings", "community_settings", "sliders", "lot.view_all"),
        ],
    ),
    (
        "Finance",
        [
            ("Owner accounts", "ledger_home", "wallet", "finance.view"),
            ("Invoices", "invoices", "receipt", "finance.view"),
            ("Work orders", "work_orders", "wrench", "finance.view"),
            ("Vendors", "vendors", "users", "finance.view"),
            ("Budgets", "budgets", "chart", "finance.view"),
            ("Collections", "collections", "dollar", "finance.view"),
            ("Resale certificates", "resales", "file", "resale.manage"),
            ("Reports", "reports", "chart", "reports.view"),
        ],
    ),
    (
        "Admin",
        [
            ("System status", "system_status", "activity", "system.view_status"),
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
    best = ("", None)  # longest matching URL wins the "current page" mark
    for title, items in SECTIONS:
        visible = []
        for label, name, icon, permission in items:
            if name in seen or not _allowed(roles.groups, permission):
                continue
            try:
                url = reverse(name)
            except NoReverseMatch:
                continue
            seen.add(name)
            item = {"label": label, "url": url, "icon": icon, "current": False}
            matches = (url == "/" and path == "/") or (url != "/" and path.startswith(url))
            if matches and len(url) > len(best[0]):
                best = (url, item)
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
