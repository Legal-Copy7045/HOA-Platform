"""Off-site backups: upload, listing, download, the bucket-lock probe, and the
Backups health check."""

import datetime
import io
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from core import health, offsite
from core.models import Heartbeat

POC = Path("infra/poc")


class _Missing(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "404"}}


class _Refused(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "AccessDenied"}}


class FakeBucket:
    """Enough of a boto3 S3 client for core.offsite."""

    def __init__(self, locked=True):
        self.objects: dict[str, bytes] = {}
        self.locked = locked

    def upload_fileobj(self, stream, bucket, key):
        self.objects[key] = stream.read()

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _Missing()
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        if self.locked:
            raise _Refused()
        self.objects.pop(Key, None)

    def download_fileobj(self, bucket, key, out):
        if key not in self.objects:
            raise _Missing()
        out.write(self.objects[key])

    def get_paginator(self, name):
        fake = self

        class Pages:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in fake.objects if k.startswith(Prefix)]}

        return Pages()


@pytest.fixture
def bucket(settings, monkeypatch):
    settings.BACKUP_S3_BUCKET = "oak-creek-backups"
    fake = FakeBucket()
    monkeypatch.setattr(offsite, "_client", lambda: fake)
    return fake


def test_upload_lands_under_backups_and_reports_the_stored_size(bucket):
    assert offsite.push("2026-11-26T0230Z-db.dump.enc", io.BytesIO(b"cipher")) == 6
    assert bucket.objects == {"backups/2026-11-26T0230Z-db.dump.enc": b"cipher"}
    with pytest.raises(offsite.OffsiteError, match="Bad backup name"):
        offsite.push("../escape", io.BytesIO(b"x"))


def test_only_complete_backups_are_listed_oldest_first(bucket):
    for stamp in ("2026-11-26T0230Z", "2026-11-25T0230Z"):
        for f in offsite.BACKUP_FILES:
            bucket.objects[f"backups/{stamp}-{f}"] = b"x"
    bucket.objects["backups/2026-11-27T0230Z-db.dump.enc"] = b"x"  # interrupted upload
    bucket.objects["backups/lock-probe-abc.txt"] = b"x"
    assert offsite.stamps() == ["2026-11-25T0230Z", "2026-11-26T0230Z"]


def test_pull_backup_lists_and_streams(bucket, capsys):
    for f in offsite.BACKUP_FILES:
        bucket.objects[f"backups/2026-11-26T0230Z-{f}"] = b"cipher"
    call_command("pull_backup", "--list")
    assert capsys.readouterr().out.strip() == "2026-11-26T0230Z"
    out = io.BytesIO()
    offsite.fetch("2026-11-26T0230Z-db.dump.enc", out)
    assert out.getvalue() == b"cipher"
    with pytest.raises(CommandError, match="Bad backup name"):
        call_command("pull_backup", "--name", "../../etc/shadow")


@pytest.mark.django_db
def test_lock_check_passes_only_when_the_bucket_refuses_a_delete(bucket):
    from audit.models import AuditEvent

    call_command("check_offsite_lock", stdout=io.StringIO())
    assert Heartbeat.objects.filter(name="offsite_lock").exists()
    assert AuditEvent.objects.get(action="system.offsite_lock_checked").metadata == {"locked": True}
    bucket.locked = False
    with pytest.raises(CommandError, match="Bucket lock rules"):
        call_command("check_offsite_lock")
    assert not Heartbeat.objects.filter(name="offsite_lock").exists()
    assert AuditEvent.objects.filter(
        action="system.offsite_lock_checked", metadata__locked=False
    ).exists()


def _ago(hours):
    return timezone.now() - datetime.timedelta(hours=hours)


@pytest.mark.django_db
def test_backups_health_check(settings):
    settings.DEPLOYMENT_STAGE = "poc"
    settings.BACKUP_S3_BUCKET = ""
    ok, detail = health._backups()
    assert not ok and "Server tasks" in detail
    call_command("backup_heartbeat", "--kind", "local")
    ok, detail = health._backups()
    # POC/local: no off-site copy is allowed, but said plainly (D57).
    assert ok and "Not production-ready: no off-site copy" in detail
    assert "restore drill never run" in detail
    settings.BACKUP_S3_BUCKET = "oak-creek-backups"
    ok, detail = health._backups()
    assert not ok and "off-site copy never" in detail
    call_command("backup_heartbeat", "--kind", "offsite")
    ok, detail = health._backups()
    assert ok and "lock not verified" in detail
    Heartbeat.objects.create(name="offsite_lock", at=timezone.now())
    assert "lock verified" in health._backups()[1]
    Heartbeat.objects.filter(name="backup_offsite").update(at=_ago(40))
    ok, detail = health._backups()
    assert not ok and "off-site copy 40 hours ago" in detail
    Heartbeat.objects.filter(name="backup_local").update(at=_ago(50))
    assert health._backups() == (False, "Last backup 50 hours ago")


