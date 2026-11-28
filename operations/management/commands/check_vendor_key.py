"""Check the vendor document key (D79) without printing it: used by
infra/poc/set-vendor-key.sh after it saves the key. Encrypts and opens a test
value, counts stored vendor documents each key opens, and with --reencrypt
rewrites documents still under a fallback key under the current one (after a
rotation). Never prints key material, file names or contents."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the vendor document key; --reencrypt moves old documents to the current key."

    def add_arguments(self, parser):
        parser.add_argument("--reencrypt", action="store_true")

    def handle(self, *args, **options):
        # Says when the portal's key wins over .env (review EXP-006); it checks and
        # re-encrypts under the key in use, never the .env one alone.
        from core import credentials

        note = credentials.override_note("vendor_key")
        if note:
            self.stderr.write(note)
        return self._check(*args, **options)

    def _check(self, *args, **options):
        from operations import vendor_crypto, vendor_onboarding
        from operations.models import VendorFile

        if not vendor_crypto.configured():
            raise CommandError(
                "VENDOR_DOCUMENT_KEY is not set (at least 32 characters). Run "
                "sudo bash infra/poc/set-vendor-key.sh on the server."
            )
        probe = vendor_crypto.encrypt(b"vendor key check", "probe")
        if vendor_crypto.decrypt(probe, "probe") != b"vendor key check":
            raise CommandError("The vendor document key does not round-trip.")
        disposed = vendor_onboarding._disposed_file_ids()
        current = old = unreadable = 0
        for vf in VendorFile.objects.order_by("id"):
            if vf.pk in disposed or not vf.file.name:
                continue
            try:
                with vf.file.open("rb") as fh:
                    index = vendor_crypto.key_index(fh.read(), vf.storage_name)
            except (vendor_crypto.VendorKeyMismatch, OSError):
                unreadable += 1
                continue
            if index == 0:
                current += 1
            else:
                old += 1
        if options["reencrypt"] and old:
            moved = vendor_onboarding.reencrypt_all()
            self.stdout.write(f"Re-encrypted {moved} vendor document(s) under the current key.")
            current, old = current + moved, 0
        self.stdout.write(
            f"Vendor document key set. Documents: {current} under the current key, {old} under "
            f"an old key, {unreadable} unreadable."
        )
        if unreadable:
            raise CommandError(
                f"{unreadable} vendor document(s) can't be opened with any configured key: put "
                "the old key in VENDOR_DOCUMENT_KEY_FALLBACKS."
            )
