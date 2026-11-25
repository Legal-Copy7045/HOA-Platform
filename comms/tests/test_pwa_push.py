"""Milestone 4: installable app (manifest, service worker) and push notifications."""

import json
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from django.conf import settings
from django.core.management import call_command
from django.templatetags.static import static
from django.test import Client
from django.urls import reverse

from audit.models import AuditEvent
from comms import push, webpush
from comms.models import PushSubscription
from community.tests.factories import LotFactory, make_member
from core.models import FeatureSetting

pytestmark = pytest.mark.django_db

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
# RFC 8291 Appendix A: the user agent's keys.
UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
UA_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"


@pytest.fixture
def vapid(settings):
    public, private = webpush.generate_vapid_keys()
    settings.VAPID_PUBLIC_KEY = public
    settings.VAPID_PRIVATE_KEY = private
    settings.VAPID_SUBJECT = "mailto:board@example.com"
    return public, private


@pytest.fixture
def push_on(vapid):
    FeatureSetting.objects.create(key="push", enabled=True)
    return vapid


@pytest.fixture
def w():
    lot_a, lot_b = LotFactory(), LotFactory()
    return {
        "owner": make_member(lots=[lot_a]),
        "other": make_member(lots=[lot_b]),
        "board": make_member(board=True),
        "lot": lot_a,
    }


def _subscribe(client, endpoint=ENDPOINT):
    return client.post(
        reverse("push_subscribe"),
        json.dumps({"endpoint": endpoint, "keys": {"p256dh": UA_PUBLIC, "auth": UA_AUTH}}),
        content_type="application/json",
    )


# --- Installable app ---------------------------------------------------------------------


def test_manifest_and_service_worker_need_sign_in(client):
    for name in ("pwa_manifest", "pwa_service_worker"):
        response = client.get(reverse(name))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))


