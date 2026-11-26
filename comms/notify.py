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


# Detail pages a routine update links to, and how to check that the person
# can still open one (each through that area's own scoping). A weekly digest
# only lists updates the person can still see (D44).
def _thread_subject(kind):
    def check(user, pk):
        from django.http import Http404

        from comms import threads

        try:
            threads.subject_for(kind, pk, user)
        except Http404:
            return False
        return True

    return check


def _scoped(model_path):
    def check(user, pk):
        from django.apps import apps

        model = apps.get_model(model_path)
        return model.objects.for_user(user).filter(pk=pk).exists()

    return check


def _survey(user, pk):
    from governance import surveys
    from governance.models import Survey

    survey = Survey.objects.filter(pk=pk).first()
    return survey is not None and surveys.can_view(survey, user)


def _event(user, pk):
    from comms.models import Event
    from core import policy

    return policy.can(user, "calendar.view") and Event.objects.for_user(user).filter(pk=pk).exists()


VISIBILITY = {
    "request_detail": _thread_subject("request"),
    "arc_detail": _thread_subject("arc"),
    "violation_detail": _thread_subject("violation"),
    "work_order_detail": _thread_subject("work_order"),
    "committee_topic_detail": _thread_subject("committee_topic"),
    "signoff_detail": _scoped("arc.SignoffRequest"),
    "reservation_detail": _scoped("community.Reservation"),
    "event_detail": _event,
    "survey_detail": _survey,
}


def still_visible(user, link: str) -> bool:
    """Can ``user`` still open the page this held update links to? False
    when their access has ended (no role left), the area is switched off,
    the page is gone, or the record is out of their scope now."""
    from urllib.parse import urlsplit

    from django.urls import Resolver404, resolve

    from core import features, policy

    if not user.is_active or not policy.roles_for(user).groups:
        return False
    path = urlsplit(link).path or "/"
    try:
        match = resolve(path)
    except Resolver404:
        return False
    if not features.path_enabled(path):
        return False
    check = VISIBILITY.get(match.url_name or "")
    pk = match.kwargs.get("pk")
    if check is None or pk is None:
        return True  # a page without a record of its own: checked when opened
    return bool(check(user, int(pk)))


def send_digests() -> int:
    """One email per person with updates held since their last digest, sent
    only when there is something new they can still open. Held updates they
    can no longer see (access ended, record out of scope) are dropped
    without an email; nobody gets an empty summary."""
    from comms.models import DigestItem

    sent = 0
    with system_access("weekly digest"):
        user_ids = set(DigestItem.objects.values_list("user_id", flat=True))
        for user_id in user_ids:
            items = list(DigestItem.objects.filter(user_id=user_id).select_related("user"))
            if not items:
                continue
            user = items[0].user
            visible = [i for i in items if still_visible(user, i.link)]
            if visible:
                lines = "\n".join(f"- {i.subject}\n  {i.link}" for i in visible)
                count = f"{len(visible)} update{'s' if len(visible) != 1 else ''}"
                body = (
                    f"Your weekly summary from {settings.SITE_NAME} ({count}):\n\n{lines}\n\n"
                    "You can switch to one email per update in My profile.\n\n"
                    f"-- \n{settings.SITE_NAME}"
                )
                send_mail(f"[{settings.SITE_NAME}] Your weekly summary", body, None, [user.email])
                sent += 1
            DigestItem.objects.filter(pk__in=[i.pk for i in items]).delete()
    return sent


def held_count(user) -> int:
    from comms.models import DigestItem

    return DigestItem.objects.filter(user=user).count()
