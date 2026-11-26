"""Bank feed (bankfeeds, D32-bankfeeds): Teller client, token encryption, the
connect page's CSP, statement file import (OFX/QFX/CSV), matching into the
reconciliation, permissions, health check and audit."""

from __future__ import annotations

import base64
import datetime
import io
import json
import logging
import os
import ssl
import stat
import urllib.error
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import health
from core.append_only import AppendOnlyError
from core.models import FeatureSetting
from core.policy import SYSTEM
from documents.validation import UploadRejected, check_upload
from ledger import accounting as acc
from ledger import services as ledger_services
from ledger.bankfeeds import csvfile, matching, ofx, services, teller, tokens
from ledger.bankfeeds.base import (
    AccountInfo,
    BalanceInfo,
    BankFeedError,
    Provider,
    TransactionInfo,
    clean_text,
    to_cents,
)
from ledger.models import (
    BankConnection,
    BankConnectionStatus,
    BankMatch,
    BankTransaction,
    ReconciledItem,
)
from operations.models import ExpenseCategory, Invoice, InvoicePayment, Vendor

pytestmark = pytest.mark.django_db
TOKEN = "test_token_" + "x" * 20
KEY = "k" * 40


def _today():
    return timezone.localdate()


@pytest.fixture
def w(settings):
    settings.BANK_FEED_TOKEN_KEY = KEY
    FeatureSetting.objects.create(key="bank_feeds", enabled=True)
    board = make_member(board=True)
    lot = LotFactory()
    owner = make_member(lots=[lot])
    manager = make_member(roles=[Role.MANAGER])
    auditor = make_member(roles=[Role.AUDITOR])
    return {"board": board, "owner": owner, "manager": manager, "auditor": auditor, "lot": lot}


def _reauth(client, user):
    client.force_login(user)
    session = client.session
    session["account_authentication_methods_user"] = str(user.pk)
    session["account_authentication_methods"] = [
        {"method": "password", "at": timezone.now().timestamp(), "reauthenticated": True}
    ]
    session.save()


# --- A fake HTTPS layer for the Teller client ---------------------------------------------


