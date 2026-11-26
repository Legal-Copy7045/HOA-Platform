"""Search providers: meetings, resolutions and policies, records requests
(core.search). Each uses the same permission and queryset as its page in
governance.views. Minutes are documents and are found by the documents
provider, under the library's own visibility rules.
"""

from django.urls import reverse

from core import policy
from core.search import LIMIT, Result, matching, register, snippet


@register("meetings", "Meetings", url_name="meetings")
def meetings(user, q):
    from governance.models import Meeting

    if not policy.can(user, "meeting.view"):
        return []
    rows = (
        Meeting.objects.for_user(user)
        .filter(matching(q, "title", "agenda", "location"))
        .order_by("-starts_at")[:LIMIT]
    )
    return [
        Result(
            title=m.title,
            url=reverse("meeting_detail", args=[m.pk]),
            snippet=snippet(q, m.agenda, m.location),
            date=m.starts_at,
            detail=m.get_kind_display(),
        )
        for m in rows
    ]


@register("resolutions", "Resolutions and policies", url_name="resolutions")
def resolutions(user, q):
    from governance.models import Resolution

    if not policy.can(user, "resolution.view"):
        return []
    from governance.services import in_force

    rows = list(
        Resolution.objects.filter(matching(q, "number", "title", "text", "citation")).order_by(
            "-adopted_on", "-pk"
        )[:LIMIT]
    )
    current = set(
        in_force(Resolution.objects.filter(pk__in=[r.pk for r in rows])).values_list(
            "pk", flat=True
        )
    )
    out = []
    for r in rows:
        superseded = r.pk not in current
        out.append(
            Result(
                title=f"{r.number} {r.title}",
                # Superseded ones are on the page's "show all" view.
                url=reverse("resolutions") + ("?all=1" if superseded else ""),
                snippet=snippet(q, r.text, r.citation),
                date=r.adopted_on,
                detail=r.get_kind_display() + (" · superseded" if superseded else ""),
            )
        )
    return out


@register("records_requests", "Records requests", url_name="records_requests")
def records_requests(user, q):
    from governance.models import RecordsRequest

    if not (
        policy.can(user, "records_request.manage") or policy.can(user, "records_request.submit")
    ):
        return []
    rows = (
        RecordsRequest.objects.for_user(user)
        .filter(matching(q, "description"))
        .order_by("-submitted_at")[:LIMIT]
    )
    return [
        Result(
            title=str(r),
            url=reverse("records_request_detail", args=[r.pk]),
            snippet=snippet(q, r.description),
            date=r.submitted_at,
        )
        for r in rows
    ]
