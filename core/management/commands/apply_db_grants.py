from django.core.management.base import BaseCommand

from core.db_roles import apply_runtime_grants


class Command(BaseCommand):
    help = "Re-apply runtime role grants (normally done automatically after migrate)."

    def handle(self, *args, **options) -> None:
        if apply_runtime_grants():
            self.stdout.write("Runtime grants applied.")
        else:
            self.stdout.write("DB_APP_ROLE not set; nothing to do.")
