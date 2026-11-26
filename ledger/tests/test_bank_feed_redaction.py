"""Bank-line descriptions never keep account or routing numbers (SEC-004), and
OFX lines without FITID keep same-day duplicates (SEC-006)."""

from __future__ import annotations

import datetime
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest
from django.apps import apps as global_apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from community.models import Role
from community.tests.factories import make_member
from core.models import FeatureSetting
from ledger.bankfeeds import csvfile, ofx, services, teller
from ledger.bankfeeds.base import MASK, TransactionInfo, clean_description, redact_numbers
from ledger.models import BankTransaction
from ledger.tests.test_bank_feeds import KEY, TOKEN, FakeOpener, _file_account

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(settings):
    settings.BANK_FEED_TOKEN_KEY = KEY
    FeatureSetting.objects.create(key="bank_feeds", enabled=True)
    return {"board": make_member(board=True), "manager": make_member(roles=[Role.MANAGER])}


ACCOUNT = "000123456789"
ROUTING = "021000021"

CASES = [
    (f"WIRE FROM ACCT {ACCOUNT} RTN {ROUTING}", f"WIRE FROM ACCT {MASK}6789 RTN {MASK}0021"),
    ("ACCT#0001-2345-6789 PMT", f"ACCT#{MASK}6789 PMT"),
    ("ACH ID 1234 5678 9012", f"ACH ID {MASK}9012"),
    ("REF 123456", f"REF {MASK}3456"),
    ("CHECK 10234", "CHECK 10234"),  # check numbers (five digits or fewer) stay
    ("CHECK 1001", "CHECK 1001"),
    ("DEPOSIT 2026-11-15 BRANCH 7", "DEPOSIT 2026-11-15 BRANCH 7"),  # dates stay
    ("POSTED 20261115", "POSTED 20261115"),
    ("ENTRY 11/15/2026", "ENTRY 11/15/2026"),
    ("SETTLED 111526", "SETTLED 111526"),
    ("TOTAL 1234567.89", "TOTAL 1234567.89"),  # amounts stay
    ("TOTAL $250000", "TOTAL $250000"),
    ("1,234,567.89 TRANSFER", "1,234,567.89 TRANSFER"),
    ("", ""),
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_redact_numbers(raw, expected):
    assert redact_numbers(raw) == expected


def test_redaction_never_keeps_more_than_four_digits_of_a_long_number():
    text = clean_description("Payee 9" * 10 + " 4111 1111 1111 4321 " + "x" * 300)
    assert "4111" not in text and text.count(MASK) == 1 and len(text) <= 200


OFX_WIRE = f"""OFXHEADER:100
DATA:OFXSGML

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD
<BANKACCTFROM><BANKID>{ROUTING}<ACCTID>99996789<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261103<TRNAMT>450.00<FITID>W1
<NAME>WIRE IN<MEMO>FROM ACCT {ACCOUNT} RTN {ROUTING}</STMTTRN>
<STMTTRN><TRNTYPE>CHECK<DTPOSTED>20261104<TRNAMT>-80.00<FITID>W2<NAME>CHECK 1001</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
""".encode()


def test_ofx_descriptions_are_redacted():
    (account,) = ofx.parse(OFX_WIRE).accounts
    first, second = account.transactions
    assert first.description == f"WIRE IN · FROM ACCT {MASK}6789 RTN {MASK}0021"
    assert second.description == "CHECK 1001"
    assert ACCOUNT not in repr(account) and ROUTING not in repr(account)


def test_csv_descriptions_and_preview_are_redacted():
    text = f"Date,Description,Amount\n2026-11-03,WIRE FROM {ACCOUNT},450.00\n"
    lines, _ = csvfile.parse(text, csvfile.Mapping(date=0, description=1, amount=2))
    assert lines[0].description == f"WIRE FROM {MASK}6789"
    _headers, sample = csvfile.head(text)
    assert ACCOUNT not in json.dumps(sample)


def test_teller_descriptions_are_redacted():
    day = datetime.date(2026, 11, 3)
    row = {
        "id": "txn_w",
        "date": day.isoformat(),
        "amount": "450.00",
        "description": f"WIRE FROM ACCT {ACCOUNT}",
        "type": "wire",
        "status": "posted",
    }
    opener = FakeOpener({"/accounts/acc_1/transactions": [row]})
    (line,) = teller.TellerProvider(TOKEN, opener=opener).transactions("acc_1", day)
    assert line.description == f"WIRE FROM ACCT {MASK}6789"


def test_storing_redacts_whatever_the_provider_passed(w):
    conn = _file_account(w)
    raw = TransactionInfo("raw-1", datetime.date(2026, 11, 3), 100, f"MEMO {ACCOUNT}")
    services.store_lines(conn, [raw], source="csv", actor=w["board"])
    assert BankTransaction.objects.get().description == f"MEMO {MASK}6789"


def test_ofx_import_page_stores_no_full_numbers(client, w):
    conn = _file_account(w)
    client.force_login(w["manager"])
    client.post(
        reverse("bank_import"), {"account": conn.pk, "file": SimpleUploadedFile("w.ofx", OFX_WIRE)}
    )
    stored = list(BankTransaction.objects.values_list("description", flat=True))
    assert len(stored) == 2
    assert not any(ACCOUNT in d or ROUTING in d for d in stored)


# --- SEC-006: OFX lines without FITID ----------------------------------------------------------

OFX_NO_FITID = b"""OFXHEADER:100
DATA:OFXSGML

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD
<BANKACCTFROM><ACCTID>6789<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261105<TRNAMT>150.00<NAME>MOBILE DEPOSIT</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261105<TRNAMT>150.00<NAME>MOBILE DEPOSIT</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261106<TRNAMT>150.00<NAME>MOBILE DEPOSIT</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def test_ofx_without_fitid_keeps_identical_same_day_lines():
    (account,) = ofx.parse(OFX_NO_FITID).accounts
    ids = [t.bank_id for t in account.transactions]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert all(i.startswith("ofxh:") for i in ids)
    # The first occurrence keeps the id earlier versions gave it, so files
    # imported before this fix are not imported twice.
    legacy = hashlib.sha256(b"2026-11-05|15000|mobile deposit").hexdigest()[:48]
    assert ids[0] == f"ofxh:{legacy}"
    assert ofx.parse(OFX_NO_FITID).accounts[0].transactions == account.transactions


def test_reimporting_a_file_without_fitid_adds_nothing(client, w):
    conn = _file_account(w)
    client.force_login(w["manager"])
    for _ in range(2):
        client.post(
            reverse("bank_import"),
            {"account": conn.pk, "file": SimpleUploadedFile("n.ofx", OFX_NO_FITID)},
        )
    assert BankTransaction.objects.filter(connection=conn).count() == 3


# --- The data migration for lines stored before the fix ------------------------------------------

_migration = importlib.import_module("ledger.migrations.0016_redact_bank_descriptions")


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_migration_masking_matches_the_importer(raw, expected):
    assert _migration.redact_numbers(raw) == redact_numbers(raw) == expected


def test_migration_redacts_stored_descriptions_and_nothing_else(w):
    conn = _file_account(w)
    day = datetime.date(2026, 11, 3)
    dirty = BankTransaction.objects.create(
        connection=conn,
        bank_id="ofx:old1",
        posted_on=day,
        amount_cents=45000,
        description=f"WIRE FROM ACCT {ACCOUNT} RTN {ROUTING}",
        kind="credit",
        source="ofx",
    )
    clean = BankTransaction.objects.create(
        connection=conn,
        bank_id="ofx:old2",
        posted_on=day,
        amount_cents=-8000,
        description="CHECK 1001",
        source="ofx",
    )
    fields = ["bank_id", "posted_on", "amount_cents", "kind", "source", "imported_at"]
    before = {
        row["id"]: row for row in BankTransaction.objects.values("id", "description", *fields)
    }

    _migration.redact(global_apps, SimpleNamespace(connection=connection))

    after = {row["id"]: row for row in BankTransaction.objects.values("id", "description", *fields)}
    assert after[dirty.pk]["description"] == f"WIRE FROM ACCT {MASK}6789 RTN {MASK}0021"
    assert after[clean.pk] == before[clean.pk]
    for name in fields:
        assert after[dirty.pk][name] == before[dirty.pk][name]
    # The append-only trigger is back on after the one statement.
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE ledger_banktransaction SET kind = 'x'")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgenabled FROM pg_trigger "
            "WHERE tgname = 'ledger_banktransaction_append_only_row'"
        )
        assert cursor.fetchone()[0] == "O"


# --- SEC-005: Teller Connect script --------------------------------------------------------------


def test_connect_script_is_limited_to_tellers_connect_path_and_sends_no_referrer(
    client, w, settings
):
    settings.TELLER_APPLICATION_ID = "app_test"
    settings.TELLER_ENVIRONMENT = "sandbox"
    client.force_login(w["board"])
    session = client.session
    session["account_authentication_methods_user"] = str(w["board"].pk)
    session["account_authentication_methods"] = [
        {"method": "password", "at": timezone.now().timestamp(), "reauthenticated": True}
    ]
    session.save()
    page = client.get(reverse("bank_connect"))
    assert page.status_code == 200
    directives = page["Content-Security-Policy"].split(";")
    script_src = next(d for d in directives if d.strip().startswith("script-src")).split()
    assert [s for s in script_src if "teller.io" in s] == ["https://cdn.teller.io/connect/"]
    tag = b'<script src="https://cdn.teller.io/connect/connect.js" referrerpolicy="no-referrer">'
    assert tag in page.content
