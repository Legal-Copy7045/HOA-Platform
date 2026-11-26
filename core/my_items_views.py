from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core import my_items, policy


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
    return render(
        request,
        "core/my_items.html",
        {
            "items": rest,
            "alerts": alerts,
            "counts": my_items.counts(rest),
            # Which actions fewer than two people can take (board only).
            "thin": policy.thinly_covered_actions()
            if policy.can(request.user, "community.manage_rules")
            else {},
        },
    )
