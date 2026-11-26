"""Fictional-data POC only: attach an existing person (e.g. you, the first
officer) to one demo lot as a joint owner.

On a real deployment your own lot comes from the register (import_register),
like every owner's. On a POC filled by seed_demo the lots belong to fictional
people, so this gives your login a home there to use the homeowner side.
Refused unless demo seeding is allowed and the lot belongs to the demo
community. Recorded through the normal ownership service (audited, as SYSTEM).
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from community import services
from community.models import Lot, Ownership, OwnershipType, Person, today
from core.policy import SYSTEM
from core.scoping import system_access


class Command(BaseCommand):
    help = "POC only: add a person as joint owner of a demo lot, for testing the owner side."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Email on your person record.")
        parser.add_argument("--lot", type=int, required=True, help="Demo lot number (1-44).")

    def handle(self, *args, email, lot, **opts):
        if not settings.SEED_DEMO_ALLOWED:
            raise CommandError("Refusing: only on a demo deployment (SEED_DEMO_ALLOWED=true).")
        with system_access("demo_make_me_owner checks"):
            target = Lot.objects.filter(number=lot).first()
            if target is None:
                raise CommandError(f"No lot {lot}.")
            current = Ownership.objects.active().filter(lot=target).select_related("person")
            if not current or any(not o.person.display_name.endswith("(demo)") for o in current):
                raise CommandError(
                    "Refusing: that lot's owners are not demo people. Use a lot created by "
                    "seed_demo."
                )
            person = Person.objects.filter(email__iexact=email.strip()).first()
            if person is None:
                raise CommandError("No person has that email. Use the one you were invited with.")
            if Ownership.objects.active().filter(lot=target, person=person).exists():
                raise CommandError("You already own that lot.")
        services.record_ownership(
            person=person,
            lot=target,
            start_date=today(),
            ownership_type=OwnershipType.JOINT,
            actor=SYSTEM,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: you are now a joint owner of demo lot {lot}. Sign out and back in; "
                "the My home section of the menu shows your own home."
            )
        )
