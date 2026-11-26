"""Self-service account recovery with a waiting period (decision D58).

For someone who lost their phone (or passkey) *and* their recovery codes.
Only ordinary logins may use it: owners, authorized / view-only / tenant
contacts and former owners. Anyone who holds, or held in the last
``STAFF_LOOKBACK_DAYS``, a board seat, an office or a manager, sysadmin,
auditor or ARC reviewer role gets the two-board-member assisted reset
instead (accounts.mfa_reset), and is never told which applies to them.

The flow:

1. Sign-in help → "I've lost my phone and recovery codes" → an email address.
   The page always answers the same way. The worker emails a single-use link
   only to an eligible, active, enrolled login (``send_link``).
2. The link opens a page where the person confirms; that starts the request
   (``start``): it waits ``recovery.wait_hours`` (never under 48). At once the
   person's every email address, their verified text number (if they opted
   in) and the board are told; every notice says how to cancel.
3. Anyone can stop it: the person from a signed-in page or simply by signing
   in with their passkey or app, a board member from My items.
4. After the wait, a fresh emailed link opened in a browser lets the person,
   in that browser session only, finish (``complete``): every old passkey,
   app and recovery code is removed, they are signed out everywhere else,
   and the enrollment window of accounts.enrollment opens in this session.
5. For ``recovery.lock_hours`` afterwards (never under 24) the account
   cannot vote, give proxies, change contact details or notices, set up
   automatic payments or ask for account changes: services call
   ``refuse_if_locked``.

Email alone never signs anyone in without the waiting period and the alerts
(the owner accepted that trade-off for ordinary accounts, D58).
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import secrets

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import (
    AccountRecovery,
    RecoveryCancelledBy,
    RecoveryLink,
    RecoveryLinkPurpose,
    User,
)
from audit.services import record_event
from core.scoping import system_access

# Floors and ceilings that hold whatever the board's settings say.
MIN_WAIT_HOURS = 48
MIN_LOCK_HOURS = 24
MAX_HOURS = 14 * 24
# Staff roles ended this recently still rule out self-service recovery.
STAFF_LOOKBACK_DAYS = 30
# At most this many recoveries started per login in 30 days.
MAX_PER_30_DAYS = 3
# Emailed links: short-lived and few.
LINK_TTL = datetime.timedelta(hours=1)
MAX_LINKS_PER_DAY = 5
# After the wait the person has this long to finish before it expires.
FINISH_WITHIN = datetime.timedelta(days=7)
# The board's My items entry after a completed recovery.
BOARD_REVIEW_DAYS = 7

SESSION_LINK = "account_recovery_link"
SESSION_TOKEN = "account_recovery_token_hash"  # noqa: S105  # nosec B105


class RecoveryError(ValueError):
    """Safe to show."""


class RecoveryLocked(RecoveryError):
    """An action refused during the lock after a recovery."""


# --- Settings ----------------------------------------------------------------------


def _hours(key: str, floor: int) -> int:
    from community.rules import rule

    try:
        value = int(rule(key) or 0)
    except (TypeError, ValueError):
        value = 0
    return min(max(value, floor), MAX_HOURS)


def wait_hours() -> int:
    """The waiting period for a request starting now (rule recovery.wait_hours)."""
    return _hours("recovery.wait_hours", MIN_WAIT_HOURS)


def lock_hours() -> int:
    """The lock after completion for a request starting now (rule recovery.lock_hours)."""
    return _hours("recovery.lock_hours", MIN_LOCK_HOURS)


def when(value: datetime.datetime) -> str:
    """'Saturday, November 28 at 3:15 PM' in the site's time zone."""
    local = timezone.localtime(value)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%A, %B} {local.day} at {hour}:{local:%M %p}"


def _absolute(path: str) -> str:
    return f"{settings.PORTAL_BASE_URL}{path}"


# --- Who may recover themselves -------------------------------------------------------


def _staff_recently(user: User) -> bool:
    """A board seat, an office or any role now, or ended within the lookback."""
    from community.models import BoardSeat, OfficeHolding, Person, RoleAssignment

    cutoff = timezone.localdate() - datetime.timedelta(days=STAFF_LOOKBACK_DAYS)
    with system_access("account recovery eligibility"):
        person = Person.objects.filter(user=user).first()
        if person is None:
            return True
        if (
            BoardSeat.objects.filter(person=person)
            .exclude(ended_on__isnull=False, ended_on__lte=cutoff)
            .exists()
        ):
            return True
        if (
            OfficeHolding.objects.filter(person=person)
            .exclude(end_date__isnull=False, end_date__lte=cutoff)
            .exists()
        ):
            return True
        return (
            RoleAssignment.objects.filter(person=person)
            .exclude(end_date__isnull=False, end_date__lte=cutoff)
            .exists()
        )


