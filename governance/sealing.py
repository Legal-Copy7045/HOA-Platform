"""Sealed staging for secret choices (decision D6x, on top of D56).

The casting transaction must write the participation ("this home voted") and
the pooled choice together: that is what makes one vote per home atomic and
loses no ballot on a crash or retry. The price is that the two rows share a
transaction id, an adjacent place in the write-ahead log and in a dump. So the
pooled row is made to say nothing: the choice is sealed here, in the web
process, before it reaches the database, and only the mixing flush opens it,
in memory, to write the permanent rows in shuffled order.

* AES-256-GCM with a random 96-bit nonce (no timestamp, unlike Fernet, which
  would put the cast time in the token). The key is derived with HKDF from
  ``DJANGO_SECRET_KEY``: it lives in the server's environment, never in the
  database, its dumps, the write-ahead log or the off-site backups.
* The associated data binds a sealed row to its kind and vote, election or
  survey, so it cannot be moved to another one.
* Every plaintext is padded to the same size for its vote, election or survey
  (a bound computed from the options, seats or questions), so the length of a
  sealed row does not tell one choice from another.
* Opening tries the current key, then ``SECRET_KEY_FALLBACKS``, so the key can
  be rotated while a secret vote is open. If no key opens a row, opening fails
  loudly (``PoolKeyError``): the row stays in the pool and nothing is counted
  without it.

Pure functions with no database access, so the migration that sealed rows
pooled before this change can use them too.
"""

from __future__ import annotations

import json
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

FORMAT = b"\x01"
NONCE_BYTES = 12
_SALT = b"oak-creek-portal governance secret-choice pool"
_INFO = b"aes-256-gcm v1"

# Worst case of one JSON-encoded id and of one answer entry, generously.
_ID = 21
_ENTRY = 128
# ensure_ascii JSON: a character outside the BMP becomes two \\uXXXX escapes.
_TEXT_BYTES_PER_CHAR = 12


class PoolKeyError(RuntimeError):
    """No configured key opens a pooled secret choice (see D6x)."""


def _key(secret: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(
        secret.encode("utf-8")
    )


def _keys() -> list[bytes]:
    secrets_ = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return [_key(s) for s in secrets_ if s]


def _aad(kind: str, target_id: int) -> bytes:
    return f"{kind}:{int(target_id)}".encode("ascii")


# --- Padded sizes ------------------------------------------------------------------------


def vote_bound() -> int:
    return 64


def election_bound(seats: int) -> int:
    return 32 + _ID * max(1, int(seats))


def survey_bound(questions: list[tuple[str, int]], text_max: int) -> int:
    """``questions``: (kind, number of choices) for every question."""
    size = 32
    for kind, n_choices in questions:
        if kind == "multiple":
            size += _ENTRY * max(1, int(n_choices))
        elif kind == "text":
            size += _ENTRY + _TEXT_BYTES_PER_CHAR * int(text_max)
        else:
            size += _ENTRY
    return size


# --- Sealing -----------------------------------------------------------------------------


def encode(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, sort_keys=True).encode(
        "ascii"
    )


def seal(kind: str, target_id: int, payload: dict, size: int) -> bytes:
    """Encrypt ``payload`` padded to exactly ``size`` bytes of plaintext."""
    body = encode(payload)
    if len(body) > size:
        # Never seal a longer row: its length would set it apart.
        raise ValueError("secret choice larger than its padded size")
    framed = struct.pack(">I", len(body)) + body + bytes(size - len(body))
    nonce = os.urandom(NONCE_BYTES)
    keys = _keys()
    if not keys:
        raise PoolKeyError("no key to seal secret choices")
    return FORMAT + nonce + AESGCM(keys[0]).encrypt(nonce, framed, _aad(kind, target_id))


def unseal(kind: str, target_id: int, blob: bytes) -> dict:
    blob = bytes(blob)
    if not blob.startswith(FORMAT) or len(blob) < 1 + NONCE_BYTES + 16 + 4:
        raise PoolKeyError("not a sealed secret choice")
    nonce, sealed = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
    for key in _keys():
        try:
            framed = AESGCM(key).decrypt(nonce, sealed, _aad(kind, target_id))
        except InvalidTag:
            continue
        (length,) = struct.unpack(">I", framed[:4])
        return json.loads(framed[4 : 4 + length])
    raise PoolKeyError("no configured key opens this secret choice")
