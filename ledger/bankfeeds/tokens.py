"""Encrypting stored bank access tokens.

Fernet (AES-128-CBC with HMAC-SHA256) under a key derived with HKDF from
BANK_FEED_TOKEN_KEY. Without that setting nothing can be connected or synced.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

MIN_KEY_LENGTH = 32
_INFO = b"oak-creek-portal bank feed access tokens v1"


class TokenKeyMissing(ValueError):
    pass


class TokenUnreadable(ValueError):
    pass


def configured() -> bool:
    return len(settings.BANK_FEED_TOKEN_KEY or "") >= MIN_KEY_LENGTH


def _fernet() -> Fernet:
    secret = settings.BANK_FEED_TOKEN_KEY or ""
    if len(secret) < MIN_KEY_LENGTH:
        raise TokenKeyMissing(
            "BANK_FEED_TOKEN_KEY is not set (at least 32 characters), so a bank "
            "connection cannot be stored safely."
        )
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt(stored: str) -> str:
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except InvalidToken as exc:  # wrong or changed key; never show the value
        raise TokenUnreadable("The stored bank connection cannot be read with this key.") from exc
