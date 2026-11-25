from django.db import transaction

from accounts.models import User
from core.breakglass import BreakGlassCommand


class Command(BreakGlassCommand):
    help = (
        "Break-glass: deactivate a login and sign it out everywhere, e.g. a "
        "compromised account (audited, shown to the board)."
    )
    command_name = "breakglass_lock_user"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--user", type=int, required=True, help="User ID")

    def handle(self, *args, **opts):
        actor = self.resolve_actor(opts)
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=opts["user"])
            user.is_active = False
            user.session_generation += 1
            user.save(update_fields=["is_active", "session_generation"])
            self.audit(actor, opts, user.pk)
        self.stdout.write("Login deactivated and signed out everywhere.")
