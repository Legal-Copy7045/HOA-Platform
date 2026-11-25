"""Print a fresh invitation link on the server (e.g. the bootstrap link was lost).

    python manage.py print_invitation_link --email you@example.org

Only for a person who has no login yet. Any earlier link for them stops
working. Audited as invite.link_issued.
"""

from django.core.management.base import BaseCommand, CommandError

from accounts import services
from community.models import Person
from core.policy import SYSTEM
from core.scoping import system_access


class Command(BaseCommand):
    help = "Print a new single-use invitation link for a person without a login."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)

    def handle(self, *args, **opts):
        email = opts["email"].strip().lower()
        with system_access("print_invitation_link"):
            person = Person.objects.filter(email__iexact=email, user__isnull=True).first()
        if person is None:
            raise CommandError("No person without a login has that email address.")
        try:
            invitation = services.create_invitation(person=person, actor=SYSTEM, send_email=False)
            link = services.issue_invitation_link(invitation)
        except services.InvitationError as exc:
            raise CommandError(str(exc)) from exc
        # Printed on its own short lines so terminal wrapping cannot break it.
        self.stdout.write("Open this link (works once; any earlier link no longer works):\n")
        self.stdout.write(link)
