from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core import my_items, policy


def _parts(user, items) -> list[dict]:
    """My items for someone with a home and board (or committee) work, in the
    dashboard's two parts (WT-003, D64): "Board work" first when anything
    waits there, then "Your home". Everyone else keeps one list (one part
    with no heading)."""
    from core.dashboard import WORK_GROUPS

    if not items:
        return []
    groups = policy.roles_for(user).groups
    if not (groups & policy.RESIDENTS and groups & WORK_GROUPS):
        return [{"key": "all", "title": "", "items": items, "counts": my_items.counts(items)}]
    board = [i for i in items if i.side != my_items.HOME]
    home = [i for i in items if i.side == my_items.HOME]
    parts = [
        {"key": "board", "title": "work", "items": board, "counts": my_items.counts(board)},
        {"key": "home", "title": "Your home", "items": home, "counts": my_items.counts(home)},
    ]
    if not board:
        parts.reverse()
    return [p for p in parts if p["items"]]


@login_required
def my_items_page(request):
    if not any(policy.can(request.user, p) for p in my_items.MY_ITEMS_PERMISSIONS):
        policy.require(request.user, my_items.MY_ITEMS_PERMISSIONS[0])
    items = my_items.items_for_request(request)
    # Set-up warnings (what the Board overview used to show) go first, as
    # alerts: two-person coverage, board terms past their end, rules missing
    # a value or citation.
    alerts = [i for i in items if i.area == "Setup"]
    rest = [i for i in items if i.area != "Setup"]
    parts = _parts(request.user, rest)
    return render(
        request,
        "core/my_items.html",
        {
            "items": rest,
            "parts": parts,
            "split": any(p["title"] for p in parts),
            "alerts": alerts,
            "counts": my_items.counts(rest),
            # Which actions fewer than two people can take (board only).
            "thin": policy.thinly_covered_actions()
            if policy.can(request.user, "community.manage_rules")
            else {},
        },
    )
