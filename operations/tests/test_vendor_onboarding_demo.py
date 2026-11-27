"""Demo vendor onboarding states (D79): every state, fictional data only."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from operations import vendor_onboarding as vo
from operations.models import (
    OnboardingRound,
    ProfileStatus,
    RoundStatus,
    Vendor,
    VendorFile,
    VendorProfile,
)
from operations.tests.vendor_helpers import KEY

pytestmark = pytest.mark.django_db


def test_oak_creek_seeds_every_onboarding_state_with_fake_w9s(settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.VENDOR_DOCUMENT_KEY = KEY
    call_command("seed_demo", stdout=io.StringIO())
    statuses = dict(VendorProfile.objects.values_list("vendor__name", "status"))
    assert statuses["Brightline Electric (demo)"] == ProfileStatus.INVITED
    assert statuses["Keystone Paving (demo)"] == ProfileStatus.SUBMITTED
    assert statuses["Valley Tree Care (demo)"] == ProfileStatus.SUBMITTED
    assert statuses["Quick Fix Handyman (demo)"] == ProfileStatus.RETURNED
    assert statuses["Cheap Seal Coating (demo)"] == ProfileStatus.DECLINED
    assert statuses["Summit Reserve Studies (demo)"] == ProfileStatus.ACTIVE
    snow = Vendor.objects.get(name="Creekside Snow Removal (demo)")
    assert vo.is_active(snow) and not vo.coi_current(snow)
    tree = OnboardingRound.objects.get(vendor__name="Valley Tree Care (demo)")
    assert tree.status == RoundStatus.SUBMITTED
    assert len(vo.approvers(vo.latest_submission(tree))) == 1
    # Older seeded vendors are grandfathered; one is missing documents.
    assert any(
        vo.missing_for_grandfathered(v) for v in Vendor.objects.all() if vo.is_grandfathered(v)
    )
    # The fake W-9s say SAMPLE and are stored encrypted.
    w9 = VendorFile.objects.filter(kind="w9").first()
    with w9.file.open("rb") as fh:
        stored = fh.read()
    assert b"SAMPLE" not in stored
    from operations import vendor_crypto

    assert b"000-00-0000 SAMPLE" in vendor_crypto.decrypt(stored, w9.storage_name)


def test_without_a_key_the_demo_skips_onboarding(settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.VENDOR_DOCUMENT_KEY = ""
    call_command("seed_demo", stdout=io.StringIO())
    assert not VendorProfile.objects.exists()
