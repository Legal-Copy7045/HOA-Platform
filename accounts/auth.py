"""Small helpers for the authentication stack."""


def axes_username(request, credentials) -> str | None:
    """The login identifier django-axes counts failures against (normalised)."""
    credentials = credentials or {}
    value = credentials.get("email") or credentials.get("username") or credentials.get("login")
    if not value and request is not None:
        value = request.POST.get("login") if hasattr(request, "POST") else None
    return value.strip().lower() if value else None


def has_strong_authenticator(user) -> bool:
    """TOTP or passkey enrolled (recovery codes alone do not count)."""
    from allauth.mfa.models import Authenticator

    if not getattr(user, "is_authenticated", False):
        return False
    return Authenticator.objects.filter(
        user=user, type__in=[Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN]
    ).exists()
