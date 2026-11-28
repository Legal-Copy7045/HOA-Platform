"""Check the bounce mailbox login on this server (used by
infra/poc/set-bounce-mailbox.sh after it saves the login, D74). Signs in over
IMAP over TLS and opens the folder only: no message is read or marked.
Prints the host and what failed; never the password, the user name or any
message."""

from django.core.management.base import BaseCommand, CommandError

WORDS = {
    "not_configured": "The bounce mailbox is not set up: {problem}",
    "auth": "The mailbox refused the sign-in: check the user name and the (app) password.",
    "connect": "{host}:{port} could not be reached from this server (host, port, firewall).",
    "tls": "{host}'s certificate did not check out (wrong host name, or not IMAP over TLS).",
    "folder": "Signed in, but the folder to read was not found: check BOUNCE_IMAP_FOLDER.",
    "protocol": "{host} answered in a way the portal did not expect (is it an IMAP server?).",
}


class Command(BaseCommand):
    help = "Check the bounce mailbox login: sign in and open the folder only."

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

        note = credentials.override_note("bounce")
        if note:
            self.stderr.write(note)
        if not options.get("server"):
            return self._check(*args, **options)
        with credentials.server_only("bounce"):
            return self._check(*args, **options)

    def _check(self, *args, **options):
        from comms import bounces

        cfg = bounces.config()
        if cfg.configured and not cfg.password:
            raise CommandError(
                "The mailbox password is given to the worker container only: run this check "
                "there (docker compose ... exec worker python manage.py check_bounce_mailbox)."
            )
        try:
            bounces.check_login()
        except bounces.MailboxError as exc:
            text = WORDS.get(exc.code, "The check failed ({code}).")
            raise CommandError(
                text.format(problem=cfg.problem, host=cfg.host, port=cfg.port, code=exc.code)
            ) from exc
        self.stdout.write(f"OK: signed in to {cfg.host}:{cfg.port} over TLS and opened the folder.")
