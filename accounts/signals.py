"""Audit events for sign-in activity (CLAUDE.md "Audit", decision D7)."""

from __future__ import annotations

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from audit.models import Outcome
from audit.services import hash_identifier, record_event


def _method(request, user) -> str | None:
    try:
        from allauth.account.internal.flows.login import get_authentication_records

        records = get_authentication_records(request, user)
    except Exception:
        return None
    for record in reversed(records):
        if record.get("method") == "mfa" and record.get("passwordless"):
            return "passkey"
        if record.get("method") in ("password", "code"):
            return record["method"]
    return None


@receiver(user_logged_in, dispatch_uid="audit_login")
def _logged_in(sender, request, user, **kwargs):
    record_event(
        "auth.login",
        request=request,
        actor=user,
        target=user,
        metadata={"method": _method(request, user)},
    )


@receiver(user_logged_out, dispatch_uid="audit_logout")
def _logged_out(sender, request, user, **kwargs):
    if user is not None:
        record_event("auth.logout", request=request, actor=user, target=user)


@receiver(user_login_failed, dispatch_uid="audit_login_failed")
def _login_failed(sender, credentials, request=None, **kwargs):
    from accounts.auth import axes_username
    from accounts.models import User

    identifier = axes_username(request, credentials)
    account = User.objects.filter(email=identifier).first() if identifier else None
    record_event(
        "auth.login_failed",
        request=request,
        actor=None,
        target=account,
        outcome=Outcome.FAILURE,
        subject_hash=hash_identifier(identifier) if identifier else "",
    )


def connect_optional_signals() -> None:
    """Receivers for allauth and axes signals (imported lazily)."""
    from allauth.account.signals import password_changed, password_reset, password_set
    from allauth.mfa.signals import authenticator_added, authenticator_removed, authenticator_reset
    from axes.signals import user_locked_out

    def _mfa(action):
        def handler(sender, request, user, authenticator, **kwargs):
            record_event(
                action,
                request=request,
                actor=user,
                target=user,
                metadata={"type": authenticator.type},
            )

        return handler

    authenticator_added.connect(
        _mfa("mfa.authenticator_added"), weak=False, dispatch_uid="audit_mfa_added"
    )
    authenticator_removed.connect(
        _mfa("mfa.authenticator_removed"), weak=False, dispatch_uid="audit_mfa_removed"
    )
    authenticator_reset.connect(
        _mfa("mfa.recovery_codes_regenerated"), weak=False, dispatch_uid="audit_mfa_reset"
    )

    def _password(action):
        def handler(sender, request, user, **kwargs):
            record_event(action, request=request, actor=user, target=user)

        return handler

    password_changed.connect(
        _password("password.changed"), weak=False, dispatch_uid="audit_pw_changed"
    )
    password_set.connect(_password("password.changed"), weak=False, dispatch_uid="audit_pw_set")
    password_reset.connect(_password("password.reset"), weak=False, dispatch_uid="audit_pw_reset")

    def _locked(sender, request, username, ip_address, **kwargs):
        record_event(
            "auth.lockout",
            request=request,
            outcome=Outcome.DENIED,
            subject_hash=hash_identifier(username) if username else "",
        )

    user_locked_out.connect(_locked, weak=False, dispatch_uid="audit_lockout")
