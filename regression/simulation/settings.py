"""Settings for the accelerated simulation (regression stage ``simulation``, D102).

The test settings (fast hashing, no outside calls, throwaway keys) on the
simulation's own database (``SIM_DATABASE_URL``, default ``hoa_sim`` on the
local Postgres), files on disk under ``regression/out/simulation/media/<db>``
so a snapshot can be served, and every outside service replaced by the fakes
in ``regression/simulation/fakes.py`` (installed by the engine, never here).
Every key below is a marked, test-only value: nothing here is a secret.
"""

import os
from pathlib import Path

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("SIM_DATABASE_URL", "postgres://hoa_owner:hoa_owner@localhost:5432/hoa_sim"),
)
os.environ.setdefault("SEED_DEMO_ALLOWED", "true")

from core.settings.test import *  # noqa: F403

OUT = Path(__file__).resolve().parent.parent / "out" / "simulation"


def media_dir(database_url: str) -> Path:
    name = (database_url.rsplit("/", 1)[-1] or "default").split("?", 1)[0]
    safe = "".join(c for c in name if c.isalnum() or c in "_-") or "default"
    return OUT / "media" / safe


MEDIA_DIR = media_dir(os.environ["DATABASE_URL"])
STORAGES["default"] = {  # noqa: F405
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {"location": str(MEDIA_DIR)},
}
SEED_DEMO_ALLOWED = True
DEPLOYMENT_STAGE = "poc"
PORTAL_BASE_URL = "https://portal.example.org"
# Email: the SMTP2Go relay, answered by the fake (fakes.FakeSmtp2go); the
# messages also land in django.core.mail.outbox through locmem.
EMAIL_BACKEND = "core.email.RedirectingBackend"
EMAIL_TRANSPORT_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_PROVIDER = "smtp2go"
SMTP2GO_API_KEY = "api-SIMONLY0123456789ABCDEFnotreal"  # nosec B105 - fake relay only, not-a-secret
SMTP2GO_REGION = "global"
EMAIL_REDIRECT_ALL_TO = ""
# Stripe in test mode, answered by payments/tests/fake_stripe.py. The key is
# assembled so it is never a literal key-shaped string.
PAYMENTS_PROVIDER = "stripe"
STRIPE_SECRET_KEY = "rk_" + "test_" + "SIMULATION" * 3  # nosec B105 - fake Stripe, not-a-secret
# Bank feed (Teller), vendor documents, books package: throwaway keys.
BANK_FEED_TOKEN_KEY = "sim-only-bank-feed-token-key-not-a-secret-0123"  # noqa: S105  # nosec B105
VENDOR_DOCUMENT_KEY = "sim-only-vendor-document-key-not-a-secret-0123"  # nosec B105
BOOKS_PACKAGE_KEY = "sim-only-books-package-key-not-a-secret-0123456"  # nosec B105
TELLER_ENVIRONMENT = "sandbox"
TELLER_APPLICATION_ID = "app_sim_only"
GEMINI_API_KEY = ""  # the AI reader is the fake (fakes.fake_ai)
USPS_IV_USERNAME = ""
USPS_IV_PASSWORD = ""  # nosec B105 - off
# Outside services the simulation has no fake for are pinned off here, whatever
# the shell exports (review EXP-002, 0.47.0); netguard.py is the backstop.
SMS_PROVIDER = "none"
SMS_ACCOUNT_SID = ""
SMS_AUTH_TOKEN = ""  # nosec B105 - off
SMS_FROM = ""
VAPID_PUBLIC_KEY = ""
VAPID_PRIVATE_KEY = ""
VAPID_SUBJECT = ""
BACKUP_S3_BUCKET = ""
BACKUP_S3_ENDPOINT_URL = ""
BACKUP_S3_ACCESS_KEY_ID = ""
BACKUP_S3_SECRET_ACCESS_KEY = ""  # nosec B105 - off
BOUNCE_IMAP_HOST = ""
BOUNCE_IMAP_USER = ""
BOUNCE_IMAP_PASSWORD = ""  # nosec B105 - off
TELLER_CERTIFICATE_B64 = ""
TELLER_PRIVATE_KEY_B64 = ""
PASSWORD_BREACH_CHECK = False
SECRET_BALLOT_MIX_MIN = 2
LOGGING["root"]["level"] = "ERROR"  # noqa: F405
for _name in list(LOGGING.get("loggers", {})):  # noqa: F405
    LOGGING["loggers"][_name]["level"] = "ERROR"  # noqa: F405
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
# Malware scanning as in production: every upload is scanned (the fake
# scanner in fakes.py answers "clean") and only clean files may be opened.
DOCUMENT_SCANNER = "clamd"
DOCUMENT_CLAMD_ADDRESS = "fake-clamd:3310"
DOCUMENT_REQUIRE_CLEAN_SCAN = True
