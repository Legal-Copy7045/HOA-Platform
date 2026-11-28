"""Application-layer encryption of vendors' documents (decision D79).

Every file a vendor uploads on the onboarding form (the W-9 above all: it
carries a taxpayer identification number) is encrypted here, in the web
process, before it reaches the file store. The bucket's own encryption is a
second layer, not the only one.

* AES-256-GCM with a random 96-bit nonce per file. The key is derived with
  HKDF-SHA256 from ``VENDOR_DOCUMENT_KEY`` (a long random value the server
  script ``infra/poc/set-vendor-key.sh`` generates; it never passes through
  the web app, the database or the backups).
* The associated data binds a blob to its file record's random storage name,
  so a blob cannot be swapped onto another record.
* Opening tries the current key, then ``VENDOR_DOCUMENT_KEY_FALLBACKS``, so
  the key can be rotated: put the old key in the fallbacks, set a new key,
  then ``manage.py check_vendor_key --reencrypt`` rewrites every blob under the
  new key.
* Without a key nothing is encrypted or opened (``VendorKeyMissing``, fails
  closed); a blob no configured key opens raises ``VendorKeyMismatch``.
  Neither message ever contains key material or file contents.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core import credentials

FORMAT = b"VD1\x00"
NONCE_BYTES = 12
MIN_KEY_LENGTH = 32
_INFO = b"oak-creek-portal vendor documents aes-256-gcm v1"
_SALT = b"oak-creek-portal vendor onboarding"

MISSING_TEXT = (
    "Vendor documents can't be stored or opened: the server's document key is not set up. "
    "A sysadmin runs infra/poc/set-vendor-key.sh on the server."
)
MISMATCH_TEXT = (
    "This document can't be opened with the server's document key. The key may have been "
    "changed without keeping the old one; a sysadmin should check it (check_vendor_key)."
)


class VendorKeyMissing(RuntimeError):
    def __init__(self, message: str = MISSING_TEXT):
        super().__init__(message)


class VendorKeyMismatch(RuntimeError):
    def __init__(self, message: str = MISMATCH_TEXT):
        super().__init__(message)


def _derive(secret: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(
        secret.encode("utf-8")
    )


def _current() -> str:
    """Keys and connections (D93), else VENDOR_DOCUMENT_KEY from the server."""
    return credentials.get("VENDOR_DOCUMENT_KEY") or ""


def _secrets() -> list[str]:
    current = _current()
    fallbacks = list(credentials.get("VENDOR_DOCUMENT_KEY_FALLBACKS") or [])
    return [s for s in [current, *fallbacks] if len(s or "") >= MIN_KEY_LENGTH]


def configured() -> bool:
    """A usable current key is set (fallbacks alone only open old files)."""
    return len(_current()) >= MIN_KEY_LENGTH


def _aad(name: str) -> bytes:
    return f"vendor-file:{name}".encode()


def encrypt(plaintext: bytes, name: str) -> bytes:
    """``name``: the file record's random storage name (bound as associated data)."""
    if not configured():
        raise VendorKeyMissing()
    nonce = os.urandom(NONCE_BYTES)
    key = _derive(_current())
    return FORMAT + nonce + AESGCM(key).encrypt(nonce, bytes(plaintext), _aad(name))


def key_index(blob: bytes, name: str) -> int:
    """Which configured key opens ``blob``: 0 the current one, 1.. a fallback.
    Raises like ``decrypt``."""
    return _open(blob, name)[1]


def decrypt(blob: bytes, name: str) -> bytes:
    return _open(blob, name)[0]


def _open(blob: bytes, name: str) -> tuple[bytes, int]:
    secrets_ = _secrets()
    if not configured() and not secrets_:
        raise VendorKeyMissing()
    blob = bytes(blob)
    if not blob.startswith(FORMAT) or len(blob) < len(FORMAT) + NONCE_BYTES + 16:
        raise VendorKeyMismatch()
    nonce = blob[len(FORMAT) : len(FORMAT) + NONCE_BYTES]
    sealed = blob[len(FORMAT) + NONCE_BYTES :]
    for index, secret in enumerate(secrets_):
        try:
            return AESGCM(_derive(secret)).decrypt(nonce, sealed, _aad(name)), index
        except InvalidTag:
            continue
    raise VendorKeyMismatch()
