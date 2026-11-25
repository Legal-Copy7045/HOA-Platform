"""Upload checks: treat every file as hostile (CLAUDE.md).

Allowed types are checked by extension *and* by the file's first bytes, so a
renamed executable is refused. Stored content type always comes from this
table, never from the browser.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass

from django.conf import settings


class UploadRejected(ValueError):
    pass


@dataclass(frozen=True)
class FileType:
    content_type: str
    magic: tuple[bytes, ...] = ()
    zip_member: str | None = None  # Office files are zip archives with this entry
    text: bool = False


TYPES: dict[str, FileType] = {
    "pdf": FileType("application/pdf", (b"%PDF-",)),
    "png": FileType("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": FileType("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": FileType("image/jpeg", (b"\xff\xd8\xff",)),
    "docx": FileType(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        (b"PK\x03\x04",),
        zip_member="word/document.xml",
    ),
    "xlsx": FileType(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        (b"PK\x03\x04",),
        zip_member="xl/workbook.xml",
    ),
    "txt": FileType("text/plain", text=True),
    "csv": FileType("text/csv", text=True),
}


def check_upload(uploaded, *, allowed: set[str] | None = None) -> FileType:
    name = (uploaded.name or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    allowed = allowed or set(TYPES)
    if ext not in allowed:
        raise UploadRejected(f"Only these file types are accepted: {', '.join(sorted(allowed))}.")
    if uploaded.size > settings.DOCUMENT_MAX_BYTES:
        mb = settings.DOCUMENT_MAX_BYTES // (1024 * 1024)
        raise UploadRejected(f"Files must be {mb} MB or smaller.")
    if uploaded.size == 0:
        raise UploadRejected("The file is empty.")
    kind = TYPES[ext]
    uploaded.seek(0)
    head = uploaded.read(8192)
    uploaded.seek(0)
    if kind.magic and not any(head.startswith(m) for m in kind.magic):
        raise UploadRejected("The file's contents do not match its type.")
    if kind.text:
        if b"\x00" in head:
            raise UploadRejected("Text files must not contain binary data.")
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadRejected("Text files must be UTF-8.") from exc
    if kind.zip_member:
        try:
            with zipfile.ZipFile(uploaded) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise UploadRejected("The file's contents do not match its type.") from exc
        finally:
            uploaded.seek(0)
        if kind.zip_member not in names or any(
            n.lower().startswith("macros") or "vbaproject" in n.lower() for n in names
        ):
            raise UploadRejected("Office files with macros, or damaged files, are not accepted.")
    return kind
