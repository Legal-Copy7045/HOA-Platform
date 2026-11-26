"""Malware-scan hook. "none" records files as unscanned; "clamd" asks a ClamAV
daemon (INSTREAM). Add a clamav container and set DOCUMENT_SCANNER=clamd to
enable real scanning.

``scan()`` returns ``(result, scanner)``; the tuple also carries ``.detail``,
a short code saying why a scan did not give an answer (D57: System status
shows it in plain words). Never an exception message or a file name.
"""

from __future__ import annotations

import socket
import struct

from django.conf import settings

from documents.models import ScanResult

# Why a scan gave no answer (stored on the scan row; shown in plain words).
UNREACHABLE = "unreachable"  # nothing listening, refused, name not found
TIMEOUT = "timeout"  # connected, but no reply in time
BAD_REPLY = "bad_reply"  # replied, but neither OK nor FOUND
NOT_CONFIGURED = "not_configured"  # DOCUMENT_SCANNER=none: never attempted
UNKNOWN_SCANNER = "unknown_scanner"  # DOCUMENT_SCANNER names nothing we know

DETAIL_WORDS = {
    UNREACHABLE: "Scanner unreachable",
    TIMEOUT: "Timed out",
    BAD_REPLY: "Scanner gave no clear answer",
    NOT_CONFIGURED: "Never attempted (scanning was off)",
    UNKNOWN_SCANNER: "Scanner not set up",
}

CLAMD_TIMEOUT_SECONDS = 30


class ScanOutcome(tuple):
    """``(result, scanner)`` with the reason code as ``.detail``."""

    detail: str

    def __new__(cls, result: str, scanner: str, detail: str = ""):
        outcome = super().__new__(cls, (result, scanner))
        outcome.detail = detail
        return outcome


def detail_of(outcome) -> str:
    """The reason code of a ``scan()`` result (plain tuples from tests: none)."""
    return getattr(outcome, "detail", "")


def scan(fileobj) -> ScanOutcome:
    scanner = settings.DOCUMENT_SCANNER
    if scanner == "none":
        return ScanOutcome(ScanResult.UNSCANNED, "none", NOT_CONFIGURED)
    if scanner == "clamd":
        result, detail = _clamd(fileobj)
        return ScanOutcome(result, "clamd", detail)
    return ScanOutcome(ScanResult.ERROR, scanner[:50], UNKNOWN_SCANNER)


def _clamd(fileobj) -> tuple[str, str]:
    host, _, port = settings.DOCUMENT_CLAMD_ADDRESS.partition(":")
    try:
        with socket.create_connection(
            (host, int(port or 3310)), timeout=CLAMD_TIMEOUT_SECONDS
        ) as sock:
            sock.sendall(b"zINSTREAM\0")
            fileobj.seek(0)
            while chunk := fileobj.read(64 * 1024):
                sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
            reply = sock.recv(4096).decode(errors="replace")
    except TimeoutError:  # socket.timeout is TimeoutError since Python 3.10
        return ScanResult.ERROR, TIMEOUT
    except (OSError, ValueError):
        return ScanResult.ERROR, UNREACHABLE
    finally:
        fileobj.seek(0)
    if reply.rstrip("\0").endswith("OK"):
        return ScanResult.CLEAN, ""
    if "FOUND" in reply:
        return ScanResult.INFECTED, ""
    return ScanResult.ERROR, BAD_REPLY