class _Response:
    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.status = status

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for urllib's opener: records each request, answers from a
    route table or raises the given error."""

    def __init__(self, routes=None, error=None):
        self.routes = routes or {}
        self.error = error
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        path = request.full_url.split("api.teller.io", 1)[1].split("?", 1)[0]
        answer = self.routes[path]
        return _Response(answer(request) if callable(answer) else answer)


ACCOUNTS = [
    {
        "id": "acc_1",
        "name": "Operating Checking",
        "last_four": "6789",
        "type": "depository",
        "status": "open",
        "institution": {"id": "demo", "name": "Demo Bank"},
    },
    {"id": "acc_card", "name": "Card", "last_four": "1111", "type": "credit", "status": "open"},
]


def _txn(n, day, amount="-10.00", status="posted"):
    return {
        "id": f"txn_{n}",
        "date": day.isoformat(),
        "amount": amount,
        "description": f"Line {n}",
        "type": "ach",
        "status": status,
    }


def test_teller_lists_accounts_with_last_four_only_and_basic_auth():
    opener = FakeOpener({"/accounts": ACCOUNTS})
    accounts = teller.TellerProvider(TOKEN, opener=opener).list_accounts()
    assert [a.last_four for a in accounts] == ["6789", "1111"]
    request, timeout = opener.calls[0]
    assert request.full_url == "https://api.teller.io/accounts"
    expected = "Basic " + base64.b64encode(f"{TOKEN}:".encode()).decode()
    assert request.get_header("Authorization") == expected
    assert timeout == teller.TIMEOUT_SECONDS
    assert TOKEN not in repr(teller.TellerProvider(TOKEN, opener=opener))


def test_teller_follows_pagination_and_stops_at_the_since_date():
    today = _today()
    page1 = [_txn(i, today) for i in range(teller.PAGE_SIZE)]
    page2 = [_txn(1000, today - datetime.timedelta(days=1)), _txn(1001, today, status="pending")]
    page2.append(_txn(1002, today - datetime.timedelta(days=400)))  # older than since

    def pages(request):
        return page2 if "from_id=" in request.full_url else page1

    opener = FakeOpener({"/accounts/acc_1/transactions": pages})
    lines = teller.TellerProvider(TOKEN, opener=opener).transactions(
        "acc_1", today - datetime.timedelta(days=30)
    )
    assert len(opener.calls) == 2
    assert f"from_id=txn_{teller.PAGE_SIZE - 1}" in opener.calls[1][0].full_url
    ids = {t.bank_id for t in lines}
    assert "txn_1000" in ids and "txn_1001" not in ids and "txn_1002" not in ids
    assert lines[0].amount_cents == -1000


def test_teller_errors_are_reported_by_status_only(caplog):
    body = json.dumps(
        {"error": {"code": "enrollment.disconnected.user_action", "message": "SECRET BODY TEXT"}}
    ).encode()
    error = urllib.error.HTTPError(
        "https://api.teller.io/accounts", 404, "Not Found", {}, io.BytesIO(body)
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(teller.TellerError) as exc:
        teller.TellerProvider(TOKEN, opener=FakeOpener(error=error)).list_accounts()
    assert exc.value.status == 404 and exc.value.needs_reauth
    assert "404" in str(exc.value) and "SECRET" not in str(exc.value)
    assert "SECRET" not in caplog.text and TOKEN not in caplog.text

    error = urllib.error.HTTPError("https://api.teller.io/accounts", 500, "x", {}, io.BytesIO(b""))
    with pytest.raises(teller.TellerError) as exc:
        teller.TellerProvider(TOKEN, opener=FakeOpener(error=error)).list_accounts()
    assert exc.value.status == 500 and not exc.value.needs_reauth

    with pytest.raises(teller.TellerError) as exc:
        teller.TellerProvider(
            TOKEN, opener=FakeOpener(error=urllib.error.URLError("down"))
        ).list_accounts()
    assert exc.value.status == 0


def test_teller_refuses_other_hosts_and_redirects(monkeypatch):
    monkeypatch.setattr(teller, "API_BASE", "https://evil.example.com")
    with pytest.raises(BankFeedError):
        teller.TellerProvider(TOKEN, opener=FakeOpener()).list_accounts()
    handler = teller._NoRedirect()
    request = urllib.request.Request("https://api.teller.io/accounts")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example.com/")


def _self_signed_pem():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test client")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def test_mtls_context_from_base64_env_uses_a_private_temp_file_then_removes_it(
    settings, monkeypatch
):
    cert, key = _self_signed_pem()
    settings.TELLER_ENVIRONMENT = "development"
    settings.TELLER_CERTIFICATE_B64 = base64.b64encode(cert).decode()
    settings.TELLER_PRIVATE_KEY_B64 = base64.b64encode(key).decode()
    seen = {}
    real = ssl.SSLContext.load_cert_chain

    def spy(self, path, *args, **kwargs):
        seen["path"] = path
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        return real(self, path, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", spy)
    context = teller.ssl_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert seen["mode"] == 0o600
    assert not os.path.exists(seen["path"])


def test_mtls_context_refuses_bad_or_missing_certificates(settings):
    settings.TELLER_ENVIRONMENT = "development"
    settings.TELLER_CERTIFICATE_B64 = ""
    settings.TELLER_PRIVATE_KEY_B64 = ""
    with pytest.raises(BankFeedError, match="certificate"):
        teller.ssl_context()
    settings.TELLER_CERTIFICATE_B64 = "not base64!!"
    settings.TELLER_PRIVATE_KEY_B64 = "x"
    with pytest.raises(BankFeedError, match="base64"):
        teller.ssl_context()
    settings.TELLER_CERTIFICATE_B64 = base64.b64encode(
        b"-----BEGIN CERTIFICATE-----\nAA\n"
    ).decode()
    settings.TELLER_PRIVATE_KEY_B64 = base64.b64encode(
        b"-----BEGIN PRIVATE KEY-----\nAA\n"
    ).decode()
    with pytest.raises(BankFeedError, match="do not load"):
        teller.ssl_context()
    settings.TELLER_ENVIRONMENT = "sandbox"
    settings.TELLER_CERTIFICATE_B64 = settings.TELLER_PRIVATE_KEY_B64 = ""
    assert isinstance(teller.ssl_context(), ssl.SSLContext)
    settings.TELLER_ENVIRONMENT = "live"
    with pytest.raises(BankFeedError):
        teller.environment()


# --- Token encryption ---------------------------------------------------------------------


def test_token_encryption_round_trip_and_refusal_without_a_key(settings):
    settings.BANK_FEED_TOKEN_KEY = KEY
    stored = tokens.encrypt(TOKEN)
    assert TOKEN not in stored and tokens.decrypt(stored) == TOKEN
    settings.BANK_FEED_TOKEN_KEY = "y" * 40
    with pytest.raises(tokens.TokenUnreadable):
        tokens.decrypt(stored)
    settings.BANK_FEED_TOKEN_KEY = ""  # nosec B105 - test value
    assert not tokens.configured()
    with pytest.raises(tokens.TokenKeyMissing):
        tokens.encrypt(TOKEN)
    settings.BANK_FEED_TOKEN_KEY = "short"  # nosec B105 - test value
    with pytest.raises(tokens.TokenKeyMissing):
        tokens.encrypt(TOKEN)


class FakeTeller(Provider):
    name = "teller"

    def __init__(self, token=None, *, lines=(), balance=None, error=None, opener=None):
        self.lines = list(lines)
        self.bal = balance
        self.error = error

    def list_accounts(self):
        return [
            AccountInfo("acc_1", "Operating Checking", "Demo Bank", "6789", "depository"),
            AccountInfo("acc_card", "Card", "Demo Bank", "1111", "credit"),
        ]

    def transactions(self, account_id, since):
        if self.error:
            raise self.error
        return [t for t in self.lines if t.posted_on >= since]

    def balance(self, account_id):
        return self.bal

    def remove_account(self, account_id):
        return None


def test_connect_refuses_without_the_token_key(w, settings):
    settings.BANK_FEED_TOKEN_KEY = ""  # nosec B105 - test value
    with pytest.raises(tokens.TokenKeyMissing):
        services.connect_teller(access_token=TOKEN, actor=w["board"], provider=FakeTeller())
    assert not BankConnection.objects.exists()


def test_connect_stores_depository_accounts_encrypted_with_last_four_only(w):
    made = services.connect_teller(access_token=TOKEN, actor=w["board"], provider=FakeTeller())
    assert len(made) == 1
    conn = made[0]
    assert conn.account_last_four == "6789" and conn.institution_name == "Demo Bank"
    assert TOKEN not in conn.token_encrypted and tokens.decrypt(conn.token_encrypted) == TOKEN
    event = AuditEvent.objects.get(action="bank_feed.connected")
    assert TOKEN not in json.dumps(event.metadata)
    with pytest.raises(PermissionDenied):
        services.connect_teller(access_token=TOKEN, actor=w["manager"], provider=FakeTeller())


def test_last_four_is_enforced_by_the_database(w):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        BankConnection.objects.create(
            provider="file", display_name="x", account_last_four="12a4", created_by=w["board"]
        )


# --- CSP only on the connect page ---------------------------------------------------------


def test_csp_allows_teller_only_on_the_connect_page(client, w, settings):
    settings.TELLER_APPLICATION_ID = "app_test"
    settings.TELLER_ENVIRONMENT = "sandbox"
    _reauth(client, w["board"])
    page = client.get(reverse("bank_connect"))
    assert page.status_code == 200
    csp = page["Content-Security-Policy"]
    assert "https://cdn.teller.io" in csp and "https://api.teller.io" in csp
    assert "'unsafe-inline'" not in csp
    assert b"cdn.teller.io/connect/connect.js" in page.content
    for other in (reverse("bank_feed"), reverse("accounting"), reverse("home")):
        response = client.get(other)
        assert "teller.io" not in response.get("Content-Security-Policy", ""), other


def test_connect_page_needs_recent_reauthentication_and_the_board(client, w, settings):
    settings.TELLER_APPLICATION_ID = "app_test"
    client.force_login(w["board"])
    response = client.get(reverse("bank_connect"))
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    for who in ("manager", "auditor", "owner"):
        _reauth(client, w[who])
        assert client.get(reverse("bank_connect")).status_code in (403, 404), who


def test_enrollment_post_needs_the_pages_nonce_and_never_shows_the_token(
    client, w, settings, caplog
):
    settings.TELLER_APPLICATION_ID = "app_test"
    caplog.set_level(logging.DEBUG)
    _reauth(client, w["board"])
    client.get(reverse("bank_connect"))
    response = client.post(reverse("bank_connect"), {"nonce": "wrong", "access_token": TOKEN})
    assert response.status_code == 302 and not BankConnection.objects.exists()
    page = client.get(reverse("bank_connect"))
    nonce = client.session["bank_connect"]["nonce"]
    assert nonce.encode() in page.content
    with (
        mock.patch("ledger.bankfeeds.services.TellerProvider", FakeTeller),
        mock.patch("ledger.bankfeeds.providers.TellerProvider", FakeTeller),
    ):
        response = client.post(
            reverse("bank_connect"), {"nonce": nonce, "access_token": TOKEN}, follow=True
        )
    assert BankConnection.objects.filter(account_last_four="6789").exists()
    assert TOKEN.encode() not in response.content
    assert TOKEN not in response.redirect_chain[-1][0]
    assert TOKEN not in caplog.text
    assert "bank_connect" not in client.session  # the nonce is single-use


# --- Disconnect ---------------------------------------------------------------------------


def test_disconnect_deletes_the_token_keeps_history_and_is_board_only(client, w):
    conn = services.connect_teller(access_token=TOKEN, actor=w["board"], provider=FakeTeller())[0]
    line = TransactionInfo("txn_1", _today(), 500, "Deposit")
    services.sync(conn, actor=w["board"], provider=FakeTeller(lines=[line]))
    _reauth(client, w["manager"])
    assert client.post(reverse("bank_disconnect", args=[conn.pk])).status_code == 403
    _reauth(client, w["board"])
    with mock.patch("ledger.bankfeeds.providers.TellerProvider", FakeTeller):
        client.post(reverse("bank_disconnect", args=[conn.pk]))
    conn.refresh_from_db()
    assert not conn.token_encrypted and conn.status == BankConnectionStatus.DISCONNECTED
    assert BankTransaction.objects.filter(connection=conn).count() == 1
    assert AuditEvent.objects.filter(action="bank_feed.disconnected").exists()


# --- Sync ---------------------------------------------------------------------------------


def test_sync_is_idempotent_records_counts_and_marks_reauth(w):
    conn = services.connect_teller(access_token=TOKEN, actor=w["board"], provider=FakeTeller())[0]
    lines = [TransactionInfo(f"txn_{i}", _today(), 100 * i, f"Line {i}") for i in range(1, 4)]
    first = services.sync(
        conn, actor=w["manager"], provider=FakeTeller(lines=lines, balance=BalanceInfo(_today(), 9))
    )
    again = services.sync(conn, actor=SYSTEM, provider=FakeTeller(lines=lines))
    assert first == {"new": 3, "seen": 0} and again == {"new": 0, "seen": 3}
    meta = AuditEvent.objects.filter(action="bank_feed.synced").first().metadata
    assert set(meta) == {"connection_id", "new", "seen"}
    with pytest.raises(teller.TellerError):
        services.sync(
            conn,
            actor=SYSTEM,
            provider=FakeTeller(error=teller.TellerError(401, needs_reauth=True)),
        )
    conn.refresh_from_db()
    assert conn.status == BankConnectionStatus.NEEDS_REAUTH and conn.last_error_status == 401
    with pytest.raises(PermissionDenied):
        services.sync(conn, actor=w["owner"], provider=FakeTeller())


def test_daily_task_syncs_only_with_the_feature_on(w):
    conn = services.connect_teller(access_token=TOKEN, actor=w["board"], provider=FakeTeller())[0]
    with mock.patch("ledger.bankfeeds.providers.TellerProvider", FakeTeller):
        assert services.sync_all() == 1
        FeatureSetting.objects.filter(key="bank_feeds").update(enabled=False)
        assert services.sync_all() == 0
    conn.refresh_from_db()
    assert conn.last_synced_at is not None


# --- OFX parser ---------------------------------------------------------------------------

OFX_SGML = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
CHARSET:1252

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20261120</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS><CURDEF>USD
<BANKACCTFROM><BANKID>031000053<ACCTID>123456789<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20261101<DTEND>20261120
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261103120000[-5:EST]<TRNAMT>450.00
<FITID>F1<NAME>DEPOSIT &amp; CO<MEMO>Branch 7</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261110<TRNAMT>-2,500.00<FITID>F2<NAME>CHECK 1001</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>2026BAD<TRNAMT>-1.00<FITID>F3<NAME>broken</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261103<TRNAMT>450.00<FITID>F1<NAME>DUPLICATE ID</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>12000.00<DTASOF>20261120</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""

OFX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE"?>
<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>USD</CURDEF>
<CCACCTFROM><ACCTID>4111111111114321</ACCTID></CCACCTFROM>
<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261102</DTPOSTED>
<TRNAMT>-12.34</TRNAMT><FITID>X1</FITID><NAME>Caf\xc3\xa9 &lt;Main&gt; &#38; &xxe;</NAME></STMTTRN>
</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>
"""


