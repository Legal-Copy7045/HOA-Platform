"""Check the Teller set-up on this server without calling Teller (used by
infra/poc/set-teller.sh after it saves the settings). Never prints a secret."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the Teller settings: application ID, environment, certificate and key."

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

        note = credentials.override_note("teller")
        if note:
            self.stderr.write(note)
        if not options.get("server"):
            return self._check(*args, **options)
        with credentials.server_only("teller"):
            return self._check(*args, **options)

    def _check(self, *args, **options):
        from ledger.bankfeeds import teller, tokens
        from ledger.bankfeeds.base import BankFeedError

        try:
            env = teller.environment()
        except BankFeedError as exc:
            raise CommandError(str(exc)) from exc
        problems = []
        if not teller.application_id():
            problems.append("the Teller application ID is missing")
        if not tokens.configured():
            problems.append("BANK_FEED_TOKEN_KEY is missing")
        if env != "sandbox" and not teller.certificate_configured():
            problems.append("the Teller certificate and key are missing")
        if not problems:
            try:
                teller.ssl_context()
            except BankFeedError as exc:
                problems.append(str(exc).rstrip("."))
        if problems:
            raise CommandError("Teller is not ready: " + "; ".join(problems) + ".")
        self.stdout.write(
            f"Teller set up ({env}). Connect a bank from Finance -> Accounting -> Bank feed."
        )
