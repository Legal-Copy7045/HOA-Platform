from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings
from django.http import HttpRequest

from core.net import client_ip


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request: HttpRequest) -> bool:
        # Invite-only (CLAUDE.md). Invitations create accounts through
        # accounts.services, never through allauth's signup.
        return False

    def send_mail(self, template_prefix: str, email: str, context: dict) -> None:
        context = {**context, "site_name": settings.SITE_NAME}
        super().send_mail(template_prefix, email, context)

    def get_client_ip(self, request: HttpRequest) -> str:
        # Rate limits must key on the real client, as established by
        # TrustedProxyMiddleware, not on the tunnel's address (security
        # review 2026-11: otherwise one client could lock out everyone).
        return client_ip(request) or "unknown"
