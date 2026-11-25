"""Client address and scheme behind a reverse proxy (decision D6).

Headers such as ``CF-Connecting-IP`` and ``X-Forwarded-Proto`` are trusted
only when the *connecting* address is in ``TRUSTED_PROXY_CIDRS`` (on the POC:
the cloudflared container's fixed address). From anywhere else they are
deleted, so nothing downstream can read a spoofed value. This matters because
the client IP drives login lockout and the audit log.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from django.conf import settings
from django.http import HttpRequest, HttpResponse

_PROXY_HEADERS = ("HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_FOR", "HTTP_X_REAL_IP")


def _meta_key(header: str) -> str:
    return "HTTP_" + header.upper().replace("-", "_")


@lru_cache(maxsize=8)
def _networks(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(cidr.strip(), strict=False) for cidr in cidrs if cidr.strip())


def _parse_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def is_trusted_proxy(remote_addr: str | None) -> bool:
    address = _parse_ip(remote_addr)
    if address is None:
        return False
    ip = ipaddress.ip_address(address)
    return any(ip in net for net in _networks(tuple(settings.TRUSTED_PROXY_CIDRS)))


class TrustedProxyMiddleware:
    """Sets ``request.client_ip`` and the request scheme. Must run first."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        meta = request.META
        remote = _parse_ip(meta.get("REMOTE_ADDR"))
        client_header = _meta_key(settings.CLIENT_IP_HEADER)
        if is_trusted_proxy(remote):
            request.client_ip = _parse_ip(meta.get(client_header)) or remote
            if meta.get("HTTP_X_FORWARDED_PROTO", "").strip().lower() == "https":
                meta["wsgi.url_scheme"] = "https"
        else:
            request.client_ip = remote
        # Nobody downstream may read these directly.
        for key in (client_header, *_PROXY_HEADERS):
            meta.pop(key, None)
        return self.get_response(request)


def client_ip(request: HttpRequest) -> str | None:
    """The client's IP as established by TrustedProxyMiddleware."""
    if hasattr(request, "client_ip"):
        return request.client_ip
    return _parse_ip(request.META.get("REMOTE_ADDR"))
