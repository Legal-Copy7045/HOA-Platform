"""Search provider: violations (notices about a home) and their threads
(core.search).

Rows come from ``Violation.objects.for_user`` like the violations list
(owners: only those a notice was sent for, from their own era). Owners'
written responses are not searched. Former owners without a current home
find their own-era notices on My history.
"""

from django.urls import reverse

from core import policy
from core.search import LIMIT, Result, matching, register, snippet, thread_results


def _title(v) -> str:
    return f"Violation #{v.pk}: {v.get_category_display()}"


@register("violations", "Violations", url_name="violations")
def violations(user, q):
    from compliance.models import Violation

    list_page = any(
        policy.can(user, p) for p in ("violation.manage", "violation.view_all", "lot.view_own")
    )
    history_only = not list_page and policy.can(user, "history.view_own")
    if not (list_page or history_only):
        return []
    subjects = Violation.objects.for_user(user)
    rows = (
        subjects.filter(matching(q, "description"))
        .select_related("lot")
        .order_by("-created_at")[:LIMIT]
    )
    out = [
        Result(
            title=_title(v),
            url=reverse("my_history") if history_only else reverse("violation_detail", args=[v.pk]),
            snippet=snippet(q, v.description),
            date=v.created_at,
            detail=f"Lot {v.lot.number} · {v.get_state_display()}",
        )
        for v in rows
    ]
    if list_page:
        out += thread_results("violation", subjects, user, q, title=_title)
    return out
