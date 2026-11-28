"""Bounced email (D74): tokens on outgoing mail, safe parsing of bounce
reports (synthetic fixtures), the IMAP client against a fake server (no
network), and the effects: bouncing addresses and paper copies of bounced
official notices."""

import datetime
import imaplib
import io
import ssl

import pytest
from django.core import mail
from django.core.mail import EmailMessage, send_mail
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from comms import bounces
from comms.models import BounceFollowUp, BouncePollState, EmailBounce
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import email as portal_email
from core import features
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db

OWNER_EMAIL = "notice-owner@example.com"


@pytest.fixture
def mailbox(settings, monkeypatch):
    # Live polling resolves a public host once and connects to that address
    # (review EXP-002, 0.46.0): a public test address, no real DNS lookup.
    from core import credential_checks

    monkeypatch.setattr(credential_checks, "resolve", lambda host: ["93.184.215.14"])
    settings.BOUNCE_IMAP_HOST = "imap.example.org"
    settings.BOUNCE_IMAP_PORT = 993
    settings.BOUNCE_IMAP_USER = "bounces@example.org"
    settings.BOUNCE_IMAP_PASSWORD = "test-only-not-a-secret"  # nosec B105 (test only)
    settings.BOUNCE_IMAP_FOLDER = "INBOX"
    features.set_feature(bounces.FEATURE, True, actor=SYSTEM)
    return settings


@pytest.fixture
def tagging(settings):
    """Send through the portal's own backend (tests normally skip it)."""
    settings.EMAIL_BACKEND = "core.email.RedirectingBackend"
    settings.EMAIL_TRANSPORT_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.EMAIL_REDIRECT_ALL_TO = ""
    mail.outbox = []
    return settings


# --- Synthetic bounce reports --------------------------------------------------------------


def dsn(ref, recipient, *, status="5.1.1", action="failed", extra_parts="", ident="dsn-1"):
    """An RFC 3464 delivery status notification, as most servers send it."""
    return (
        "From: Mail Delivery System <mailer-daemon@mx.example.net>\r\n"
        "To: bounces@example.org\r\n"
        "Subject: Undelivered Mail Returned to Sender\r\n"
        f"Message-ID: <{ident}@mx.example.net>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n'
        "\r\n--B\r\n"
        "Content-Type: text/plain; charset=us-ascii\r\n\r\n"
        "This is the mail system. Your message could not be delivered.\r\n"
        "\r\n--B\r\n"
        "Content-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mx.example.net\r\n\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\n"
        f"Original-Recipient: rfc822;{recipient}\r\n"
        f"Action: {action}\r\n"
        f"Status: {status}\r\n"
        "Diagnostic-Code: smtp; 550 5.1.1 user unknown (should never be stored)\r\n"
        "\r\n--B\r\n"
        "Content-Type: text/rfc822-headers\r\n\r\n"
        "From: portal@example.com\r\n"
        f"To: {recipient}\r\n"
        "Subject: [Portal] Meeting notice\r\n"
        f"X-Portal-Ref: {ref}\r\n"
        f"{extra_parts}"
        "\r\n--B--\r\n"
    ).encode()


def exim(ref, recipient):
    """Exim style: no report part, X-Failed-Recipients, the message quoted."""
    return (
        "From: Mail Delivery System <Mailer-Daemon@mx.example.net>\r\n"
        "To: portal@example.com\r\n"
        "Subject: Mail delivery failed: returning message to sender\r\n"
        f"X-Failed-Recipients: {recipient}\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "This message was created automatically by mail delivery software.\r\n\r\n"
        "A message that you sent could not be delivered to one or more of its\r\n"
        f"recipients. This is a permanent error.\r\n\r\n  {recipient}\r\n"
        "    host mx.example.net: 550 5.1.1 No such user\r\n\r\n"
        "------ This is a copy of the message, including all the headers. ------\r\n"
        f"Message-ID: <1.2.3.{ref}@portal.example.com>\r\n"
        "Subject: Your statement\r\n"
    ).encode()


