"""The public vendor form (D79): the only pages besides the sign-in steps that
open without signing in. The emailed token is swapped for a short session
bound to one round; that session reaches nothing else in the portal."""

from __future__ import annotations

import itertools
from unittest import mock

import pytest
from django.test import Client
from django.urls import NoReverseMatch, get_resolver, reverse

from community.models import Role
from community.tests.factories import make_member
from core.tests.test_url_matrix import OLD_EMAIL_LINKS, PUBLIC
from operations import onboarding_views as ov
from operations import vendor_onboarding as vo
from operations.models import OnboardingRound, RoundStatus, VendorFile
from operations.tests.vendor_helpers import FAKE_W9, enable, pdf, send_open_link

pytestmark = pytest.mark.django_db
MARKER = "Zebra Ponds Unlimited (demo)"
FORM_PAGES = {
    "vendor_onboarding_start",
    "vendor_onboarding_form",
    "vendor_onboarding_upload",
    "vendor_onboarding_submit",
    "vendor_onboarding_done",
}


@pytest.fixture
def w(settings):
    enable(settings)
    manager = make_member(roles=[Role.MANAGER])
    vendor = vo.invite_vendor(
        name="Pond Pros (demo)",
        email="vendor@example.test",
        services="Pond care",
        vendor_type="contractor",
        actor=manager,
    )
    rnd = vendor.onboarding_rounds.get()
    return {"manager": manager, "vendor": vendor, "rnd": rnd, "token": send_open_link(rnd)}


def _open(client, token):
    return client.get(reverse("vendor_onboarding_start", args=[token]))


def test_opening_the_link_swaps_the_token_for_a_session_and_leaves_the_url(w):
    client = Client()
    response = _open(client, w["token"])
    assert response.status_code == 302
    assert response["Location"] == reverse("vendor_onboarding_form")
    assert w["token"] not in response["Location"]
    assert response["Referrer-Policy"] == "no-referrer"
    assert "no-store" in response["Cache-Control"]
    assert "_auth_user_id" not in client.session
    page = client.get(reverse("vendor_onboarding_form"))
    assert page.status_code == 200
    body = page.content.decode()
    assert w["token"] not in body
    # Only the association's name and what to provide: no portal navigation.
    assert 'class="sidebar"' not in body and "nav-search" not in body
    assert 'href="/"' not in body and "/finance/" not in body and "/my-" not in body
    assert "<script" not in body
    csp = page["Content-Security-Policy"]
    assert "script-src 'none'" in csp and "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert page["Referrer-Policy"] == "no-referrer"


def test_the_link_reopens_until_submission_and_counts_opens(w):
    first, second = Client(), Client()
    _open(first, w["token"])
    _open(second, w["token"])
    assert second.get(reverse("vendor_onboarding_form")).status_code == 200
    w["rnd"].links.get().refresh_from_db()
    assert w["rnd"].links.get().opened_count == 2


@pytest.mark.parametrize("token", ["nope", "A" * 43, "x" * 150])
def test_unknown_tokens_get_the_neutral_answer(w, token):
    response = _open(Client(), token)
    assert response.status_code == 404
    assert b"This link can" in response.content
    assert b"Pond" not in response.content


def test_expired_replaced_and_used_links_answer_the_same(w):
    from django.utils import timezone

    neutral = _open(Client(), "unknown-token-xyz").content
    vo.resend_link(w["rnd"], actor=w["manager"])
    replaced = _open(Client(), w["token"])
    assert replaced.status_code == 404 and replaced.content == neutral
    token = send_open_link(w["rnd"])
    w["rnd"].links.filter(ended_at__isnull=True).update(expires_at=timezone.now())
    expired = _open(Client(), token)
    assert expired.status_code == 404 and expired.content == neutral