def _fresh_backups(offsite=True):
    call_command("backup_heartbeat", "--kind", "local")
    if offsite:
        call_command("backup_heartbeat", "--kind", "offsite")
    Heartbeat.objects.create(name="offsite_lock", at=timezone.now())


@pytest.mark.django_db
def test_production_needs_an_offsite_copy(settings):
    settings.DEPLOYMENT_STAGE = "production"
    settings.BACKUP_S3_BUCKET = ""
    _fresh_backups(offsite=False)
    call_command("backup_heartbeat", "--kind", "restore_drill")
    ok, detail = health._backups()
    assert not ok and "no off-site copy is set up" in detail and "production" in detail


@pytest.mark.django_db
def test_production_fails_on_an_offsite_copy_older_than_36_hours(settings):
    settings.DEPLOYMENT_STAGE = "production"
    settings.BACKUP_S3_BUCKET = "oak-creek-backups"
    _fresh_backups()
    call_command("backup_heartbeat", "--kind", "restore_drill")
    assert health._backups()[0]
    Heartbeat.objects.filter(name="backup_offsite").update(at=_ago(37))
    ok, detail = health._backups()
    assert not ok and "off-site copy 37 hours ago" in detail


@pytest.mark.django_db
def test_production_needs_a_restore_drill_every_90_days(settings):
    settings.DEPLOYMENT_STAGE = "production"
    settings.BACKUP_S3_BUCKET = "oak-creek-backups"
    _fresh_backups()
    ok, detail = health._backups()
    assert not ok and "restore drill never run" in detail and "every 90 days" in detail
    call_command("backup_heartbeat", "--kind", "restore_drill")
    ok, detail = health._backups()
    assert ok and "restore drill 0 days ago" in detail
    Heartbeat.objects.filter(name="restore_drill").update(at=_ago(24 * 89))
    assert health._backups()[0]
    Heartbeat.objects.filter(name="restore_drill").update(at=_ago(24 * 91))
    ok, detail = health._backups()
    assert not ok and "restore drill 91 days ago" in detail


@pytest.mark.django_db
def test_poc_shows_an_old_restore_drill_but_stays_ok(settings):
    settings.DEPLOYMENT_STAGE = "poc"
    settings.BACKUP_S3_BUCKET = "oak-creek-backups"
    _fresh_backups()
    ok, detail = health._backups()
    assert ok and "restore drill never run" in detail
    assert "Not production-ready: no restore drill in 90 days" in detail


def test_the_drill_records_itself_on_the_live_portal_only_when_signed():
    drill = (POC / "restore-drill.sh").read_text()
    record = drill.index("backup_heartbeat --kind restore_drill")
    # Only after every check passed, and only for a backup with the keyed checksum.
    assert drill.index("Restore drill passed") < record
    assert drill.rindex("${#VERIFY_ARGS[@]} -eq 0", 0, record) > drill.index("Restore drill passed")
    assert "-f docker-compose.tunnel.yml" in drill[drill.rindex("LIVE=(", 0, record) : record]


def test_scripts_record_backups_and_drill_from_offsite():
    backup = (POC / "backup.sh").read_text()
    assert "backup_heartbeat --kind local" in backup and "backup_heartbeat --kind offsite" in backup
    # The off-site heartbeat comes only after every file uploaded.
    assert backup.index("push_backup") < backup.index("backup_heartbeat --kind offsite")
    drill = (POC / "restore-drill.sh").read_text()
    assert "--from-offsite" in drill and "pull_backup --list" in drill
    # Downloaded backups are checked like local ones, before anything is decrypted.
    assert drill.index("pull_backup --name") < drill.index('backup_mac.py" verify')
    setup = (POC / "setup-offsite-backups.sh").read_text()
    assert 'read -r -s -p "R2 Secret Access Key' in setup
    assert "set_env .env BACKUP_S3_SECRET_ACCESS_KEY" in setup
    assert "check_offsite_lock" in setup and "Use a separate bucket" in setup


def test_the_version_label_goes_off_site_and_is_shown_sanitised():
    backup = (POC / "backup.sh").read_text()
    assert "HMAC-SHA256 APP_VERSION; do" in backup
    drill = (POC / "restore-drill.sh").read_text()
    assert '--name "$REMOTE-APP_VERSION"' in drill and "|| rm -f" in drill
    assert "tr -cd 'a-z0-9.-'" in drill and "${VERSION:-unknown}" in drill
    # A backup without the label is still complete.
    assert "APP_VERSION" not in offsite.BACKUP_FILES
