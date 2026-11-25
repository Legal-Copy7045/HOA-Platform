"""Malware-scan hook. "none" records files as unscanned; "clamd" asks a ClamAV
daemon (INSTREAM). Add a clamav container and set DOCUMENT_SCANNER=clamd to
enable real scanning."""

from __future__ import annotations

import socket
import struct

from django.conf import settings

from documents.models import ScanResult


def scan(fileobj) -> tuple[str, str]:
    scanner = settings.DOCUMENT_SCANNER
    if scanner == "none":
        return ScanResult.UNSCANNED, "none"
    if scanner == "clamd":
        return _clamd(fileobj), "clamd"
    return ScanResult.ERROR, scanner


def _clamd(fileobj) -> str:
    host, _, port = settings.DOCUMENT_CLAMD_ADDRESS.partition(":")
    try:
        with socket.create_connection((host, int(port or 3310)), timeout=30) as sock:
            sock.sendall(b"zINSTREAM\0")
            fileobj.seek(0)
            while chunk := fileobj.read(64 * 1024):
                sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
            reply = sock.recv(4096).decode(errors="replace")
    except OSError:
        return ScanResult.ERROR
    finally:
        fileobj.seek(0)
    if reply.rstrip("\0").endswith("OK"):
        return ScanResult.CLEAN
    if "FOUND" in reply:
        return ScanResult.INFECTED
    return ScanResult.ERROR
