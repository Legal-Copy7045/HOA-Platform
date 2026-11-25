"""Client network details.

Step 5 of Milestone 1 replaces this with trusted-proxy handling (decision D6).
Until then the connecting address is the only source, which cannot be spoofed
by a header.
"""

from django.http import HttpRequest


def client_ip(request: HttpRequest) -> str | None:
    return getattr(request, "client_ip", None) or request.META.get("REMOTE_ADDR") or None
