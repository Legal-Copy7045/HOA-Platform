"""Deployment checks shown by `manage.py check --deploy` (security review C2)."""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=True)
def malware_scanning(app_configs, **kwargs):
    if settings.DOCUMENT_SCANNER == "none":
        return [
            Warning(
                "Uploaded files are not scanned for malware (DOCUMENT_SCANNER=none).",
                hint="Run a ClamAV container, set DOCUMENT_SCANNER=clamd and "
                "DOCUMENT_REQUIRE_CLEAN_SCAN=true, then run rescan_files.",
                id="hoa.W001",
            )
        ]
    return []


@register(Tags.security)
def demo_data_in_production(app_configs, **kwargs):
    """Production settings force SEED_DEMO_ALLOWED off outside the POC (D53);
    if it is ever on in production anyway, say so loudly at every start."""
    if settings.SEED_DEMO_ALLOWED and settings.DEPLOYMENT_STAGE == "production":
        return [
            Warning(
                "SEED_DEMO_ALLOWED is true on a production deployment: demo data could "
                "replace real data.",
                hint="Remove SEED_DEMO_ALLOWED from the environment (production settings "
                "should already force it off; check DEPLOYMENT_STAGE and the settings module).",
                id="hoa.W002",
            )
        ]
    return []


@register(Tags.security)
def demo_mode_only_on_the_poc(app_configs, **kwargs):
    """Demo mode (D97) switches a board member's session to a demo persona.
    It must never be possible outside the POC: refuse to start if the gate
    (core.demo_mode.configured) ever opens on another stage or with demo data
    not allowed, e.g. after an edit that widens it."""
    from core import demo_mode

    if demo_mode.configured() and not (
        settings.DEPLOYMENT_STAGE == "poc" and settings.SEED_DEMO_ALLOWED
    ):
        return [
            Error(
                "Demo mode would be available outside the POC.",
                hint="Demo mode needs DEPLOYMENT_STAGE=poc and SEED_DEMO_ALLOWED=true; "
                "check core/demo_mode.py configured().",
                id="hoa.E003",
            )
        ]
    if "core.demo_mode.DemoSwitchMiddleware" in settings.MIDDLEWARE:
        order = settings.MIDDLEWARE
        mfa = "accounts.middleware.RequireMFAMiddleware"
        if mfa not in order or order.index(mfa) > order.index(
            "core.demo_mode.DemoSwitchMiddleware"
        ):
            return [
                Error(
                    "The demo switch must run after the MFA check of the real user.",
                    hint="Put core.demo_mode.DemoSwitchMiddleware after "
                    "accounts.middleware.RequireMFAMiddleware in MIDDLEWARE.",
                    id="hoa.E004",
                )
            ]
    return []
