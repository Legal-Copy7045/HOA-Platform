"""Copy one (already encrypted) backup file to the off-site bucket.

    cat db-2027-01-01.dump.enc | python manage.py push_backup --name db-2027-01-01.dump.enc

The off-site bucket is separate from the portal's own file store and owned by
the HOA (e.g. Cloudflare R2). Its credentials come from BACKUP_S3_* in .env;
files arrive already encrypted on the VM, so the bucket never sees plain data.
"""

import re
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Upload an encrypted backup file from stdin to the off-site bucket."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)

    def handle(self, *args, name, **opts):
        if not settings.BACKUP_S3_BUCKET:
            raise CommandError("No off-site bucket configured (BACKUP_S3_BUCKET).")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name):
            raise CommandError("Bad backup name.")
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.BACKUP_S3_ENDPOINT_URL or None,
            aws_access_key_id=settings.BACKUP_S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.BACKUP_S3_SECRET_ACCESS_KEY,
            region_name=settings.BACKUP_S3_REGION,
            config=Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        try:
            client.upload_fileobj(sys.stdin.buffer, settings.BACKUP_S3_BUCKET, f"backups/{name}")
        except Exception as exc:  # report the store's code only
            response = getattr(exc, "response", None) or {}
            code = (response.get("Error") or {}).get("Code") or type(exc).__name__
            raise CommandError(f"Off-site upload failed: {code}") from exc
        self.stdout.write(f"Uploaded {name} off-site.")
