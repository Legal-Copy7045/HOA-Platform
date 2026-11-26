"""Search provider: the document library (core.search).

Same permission, queryset and matching as the library page
(documents.views.library and services.search_documents).
"""

from django.urls import reverse

from core import policy
from core.search import LIMIT, Result, register, snippet


@register("documents", "Documents", url_name="documents")
def documents(user, q):
    from documents import services
    from documents.models import Document

    if not policy.can(user, "document.view_owner"):
        return []
    rows = Document.objects.for_user(user).select_related("folder")
    if not policy.can(user, "document.manage"):
        rows = rows.filter(archived=False)
    rows = services.search_documents(rows, q).order_by("-created_at", "-pk")[:LIMIT]
    return [
        Result(
            title=d.title,
            url=reverse("document_detail", args=[d.pk]),
            snippet=snippet(q, d.description, d.folder.name if d.folder_id else ""),
            date=d.created_at,
            detail=d.get_category_display() + (" · archived" if d.archived else ""),
        )
        for d in rows
    ]
