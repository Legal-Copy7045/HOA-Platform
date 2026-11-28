"""Assisted MFA reset: two board members plus an out-of-band code (D5)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from allauth.mfa.models import Authenticator
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import MfaReset, User, VerificationMethod
from audit.models import Outcome
from audit.services import record_event
from core.policy import require
from core.scoping import system_access

# No 0/O or 1/I/L: the code is read aloud or copied by hand.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class MfaResetError(ValueError):
    """Safe to show."""


def _hash(code: str) -> str:
    normalised = code.replace("-", "").replace(" ", "").upper()
    return hashlib.sha256(normalised.encode()).hexdigest()


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return "-".join(raw[i : i + 4] for i in range(0, 12, 4))


def _board_notice(kind: str) -> None:
    from community.tasks import notify_board

    transaction.on_commit(lambda: notify_board.defer(kind=kind))


def _refuse_real_login(user_id) -> None:
    """Demo mode (D97): a persona never resets a real login's sign-in."""
    from core.demo_mode import refuse_real_target

    refuse_real_target(user_id=user_id, error=MfaResetError)


def open_reset_for(user: User) -> MfaReset | None:
    """The reset blocking this user's account, if any (approved, code not used)."""
    return (
        MfaReset.objects.unscoped("open reset check")
        .filter(
            subject=user,
            approved_at__isnull=False,
            code_used_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .order_by("-approved_at")
        .first()
    )


def start_reset(*, subject: User, verification_method: str, actor, request=None) -> MfaReset:
    require(actor, "mfa_reset.start")
    _refuse_real_login(subject.pk)
    if verification_method not in VerificationMethod.values:
        raise MfaResetError("Choose how you confirmed the person's identity.")
    if subject.pk == actor.pk:
        raise MfaResetError("Another board member must start a reset of your own account.")
    with transaction.atomic(), system_access("mfa reset"):
        pending = MfaReset.objects.select_for_update().filter(
            subject=subject,
            approved_at__isnull=True,
            cancelled_at__isnull=True,
            request_expires_at__gt=timezone.now(),
        )
        if pending.exists():
            raise MfaResetError("A reset for this account is already waiting for approval.")
        reset = MfaReset.objects.create(
            subject=subject,
            verification_method=verification_method,
            started_by=actor,
            request_expires_at=timezone.now() + timedelta(days=settings.MFA_RESET_REQUEST_TTL_DAYS),
        )
        record_event(
            "mfa_reset.started",
            actor=actor,
            request=request,
            target=subject,
            metadata={"user_id": subject.pk, "verification_method": verification_method},
        )
        _board_notice("mfa_reset_started")
    return reset


def approve_reset(reset: MfaReset, *, actor, request=None) -> str:
    """Approve; returns the enrollment code, which is shown once and never stored."""
    require(actor, "mfa_reset.approve")
    _refuse_real_login(reset.subject_id)
    with transaction.atomic(), system_access("mfa reset"):
        reset = MfaReset.objects.select_for_update().get(pk=reset.pk)
        if reset.status != "awaiting_approval":
            raise MfaResetError("This reset is not waiting for approval.")
        if actor.pk == reset.started_by_id:
            raise MfaResetError("A different board member must approve a reset you started.")
        if actor.pk == reset.subject_id:
            raise MfaResetError("You cannot approve a reset of your own account.")
        subject = User.objects.select_for_update().get(pk=reset.subject_id)
        Authenticator.objects.filter(user=subject).delete()
        subject.session_generation += 1
        subject.save(update_fields=["session_generation"])
        code = _new_code()
        reset.approved_by = actor
        reset.approved_at = timezone.now()
        reset.code_hash = _hash(code)
        reset.code_expires_at = reset.approved_at + timedelta(
            hours=settings.MFA_RESET_CODE_TTL_HOURS
        )
        reset.save(update_fields=["approved_by", "approved_at", "code_hash", "code_expires_at"])
        record_event(
            "mfa_reset.approved",
            actor=actor,
            request=request,
            target=subject,
            metadata={"user_id": subject.pk},
        )
        _board_notice("mfa_reset_approved")
    return code


def cancel_reset(reset: MfaReset, *, actor, request=None) -> MfaReset:
    require(actor, "mfa_reset.start")
    _refuse_real_login(reset.subject_id)
    with transaction.atomic(), system_access("mfa reset"):
        reset = MfaReset.objects.select_for_update().get(pk=reset.pk)
        if reset.status != "awaiting_approval":
            raise MfaResetError("Only a reset waiting for approval can be cancelled.")
        reset.cancelled_at = timezone.now()
        reset.cancelled_by = actor
        reset.save(update_fields=["cancelled_at", "cancelled_by"])
        record_event(
            "mfa_reset.cancelled",
            actor=actor,
            request=request,
            target=reset.subject,
            metadata={"user_id": reset.subject_id},
        )
    return reset


def redeem_code(*, user: User, code: str, request=None) -> bool:
    with transaction.atomic(), system_access("mfa reset"):
        reset = open_reset_for(user)
        if reset is None:
            return False
        reset = MfaReset.objects.select_for_update().get(pk=reset.pk)
        if reset.status != "awaiting_code":
            return False
        if hmac.compare_digest(reset.code_hash, _hash(code or "")):
            reset.code_used_at = timezone.now()
            reset.code_hash = ""
            reset.save(update_fields=["code_used_at", "code_hash"])
            record_event(
                "mfa_reset.code_redeemed",
                actor=user,
                request=request,
                target=user,
                metadata={"user_id": user.pk},
            )
            return True
        reset.failed_attempts += 1
        reset.save(update_fields=["failed_attempts"])
        record_event(
            "mfa_reset.code_failed",
            actor=user,
            request=request,
            target=user,
            outcome=Outcome.FAILURE,
            metadata={"user_id": user.pk, "attempts": reset.failed_attempts},
        )
        if reset.failed_attempts == reset.MAX_CODE_ATTEMPTS:
            # Someone may be guessing (review B4): tell the board, who can
            # start a new reset once they have spoken to the owner.
            _board_notice("mfa_reset_code_locked")
    return False
