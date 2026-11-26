"""Deployment checks shown by `manage.py check --deploy` (security review C2)."""

from django.conf import settings
from django.core.checks import Tags, Warning, register


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