def test_manifest_names_the_site_and_its_icons_exist(client, w):
    client.force_login(w["owner"])
    response = client.get(reverse("pwa_manifest"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/manifest+json"
    data = json.loads(response.content)
    assert data["name"] == settings.SITE_NAME
    assert len(data["short_name"]) <= 12
    assert data["start_url"] == "/" and data["display"] == "standalone"
    static_root = Path(settings.BASE_DIR) / "static"
    for icon in data["icons"]:
        relative = icon["src"].removeprefix(static(""))
        assert (static_root / relative).is_file(), icon["src"]


def test_service_worker_is_at_the_root_and_caches_static_files_only(client, w):
    assert reverse("pwa_service_worker") == "/sw.js"  # scope "/" needs the root
    client.force_login(w["owner"])
    response = client.get(reverse("pwa_service_worker"))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/javascript")
    assert response["Cache-Control"] == "no-cache"
    body = response.content.decode()
    config = json.loads(body.split("const CONFIG = ", 1)[1].split(";\n", 1)[0])
    prefix = static("")
    assert config["staticPrefix"] == prefix
    assert config["precache"] and all(p.startswith(prefix) for p in config["precache"])
    # Only GETs under the static prefix are intercepted; everything else
    # (pages, downloads) goes to the network untouched.
    assert "url.pathname.startsWith(CONFIG.staticPrefix)) return;" in body
    assert 'request.method !== "GET") return;' in body
    assert w["owner"].email not in body


def test_signed_in_pages_offer_the_app_and_sign_in_page_does_not(client, w):
    page = client.get(reverse("account_login")).content.decode()
    assert "manifest" not in page and "data-sw" not in page
    client.force_login(w["owner"])
    page = client.get(reverse("my_profile")).content.decode()
    assert f'rel="manifest" href="{reverse("pwa_manifest")}"' in page
    assert 'data-sw="/sw.js"' in page
    assert "Install the app" in page
    assert "Notifications on this device" not in page  # push feature off by default


def test_content_security_policy_stays_strict(client, w):
    client.force_login(w["owner"])
    csp = client.get(reverse("my_profile"))["Content-Security-Policy"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if d.strip())
    assert directives["worker-src"] == "'self'"
    assert directives["manifest-src"] == "'self'"
    assert directives["script-src"] == "'self'"
    assert "unsafe" not in csp


# --- Web Push crypto ---------------------------------------------------------------------


def test_encryption_matches_the_rfc_8291_test_vector():
    body = webpush.encrypt(
        b"When I grow up, I want to be a watermelon",
        p256dh=UA_PUBLIC,
        auth=UA_AUTH,
        _sender_key=webpush.private_key_from_b64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
        _salt=webpush.b64u_decode("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert webpush.b64u_encode(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmY"
        "WAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQe"
        "xSgSxsj_Qulcy4a-fN"
    )


def test_vapid_token_is_signed_by_our_key_for_the_push_service(vapid):
    public, private = vapid
    header = webpush.vapid_header(
        ENDPOINT, private_key=private, public_key=public, subject="mailto:board@example.com"
    )
    token = header.split("t=", 1)[1].split(",", 1)[0]
    assert header.endswith(f"k={public}")
    head, claims, signature = token.split(".")
    assert json.loads(webpush.b64u_decode(claims))["aud"] == "https://fcm.googleapis.com"
    raw = webpush.b64u_decode(signature)
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64u_decode(public))
    key.verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )


@pytest.mark.parametrize(
    "endpoint,ok",
    [
        (ENDPOINT, True),
        ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
        ("https://web.push.apple.com/QGx", True),
        ("https://wns2-by3p.notify.windows.com/w/?token=x", True),
        ("http://fcm.googleapis.com/fcm/send/x", False),
        ("https://fcm.googleapis.com.evil.example/x", False),
        ("https://evilfcm.googleapis.com.example/x", False),
        ("https://127.0.0.1/x", False),
        ("https://localhost/x", False),
        ("https://fcm.googleapis.com:8443/x", False),
        ("https://user:pw@fcm.googleapis.com/x", False),
        ("file:///etc/passwd", False),
        ("", False),
    ],
)
def test_only_known_push_services_can_be_reached(endpoint, ok):
    assert webpush.endpoint_allowed(endpoint) is ok


def test_post_refuses_unknown_endpoints_without_a_network_call():
    with mock.patch("urllib.request.OpenerDirector.open") as opened:
        result = webpush.post("https://10.0.0.5/internal", b"x", authorization="vapid")
    assert not result.ok and result.gone
    opened.assert_not_called()


def test_generate_vapid_keys_command_prints_a_working_pair():
    out = StringIO()
    call_command("generate_vapid_keys", stdout=out, stderr=StringIO())
    lines = dict(line.split("=", 1) for line in out.getvalue().split())
    key = webpush.private_key_from_b64u(lines["VAPID_PRIVATE_KEY"])
    assert webpush.b64u_encode(webpush._public_bytes(key.public_key())) == lines["VAPID_PUBLIC_KEY"]


# --- Subscriptions -----------------------------------------------------------------------


def test_subscribe_is_off_until_the_board_switches_push_on(client, w, vapid):
    client.force_login(w["owner"])
    assert _subscribe(client).status_code == 404
    assert not PushSubscription.objects.unscoped("t").exists()


def test_subscribe_needs_vapid_keys(client, w, settings):
    FeatureSetting.objects.create(key="push", enabled=True)
    settings.VAPID_PUBLIC_KEY = settings.VAPID_PRIVATE_KEY = ""
    client.force_login(w["owner"])
    assert _subscribe(client).status_code == 400
    page = client.get(reverse("my_profile")).content.decode()
    assert "Notifications on this device" not in page


def test_subscribe_saves_this_device_for_the_signed_in_login(client, w, push_on):
    client.force_login(w["owner"])
    response = _subscribe(client)
    assert response.status_code == 200 and response.json() == {"ok": True}
    sub = PushSubscription.objects.for_user(w["owner"]).get()
    assert sub.endpoint == ENDPOINT
    event = AuditEvent.objects.get(action="push.subscribed")
    assert event.actor == w["owner"] and ENDPOINT not in json.dumps(event.metadata)
    page = client.get(reverse("my_profile")).content.decode()
    assert "Notifications on this device" in page and "On for 1 device." in page
    assert f'data-vapid-key="{push_on[0]}"' in page


def test_subscribe_refuses_other_endpoints_and_bad_keys(client, w, push_on):
    client.force_login(w["owner"])
    assert _subscribe(client, "https://169.254.169.254/latest").status_code == 400
    bad = client.post(
        reverse("push_subscribe"),
        json.dumps({"endpoint": ENDPOINT, "keys": {"p256dh": "short", "auth": UA_AUTH}}),
        content_type="application/json",
    )
    assert bad.status_code == 400
    assert not PushSubscription.objects.unscoped("t").exists()


def test_subscribe_needs_sign_in_and_csrf(w, push_on):
    anonymous = Client(enforce_csrf_checks=True)
    response = _subscribe(anonymous)
    assert response.status_code in (302, 403)
    signed_in = Client(enforce_csrf_checks=True)
    signed_in.force_login(w["owner"])
    assert _subscribe(signed_in).status_code == 403
    assert not PushSubscription.objects.unscoped("t").exists()


def test_people_see_and_remove_only_their_own_devices(client, w, push_on):
    client.force_login(w["other"])
    _subscribe(client, ENDPOINT + "-other")
    client.force_login(w["owner"])
    _subscribe(client)
    # Board members do not see other people's devices either.
    assert PushSubscription.objects.for_user(w["board"]).count() == 0
    assert PushSubscription.objects.for_user(w["owner"]).count() == 1
    # Owner tries to remove the other person's endpoint: nothing happens.
    response = client.post(
        reverse("push_unsubscribe"),
        json.dumps({"endpoint": ENDPOINT + "-other"}),
        content_type="application/json",
    )
    assert response.json()["removed"] == 0
    assert PushSubscription.objects.for_user(w["other"]).count() == 1
    # "Turn off on all my devices" (plain form) removes only their own.
    response = client.post(reverse("push_unsubscribe"))
    assert response.status_code == 302
    assert PushSubscription.objects.for_user(w["owner"]).count() == 0
    assert PushSubscription.objects.for_user(w["other"]).count() == 1
    assert AuditEvent.objects.filter(action="push.unsubscribed").get().metadata == {"devices": 1}


def test_scoped_manager_refuses_unscoped_queries():
    from core.scoping import UnscopedQueryError

    with pytest.raises(UnscopedQueryError):
        list(PushSubscription.objects.all())


# --- Sending -----------------------------------------------------------------------------


class FakeSender:
    def __init__(self, status=201):
        self.sent = []
        self.status = status

    def send(self, sub, payload):
        self.sent.append((sub.endpoint, json.loads(payload)))
        if self.status in (404, 410):
            return webpush.PushResult(ok=False, gone=True, status=self.status)
        return webpush.PushResult(ok=self.status < 300, status=self.status)


def _thread_reply(w, django_capture_on_commit_callbacks):
    from comms import threads
    from operations import services as ops

    req = ops.submit_request(
        lot=w["lot"],
        request_type="general",
        subject="Leaking gutter at 12 Main",
        description="private detail",
        actor=w["owner"],
    )
    with django_capture_on_commit_callbacks(execute=True):
        threads.post("request", req, body="Private answer", actor=w["board"])
    return req


def test_updates_push_a_title_and_link_only(client, w, push_on, django_capture_on_commit_callbacks):
    client.force_login(w["owner"])
    _subscribe(client)
    with mock.patch("comms.tasks.send_push.defer") as defer:
        req = _thread_reply(w, django_capture_on_commit_callbacks)
    kwargs = defer.call_args.kwargs
    assert kwargs["user_ids"] == [w["owner"].pk]
    assert kwargs["path"] == reverse("request_detail", args=[req.pk])
    assert "Leaking" not in kwargs["title"] and "Private" not in kwargs["title"]
    fake = FakeSender()
    with mock.patch("comms.push.sender", return_value=fake):
        counts = push.deliver(**kwargs)
    assert counts["sent"] == 1
    endpoint, payload = fake.sent[0]
    assert endpoint == ENDPOINT and set(payload) == {"title", "url"}
    assert payload["url"].startswith("/") and not payload["title"].startswith("[")


def test_no_push_for_digest_readers_or_when_the_feature_is_off(
    client, w, push_on, django_capture_on_commit_callbacks
):
    client.force_login(w["owner"])
    _subscribe(client)
    client.post(reverse("my_profile"), {"weekly_digest": "on"})
    with mock.patch("comms.tasks.send_push.defer") as defer:
        _thread_reply(w, django_capture_on_commit_callbacks)
    defer.assert_not_called()
    client.post(reverse("my_profile"), {})
    FeatureSetting.objects.filter(key="push").update(enabled=False)
    with mock.patch("comms.tasks.send_push.defer") as defer:
        _thread_reply(w, django_capture_on_commit_callbacks)
    defer.assert_not_called()


def test_legal_notices_never_push(client, w, push_on, django_capture_on_commit_callbacks):
    from comms.notify import send_now

    client.force_login(w["owner"])
    _subscribe(client)
    with (
        mock.patch("comms.tasks.send_push.defer") as defer,
        django_capture_on_commit_callbacks(execute=True),
    ):
        send_now([w["owner"].email], subject="Notice", body="Official notice")
    defer.assert_not_called()


@pytest.mark.parametrize("status", [404, 410])
def test_gone_devices_are_removed(client, w, push_on, status):
    client.force_login(w["owner"])
    _subscribe(client)
    with mock.patch("comms.push.sender", return_value=FakeSender(status)):
        counts = push.deliver([w["owner"].pk], title="Update", path="/my-items/")
    assert counts == {"sent": 0, "failed": 0, "removed": 1}
    assert not PushSubscription.objects.unscoped("t").exists()


def test_other_failures_keep_the_device(client, w, push_on):
    client.force_login(w["owner"])
    _subscribe(client)
    with mock.patch("comms.push.sender", return_value=FakeSender(500)):
        counts = push.deliver([w["owner"].pk], title="Update", path="/my-items/")
    assert counts["failed"] == 1
    assert PushSubscription.objects.unscoped("t").count() == 1


@pytest.mark.parametrize("path", ["https://evil.example/x", "//evil.example/x", "javascript:x"])
def test_links_outside_the_portal_are_never_pushed(client, w, push_on, path):
    client.force_login(w["owner"])
    _subscribe(client)
    fake = FakeSender()
    with mock.patch("comms.push.sender", return_value=fake):
        push.deliver([w["owner"].pk], title="Update", path=path)
    with mock.patch("comms.tasks.send_push.defer") as defer:
        push.queue_for_users([w["owner"].pk], title="Update", link=path)
    assert fake.sent == [] and not defer.called


def test_web_push_sender_encrypts_and_signs(client, w, push_on):
    client.force_login(w["owner"])
    _subscribe(client)
    with mock.patch("comms.webpush.post", return_value=webpush.PushResult(ok=True)) as post:
        counts = push.deliver([w["owner"].pk], title="Update", path="/my-items/")
    assert counts["sent"] == 1
    endpoint, body = post.call_args.args
    assert endpoint == ENDPOINT
    assert b"my-items" not in body  # encrypted for the device
    assert post.call_args.kwargs["authorization"].startswith("vapid t=")


def test_a_broken_server_key_never_deletes_subscriptions(client, w, push_on, settings):
    """Review R2-005."""
    client.force_login(w["owner"])
    _subscribe(client)
    settings.VAPID_PRIVATE_KEY = "not-a-key"
    counts = push.deliver([w["owner"].pk], title="Hello", path="/")
    assert counts["removed"] == 0
    assert PushSubscription.objects.unscoped("t").filter(user=w["owner"]).count() == 1