def test_ofx_sgml_statement():
    statement = ofx.parse(OFX_SGML)
    (account,) = statement.accounts
    assert account.last_four == "6789"
    assert [t.bank_id for t in account.transactions] == ["ofx:F1", "ofx:F2"]
    first, second = account.transactions
    assert first.posted_on == datetime.date(2026, 11, 3) and first.amount_cents == 45000
    assert first.description == "DEPOSIT & CO · Branch 7" and first.kind == "credit"
    assert second.amount_cents == -250000
    assert account.balance == BalanceInfo(datetime.date(2026, 11, 20), 1200000)
    assert statement.skipped == 1
    assert "031000053" not in repr(statement) and "123456789" not in repr(statement)


def test_ofx_xml_statement_decodes_only_safe_entities():
    (account,) = ofx.parse(OFX_XML).accounts
    assert account.kind == "credit" and account.last_four == "4321"
    (line,) = account.transactions
    assert line.description == "Café <Main> & &xxe;"
    assert line.amount_cents == -1234


@pytest.mark.parametrize(
    "data",
    [
        b'<?xml version="1.0"?><!DOCTYPE OFX [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><OFX></OFX>',
        b"OFXHEADER:100\n<!ENTITY a 'b'>\n<OFX></OFX>",
        b"<OFX><STMTRS><![CDATA[x]]></STMTRS></OFX>",
    ],
)
def test_ofx_refuses_doctype_entities_and_cdata(data):
    with pytest.raises(BankFeedError, match="not allowed"):
        ofx.parse(data)


