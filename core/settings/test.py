"""Test settings: fast hashing, in-memory email, no external calls.

Defaults below let `pytest` run against a local Postgres with the two roles
created by docker/postgres/init-roles.sh. CI and developers may override them.
The values are test-only and grant nothing outside a disposable database.
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-a-secret")
os.environ.setdefault("DATABASE_URL", "postgres://hoa_owner:hoa_owner@localhost:5432/hoa")
os.environ.setdefault("DB_APP_ROLE", "hoa_app")
os.environ.setdefault("AUDIT_HASH_KEY", "test-only-audit-hash-key-not-a-secret")

from .base import *  # noqa: F403

DEBUG = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # tests only
EMAIL_TRANSPORT_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_BREACH_CHECK = False  # tests that need it mock the HTTP call
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = True
EMAIL_REDIRECT_ALL_TO = ""
STORAGES["default"] = {"BACKEND": "django.core.files.storage.InMemoryStorage"}  # noqa: F405
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
}
# Keys and connections (D93): a throwaway key-encryption key for the tests.
CREDENTIALS_KEK = os.environ.get("CREDENTIALS_KEK", "test-only-credentials-kek-not-a-secret-0123")
