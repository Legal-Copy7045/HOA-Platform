#!/usr/bin/env python3
"""Keyed checksums for the portal's encrypted backups (R2-003, R2-004).

Run with the VM's own python3, never with the key on the command line:

    python3 -I backup_mac.py sign   <backup dir> <key file> <stamp>
    python3 -I backup_mac.py verify <backup dir> <key file> [--allow-unsigned]

The key file is the backup encryption key. A separate MAC key is derived from
it, so the checksum never uses the encryption key directly. Each MAC covers
the backup stamp, the file name and the file's bytes, so a file cannot be
swapped for one from an older backup or renamed. Both fixed files
(db.dump.enc, files.tar.enc) are always checked, whatever HMAC-SHA256 lists.

``verify`` prints the signed stamp on stdout (messages go to stderr). Backups
made before this format are refused unless ``--allow-unsigned`` is given; then
an older keyed checksum is still checked if present, with a warning.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sys
from pathlib import Path

FILES = ("db.dump.enc", "files.tar.enc")
MAC_FILE = "HMAC-SHA256"
FORMAT = "hoa-backup-mac-v2"
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}Z$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_DERIVE_LABEL = b"oak-creek-portal backup MAC key v2"
_CHUNK = 1024 * 1024


class Refused(Exception):
    """The backup must not be restored."""


def read_key(key_file: Path) -> bytes:
    # The same bytes the old shell code used: "$(cat key)" drops trailing newlines.
    key = Path(key_file).read_bytes().rstrip(b"\n")
    if not key:
        raise Refused("The backup key file is empty.")
    return key


def mac_key(key: bytes) -> bytes:
    """A separate key for checksums, derived from the backup key."""
    return hmac.new(key, _DERIVE_LABEL, hashlib.sha256).digest()


def _stream(mac, path: Path):
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            mac.update(chunk)
    return mac.hexdigest()


def file_mac(key: bytes, stamp: str, name: str, path: Path) -> str:
    mac = hmac.new(mac_key(key), digestmod=hashlib.sha256)
    mac.update(f"{FORMAT}\0{stamp}\0{name}\0".encode())
    return _stream(mac, path)


def legacy_mac(key: bytes, path: Path) -> str:
    """The checksum older backups carry: HMAC of the bytes with the raw key."""
    return _stream(hmac.new(key, digestmod=hashlib.sha256), path)


def sign(directory: Path, key: bytes, stamp: str) -> None:
    if not STAMP_RE.match(stamp):
        raise Refused("The backup stamp is not in the expected form.")
    lines = [f"format {FORMAT}", f"stamp {stamp}"]
    for name in FILES:
        lines.append(f"{file_mac(key, stamp, name, directory / name)}  {name}")
    (directory / MAC_FILE).write_text("\n".join(lines) + "\n")


def _parse(text: str) -> tuple[str | None, str | None, dict[str, str]]:
    fmt = stamp = None
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("format "):
            fmt = line[len("format ") :].strip()
        elif line.startswith("stamp "):
            stamp = line[len("stamp ") :].strip()
        else:
            parts = line.split()
            if len(parts) == 2 and HEX_RE.match(parts[0]):
                sums[parts[1]] = parts[0]
    return fmt, stamp, sums


def verify(directory: Path, key: bytes, *, allow_unsigned: bool = False) -> str:
    """Check both backup files. Returns the signed stamp ("" for an old backup)."""
    for name in FILES:
        if not (directory / name).is_file():
            raise Refused(f"{name} is missing from the backup.")
    mac_path = directory / MAC_FILE
    text = mac_path.read_text() if mac_path.is_file() else ""
    fmt, stamp, sums = _parse(text)
    if fmt == FORMAT:
        if not stamp or not STAMP_RE.match(stamp):
            raise Refused("The keyed checksum has no valid backup stamp.")
        for name in FILES:
            want = sums.get(name)
            if not want:
                raise Refused(f"{name} has no keyed checksum.")
            if not hmac.compare_digest(want, file_mac(key, stamp, name, directory / name)):
                raise Refused(f"{name} fails its keyed checksum.")
        # A backup directory is named after its stamp; a mismatch means files
        # from a different backup were put here.
        if STAMP_RE.match(directory.name) and directory.name != stamp:
            raise Refused(
                f"The folder is named {directory.name} but the backup was made at {stamp}."
            )
        return stamp
    if fmt is not None:
        raise Refused("The keyed checksum is in an unknown format.")
    if not allow_unsigned:
        raise Refused(
            "This backup has no stamped keyed checksum (made before the current "
            "format, or the checksum was removed). Re-run with --allow-unsigned "
            "only if you know it is an older backup of yours."
        )
    print(
        "WARNING: restoring a backup without a stamped keyed checksum. Its age and "
        "origin cannot be proven; only continue if you made it yourself.",
        file=sys.stderr,
    )
    if sums:
        for name in FILES:
            want = sums.get(name)
            if not want or not hmac.compare_digest(want, legacy_mac(key, directory / name)):
                raise Refused(f"{name} fails its older keyed checksum.")
        print("Older keyed checksum matches.", file=sys.stderr)
    else:
        print("WARNING: no keyed checksum at all.", file=sys.stderr)
    return ""


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 4 and argv[0] == "sign":
            sign(Path(argv[1]), read_key(Path(argv[2])), argv[3])
            return 0
        if len(argv) in (3, 4) and argv[0] == "verify":
            extra = argv[3:]
            if extra not in ([], ["--allow-unsigned"]):
                raise SystemExit(__doc__)
            stamp = verify(Path(argv[1]), read_key(Path(argv[2])), allow_unsigned=bool(extra))
            print(stamp)
            return 0
    except Refused as exc:
        print(f"Refusing to restore: {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
