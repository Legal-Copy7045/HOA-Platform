"""Check the SMTP2Go API key on this server (infra/poc/set-smtp2go.sh runs
it after saving the key). Asks SMTP2Go for a usage summary, which sends no
email, and reads the newest page of activity (counts only). Prints the
region's host and what failed in plain words; never the key or an address."""

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Check the SMTP2Go API key with a harmless call (no email is sent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--server",
            action="store_true",
            help="Check the server's .env values, not what the portal uses (set-*.sh).",
        )

    def handle(self, *args, **options):
        # With --server (the set-*.sh scripts) this checks what was just written
        # to .env; either way it says when the portal's values win (EXP-006).
        from core import credentials

        note = credentials.override_note("smtp2go")
        if note:
            self.stderr.write(note)
        if not options.get("server"):
            return self._check(*args, **options)
        with credentials.server_only("smtp2go"):
            return self._check(*args, **options)

    def _check(self, *args, **options):
        from comms import smtp2go

        if not smtp2go.selected():
            raise CommandError("EMAIL_PROVIDER is not smtp2go: email goes through SMTP.")
        cfg = smtp2go.config()
        if not cfg.configured:
            raise CommandError(cfg.problem)
        try:
            smtp2go.check_key()
        except smtp2go.Smtp2goError as exc:
            raise CommandError(f"The key check failed: {exc.words}.") from exc
        now = timezone.now()
        try:
            events, _ = smtp2go.search_activity(now - datetime.timedelta(hours=1), now)
        except smtp2go.Smtp2goError as exc:
            raise CommandError(
                f"The key works for sending checks, but reading activity failed: {exc.words}. "
                "Give the key the Activity permission in SMTP2Go."
            ) from exc
        self.stdout.write(
            f"OK: SMTP2Go ({cfg.host}) accepted the key; {len(events)} delivery report"
            f"{'' if len(events) == 1 else 's'} in the last hour."
        )
