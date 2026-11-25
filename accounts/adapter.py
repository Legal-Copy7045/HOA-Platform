from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings
from django.http import HttpRequest


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request: HttpRequest) -> bool:
        # Invite-only (CLAUDE.md). Invitations create accounts through
        # accounts.services, never through allauth's signup.
        return False

    def send_mail(self, template_prefix: str, email: str, context: dict) -> None:
        context = {**context, "site_name": settings.SITE_NAME}
        super().send_mail(template_prefix, email, context)
