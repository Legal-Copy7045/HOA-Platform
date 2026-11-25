from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core import my_items, policy


@login_required
def my_items_page(request):
    if not any(policy.can(request.user, p) for p in my_items.MY_ITEMS_PERMISSIONS):
        policy.require(request.user, my_items.MY_ITEMS_PERMISSIONS[0])
    items = my_items.items_for_request(request)
    return render(
        request,
        "core/my_items.html",
        {"items": items, "counts": my_items.counts(items)},
    )
