"""Upload checks: treat every file as hostile (CLAUDE.md).

Allowed types are checked by extension *and* by the file's first bytes, so a
renamed executable is refused. Stored content type always comes from this
table, never from the browser.
"""

from __future__ import annotations

import codecs
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, replace

from django.conf import settings


class UploadRejected(ValueError):
    pass


@dataclass(frozen=True)
class FileType:
    content_type: str
    magic: tuple[bytes, ...] = ()
    zip_member: str | None = None  # Office files are zip archives with this entry
    text: bool = False
    ext: str = ""  # the extension that was validated (set by check_upload)


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


# Office parts that can run or fetch code when the file is opened.
_OFFICE_FORBIDDEN_MEMBER = re.compile(
    r"(^|/)(macros|activex|embeddings)(/|$)|vbaproject|\.bin$|vbadata", re.IGNORECASE
)
_OFFICE_FORBIDDEN_CONTENT = (
    b"macroEnabled",
    b"vnd.ms-office.vbaProject",
    b"vnd.ms-office.activeX",
    b"oleObject",
)
_EXTERNAL_REL = re.compile(rb'TargetMode\s*=\s*"External"', re.IGNORECASE)
_RISKY_REL_TYPES = (b"attachedTemplate", b"oleObject", b"/frame", b"subDocument", b"aFChunk")


def _office_problem(archive: zipfile.ZipFile, kind: FileType) -> str | None:
    names = archive.namelist()
    if kind.zip_member not in names:
        return "damaged"
    total = 0
    for info in archive.infolist():
        total += info.file_size
        if total > 200 * 1024 * 1024 or (
            info.compress_size and info.file_size / info.compress_size > 200
        ):
            return "too large when unpacked"
        if _OFFICE_FORBIDDEN_MEMBER.search(info.filename):
            return "macros or embedded objects"
    if "[Content_Types].xml" in names:
        types = archive.read("[Content_Types].xml")
        if any(marker.lower() in types.lower() for marker in _OFFICE_FORBIDDEN_CONTENT):
            return "macros or embedded objects"
    for name in names:
        if name.endswith(".rels"):
            rels = archive.read(name)
            for rel in re.findall(rb"<Relationship\b[^>]*>", rels):
                if _EXTERNAL_REL.search(rel) and any(t in rel for t in _RISKY_REL_TYPES):
                    return "links to outside templates or objects"
    return None


def _check_text(uploaded) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    uploaded.seek(0)
    try:
        for chunk in uploaded.chunks():
            if b"\x00" in chunk:
                raise UploadRejected("Text files must not contain binary data.")
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise UploadRejected("Text files must be UTF-8.") from exc
    finally:
        uploaded.seek(0)


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
    kind = replace(TYPES[ext], ext=ext)
    uploaded.seek(0)
    head = uploaded.read(8192)
    uploaded.seek(0)
    if kind.magic and not any(head.startswith(m) for m in kind.magic):
        raise UploadRejected("The file's contents do not match its type.")
    if kind.text:
        _check_text(uploaded)
    if kind.zip_member:
        try:
            with zipfile.ZipFile(uploaded) as archive:
                problem = _office_problem(archive, kind)
        except zipfile.BadZipFile as exc:
            raise UploadRejected("The file's contents do not match its type.") from exc
        finally:
            uploaded.seek(0)
        if problem:
            raise UploadRejected(
                "Office files with macros, embedded objects, outside links, or damaged "
                "files are not accepted."
            )
    return kind


_EXT_FOR_TYPE = {t.content_type: e for e, t in TYPES.items() if e != "jpeg"}


def safe_filename(name: str, ext: str) -> str:
    """A stored or downloaded name always ends in the *validated* extension,
    however the original was spelled or shortened (no ``x.hta…x.pdf`` tricks)."""
    base = os.path.basename((name or "").replace("\\", "/")) or "file"
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = unicodedata.normalize("NFKC", stem)
    stem = "".join(c for c in stem if c.isalnum() or c in "_- ")
    stem = stem.replace(".", "_").strip(" -_")[:120] or "file"
    return f"{stem}.{ext}"


def download_filename(stored_name: str, content_type: str) -> str:
    return safe_filename(stored_name, _EXT_FOR_TYPE.get(content_type, "bin"))
