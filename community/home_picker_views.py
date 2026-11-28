"""Suggestions for the home picker (D96): signed in, capped, never logged."""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from community import home_picker


@login_required
@require_GET
def home_suggest(request):
    """Up to 10 homes matching ``q`` (2+ characters): address and current
    owners, public within the community (owner's decision, D96). Nothing
    else about a home: no balances, contacts, documents or requests. The
    query is never logged or audited, like portal search."""
    if not home_picker.may_suggest(request.user):
        results, status = [], 200
    elif home_picker.rate_limited(request.user):
        results, status = [], 429
    else:
        results, status = home_picker.suggestions(request.GET.get("q", "")), 200
    response = JsonResponse({"results": results}, status=status)
    response["Cache-Control"] = "no-store"
    return response
