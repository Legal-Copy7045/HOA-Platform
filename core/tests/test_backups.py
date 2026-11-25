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
