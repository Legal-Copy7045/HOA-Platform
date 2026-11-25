"""Break-glass: a one-time enrollment code for a login with no authenticator,
when the board cannot run an assisted reset.

    python manage.py breakglass_enrollment_code --user 12 --actor you@example.org \
        --reason "first officer missed the setup window"

Only while fewer than two *other* board members have an authenticator (for
example right after bootstrap): otherwise the two-board-member assisted reset
is the only way (decision D5). The code is printed once, works once within
the reset code lifetime, and must be given to the person directly. Audited
and shown on the board's sysadmin activity page.
"""

from datetime import timedelta

from allauth.mfa.models import Authenticator
from django.conf import settings
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from accounts import mfa_reset
from accounts.auth import has_strong_authenticator
from accounts.models import MfaReset, User, VerificationMethod
from community.models import BoardSeat
from core.breakglass import BreakGlassCommand
from core.scoping import system_access


class Command(BreakGlassCommand):
    help = "Break-glass: one-time enrollment code when no two other board members can reset."
    command_name = "breakglass_enrollment_code"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--user", type=int, required=True, help="User ID")

    def handle(self, *args, **opts):
        actor = self.resolve_actor(opts)
        subject = User.objects.filter(pk=opts["user"], is_active=True).first()
        if subject is None:
            raise CommandError("No active login with that ID.")
        if has_strong_authenticator(subject):
            raise CommandError("That login already has a passkey or authenticator app.")
        with system_access("break-glass enrollment code"):
            seated = (
                BoardSeat.objects.active()
                .exclude(person__user=subject)
                .values_list("person__user_id", flat=True)
            )
            enrolled = (
                Authenticator.objects.filter(
                    user_id__in=[u for u in seated if u],
                    type__in=[Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN],
                )
                .values("user_id")
                .distinct()
                .count()
            )
        if enrolled >= 2:
            raise CommandError(
                "Two other board members can sign in: use the assisted reset in the portal."
            )
        code = mfa_reset._new_code()
        now = timezone.now()
        with transaction.atomic(), system_access("break-glass enrollment code"):
            MfaReset.objects.create(
                subject=subject,
                verification_method=VerificationMethod.OTHER,
                started_by=actor,
                request_expires_at=now,
                approved_by=actor,
                approved_at=now,
                code_hash=mfa_reset._hash(code),
                code_expires_at=now + timedelta(hours=settings.MFA_RESET_CODE_TTL_HOURS),
            )
            subject.session_generation += 1
            subject.save(update_fields=["session_generation"])
            self.audit(actor, opts, subject.pk)
        self.stdout.write("One-time enrollment code (give it to the person directly):")
        self.stdout.write(code)
