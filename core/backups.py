"""File-store export and import for backups (Milestone 4).

The database is dumped with pg_dump by infra/poc/backup.sh; uploaded files
live in the object store, which pg_dump cannot see. These helpers stream
every stored object into a tar archive (and back), through the app's own
storage backend, so the same code works on SeaweedFS, R2 or any S3 store.
Only object names and contents go into the archive; nothing is logged.
"""

from __future__ import annotations

import io
import tarfile

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

SKIP_PREFIXES = ("healthcheck/",)


def _walk(storage, path=""):
    dirs, files = storage.listdir(path)
    for name in files:
        key = f"{path}/{name}" if path else name
        if not key.startswith(SKIP_PREFIXES):
            yield key
    for d in dirs:
        yield from _walk(storage, f"{path}/{d}" if path else d)


def referenced_files() -> set[str]:
    """Every storage key a FileField in the database points at."""
    from django.apps import apps
    from django.db import models

    from core.scoping import system_access

    keys: set[str] = set()
    with system_access("list stored files"):
        for model in apps.get_models():
            for field in model._meta.concrete_fields:
                if isinstance(field, models.FileField):
                    values = model._base_manager.exclude(**{field.name: ""}).values_list(
                        field.name, flat=True
                    )
                    keys.update(v for v in values if v)
    return keys


def unreferenced_files(storage=None) -> list[str]:
    """Stored objects no record refers to (after a demo reseed, D53)."""
    storage = storage or default_storage
    keep = referenced_files()
    return [key for key in _walk(storage) if key not in keep]


def export_files(fileobj, storage=None) -> int:
    """Write every stored object to ``fileobj`` as a tar stream. Returns count."""
    storage = storage or default_storage
    count = 0
    with tarfile.open(fileobj=fileobj, mode="w|") as tar:
        for key in _walk(storage):
            with storage.open(key, "rb") as fh:
                data = fh.read()
            info = tarfile.TarInfo(name=key)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            count += 1
    return count


def import_files(fileobj, storage=None, *, overwrite: bool = False) -> int:
    """Restore objects from a tar stream made by export_files."""
    storage = storage or default_storage
    count = 0
    with tarfile.open(fileobj=fileobj, mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = member.name
            # Never trust paths from an archive (no absolute paths, no "..").
            if name.startswith("/") or ".." in name.split("/"):
                continue
            if storage.exists(name):
                if not overwrite:
                    continue
                storage.delete(name)
            data = tar.extractfile(member).read()
            saved = storage.save(name, ContentFile(data))
            if saved != name:  # the backend renamed it: keep keys exact
                storage.delete(saved)
                raise RuntimeError("The file store renamed a restored object.")
            count += 1
    return count
