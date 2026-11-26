"""Milestone 4: opt-in text messages for urgent notices. No network: a fake
provider stands in for Twilio, and the Twilio client is checked against a
mocked urlopen."""

import io
import json
import urllib.error
from datetime import timedelta
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from comms import sms
from comms.models import SmsVerification, UrgentNotice
from community import services as community
from community.models import ContactProfile, Ownership, Person
from community.tests.factories import LotFactory, make_member
from core.models import FeatureSetting
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


class FakeProvider(sms.SmsProvider):
    name = "fake"
    available = True

    def __init__(self, ok=True, opted_out=()):
        self.sent = []
        self.ok = ok
        self.opted_out = set(opted_out)

    def send(self, to, body):
        self.sent.append((to, body))
        if to in self.opted_out:
            return sms.SendResult(ok=False, opted_out=True)
        return sms.SendResult(ok=self.ok)

    def code_for(self, number):
        body = [b for to, b in self.sent if to == number][-1]
        return body.split("your code is ", 1)[1][:6]


@pytest.fixture
def fake():
    provider = FakeProvider()
    with mock.patch("comms.sms.provider", return_value=provider):
        yield provider


@pytest.fixture
def sms_on():
    FeatureSetting.objects.create(key="sms", enabled=True)


@pytest.fixture
def w():
    lot_a, lot_b = LotFactory(street_address="1 Alpha Way"), LotFactory()
    return {
        "owner": make_member(lots=[lot_a]),
        "other": make_member(lots=[lot_b]),
        "board": make_member(board=True),
        "board2": make_member(board=True),
        "lot": lot_a,
    }


def _opt_in(user, number, fake):
    sms.start_verification(user=user, number=number)
    return sms.confirm(user=user, code=fake.code_for(sms.normalize_number(number)))


def _profile(user):
    with system_access("t"):
        return ContactProfile.objects.filter(person__user=user).first()


# --- Numbers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("215-555-0123", "+12155550123"),
        ("(215) 555 0123", "+12155550123"),
        ("1 215 555 0123", "+12155550123"),
        ("+1 215 555 0123", "+12155550123"),
        ("+44 7700 900123", "+447700900123"),
    ],
)
def test_numbers_are_normalized(raw, expected):
    assert sms.normalize_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "555-0123", "015-555-0123", "215-055-0123", "+12", "abc"])
def test_bad_numbers_are_refused(raw):
    with pytest.raises(ValidationError):
        sms.normalize_number(raw)


# --- Opt-in ------------------------------------------------------------------------------


def test_text_pages_are_off_until_the_board_switches_them_on(client, w, fake):
    client.force_login(w["owner"])
    assert client.post(reverse("sms_start"), {"number": "215-555-0123"}).status_code == 404
    assert "Urgent text messages" not in client.get(reverse("my_profile")).content.decode()
    assert fake.sent == []


def test_owner_confirms_a_number_with_a_texted_code(client, w, fake, sms_on):
    client.force_login(w["owner"])
    page = client.get(reverse("my_profile")).content.decode()
    assert "Urgent text messages" in page and "Text me a code" in page
    response = client.post(reverse("sms_start"), {"number": "215-555-0123"})
    assert response.status_code == 302
    ((to, body),) = fake.sent
    assert to == "+12155550123"
    assert "Reply STOP" in body and "expires in 10 minutes" in body
    code = fake.code_for(to)
    verification = SmsVerification.objects.for_user(w["owner"]).get()
    assert code not in verification.code_hash  # only a keyed hash is stored
    assert verification.expires_at - verification.created_at == timedelta(minutes=10)
    page = client.get(reverse("my_profile")).content.decode()
    assert "ending in 0123" in page and 'name="code"' in page
    client.post(reverse("sms_verify"), {"code": code})
    profile = _profile(w["owner"])
    assert profile.sms_number == "+12155550123" and profile.sms_opt_in
    assert profile.sms_verified_at is not None
    page = client.get(reverse("my_profile")).content.decode()
    assert "Texts go to your number ending in 0123" in page
    assert "+12155550123" not in page
    # Audit: who and what, never the number or code.
    events = AuditEvent.objects.filter(action__startswith="sms.")
    assert {e.action for e in events} == {"sms.code_sent", "sms.opted_in"}
    for e in events:
        assert "0123" not in json.dumps(e.metadata) and code not in json.dumps(e.metadata)


