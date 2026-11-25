"""What each person sees on their home page: their numbers and their to-dos.

Everything goes through the same permission table and scoped managers as the
pages it links to, so a dashboard never shows more than the user could open.
The to-dos are core.my_items (the same list as My items, the Board overview
counts and the sidebar bubbles), so the three can never disagree.
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
    from arc.models import ArcRequest
    from community.models import Lot
    from compliance.models import Violation
    from core.scoping import own_records
    from governance.models import Vote
    from governance.services import ballot_choices_for
    from ledger.services import owner_statement

    accounts = []
    since = roles.lot_since
    for lot in Lot.objects.for_user(user).filter(pk__in=roles.account_lot_ids):
        start = since.get(lot.pk)
        accounts.append({"lot": lot, "balance": owner_statement(lot.pk, start).balance})
    open_arc = (
        ArcRequest.objects.for_user(user)
        .filter(lot_id__in=roles.account_lot_ids)
        .exclude(state__in=["completed", "withdrawn", "denied", "appeal_decided"])
        .count()
    )
    notices = own_records(Violation.objects.for_user(user), user).filter(
        state__in=["noticed", "responded", "hearing"]
    )
    votes = [
        v for v in Vote.objects.filter(status="open") if ballot_choices_for(v, user)["can_vote"]
    ]
    return {
        "accounts": accounts,
        "open_arc": open_arc,
        "notices": notices.count(),
        "votes": len(votes),
        "proxy_meetings": proxy_meetings(user),
    }


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
                "hint": f"{_n(behind, 'lot', 'lots')} with a balance",
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
    return data
