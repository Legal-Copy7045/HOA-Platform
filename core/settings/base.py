"""Settings shared by every environment.

All configuration comes from environment variables (CLAUDE.md rule 2). Every
variable read here is documented in .env.example. Defaults are the *secure*
choice; local.py relaxes only what plain-HTTP localhost development needs.
"""

from pathlib import Path

import environ
from botocore.config import Config as BotoConfig

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# --- Core -------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
USE_X_FORWARDED_HOST = False  # never trust a client-supplied Host override

SITE_NAME = env("SITE_NAME", default="Oak Creek Farms HOA")
# Shown on the sysadmin status page; start-portal.sh sets it to the git commit.
APP_VERSION = env("APP_VERSION", default="dev")
# seed_demo refuses to run unless this is true (never set it in production).
SEED_DEMO_ALLOWED = env.bool("SEED_DEMO_ALLOWED", default=False)
# Decision D21 (security review A3): a new board seat needs a second board
# member's approval. On in production; the POC turns it off (see production.py).
BOARD_SEAT_SECOND_APPROVAL = env.bool("BOARD_SEAT_SECOND_APPROVAL", default=True)

# ARC neighbor sign-off (Milestone 3): single-use links expire after this
# many days. Gemini reads scanned signed forms when a key is set; a person
# confirms what it reads unless the board switches on automatic confirming.
SIGNOFF_LINK_DAYS = env.int("SIGNOFF_LINK_DAYS", default=14)

# Off-site backups (Milestone 4): an S3-compatible bucket the HOA owns,
# separate from the portal's file store. Empty bucket = local backups only.
BACKUP_S3_BUCKET = env("BACKUP_S3_BUCKET", default="")
BACKUP_S3_ENDPOINT_URL = env("BACKUP_S3_ENDPOINT_URL", default="")
BACKUP_S3_ACCESS_KEY_ID = env("BACKUP_S3_ACCESS_KEY_ID", default="")
BACKUP_S3_SECRET_ACCESS_KEY = env("BACKUP_S3_SECRET_ACCESS_KEY", default="")
BACKUP_S3_REGION = env("BACKUP_S3_REGION", default="auto")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_MODEL = env("GEMINI_MODEL", default="gemini-2.5-flash")
# Absolute address used in emailed links (the worker has no request to ask).
PORTAL_BASE_URL = (env("PORTAL_BASE_URL", default="") or "http://localhost:8000").rstrip("/")
INVITE_TTL_HOURS = env.int("INVITE_TTL_HOURS", default=168)
# Two-person flows (decisions D4, D5)
SYSADMIN_NOMINATION_TTL_DAYS = env.int("SYSADMIN_NOMINATION_TTL_DAYS", default=7)
MFA_RESET_REQUEST_TTL_DAYS = env.int("MFA_RESET_REQUEST_TTL_DAYS", default=7)
MFA_RESET_CODE_TTL_HOURS = env.int("MFA_RESET_CODE_TTL_HOURS", default=24)

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.forms",
    "django.contrib.postgres",
    "simple_history",
    "procrastinate.contrib.django",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "axes",
    "core",
    "accounts",
    "audit",
    "community",
    "comms",
    "operations",
    "documents",
    "governance",
    "arc",
    "compliance",
    "ledger",
    "payments",
    "reports",
]

AUTH_USER_MODEL = "accounts.User"

ADMIN_ENABLED = env.bool("ADMIN_ENABLED", default=False)
ADMIN_URL = env("ADMIN_URL", default="admin/")
if ADMIN_ENABLED:
    INSTALLED_APPS.insert(0, "django.contrib.admin")

MIDDLEWARE = [
    "core.middleware.RequestIDMiddleware",
    "core.net.TrustedProxyMiddleware",
    "core.uploads.RequestSizeLimitMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "csp.middleware.CSPMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "accounts.middleware.RequireMFAMiddleware",
    "core.features.FeatureGateMiddleware",
    "core.middleware.EmailUnavailableMiddleware",
    "core.preview.OwnerPreviewMiddleware",
    "core.sysadmin_audit.SysadminRequestAuditMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Must be last: turns an axes lockout into the lockout page.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "core.urls"
WSGI_APPLICATION = "core.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site",
                "core.context_processors.navigation",
                "core.context_processors.login_background",
            ],
        },
    },
]

