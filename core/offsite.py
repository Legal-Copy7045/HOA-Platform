"""Off-site backup bucket (e.g. Cloudflare R2 with a bucket lock).

Backups reach the bucket already encrypted on the VM (infra/poc/backup.sh),
under ``backups/<stamp>-<file>``. The bucket is meant to carry a lock rule on
``backups/`` so nothing there can be deleted or overwritten for a set number
of days, even with valid keys; ``lock_probe`` proves that. Errors report the
store's error code only, never messages, keys or URLs.
"""

from __future__ import annotations

import contextlib
import re
import uuid

from django.conf import settings

PREFIX = "backups/"
NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,120}")
# backup.sh names files "<YYYY-MM-DDTHHMMZ>-<file>".
STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{4}Z)-")
BACKUP_FILES = ("db.dump.enc", "files.tar.enc", "SHA256SUMS", "HMAC-SHA256")


class OffsiteError(Exception):
    pass


def configured() -> bool:
    return bool(settings.BACKUP_S3_BUCKET)


def _client():
    if not configured():
        raise OffsiteError("No off-site bucket configured (BACKUP_S3_BUCKET).")
    import boto3
    from botocore.config import Config

    return boto3.client(
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


def _code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code") or type(exc).__name__


def _check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise OffsiteError("Bad backup name.")
    return PREFIX + name


def push(name: str, stream) -> int:
    """Upload one file; returns its size as the bucket reports it."""
    key = _check_name(name)
    client = _client()
    try:
        client.upload_fileobj(stream, settings.BACKUP_S3_BUCKET, key)
        return client.head_object(Bucket=settings.BACKUP_S3_BUCKET, Key=key)["ContentLength"]
    except Exception as exc:
        raise OffsiteError(f"Off-site upload failed: {_code(exc)}") from exc


def stamps() -> list[str]:
    """Stamps of complete backups (all four files present), oldest first."""
    client = _client()
    found: dict[str, set[str]] = {}
    try:
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=settings.BACKUP_S3_BUCKET, Prefix=PREFIX
        ):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(PREFIX) :]
                match = STAMP_RE.match(name)
                if match:
                    found.setdefault(match.group(1), set()).add(name[match.end() :])
    except Exception as exc:
        raise OffsiteError(f"Off-site listing failed: {_code(exc)}") from exc
    return sorted(s for s, files in found.items() if set(BACKUP_FILES) <= files)


def fetch(name: str, out) -> None:
    key = _check_name(name)
    try:
        _client().download_fileobj(settings.BACKUP_S3_BUCKET, key, out)
    except Exception as exc:
        raise OffsiteError(f"Off-site download failed: {_code(exc)}") from exc


def lock_probe() -> bool:
    """Upload a tiny file under backups/, try to delete it, and report whether
    it survived (True: the bucket lock protects backups). The probe file
    stays until the lock lets it go; it holds no data."""
    client = _client()
    bucket = settings.BACKUP_S3_BUCKET
    key = f"{PREFIX}lock-probe-{uuid.uuid4().hex}.txt"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"lock probe: no data\n")
    except Exception as exc:
        raise OffsiteError(f"Could not write to the bucket: {_code(exc)}") from exc
    # A refusal is what a lock should do; either way, check what survived.
    with contextlib.suppress(Exception):
        client.delete_object(Bucket=bucket, Key=key)
    try:
        client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise OffsiteError(f"Could not check the probe file: {_code(exc)}") from exc
    return True
