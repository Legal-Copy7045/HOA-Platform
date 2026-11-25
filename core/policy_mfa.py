"""Which people have MFA enrolled (TOTP or passkey), for the coverage check."""


def people_with_mfa() -> set[int] | None:
    from allauth.mfa.models import Authenticator

    from community.models import Person
    from core.scoping import system_access

    user_ids = Authenticator.objects.filter(
        type__in=[Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN]
    ).values("user_id")
    with system_access("coverage: people with MFA"):
        return set(Person.objects.filter(user_id__in=user_ids).values_list("pk", flat=True))