def test_ofx_refuses_malformed_and_huge_files():
    with pytest.raises(BankFeedError, match="not an OFX"):
        ofx.parse(b"Date,Amount\n2026-11-01,5\n")
    with pytest.raises(BankFeedError, match="No account"):
        ofx.parse(b"<OFX><SIGNONMSGSRSV1></SIGNONMSGSRSV1></OFX>")
    with pytest.raises(BankFeedError, match="too large"):
        ofx.parse(b"<OFX>" + b" " * (ofx.MAX_BYTES + 1))
    with mock.patch.object(ofx, "MAX_TOKENS", 50), pytest.raises(BankFeedError, match="too large"):
        ofx.parse(b"<OFX><STMTRS>" + b"<A>1" * 100 + b"</STMTRS></OFX>")
    # Garbage inside a statement does not crash the reader.
    statement = ofx.parse(b"<OFX><STMTRS><STMTTRN><TRNAMT>abc</STMTTRN></STMTRS></OFX>")
    assert statement.accounts[0].transactions == [] and statement.skipped == 1


def test_amount_and_text_cleaning():
    assert to_cents("1,234.56") == 123456 and to_cents("(40.00)") == -4000
    assert to_cents("$7") == 700 and to_cents("-12,5") == -1250
    for bad in ("", "abc", "1e9", "99999999999999"):
        with pytest.raises(BankFeedError):
            to_cents(bad)
    assert clean_text("a\x00b" + chr(0x202E) + "\nc" + "x" * 500) == ("a b c" + "x" * 500)[:200]


