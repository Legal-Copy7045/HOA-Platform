"""Invitations (decision D4: an invitation grants nothing; it only links a
login to an existing Person, whose records decide what they can do)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts.models import Invitation, User, normalize_email_address
from audit.models import Channel
from audit.services import record_event
from community import services as community
from community.models import Person
from core.policy import SYSTEM, require
from core.scoping import system_access


class InvitationError(ValueError):
    """Safe to show to the user."""


# A login stuck without a passkey or app for this long shows on the board's
# My items (D59).
STUCK_WITHOUT_AUTHENTICATOR_DAYS = 7


def setup_again_refusal(person: Person) -> str | None:
    """Why a person who already has a login cannot be sent a new setup link,
    or None when they can (D59).

    Only a login that has *never* had a passkey or authenticator app: the
    first setup link was the only thing that let them in, and a new one is no
    weaker. Anyone who ever enrolled, or went through an assisted reset, needs
    the two-board-member reset instead (D5): one board member and the mailbox
    must never be enough to replace a lost authenticator.
    """
    from accounts.auth import has_strong_authenticator
    from accounts.models import MfaReset
    from audit.models import AuditEvent

    with system_access("setup link check"):
        user = User.objects.filter(pk=person.user_id).first()
        if user is None:
            return "This person has no login yet: send an ordinary invitation."
        if not user.is_active:
            return "This login is switched off."
        if has_strong_authenticator(user):
            return "This person already has a passkey or authenticator app."
        if (
            MfaReset.objects.unscoped("setup link check").filter(subject=user).exists()
            or AuditEvent.objects.filter(action="mfa.authenticator_added", actor=user).exists()
        ):
            return (
                "This login has had a passkey or app before. Start a sign-in reset "
                "instead (two board members)."
            )
        if normalize_email_address(person.email or "") != user.email:
            return (
                "The login's email differs from the person's record. Start a sign-in "
                "reset instead (two board members)."
            )
    return None


def stuck_logins(older_than_days: int = STUCK_WITHOUT_AUTHENTICATOR_DAYS) -> list[Person]:
    """People whose login has had no passkey or app for ``older_than_days``,
    with no open setup link or reset waiting (board only: the caller checks)."""
    from allauth.mfa.models import Authenticator

    from accounts.models import MfaReset

    cutoff = timezone.now() - timedelta(days=older_than_days)
    with system_access("stuck logins"):
        enrolled = Authenticator.objects.filter(
            type__in=[Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN]
        ).values("user_id")
        # An open setup link, or one accepted within the period, is still in hand.
        open_links = (
            Invitation.objects.pending() | Invitation.objects.filter(accepted_at__gt=cutoff)
        ).values("person_id")
        open_resets = (
            MfaReset.objects.unscoped("stuck logins")
            .filter(cancelled_at__isnull=True, code_used_at__isnull=True)
            .values("subject_id")
        )
        return list(
            Person.objects.filter(
                user__isnull=False, user__is_active=True, user__date_joined__lte=cutoff
            )
            .exclude(user_id__in=enrolled)
            .exclude(pk__in=open_links)
            .exclude(user_id__in=open_resets)
            .order_by("display_name")
        )


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _actor_user(actor):
    return None if actor is SYSTEM else actor


def _enqueue_send(invitation: Invitation) -> None:
    from accounts.tasks import send_invitation

    invitation_id = invitation.pk
    transaction.on_commit(lambda: send_invitation.defer(invitation_id=invitation_id))


def create_invitation(
    *,
    person: Person,
    actor,
    request: HttpRequest | None = None,
    send_email: bool = True,
) -> Invitation:
    if actor is not SYSTEM:
        require(actor, "invite.send")
    # Always the address on the person's record: an invitation can never be
    # pointed at someone else's mailbox (security review 2026-11, finding A1).
    email = normalize_email_address(person.email or "")
    if not email:
        raise InvitationError("Add an email address to this person's record before inviting them.")
    setup_again = person.user_id is not None
    if setup_again:
        # D59: a new setup link, only for a login that never finished setting up.
        refusal = setup_again_refusal(person)
        if refusal:
            raise InvitationError(refusal)
    elif User.objects.filter(email=email).exists():
        raise InvitationError("That email address already belongs to a login.")
    now = timezone.now()
    with transaction.atomic(), system_access("create_invitation"):
        for old in Invitation.objects.select_for_update().filter(person=person).pending():
            old.revoked_at = now
            old.revoked_by = _actor_user(actor)
            old.save(update_fields=["revoked_at", "revoked_by"])
        invitation = Invitation.objects.create(
            person=person,
            email=email,
            created_by=_actor_user(actor),
            expires_at=now + timedelta(hours=settings.INVITE_TTL_HOURS),
        )
        record_event(
            "invite.created",
            actor=_actor_user(actor),
            request=request,
            target=invitation,
            metadata={"person_id": person.pk, "setup_again": setup_again},
            channel=Channel.WEB if request else Channel.CLI,
        )
        if send_email:
            _enqueue_send(invitation)
    return invitation


def resend_invitation(invitation: Invitation, *, actor, request=None) -> Invitation:
    require(actor, "invite.resend")
    with transaction.atomic(), system_access("resend_invitation"):
        invitation = Invitation.objects.select_for_update().get(pk=invitation.pk)
        if invitation.accepted_at or invitation.revoked_at:
            raise InvitationError("Only an open invitation can be re-sent.")
        if not _matches_record(invitation):
            raise InvitationError(
                "This invitation is not for the address on the person's record. "
                "Revoke it and invite again."
            )
        invitation.expires_at = timezone.now() + timedelta(hours=settings.INVITE_TTL_HOURS)
        # The old link stops working now, not when the worker sends the new one.
        invitation.token_hash = ""  # nosec B105
        invitation.save(update_fields=["expires_at", "token_hash"])
        record_event(
            "invite.resent",
            actor=actor,
            request=request,
            target=invitation,
            metadata={"person_id": invitation.person_id},
        )
        _enqueue_send(invitation)
    return invitation


def revoke_invitation(invitation: Invitation, *, actor, request=None) -> Invitation:
    require(actor, "invite.revoke")
    with transaction.atomic(), system_access("revoke_invitation"):
        invitation = Invitation.objects.select_for_update().get(pk=invitation.pk)
        if invitation.accepted_at or invitation.revoked_at:
            raise InvitationError("This invitation is already closed.")
        invitation.revoked_at = timezone.now()
        invitation.revoked_by = actor
        invitation.token_hash = ""  # nosec B105 - clearing a hash, not a secret
        invitation.save(update_fields=["revoked_at", "revoked_by", "token_hash"])
        record_event(
            "invite.revoked",
            actor=actor,
            request=request,
            target=invitation,
            metadata={"person_id": invitation.person_id},
        )
    return invitation


def send_invitation_email(invitation_id: int) -> bool:
    """Worker side: mint a fresh token, store its hash, email the link.

    The raw token exists only in memory here and in the email itself.
    """
    token = secrets.token_urlsafe(32)
    with transaction.atomic(), system_access("send_invitation_email"):
        invitation = Invitation.objects.select_for_update().filter(pk=invitation_id).first()
        if invitation is None or invitation.status not in ("queued", "pending"):
            return False
        invitation.token_hash = _hash(token)
        invitation.sent_at = timezone.now()
        invitation.send_count += 1
        invitation.save(update_fields=["token_hash", "sent_at", "send_count"])
        link = settings.PORTAL_BASE_URL + reverse("invite_landing", args=[token])
        context = {
            "site_name": settings.SITE_NAME,
            "link": link,
            "expires_at": invitation.expires_at,
        }
        send_mail(
            subject=f"Your invitation to the {settings.SITE_NAME} portal",
            message=render_to_string("accounts/email/invitation.txt", context),
            from_email=None,
            recipient_list=[invitation.email],
        )
        record_event(
            "invite.sent",
            target=invitation,
            channel=Channel.WORKER,
            metadata={"person_id": invitation.person_id, "send_count": invitation.send_count},
        )
    return True


def _matches_record(invitation: Invitation) -> bool:
    with system_access("invitation address check"):
        on_file = normalize_email_address(invitation.person.email or "")
    return bool(on_file) and invitation.email == on_file


def matches_record(invitation: Invitation) -> bool:
    """Still addressed to the email on the person's record (not changed since)."""
    return _matches_record(invitation)


