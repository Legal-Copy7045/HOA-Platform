"""SMTP2Go as the email relay (0.43.0): sending over its HTTP API, the key
never logged, the pull of delivery reports with a cursor and idempotency,
bounce marking, forged reports refused (BUG-002 / EXP-001), the ARC decision
email's delivery status for the board only, System status and the set-up
wizard. A fake SMTP2Go replaces the transport: nothing reaches the network."""

from __future__ import annotations

import datetime
import json
import logging

import pytest
from django.core import mail
from django.core.mail import EmailMessage, send_mail
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from comms import bounces, delivery, smtp2go
from comms.models import EmailBounce, EmailDelivery, EmailProviderState, OutgoingEmail
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import email as portal_email
from core import features
from core.logging import JsonFormatter
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db

KEY = "api-TESTONLY0123456789ABCDEFnotreal"  # nosec B105 - test value
OWNER_EMAIL = "decision-owner@example.com"


class FakeSmtp2go:
    """Answers like SMTP2Go's v3 API. ``events`` is the activity log."""

    def __init__(self):
        self.calls: list[dict] = []
        self.events: list[dict] = []
        self.status = {}
        self.next_id = 0

    def __call__(self, url, data, headers, timeout):
        body = json.loads(data)
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        path = url.split("/v3/", 1)[1]
        if path in self.status:
            return self.status[path], json.dumps({"data": {"error": "Denied"}}).encode()
        if path == "email/send":
            self.next_id += 1
            email_id = f"1abcDE-{self.next_id:06d}-zz"
            return 200, json.dumps(
                {"data": {"succeeded": len(body["to"]), "failed": 0, "email_id": email_id}}
            ).encode()
        if path == "activity/search":
            token = body.get("continue_token", "")
            if not token and len(self.events) > 2:
                return 200, json.dumps(
                    {"data": {"events": self.events[:2], "continue_token": "page2"}}  # nosec B105
                ).encode()
            rest = self.events[2:] if token else self.events
            return 200, json.dumps({"data": {"events": rest}}).encode()
        if path == "stats/email_summary":
            return 200, json.dumps({"data": {"cycle_used": 3}}).encode()
        return 404, b"{}"

    def sent(self):
        return [c for c in self.calls if c["url"].endswith("/email/send")]


@pytest.fixture
def fake(settings, monkeypatch):
    settings.EMAIL_BACKEND = "core.email.RedirectingBackend"
    settings.EMAIL_PROVIDER = "smtp2go"
    settings.SMTP2GO_API_KEY = KEY
    settings.SMTP2GO_REGION = "global"
    settings.EMAIL_REDIRECT_ALL_TO = ""
    settings.BOUNCE_RETURN_PATH = "bounces@example.org"  # ignored with SMTP2Go
    fake = FakeSmtp2go()
    monkeypatch.setattr(smtp2go, "transport", fake)
    mail.outbox = []
    return fake


def _event(email_id, event, recipient, minutes=1, **extra):
    at = timezone.now() - datetime.timedelta(minutes=minutes)
    return {
        "email_id": email_id,
        "event": event,
        "recipient": recipient,
        "date": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        **extra,
    }


# --- Sending -------------------------------------------------------------------------------


def test_send_goes_to_smtp2go_with_the_key_header_and_is_recorded(fake):
    with portal_email.mail_kind("notice"), portal_email.mail_about("meeting_notice", 7):
        assert send_mail("Hello", "Body text", "HOA <portal@example.org>", [OWNER_EMAIL]) == 1
    (call,) = fake.sent()
    assert call["url"] == "https://api.smtp2go.com/v3/email/send"
    assert call["headers"]["X-Smtp2go-Api-Key"] == KEY
    assert call["timeout"] == 15
    body = call["body"]
    assert body["to"] == [OWNER_EMAIL] and body["subject"] == "Hello"
    assert body["text_body"] == "Body text" and body["sender"] == "HOA <portal@example.org>"
    headers = {h["header"]: h["value"] for h in body["custom_headers"]}
    assert bounces.read_ref(headers["X-Portal-Ref"]) is not None
    assert "X-Portal-Kind" not in headers and "From" not in headers
    with system_access("test"):
        row = OutgoingEmail.objects.get()
    assert row.provider == "smtp2go" and row.provider_message_id == "1abcDE-000001-zz"
    assert row.about == "meeting_notice:7" and row.mail_kind == "notice"
    assert row.address_hash == bounces.address_hash(OWNER_EMAIL)
    assert row.address_hash in row.address_hashes.split()
    assert EmailProviderState.get().last_send_at is not None