# --- CSV ------------------------------------------------------------------------------------

CSV_SIGNED = (
    "Posting Date,Description,Amount,Balance\n"
    "11/03/2026,Deposit,450.00,1000\n"
    "11/03/2026,Deposit,450.00,1450\n"
    "2026-11-10,=HYPERLINK(evil),-2500.00,0\n"
    "Total,,,\n"
)
CSV_SPLIT = "Date;Memo;Debit;Credit\n2026-11-04;Fee;5.00;\n2026-11-05;Interest;;1.25\n"


def test_csv_mapping_signed_amount_keeps_same_day_duplicates_and_skips_footers():
    headers, _sample = csvfile.head(CSV_SIGNED)
    guess = csvfile.guess(headers)
    assert guess == {"date": 0, "description": 1, "amount": 2, "debit": None, "credit": None}
    lines, skipped = csvfile.parse(CSV_SIGNED, csvfile.Mapping(date=0, description=1, amount=2))
    assert [t.amount_cents for t in lines] == [45000, 45000, -250000]
    assert len({t.bank_id for t in lines}) == 3 and skipped == 1
    again, _ = csvfile.parse(CSV_SIGNED, csvfile.Mapping(date=0, description=1, amount=2))
    assert [t.bank_id for t in again] == [t.bank_id for t in lines]
    flipped, _ = csvfile.parse(
        CSV_SIGNED, csvfile.Mapping(date=0, description=1, amount=2, flip=True)
    )
    assert flipped[0].amount_cents == -45000


