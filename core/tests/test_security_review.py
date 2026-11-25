"""Regression tests for the 2026-11 security review (docs/security-review-2026-11.md)."""

import io
import logging
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings

from core.logging import JsonFormatter


def test_rate_limits_key_on_the_real_client_not_the_tunnel():
    """E1: behind cloudflared every request came from one address, so one client
    could exhaust allauth's per-IP limits for everyone."""
    from accounts.adapter import AccountAdapter

    factory = RequestFactory()
    with override_settings(TRUSTED_PROXY_CIDRS=["172.30.50.10/32"]):
        from core.net import TrustedProxyMiddleware

        seen = []
        middleware = TrustedProxyMiddleware(lambda r: seen.append(r) or None)
        for client in ("198.51.100.1", "198.51.100.2"):
            request = factory.get("/", REMOTE_ADDR="172.30.50.10", HTTP_CF_CONNECTING_IP=client)
            middleware(request)
    adapter = AccountAdapter()
    assert [adapter.get_client_ip(r) for r in seen] == ["198.51.100.1", "198.51.100.2"]


def test_logs_never_carry_token_urls():
    """D1/E2: Django's request logger passes the request, whose repr has the path."""
    request = RequestFactory().get("/invite/SECRET-TOKEN/?email=someone@example.org")
    record = logging.LogRecord(
        "django.request",
        logging.ERROR,
        __file__,
        1,
        "%s: %s",
        ("Internal Server Error", "/invite/SECRET-TOKEN/"),
        None,
    )
    record.request = request
    record.status_code = 500
    line = JsonFormatter().format(record)
    assert "SECRET-TOKEN" not in line
    assert "someone@example.org" not in line
    assert '"status_code": 500' in line


def test_oversized_request_is_refused_before_it_is_read(client, settings):
    """C4: the multipart parser used to buffer the whole body first."""
    settings.DOCUMENT_MAX_BYTES = 1000
    response = client.post(
        "/healthz",
        data=b"x",
        content_type="application/octet-stream",
        CONTENT_LENGTH=str(5 * 1024 * 1024),
    )
    assert response.status_code == 413


# --- Uploads ------------------------------------------------------------------


def _docx(extra: dict[str, bytes] | None = None, rels: bytes = b"") -> SimpleUploadedFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<w:document/>")
        archive.writestr(
            "word/_rels/settings.xml.rels",
            b'<Relationships xmlns="x">' + rels + b"</Relationships>",
        )
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return SimpleUploadedFile("plan.docx", buffer.getvalue())


def test_plain_docx_is_accepted():
    from documents.validation import check_upload

    assert check_upload(_docx()).ext == "docx"


@pytest.mark.parametrize(
    "upload",
    [
        lambda: _docx(
            rels=b'<Relationship Id="r1" Type="http://x/attachedTemplate" '
            b'Target="https://evil.test/t.dotm" TargetMode="External"/>'
        ),
        lambda: _docx({"word/embeddings/oleObject1.bin": b"x"}),
        lambda: _docx({"word/activeX/activeX1.xml": b"x"}),
    ],
    ids=["remote-template", "ole-embedding", "activex"],
)
def test_office_files_that_can_run_or_fetch_code_are_refused(upload):
    """C3."""
    from documents.validation import UploadRejected, check_upload

    with pytest.raises(UploadRejected):
        check_upload(upload())


def test_text_is_checked_beyond_the_first_8kb():
    from documents.validation import UploadRejected, check_upload

    ok = "é".join(["a" * 4095] * 4).encode()  # multibyte characters across 8192
    assert check_upload(SimpleUploadedFile("notes.txt", ok)).ext == "txt"
    with pytest.raises(UploadRejected):
        check_upload(SimpleUploadedFile("notes.txt", b"a" * 9000 + b"\x00"))


@pytest.mark.parametrize(
    "name",
    [
        "Site_plan" + "_" * 137 + ".hta" + "x" * 40 + ".pdf",
        "evil.hta.pdf",
        "..\\..\\x.exe.pdf",
        "a" * 400 + ".pdf",
    ],
)
def test_stored_names_always_end_in_the_validated_extension(name):
    """C1: cutting a long name to 150 characters could leave it ending in .hta."""
    from documents.validation import safe_filename

    result = safe_filename(name, "pdf")
    assert result.endswith(".pdf")
    assert result.count(".") == 1
    assert len(result) <= 125


def test_attachments_obey_require_clean_scan(settings):
    """C2: attachments ignored DOCUMENT_REQUIRE_CLEAN_SCAN."""
    from documents.services import scan_allows_download

    settings.DOCUMENT_REQUIRE_CLEAN_SCAN = True
    assert scan_allows_download("unscanned") is False
    assert scan_allows_download("clean") is True
    settings.DOCUMENT_REQUIRE_CLEAN_SCAN = False
    assert scan_allows_download("unscanned") is True
    assert scan_allows_download("infected") is False


