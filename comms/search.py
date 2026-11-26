"""Search providers: announcements, calendar and FAQ (core.search).

Each uses the same permission and queryset as its page in comms.views.
"""

from django.urls import reverse

from core import policy
from core.search import LIMIT, Result, matching, register, snippet


@register("announcements", "Announcements", url_name="announcements")
def announcements(user, q):
    from comms.models import Announcement

    if not policy.can(user, "announcement.view"):
        return []
    rows = (
        Announcement.objects.for_user(user)
        .filter(matching(q, "title", "body"))
        .order_by("-published_at")[:LIMIT]
    )
    return [
        Result(
            title=a.title,
            url=reverse("announcement_detail", args=[a.pk]),
            snippet=snippet(q, a.body),
            date=a.published_at,
        )
        for a in rows
    ]


@register("calendar", "Calendar", url_name="calendar", soonest_first=True)
def calendar(user, q):
    from comms import services

    if not policy.can(user, "calendar.view"):
        return []
    # Each hit opens its own page; a meeting's event opens the meeting page
    # when this person may open that meeting (D49).
    rows = services.with_page_urls(
        services.upcoming(user)
        .filter(matching(q, "title", "description", "location"))
        .order_by("starts_at")[:LIMIT],
        user,
    )
    return [
        Result(
            title=e.title,
            url=e.page_url,
            snippet=snippet(q, e.description, e.location),
            date=e.starts_at,
            detail="Cancelled" if e.cancelled else "",
        )
        for e in rows
    ]


@register("faq", "Frequently asked questions", url_name="faq")
def faq(user, q):
    from urllib.parse import urlencode

    from comms.models import FaqEntry

    if not policy.can(user, "announcement.view"):
        return []
    entries = (
        FaqEntry.objects.all()
        if policy.can(user, "faq.manage")
        else FaqEntry.objects.filter(published=True)
    )
    rows = entries.filter(matching(q, "question", "answer", "category")).order_by("-updated_at")
    url = reverse("faq") + "?" + urlencode({"q": q})
    return [
        Result(
            title=e.question,
            url=url,
            snippet=snippet(q, e.answer),
            date=e.updated_at,
            detail="" if e.published else "Hidden",
        )
        for e in rows[:LIMIT]
    ]