# Forms render through our templates (templates/django/forms/).
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

# --- Database ---------------------------------------------------------------
# DATABASE_URL is the *runtime* (app role) connection. Migrations run in a
# separate one-shot process whose DATABASE_URL is the owner role (decision Q8).
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DATABASE_CONN_MAX_AGE", default=600)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# The worker is woken instantly by Postgres notifications; polling is only a
# fallback, so once a minute is plenty (optimization review A7).
PROCRASTINATE_WORKER_DEFAULTS = {
    "fetch_job_polling_interval": 60,
    "abort_job_polling_interval": 60,
}
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DB_APP_ROLE = env("DB_APP_ROLE", default="")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Cache (shared by all processes: login throttling, allauth rate limits) ---
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "hoa_cache",
    }
}

# --- Authentication (decisions D5, D6) ------------------------------------------
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",  # must be first
    "allauth.account.auth_backends.AuthenticationBackend",
]
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "home"

ACCOUNT_ADAPTER = "accounts.adapter.AccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]  # signup is closed anyway
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_USER_DISPLAY = "core.people.display_name"  # names, never "User #1"
# Invitations prove the address; there is no self-service email change.
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_SESSION_REMEMBER = False
ACCOUNT_LOGIN_ON_PASSWORD_RESET = False
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = False
ACCOUNT_REAUTHENTICATION_REQUIRED = True
# Re-authentication before sensitive changes (adding or removing an
# authenticator, recovery codes) never accepts an emailed code: email is not
# a factor (D5; security review B6). Password or an authenticator only.
ACCOUNT_REAUTHENTICATION_METHODS = ("password", "mfa")
# Emailed sign-in code instead of a password (D5): first factor only.
ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_LOGIN_BY_CODE_TIMEOUT = 600
ACCOUNT_LOGIN_BY_CODE_MAX_ATTEMPTS = 3
ACCOUNT_RATE_LIMITS = {
    # D6: per-email ceiling regardless of IP; axes handles email+IP below.
    "login_failed": "10/m/ip,20/h/key",
}
ACCOUNT_EMAIL_SUBJECT_PREFIX = ""

MFA_SUPPORTED_TYPES = ["totp", "recovery_codes", "webauthn"]
MFA_ADAPTER = "accounts.mfa_adapter.MFAAdapter"
MFA_PASSKEY_LOGIN_ENABLED = True
MFA_PASSKEY_SIGNUP_ENABLED = False
MFA_TRUST_ENABLED = False  # never skip the second factor on a "trusted" browser
MFA_TOTP_ISSUER = env("SITE_NAME", default="Oak Creek Farms HOA")
# Accept the code from the neighboring 30-second window too (RFC 6238 s.5.2),
# so a code typed as it rolls over, or a few seconds of clock drift, still works.
MFA_TOTP_TOLERANCE = 1
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = False
MFA_RECOVERY_CODE_COUNT = 10

# django-axes: 5 failures for one email from one IP -> 30-minute lockout (D6).
AXES_HANDLER = "axes.handlers.cache.AxesCacheHandler"  # no per-attempt DB rows
AXES_FAILURE_LIMIT = env.int("AXES_FAILURE_LIMIT", default=5)
AXES_COOLOFF_TIME = env.float("AXES_COOLOFF_HOURS", default=0.5)
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_CLIENT_IP_CALLABLE = "core.net.client_ip"
AXES_USERNAME_CALLABLE = "accounts.auth.axes_username"
AXES_LOCKOUT_TEMPLATE = "account/lockout.html"
AXES_VERBOSE = False

# --- Passwords --------------------------------------------------------------
# NIST SP 800-63B rev 4: length over composition. 12 minimum because every
# account also needs a second factor (decisions Q5/Q5b).
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "accounts.validators.BreachedPasswordValidator"},
]
# HIBP k-anonymity check (decision D3). Only a 5-character hash prefix leaves.
PASSWORD_BREACH_CHECK = env.bool("PASSWORD_BREACH_CHECK", default=True)
PASSWORD_BREACH_CHECK_TIMEOUT = env.float("PASSWORD_BREACH_CHECK_TIMEOUT", default=3.0)

# --- I18N -------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="America/New_York")
USE_I18N = True
USE_TZ = True

