"""The account statement in plain words, with a "Pay online" QR code (D40).

The QR code holds only the address of a signed-in page (no token, account
number or personal data); the page opens the reader's own home, so a
scanned statement never shows anyone else's account. The wording covers
each state an owner can be in: due, past due, partly paid, in credit,
prepaid months, nothing to pay."""

import datetime
import re
from urllib.parse import urlsplit

import pytest
import segno
from django.urls import resolve, reverse
from django.utils import timezone

from community import services as community
from community.models import Account, Person
from community.tests.factories import LotFactory, make_member
from core import pdf, qr
from core.models import FeatureSetting
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import letters, services
from ledger.models import LetterKind
from payments import services as fees

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()
BASE = "https://portal.example.com"
URL_RE = re.compile(r"https://portal\.example\.com/my-account/pay/\?home=\d{1,9}")


@pytest.fixture
def st(settings):
    settings.PORTAL_BASE_URL = BASE
    board = make_member(board=True)
    community.set_rule(
        key="dues.monthly_cents", value="4500", citation="Declaration §7.2 (test)", actor=SYSTEM
    )
    lot = LotFactory(street_address="12 Willow Lane")
    other = LotFactory(street_address="14 Willow Lane")
    return {
        "board": board,
        "lot": lot,
        "owner": make_member(lots=[lot]),
        "other": other,
        "owner_b": make_member(lots=[other]),
    }


@pytest.fixture
def online(settings):
    """Online payments usable: the feature on and a Stripe test key."""
    settings.PAYMENTS_PROVIDER = "stripe"
    settings.STRIPE_SECRET_KEY = "rk_test_" + "K" * 24
    settings.DEPLOYMENT_STAGE = "poc"
    FeatureSetting.objects.update_or_create(key="online_payments", defaults={"enabled": True})
    community.set_rule(
        key="payments.credit_card_surcharge_bp", value="300", citation="Res. 2027-09", actor=SYSTEM
    )


def _charge(st, cents, on, desc="Gate repair share", lot=None):
    return services.post_charge(
        lot=lot or st["lot"],
        kind="other_charge",
        amount_cents=cents,
        description=desc,
        posted_on=on,
        actor=st["board"],
    )


def _pay(st, cents, on, reference, lot=None):
    return services.record_payment(
        lot=lot or st["lot"],
        amount_cents=cents,
        received_on=on,
        reference=reference,
        actor=st["board"],
    )


def _text(paras) -> str:
    out = []
    for _style, t in paras:
        out += list(t.caption) if isinstance(t, pdf.QrBlock) else [t]
    return "\n".join(out).replace("**", "")


def _qr(paras):
    blocks = [t for s, t in paras if s == "qr"]
    assert len(blocks) <= 1
    return blocks[0] if blocks else None


def _long(d):
    return f"{d:%B} {d.day}, {d.year}"


# --- The QR code: when, and what it holds -----------------------------------------------------


def test_qr_code_holds_only_the_plain_pay_address(st, online):
    url = fees.statement_pay_url(st["lot"])
    assert URL_RE.fullmatch(url), url
    parts = urlsplit(url)
    assert resolve(parts.path).url_name == "pay_start"
    assert parts.query == f"home={st['lot'].pk}"
    # Nothing token-like or personal: no long hex/base64 runs, no account
    # number, no name, no email, no street address.
    with system_access("t"):
        person = Person.objects.get(user=st["owner"])
        account = Account.objects.get(lot=st["lot"], closed_on__isnull=True)
    assert not re.search(r"[A-Za-z0-9_-]{20,}", parts.query)
    for secret in (account.number, person.display_name, st["owner"].email, "Willow"):
        assert secret not in url
    paras, _balance = letters.statement_paragraphs(st["lot"])
    block = _qr(paras)
    assert block is not None and block.modules == qr.matrix(url)
    caption = "\n".join(block.caption)
    assert "Scan to pay online. You'll sign in first." in caption and url in caption


def test_qr_matrix_round_trips_through_the_pdf(st, online):
    """The squares drawn in the PDF rebuild exactly the encoder's symbol for
    the address, and the QR drawing is numbers and operators only."""
    url = fees.statement_pay_url(st["lot"])
    expected = qr.matrix(url)
    assert expected == tuple(
        tuple(bool(v) for v in row) for row in segno.make(url, error="m", micro=False).matrix
    )
    data = letters.statement_pdf(st["lot"])
    drawing = re.search(rb"q\n0 0 0 rg\n(.*?)\nf\nQ", data, re.S).group(1).decode()
    rects = [line.split() for line in drawing.splitlines()]
    assert all(re.fullmatch(r"[\d.]+ [\d.]+ [\d.]+ [\d.]+ re", " ".join(r)) for r in rects)
    x0 = min(float(r[0]) for r in rects)
    y0 = max(float(r[1]) for r in rects)
    n = len(expected)
    rebuilt = [[False] * n for _ in range(n)]
    for x, y, w, _h, _op in rects:
        row, col = round((y0 - float(y)) / 2), round((float(x) - x0) / 2)
        for c in range(col, col + round(float(w) / 2)):
            rebuilt[row][c] = True

    assert tuple(tuple(r) for r in rebuilt) == expected


