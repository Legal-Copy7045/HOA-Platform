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
