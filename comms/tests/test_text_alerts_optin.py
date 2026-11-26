"""D40: text alerts are an opt-in under Phone on My profile: off by default,
never pre-ticked, a texted code still required, unticking stops them at once,
and nothing about texts shows while the feature is off."""

import pytest
from django.urls import reverse

from audit.models import AuditEvent
from comms.tests.test_sms import _opt_in, _profile, fake, sms_on, w  # noqa: F401 - fixtures
from core.models import FeatureSetting

pytestmark = pytest.mark.django_db

LABEL = "Receive text alerts (urgent community notices only)"


def _texts(page: str) -> str:
    return page[page.index('id="texts"') : page.index('id="mailing"')]


def test_checkbox_is_off_by_default_and_never_preticked(client, w, fake, sms_on):  # noqa: F811
    client.force_login(w["owner"])
    page = client.get(reverse("my_profile")).content.decode()
    texts = _texts(page)
    assert LABEL in texts
    assert 'name="opt_in">' in texts and "checked" not in texts
    assert "Message and data rates may apply" in texts and "Reply STOP" in texts
    # The number field only shows once the box is ticked (static/js/forms.js).
    assert 'data-show-if="opt_in=on"' in texts and "Send me a code" in texts
    assert page.index("<h3>Phone</h3>") < page.index('id="texts"')
    assert page.index('id="texts"') < page.index("<h3>Mailing address for post</h3>")


def test_a_code_is_not_sent_unless_the_box_is_ticked(client, w, fake, sms_on):  # noqa: F811
    client.force_login(w["owner"])
    client.post(reverse("sms_start"), {"number": "215-555-0123"})
    assert fake.sent == []
    profile = _profile(w["owner"])
    assert profile is None or not profile.sms_opt_in


def test_opting_in_needs_the_texted_code(client, w, fake, sms_on):  # noqa: F811
    client.force_login(w["owner"])
    client.post(reverse("sms_start"), {"opt_in": "on", "number": "215-555-0123"})
    assert len(fake.sent) == 1
    profile = _profile(w["owner"])
    assert profile is None or not profile.sms_opt_in  # not yet: code first
    code = fake.code_for("+12155550123")
    client.post(reverse("sms_verify"), {"code": code})
    profile = _profile(w["owner"])
    assert profile.sms_opt_in and profile.sms_verified_at
    page = client.get(reverse("my_profile")).content.decode()
    texts = _texts(page)
    assert "checked" in texts and "ending in 0123" in texts


def test_unticking_turns_texts_off_at_once_and_is_audited(client, w, fake, sms_on):  # noqa: F811
    _opt_in(w["owner"], "215-555-0123", fake)
    client.force_login(w["owner"])
    # Saved still ticked: nothing changes.
    client.post(reverse("sms_stop"), {"opt_in": "on"})
    assert _profile(w["owner"]).sms_opt_in
    client.post(reverse("sms_stop"), {})
    profile = _profile(w["owner"])
    assert not profile.sms_opt_in and profile.sms_number == ""
    assert AuditEvent.objects.filter(action="sms.opted_out", metadata={"by": "owner"}).exists()


def test_nothing_about_texts_when_the_feature_is_off(client, w, fake):  # noqa: F811
    client.force_login(w["owner"])
    page = client.get(reverse("my_profile")).content.decode()
    assert LABEL not in page and 'id="texts"' not in page and "Text alerts" not in page
    FeatureSetting.objects.create(key="sms", enabled=False)
    page = client.get(reverse("my_profile")).content.decode()
    assert LABEL not in page and 'id="texts"' not in page


def test_adding_a_phone_never_signs_up_for_texts(client, w, fake, sms_on):  # noqa: F811
    client.force_login(w["owner"])
    client.post(
        reverse("my_profile"),
        {"section": "phone_add", "phone_type": "mobile", "number": "215-555-0123"},
    )
    assert fake.sent == []
    profile = _profile(w["owner"])
    assert not profile.sms_opt_in and profile.sms_number == ""