def test_wrong_code_counts_attempts_and_locks_after_five(w, fake, sms_on):
    sms.start_verification(user=w["owner"], number="215-555-0123")
    code = fake.code_for("+12155550123")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(sms.MAX_ATTEMPTS):
        with pytest.raises(sms.SmsError):
            sms.confirm(user=w["owner"], code=wrong)
    with pytest.raises(sms.SmsError, match="expired"):
        sms.confirm(user=w["owner"], code=code)
    assert not _profile(w["owner"]) or not _profile(w["owner"]).sms_opt_in


def test_codes_expire_after_ten_minutes(w, fake, sms_on):
    sms.start_verification(user=w["owner"], number="215-555-0123")
    SmsVerification.objects.unscoped("t").update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(sms.SmsError, match="expired"):
        sms.confirm(user=w["owner"], code=fake.code_for("+12155550123"))


def test_a_new_code_replaces_the_old_one(w, fake, sms_on):
    sms.start_verification(user=w["owner"], number="215-555-0123")
    first = fake.code_for("+12155550123")
    sms.start_verification(user=w["owner"], number="215-555-0123")
    second = fake.code_for("+12155550123")
    if first != second:
        with pytest.raises(sms.SmsError):
            sms.confirm(user=w["owner"], code=first)
    sms.confirm(user=w["owner"], code=second)
    assert _profile(w["owner"]).sms_opt_in


def test_codes_are_rate_limited_per_login_and_per_number(w, fake, sms_on):
    for _ in range(sms.MAX_CODES_PER_HOUR):
        sms.start_verification(user=w["owner"], number="215-555-0123")
    with pytest.raises(sms.SmsError, match="Too many"):
        sms.start_verification(user=w["owner"], number="215-555-0199")
    # Someone else cannot use the portal to flood the same number either.
    with pytest.raises(sms.SmsError, match="Too many"):
        sms.start_verification(user=w["other"], number="215-555-0123")
    assert len(fake.sent) == sms.MAX_CODES_PER_HOUR


def test_one_login_cannot_confirm_with_another_logins_code(w, fake, sms_on):
    sms.start_verification(user=w["other"], number="215-555-0123")
    with pytest.raises(sms.SmsError):
        sms.confirm(user=w["owner"], code=fake.code_for("+12155550123"))
    assert SmsVerification.objects.for_user(w["owner"]).count() == 0


def test_only_owners_opt_in_and_former_owners_cannot(w, fake, sms_on):
    with pytest.raises(PermissionDenied):
        sms.start_verification(user=w["board"], number="215-555-0123")  # no lot
    with system_access("t"):
        ownership = Ownership.objects.get(person__user=w["owner"])
    community.end_ownership(ownership, actor=SYSTEM)
    with pytest.raises(PermissionDenied):
        sms.start_verification(user=w["owner"], number="215-555-0123")
    assert fake.sent == []


def test_no_provider_means_no_texts(w, sms_on, client):
    client.force_login(w["owner"])
    assert "aren't set up yet" in client.get(reverse("my_profile")).content.decode()
    with pytest.raises(sms.SmsNotConfigured):
        sms.start_verification(user=w["owner"], number="215-555-0123")


def test_owner_can_stop_texts(client, w, fake, sms_on):
    _opt_in(w["owner"], "215-555-0123", fake)
    client.force_login(w["owner"])
    client.post(reverse("sms_stop"))
    profile = _profile(w["owner"])
    assert profile.sms_number == "" and not profile.sms_opt_in
    assert AuditEvent.objects.get(action="sms.opted_out").metadata == {"by": "owner"}
    assert community.sms_recipients() == []


def test_text_endpoints_need_sign_in(client, sms_on):
    for name in ("sms_start", "sms_verify", "sms_stop"):
        response = client.post(reverse(name))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))


def test_texts_never_appear_in_the_directory(client, w, fake, sms_on):
    _opt_in(w["owner"], "215-555-0123", fake)
    community.update_my_profile(
        user=w["owner"], in_directory=True, directory_show_phone=True, weekly_digest=False
    )
    client.force_login(w["other"])
    assert "555-0123" not in client.get(reverse("directory")).content.decode()
    assert "0123" not in client.get(reverse("directory")).content.decode()


# --- Urgent notices ----------------------------------------------------------------------


