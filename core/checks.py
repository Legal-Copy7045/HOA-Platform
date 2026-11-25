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