def test_csv_mapping_debit_and_credit_columns():
    headers, _ = csvfile.head(CSV_SPLIT)
    assert csvfile.guess(headers)["debit"] == 2 and csvfile.guess(headers)["credit"] == 3
    lines, _ = csvfile.parse(CSV_SPLIT, csvfile.Mapping(date=0, description=1, debit=2, credit=3))
    assert [t.amount_cents for t in lines] == [-500, 125]
    with pytest.raises(BankFeedError):
        csvfile.parse(CSV_SPLIT, csvfile.Mapping(date=0, description=0, amount=2))
    with pytest.raises(BankFeedError):
        csvfile.parse(CSV_SPLIT, csvfile.Mapping(date=0, description=1))


# --- Upload validation ----------------------------------------------------------------------


def test_statement_types_only_where_asked_for_and_checked_by_content():
    with pytest.raises(UploadRejected):
        check_upload(SimpleUploadedFile("s.ofx", OFX_SGML))  # not a library document type
    assert check_upload(SimpleUploadedFile("s.qfx", OFX_SGML), allowed={"qfx"}).ext == "qfx"
    latin = OFX_SGML.replace(b"Branch 7", b"Caf\xe9")  # Windows-1252
    assert check_upload(SimpleUploadedFile("s.ofx", latin), allowed={"ofx"})
    with pytest.raises(UploadRejected):
        check_upload(SimpleUploadedFile("s.ofx", b"MZ\x90\x00binary"), allowed={"ofx"})
    with pytest.raises(UploadRejected):
        check_upload(SimpleUploadedFile("s.ofx", b"just some text"), allowed={"ofx"})


# --- File import through the pages ------------------------------------------------------------


def _file_account(w, last_four=""):
    return services.add_file_account(
        display_name="Operating", last_four_digits=last_four, actor=w["board"]
    )


def test_ofx_import_is_idempotent_and_audited_without_descriptions(client, w):
    conn = _file_account(w)
    client.force_login(w["manager"])
    for _ in range(2):
        client.post(
            reverse("bank_import"),
            {"account": conn.pk, "file": SimpleUploadedFile("nov.ofx", OFX_SGML)},
        )
    assert BankTransaction.objects.filter(connection=conn).count() == 2
    conn.refresh_from_db()
    assert conn.account_last_four == "6789"
    events = list(AuditEvent.objects.filter(action="bank_feed.imported").order_by("id"))
    assert [e.metadata["new"] for e in events] == [2, 0]
    assert events[1].metadata["seen"] == 2
    dumped = json.dumps([e.metadata for e in AuditEvent.objects.all()])
    assert "DEPOSIT" not in dumped and "CHECK 1001" not in dumped and "6789" not in dumped


def test_import_refuses_a_file_for_another_account(client, w):
    conn = _file_account(w, last_four="0000")
    client.force_login(w["board"])
    client.post(
        reverse("bank_import"), {"account": conn.pk, "file": SimpleUploadedFile("n.ofx", OFX_SGML)}
    )
    assert not BankTransaction.objects.exists()


def test_csv_import_maps_columns_in_a_second_step(client, w):
    conn = _file_account(w)
    client.force_login(w["manager"])
    page = client.post(
        reverse("bank_import"),
        {"account": conn.pk, "file": SimpleUploadedFile("nov.csv", CSV_SIGNED.encode())},
    )
    assert page.status_code == 200 and b"Match the columns" in page.content
    assert not BankTransaction.objects.exists()
    bad = client.post(
        reverse("bank_import"),
        {
            "account": conn.pk,
            "step": "map",
            "csv_text": CSV_SIGNED,
            "col_date": "0",
            "col_description": "1",
        },
    )
    assert bad.status_code == 400 and b'role="alert"' in bad.content
    form = {
        "account": conn.pk,
        "step": "map",
        "csv_text": CSV_SIGNED,
        "col_date": "0",
        "col_description": "1",
        "col_amount": "2",
    }
    client.post(reverse("bank_import"), form)
    client.post(reverse("bank_import"), form)
    assert BankTransaction.objects.filter(connection=conn, source="csv").count() == 3


