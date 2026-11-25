from django.db import transaction

from community import services
from community.models import Ownership
from core.breakglass import BreakGlassCommand
from core.policy import SYSTEM
from core.scoping import system_access


class Command(BreakGlassCommand):
    help = "Break-glass: end an ownership today (audited, shown to the board)."
    command_name = "breakglass_end_ownership"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--ownership", type=int, required=True)

    def handle(self, *args, **opts):
        actor = self.resolve_actor(opts)
        with system_access("breakglass"):
            ownership = Ownership.objects.get(pk=opts["ownership"])
        with transaction.atomic():
            services.end_ownership(ownership, actor=SYSTEM)
            self.audit(actor, opts, ownership.pk)
        self.stdout.write("Ownership ended; the board can see this on the activity page.")