# --- Ownership transfer privacy (A4, A7) ----------------------------------------


@pytest.fixture
def sold_lot(db):
    from datetime import timedelta

    from django.utils import timezone

    from arc import services as arc
    from arc.models import ArcRequest
    from community import services as community
    from community.models import Person
    from community.tests.factories import LotFactory, make_member
    from compliance import services as compliance
    from compliance.models import Violation
    from core.policy import SYSTEM
    from core.scoping import system_access

    lot = LotFactory()
    board = make_member(board=True)
    seller = make_member(lots=[lot])
    old_arc = arc.create_request(lot=lot, title="Seller's deck", description="x", actor=seller)
    resolved = compliance.report(lot=lot, category="trash", description="bins", actor=board)
    compliance.VIOLATION.apply(resolved, "send_notice", actor=board)
    compliance.respond(resolved, text="SELLER-PRIVATE-REPLY", actor=seller)
    unresolved = compliance.report(lot=lot, category="trash", description="weeds", actor=board)
    compliance.VIOLATION.apply(unresolved, "send_notice", actor=board)
    a_month_ago = timezone.now() - timedelta(days=30)
    with system_access("t"):
        ArcRequest.objects.filter(pk=old_arc.pk).update(created_at=a_month_ago)
        Violation.objects.filter(pk__in=[resolved.pk, unresolved.pk]).update(created_at=a_month_ago)
        Violation.objects.filter(pk=resolved.pk).update(state="closed")
    draft = compliance.report(lot=lot, category="trash", description="draft", actor=board)
    buyer = make_member()
    with system_access("t"):
        buyer_person = Person.objects.get(user=buyer)
    community.transfer_lot(
        lot, new_owners=[buyer_person], transfer_date=timezone.localdate(), actor=SYSTEM
    )
    return {
        "lot": lot,
        "seller": seller,
        "buyer": buyer,
        "board": board,
        "old_arc": old_arc,
        "resolved": resolved,
        "unresolved": unresolved,
        "draft": draft,
    }


def test_buyer_cannot_see_sellers_arc_requests_or_closed_notices(sold_lot, client):
    from django.urls import reverse

    client.force_login(sold_lot["buyer"])
    assert client.get(reverse("arc_detail", args=[sold_lot["old_arc"].pk])).status_code == 404
    assert (
        client.get(reverse("violation_detail", args=[sold_lot["resolved"].pk])).status_code == 404
    )


def test_buyer_sees_unresolved_notice_but_not_the_sellers_reply(sold_lot, client):
    from django.urls import reverse

    client.force_login(sold_lot["buyer"])
    response = client.get(reverse("violation_detail", args=[sold_lot["unresolved"].pk]))
    assert response.status_code == 200
    assert b"SELLER-PRIVATE-REPLY" not in response.content
    assert sold_lot["board"].email.encode() not in response.content  # role, not email


def test_owner_never_sees_a_violation_before_notice(sold_lot, client):
    from django.urls import reverse

    client.force_login(sold_lot["buyer"])
    assert client.get(reverse("violation_detail", args=[sold_lot["draft"].pk])).status_code == 404


def test_board_still_sees_the_whole_history(sold_lot, client):
    from django.urls import reverse

    client.force_login(sold_lot["board"])
    assert client.get(reverse("arc_detail", args=[sold_lot["old_arc"].pk])).status_code == 200
    page = client.get(reverse("violation_detail", args=[sold_lot["resolved"].pk]))
    assert b"SELLER-PRIVATE-REPLY" in page.content


@pytest.mark.parametrize(
    "value,expected",
    [
        (" =HYPERLINK(1)", "' =HYPERLINK(1)"),
        ("\n=1+1", "'\n=1+1"),
        (chr(0xFF1D) + "1", "'" + chr(0xFF1D) + "1"),  # full-width equals
        ("=SUM(A1)", "'=SUM(A1)"),
        ("@x", "'@x"),
        ("-12.50", "-12.50"),
        ("$1,000", "$1,000"),
        ("Plain", "Plain"),
    ],
)
def test_csv_cells_cannot_start_a_formula(value, expected):
    """C5: leading whitespace and full-width signs got past the old check."""
    from reports.views import _safe_cell

    assert _safe_cell(value) == expected


def test_titles_are_single_line_for_email_subjects():
    """C6: a newline in an announcement title made every send fail."""
    from core.text import single_line

    assert single_line("Pond\nclosed" + chr(0x2028) + "today\x0b!") == "Pond closed today !"


def test_permissions_policy_header(client):
    assert "camera=()" in client.get("/healthz")["Permissions-Policy"]
