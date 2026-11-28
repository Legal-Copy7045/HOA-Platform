"""Record the off-site backup points for Backups -> Restore from a backup
(decision D98). Run by the VM agent every 15 minutes and after each server
task; takes no value from the database.

    python manage.py backup_inventory --offsite

The local backup points are written by the agent itself (it reads the
server's backup folder). Errors keep the last good list and show the store's
error code only.
"""

from django.core.management.base import BaseCommand

from core import backup_points, offsite


class Command(BaseCommand):
    help = "Record the complete off-site backups (stamp, size, release) for the restore page."

    def add_arguments(self, parser):
        parser.add_argument("--offsite", action="store_true", required=True)

    def handle(self, *args, **opts):
        if not offsite.configured():
            self.stdout.write("No off-site bucket set up: nothing to list.")
            return
        try:
            rows = offsite.inventory(backup_points.known_offsite_versions())
        except offsite.OffsiteError as exc:
            backup_points.save_offsite(None, str(exc))
            self.stdout.write(f"Off-site list not refreshed: {exc}")
            return
        backup_points.save_offsite(rows)
        self.stdout.write(f"Recorded {len(rows)} off-site backup point(s).")