# --- Static and media -------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Uploads never touch local disk (CLAUDE.md): private S3-compatible bucket,
# served only through short-lived signed URLs.
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "endpoint_url": env("S3_ENDPOINT_URL", default=None),
            "bucket_name": env("S3_BUCKET", default=""),
            "access_key": env("S3_ACCESS_KEY_ID", default=""),
            "secret_key": env("S3_SECRET_ACCESS_KEY", default=""),
            "region_name": env("S3_REGION", default="auto"),
            "signature_version": "s3v4",
            "addressing_style": "path",
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": env.int("S3_SIGNED_URL_TTL_SECONDS", default=300),
            "file_overwrite": False,
            # Send upload checksums only when the store requires them. Newer
            # boto3 adds one to every upload by default, which S3-compatible
            # stores such as SeaweedFS can reject (POC, 2026-11).
            "client_config": BotoConfig(
                request_checksum_calculation=env("S3_CHECKSUMS", default="when_required"),
                response_checksum_validation=env("S3_CHECKSUMS", default="when_required"),
            ),
        },
    },
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Document uploads (CLAUDE.md "File uploads")
DOCUMENT_MAX_BYTES = env.int("DOCUMENT_MAX_BYTES", default=25 * 1024 * 1024)
# Malware scanning: "none" (record as unscanned) or "clamd" (host:port in
# DOCUMENT_CLAMD_ADDRESS). With DOCUMENT_REQUIRE_CLEAN_SCAN, only files a
# scanner marked clean can be downloaded.
DOCUMENT_SCANNER = env("DOCUMENT_SCANNER", default="none")
DOCUMENT_CLAMD_ADDRESS = env("DOCUMENT_CLAMD_ADDRESS", default="")
DOCUMENT_REQUIRE_CLEAN_SCAN = env.bool("DOCUMENT_REQUIRE_CLEAN_SCAN", default=False)
# Files per ARC request, violation or invoice. Attachments are permanent, so
# this stops one account filling the bucket.
# How long a new login (fresh invitation or assisted reset) has to enroll a
# passkey or authenticator app, in that same browser session (finding B2).
MFA_ENROLLMENT_WINDOW_SECONDS = env.int("MFA_ENROLLMENT_WINDOW_SECONDS", default=3600)
ATTACHMENTS_PER_OBJECT_MAX = env.int("ATTACHMENTS_PER_OBJECT_MAX", default=40)
FILE_UPLOAD_HANDLERS = [
    "core.uploads.LimitedUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]

# Ledger: late fees cannot be posted until the board has reviewed PA law and
# turned this on (decision recorded 2026-11-25).
# Decision D21: late fees may be posted (by the board, or automatically when
# the late_fee.auto_apply rule is on). This stays as an emergency off switch.
LEDGER_LATE_FEES_ENABLED = env.bool("LEDGER_LATE_FEES_ENABLED", default=True)
# Online payments (D32-payments): "none" or "stripe" (hosted Checkout; the app
# never sees card or bank numbers). The key lives only in the server's .env:
# a Stripe restricted key (rk_...) with read-only rights except Checkout
# Sessions. Test keys are refused in production; live keys only in
# production with PAYMENTS_LIVE_ALLOWED=true (payments/providers.py).
PAYMENTS_PROVIDER = env("PAYMENTS_PROVIDER", default="none")
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
PAYMENTS_LIVE_ALLOWED = env.bool("PAYMENTS_LIVE_ALLOWED", default=False)
# Where this server runs: "local", "poc" or "production" (production
# settings default to "production").
DEPLOYMENT_STAGE = env("DEPLOYMENT_STAGE", default="local")

# --- Installable app: push notifications (Milestone 4) ---------------------------------
# Web Push (VAPID) keys: base64url, as printed by `manage.py generate_vapid_keys`.
# Push is off while either key is empty (and while the board leaves the
# "push" feature off). The subject is a mailto: or https: contact for the
# push services.
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
VAPID_SUBJECT = env("VAPID_SUBJECT", default="")
# Push services the portal will post to (subscriptions elsewhere are refused,
# so a browser cannot point the server at an arbitrary address).
PUSH_ALLOWED_HOSTS = env.list(
    "PUSH_ALLOWED_HOSTS",
    default=[
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "push.services.mozilla.com",
        "web.push.apple.com",
        "notify.windows.com",
    ],
)

# --- Text messages for urgent notices (Milestone 4) -------------------------------------
# "none" (off) or "twilio" (Twilio's Messages API, or a compatible service at
# SMS_API_BASE). Credentials from the environment only.
SMS_PROVIDER = env("SMS_PROVIDER", default="none")
SMS_ACCOUNT_SID = env("SMS_ACCOUNT_SID", default="")
SMS_AUTH_TOKEN = env("SMS_AUTH_TOKEN", default="")
SMS_FROM = env("SMS_FROM", default="")
SMS_API_BASE = env("SMS_API_BASE", default="https://api.twilio.com")

# --- Bank feed (optional feature "bank_feeds", decision D32-bankfeeds) ----------------
# Key that encrypts stored bank access tokens (any long random string; the
# Fernet key is derived from it). Connecting a bank is refused while empty.
BANK_FEED_TOKEN_KEY = env("BANK_FEED_TOKEN_KEY", default="")
# Teller (teller.io), free developer tier. sandbox | development | production.
# Development is the free tier for real accounts. The certificate and private
# key from the Teller dashboard, each base64 of the PEM file (base64 -w0).
TELLER_APPLICATION_ID = env("TELLER_APPLICATION_ID", default="")
TELLER_ENVIRONMENT = env("TELLER_ENVIRONMENT", default="sandbox")
TELLER_CERTIFICATE_B64 = env("TELLER_CERTIFICATE_B64", default="")
TELLER_PRIVATE_KEY_B64 = env("TELLER_PRIVATE_KEY_B64", default="")

# --- Email ------------------------------------------------------------------
# The real transport; core.email.RedirectingBackend wraps it (POC redirect, D2).
EMAIL_BACKEND = "core.email.RedirectingBackend"
EMAIL_TRANSPORT_BACKEND = env(
    "EMAIL_TRANSPORT_BACKEND", default="django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=20)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="portal@example.com")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
# POC only: deliver every message to one inbox (decision Q2). Unset in production.
EMAIL_REDIRECT_ALL_TO = env("EMAIL_REDIRECT_ALL_TO", default="")

# --- Sessions, cookies, transport security ----------------------------------
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=12 * 60 * 60)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
# The container healthcheck calls /healthz over plain HTTP from inside.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]

# Reverse proxy trust (decision D6). Empty = trust no proxy headers.
TRUSTED_PROXY_CIDRS = env.list("TRUSTED_PROXY_CIDRS", default=[])
CLIENT_IP_HEADER = env("CLIENT_IP_HEADER", default="CF-Connecting-IP")
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = False

# Content Security Policy (django-csp 4). Everything self-hosted; no inline
# script or style, no third-party origins.
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ["'self'"],
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        "img-src": ["'self'", "data:"],
        "font-src": ["'self'"],
        "connect-src": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
        "base-uri": ["'none'"],
        "object-src": ["'none'"],
        # Installable app: the service worker and manifest come from this site only.
        "worker-src": ["'self'"],
        "manifest-src": ["'self'"],
    },
}

# --- Audit (decision D7) ------------------------------------------------------
# Key for hashing identifiers we must not store in clear (unknown login emails).
AUDIT_HASH_KEY = env("AUDIT_HASH_KEY", default="")
AUDIT_IP_RETENTION_DAYS = env.int("AUDIT_IP_RETENTION_DAYS", default=365)

# --- Governance rules from the Declaration/bylaws (CLAUDE.md rule 6) ----------
# Officers must hold an active board seat (decision D1, 1a).
OFFICERS_REQUIRE_BOARD_SEAT = env.bool("OFFICERS_REQUIRE_BOARD_SEAT", default=True)
OFFICERS_REQUIRE_BOARD_SEAT_CITATION = env("OFFICERS_REQUIRE_BOARD_SEAT_CITATION", default="")

# --- Logging ----------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "core.logging.JsonFormatter"}},
    "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        # Request-level errors are summarised by our formatter (no bodies).
        "django.request": {"level": "ERROR", "propagate": True},
        "django.security": {"level": "WARNING", "propagate": True},
        "procrastinate": {"level": "WARNING", "propagate": True},
    },
}