def eligible(user: User | None) -> bool:
    """May this login recover itself? Ordinary roles only (D58)."""
    from accounts.auth import has_strong_authenticator
    from core import policy

    if user is None or not user.is_active or user.is_staff or user.is_superuser:
        return False
    if _staff_recently(user):
        return False
    groups = policy.roles_for(user).groups
    if not groups or not groups <= policy.SELF_SERVICE | {policy.COMMITTEE_MEMBER}:
        return False
    # Only an enrolled login recovers; one that never set up a passkey or app
    # needs a new invitation from the board.
    return has_strong_authenticator(user)


# --- Requests -----------------------------------------------------------------------


def open_recovery(user: User) -> AccountRecovery | None:
    """The waiting or ready request of this login, if any."""
    now = timezone.now()
    return (
        AccountRecovery.objects.unscoped("open recovery")
        .filter(
            subject=user,
            cancelled_at__isnull=True,
            completed_at__isnull=True,
            expires_at__gt=now,
        )
        .first()
    )


def _recent_count(user: User) -> int:
    since = timezone.now() - datetime.timedelta(days=30)
    return (
        AccountRecovery.objects.unscoped("recovery limit")
        .filter(subject=user, started_at__gte=since)
        .count()
    )


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _new_link(user: User, purpose: str, recovery: AccountRecovery | None = None) -> str:
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    RecoveryLink.objects.create(
        subject=user,
        purpose=purpose,
        recovery=recovery,
        token_hash=_hash(token),
        created_at=now,
        expires_at=now + LINK_TTL,
    )
    return token


def _refuse(user: User, reason: str) -> None:
    record_event(
        "account_recovery.link_refused",
        target=user,
        metadata={"user_id": user.pk, "reason": reason},
    )


def _mail(emails, subject: str, body: str) -> None:
    from comms.notify import send_now

    send_now(emails, subject=f"[{settings.SITE_NAME}] {subject}", body=body)


def _status_url() -> str:
    return _absolute(reverse("account_recovery_status"))


def send_link(user_id: int) -> str:
    """Worker: what the "lost my phone and recovery codes" form leads to.

    Returns what happened ("sent_start", "sent_finish", "waiting", or why
    nothing was sent) for tests and the audit; the page never shows it."""
    if not user_id:
        return "unknown"
    with transaction.atomic(), system_access("account recovery link"):
        user = User.objects.select_for_update().filter(pk=user_id).first()
        if user is None:
            return "unknown"
        if not eligible(user):
            _refuse(user, "ineligible")
            return "ineligible"
        since = timezone.now() - datetime.timedelta(days=1)
        if (
            RecoveryLink.objects.filter(subject=user, created_at__gte=since).count()
            >= MAX_LINKS_PER_DAY
        ):
            _refuse(user, "link_limit")
            return "link_limit"
        current = open_recovery(user)
        emails, _number = _contacts(user)
        if current is not None and current.status == "waiting":
            _mail(
                emails,
                "Your account recovery is still waiting",
                f"Someone asked again to recover the sign-in for this address.\n\n"
                f"The recovery that is already waiting can be finished from "
                f"{when(current.ready_at)}. Ask for a new link on the sign-in help page "
                f"then.\n\nIf this wasn't you, cancel it: sign in as usual with your "
                f"passkey or authenticator app (that cancels it), or open\n"
                f"{_status_url()}\nor call the board.\n\n-- \n{settings.SITE_NAME}",
            )
            _refuse(user, "waiting")
            return "waiting"
        if current is not None:  # ready: a link to finish it
            token = _new_link(user, RecoveryLinkPurpose.FINISH, current)
            purpose, subject = "finish", "Finish recovering your sign-in"
            text = (
                "The waiting period for your account recovery is over. To remove your old "
                "passkeys and authenticator app and set up new ones, open this link within "
                f"an hour, on the phone or computer you will use:\n\n{_link_url(token)}\n\n"
                "The link works once."
            )
        else:
            if _recent_count(user) >= MAX_PER_30_DAYS:
                _refuse(user, "limit")
                return "limit"
            token = _new_link(user, RecoveryLinkPurpose.START)
            purpose, subject = "start", "Recover your sign-in"
            text = (
                "Someone (we hope you) asked to recover the sign-in for this address because "
                "the phone or passkey and the recovery codes are lost.\n\n"
                f"To start, open this link within an hour:\n\n{_link_url(token)}\n\n"
                f"For your security a recovery waits {wait_hours()} hours, and you, the board "
                "and every address we have for you are told. If you still have your passkey, "
                "authenticator app or a recovery code, you don't need this: sign in as usual."
            )
        _mail(
            [user.email],
            subject,
            f"{text}\n\nIf this wasn't you, ignore this email: nothing changes unless the "
            f"link is used.\n\n-- \n{settings.SITE_NAME}",
        )
        record_event(
            "account_recovery.link_sent",
            target=user,
            metadata={"user_id": user.pk, "purpose": purpose},
        )
    return f"sent_{purpose}"