def test_no_qr_code_or_online_wording_when_online_payments_are_off(st, settings):
    paras, _balance = letters.statement_paragraphs(st["lot"])  # feature off by default
    assert _qr(paras) is None
    text = _text(paras)
    assert "Pay online" not in text and "surcharge" not in text and "sign in" not in text
    assert "By check:" in text


def test_no_qr_code_without_stripe_keys_or_a_site_address(st, online, settings):
    settings.PAYMENTS_PROVIDER = "none"  # Stripe not set up
    assert fees.statement_pay_url(st["lot"]) == ""
    assert _qr(letters.statement_paragraphs(st["lot"])[0]) is None
    settings.PAYMENTS_PROVIDER = "stripe"
    settings.PORTAL_BASE_URL = ""
    assert fees.statement_pay_url(st["lot"]) == ""
    assert _qr(letters.statement_paragraphs(st["lot"])[0]) is None
    settings.PORTAL_BASE_URL = BASE
    FeatureSetting.objects.filter(key="online_payments").update(enabled=False)
    assert fees.statement_pay_url(st["lot"]) == ""


def test_the_fee_line_reads_the_configured_surcharge(st, online):
    caption = "\n".join(_qr(letters.statement_paragraphs(st["lot"])[0]).caption)
    assert "Bank transfer or debit card: no fee. Credit card: 3% surcharge." in caption
    community.set_rule(
        key="payments.credit_card_surcharge_bp", value="250", citation="Res.", actor=SYSTEM
    )
    caption = "\n".join(_qr(letters.statement_paragraphs(st["lot"])[0]).caption)
    assert "Credit card: 2.5% surcharge." in caption
    community.set_rule(
        key="payments.credit_card_surcharge_bp", value="0", citation="Res.", actor=SYSTEM
    )
    caption = "\n".join(_qr(letters.statement_paragraphs(st["lot"])[0]).caption)
    assert "No fee for any online payment." in caption and "surcharge" not in caption


def test_delinquency_notices_carry_the_qr_code_on_the_attached_statement(st, online):
    _charge(st, 4500, TODAY - datetime.timedelta(days=40))
    url = fees.statement_pay_url(st["lot"]).encode()
    for kind in (LetterKind.FIRST, LetterKind.SECOND, LetterKind.FINAL):
        data = letters.notice_pdf(st["lot"], kind)
        assert data.startswith(b"%PDF") and b"/Count 2" in data
        assert url in data and b"0 0 0 rg" in data and b"Amount due: $45.00" in data


def test_qr_block_accepts_only_a_square_matrix_of_booleans():
    good = qr.matrix("https://portal.example.com/my-account/pay/?home=1")
    pdf.QrBlock(good)
    bad = [
        tuple(tuple(int(v) for v in row) for row in good),  # 0/1, not booleans
        tuple(tuple("x" for _v in row) for row in good),
        good[:-1],  # not square
        tuple(row[:-1] for row in good),
        (),
        [list(row) for row in good],  # lists
    ]
    for modules in bad:
        with pytest.raises(ValueError):
            pdf.QrBlock(modules)
    with pytest.raises(ValueError):
        pdf.QrBlock(good, caption=("ok", 5))
    with pytest.raises(TypeError):
        pdf.render([("qr", "not a block")])
    with pytest.raises(TypeError):
        pdf.render([("normal", pdf.QrBlock(good))])


# --- The page behind the QR code: the reader's own home only ---------------------------------


def test_pay_start_opens_the_readers_own_home(client, st, online):
    url = fees.statement_pay_url(st["lot"])
    path = url.removeprefix(BASE)
    # Not signed in: the login wall, and back here afterwards.
    response = client.get(path)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))
    # The owner of the home goes straight to its ways to pay, every charge included.
    client.force_login(st["owner"])
    response = client.get(path)
    assert response["Location"] == reverse("pay_online", args=[st["lot"].pk]) + "?all=1"
    # Another owner who scans this statement lands on their own home, never this one.
    client.force_login(st["owner_b"])
    response = client.get(path)
    assert response["Location"] == reverse("pay_online", args=[st["other"].pk]) + "?all=1"
    assert client.get(reverse("pay_online", args=[st["lot"].pk]) + "?all=1").status_code == 404
    # Junk hints are ignored.
    for hint in ("abc", "-1", "²", "9" * 30, f"{st['lot'].pk}&home=1"):
        response = client.get(reverse("pay_start"), {"home": hint})
        assert response["Location"] == reverse("pay_online", args=[st["other"].pk]) + "?all=1"


