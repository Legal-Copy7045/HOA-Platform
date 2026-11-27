"""Create the first board officer (and optionally the first sysadmin).

Run once, on the server, by the person who will be that officer:

    python manage.py bootstrap_first_officer --name "Pat Example" \
        --email pat@example.org --office president --also-sysadmin

Prints a single-use invitation link (email may not be set up yet). Refuses
to run once any board seat exists (decision D4, 4c-i): after that, the board
adds people through the portal, and sysadmin needs two board members.

    python manage.py bootstrap_first_officer --status

prints one word for infra/poc/install.sh (D94): ``none`` (no board seat yet:
run the bootstrap), ``invited`` (a board seat but nobody has signed in yet:
print_invitation_link gives a fresh link) or ``done``. No names or addresses.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts import services as accounts
from audit.models import Channel
from audit.services import record_event
from community import services
from community.models import BoardSeat, Office, Person, Role, RoleAssignment, SeatType, today
from core.policy import SYSTEM
from core.scoping import system_access


def bootstrap_status() -> str:
    """none: no board seat; invited: nobody has a login yet; done."""
    with system_access("bootstrap status"):
        if not BoardSeat.objects.exists():
            return "none"
        if not Person.objects.filter(user__isnull=False).exists():
            return "invited"
    return "done"


class Command(BaseCommand):
    help = "Create the first board officer and print their invitation link (run once)."

    def add_arguments(self, parser):
        parser.add_argument("--name")
        parser.add_argument("--email")
        parser.add_argument(
            "--status",
            action="store_true",
            help="Print none, invited or done (for install.sh) and change nothing.",
        )
        parser.add_argument("--office", default=Office.PRESIDENT, choices=Office.values)
        parser.add_argument("--seat-type", default=SeatType.OWNER_ELECTED, choices=SeatType.values)
        parser.add_argument("--citation", default="", help='e.g. "Bylaws §3.2"')
        parser.add_argument(
            "--also-sysadmin",
            action="store_true",
            help="Also make this person the initial sysadmin.",
        )

    def handle(self, *args, **opts):
        if opts["status"]:
            self.stdout.write(bootstrap_status())
            return
        if not opts["name"] or not opts["email"]:
            raise CommandError("Give --name and --email (or --status).")
        with system_access("bootstrap checks"):
            if BoardSeat.objects.exists():
                raise CommandError("A board seat already exists; add people in the portal.")
            if (
                opts["also_sysadmin"]
                and RoleAssignment.objects.filter(role=Role.SYSADMIN).active().exists()
            ):
                raise CommandError("A sysadmin already exists; use the two-person approval.")
        with transaction.atomic():
            person = services.record_person(
                display_name=opts["name"], email=opts["email"], actor=SYSTEM
            )
            services.record_board_seat(
                person=person,
                seat_type=opts["seat_type"],
                term_start=today(),
                citation=opts["citation"],
                actor=SYSTEM,
            )
            services.record_office(
                person=person, office=opts["office"], start_date=today(), actor=SYSTEM
            )
            if opts["also_sysadmin"]:
                services._record_role(
                    person=person,
                    role=Role.SYSADMIN,
                    start_date=today(),
                    appointed_by="",
                    citation="bootstrap",
                    actor=SYSTEM,
                    request=None,
                )
            invitation = accounts.create_invitation(person=person, actor=SYSTEM, send_email=False)
            link = accounts.issue_invitation_link(invitation)
            record_event(
                "system.bootstrap",
                channel=Channel.CLI,
                target=person,
                metadata={
                    "person_id": person.pk,
                    "sysadmin_person_id": person.pk if opts["also_sysadmin"] else None,
                },
            )
        self.stdout.write(self.style.SUCCESS("First officer created."))
        self.stdout.write(
            "Open this link to create the login (works once, expires in "
            f"{invitation.expires_at:%Y-%m-%d}). Do not share it:\n\n  {link}\n"
        )