def _link_url(token: str) -> str:
    return _absolute(reverse("recovery_link", args=[token]))


def _contacts(user: User) -> tuple[list[str], str]:
    from community.services import recovery_contacts

    return recovery_contacts(user)


def find_link(token: str) -> RecoveryLink | None:
    """An unused, unexpired link for this token (or None)."""
    if not token or len(token) > 100:
        return None
    link = (
        RecoveryLink.objects.unscoped("recovery link")
        .select_related("subject", "recovery")
        .filter(token_hash=_hash(token))
        .first()
    )
    return link if link is not None and link.usable else None


def link_from_session(request) -> RecoveryLink | None:
    """The link this browser session opened, if still usable and current."""
    link_id = request.session.get(SESSION_LINK)
    token_hash = request.session.get(SESSION_TOKEN) or ""
    if not link_id or not token_hash:
        return None
    link = (
        RecoveryLink.objects.unscoped("recovery link from session")
        .select_related("subject", "recovery")
        .filter(pk=link_id)
        .first()
    )
    if link is None or not hmac.compare_digest(link.token_hash, token_hash) or not link.usable:
        return None
    return link


def forget_session(request) -> None:
    request.session.pop(SESSION_LINK, None)
    request.session.pop(SESSION_TOKEN, None)


def _use(link: RecoveryLink) -> RecoveryLink:
    """Mark the link used (under a lock); refuses one already used."""
    locked = RecoveryLink.objects.unscoped("use recovery link").select_for_update().get(pk=link.pk)
    if not locked.usable:
        raise RecoveryError("This link has already been used or has expired.")
    locked.used_at = timezone.now()
    locked.save(update_fields=["used_at"])
    return locked


def start(link: RecoveryLink, *, request=None) -> AccountRecovery:
    """The person confirmed on the page: start the waiting period and tell
    everyone at once."""
    if link.purpose != RecoveryLinkPurpose.START:
        raise RecoveryError("This link is not for starting a recovery.")
    with transaction.atomic(), system_access("start account recovery"):
        user = User.objects.select_for_update().get(pk=link.subject_id)
        _use(link)
        if not eligible(user):
            _refuse(user, "ineligible")
            raise RecoveryError(INELIGIBLE)
        if open_recovery(user) is not None:
            raise RecoveryError("A recovery for this account is already under way.")
        if _recent_count(user) >= MAX_PER_30_DAYS:
            _refuse(user, "limit")
            raise RecoveryError(
                "Too many recoveries were started for this account recently. Please ask "
                "the board for an assisted reset."
            )
        now = timezone.now()
        wait, lock = wait_hours(), lock_hours()
        recovery = AccountRecovery.objects.create(
            subject=user,
            started_at=now,
            wait_hours=wait,
            lock_hours=lock,
            ready_at=now + datetime.timedelta(hours=wait),
            expires_at=now + datetime.timedelta(hours=wait) + FINISH_WITHIN,
        )
        emails, number, board = _notify_started(user, recovery)
        record_event(
            "account_recovery.started",
            request=request,
            target=user,
            metadata={
                "recovery_id": recovery.pk,
                "user_id": user.pk,
                "wait_hours": wait,
                "lock_hours": lock,
                "emails": emails,
                "sms": number,
                "board": board,
            },
        )
    return recovery


INELIGIBLE = (
    "This account can't be recovered this way. Please ask two board members for an assisted reset."
)


