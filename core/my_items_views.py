from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core import my_items, policy

# A long queue stays short on a phone (D100, WT-020): each group shows its
# first few items with "Show N more" for the rest, and only the first groups
# of each part (and any with overdue items) start open; every group's heading
# keeps its count, so nothing is out of reach.
GROUP_SHOWN = 4
GROUPS_OPEN = 2


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


def _groups(part: dict) -> list[dict]:
    """The part's items by group (in My items' group order), folded."""
    from django.utils.text import slugify

    by_area: dict[str, list] = {}
    for item in part["items"]:
        by_area.setdefault(item.area, []).append(item)
    out = []
    for n, row in enumerate(part["counts"]):
        items = by_area[row["area"]]
        out.append(
            {
                "area": row["area"],
                "id": f"area-{part['key']}-{slugify(row['area'])}",
                "count": row["count"],
                "overdue": row["overdue"],
                "shown": items[:GROUP_SHOWN],
                "rest": items[GROUP_SHOWN:],
                "open": n < GROUPS_OPEN or bool(row["overdue"]),
            }
        )
    return out


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
    overdue_count = sum(1 for i in rest if i.overdue)
    # "Overdue only" (D100): the same page, the overdue items alone.
    only_overdue = request.GET.get("show") == "overdue"
    shown = [i for i in rest if i.overdue] if only_overdue else rest
    parts = _parts(request.user, shown)
    for part in parts:
        part["groups"] = _groups(part)
    return render(
        request,
        "core/my_items.html",
        {
            "items": rest,
            "parts": parts,
            "split": any(p["title"] for p in parts),
            "alerts": alerts,
            "counts": my_items.counts(shown),
            "total": len(rest),
            "overdue_count": overdue_count,
            "only_overdue": only_overdue,
            # Which actions fewer than two people can take (board only).
            "thin": policy.thinly_covered_actions()
            if policy.can(request.user, "community.manage_rules")
            else {},
        },
    )
