from django.core.management.base import BaseCommand

from core.policy import thinly_covered_actions


class Command(BaseCommand):
    help = "List actions fewer than two people can perform (decision D4)."

    def handle(self, *args, **options) -> None:
        thin = thinly_covered_actions()
        if not thin:
            self.stdout.write("Every action can be performed by at least two people.")
            return
        for action, count in sorted(thin.items()):
            self.stdout.write(self.style.WARNING(f"{action}: {count} person(s)"))