def _notify_started(user: User, recovery: AccountRecovery) -> tuple[int, bool, int]:
    """Every email on file, the verified text number, and the board. Returns
    counts (for the audit) only."""
    from community.services import board_emails, person_and_homes

    emails, number = _contacts(user)
    ready = when(recovery.ready_at)
    _mail(
        emails,
        "Someone asked to recover your sign-in",
        "Someone asked to recover the sign-in to your account because the phone or passkey "
        f"and the recovery codes were lost.\n\nIf it was you: from {ready} you can finish "
        "it. Ask for a new link on the sign-in help page then.\n\n"
        "If this wasn't you, cancel: sign in as usual with your passkey or authenticator app "
        f"(that cancels it at once), or open\n{_status_url()}\nor call the board. Nothing "
        f"changes before {ready}.\n\n-- \n{settings.SITE_NAME}",
    )
    sent_sms = False
    if number:
        sent_sms = _text(number)
    name, homes = person_and_homes(user)
    where = f" ({', '.join(homes)})" if homes else ""
    board = board_emails()
    _mail(
        board,
        "Account recovery requested",
        f"{name}{where} asked to recover their sign-in (lost phone and recovery codes). "
        f"They can finish it from {ready}.\n\nIf this looks wrong, cancel it from My items: "
        f"{_absolute(reverse('recovery_board', args=[recovery.pk]))}\n\n"
        f"-- \n{settings.SITE_NAME}",
    )
    return len(emails), sent_sms, len(board)


def _text(number: str) -> bool:
    """One short text, only while the text-alert feature is on and set up.
    The text carries no names or account details."""
    from comms import sms

    if not sms.available() or not sms.can_send():
        return False
    body = (
        f"{settings.SITE_NAME}: someone asked to recover your sign-in. If this wasn't you, "
        f"cancel: {_status_url()} {sms.STOP_NOTE}"
    )

    def go():
        with contextlib.suppress(sms.SmsError):
            sms.provider().send(number, body)

    transaction.on_commit(go)
    return True


def cancel(recovery: AccountRecovery, *, actor, by: str, request=None) -> AccountRecovery:
    """Stop a waiting or ready recovery: the person, a board member, or the
    person signing in with their existing passkey or app."""
    from core import policy

    if by == RecoveryCancelledBy.BOARD:
        policy.require(actor, "account_recovery.cancel")
    elif actor is None or actor.pk != recovery.subject_id:
        raise RecoveryError("Only the person or the board can cancel this.")
    with transaction.atomic(), system_access("cancel account recovery"):
        row = AccountRecovery.objects.select_for_update().get(pk=recovery.pk)
        if not row.is_open:
            raise RecoveryError("This recovery is not waiting any more.")
        row.cancelled_at = timezone.now()
        row.cancelled_by = actor
        row.cancel_reason = by
        row.save(update_fields=["cancelled_at", "cancelled_by", "cancel_reason"])
        RecoveryLink.objects.filter(recovery=row, used_at__isnull=True).update(
            used_at=row.cancelled_at
        )
        record_event(
            "account_recovery.cancelled",
            actor=actor,
            request=request,
            target=row.subject,
            metadata={"recovery_id": row.pk, "user_id": row.subject_id, "by": by},
        )
        emails, _number = _contacts(row.subject)
        who = {
            RecoveryCancelledBy.BOARD: "A board member cancelled",
            RecoveryCancelledBy.PERSON: "You cancelled",
            RecoveryCancelledBy.SIGNED_IN: "You signed in with your passkey or app, which "
            "cancelled",
        }[by]
        _mail(
            emails,
            "Account recovery cancelled",
            f"{who} the request to recover the sign-in to your account. Nothing was "
            f"changed.\n\n-- \n{settings.SITE_NAME}",
        )
    return row


def cancel_on_sign_in(user: User, request=None) -> AccountRecovery | None:
    """Called after a sign-in that used an existing passkey or app."""
    current = open_recovery(user)
    if current is None:
        return None
    return cancel(current, actor=user, by=RecoveryCancelledBy.SIGNED_IN, request=request)


