"""Routine update emails, sent at once or held for a weekly digest according
to each person's choice (Milestone 4). Legal and security notices (meeting
notices, violation notices, invitations, resets, health alerts) do not come
through here: they are always sent at once."""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

from core.scoping import system_access


def _wants_digest(emails: set[str]) -> dict[str, object]:
    """email -> User, for active logins who chose the weekly digest."""
    from accounts.models import User
    from community.models import ContactProfile

    with system_access("digest preferences"):
        digest_people = set(
            ContactProfile.objects.filter(weekly_digest=True).values_list("person_id", flat=True)
        )
        users = User.objects.filter(email__in=list(emails), is_active=True).select_related("person")
        return {
            u.email: u
            for u in users
            if getattr(u, "person", None) is not None and u.person.pk in digest_people
        }


def send_update(emails, *, subject: str, body: str, link: str) -> None:
    """Send now, after the current transaction commits, or hold for the digest."""
    emails = {e for e in emails if e}
    if not emails:
        return
    from comms.models import DigestItem

    digest = _wants_digest(emails)
    for user in digest.values():
        DigestItem.objects.create(user=user, subject=subject[:200], link=link[:500])
    now = [e for e in sorted(emails) if e not in digest]

    def go():
        for email in now:
            send_mail(subject, body, None, [email], fail_silently=True)

    transaction.on_commit(go)
    _push(now, subject=subject, link=link)


def _push(emails, *, subject: str, link: str) -> None:
    """Also notify the installed app of people who get updates at once.
    The subject lines used here carry no personal data (they travel in clear
    in email too); the push holds that title and the page's path only."""
    from comms import push

    if not emails or not push.available():
        return
    from accounts.models import User

    user_ids = User.objects.filter(email__in=list(emails), is_active=True).values_list(
        "pk", flat=True
    )
    title = subject.removeprefix(f"[{settings.SITE_NAME}]").strip()
    push.queue_for_users(user_ids, title=title, link=link)


def send_now(emails, *, subject: str, body: str) -> None:
    """Account notices that must not wait for a weekly digest: sent after the
    current transaction commits."""
    emails = sorted({e for e in emails if e})

    def go():
        for email in emails:
            send_mail(subject, body, None, [email], fail_silently=True)

    if emails:
        transaction.on_commit(go)


def send_digests() -> int:
    """One email per person with everything held since their last digest."""
    from comms.models import DigestItem

    sent = 0
    with system_access("weekly digest"):
        user_ids = set(DigestItem.objects.values_list("user_id", flat=True))
        for user_id in user_ids:
            items = list(DigestItem.objects.filter(user_id=user_id).select_related("user"))
            if not items:
                continue
            user = items[0].user
            lines = "\n".join(f"- {i.subject}\n  {i.link}" for i in items)
            body = (
                f"Your weekly summary from {settings.SITE_NAME} "
                f"({len(items)} update{'s' if len(items) != 1 else ''}):\n\n{lines}\n\n"
                "You can switch to one email per update in My profile.\n\n"
                f"-- \n{settings.SITE_NAME}"
            )
            if user.is_active:
                send_mail(f"[{settings.SITE_NAME}] Your weekly summary", body, None, [user.email])
                sent += 1
            DigestItem.objects.filter(pk__in=[i.pk for i in items]).delete()
    return sent


def held_count(user) -> int:
    from comms.models import DigestItem

    return DigestItem.objects.filter(user=user).count()
