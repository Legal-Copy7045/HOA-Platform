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


def login_pauses() -> dict:
    """The sign-in pauses in plain words, from the settings in effect (D62).

    ``wait``: the django-axes pause after ``AXES_FAILURE_LIMIT`` wrong
    passwords for one email from one connection (``AXES_COOLOFF_HOURS``).
    ``others``: allauth's ``login_failed`` limits, per connection and per
    email address (``ACCOUNT_RATE_LIMITS``). ``code_attempts``: wrong
    emailed sign-in codes before a new one is needed.
    """
    from allauth.account import app_settings as allauth_settings
    from allauth.core.internal.ratelimit import parse_rates
    from axes.helpers import get_cool_off
    from django.conf import settings

    try:
        rates = parse_rates(allauth_settings.RATE_LIMITS.get("login_failed"))
    except ValueError:
        rates = []
    others = []
    for rate in rates:
        span = duration_words(datetime.timedelta(seconds=rate.duration))
        if rate.per == "ip":
            others.append(
                f"{rate.amount} failed sign-ins from one phone or computer within {span} "
                f"pause it for up to {span}."
            )
        elif rate.per == "key":
            others.append(
                f"{rate.amount} failed sign-ins for one email address within {span} "
                f"pause that address for up to {span}."
            )
    return {
        "limit": settings.AXES_FAILURE_LIMIT,
        "wait": duration_words(get_cool_off()),
        "others": others,
        "code_attempts": settings.ACCOUNT_LOGIN_BY_CODE_MAX_ATTEMPTS,
    }