def complete(link: RecoveryLink, *, request) -> User:
    """After the wait, in the browser session that opened the finish link:
    remove the old authenticators, sign out everywhere else, sign in here and
    open the enrollment window (accounts.enrollment) for this session only."""
    from allauth.account.internal.flows.login import record_authentication
    from allauth.mfa.models import Authenticator
    from django.contrib.auth import login

    from accounts import enrollment

    if link.purpose != RecoveryLinkPurpose.FINISH or link.recovery_id is None:
        raise RecoveryError("This link is not for finishing a recovery.")
    with transaction.atomic(), system_access("complete account recovery"):
        user = User.objects.select_for_update().get(pk=link.subject_id)
        _use(link)
        recovery = AccountRecovery.objects.select_for_update().get(pk=link.recovery_id)
        if recovery.status == "waiting":
            raise RecoveryError(
                f"The waiting period is not over yet: come back on {when(recovery.ready_at)}."
            )
        if recovery.status != "ready":
            raise RecoveryError("This recovery is not open any more.")
        if not eligible(user):
            _refuse(user, "ineligible")
            raise RecoveryError(INELIGIBLE)
        removed, _ = Authenticator.objects.filter(user=user).delete()
        user.session_generation += 1
        user.save(update_fields=["session_generation"])
        recovery.completed_at = timezone.now()
        recovery.save(update_fields=["completed_at"])
        record_event(
            "account_recovery.completed",
            request=request,
            actor=user,
            target=user,
            metadata={
                "recovery_id": recovery.pk,
                "user_id": user.pk,
                "removed": removed,
                "lock_hours": recovery.lock_hours,
            },
        )
        emails, _number = _contacts(user)
        until = when(recovery.lock_until)
        _mail(
            emails,
            "Your sign-in was recovered",
            "Your account's old passkeys, authenticator app and recovery codes were removed "
            "and a new one is being set up. For your security, voting, proxies, contact "
            f"details and automatic payments are locked until {until}.\n\nIf this wasn't "
            f"you, call the board at once.\n\n-- \n{settings.SITE_NAME}",
        )
        from community.services import board_emails, person_and_homes

        name, homes = person_and_homes(user)
        where = f" ({', '.join(homes)})" if homes else ""
        _mail(
            board_emails(),
            "Account recovery completed",
            f"{name}{where} recovered their sign-in after the waiting period. Review it if "
            f"you did not expect it: {_absolute(reverse('recovery_board', args=[recovery.pk]))}"
            f"\n\n-- \n{settings.SITE_NAME}",
        )
    request.session.cycle_key()
    forget_session(request)
    record_authentication(
        request, user, method="password", reauthenticated=True, via="account_recovery"
    )
    login(request, user, backend="allauth.account.auth_backends.AuthenticationBackend")
    enrollment.grant(request, via="recovery")
    return user


# --- The lock after a recovery --------------------------------------------------------


def locked_until(user) -> datetime.datetime | None:
    """While the lock after a completed recovery lasts: when it ends."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.pk:
        return None
    now = timezone.now()
    rows = (
        AccountRecovery.objects.unscoped("recovery lock")
        .filter(
            subject_id=user.pk,
            completed_at__gte=now - datetime.timedelta(hours=MAX_HOURS),
        )
        .values_list("completed_at", "lock_hours")
    )
    until = None
    for completed_at, hours in rows:
        end = completed_at + datetime.timedelta(hours=max(hours, MIN_LOCK_HOURS))
        if end > now and (until is None or end > until):
            until = end
    return until


def lock_message(until: datetime.datetime) -> str:
    return (
        f"For your security, this is available again on {when(until)} because your "
        "sign-in was recently recovered."
    )


def refuse_if_locked(actor, what: str, error: type[Exception] = RecoveryLocked) -> None:
    """Every service in the lock list calls this first (D58). ``error`` is the
    calling service's own error class, so its views show the message on the
    form like any other refusal. ``what`` names the action, for readers."""
    from core.policy import SYSTEM

    if actor is SYSTEM or actor is None:
        return
    until = locked_until(actor)
    if until is not None:
        raise error(lock_message(until))


# --- Board review -------------------------------------------------------------------


def board_items_queryset():
    """Requests the board should see: open ones, and completed in the last week."""
    from django.db.models import Q

    now = timezone.now()
    return (
        AccountRecovery.objects.unscoped("board recovery items")
        .filter(
            Q(cancelled_at__isnull=True, completed_at__isnull=True, expires_at__gt=now)
            | Q(completed_at__gte=now - datetime.timedelta(days=BOARD_REVIEW_DAYS))
        )
        .select_related("subject")
        .order_by("started_at")
    )


def has_recovery_on_record(user) -> bool:
    """Any self-service recovery ever started for this login (open, finished,
    cancelled or expired). Counts as "a reset on record" for other sign-in
    repair paths (e.g. a board "send a new setup link"), so they never overlap."""
    return AccountRecovery.objects.unscoped("recovery on record").filter(subject=user).exists()