def token_hash(token: str) -> str:
    return _hash(token)


def find_open_invitation(token: str) -> Invitation | None:
    if not token or len(token) > 100:
        return None
    with system_access("find_open_invitation"):
        invitation = Invitation.objects.filter(token_hash=_hash(token)).pending().first()
    return invitation


def accept_invitation(invitation: Invitation, *, password: str, request=None) -> User:
    with transaction.atomic(), system_access("accept_invitation"):
        invitation = Invitation.objects.select_for_update().get(pk=invitation.pk)
        if invitation.status != "pending" or not _matches_record(invitation):
            raise InvitationError("This invitation is no longer valid.")
        person = Person.objects.select_for_update().get(pk=invitation.person_id)
        setup_again = person.user_id is not None
        if setup_again:
            # D59: a new setup link for a login that never finished setting
            # up. Checked again here: the login may have enrolled, or been
            # through a reset, since the link was sent.
            if setup_again_refusal(person):
                raise InvitationError("This invitation is no longer valid.")
            user = User.objects.select_for_update().get(pk=person.user_id)
            validate_password(password, user)
            user.set_password(password)
            # Any other half-finished session of this login ends now.
            user.session_generation += 1
            user.save(update_fields=["password", "session_generation"])
        else:
            if User.objects.filter(email=invitation.email).exists():
                raise InvitationError("That email address already belongs to a login.")
            user = User(email=invitation.email)
            validate_password(password, user)
            user.set_password(password)
            user.save()
            EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
            community.link_login(person, user, actor=SYSTEM, request=request)
        invitation.accepted_at = timezone.now()
        invitation.accepted_user = user
        invitation.token_hash = ""  # nosec B105 - clearing a hash, not a secret
        invitation.save(update_fields=["accepted_at", "accepted_user", "token_hash"])
        record_event(
            "invite.accepted",
            actor=user,
            request=request,
            target=invitation,
            metadata={"person_id": invitation.person_id, "setup_again": setup_again},
        )
    return user