def qmail(ref):
    """qmail style: plain text, no status code at all."""
    return (
        "From: MAILER-DAEMON@mx.example.net\r\n"
        "To: portal@example.com\r\n"
        "Subject: failure notice\r\n\r\n"
        "Hi. This is the qmail-send program at mx.example.net.\r\n"
        "I'm afraid I wasn't able to deliver your message.\r\n\r\n"
        "--- Below this line is a copy of the message.\r\n"
        f"X-Portal-Ref: {ref}\r\n"
    ).encode()


def out_of_office(ref):
    return (
        "From: someone@example.net\r\n"
        "To: portal@example.com\r\n"
        "Subject: Out of office: Meeting notice\r\n"
        f"References: <1.2.3.{ref}@portal.example.com>\r\n\r\n"
        "I am away until Monday.\r\n"
    ).encode()


def _ref(kind="notice", to=OWNER_EMAIL, **kw):
    return bounces.make_ref(kind, [to], **kw)


# --- Tokens --------------------------------------------------------------------------------


def test_token_round_trip_and_tampering():
    ref = _ref()
    found = bounces.read_ref(f"junk {ref} junk")
    assert found.kind == "notice"
    assert found.addr16 == bounces.address_hash(OWNER_EMAIL)[:16]
    assert OWNER_EMAIL not in ref and "notice-owner" not in ref
    forged = ref[:-1] + ("0" if ref[-1] != "0" else "1")
    assert bounces.read_ref(forged) is None
    # Mail servers that upper-case still match.
    assert bounces.read_ref(ref.upper()) is not None


def test_old_tokens_no_longer_count():
    old = _ref(now=timezone.now() - datetime.timedelta(days=bounces.TOKEN_DAYS + 1))
    assert bounces.read_ref(old) is None


def test_several_recipients_carry_no_address():
    ref = bounces.make_ref("other", ["a@example.com", "b@example.com"])
    assert bounces.read_ref(ref).addr16 == bounces.NO_ADDRESS


def test_address_hash_is_keyed_and_case_blind(settings):
    assert bounces.address_hash(" Notice-Owner@Example.com ") == bounces.address_hash(OWNER_EMAIL)
    before = bounces.address_hash(OWNER_EMAIL)
    settings.AUDIT_HASH_KEY = "another-test-only-key-not-a-secret"
    assert bounces.address_hash(OWNER_EMAIL) != before


# --- Tagging outgoing mail -----------------------------------------------------------------


def test_every_email_gets_a_token_and_the_kind_from_the_context(tagging):
    send_mail("Hello", "Body", None, ["x@example.com"])
    with portal_email.mail_kind(portal_email.NOTICE):
        send_mail("Notice", "Body", None, ["y@example.com"])
    plain, notice = mail.outbox
    assert bounces.read_ref(plain.extra_headers["X-Portal-Ref"]).kind == "other"
    found = bounces.read_ref(notice.extra_headers["X-Portal-Ref"])
    assert found.kind == "notice"
    assert found.addr16 == bounces.address_hash("y@example.com")[:16]
    assert notice.extra_headers["X-Portal-Ref"] in notice.extra_headers["Message-ID"]
    assert notice.from_email == tagging.DEFAULT_FROM_EMAIL  # no VERP unless asked


def test_kind_header_is_used_and_removed(tagging):
    message = EmailMessage(
        "S", "B", None, ["z@example.com"], headers={portal_email.KIND_HEADER: "statement"}
    )
    message.send()
    sent = mail.outbox[0]
    assert portal_email.KIND_HEADER not in sent.extra_headers
    assert bounces.read_ref(sent.extra_headers["X-Portal-Ref"]).kind == "statement"
    assert portal_email.KIND_HEADER in message.extra_headers  # the original is untouched


def test_verp_envelope_sender_keeps_the_from_header(tagging):
    tagging.BOUNCE_RETURN_PATH = "bounces@example.org"
    send_mail("Hi", "B", "portal@example.com", ["z@example.com"])
    sent = mail.outbox[0]
    ref = sent.extra_headers["X-Portal-Ref"]
    assert sent.from_email == f"bounces+{ref}@example.org"
    assert sent.extra_headers["From"] == "portal@example.com"
    assert sent.message()["From"] == "portal@example.com"
    assert bounces.read_ref(sent.from_email) is not None


