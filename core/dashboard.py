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
    # "Record payments received" is also offered on My items ({% quick_actions %}).
    if _can(roles.groups, "ledger.manage"):
        out.append(
            {
                "label": "Record payments received",
                "url": reverse("record_payments"),
                "icon": "receipt",
            }
        )
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
# Roles whose work is the association's (D64): with a home as well, the
# dashboard has two headed parts, "Your home" and "Board work".
WORK_GROUPS = frozenset({policy.BOARD, policy.MANAGER, policy.AUDITOR, policy.ARC_REVIEWER})
# Quick actions that are board work; the rest are the person's own.
BOARD_QUICK = {"record_payments"}
# Per part, as on the single list (phones show the first four of each).
PART_LIMIT = 6


def parts(items, quick, can_open_queue, groups=frozenset({policy.BOARD})) -> list[dict]:
    """The dashboard of someone with a home and board work, in two parts:
    "Board work" first when anything waits there (WT-040), "Your home"
    otherwise first. Each holds its to-dos (overdue first) and its quick
    actions; nothing is dropped, the rest is one tap away in My items."""
    from core import navigation
    from core.my_items import HOME

    board_url = {reverse(n) for n in BOARD_QUICK}
    home_items = [i for i in items if i.side == HOME]
    board_items = [i for i in items if i.side != HOME]

    def part(key, title, todos, quick_links):
        shown = todos[:PART_LIMIT] if can_open_queue else todos
        return {
            "key": key,
            "title": title,
            "todos": shown,
            "more": len(todos) - len(shown),
            "quick": quick_links,
        }

    board = part(
        "board",
        f"{navigation.work_label(groups)} work",
        board_items,
        [q for q in quick if q["url"] in board_url],
    )
    home = part(
        "home",
        "Your home",
        home_items,
        [q for q in quick if q["url"] not in board_url],
    )
    return [board, home] if board_items else [home, board]


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
    if (roles.groups & policy.RESIDENTS) and (roles.groups & WORK_GROUPS):
        data["parts"] = parts(items, data["quick"], can_open_queue, roles.groups)
    # A sign-in to-do (backup method, new recovery codes, D58) is the
    # login's own, not role work: the role shortcuts still show beside it.
    if not [i for i in items if i.area != "Sign-in and security"]:
        data["shortcuts"] = role_shortcuts(user, roles, request)
    from comms.models import Announcement, AnnouncementRead
    from comms.services import upcoming, with_page_urls

    data["news"], data["events"] = [], []
    if policy.can(user, "announcement.view"):
        from community.models import CommunityProfile

        data["welcome"] = CommunityProfile.get().welcome_message
    if policy.can(user, "announcement.view"):
        read = set(
            AnnouncementRead.objects.filter(user=user).values_list("announcement_id", flat=True)
        )
        data["news"] = [(a, a.pk in read) for a in Announcement.objects.for_user(user)[:4]]
        data["events"] = with_page_urls(upcoming(user)[:4], user)
    if policy.can(user, "calendar.view"):
        from comms.rsvp import my_upcoming

        data["going"] = my_upcoming(user)
    return data
