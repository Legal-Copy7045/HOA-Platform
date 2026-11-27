"""The oak_creek demo's renewals and retention cases (D72): every state is
there for a walkthrough, all fictional."""

import io

import pytest
from django.core.management import call_command

from documents import renewals, retention
from documents.models import DocumentDisposal, InsurancePolicy, LegalHold

pytestmark = pytest.mark.django_db


def test_demo_has_every_state(settings):
    settings.SEED_DEMO_ALLOWED = True
    call_command("seed_demo", stdout=io.StringIO())
    items = renewals.tracked()
    tones = {r.tone for r in items}
    assert {"danger", "warning", "success"} <= tones
    assert any(r.kind == "contract" and r.expired for r in items)  # snow: notice passed
    assert any(r.kind == "vendor" and r.expired for r in items)
    assert InsurancePolicy.objects.filter(renews__isnull=False).count() == 1
    assert renewals.overdue_count() >= 3
    p = retention.preview()
    # Ready: the 2017 statements and the pond-dredging contract (ended 8 years ago).
    assert {d.basis for d in p["ready"]} == {"date", "contract_end"}
    assert len(p["ready"]) == 2 and len(p["pending"]) == 1 and len(p["held"]) == 1
    assert DocumentDisposal.objects.count() == 1
    assert LegalHold.objects.count() == 1
    for policy in InsurancePolicy.objects.all():
        assert "(demo)" in policy.carrier
