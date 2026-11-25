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
    email: str = "",
    actor,
    request: HttpRequest | None = None,
    send_email: bool = True,
) -> Invitation:
    if actor is not SYSTEM:
        require(actor, "invite.send")
    email = normalize_email_address(email or person.email)
    if not email:
        raise InvitationError("An email address is needed to send an invitation.")
    if person.user_id is not None:
        raise InvitationError("This person already has a login.")
    if User.objects.filter(email=email).exists():
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
            metadata={"person_id": person.pk},
            channel=Channel.WEB if request else Channel.CLI,
        )
        if send_email:
            _enqueue_send(invitation)
    return invitation


def resend_invitation(invitation: Invitation, *, actor, request=None) -> Invitation:
    require(actor, "invite.send")
    with transaction.atomic(), system_access("resend_invitation"):
        invitation = Invitation.objects.select_for_update().get(pk=invitation.pk)
        if invitation.accepted_at or invitation.revoked_at:
            raise InvitationError("Only an open invitation can be re-sent.")
        invitation.expires_at = timezone.now() + timedelta(hours=settings.INVITE_TTL_HOURS)
        invitation.save(update_fields=["expires_at"])
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


def find_open_invitation(token: str) -> Invitation | None:
    if not token or len(token) > 100:
        return None
    with system_access("find_open_invitation"):
        invitation = Invitation.objects.filter(token_hash=_hash(token)).pending().first()
    return invitation


def accept_invitation(invitation: Invitation, *, password: str, request=None) -> User:
    with transaction.atomic(), system_access("accept_invitation"):
        invitation = Invitation.objects.select_for_update().get(pk=invitation.pk)
        if invitation.status != "pending":
            raise InvitationError("This invitation is no longer valid.")
        if User.objects.filter(email=invitation.email).exists():
            raise InvitationError("That email address already belongs to a login.")
        user = User(email=invitation.email)
        validate_password(password, user)
        user.set_password(password)
        user.save()
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
        community.link_login(invitation.person, user, actor=SYSTEM, request=request)
        invitation.accepted_at = timezone.now()
        invitation.accepted_user = user
        invitation.token_hash = ""  # nosec B105 - clearing a hash, not a secret
        invitation.save(update_fields=["accepted_at", "accepted_user", "token_hash"])
        record_event(
            "invite.accepted",
            actor=user,
            request=request,
            target=invitation,
            metadata={"person_id": invitation.person_id},
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