def test_a_replaced_link_ends_a_session_already_open(w):
    client = Client()
    _open(client, w["token"])
    vo.resend_link(w["rnd"], actor=w["manager"])
    assert client.get(reverse("vendor_onboarding_form")).status_code == 404


def test_signed_in_users_are_refused(w, client):
    client.force_login(w["manager"])
    response = _open(client, w["token"])
    assert response.status_code == 400
    assert client.get(reverse("vendor_onboarding_form")).status_code == 404


def test_session_expires(w):
    client = Client()
    _open(client, w["token"])
    session = client.session
    session[ov.SESSION_STARTED] = session[ov.SESSION_STARTED] - vo.SESSION_SECONDS - 5
    session.save()
    assert client.get(reverse("vendor_onboarding_form")).status_code == 404


def test_opening_is_rate_limited_per_link_and_per_client(w):
    client = Client()
    for _ in range(ov.LIMITS["open_token"]):
        assert _open(client, "same-bad-token").status_code == 404
    assert _open(client, "same-bad-token").status_code == 429
    for i in range(ov.LIMITS["open_ip"] - ov.LIMITS["open_token"]):
        _open(client, f"other-{i}")
    response = _open(client, w["token"])
    assert response.status_code == 429  # even a good link, from this client, for an hour


def test_full_flow_through_the_pages(w):
    client = Client()
    _open(client, w["token"])
    form_url = reverse("vendor_onboarding_form")
    data = {
        "legal_name": "Pond Pros LLC (demo)",
        "entity_type": "c_corp",
        "contact_name": "Pat Demo",
        "contact_email": "pat@example.test",
        "contact_phone": "610-555-0100",
        "remit_address": "1 Demo Lane",
        "services": "Pond care",
        "conflict": "no",
        "terms_accepted": "on",
        "w9_revision_confirmed": "on",
        "coi_gl_carrier": "Demo Mutual",
        "coi_gl_amount_cents": "1,000,000",
        "coi_gl_expires": "2030-01-31",
        "coi_auto_carrier": "Demo Mutual",
        "coi_auto_amount_cents": "1000000",
        "coi_auto_expires": "2030-01-31",
        "workers_comp_mode": "exempt",
    }
    assert client.post(form_url, data).status_code == 302
    bad = client.post(form_url, dict(data, services="EIN 12-3456789"))
    assert bad.status_code == 200 and b"tax ID" in bad.content
    for kind in ("w9", "coi_gl", "coi_auto", "workers_comp"):
        response = client.post(
            reverse("vendor_onboarding_upload"), {"kind": kind, "file": pdf(f"{kind}.pdf")}
        )
        assert response.status_code == 302
    page = client.get(form_url).content.decode()
    assert "w9.pdf" in page and "Still needed" not in page
    # The vendor never downloads anything back.
    assert "/files/" not in page
    response = client.post(reverse("vendor_onboarding_submit"))
    assert response["Location"] == reverse("vendor_onboarding_done")
    rnd = OnboardingRound.objects.get(pk=w["rnd"].pk)
    assert rnd.status == RoundStatus.SUBMITTED
    assert rnd.answers["coi_gl_amount_cents"] == 100000000
    # The session is over and the link used up.
    assert client.get(form_url).status_code == 404
    assert _open(Client(), w["token"]).status_code == 404


def test_upload_refuses_large_files_before_buffering(w, settings):
    client = Client()
    _open(client, w["token"])
    big = pdf("big.pdf", b"%PDF-1.4\n" + b"0" * (vo.MAX_FILE_BYTES + 10))
    with mock.patch("operations.vendor_onboarding.upload_file") as upload:
        response = client.post(reverse("vendor_onboarding_upload"), {"kind": "w9", "file": big})
    assert response.status_code == 302
    upload.assert_not_called()
    assert not VendorFile.objects.exists()


