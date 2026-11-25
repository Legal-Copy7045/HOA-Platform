"""Run the health checks; with --alert, email the sysadmins about changes.

    python manage.py health_check            # print
    python manage.py health_check --alert    # print and alert (the VM timer)

Exit code 1 if any check fails.
"""

from django.core.management.base import BaseCommand, CommandError

from core.health import alert, run_checks


class Command(BaseCommand):
    help = "Check database, worker, disk, file store, scanner and errors."

    def add_arguments(self, parser):
        parser.add_argument("--alert", action="store_true")

    def handle(self, *args, **opts):
        results = run_checks()
        for r in results:
            self.stdout.write(f"{'OK  ' if r.ok else 'FAIL'}  {r.label}: {r.detail}")
        if opts["alert"]:
            alert(results)
        if not all(r.ok for r in results):
            raise CommandError("Some checks failed.")
