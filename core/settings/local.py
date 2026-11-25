"""Local development on http://localhost only.

Relaxes exactly what plain-HTTP localhost needs. Never used on any host
reachable from another machine (decision Q6: no LAN mode).
"""

from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# Browsers will not send Secure cookies over http://localhost in every case.
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=False)

ADMIN_ENABLED = env.bool("ADMIN_ENABLED", default=True)
if ADMIN_ENABLED and "django.contrib.admin" not in INSTALLED_APPS:  # noqa: F405
    INSTALLED_APPS.insert(0, "django.contrib.admin")  # noqa: F405

# Passkeys on http://localhost (browsers allow WebAuthn there).
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = True
SEED_DEMO_ALLOWED = env.bool("SEED_DEMO_ALLOWED", default=True)
BOARD_SEAT_SECOND_APPROVAL = env.bool("BOARD_SEAT_SECOND_APPROVAL", default=False)
