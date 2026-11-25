"""Print a fresh invitation link on the server (e.g. the bootstrap link was lost).

    python manage.py print_invitation_link --email you@example.org

Only while the portal has no logins at all, i.e. to finish the bootstrap of
the first officer (security review E7): afterwards a printed link would let
whoever runs the command take over the person's identity, so invitations go
by email from the board instead. Any earlier link stops working. Audited as
invite.link_issued (channel: command line) and shown on the board's
sysadmin activity page.
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
            if Person.objects.filter(user__isnull=False).exists():
                raise CommandError(
                    "Refusing: the portal already has logins. Ask the board to send the "
                    "invitation by email from the person's page."
                )
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
