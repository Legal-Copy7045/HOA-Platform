"""Audit server tasks the VM agent has finished (run by the agent).

    python manage.py record_host_tasks

Records ``system.task_finished`` (kind and state only) once per finished
task. Takes no arguments: nothing from the database reaches its command line.
"""

from django.core.management.base import BaseCommand

from core.host_tasks import record_finished


class Command(BaseCommand):
    help = "Record finished server tasks in the audit log."

    def handle(self, *args, **opts):
        self.stdout.write(f"Recorded {record_finished()} finished task(s).")
