"""Fictional demo data, from one of the datasets in ``community/demo``.

    python manage.py seed_demo                          # oak_creek, the full demo
    python manage.py seed_demo --dataset fresh_start    # just handed over, no history
    python manage.py seed_demo --list-datasets          # names, one per line

Everything is invented (names end in "(demo)", emails use example.com).
Refuses unless SEED_DEMO_ALLOWED=true (production settings force it off
outside DEPLOYMENT_STAGE=poc), and refuses if lots already exist. Records
which dataset was loaded (core.DemoSeed, shown on Settings -> Demo data) and
audits it. To replace the data on the POC, a sysadmin uses Settings -> Demo
data, which runs infra/poc/reseed-demo.sh through the server agent (D53).
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from community import demo
from community.models import Lot
from core.scoping import system_access


class Command(BaseCommand):
    help = "Create fictional demo data (never in production)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset",
            default=demo.DEFAULT,
            choices=demo.names(),
            help=f"Which demo community to create (default {demo.DEFAULT}).",
        )
        parser.add_argument(
            "--list-datasets",
            action="store_true",
            help="Print the dataset names, one per line, and stop.",
        )

    def handle(self, *args, dataset, list_datasets, **opts):
        if list_datasets:
            for name in demo.names():
                self.stdout.write(name)
            return
        if not settings.SEED_DEMO_ALLOWED:
            raise CommandError("Refusing: set SEED_DEMO_ALLOWED=true (never in production).")
        with system_access("seed_demo"):
            if Lot.objects.exists():
                raise CommandError("Lots already exist; seed_demo only fills an empty register.")
        result = demo.seed(dataset)
        _record(dataset)
        self.stdout.write(self.style.SUCCESS(result.summary))
        if result.logins:
            self.stdout.write(
                "\nDemo logins (fictional, no password: they only author demo records; "
                "sign in with your own login):"
            )
            for email, who in result.logins:
                self.stdout.write(f"  {email}  ({who})")
        if result.invite_first:
            self.stdout.write(
                "\nPeople to invite first (Association -> Invitations and sign-ups; "
                "with EMAIL_REDIRECT_ALL_TO set they arrive in one test inbox):"
            )
            for email, who in result.invite_first:
                self.stdout.write(f"  {email}  ({who})")


def _record(dataset: str) -> None:
    from audit.models import Channel
    from audit.services import record_event
    from core.models import DemoSeed

    seeded = DemoSeed.objects.create(dataset=dataset)
    record_event(
        "system.demo_seeded",
        actor=None,
        channel=Channel.CLI,
        target=seeded,
        metadata={"dataset": dataset},
    )