def test_html_alternative_attachments_and_region(fake, settings):
    from django.core.mail import EmailMultiAlternatives

    settings.SMTP2GO_REGION = "eu"
    msg = EmailMultiAlternatives("S", "plain", None, [OWNER_EMAIL], reply_to=["board@example.org"])
    msg.attach_alternative("<p>html</p>", "text/html")
    msg.attach("letter.pdf", b"%PDF-1.4 test", "application/pdf")
    msg.send()
    body = fake.sent()[0]["body"]
    assert fake.sent()[0]["url"].startswith("https://eu-api.smtp2go.com/v3/")
    assert body["html_body"] == "<p>html</p>" and body["text_body"] == "plain"
    assert body["attachments"][0]["filename"] == "letter.pdf"
    assert body["attachments"][0]["mimetype"] == "application/pdf"
    assert {"header": "Reply-To", "value": "board@example.org"} in body["custom_headers"]


def test_no_verp_sender_with_smtp2go(fake):
    send_mail("S", "B", "portal@example.org", [OWNER_EMAIL])
    assert fake.sent()[0]["body"]["sender"] == "portal@example.org"


def test_plain_smtp_still_works_and_records_the_send(settings):
    settings.EMAIL_BACKEND = "core.email.RedirectingBackend"
    settings.EMAIL_PROVIDER = "smtp"
    settings.EMAIL_TRANSPORT_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    send_mail("S", "B", None, [OWNER_EMAIL])
    assert len(mail.outbox) == 1
    with system_access("test"):
        row = OutgoingEmail.objects.get()
    assert row.provider == "other" and row.provider_message_id == ""


@pytest.mark.parametrize(
    ("status", "code"), [(401, "auth"), (403, "auth"), (429, "rate_limited"), (503, "server")]
)
def test_a_refused_send_is_a_friendly_error_with_plain_words(fake, status, code):
    from core import health

    fake.status["email/send"] = status
    with pytest.raises(portal_email.EmailUnavailable):
        send_mail("S", "B", None, [OWNER_EMAIL])
    assert EmailProviderState.get().last_send_failure == code
    ok, detail = health._email()
    assert not ok and smtp2go.FAILURE_WORDS[code] in detail
    with system_access("test"):
        assert not OutgoingEmail.objects.exists()


def test_the_key_and_addresses_are_never_logged(fake, caplog):
    caplog.set_level(logging.DEBUG)
    send_mail("S", "B", None, [OWNER_EMAIL])
    fake.status["email/send"] = 401
    with pytest.raises(portal_email.EmailUnavailable):
        send_mail("S", "B", None, [OWNER_EMAIL])
    fake.status.clear()
    fake.events = [_event("1abcDE-000001-zz", "hard_bounce", OWNER_EMAIL)]
    delivery.pull()
    formatter = JsonFormatter()
    text = "\n".join(formatter.format(r) for r in caplog.records)
    assert caplog.records  # something was logged
    assert KEY not in text and OWNER_EMAIL not in text
    assert KEY not in repr(smtp2go.config())


def test_only_smtp2go_hosts_and_no_key_means_not_configured(fake, settings):
    settings.SMTP2GO_REGION = "elsewhere"
    assert not smtp2go.config().configured
    settings.SMTP2GO_REGION = "global"
    settings.SMTP2GO_API_KEY = "not-a-key"
    assert smtp2go.config().code == "bad_key"
    with pytest.raises(portal_email.EmailUnavailable):
        send_mail("S", "B", None, [OWNER_EMAIL])
    assert fake.calls == []
    cfg = smtp2go.Config(True, "api.smtp2go.com", KEY)
    assert not smtp2go._allowed("https://evil.example.org/v3/email/send", cfg)
    assert not smtp2go._allowed("http://api.smtp2go.com/v3/email/send", cfg)
    assert smtp2go._allowed("https://api.smtp2go.com/v3/email/send", cfg)


def test_check_smtp2go_command(fake, capsys):
    call_command("check_smtp2go")
    assert "accepted the key" in capsys.readouterr().out
    assert not fake.sent()  # nothing was emailed
    fake.status["stats/email_summary"] = 401
    with pytest.raises(CommandError, match="refused the API key"):
        call_command("check_smtp2go")


# --- Reading delivery reports ---------------------------------------------------------


@pytest.fixture
def owner(fake):
    features.set_feature(bounces.FEATURE, True, actor=SYSTEM)
    user = make_member(lots=[LotFactory()], email=OWNER_EMAIL)
    return user


def _sent_id(fake, kind="notice"):
    with portal_email.mail_kind(kind):
        send_mail("S", "B", None, [OWNER_EMAIL])
    return f"1abcDE-{fake.next_id:06d}-zz"


