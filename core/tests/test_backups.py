"""Milestone 4: file-store export and import used by backups and the
restore drill."""

import io
import tarfile

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.core.management import CommandError, call_command

from core.backups import export_files, import_files


def _store(**files):
    store = InMemoryStorage()
    for name, data in files.items():
        store.save(name.replace("__", "/"), ContentFile(data))
    return store


def test_round_trip_keeps_names_and_contents():
    source = _store(attachments__a__plan_pdf=b"%PDF-1", documents__v1_bin=b"\x00\x01")
    buf = io.BytesIO()
    assert export_files(buf, storage=source) == 2
    target = InMemoryStorage()
    buf.seek(0)
    assert import_files(buf, storage=target) == 2
    with target.open("attachments/a/plan_pdf") as fh:
        assert fh.read() == b"%PDF-1"


def test_existing_files_are_kept_unless_overwrite():
    source = _store(x__f=b"new")
    target = _store(x__f=b"old")
    buf = io.BytesIO()
    export_files(buf, storage=source)
    buf.seek(0)
    assert import_files(buf, storage=target) == 0
    with target.open("x/f") as fh:
        assert fh.read() == b"old"


def test_unsafe_paths_in_an_archive_are_ignored():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in ("../escape", "/abs/path", "ok/file"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    buf.seek(0)
    target = InMemoryStorage()
    assert import_files(buf, storage=target) == 1
    assert target.exists("ok/file")


def test_push_backup_needs_a_bucket(settings):
    settings.BACKUP_S3_BUCKET = ""
    with pytest.raises(CommandError, match="BACKUP_S3_BUCKET"):
        call_command("push_backup", name="db.dump.enc")


def test_push_backup_refuses_odd_names(settings):
    settings.BACKUP_S3_BUCKET = "hoa-backups"
    with pytest.raises(CommandError, match="Bad backup name"):
        call_command("push_backup", name="../etc/passwd")


@pytest.mark.django_db
def test_overwrite_import_is_break_glass_and_imports_are_audited(tmp_path):
    from django.core.management import CommandError, call_command

    from audit.models import AuditEvent

    archive = tmp_path / "files.tar"
    call_command("export_files", out=str(archive))
    assert AuditEvent.objects.filter(action="backup.files_exported").exists()
    with pytest.raises(CommandError):
        call_command("import_files", source=str(archive), overwrite=True)
    call_command("import_files", source=str(archive))
    assert AuditEvent.objects.filter(action="backup.files_imported").exists()


def test_backup_scripts_keep_the_password_off_the_command_line():
    from pathlib import Path

    for name in ("backup.sh", "restore-drill.sh"):
        text = Path("infra/poc", name).read_text()
        assert 'PGPASSWORD="$OWNER_PW" "${' in text
        assert '-e PGPASSWORD="' not in text
    assert "HMAC-SHA256" in Path("infra/poc/backup.sh").read_text()
    assert "keyed checksum" in Path("infra/poc/restore-drill.sh").read_text()


def test_scanner_reloads_in_place():
    from pathlib import Path

    assert "ConcurrentDatabaseReload no" in Path("docker/clamav/clamd.conf").read_text()
    assert (
        "docker/clamav/clamd.conf:/etc/clamav/clamd.conf:ro"
        in Path("docker-compose.tunnel.yml").read_text()
    )


def test_r2_switch_keeps_the_local_store_optional():
    from pathlib import Path

    compose = Path("docker-compose.yml").read_text()
    assert 'profiles: ["local-store"]' in compose
    assert "required: false" in compose
    script = Path("infra/poc/move-files-to-r2.sh").read_text()
    assert "read -r -s" in script  # the secret is never echoed
    assert "check_storage" in script and "import_files" in script


@pytest.mark.django_db
def test_too_large_upload_gets_a_page_people_can_act_on(client, settings):
    settings.DOCUMENT_MAX_BYTES = 10
    response = client.post("/", data=b"x" * 1_200_000, content_type="application/octet-stream")
    assert response.status_code == 413 and b"smaller groups" in response.content
