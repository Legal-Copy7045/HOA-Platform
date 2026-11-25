"""Settings for the regression page stage: local settings, a throwaway database
seeded with the fictional demo community, files in memory, no outbound mail."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "dev-insecure-regression-pages")
os.environ.setdefault("AUDIT_HASH_KEY", "dev-insecure-regression-audit-key-0000000000")
os.environ.setdefault("SEED_DEMO_ALLOWED", "true")
os.environ.setdefault("DJANGO_DEBUG", "false")

from core.settings.local import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
STORAGES["default"] = {"BACKEND": "django.core.files.storage.InMemoryStorage"}  # noqa: F405
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
