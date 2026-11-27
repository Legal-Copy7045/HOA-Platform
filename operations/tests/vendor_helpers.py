"""Shared set-up for the vendor onboarding tests (D79). Fictional data only."""

from __future__ import annotations

import re

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile

from operations import vendor_onboarding as vo
from operations.models import OnboardingLink

KEY = "test-only-vendor-document-key-not-a-secret-0123456789"  # nosec B105 - test value
FAKE_W9 = (
    b"%PDF-1.4\n% SAMPLE W-9 for tests: TIN 000-00-0000 SAMPLE, not a real form\n"
    b"1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
)


def pdf(name="w9.pdf", body=FAKE_W9):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


def enable(settings):
    from core.models import FeatureSetting

    settings.VENDOR_DOCUMENT_KEY = KEY
    settings.VENDOR_DOCUMENT_KEY_FALLBACKS = []
    FeatureSetting.objects.update_or_create(key="vendor_onboarding", defaults={"enabled": True})


def token_from_outbox() -> str:
    body = mail.outbox[-1].body
    match = re.search(r"/vendor-form/start/([A-Za-z0-9_-]+)/", body)
    assert match, body
    return match.group(1)


def send_open_link(rnd) -> str:
    """Run the worker's step for the round's open link; return the token."""
    link = OnboardingLink.objects.filter(round=rnd, ended_at__isnull=True).latest("id")
    assert vo.send_link_email(link.pk)
    return token_from_outbox()


FULL_ANSWERS = {
    "legal_name": "Pond Pros LLC (demo)",
    "dba": "",
    "entity_type": "smllc",
    "contact_name": "Pat Demo",
    "contact_email": "pat@example.test",
    "contact_phone": "610-555-0100",
    "remit_address": "1 Demo Lane\nNowhere, PA 19000",
    "services": "Pond care",
    "conflict": "no",
    "terms_accepted": True,
    "terms_version": 0,
    "w9_revision_confirmed": True,
    "coi_gl_carrier": "Demo Mutual",
    "coi_gl_amount_cents": 100000000,
    "coi_gl_expires": "2030-01-31",
    "coi_auto_carrier": "Demo Mutual",
    "coi_auto_amount_cents": 100000000,
    "coi_auto_expires": "2030-01-31",
    "workers_comp_mode": "exempt",
}


def fill_and_submit(rnd, answers=None):
    vo.save_answers(rnd, dict(FULL_ANSWERS, **(answers or {})))
    rnd.refresh_from_db()
    for key in vo.round_items(rnd):
        item = vo.ITEMS[key]
        if item.file_kind and item.file_required:
            vo.upload_file(rnd, item.file_kind, pdf(f"{item.file_kind}.pdf"))
    rnd.refresh_from_db()
    return vo.submit(rnd)
