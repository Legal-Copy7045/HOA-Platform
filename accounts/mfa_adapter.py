"""allauth MFA adapter: an account always keeps at least one strong authenticator.

Re-audit of the 2026-11 security review: with enrollment limited to the
session that accepted an invitation, removing your only passkey or app would
lock you out until two board members reset it. Add the new one first, then
remove the old.
"""

from allauth.mfa.adapter import DefaultMFAAdapter
from allauth.mfa.models import Authenticator

STRONG = (Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN)


class MFAAdapter(DefaultMFAAdapter):
    def can_delete_authenticator(self, authenticator: Authenticator) -> bool:
        if authenticator.type not in STRONG:
            return True
        others = (
            Authenticator.objects.filter(user_id=authenticator.user_id, type__in=STRONG)
            .exclude(pk=authenticator.pk)
            .exists()
        )
        return others