def test_bank_lines_are_append_only(w):
    conn = _file_account(w)
    services.store_lines(
        conn, [TransactionInfo("a", _today(), 100, "x")], source="csv", actor=w["board"]
    )
    line = BankTransaction.objects.get()
    line.amount_cents = 5
    with pytest.raises(AppendOnlyError):
        line.save()
    with pytest.raises(AppendOnlyError):
        line.delete()
    with pytest.raises(AppendOnlyError):
        BankTransaction.objects.all().update(amount_cents=1)


# --- Matching into the reconciliation --------------------------------------------------------


@pytest.fixture
def books(w):
    """A deposit of $450 three days ago and a $2,500 vendor payment."""
    today = _today()
    payment = ledger_services.record_payment(
        lot=w["lot"], amount_cents=45000, received_on=today, actor=w["manager"]
    )
    deposit = acc.create_deposit(
        entry_ids=[payment.pk], deposited_on=today - datetime.timedelta(days=3), actor=w["board"]
    )
    invoice = Invoice.objects.create(
        vendor=Vendor.objects.create(name="Mow Co"),
        number="A1",
        invoice_date=today,
        amount_cents=250000,
        category=ExpenseCategory.objects.create(code="5100", name="Landscaping"),
        entered_by=w["manager"],
        state="paid",
    )
    vendor_payment = InvoicePayment.objects.create(
        invoice=invoice,
        paid_on=today - datetime.timedelta(days=20),
        method="check",
        amount_cents=250000,
        recorded_by=w["board"],
    )
    conn = _file_account(w)
    lines = [
        TransactionInfo("d", today, 45000, "DEPOSIT"),
        TransactionInfo("p", today, -250000, "CHECK 1001"),
        TransactionInfo("far", today - datetime.timedelta(days=30), 45000, "OLD DEPOSIT"),
        TransactionInfo("fee", today, -500, "SERVICE FEE"),
    ]
    services.store_lines(conn, lines, source="csv", actor=w["board"])
    by_id = {t.bank_id: t for t in BankTransaction.objects.all()}
    return {"deposit": deposit, "payment": vendor_payment, "conn": conn, "lines": by_id}


def test_suggestions_by_amount_and_date_window(w, books):
    rows = {r["line"].bank_id: r["candidates"] for r in matching.suggestions()}
    assert [(c.kind, c.object_id) for c in rows["d"]] == [("deposit", books["deposit"].pk)]
    assert rows["d"][0].days_apart == 3
    assert [(c.kind, c.object_id) for c in rows["p"]] == [("payment", books["payment"].pk)]
    assert rows["far"] == [] and rows["fee"] == []


def test_confirmed_match_is_ticked_and_cleared_by_the_reconciliation(client, w, books):
    line = books["lines"]["d"]
    client.force_login(w["manager"])
    client.post(
        reverse("bank_match"),
        {"action": "confirm", "line": line.pk, "kind": "deposit", "object_id": books["deposit"].pk},
    )
    match = BankMatch.objects.get()
    with pytest.raises(BankFeedError):  # the same line or item twice
        matching.confirm(line, kind="deposit", object_id=books["deposit"].pk, actor=w["board"])
    with pytest.raises(BankFeedError):  # wrong amount
        matching.confirm(
            books["lines"]["fee"], kind="payment", object_id=books["payment"].pk, actor=w["board"]
        )
    today = _today()
    page = client.get(reverse("reconcile") + f"?date={today.isoformat()}").content.decode()
    assert f'name="deposits" value="{books["deposit"].pk}" checked' in page
    assert f'name="payments" value="{books["payment"].pk}" checked' not in page
    acc.reconcile(
        statement_date=today,
        statement_balance_cents=45000,
        cleared_deposit_ids=[books["deposit"].pk],
        cleared_payment_ids=[],
        actor=w["manager"],
    )
    assert ReconciledItem.objects.filter(kind="deposit", object_id=books["deposit"].pk).exists()
    with pytest.raises(BankFeedError):  # reconciled: the match stays
        matching.undo(match, actor=w["board"])
    meta = AuditEvent.objects.get(action="bank_feed.match_confirmed").metadata
    assert set(meta) == {"connection_id", "kind", "object_id"}