def test_a_bad_return_path_is_ignored(settings):
    settings.BOUNCE_RETURN_PATH = "bounces+x@example.org"
    assert bounces.return_path("ref") == ""
    settings.BOUNCE_RETURN_PATH = "not-an-address"
    assert bounces.return_path("ref") == ""


def test_tagging_never_stops_an_email(tagging, monkeypatch):
    monkeypatch.setattr(bounces, "make_ref", lambda *a, **k: 1 / 0)
    send_mail("Hi", "B", None, ["z@example.com"])
    assert len(mail.outbox) == 1


# --- Parsing -------------------------------------------------------------------------------


def test_rfc3464_hard_bounce():
    (p,) = bounces.parse(dsn(_ref(), OWNER_EMAIL))
    assert (p.kind, p.status, p.mail_kind) == ("hard", "5.1.1", "notice")
    assert p.address_hash == bounces.address_hash(OWNER_EMAIL)
    assert "user unknown" not in repr(p) and OWNER_EMAIL not in repr(p)


def test_delayed_is_soft_and_delivered_is_ignored():
    (p,) = bounces.parse(dsn(_ref(), OWNER_EMAIL, status="4.2.2", action="delayed"))
    assert p.kind == "soft" and p.status == "4.2.2"
    assert bounces.parse(dsn(_ref(), OWNER_EMAIL, status="2.0.0", action="delivered")) == []


def test_exim_and_qmail_formats():
    (p,) = bounces.parse(exim(_ref("statement"), OWNER_EMAIL))
    assert (p.kind, p.status, p.mail_kind) == ("hard", "5.1.1", "statement")
    (q,) = bounces.parse(qmail(_ref()))
    assert q.kind == "soft"  # a failure we cannot classify never marks an address
    assert q.addr16 == bounces.address_hash(OWNER_EMAIL)[:16] and q.address_hash == ""


def test_not_a_bounce_forged_or_unreadable():
    assert bounces.parse(out_of_office(_ref())) == []
    assert bounces.parse(dsn("o123456.0000000000000000.0000000000000000", OWNER_EMAIL)) == []
    assert bounces.parse(b"\x00\xff garbage") == []
    assert bounces.parse(dsn("", OWNER_EMAIL)) == []


def test_tokens_inside_attachments_are_not_read():
    ref = _ref()
    raw = (
        "From: mailer-daemon@mx.example.net\r\nSubject: Undeliverable\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\n'
        "Content-Type: text/plain\r\n\r\n550 5.1.1 rejected\r\n--B\r\n"
        "Content-Type: application/octet-stream\r\n"
        'Content-Disposition: attachment; filename="x.bin"\r\n\r\n'
        f"X-Portal-Ref: {ref}\r\n--B--\r\n"
    ).encode()
    assert bounces.parse(raw) == []


def test_huge_input_is_cut_before_parsing():
    raw = dsn(_ref(), OWNER_EMAIL) + b"x" * (bounces.MAX_BYTES * 2)
    assert len(bounces.parse(raw)) == 1


