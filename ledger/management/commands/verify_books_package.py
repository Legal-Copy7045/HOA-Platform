"""Check a year-end package (D71): every file's SHA-256 against its manifest,
and the manifest's HMAC against the server's key (BOOKS_PACKAGE_KEY, else the
key derived from the Django secret). Reads the .zip from a path; changes
nothing."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verify a year-end package .zip (file checksums and the manifest signature)."

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, *args, **opts):
        from ledger.books.package import verify

        path = Path(opts["path"])
        if not path.is_file():
            raise CommandError("No such file.")
        problems = verify(path.read_bytes())
        if problems:
            for p in problems:
                self.stderr.write(p)
            raise CommandError("The package does NOT verify.")
        self.stdout.write("The package verifies: every file matches and the signature is good.")