def test_owners_cannot_send_urgent_texts(client, w, fake, sms_on):
    client.force_login(w["owner"])
    assert client.get(reverse("urgent_notice")).status_code == 403
    assert (
        client.post(reverse("urgent_notice"), {"message": "x", "link": "home"}).status_code == 403
    )
    with pytest.raises(PermissionDenied):
        sms.send_urgent_notice(actor=w["owner"], message="Water off", link="home")


def test_board_sends_an_urgent_text_to_current_owners_who_opted_in(
    client, w, fake, sms_on, django_capture_on_commit_callbacks
):
    _opt_in(w["owner"], "215-555-0123", fake)
    _opt_in(w["other"], "215-555-0177", fake)
    # A pending (unconfirmed) number never gets texts.
    third = make_member(lots=[LotFactory()])
    sms.start_verification(user=third, number="215-555-0188")
    # A former owner who had opted in stops getting texts once they sell.
    with system_access("t"):
        other_ownership = Ownership.objects.get(person__user=w["other"])
    community.end_ownership(other_ownership, actor=SYSTEM)
    fake.sent.clear()

    client.force_login(w["board"])
    page = client.get(reverse("urgent_notice"))
    assert page.status_code == 200 and b"1 owner opted in" in page.content
    with (
        mock.patch("comms.tasks.send_urgent_sms.defer") as defer,
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = client.post(
            reverse("urgent_notice"),
            {"message": "Water main break on Alpha Way; water off until 5 pm.", "link": "home"},
        )
    assert response.status_code == 302
    notice = UrgentNotice.objects.get()
    defer.assert_called_once_with(notice_id=notice.pk)
    assert notice.recipients == 1
    counts = sms.deliver_urgent_notice(notice.pk)
    assert counts == {"sent": 1, "failed": 0, "opted_out": 0}
    ((to, body),) = fake.sent
    assert to == "+12155550123"
    assert body.startswith(f"{sms.settings.SITE_NAME} urgent: Water main break")
    assert "Sign in to read more: " in body and body.endswith(sms.STOP_NOTE)
    with system_access("t"):
        owner_name = Person.objects.get(user=w["owner"]).display_name
    assert owner_name not in body and w["owner"].email not in body
    # Audit: counts only.
    queued = AuditEvent.objects.get(action="sms.urgent_queued")
    assert queued.actor == w["board"]
    assert set(queued.metadata) == {"notice_id", "recipients"}
    delivered = AuditEvent.objects.get(action="sms.urgent_delivered")
    assert delivered.metadata == {"notice_id": notice.pk, "sent": 1, "failed": 0, "opted_out": 0}
    # Delivering twice never texts twice.
    assert sms.deliver_urgent_notice(notice.pk)["sent"] == 0
    assert len(fake.sent) == 1


def test_urgent_text_links_only_to_signed_in_pages(w, fake, sms_on):
    from comms.models import Announcement, Audience

    board_only = Announcement.objects.create(
        title="Board only", body="x", audience=Audience.BOARD, published_by=w["board"]
    )
    members = Announcement.objects.create(
        title="Water", body="x", audience=Audience.MEMBERS, published_by=w["board"]
    )
    assert sms.link_path(f"announcement:{members.pk}") == reverse(
        "announcement_detail", args=[members.pk]
    )
    for bad in (f"announcement:{board_only.pk}", "https://evil.example", "announcement:x", ""):
        with pytest.raises(sms.SmsError):
            sms.link_path(bad)


@pytest.mark.parametrize(
    "message,error",
    [
        ("", "Write the message"),
        ("x" * 301, "300 characters"),
        ("Lot 4 owes $1,200", "amounts"),
        ("Contact jane@example.com", "email"),
    ],
)
def test_urgent_text_wording_is_checked(w, fake, sms_on, message, error):
    _opt_in(w["owner"], "215-555-0123", fake)
    with pytest.raises(sms.SmsError, match=error):
        sms.send_urgent_notice(actor=w["board"], message=message, link="home")
    assert not UrgentNotice.objects.exists()


def test_urgent_text_needs_a_provider_and_someone_opted_in(w, sms_on, fake):
    with pytest.raises(sms.SmsError, match="No owners"):
        sms.send_urgent_notice(actor=w["board"], message="Road closed", link="home")
    with (
        mock.patch("comms.sms.provider", return_value=sms.SmsProvider()),
        pytest.raises(sms.SmsNotConfigured),
    ):
        sms.send_urgent_notice(actor=w["board"], message="Road closed", link="home")


def test_a_stop_reply_reported_by_the_provider_turns_texts_off(w, fake, sms_on):
    _opt_in(w["owner"], "215-555-0123", fake)
    notice = sms.send_urgent_notice(actor=w["board"], message="Road closed", link="calendar")
    fake.opted_out.add("+12155550123")
    counts = sms.deliver_urgent_notice(notice.pk)
    assert counts == {"sent": 0, "failed": 1, "opted_out": 1}
    assert not _profile(w["owner"]).sms_opt_in
    assert community.sms_recipients() == []


def test_urgent_notices_are_kept(w, fake, sms_on):
    _opt_in(w["owner"], "215-555-0123", fake)
    notice = sms.send_urgent_notice(actor=w["board"], message="Road closed", link="home")
    with pytest.raises(PermissionError):
        notice.delete()


# --- Twilio client (mocked HTTP) ---------------------------------------------------------


@pytest.fixture
def twilio(settings):
    settings.SMS_PROVIDER = "twilio"
    settings.SMS_ACCOUNT_SID = "ACtest"
    settings.SMS_AUTH_TOKEN = "test-token"  # nosec B105 - not a real token
    settings.SMS_FROM = "+12155550100"
    settings.SMS_API_BASE = "https://api.twilio.example"


class _Response(io.BytesIO):
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_twilio_provider_posts_the_message(twilio):
    out = sms.provider()
    assert isinstance(out, sms.TwilioProvider) and out.available
    with mock.patch("urllib.request.urlopen", return_value=_Response(b"{}")) as urlopen:
        result = out.send("+12155550123", "Hello")
    assert result.ok
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://api.twilio.example/2010-04-01/Accounts/ACtest/Messages.json"
    assert request.get_method() == "POST"
    assert request.headers["Authorization"].startswith("Basic ")
    assert b"To=%2B12155550123" in request.data and b"Body=Hello" in request.data


def test_twilio_unsubscribed_recipient_is_reported_as_opted_out(twilio):
    error = urllib.error.HTTPError(
        "https://api.twilio.example", 400, "Bad", {}, io.BytesIO(b'{"code": 21610}')
    )
    with mock.patch("urllib.request.urlopen", side_effect=error):
        result = sms.provider().send("+12155550123", "Hello")
    assert not result.ok and result.opted_out


def test_twilio_needs_every_setting_and_https(twilio, settings):
    settings.SMS_API_BASE = "http://api.twilio.example"
    assert not sms.provider().available
    settings.SMS_API_BASE = "https://api.twilio.example"
    settings.SMS_AUTH_TOKEN = ""  # nosec B105 - unset on purpose
    out = sms.provider()
    assert not out.available
    with pytest.raises(sms.SmsNotConfigured):
        out.send("+12155550123", "Hello")


def test_default_provider_sends_nothing(settings):
    assert settings.SMS_PROVIDER == "none"
    assert not sms.provider().available
    with pytest.raises(sms.SmsNotConfigured):
        sms.provider().send("+12155550123", "Hello")


# --- R2-008: turning texts off always works ---------------------------------------


def test_number_can_be_removed_while_texts_are_switched_off(client, w, fake, sms_on):
    _opt_in(w["owner"], "215-555-0123", fake)
    FeatureSetting.objects.filter(key="sms").update(enabled=False)
    client.force_login(w["owner"])
    page = client.get(reverse("my_profile")).content.decode()
    assert "switched off for the community" in page
    assert reverse("sms_stop") in page and "Stop texts and remove my number" in page
    assert reverse("sms_start") not in page  # no new sign-ups while off
    assert client.post(reverse("sms_start"), {"number": "215-555-0199"}).status_code == 404
    response = client.post(reverse("sms_stop"))
    assert response.status_code == 302
    profile = _profile(w["owner"])
    assert profile.sms_number == "" and not profile.sms_opt_in
    assert AuditEvent.objects.filter(action="sms.opted_out").exists()
    # Nothing stored: the panel is gone.
    assert reverse("sms_stop") not in client.get(reverse("my_profile")).content.decode()


def test_stop_texts_only_touches_my_own_number(client, w, fake, sms_on):
    _opt_in(w["owner"], "215-555-0123", fake)
    _opt_in(w["other"], "215-555-0124", fake)
    FeatureSetting.objects.filter(key="sms").update(enabled=False)
    client.force_login(w["owner"])
    client.post(reverse("sms_stop"))
    assert _profile(w["other"]).sms_number != ""
    assert _profile(w["owner"]).sms_number == ""