def test_pay_start_with_several_homes_or_none(client, st, online):
    third = LotFactory(street_address="16 Willow Lane")
    community.record_ownership(
        person=Person.objects.unscoped("t").get(user=st["owner"]),
        lot=third,
        start_date=TODAY - datetime.timedelta(days=30),
        actor=SYSTEM,
    )
    client.force_login(st["owner"])
    for lot in (st["lot"], third):
        response = client.get(reverse("pay_start"), {"home": lot.pk})
        assert response["Location"] == reverse("pay_online", args=[lot.pk]) + "?all=1"
    # A hint for someone else's home: My account lists their own homes.
    response = client.get(reverse("pay_start"), {"home": st["other"].pk})
    assert response["Location"] == reverse("my_account")
    # Nobody to pay for (board member without a home here): My account.
    client.force_login(st["board"])
    response = client.get(reverse("pay_start"), {"home": st["lot"].pk})
    assert response["Location"] == reverse("my_account")


def test_pay_start_is_404_when_online_payments_are_off(client, st):
    client.force_login(st["owner"])
    assert client.get(reverse("pay_start")).status_code == 404


# --- Plain words for each state ----------------------------------------------------------------


def test_due_but_not_late_says_pay_by_the_date(st):
    _charge(st, 4500, TODAY, "Pool key")
    paras, balance = letters.statement_paragraphs(st["lot"])
    text = _text(paras)
    assert balance == 4500
    assert "Amount due: $45.00" in text and f"Pay by: {_long(TODAY)}" in text
    assert "Past due" not in text and "Now" not in text
    assert "What this amount is made of" in text and "Pool key" in text
    assert "Account number" in text and "Home 12 Willow Lane" in text


def test_past_due_says_since_when_and_partly_paid_charges_say_so(st):
    old = TODAY - datetime.timedelta(days=40)
    _charge(st, 10000, old, "Gate repair share")
    _pay(st, 4000, TODAY - datetime.timedelta(days=20), "1042")
    _charge(st, 4500, TODAY, "Pool key")
    paras, balance = letters.statement_paragraphs(st["lot"])
    text = _text(paras)
    assert balance == 10500
    assert "Amount due: $105.00" in text and f"Past due since {_long(old)}." in text
    rows = [line for line in text.splitlines() if "$60.00" in line or "Pool key" in line]
    gate = next(line for line in rows if "Gate repair share" in line)
    assert "$100.00" in gate and gate.endswith("partly paid")
    pool = next(line for line in rows if "Pool key" in line)
    assert "$45.00" in pool and "partly paid" not in pool
    # The activity shows the payment as a payment, with its check number.
    payment = next(line for line in text.splitlines() if "Payment (check) #1042" in line)
    assert "$40.00" in payment and "-$40.00" not in payment
    assert "Pay by:" in text and "Past due" in text  # the slip


def test_credit_says_nothing_to_pay_and_shows_the_prepaid_months(st):
    _pay(st, 13500, TODAY - datetime.timedelta(days=2), "2001")
    paras, balance = letters.statement_paragraphs(st["lot"])
    text = _text(paras)
    assert balance == -13500
    assert "You have a credit of $135.00. Nothing to pay." in text
    assert "Amount due: $45.00" not in text and "Past due" not in text
    assert "Paid ahead" in text and text.count("covered by your credit") == 3
    assert "Nothing due" in text  # the slip


def test_part_of_a_month_left_over_from_a_prepayment_is_explained(st):
    _pay(st, 10000, TODAY - datetime.timedelta(days=2), "2002")
    text = _text(letters.statement_paragraphs(st["lot"])[0])
    assert text.count("covered by your credit") == 2
    assert "The remaining $10.00 of your credit goes toward the dues due" in text


def test_dues_posted_ahead_and_paid_by_the_prepayment_say_so(st):
    _pay(st, 9000, TODAY - datetime.timedelta(days=2), "2003")
    _charge(st, 4500, TODAY + datetime.timedelta(days=10), "Dues paid ahead")
    paras, balance = letters.statement_paragraphs(st["lot"])
    text = _text(paras)
    assert balance == -9000  # the charge is not due yet: not in the statement's balance
    line = next(t for t in text.splitlines() if t.startswith("Dues paid ahead"))
    assert "covered by your credit" in line