def test_undo_returns_the_line_to_the_lists(w, books):
    line = books["lines"]["p"]
    match = matching.confirm(line, kind="payment", object_id=books["payment"].pk, actor=w["board"])
    assert line.pk not in {t.pk for t in matching.unmatched_lines()}
    matching.undo(match, actor=w["manager"])
    assert line.pk in {t.pk for t in matching.unmatched_lines()}
    assert BankMatch.objects.count() == 2  # the undo is a new row
    with pytest.raises(BankFeedError):
        matching.undo(match, actor=w["board"])


def test_reconcile_page_fills_the_balance_from_the_feed(client, w):
    conn = _file_account(w)
    services.import_ofx(conn, OFX_SGML, actor=w["board"])
    client.force_login(w["board"])
    page = client.get(reverse("reconcile") + "?date=2026-11-20").content.decode()
    assert 'value="12000.00"' in page
    # Ten days earlier: the balance less the lines posted after.
    assert services.feed_balance(conn, datetime.date(2026, 11, 5)) == 1200000 + 250000


# --- Permissions ------------------------------------------------------------------------------


def test_owners_never_see_bank_pages(client, w):
    conn = _file_account(w)
    client.force_login(w["owner"])
    for url in (reverse("bank_feed"), reverse("bank_connect")):
        assert client.get(url).status_code in (403, 404), url
    for url in (
        reverse("bank_import"),
        reverse("bank_match"),
        reverse("bank_account_add"),
        reverse("bank_sync", args=[conn.pk]),
        reverse("bank_disconnect", args=[conn.pk]),
    ):
        assert client.post(url, {"account": conn.pk}).status_code in (403, 404), url
    page = client.get(reverse("home")).content
    assert b"Bank feed" not in page


def test_manager_imports_and_matches_but_cannot_connect_or_disconnect(client, w):
    conn = _file_account(w)
    _reauth(client, w["manager"])
    assert client.get(reverse("bank_feed")).status_code == 200
    page = client.get(reverse("bank_feed")).content
    assert b"Import a statement file" in page and b"Disconnect" not in page
    assert b"Connect a bank" not in page
    assert client.get(reverse("bank_connect")).status_code == 403
    assert client.post(reverse("bank_disconnect", args=[conn.pk])).status_code == 403
    assert client.post(reverse("bank_account_add"), {"display_name": "Reserve"}).status_code == 302
    assert BankConnection.objects.filter(display_name="Reserve").exists()


def test_auditor_sees_but_cannot_change(client, w):
    conn = _file_account(w)
    client.force_login(w["auditor"])
    page = client.get(reverse("bank_feed"))
    assert page.status_code == 200 and b"Import a statement file" not in page.content
    assert client.post(reverse("bank_import"), {"account": conn.pk}).status_code == 403
    assert client.post(reverse("bank_match"), {"action": "confirm"}).status_code == 403


def test_pages_are_gone_with_the_feature_off(client, w):
    FeatureSetting.objects.filter(key="bank_feeds").update(enabled=False)
    client.force_login(w["board"])
    assert client.get(reverse("bank_feed")).status_code == 404
    assert b"Bank feed" not in client.get(reverse("accounting")).content


# --- Health check -----------------------------------------------------------------------------


def _bank_row():
    return next((r for r in health.run_checks() if r.key == "bank_feed"), None)


def test_health_row_only_with_a_connection_and_fails_when_stale_or_reauth(w):
    with mock.patch.object(health, "CHECKS", [("bank_feed", "Bank feed", health._bank_feed)]):
        assert _bank_row() is None
        _file_account(w)
        assert _bank_row() is None  # statement files are not synced
        conn = services.connect_teller(access_token=TOKEN, actor=w["board"], provider=FakeTeller())[
            0
        ]
        services.sync(conn, actor=SYSTEM, provider=FakeTeller())
        assert _bank_row().ok
        BankConnection.objects.filter(pk=conn.pk).update(
            last_synced_at=timezone.now() - datetime.timedelta(days=4)
        )
        row = _bank_row()
        assert not row.ok and row.detail == "Not synced for more than 3 days"
        BankConnection.objects.filter(pk=conn.pk).update(status="needs_reauth")
        assert _bank_row().detail == "A bank account needs reconnecting"
