"""After a demo reseed, give the person who asked for it a board seat and a
demo home in the new data (decision D53).

    python manage.py demo_attach_requester --task <server task id>

Run by infra/poc/reseed-demo.sh after seed_demo, only when the reseed was
requested with "After reseeding, make me a board member and the owner of a
demo home in the new data" ticked (``HostTask.attach_requester``). Refused
unless DEPLOYMENT_STAGE=poc and SEED_DEMO_ALLOWED=true, so it never applies
in production. The requester is a kept (non-demo) login: they get an active
declarant-appointed board seat from today and become joint owner of the
first demo home that has a single demo owner. Recorded as SYSTEM through the
community services and audited (``system.demo_requester_attached``, ids only).
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from community import demo
from community.demo.attach import attach_requester


class Command(BaseCommand):
    help = "POC only: make the reseed's requester a board member and a demo home owner."

    def add_arguments(self, parser):
        parser.add_argument("--task", type=int, required=True, help="The reseed server task id.")

    def handle(self, *args, task, **opts):
        if not (settings.SEED_DEMO_ALLOWED and settings.DEPLOYMENT_STAGE == "poc"):
            raise CommandError(
                "Refusing: only on the POC (DEPLOYMENT_STAGE=poc, SEED_DEMO_ALLOWED=true)."
            )
        try:
            with transaction.atomic():
                result = attach_requester(task)
        except demo.DemoError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Requester attached: board seat, joint owner of demo home {result['lot_number']}."
        )
