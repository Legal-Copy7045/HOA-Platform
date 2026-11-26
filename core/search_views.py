"""The search page (core.search). The query is never logged or audited."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from core import search


@never_cache
@require_safe
@login_required
def search_page(request):
    q = search.clean_query(request.GET.get("q"))
    problem = search.query_problem(q) if q else None
    groups = search.search(request.user, q, request) if q and not problem else []
    response = render(
        request,
        "core/search.html",
        {
            "q": q[: search.MAX_LENGTH * 2],
            "problem": problem,
            "groups": groups,
            "total": sum(len(g.results) for g in groups),
            "more": any(g.more for g in groups),
            "min_length": search.MIN_LENGTH,
            "max_length": search.MAX_LENGTH,
            "per_area": search.PER_AREA,
        },
    )
    response["Cache-Control"] = "no-store, private"
    return response
