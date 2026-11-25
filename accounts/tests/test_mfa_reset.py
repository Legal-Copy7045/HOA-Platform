"""Assisted reset after losing every authenticator (decision D5)."""

import re
from datetime import timedelta
from unittest import mock

import pytest
from allauth.mfa.models import Authenticator
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from accounts import mfa_reset
from accounts.models import MfaReset, VerificationMethod
from accounts.tests.helpers import PASSWORD
from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member

pytestmark = pytest.mark.django_db
PHONE = VerificationMethod.PHONE_KNOWN_NUMBER


@pytest.fixture
def board_a():
    return make_member(board=True)


@pytest.fixture
def board_b():
    return make_member(board=True)


@pytest.fixture
def owner():
    user = make_member(lots=[LotFactory()])
    user.set_password(PASSWORD)
    user.save()
    return user


@pytest.fixture(autouse=True)
def _no_notices():
    with mock.patch("community.tasks.notify_board.defer"):
        yield


def approved_reset(owner, board_a, board_b):
    reset = mfa_reset.start_reset(subject=owner, verification_method=PHONE, actor=board_a)
    return reset, mfa_reset.approve_reset(reset, actor=board_b)


def test_two_board_members_reset_and_the_code_is_never_stored(owner, board_a, board_b):
    reset, code = approved_reset(owner, board_a, board_b)
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    reset.refresh_from_db()
    assert code.replace("-", "") not in reset.code_hash
    assert not Authenticator.objects.filter(user=owner).exists()
    started = AuditEvent.objects.get(action="mfa_reset.started")
    assert started.metadata == {"user_id": owner.pk, "verification_method": PHONE}
    assert AuditEvent.objects.filter(action="mfa_reset.approved", actor=board_b).exists()


def test_the_starter_cannot_approve(owner, board_a):
    reset = mfa_reset.start_reset(subject=owner, verification_method=PHONE, actor=board_a)
    with pytest.raises(mfa_reset.MfaResetError, match="different board member"):
        mfa_reset.approve_reset(reset, actor=board_a)


def test_board_member_cannot_reset_or_approve_their_own_account(board_a, board_b):
    with pytest.raises(mfa_reset.MfaResetError):
        mfa_reset.start_reset(subject=board_a, verification_method=PHONE, actor=board_a)
    reset = mfa_reset.start_reset(subject=board_b, verification_method=PHONE, actor=board_a)
    with pytest.raises(mfa_reset.MfaResetError, match="own account"):
        mfa_reset.approve_reset(reset, actor=board_b)


@pytest.mark.parametrize("roles", [[Role.SYSADMIN], [Role.AUDITOR]])
def test_only_board_members_take_part(owner, board_a, roles):
    outsider = make_member(roles=roles)
    with pytest.raises(PermissionDenied):
        mfa_reset.start_reset(subject=owner, verification_method=PHONE, actor=outsider)
    reset = mfa_reset.start_reset(subject=owner, verification_method=PHONE, actor=board_a)
    with pytest.raises(PermissionDenied):
        mfa_reset.approve_reset(reset, actor=outsider)


def test_approval_signs_the_user_out_everywhere(client, owner, board_a, board_b):
    client.force_login(owner)
    assert client.get(reverse("home")).status_code == 200
    approved_reset(owner, board_a, board_b)
    response = client.get(reverse("home"))
    assert "_auth_user_id" not in client.session
    assert response.status_code == 200 and b"Sign in" in response.content


def test_after_reset_only_the_code_page_works_until_the_code_is_entered(
    client, owner, board_a, board_b
):
    _, code = approved_reset(owner, board_a, board_b)
    # Password alone now signs in (no authenticators left)...
    client.post(reverse("account_login"), {"login": owner.email, "password": PASSWORD})
    # ...but every page, including MFA enrollment, sends them to the code page.
    for name in ("home", "mfa_index", "mfa_activate_totp"):
        assert client.get(reverse(name))["Location"] == reverse("enroll_with_code")
    response = client.post(reverse("enroll_with_code"), {"code": code.lower()})
    assert response["Location"] == reverse("mfa_index")
    assert client.get(reverse("mfa_index")).status_code == 200
    assert client.get(reverse("home"))["Location"] == reverse("mfa_index")  # enroll next
    assert AuditEvent.objects.filter(action="mfa_reset.code_redeemed", actor=owner).exists()


def test_five_wrong_codes_lock_the_reset(client, owner, board_a, board_b):
    reset, code = approved_reset(owner, board_a, board_b)
    owner.refresh_from_db()  # approval bumped session_generation
    client.force_login(owner)
    for _ in range(5):
        client.post(reverse("enroll_with_code"), {"code": "AAAA-AAAA-AAAA"})
    reset.refresh_from_db()
    assert reset.status == "code_locked"
    response = client.post(reverse("enroll_with_code"), {"code": code})
    assert b"no longer be used" in response.content
    assert client.get(reverse("mfa_index"))["Location"] == reverse("enroll_with_code")


def test_expired_code_cannot_be_used(client, owner, board_a, board_b):
    reset, code = approved_reset(owner, board_a, board_b)
    MfaReset.objects.unscoped("test").filter(pk=reset.pk).update(
        code_expires_at=timezone.now() - timedelta(minutes=1)
    )
    owner.refresh_from_db()  # approval bumped session_generation
    client.force_login(owner)
    assert not mfa_reset.redeem_code(user=owner, code=code)
    assert b"no longer be used" in client.get(reverse("enroll_with_code")).content


def test_pending_reset_can_be_cancelled_and_changes_nothing(owner, board_a, board_b):
    reset = mfa_reset.start_reset(subject=owner, verification_method=PHONE, actor=board_a)
    mfa_reset.cancel_reset(reset, actor=board_b)
    assert Authenticator.objects.filter(user=owner).exists()
    with pytest.raises(mfa_reset.MfaResetError):
        mfa_reset.approve_reset(reset, actor=board_b)


def test_identity_check_method_is_required(owner, board_a):
    with pytest.raises(mfa_reset.MfaResetError):
        mfa_reset.start_reset(subject=owner, verification_method="vibes", actor=board_a)
