"""Web Push without extra dependencies (Milestone 4).

Implements the two small standards the browsers' push services need, using
the ``cryptography`` package the portal already ships:

- RFC 8291 message encryption (``aes128gcm``): the payload is encrypted for
  the one browser that subscribed, so the push service cannot read it.
- RFC 8292 VAPID: a short-lived ES256 token tells the push service which
  server is sending.

pywebpush was not added: it pulls in requests, aiohttp and more for what is
about eighty lines here, and every dependency is pinned and audited.

The HTTP post refuses redirects and any endpoint outside
``settings.PUSH_ALLOWED_HOSTS`` (see ``endpoint_allowed``), so a subscription
cannot point the server at an internal address.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

RECORD_SIZE = 4096
TIMEOUT_SECONDS = 10


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(value: str) -> bytes:
    value = value.strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def private_key_from_b64u(value: str) -> ec.EllipticCurvePrivateKey:
    raw = b64u_decode(value)
    if len(raw) != 32:
        raise ValueError("A VAPID private key is 32 bytes, base64url encoded.")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def generate_vapid_keys() -> tuple[str, str]:
    """(public, private), both base64url, for VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    return b64u_encode(_public_bytes(key.public_key())), b64u_encode(private)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(
    payload: bytes,
    *,
    p256dh: str,
    auth: str,
    _sender_key: ec.EllipticCurvePrivateKey | None = None,
    _salt: bytes | None = None,
) -> bytes:
    """RFC 8291 ``aes128gcm`` body for one subscription. The two underscore
    arguments exist only for the RFC's published test vector."""
    ua_public = b64u_decode(p256dh)
    auth_secret = b64u_decode(auth)
    if len(ua_public) != 65 or len(auth_secret) != 16:
        raise ValueError("Malformed subscription keys.")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_key = _sender_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_key.public_key())
    salt = _salt or os.urandom(16)
    ecdh_secret = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, ecdh_secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) + 1 + 16 > RECORD_SIZE:
        raise ValueError("Push payload too large.")
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


def vapid_header(endpoint: str, *, private_key: str, public_key: str, subject: str) -> str:
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(time.time()) + 12 * 3600,
        "sub": subject,
    }
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = (
        b64u_encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64u_encode(json.dumps(claims, separators=(",", ":")).encode())
    )
    key = private_key_from_b64u(private_key)
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={signing_input}.{b64u_encode(signature)}, k={public_key}"


def endpoint_allowed(endpoint: str) -> bool:
    """Only https endpoints of a known push service, default port, no userinfo."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in (h.strip().lower() for h in settings.PUSH_ALLOWED_HOSTS)
        if allowed
    )


@dataclass(frozen=True)
class PushResult:
    ok: bool
    gone: bool = False  # the subscription no longer exists: delete it
    status: int = 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post(endpoint: str, body: bytes, *, authorization: str, ttl: int = 86400) -> PushResult:
    if not endpoint_allowed(endpoint):
        return PushResult(ok=False, gone=True)
    request = urllib.request.Request(  # noqa: S310 - https only, allowlisted host
        endpoint,
        data=body,
        method="POST",
        headers={
            "TTL": str(ttl),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Urgency": "normal",
            "Authorization": authorization,
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            return PushResult(ok=200 <= response.status < 300, status=response.status)
    except urllib.error.HTTPError as exc:
        return PushResult(ok=False, gone=exc.code in (404, 410), status=exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return PushResult(ok=False)
