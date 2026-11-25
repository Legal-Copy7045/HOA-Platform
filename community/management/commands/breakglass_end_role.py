from django.db import transaction

from community import services
from community.models import RoleAssignment
from core.breakglass import BreakGlassCommand
from core.policy import SYSTEM
from core.scoping import system_access


class Command(BreakGlassCommand):
    help = "Break-glass: end a role assignment today (audited, shown to the board)."
    command_name = "breakglass_end_role"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--assignment", type=int, required=True)

    def handle(self, *args, **opts):
        actor = self.resolve_actor(opts)
        with system_access("breakglass"):
            assignment = RoleAssignment.objects.get(pk=opts["assignment"])
        with transaction.atomic():
            services.end_role(assignment, actor=SYSTEM)
            self.audit(actor, opts, assignment.pk)
        self.stdout.write("Role ended; the board can see this on the activity page.")