# --- The IMAP client, against a fake server ------------------------------------------------


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL. Records how it was opened."""

    instances: list = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port, self.ssl_context, self.timeout = host, port, ssl_context, timeout
        self.messages = dict(FakeIMAP.messages)
        self.seen = set()
        self.logged_out = False
        FakeIMAP.instances.append(self)
        if FakeIMAP.fail == "connect":
            raise OSError("refused")
        if FakeIMAP.fail == "tls":
            raise ssl.SSLCertVerificationError("bad certificate")

    messages: dict = {}
    fail = ""

    def login(self, user, password):
        if FakeIMAP.fail == "auth":
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.user = user
        return "OK", [b"Logged in"]

    def select(self, folder):
        self.folder = folder
        return ("NO", [b"No such folder"]) if FakeIMAP.fail == "folder" else ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "SEARCH":
            unseen = [u for u in self.messages if u not in FakeIMAP.seen_all]
            return "OK", [b" ".join(unseen)]
        if command == "FETCH":
            uid, what = args
            raw = self.messages[uid]
            if what == "(RFC822.SIZE)":
                return "OK", [uid + b" (UID " + uid + b" RFC822.SIZE %d)" % len(raw)]
            assert "BODY.PEEK[]<0." in what  # never marks seen, never more than the cap
            return "OK", [(uid + b" (BODY[]<0> {%d}" % len(raw), raw), b")"]
        if command == "STORE":
            FakeIMAP.seen_all.add(args[0])
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]

    seen_all: set = set()


@pytest.fixture
def fake_imap():
    FakeIMAP.instances, FakeIMAP.messages, FakeIMAP.fail, FakeIMAP.seen_all = [], {}, "", set()
    return FakeIMAP


def test_poll_reads_unseen_marks_them_seen_and_records_once(mailbox, fake_imap):
    owner = make_member(lots=[LotFactory()], email=OWNER_EMAIL)
    fake_imap.messages = {
        b"1": dsn(_ref(), OWNER_EMAIL),
        b"2": out_of_office(_ref()),
        b"3": b"x" * (bounces.MAX_BYTES + 1),  # skipped: too big to be a report
    }
    result = bounces.poll(factory=fake_imap)
    assert result == {
        "state": "ok",
        "read": 3,
        "recorded": 1,
        "hard": 1,
        "unmatched": 1,
        "skipped": 1,
    }
    box = fake_imap.instances[0]
    assert (box.host, box.port, box.user, box.folder) == (
        "imap.example.org",
        993,
        "bounces@example.org",
        '"INBOX"',
    )
    assert box.ssl_context.verify_mode == ssl.CERT_REQUIRED and box.ssl_context.check_hostname
    assert box.ssl_context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert box.timeout == bounces.TIMEOUT_SECONDS and box.logged_out
    assert fake_imap.seen_all == {b"1", b"2", b"3"}
    with system_access("t"):
        (row,) = EmailBounce.objects.all()
    assert row.person_id == owner.person.pk and row.kind == "hard" and row.mail_kind == "notice"
    assert row.address_hash == bounces.address_hash(OWNER_EMAIL)
    event = AuditEvent.objects.get(action="comms.bounces_read")
    assert event.metadata == {"read": 3, "recorded": 1, "hard": 1, "unmatched": 1, "skipped": 1}
    # The same report again (e.g. marked unread) is not recorded twice.
    fake_imap.seen_all = set()
    assert bounces.poll(factory=fake_imap)["recorded"] == 0
    assert BouncePollState.get().last_success_at is not None


@pytest.mark.parametrize("failure", ["auth", "connect", "tls", "folder"])
def test_poll_failures_are_recorded_and_the_check_fails_once_stale(mailbox, fake_imap, failure):
    fake_imap.fail = failure
    assert bounces.poll(factory=fake_imap) == {"state": "failed", "code": failure, "read": 0}
    state = BouncePollState.get()
    assert state.last_failure == failure
    ok, detail = bounces.health()
    assert not ok and bounces.FAILURE_WORDS[failure] in detail
    assert "test-only-not-a-secret" not in detail
    # A recent success keeps one failed read from alerting.
    state.last_success_at = timezone.now() - datetime.timedelta(minutes=30)
    state.last_failure_at = timezone.now()
    state.save()
    assert bounces.health()[0]


def test_health_hidden_until_set_up_or_switched_on(settings):
    settings.BOUNCE_IMAP_HOST = ""
    assert bounces.health() is None
    features.set_feature(bounces.FEATURE, True, actor=SYSTEM)
    ok, detail = bounces.health()
    assert not ok and "set-bounce-mailbox.sh" in detail


def test_config_refuses_odd_hosts(settings):
    settings.BOUNCE_IMAP_HOST = "imap.example.org/evil"
    settings.BOUNCE_IMAP_USER, settings.BOUNCE_IMAP_PASSWORD = "u", "p"
    assert not bounces.config().configured
    settings.BOUNCE_IMAP_HOST = "imap.example.org"
    settings.BOUNCE_IMAP_PORT = 70000
    assert not bounces.config().configured


def test_check_bounce_mailbox_command(mailbox, fake_imap, monkeypatch):
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_imap)
    out = io.StringIO()
    call_command("check_bounce_mailbox", stdout=out)
    assert "OK: signed in to imap.example.org:993 over TLS" in out.getvalue()
    fake_imap.fail = "auth"
    with pytest.raises(CommandError) as exc:
        call_command("check_bounce_mailbox")
    assert "refused the sign-in" in str(exc.value)
    assert "test-only-not-a-secret" not in str(exc.value)
    assert "bounces@example.org" not in str(exc.value)


def test_task_does_nothing_while_off(settings):
    from comms.tasks import poll_bounces

    settings.BOUNCE_IMAP_HOST = ""
    assert poll_bounces(timestamp=0) == {"state": "off"}


# --- Effects -------------------------------------------------------------------------------


def _notice_world(tagging):
    """A consenting owner, a meeting notice sent through the portal's
    backend; the token as it went out."""
    from governance import services as gov

    lot = LotFactory(street_address="12 Bounce Lane")
    owner = make_member(lots=[lot], email=OWNER_EMAIL)
    community.update_my_profile(user=owner, electronic_notice_consent=True)
    board = make_member(board=True)
    meeting = gov.create_meeting(
        title="Annual meeting",
        kind="annual",
        location="Clubhouse",
        starts_at=timezone.now() + datetime.timedelta(days=30),
        actor=board,
    )
    mail.outbox = []
    gov.send_notice(meeting, actor=board)
    (sent,) = [m for m in mail.outbox if OWNER_EMAIL in m.to]
    return lot, owner, board, sent.extra_headers["X-Portal-Ref"]


def _titles(user):
    from core.my_items import items_for

    return [i.title for i in items_for(user)]


def test_a_bounced_official_notice_asks_the_board_for_a_paper_copy(
    client, tagging, mailbox, fake_imap
):
    lot, owner, board, ref = _notice_world(tagging)
    assert bounces.read_ref(ref).kind == "notice"
    fake_imap.messages = {b"7": dsn(ref, OWNER_EMAIL)}
    bounces.poll(factory=fake_imap)
    title = "Send a paper copy to 12 Bounce Lane"
    assert title in _titles(board)
    manager = make_member(roles=[Role.MANAGER])
    assert title in _titles(manager)
    assert title not in _titles(owner)
    client.force_login(board)
    page = client.get(reverse("paper_mail")).content.decode()
    assert "Official notices that bounced" in page and title in page
    assert bounces.paper_url(lot) in page.replace("&amp;", "&")
    # Owners cannot close it.
    client.force_login(owner)
    assert client.post(reverse("bounce_paper_done", args=[lot.pk])).status_code in (403, 404)
    # The board says it went another way: closed, audited with ids only.
    client.force_login(board)
    response = client.post(reverse("bounce_paper_done", args=[lot.pk]))
    assert response.status_code == 302
    assert title not in _titles(board)
    with system_access("t"):
        assert BounceFollowUp.objects.count() == 1
    event = AuditEvent.objects.get(action="comms.bounce_paper_sent")
    assert event.metadata == {"lot_id": lot.pk, "bounces": 1}


def test_a_mailing_for_the_home_closes_it(tagging, mailbox, fake_imap):
    from comms import papermail

    lot, _owner, board, ref = _notice_world(tagging)
    fake_imap.messages = {b"7": dsn(ref, OWNER_EMAIL)}
    bounces.poll(factory=fake_imap)
    assert bounces.paper_copies_needed()
    papermail.create_mailing(
        actor=board,
        defaults=papermail.source_defaults(),
        subject="Annual meeting notice",
        body="Notice.",
        everyone=True,
        lot_id=lot.pk,
    )
    assert bounces.paper_copies_needed() == []


def test_soft_bounces_and_other_kinds_ask_for_nothing(tagging, mailbox, fake_imap):
    _lot, owner, _board, ref = _notice_world(tagging)
    fake_imap.messages = {
        b"1": dsn(ref, OWNER_EMAIL, status="4.2.2", action="delayed"),
        b"2": dsn(_ref("statement"), OWNER_EMAIL, ident="dsn-2"),
    }
    bounces.poll(factory=fake_imap)
    assert bounces.paper_copies_needed() == []
    # The hard statement bounce still marks the address.
    assert [b["address"] for b in bounces.bouncing(owner.person)] == [OWNER_EMAIL]


def test_bouncing_address_on_the_person_page_and_my_profile(client, mailbox, fake_imap):
    lot = LotFactory(street_address="3 Echo Court")
    owner = make_member(lots=[lot], email=OWNER_EMAIL)
    board = make_member(board=True)
    other = make_member(lots=[LotFactory()], email="other-owner@example.com")
    fake_imap.messages = {b"1": dsn(_ref("digest"), OWNER_EMAIL)}
    bounces.poll(factory=fake_imap)
    client.force_login(board)
    page = client.get(reverse("person_detail", args=[owner.person.pk])).content.decode()
    assert "Email bouncing:" in page and OWNER_EMAIL in page
    client.force_login(owner)
    page = client.get(reverse("my_profile")).content.decode()
    assert "came back as undeliverable" in page
    client.force_login(other)
    assert "came back as undeliverable" not in client.get(reverse("my_profile")).content.decode()
    # Owners cannot clear anyone's; the board can.
    client.force_login(owner)
    url = reverse("person_email_bounce_clear", args=[owner.person.pk])
    assert client.post(url).status_code == 403
    client.force_login(board)
    assert client.post(url).status_code == 302
    assert bounces.bouncing(owner.person) == []
    event = AuditEvent.objects.get(action="comms.bounce_cleared")
    assert event.metadata == {"person_id": owner.person.pk, "addresses": 1}
    # A later bounce shows again.
    fake_imap.messages = {b"9": dsn(_ref("digest"), OWNER_EMAIL).replace(b"dsn-1", b"dsn-9")}
    fake_imap.seen_all = set()
    bounces.poll(factory=fake_imap)
    assert bounces.bouncing(owner.person)


def test_owners_see_only_their_own_bounce_rows(mailbox, fake_imap):
    owner = make_member(lots=[LotFactory()], email=OWNER_EMAIL)
    other = make_member(lots=[LotFactory()], email="other-owner@example.com")
    fake_imap.messages = {
        b"1": dsn(_ref(to=OWNER_EMAIL), OWNER_EMAIL),
        b"2": dsn(_ref(to="other-owner@example.com"), "other-owner@example.com").replace(
            b"dsn-1", b"dsn-2"
        ),
    }
    bounces.poll(factory=fake_imap)
    assert EmailBounce.objects.for_user(owner).count() == 1
    assert EmailBounce.objects.for_user(other).get().person_id == other.person.pk


def test_feature_off_hides_everything(client, mailbox, fake_imap):
    owner = make_member(lots=[LotFactory()], email=OWNER_EMAIL)
    board = make_member(board=True)
    fake_imap.messages = {b"1": dsn(_ref(), OWNER_EMAIL)}
    bounces.poll(factory=fake_imap)
    features.set_feature(bounces.FEATURE, False, actor=SYSTEM)
    client.force_login(board)
    page = client.get(reverse("person_detail", args=[owner.person.pk])).content.decode()
    assert "Email bouncing:" not in page
    assert (
        client.post(reverse("person_email_bounce_clear", args=[owner.person.pk])).status_code == 404
    )


def test_a_burst_of_hard_bounces_goes_on_the_timeline(mailbox, fake_imap):
    from core.models import OpsEvent

    addresses = [f"burst{i}@example.com" for i in range(bounces.BURST)]
    fake_imap.messages = {
        str(i).encode(): dsn(_ref(to=a), a).replace(b"dsn-1", f"dsn-b{i}".encode())
        for i, a in enumerate(addresses)
    }
    bounces.poll(factory=fake_imap)
    event = OpsEvent.objects.get(kind="email")
    assert event.state == "problem" and event.count == bounces.BURST
    assert not any(a in event.message for a in addresses)


def test_set_up_status_for_the_wizard(settings):
    settings.BOUNCE_IMAP_HOST = ""
    assert bounces.setup_status()["state"] == "not_set_up"
    assert "set-bounce-mailbox.sh" in bounces.setup_status()["text"]
