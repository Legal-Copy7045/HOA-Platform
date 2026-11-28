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
    failing = [r for r in health if not r.ok]
    return {
        # Each failing check with the button that fixes it (WT-010, D100).
        "failing": [(r, check_action(r.key)) for r in failing],
        "checks": len(health),
        "update": _update_status(),
    }


# Where a sysadmin fixes a failing health check: (button label, URL). Checks
# not listed open System status, which explains each one.
CHECK_ACTIONS = {
    "backups": ("Take a backup", "server_tasks", "#backups"),
    "email": ("Open Keys and connections", "keys_connections", ""),
    "email_delivery": ("Open Keys and connections", "keys_connections", ""),
    "bounce_mailbox": ("Open Keys and connections", "keys_connections", ""),
    "payments": ("Open Keys and connections", "keys_connections", ""),
    "bank_feed": ("Open Keys and connections", "keys_connections", ""),
    "worker": ("Open Server tasks", "server_tasks", ""),
    "jobs": ("Open Server tasks", "server_tasks", ""),
}


def check_action(key: str) -> dict:
    label, name, anchor = CHECK_ACTIONS.get(key, ("Open System status", "system_status", ""))
    return {"label": label, "url": reverse(name) + anchor}


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
    # Governing documents, My profile and Sign-in and security are in the
    # menu (Documents, the account links at its top) and the checklist, so
    # they are not repeated here (WT-018, D100).
    if roles.groups & policy.ACCOUNT_VIEWERS and _can(roles.groups, "arc.submit"):
        out.append(
            {
                "label": "My ARC requests",
                "url": reverse("arc_list") + "?mine=1",
                "icon": "pencil",
            }
        )
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


# How many to-dos the dashboard lists before "See all in My items": a short
# summary (the counts per group and the top few), never a copy of the My
# items page (D100, WT-001/WT-020). Owners, who have no My items page, see
# every to-do.
TODO_LIMIT = 5
# Roles whose work is the association's (D64): with a home as well, the
# dashboard has two headed parts, "Your home" and "Board work".
WORK_GROUPS = frozenset({policy.BOARD, policy.MANAGER, policy.AUDITOR, policy.ARC_REVIEWER})
# Quick actions that are board work; the rest are the person's own.
BOARD_QUICK = {"record_payments"}
# Per part of a board member's dashboard (D100): the top three.
PART_LIMIT = 3


def group_links(items, part_key: str) -> list[dict]:
    """One link per My items group with its count and overdue count, to that
    group's card on the My items page (its id is area-<part>-<group>)."""
    from django.utils.text import slugify

    from core.my_items import counts

    base = reverse("my_items")
    return [
        # Set-up warnings are alerts at the top of My items, not a card.
        {
            **c,
            "url": base if c["area"] == "Setup" else f"{base}#area-{part_key}-{slugify(c['area'])}",
        }
        for c in counts(items)
    ]


def parts(items, quick, can_open_queue, groups=frozenset({policy.BOARD})) -> list[dict]:
    """The dashboard of someone with a home and board work, in two parts:
    "Board work" first when anything waits there (WT-040), "Your home"
    otherwise first. Each holds its to-dos (overdue first) and its quick
    actions; nothing is dropped, the rest is one tap away in My items."""
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
            "total": len(todos),
            # Only staff can open the My items page (owners see every to-do).
            "queue": can_open_queue,
            "quick": quick_links,
            # The summary line: counts per group, each a jump into My items.
            "groups": group_links(todos, key) if can_open_queue else [],
        }

    board = part(
        "board",
        # The badge before it carries the label (dash_parts.html), so the
        # heading reads "Board work" once, never "Staff Staff work" (WT-006).
        "work",
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
    # Overdue first, then legal deadlines, then dated items (WT-023): a
    # shortened list never leaves out a records request with a legal due date.
    items = sorted(items, key=my_items.priority)
    # Getting started (D73): the card replaces its own My items entry here.
    from community import checklist

    data["checklist"] = checklist.for_dashboard(user, request)
    if data["checklist"] is not None:
        started = reverse("getting_started")
        items = [i for i in items if i.url != started]
    can_open_queue = any(policy.can(user, p) for p in my_items.MY_ITEMS_PERMISSIONS)
    data["todos"] = items[:TODO_LIMIT] if can_open_queue else items
    data["todos_more"] = len(items) - len(data["todos"])
    data["todo_groups"] = group_links(items, "all") if can_open_queue else []
    data["can_open_queue"] = can_open_queue
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
    if not [i for i in items if i.area != "Sign-in and security"] and "system" not in data:
        # A sysadmin-only login has the System status card with a button per
        # failing check instead of a list of its menu entries (WT-010).
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