def issue_invitation_link(invitation: Invitation) -> str:
    """CLI only (bootstrap): mint a token and return the link instead of emailing it.

    For the very first account, before email is configured. The operator
    running the command is the person being invited.
    """
    token = secrets.token_urlsafe(32)
    with transaction.atomic(), system_access("issue_invitation_link"):
        invitation = Invitation.objects.select_for_update().get(pk=invitation.pk)
        if invitation.status not in ("queued", "pending"):
            raise InvitationError("This invitation is closed.")
        invitation.token_hash = _hash(token)
        invitation.sent_at = timezone.now()
        invitation.send_count += 1
        invitation.save(update_fields=["token_hash", "sent_at", "send_count"])
        record_event(
            "invite.link_issued",
            target=invitation,
            channel=Channel.CLI,
            metadata={"person_id": invitation.person_id},
        )
    return settings.PORTAL_BASE_URL + reverse("invite_landing", args=[token])


# --- Bulk onboarding (Milestone 4) ---------------------------------------------------


def onboarding_rows() -> list[dict]:
    """Every current owner with their sign-up status. Board only (the view
    checks invite.send). Invitations still go only to the email on record."""
    from community.models import Ownership

    now = timezone.now()
    with system_access("onboarding status"):
        owners = (
            Ownership.objects.active()
            .select_related("person", "lot")
            .order_by("lot__number", "person__display_name")
        )
        latest = {}
        for inv in Invitation.objects.order_by("created_at"):
            latest[inv.person_id] = inv
        from allauth.mfa.models import Authenticator

        enrolled = set(
            Authenticator.objects.filter(
                type__in=[Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN]
            ).values_list("user_id", flat=True)
        )
        rows, seen = [], set()
        for o in owners:
            person = o.person
            if person.pk in seen:
                continue
            seen.add(person.pk)
            inv = latest.get(person.pk)
            if person.user_id and person.user_id not in enrolled:
                # D59: signed in once but never added a passkey or app, so
                # they cannot get back in. Not "signed up".
                status = "no_authenticator"
            elif person.user_id:
                status = "signed_up"
            elif not person.email:
                status = "no_email"
            elif inv is None or inv.revoked_at:
                status = "not_invited"
            elif inv.accepted_at:
                status = "signed_up"
            elif inv.expires_at <= now:
                status = "expired"
            else:
                status = "invited"
            rows.append({"person": person, "lot": o.lot, "status": status, "invitation": inv})
    return rows