def test_upload_refuses_non_pdf_and_needs_csrf(w):
    client = Client(enforce_csrf_checks=True)
    _open(client, w["token"])
    response = client.post(reverse("vendor_onboarding_upload"), {"kind": "w9", "file": pdf()})
    assert response.status_code == 403  # no CSRF token
    client = Client()
    _open(client, w["token"])
    client.post(
        reverse("vendor_onboarding_upload"),
        {"kind": "w9", "file": pdf("x.pdf", b"<html>not a pdf</html>")},
    )
    assert not VendorFile.objects.exists()
    client.post(reverse("vendor_onboarding_upload"), {"kind": "w9", "file": pdf()})
    assert VendorFile.objects.count() == 1


def test_feature_off_closes_the_form(w):
    from core.models import FeatureSetting

    FeatureSetting.objects.filter(key="vendor_onboarding").update(enabled=False)
    assert _open(Client(), w["token"]).status_code == 404


# --- The session reaches nothing else -----------------------------------------------------

_POOL = [
    "1",
    "x",
    "approve",
    "update",
    "propose",
    "done",
    "statement",
    "first",
    "charge",
    "xlsx",
    "2026",
    "request",
    "violation",
    "revoke",
    "open",
    "archive",
    "notice",
    "withdraw",
    "decision",
    "csv",
]


def _all_urls():
    """One concrete URL for every named route (dummy arguments)."""
    resolver = get_resolver()
    out = {}
    for name in [k for k in resolver.reverse_dict if isinstance(k, str)]:
        params = set()
        for possibility, _pattern, _defaults, _converters in resolver.reverse_dict.getlist(name):
            for _fmt, names in possibility:
                params |= set(names)
        params = sorted(params)
        url = None
        for combo in itertools.product(_POOL, repeat=min(len(params), 3)):
            kwargs = dict(zip(params, combo, strict=False))
            try:
                url = reverse(name, kwargs=kwargs)
                break
            except NoReverseMatch:
                continue
        if url is not None:
            out[name] = url
    return out


def test_the_vendor_session_reaches_nothing_else(w, client):
    from core.models import FeatureSetting

    for key in ("push", "sms", "bank_feeds", "usps_tracking"):
        FeatureSetting.objects.update_or_create(key=key, defaults={"enabled": True})
    vo.save_answers(w["rnd"], {"legal_name": MARKER})
    _open(client, w["token"])
    assert MARKER in client.get(reverse("vendor_onboarding_form")).content.decode()
    urls = _all_urls()
    assert len(urls) > 300  # nearly every route was exercised
    login = reverse("account_login")
    allowed_open = PUBLIC | OLD_EMAIL_LINKS
    for name, url in sorted(urls.items()):
        if name in FORM_PAGES:
            continue
        response = client.get(url)
        assert MARKER.encode() not in response.content, name
        if name in allowed_open or name.startswith("account_"):
            continue  # sign-in steps: open to anyone, show nothing of the draft
        if response.status_code in (301, 302):
            location = response["Location"]
            assert location.startswith(login) or not location.startswith("/vendor-form"), name
            continue
        assert response.status_code in (400, 403, 404, 405), (name, response.status_code)
    # Still a vendor session, never a portal login.
    assert "_auth_user_id" not in client.session


def test_the_draft_of_one_round_is_not_visible_from_another(w):
    other = vo.invite_vendor(
        name="Other Vendor (demo)",
        email="other@example.test",
        services="",
        vendor_type="supplier",
        actor=w["manager"],
    ).onboarding_rounds.get()
    vo.save_answers(other, {"legal_name": MARKER})
    client = Client()
    _open(client, w["token"])
    assert MARKER not in client.get(reverse("vendor_onboarding_form")).content.decode()
    # Uploads land in the session's own round only.
    client.post(reverse("vendor_onboarding_upload"), {"kind": "w9", "file": pdf()})
    assert VendorFile.objects.get().round_id == w["rnd"].pk
    with VendorFile.objects.get().file.open("rb") as fh:
        assert fh.read() != FAKE_W9