def test_charge_not_due_yet_is_listed_apart_from_the_amount_due(st):
    _charge(st, 2000, TODAY - datetime.timedelta(days=5), "Old fine")
    _charge(st, 3000, TODAY + datetime.timedelta(days=5), "Future fine")
    paras, balance = letters.statement_paragraphs(st["lot"])
    text = _text(paras)
    assert balance == 2000 and "Amount due: $20.00" in text
    assert "Charges not due yet (not in the amount due)" in text and "Future fine" in text


def test_nothing_to_pay(st):
    _charge(st, 4500, TODAY - datetime.timedelta(days=10), "Pool key")
    _pay(st, 4500, TODAY - datetime.timedelta(days=5), "3001")
    paras, balance = letters.statement_paragraphs(st["lot"])
    text = _text(paras)
    assert balance == 0
    assert "Nothing to pay. Thank you." in text and "What this amount is made of" not in text
    assert "Balance brought forward on" in text


def test_brought_forward_uses_the_ownership_start(st):
    since = TODAY - datetime.timedelta(days=60)
    _charge(st, 1500, since - datetime.timedelta(days=5), "Earlier fine")
    _charge(st, 2500, since + datetime.timedelta(days=5), "Later fine")
    paras, balance = letters.statement_paragraphs(st["lot"], since=since)
    text = _text(paras)
    assert balance == 4000
    assert f"Balance brought forward on {since:%b} {since.day}, {since.year}" in text
    assert "From before your ownership" in text


def test_a_long_history_moves_the_activity_to_page_two_and_keeps_the_slip_on_page_one(st):
    for i in range(40):
        _charge(st, 100, TODAY - datetime.timedelta(days=200 - i), f"Fee {i}")
    paras, _balance = letters.statement_paragraphs(st["lot"])
    styles = [s for s, _t in paras]
    first_page = paras[: styles.index("pagebreak")]
    assert any(s == "cut" for s, _t in first_page)
    assert "Account activity" not in _text(first_page)
    assert "Your account activity is on the next page." in _text(first_page)
    assert "Fee 39" in _text(paras[styles.index("pagebreak") :])
    data = pdf.render(paras)
    assert data.startswith(b"%PDF")


def test_a_short_statement_is_one_page(st, online):
    _charge(st, 4500, TODAY - datetime.timedelta(days=40), "Dues")
    _pay(st, 2000, TODAY - datetime.timedelta(days=20), "4001")
    paras, _balance = letters.statement_paragraphs(st["lot"])
    assert "pagebreak" not in [s for s, _t in paras]
    assert pdf.page_count(paras, margin=letters.STATEMENT_MARGIN) == 1


def test_amounts_and_entries_are_unchanged(st):
    """Same balance as the ledger, and every entry of the account once."""
    _charge(st, 4500, TODAY - datetime.timedelta(days=40), "One")
    _pay(st, 1000, TODAY - datetime.timedelta(days=30), "5001")
    _charge(st, 999, TODAY - datetime.timedelta(days=3), "Two")
    paras, balance = letters.statement_paragraphs(st["lot"])
    assert balance == services.balance(st["lot"].pk) == 4499
    text = _text(paras)
    activity = text[text.index("Account activity") :]
    assert activity.count("One") == 1 and activity.count("Two") == 1
    assert activity.count("#5001") == 1


# --- Hostile text stays text --------------------------------------------------------------------


def test_hostile_descriptions_and_names_cannot_inject_pdf_syntax(st, online):
    _charge(st, 4500, TODAY - datetime.timedelta(days=40), "Evil) Tj (pwn) Tj ET \\ Ω漢")
    with system_access("t"):
        person = Person.objects.get(user=st["owner"])
        person.display_name = "Pat **bold** (x) ☃"
        person.save()
    paras, _balance = letters.statement_paragraphs(st["lot"])
    assert "Pat * *bold* * (x)" in _text(paras).replace("**", "")
    data = letters.statement_pdf(st["lot"])
    assert b"Evil\\) Tj \\(pwn\\) Tj ET \\\\ ??" in data
    assert b"Evil) Tj" not in data and b"(pwn)" not in data
    assert "漢".encode() not in data and "☃".encode() not in data
    # Every text operator in the file is a well-formed escaped string.
    for line in data.split(b"\n"):
        if line.endswith(b" Tj"):
            assert re.fullmatch(rb"\((?:[^()\\]|\\[()\\])*\) Tj", line), line
    notice = letters.notice_pdf(st["lot"], LetterKind.FIRST)
    assert b"Evil) Tj" not in notice