ONBOARDING_LABELS = {
    "signed_up": "Signed up",
    "no_authenticator": "Signed in, no passkey or app yet",
    "invited": "Invitation sent",
    "expired": "Invitation expired",
    "not_invited": "Not invited yet",
    "no_email": "No email on record",
}


def invite_everyone(*, actor, request=None) -> int:
    """Invite every owner with an email who has no login and no live invitation
    (expired ones get a fresh invitation)."""
    require(actor, "invite.send")
    sent = 0
    for row in onboarding_rows():
        if row["status"] in ("not_invited", "expired"):
            try:
                create_invitation(person=row["person"], actor=actor, request=request)
            except InvitationError:
                continue
            sent += 1
    return sent


def remind_pending(*, actor, older_than_days: int = 3, request=None) -> int:
    """Re-send open invitations nobody has used for a few days (new link)."""
    require(actor, "invite.resend")
    reminded = 0
    for inv in reminder_due(older_than_days):
        try:
            resend_invitation(inv, actor=actor, request=request)
        except InvitationError:
            continue
        reminded += 1
    return reminded


def reminder_due(older_than_days: int = 3, rows=None) -> list:
    """Open invitations a reminder would go to (the count is shown in the
    confirmation before sending)."""
    cutoff = timezone.now() - timedelta(days=older_than_days)
    return [
        row["invitation"]
        for row in (onboarding_rows() if rows is None else rows)
        if row["status"] == "invited"
        and (row["invitation"].sent_at or row["invitation"].created_at) <= cutoff
    ]


def email_request_letters_pdf(rows) -> bytes:
    """One printable page per owner with no email on record, asking them to
    give the board an address. No codes or links: invitations only ever go to
    an email on the person's record."""
    from core import pdf

    paras = []
    for i, row in enumerate(rows):
        if i:
            paras.append(("pagebreak", ""))
        paras += [
            ("heading", f"{settings.SITE_NAME}: the owners' portal"),
            ("normal", f"To the owner of {row['lot'].label}"),
            ("normal", f"Dear {row['person'].display_name},"),
            (
                "normal",
                "The association now has a secure online portal where owners see their "
                "account, documents, notices and meetings, and apply for architectural "
                "approval.",
            ),
            (
                "normal",
                "We don't have an email address for you. To join, please send the board an "
                "email address you would like to use (write it below and return this page, or "
                "tell a board member). We will then email you a personal invitation.",
            ),
            ("normal", "Email address: ______________________________________________"),
            ("normal", "Name: _____________________________  Date: ______________"),
            ("small", "Your address is used only for association business."),
        ]
    return pdf.render(
        paras or [("normal", "Everyone has an email on record.")], title="Portal letters"
    )
