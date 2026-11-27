"""Check the Teller set-up on this server without calling Teller (used by
infra/poc/set-teller.sh after it saves the settings). Never prints a secret."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the Teller settings: application ID, environment, certificate and key."

    def handle(self, *args, **options):
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
