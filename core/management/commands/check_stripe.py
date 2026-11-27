"""Check the Stripe key on this server with two read-only calls (used by
infra/poc/set-stripe-key.sh after it saves the key). Prints the mode and
what is missing; never prints the key."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the Stripe key: reachable, test or live mode, read rights in place."

    def handle(self, *args, **options):
        from payments import providers, stripe_api

        try:
            stripe_api.secret_key()
        except Exception as exc:
            problem = providers.config().problem  # never the key itself
            raise CommandError(
                f"No usable Stripe key is set (STRIPE_SECRET_KEY). {problem}".strip()
            ) from exc
        checks = [
            ("balance", "/v1/balance", None),
            ("checkout_sessions", "/v1/checkout/sessions", {"limit": 1}),
        ]
        mode = ""
        problems = []
        for what, path, params in checks:
            try:
                data = stripe_api.get(path, params, what=f"check_{what}")
            except stripe_api.StripeError as exc:
                if exc.status == 0:
                    raise CommandError("Stripe could not be reached from this server.") from exc
                if exc.status == 401:
                    raise CommandError("Stripe refused the key (wrong or revoked).") from exc
                problems.append(f"{what} (HTTP {exc.status})")
                continue
            if what == "balance":
                mode = "live" if data.get("livemode") else "test"
        if problems:
            raise CommandError(
                "The key works but lacks read rights for: "
                + ", ".join(problems)
                + '. Add them to the restricted key (docs/runbook.md, "Online payments").'
            )
        self.stdout.write(f"Stripe key OK ({mode} mode).")
