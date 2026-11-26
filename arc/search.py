"""Search provider: architectural review requests and their threads
(core.search).

Rows come from ``ArcRequest.objects.for_user`` like the ARC list (owners: their
own era through owner_era_q; reviewers and the board: all). Only the request's
own text and its thread are searched, never deliberations (positions); thread
internal notes follow comms.threads, so a recused reviewer never finds them.
Former owners without a current home find their own-era requests on
My history, which is the only page they can open.
"""

from django.urls import reverse

from core import policy
from core.search import LIMIT, Result, matching, register, snippet, thread_results


def _title(r) -> str:
    return f"ARC-{r.pk} {r.title}"


@register("arc", "Architectural review", url_name="arc_list")
def arc(user, q):
    from arc.models import ArcRequest

    list_page = policy.can(user, "arc.submit") or policy.can(user, "arc.view_all")
    history_only = not list_page and policy.can(user, "history.view_own")
    if not (list_page or history_only):
        return []
    subjects = ArcRequest.objects.for_user(user)
    rows = (
        subjects.filter(matching(q, "title", "description", "project_type"))
        .select_related("lot")
        .order_by("-created_at")[:LIMIT]
    )
    out = [
        Result(
            title=_title(r),
            url=reverse("my_history") if history_only else reverse("arc_detail", args=[r.pk]),
            snippet=snippet(q, r.description, r.project_type),
            date=r.created_at,
            detail=f"Lot {r.lot.number} · {r.get_state_display()}",
        )
        for r in rows
    ]
    if list_page:
        out += thread_results("arc", subjects, user, q, title=_title)
    return out
