"""What each person sees on their home page: their numbers and their to-dos.

Everything goes through the same permission table and scoped managers as the
pages it links to, so a dashboard never shows more than the user could open.
The to-dos are core.my_items (the same list as My items and the sidebar
bubbles), so they can never disagree.
"""

from __future__ import annotations

from django.urls import reverse
from django.utils import timezone

from core import policy


def _can(groups, action) -> bool:
    return bool(policy.PERMISSIONS[action] & groups)


def _n(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def owner_panel(user, roles) -> dict:
    """The balance tile per account. Open votes, ARC requests and notices are
    to-dos already, so they have no tiles; "Pay $X" is on the balance to-do
    (core.my_items)."""
    from community.models import Lot
    from ledger.services import owner_statement

    accounts = []
    since = roles.lot_since
    for lot in Lot.objects.for_user(user).filter(pk__in=roles.account_lot_ids):
        accounts.append({"lot": lot, "balance": owner_statement(lot.pk, since.get(lot.pk)).balance})
    return {"accounts": accounts, "proxy_meetings": proxy_meetings(user)}


def system_panel() -> dict:
    """A sysadmin-only login's home: the System status summary (health only,
    never owner data)."""
    from core.health import run_checks
    from core.views import _update_status

    health = run_checks()
    return {
        "failing": [r for r in health if not r.ok],
        "checks": len(health),
        "update": _update_status(),
    }


# Roles that are never assigned anything (no My items): their dashboard
# shows the pages they use most instead of an always-empty to-do card (WT-016).
ROLE_SHORTCUTS = {
    policy.AUDITOR: ["reports", "accounting", "sysadmin_activity", "community_settings"],
    policy.SYSADMIN: ["system_status", "server_tasks", "sysadmin_activity"],
}


def role_shortcuts(user, roles, request=None) -> list[dict]:
    """Shortcuts for a login whose roles never receive to-dos (auditor,
    sysadmin), or [] for anyone who can (staff with a queue, residents,
    former owners)."""
    from core import navigation
    from core.my_items import MY_ITEMS_PERMISSIONS

    if any(policy.can(user, p) for p in MY_ITEMS_PERMISSIONS):
        return []
    if roles.groups & (policy.RESIDENTS | {policy.FORMER_OWNER, policy.BOARD}):
        return []
    names: list[str] = []
    for group, pages in ROLE_SHORTCUTS.items():
        if group in roles.groups:
            names += [n for n in pages if n not in names]
    return navigation.shortcuts(request, names, roles.groups) if names else []


def quick_actions(user, roles) -> list[dict]:
    out = []
    # "Record checks" is also offered on My items ({% quick_actions %}).
    if _can(roles.groups, "ledger.manage"):
        out.append({"label": "Record checks", "url": reverse("record_payments"), "icon": "receipt"})
    if _can(roles.groups, "request.submit"):
        out.append(
            {
                "label": "Ask the board a question",
                "url": reverse("requests") + "?mine=1&new=1#new-request",
                "icon": "mail",
            }
        )
    if roles.groups & policy.ACCOUNT_VIEWERS:
        if _can(roles.groups, "arc.submit"):
            out.append(
                {
                    "label": "My home improvement requests (ARC)",
                    "url": reverse("arc_list") + "?mine=1",
                    "icon": "pencil",
                }
            )
        out += [
            {"label": "Governing documents", "url": reverse("documents"), "icon": "folder"},
            {"label": "Update my contact details", "url": reverse("my_profile"), "icon": "user"},
            {"label": "Sign-in and security", "url": reverse("mfa_index"), "icon": "lock"},
        ]
    return out


def proxy_meetings(user) -> list:
    """Coming meetings where owners vote (an election is attached), so the
    dashboard can offer "Can't attend? Give a proxy"."""
    if not policy.can(user, "vote.cast"):
        return []
    from governance.models import Election, ElectionState, Meeting

    ids = Election.objects.filter(
        state__in=[ElectionState.NOMINATIONS, ElectionState.VOTING],
        meeting__isnull=False,
    ).values_list("meeting_id", flat=True)
    return list(
        Meeting.objects.for_user(user)
        .filter(pk__in=ids, status="scheduled", starts_at__gte=timezone.now())
        .order_by("starts_at")[:2]
    )


def staff_panel(user, roles) -> dict:
    from ledger.models import CollectionCase
    from ledger.services import balances
    from operations.models import Invoice, WorkOrder

    g = roles.groups
    stats = []

    if _can(g, "finance.view"):
        bal = balances()
        owed = sum(v for v in bal.values() if v > 0)
        behind = sum(1 for v in bal.values() if v > 0)
        stats.append(
            {
                "label": "Owed by owners",
                "value": owed,
                "money": True,
                "hint": f"{_n(behind, 'home', 'homes')} with a balance",
                "url": reverse("ledger_home"),
                "tone": "warning" if owed else "success",
            }
        )
        stats.append(
            {
                "label": "Invoices awaiting approval",
                "value": Invoice.objects.filter(state="pending_approval").count(),
                "url": reverse("invoices") + "?state=pending_approval",
            }
        )
        stats.append(
            {
                "label": "Open work orders",
                "value": WorkOrder.objects.exclude(state__in=["closed", "cancelled"]).count(),
                "url": reverse("work_orders"),
            }
        )
        stats.append(
            {
                "label": "Collection cases",
                "value": CollectionCase.objects.for_user(user).exclude(state="closed").count(),
                "url": reverse("collections"),
            }
        )
    return {"stats": stats}


# How many to-dos the dashboard lists before "See all in My items".
TODO_LIMIT = 8


def build(user, request=None) -> dict:
    from core import my_items

    roles = policy.roles_for(user)
    data = {"roles": roles}
    items = my_items.items_for_request(request) if request else my_items.items_for(user)
    items = sorted(items, key=lambda i: not i.overdue)  # overdue first, then by area
    can_open_queue = any(policy.can(user, p) for p in my_items.MY_ITEMS_PERMISSIONS)
    data["todos"] = items[:TODO_LIMIT] if can_open_queue else items
    data["todos_more"] = len(items) - len(data["todos"])
    if roles.groups & policy.ACCOUNT_VIEWERS:
        data["owner"] = owner_panel(user, roles)
    if roles.groups & {policy.BOARD, policy.MANAGER, policy.AUDITOR, policy.ARC_REVIEWER}:
        data["staff"] = staff_panel(user, roles)
    if roles.groups == {policy.SYSADMIN}:  # sysadmin-only login: operations first
        data["system"] = system_panel()
    data["quick"] = quick_actions(user, roles)
    if not items:
        data["shortcuts"] = role_shortcuts(user, roles, request)
    from comms.models import Announcement, AnnouncementRead
    from comms.services import upcoming

    data["news"], data["events"] = [], []
    if policy.can(user, "announcement.view"):
        from community.models import CommunityProfile

        data["welcome"] = CommunityProfile.get().welcome_message
    if policy.can(user, "announcement.view"):
        read = set(
            AnnouncementRead.objects.filter(user=user).values_list("announcement_id", flat=True)
        )
        data["news"] = [(a, a.pk in read) for a in Announcement.objects.for_user(user)[:4]]
        data["events"] = list(upcoming(user)[:4])
    if policy.can(user, "calendar.view"):
        from comms.rsvp import my_upcoming

        data["going"] = my_upcoming(user)
    return data
