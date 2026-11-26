"""Milestone 4: statements, delinquency notices, letter wording, resale certificate."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import CommunityProfile, Role
from community.tests.factories import LotFactory, make_member
from core import pdf
from core.tests.pdftext import pdf_text
from ledger import letters, services
from ledger.models import EntryKind, LetterKind

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    board = make_member(board=True)
    community.set_rule(
        key="dues.monthly_cents", value="4500", citation="Declaration §7.2 (demo)", actor=board
    )
    lot = LotFactory()
    owner = make_member(lots=[lot])
    services.post_charge(
        lot=lot,
        kind=EntryKind.OTHER_CHARGE,
        amount_cents=12345,
        description="Gate repair share",
        posted_on=timezone.localdate() - datetime.timedelta(days=40),
        actor=board,
    )
    return {
        "board": board,
        "lot": lot,
        "owner": owner,
        "manager": make_member(roles=[Role.MANAGER]),
    }


def test_placeholders_are_plain_substitution_never_format():
    text = "Owed {amount} {owner_names.__class__} {0} {unknown} {{amount}}"
    out = letters.fill(text, {"amount": "$1.00", "owner_names": "Pat"})
    assert out.startswith("Owed $1.00 {owner_names.__class__} {0} {unknown}")


def test_bold_markers_become_bold_font_and_are_not_printed():
    data = pdf.render([("normal", "Plain then **bold value** then plain")])
    assert b"/F2" in data and b"**" not in data


def test_first_notice_has_letter_amount_and_statement(w):
    data = letters.notice_pdf(w["lot"], LetterKind.FIRST)
    assert data.startswith(b"%PDF")
    assert b"DELINQUENT NOTICE" in data and b"$123.45" in data
    assert b"Account statement as of" in data and b"Gate repair share" in data
    assert b"CERTIFIED" not in data
    assert b"ten \\(10\\)" in data or b"ten (10)" in data


def test_second_and_final_are_certified(w):
    for kind in (LetterKind.SECOND, LetterKind.FINAL):
        assert b"CERTIFIED AND REGULAR MAIL" in letters.notice_pdf(w["lot"], kind)


def test_board_wording_is_used(w):
    services.save_letter_template(
        "first",
        subject="Friendly reminder",
        body="Hello {salutation}, you owe {amount}.",
        certified=False,
        actor=w["board"],
    )
    data = letters.notice_pdf(w["lot"], LetterKind.FIRST)
    assert b"Friendly reminder" in data and b"you owe $123.45." in data


def test_statement_running_total_and_stub(w):
    paras, balance = letters.statement_paragraphs(w["lot"])
    assert balance == 12345
    text = "\n".join(t for _s, t in paras)
    # D43 wording: brought forward, the slip's amount due, past due since the date.
    assert "Balance brought forward on" in text and "Amount due:" in text
    assert "REMITTANCE SLIP" in text and "Past due since" in text


def test_only_the_board_edits_wording_and_settings(w):
    with pytest.raises(PermissionDenied):
        services.save_letter_template(
            "first", subject="x", body="y", certified=False, actor=w["manager"]
        )
    with pytest.raises(PermissionDenied):
        community.update_letter_settings(letterhead="X", actor=w["owner"])


def test_notice_download_is_audited_and_scoped(client, w):
    case = services.open_case(lot=w["lot"], actor=w["board"])
    client.force_login(w["manager"])
    response = client.get(reverse("collection_notice_pdf", args=[case.pk, "second"]))
    assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
    assert AuditEvent.objects.filter(action="ledger.notice_generated").exists()
    client.force_login(w["owner"])
    for url in (
        reverse("collection_notice_pdf", args=[case.pk, "first"]),
        reverse("lot_notice_pdf", args=[w["lot"].pk, "first"]),
    ):
        assert client.get(url).status_code in (403, 404)


def test_auditor_sees_the_case_but_cannot_print_notices(client, w):
    case = services.open_case(lot=w["lot"], actor=w["board"])
    client.force_login(make_member(roles=[Role.AUDITOR]))
    page = client.get(reverse("collection_detail", args=[case.pk])).content.decode()
    assert reverse("collection_notice_pdf", args=[case.pk, "first"]) not in page
    response = client.get(reverse("collection_notice_pdf", args=[case.pk, "first"]))
    assert response.status_code == 403
    assert not AuditEvent.objects.filter(action="ledger.notice_generated").exists()


def test_letters_page_saves_settings_and_wording(client, w):
    client.force_login(w["board"])
    assert client.get(reverse("letters_settings")).status_code == 200
    client.post(
        reverse("letters_settings"),
        {
            "section": "settings",
            "letterhead": "Example HOA\n1 Main St",
            "remit_to": "Example HOA\nPO Box 1",
            "resale_reserves_cents": "1,250.50",
        },
    )
    profile = CommunityProfile.get()
    assert profile.letterhead.startswith("Example HOA") and profile.resale_reserves_cents == 125050
    client.post(
        reverse("letters_settings"),
        {"section": "template", "kind": "final", "subject": "Last", "body": "Pay {amount}"},
    )
    assert letters.template("final").subject == "Last"
    client.force_login(w["manager"])
    assert client.get(reverse("letters_settings")).status_code == 403


def test_resale_certificate_shows_application_data_in_bold(w):
    community.set_rule(key="resale.fee_cents", value="15000", citation="Demo", actor=w["board"])
    community.set_rule(key="resale.deadline_days", value="10", citation="Demo", actor=w["board"])
    community.update_letter_settings(
        resale_insurance_company="Example Mutual",
        resale_insurance_cents=100000000,
        actor=w["board"],
    )
    req = services.request_resale(lot=w["lot"], requester_name="Seller", actor=w["owner"])
    data = services.resale_certificate(req)
    assert b"/F2" in data  # bold font used for the filled-in data
    assert b"Example Mutual" in data and b"$1,000,000.00" in data
    assert b"$123.45" in data  # the unpaid balance
    assert b"DELINQUENT" not in data  # not the delinquency packet


def test_unused_lot_without_owners_still_renders():
    lot = LotFactory()
    assert letters.statement_pdf(lot).startswith(b"%PDF")


def test_formal_documents_show_the_address_with_the_lot_number(w):
    """D40: statements, delinquency letters and the resale certificate name
    the home as "<address> (Lot N)"."""
    lot = w["lot"]
    legal = f"{lot.street_address} (Lot {lot.number})"
    assert lot.legal_label == legal
    assert legal in pdf_text(letters.statement_pdf(lot))
    for kind in (LetterKind.FIRST, LetterKind.SECOND, LetterKind.FINAL):
        assert legal in pdf_text(letters.notice_pdf(lot, kind)), kind
    req = services.request_resale(lot=lot, requester_name="Seller", actor=w["owner"])
    text = pdf_text(services.resale_certificate(req))
    assert f"with respect to {legal}" in text and f"Account of {legal}" in text
