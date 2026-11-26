"""Check the USPS Informed Visibility login on this server (used by
infra/poc/set-usps.sh after it saves the login). Signs in only: no piece is
looked up. Prints the environment and what failed; never the password, the
user name or a token."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the USPS Informed Visibility login: sign in only, no piece query."

    def handle(self, *args, **options):
        from comms import usps_iv

        cfg = usps_iv.config()
        if not cfg.configured:
            raise CommandError(f"USPS tracking is not set up: {cfg.problem}")
        try:
            usps_iv.check_login()
        except usps_iv.UspsAuthError as exc:
            raise CommandError(
                f"USPS refused the sign-in (HTTP {exc.status}): check the Business Customer "
                "Gateway user name and password, and that this user has IV-MTR access "
                "(given by the BSA or a BSA Delegate)."
            ) from exc
        except usps_iv.UspsRateLimited as exc:
            raise CommandError("USPS said too many requests; try again in a minute.") from exc
        except usps_iv.UspsError as exc:
            if exc.status == 0:
                raise CommandError(
                    f"USPS ({cfg.auth_host}) could not be reached from this server."
                ) from exc
            raise CommandError(f"USPS answered with an error (HTTP {exc.status}).") from exc
        finally:
            usps_iv.forget_token()
        self.stdout.write(f"OK: signed in to {cfg.label} ({cfg.auth_host}).")
