"""Settings for the regression page stage: local settings, a throwaway database
seeded with the fictional demo community, files on local disk, no outbound mail."""

import os
from pathlib import Path

os.environ.setdefault("DJANGO_SECRET_KEY", "dev-insecure-regression-pages")
os.environ.setdefault("AUDIT_HASH_KEY", "dev-insecure-regression-audit-key-0000000000")
os.environ.setdefault("SEED_DEMO_ALLOWED", "true")
os.environ.setdefault("DJANGO_DEBUG", "false")

from core.settings.local import *  # noqa: F403


def media_dir(database_url: str) -> Path:
    """regression/out/media/<database name> (the page and walkthrough stages
    run side by side, each on its own database)."""
    name = (database_url.rsplit("/", 1)[-1] or "default").split("?", 1)[0]
    safe = "".join(c for c in name if c.isalnum() or c in "_-") or "default"
    return Path(__file__).resolve().parent / "out" / "media" / safe


DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
# Files on disk under regression/out, one folder per database: the demo is
# seeded by one process (seed_demo) and served by another (runserver), so files
# kept in memory were gone by the time a page opened them (walkthrough 0.44.0:
# every demo receipt was a 404).
MEDIA_DIR = media_dir(os.environ.get("DATABASE_URL", ""))
STORAGES["default"] = {  # noqa: F405
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {"location": str(MEDIA_DIR)},
}
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_TRANSPORT_BACKEND = EMAIL_BACKEND
PASSWORD_BREACH_CHECK = False
LOGGING["root"]["level"] = "WARNING"  # noqa: F405
# Serve static files straight from the app directories (no collectstatic step).
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True
