"""Remove the values saved on Keys and connections for one service, from the
server (review EXP-006, 0.44.0): the server's .env applies again at once.

For the server operator, who controls .env already (D93): after a sysadmin
login was misused, or a wrong value was saved in the portal, the set-*.sh
script's fix only takes effect once the portal's value is gone. Audited
(``system.credential_cleared`` with ``from_server``), the board is told
through the server's own email settings, and the vendor document key is
still refused while vendor documents are encrypted under it. Never prints a
value."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Remove the portal's saved values for a service (the server's .env applies again)."

    def add_arguments(self, parser):
        parser.add_argument("service", help="e.g. smtp2go, smtp, stripe, teller, usps, bounce, ai")

    def handle(self, *args, **options):
        from core import credentials
        from core.policy import SYSTEM

        key = options["service"]
        if key not in credentials.BY_KEY:
            raise CommandError(
                f"Unknown service {key!r}. One of: {', '.join(sorted(credentials.BY_KEY))}."
            )
        try:
            change = credentials.clear(SYSTEM, key, from_server=True)
        except credentials.CredentialError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Removed the portal's values for {credentials.BY_KEY[key].label} (change "
            f"#{change.pk}); the server's .env applies now. The board was told."
        )