@pytest.mark.parametrize(
    ("raw", "kind", "status"),
    [
        ({"event": "delivered"}, "delivered", ""),
        ({"event": "hard_bounce", "smtp_response": "550 5.1.1 no such user"}, "hard", "5.1.1"),
        ({"event": "Hard Bounced"}, "hard", "5.0.0"),
        ({"event": "soft-bounce", "smtp_response": "452 mailbox full"}, "soft", "4.0.0"),
        ({"event": "bounce", "bounce": "soft"}, "soft", "4.0.0"),
        ({"event": "spam"}, "spam", ""),
        ({"event": "Spam Complaint"}, "spam", ""),
        ({"event": "unsubscribed"}, "unsub", ""),
        ({"event": "opened"}, None, ""),
        ({"event": "processed"}, None, ""),
    ],
)
def test_event_names_are_normalized(raw, kind, status):
    event = smtp2go.normalize_event({"email_id": "abc-1", "recipient": "x@example.com", **raw})
    if kind is None:
        assert event is None
    else:
        assert event.kind == kind and event.status == status
        assert "x@example.com" not in repr(event)


def test_pull_uses_a_cursor_pages_and_is_idempotent(fake, owner):
    email_id = _sent_id(fake)
    fake.events = [
        _event(email_id, "delivered", OWNER_EMAIL, minutes=30),
        _event(email_id, "opened", OWNER_EMAIL, minutes=29),
        _event(email_id, "hard_bounce", OWNER_EMAIL, minutes=5, smtp_response="550 5.1.1"),
    ]
    now = timezone.now()
    result = delivery.pull(now=now)
    assert result["state"] == "ok" and result["pages"] == 2 and result["stored"] == 2
    searches = [c["body"] for c in fake.calls if c["url"].endswith("/activity/search")]
    assert searches[0]["start_date"] == (now - datetime.timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert searches[1]["continue_token"] == "page2"
    state = EmailProviderState.get()
    assert state.cursor_at is not None and state.last_pull_success_at == now
    with system_access("test"):
        counts = (EmailDelivery.objects.count(), EmailBounce.objects.count())
    assert counts == (2, 1)
    # The same reports again (the overlap re-reads them): nothing changes.
    later = now + datetime.timedelta(minutes=5)
    again = delivery.pull(now=later)
    assert again["stored"] == 0 and again["bounces"] == 0
    with system_access("test"):
        assert (EmailDelivery.objects.count(), EmailBounce.objects.count()) == counts
    last = [c["body"] for c in fake.calls if c["url"].endswith("/activity/search")][-1]
    assert last["start_date"] == (state.cursor_at - datetime.timedelta(minutes=15)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_a_hard_bounce_marks_the_address_and_asks_for_a_paper_copy(fake, owner):
    email_id = _sent_id(fake, kind="notice")
    fake.events = [_event(email_id, "hard_bounce", OWNER_EMAIL, smtp_response="550 5.1.1")]
    delivery.pull()
    person = owner.person
    rows = bounces.bouncing(person)
    assert [r["address"] for r in rows] == [OWNER_EMAIL]
    assert bounces.paper_copies_needed()
    with system_access("test"):
        bounce = EmailBounce.objects.get()
    assert bounce.person_id == person.pk and bounce.mail_kind == "notice"
    assert bounce.status == "5.1.1"


def test_spam_complaint_and_unsubscribe_count_like_a_hard_bounce(fake, owner):
    email_id = _sent_id(fake, kind="statement")
    fake.events = [_event(email_id, "spam", OWNER_EMAIL)]
    delivery.pull()
    (row,) = bounces.bouncing(owner.person)
    assert "spam" in row["why"].lower()


def test_soft_bounce_does_not_mark_the_address(fake, owner):
    email_id = _sent_id(fake)
    fake.events = [_event(email_id, "soft_bounce", OWNER_EMAIL)]
    delivery.pull()
    assert bounces.bouncing(owner.person) == []


def test_reports_for_messages_the_portal_did_not_send_are_ignored(fake, owner):
    """EXP-001: only events for the portal's own message ids, and only for a
    recipient of that message, count."""
    email_id = _sent_id(fake)
    victim = make_member(lots=[LotFactory()], email="victim@example.com")
    fake.events = [
        _event("9zzzzz-999999-zz", "hard_bounce", OWNER_EMAIL),  # not ours
        _event(email_id, "hard_bounce", "victim@example.com"),  # not its recipient
    ]
    result = delivery.pull()
    assert result["skipped"] == 2 and result["stored"] == 0
    assert bounces.bouncing(owner.person) == [] and bounces.bouncing(victim.person) == []


def test_a_failed_pull_keeps_the_cursor_and_shows_in_health(fake, owner):
    fake.status["activity/search"] = 401
    assert delivery.pull()["state"] == "failed"
    state = EmailProviderState.get()
    assert state.cursor_at is None and state.last_pull_failure == "auth"
    ok, detail = delivery.health()
    assert not ok and "not being read" in detail


def test_bounce_mailbox_is_not_needed_with_smtp2go(fake):
    features.set_feature(bounces.FEATURE, True, actor=SYSTEM)
    assert bounces.health() is None
    assert bounces.setup_status()["state"] == "not_needed"


def test_the_pull_task_runs_only_with_smtp2go(fake, settings):
    from comms.tasks import poll_email_activity

    assert poll_email_activity(0)["state"] == "ok"
    settings.EMAIL_PROVIDER = "smtp"
    assert poll_email_activity(0) == {"state": "off"}


# --- Forged bounce reports to the mailbox (BUG-002 / EXP-001) ----------------------------------


def _dsn(token, recipient, ident="forged-1"):
    return (
        "From: Mail Delivery System <mailer-daemon@mx.example.net>\r\n"
        f"To: bounces+{token}@example.org\r\n"
        "Subject: Undelivered Mail Returned to Sender\r\n"
        f"Message-ID: <{ident}@mx.example.net>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n'
        "\r\n--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mx.example.net\r\n\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\nAction: failed\r\nStatus: 5.1.1\r\n"
        "\r\n--B--\r\n"
    ).encode()


def test_a_forged_report_cannot_mark_another_address():
    token = bounces.make_ref("notice", ["mallory@example.com"])
    assert bounces.parse(_dsn(token, "victim@example.com")) == []
    own = bounces.parse(_dsn(token, "mallory@example.com"))
    assert [p.address_hash for p in own] == [bounces.address_hash("mallory@example.com")]


def test_a_several_recipient_token_counts_only_for_its_recorded_recipients(settings):
    settings.EMAIL_BACKEND = "core.email.RedirectingBackend"
    settings.EMAIL_PROVIDER = "smtp"
    settings.EMAIL_TRANSPORT_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    victim = make_member(lots=[LotFactory()], email="victim@example.com")
    EmailMessage("S", "B", None, ["a@example.com", "b@example.com"]).send()
    token = mail.outbox[0].extra_headers["X-Portal-Ref"]
    forged = bounces.parse(_dsn(token, "victim@example.com", ident="f-2"))
    assert bounces.record(forged) == (0, 0)  # victim was not a recipient
    assert bounces.bouncing(victim.person) == []
    real = bounces.parse(_dsn(token, "b@example.com", ident="r-1"))
    assert bounces.record(real) == (1, 1)
    # A several-recipient token with no send record counts for nobody.
    stray = bounces.make_ref("notice", ["x@example.com", "y@example.com"])
    assert bounces.record(bounces.parse(_dsn(stray, "x@example.com", ident="s-1"))) == (0, 0)


# --- ARC decision email: delivery status for the board only ----------------------------------


@pytest.fixture
def arc(fake):
    from arc import services
    from community import services as community

    lot, other_lot = LotFactory(), LotFactory()
    board = make_member(board=True)
    community.set_rule(key="arc.review_days", value="45", citation="Decl. §9.3", actor=board)
    owner = make_member(lots=[lot], email=OWNER_EMAIL)
    manager = make_member(roles=[Role.MANAGER])
    req = services.create_request(lot=lot, title="Fence", description="4 ft", actor=owner)
    services.ARC.apply(req, "submit", actor=owner)
    req = services.ARC.apply(req, "accept", actor=manager)
    return {
        "req": req,
        "board": board,
        "owner": owner,
        "stranger": make_member(lots=[other_lot]),
        "reviewer": make_member(roles=[Role.ARC_REVIEWER]),
    }


def _decide(arc, django_capture_on_commit_callbacks):
    from arc import services
    from arc.models import ArcState

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        decision = services.decide(
            arc["req"], outcome=ArcState.APPROVED, citation="Decl. §9.3", actor=arc["reviewer"]
        )
    assert callbacks  # the email is queued after commit
    return decision


def test_a_decision_emails_the_applicant_once(arc, fake, django_capture_on_commit_callbacks):
    from comms.tasks import send_arc_decision_email

    decision = _decide(arc, django_capture_on_commit_callbacks)
    assert send_arc_decision_email(decision.pk) == 1
    (call,) = fake.sent()
    assert call["body"]["to"] == [OWNER_EMAIL]
    assert "approved" in call["body"]["text_body"].lower()
    assert reverse("arc_detail", args=[arc["req"].pk]) in call["body"]["text_body"]
    # A retry does not email the same address again.
    assert send_arc_decision_email(decision.pk) == 0
    assert len(fake.sent()) == 1
    with system_access("test"):
        row = OutgoingEmail.objects.get()
    assert row.about == f"arc_decision:{decision.pk}" and row.mail_kind == "notice"


def test_a_failed_send_raises_for_the_worker_to_retry(
    arc, fake, django_capture_on_commit_callbacks
):
    from comms.tasks import EMAIL_RETRY, send_arc_decision_email

    decision = _decide(arc, django_capture_on_commit_callbacks)
    fake.status["email/send"] = 503
    with pytest.raises(portal_email.EmailUnavailable):
        send_arc_decision_email(decision.pk)
    assert EMAIL_RETRY.max_attempts == 5 and EMAIL_RETRY.exponential_wait


def _page(client, user, arc):
    client.force_login(user)
    return client.get(reverse("arc_detail", args=[arc["req"].pk]))


def test_the_board_sees_delivered_bounced_or_pending(
    arc, fake, client, django_capture_on_commit_callbacks
):
    from comms.tasks import send_arc_decision_email

    decision = _decide(arc, django_capture_on_commit_callbacks)
    page = _page(client, arc["board"], arc).content.decode()
    assert 'data-testid="decision-delivery"' in page and "Pending" in page
    send_arc_decision_email(decision.pk)
    email_id = f"1abcDE-{fake.next_id:06d}-zz"
    fake.events = [_event(email_id, "delivered", OWNER_EMAIL)]
    delivery.pull()
    page = _page(client, arc["board"], arc).content.decode()
    assert "Delivered" in page and OWNER_EMAIL not in page.split("decision-delivery", 1)[1][:400]
    fake.events.append(_event(email_id, "hard_bounce", OWNER_EMAIL, minutes=0))
    delivery.pull(now=timezone.now() + datetime.timedelta(minutes=5))
    page = _page(client, arc["board"], arc).content.decode()
    assert "Bounced" in page.split("decision-delivery", 1)[1][:600]


def test_delivery_status_is_only_for_those_who_see_the_request_as_board(
    arc, fake, client, django_capture_on_commit_callbacks
):
    """Cross-owner: another owner gets 404; the applicant sees the request
    but not the board's delivery line; the board and reviewers see it."""
    _decide(arc, django_capture_on_commit_callbacks)
    assert _page(client, arc["stranger"], arc).status_code == 404
    applicant = _page(client, arc["owner"], arc)
    assert applicant.status_code == 200
    assert 'data-testid="decision-delivery"' not in applicant.content.decode()
    for staff in (arc["board"], arc["reviewer"]):
        response = _page(client, staff, arc)
        assert 'data-testid="decision-delivery"' in response.content.decode()
    from arc import notify

    assert notify.delivery_for(arc["req"], arc["stranger"]) == {}
    assert notify.delivery_for(arc["req"], arc["owner"]) == {}


# --- System status and the set-up wizard ------------------------------------------------------


def test_system_status_shows_provider_last_send_last_pull_and_bounce_count(fake, owner, client):
    email_id = _sent_id(fake)
    fake.events = [_event(email_id, "hard_bounce", OWNER_EMAIL)]
    delivery.pull()
    admin = make_member(roles=[Role.SYSADMIN])
    client.force_login(admin)
    page = client.get(reverse("system_status")).content.decode()
    card = page.split('id="email"', 1)[1].split("</section>", 1)[0]
    assert "SMTP2Go (HTTP API)" in card
    assert "Last successful send" in card and "None recorded yet" not in card
    assert "Last read of delivery reports" in card and "Not yet" not in card
    assert "Bounces in the last 7 days</dt><dd>1" in card
    assert OWNER_EMAIL not in page and KEY not in page
    assert "Email delivery reports" in page


def test_setup_wizard_says_configured_or_not_and_never_the_key(fake, client, settings):
    board = make_member(board=True)
    client.force_login(board)
    page = client.get(reverse("setup_step", args=["server"])).content.decode()
    line = page.split('data-testid="setup-smtp2go"', 1)[1].split("</li>", 1)[0]
    assert "Configured" in line and "infra/poc/set-smtp2go.sh" in line and KEY not in page
    settings.EMAIL_PROVIDER = "smtp"
    page = client.get(reverse("setup_step", args=["server"])).content.decode()
    line = page.split('data-testid="setup-smtp2go"', 1)[1].split("</li>", 1)[0]
    assert "Not configured" in line
