"""Small helpers for the sign-in pages (UX review: who to contact, how long to wait)."""

from __future__ import annotations

import datetime

from django import template

register = template.Library()


@register.simple_tag
def signin_contact() -> str:
    """Who to contact about sign-in trouble, from Community settings ('' if unset)."""
    from community.models import CommunityProfile

    profile = CommunityProfile.objects.filter(pk=1).only("signin_help_contact").first()
    return profile.signin_help_contact if profile else ""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def duration_words(span: datetime.timedelta | None) -> str:
    if not span:
        return "a while"
    minutes = max(1, round(span.total_seconds() / 60))
    if minutes < 60:
        return _plural(minutes, "minute")
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return _plural(hours, "hour")
    return f"{_plural(hours, 'hour')} {_plural(rest, 'minute')}"


@register.simple_tag
def lockout_wait(cooloff=None) -> str:
    """The sign-in pause in words, from the axes setting (e.g. "30 minutes")."""
    if not isinstance(cooloff, datetime.timedelta):
        from axes.helpers import get_cool_off

        cooloff = get_cool_off()
    return duration_words(cooloff)


@register.simple_tag
def enrollment_window() -> str:
    """How long a new or reset login has to add a passkey or app (e.g. "1 hour")."""
    from django.conf import settings

    return duration_words(datetime.timedelta(seconds=settings.MFA_ENROLLMENT_WINDOW_SECONDS))
