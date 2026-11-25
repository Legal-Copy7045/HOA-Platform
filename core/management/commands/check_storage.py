"""Check the file store works: write, read back and delete a small probe file.

    python manage.py check_storage

Prints OK, or the store's error code (no secrets, no file contents).
"""

import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Write, read and delete a probe file in the configured file store."

    def handle(self, *args, **options):
        name = f"healthcheck/{uuid.uuid4().hex}.txt"
        try:
            saved = default_storage.save(name, ContentFile(b"ok"))
            with default_storage.open(saved, "rb") as fh:
                if fh.read() != b"ok":
                    raise CommandError("The file store returned different content.")
            default_storage.delete(saved)
        except CommandError:
            raise
        except Exception as exc:  # report the store's code, nothing else
            response = getattr(exc, "response", None) or {}
            code = (response.get("Error") or {}).get("Code") or type(exc).__name__
            raise CommandError(f"File store check failed: {code}") from exc
        self.stdout.write("File store OK: write, read and delete all work.")
