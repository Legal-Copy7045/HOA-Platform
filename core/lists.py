"""List tools shared by list pages: paging (and, later, export)."""

from __future__ import annotations

from django.core.paginator import Paginator

PAGE_SIZE = 25


def paginate(request, queryset, per_page: int = PAGE_SIZE):
    """A page of ``queryset`` from ``?page=``; bad numbers give the last or
    first page rather than an error."""
    return Paginator(queryset, per_page).get_page(request.GET.get("page"))
