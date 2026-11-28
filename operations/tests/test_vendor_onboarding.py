"""Vendor onboarding services (D79): tokens, encryption, requirements, the
two-approver review, conflicts, invoice gating, W-9 access and retention."""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Person, Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from operations import services, vendor_crypto
from operations import vendor_onboarding as vo
from operations.models import (
    ExpenseCategory,
    InvoicePayment,
    LinkEnd,
    OnboardingLink,
    OnboardingRound,
    OnboardingSubmission,
    RoundStatus,
    Vendor,
    VendorFile,
)
from operations.tests.vendor_helpers import (
    FAKE_W9,
    KEY,
    enable,
    fill_and_submit,
    pdf,
    send_open_link,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(settings):
    enable(settings)
    return {
        "manager": make_member(roles=[Role.MANAGER]),
        "b1": make_member(board=True),
        "b2": make_member(board=True),
        "b3": make_member(board=True),
        "owner": make_member(lots=[LotFactory()]),
    }


def invite(w, vendor_type="contractor", by="manager"):
    vendor = vo.invite_vendor(
        name="Pond Pros (demo)",
        email="vendor@example.test",
        services="Pond care",
        vendor_type=vendor_type,
        actor=w[by],
    )
    return vendor, vendor.onboarding_rounds.get()


# --- Link tokens ------------------------------------------------------------------------


def test_invite_creates_an_inactive_vendor_and_a_link_storing_only_a_keyed_hash(w):
    vendor, rnd = invite(w)
    assert vendor.active is False and not vo.is_active(vendor)
    token = send_open_link(rnd)
    link = OnboardingLink.objects.get(round=rnd)
    assert len(token) >= 43  # 32 random bytes
    assert link.token_hash and token not in link.token_hash
    assert link.token_hash == vo.token_hash(token)
    import hashlib

    assert link.token_hash != hashlib.sha256(token.encode()).hexdigest()  # keyed, not bare
    assert vo.find_open_link(token) == link
    # The token is in no database row and no audit metadata.
    assert not AuditEvent.objects.filter(metadata__icontains=token).exists()


def test_link_email_subject_carries_no_portal_data(w):
    from django.core import mail

    _, rnd = invite(w)
    send_open_link(rnd)
    message = mail.outbox[-1]
    assert message.to == ["vendor@example.test"]
    assert message.subject.startswith("Vendor information form")
    assert "Pond" not in message.subject


def test_link_expires_after_the_rule_days(w):
    from community import services as community

    community.set_rule(key="vendor.onboarding_link_days", value="3", citation="R-1", actor=w["b1"])
    _, rnd = invite(w)
    token = send_open_link(rnd)
    link = OnboardingLink.objects.get(round=rnd)
    assert link.expires_at - link.created_at == datetime.timedelta(days=3)
    OnboardingLink.objects.filter(pk=link.pk).update(expires_at=timezone.now())
    assert vo.find_open_link(token) is None


def test_resend_kills_the_old_link(w):
    _, rnd = invite(w)
    old = send_open_link(rnd)
    vo.resend_link(rnd, actor=w["manager"])
    new = send_open_link(rnd)
    assert vo.find_open_link(old) is None
    assert vo.find_open_link(new) is not None
    assert OnboardingLink.objects.filter(round=rnd, end_reason=LinkEnd.REPLACED).count() == 1


def test_submission_uses_the_link_up(w):
    _, rnd = invite(w)
    token = send_open_link(rnd)
    fill_and_submit(rnd)
    assert vo.find_open_link(token) is None
    assert OnboardingLink.objects.get(round=rnd).end_reason == LinkEnd.SUBMITTED


@pytest.mark.parametrize("token", ["", "x" * 200, "../../etc", "a b", "Z" * 43])
def test_unknown_or_malformed_tokens_find_nothing(w, token):
    assert vo.find_open_link(token) is None


def test_worker_mints_once(w):
    _, rnd = invite(w)
    link = OnboardingLink.objects.get(round=rnd)
    assert vo.send_link_email(link.pk) is True
    assert vo.send_link_email(link.pk) is False  # already minted: never a second token


def test_owners_cannot_invite_and_manager_cannot_approve(w):
    with pytest.raises(PermissionDenied):
        invite(w, by="owner")
    _, rnd = invite(w)
    fill_and_submit(rnd)
    with pytest.raises(PermissionDenied):
        vo.approve(rnd, actor=w["manager"])


# --- Encryption at rest -----------------------------------------------------------------


def test_w9_bytes_are_encrypted_at_rest_and_open_only_with_the_key(w, settings):
    _, rnd = invite(w)
    vf = vo.upload_file(rnd, "w9", pdf())
    with vf.file.open("rb") as fh:
        stored = fh.read()
    assert stored != FAKE_W9 and b"%PDF" not in stored and b"SAMPLE" not in stored
    assert vendor_crypto.decrypt(stored, vf.storage_name) == FAKE_W9
    assert vf.sha256 and vf.size == len(FAKE_W9)
    # Bound to its record: another record's name does not open it.
    with pytest.raises(vendor_crypto.VendorKeyMismatch):
        vendor_crypto.decrypt(stored, "another-name")
    settings.VENDOR_DOCUMENT_KEY = "a-different-key-" * 4
    with pytest.raises(vendor_crypto.VendorKeyMismatch):
        vendor_crypto.decrypt(stored, vf.storage_name)


def test_without_the_key_uploads_and_viewing_fail_closed(w, settings):
    _, rnd = invite(w)
    vf = vo.upload_file(rnd, "w9", pdf())
    settings.VENDOR_DOCUMENT_KEY = ""
    with pytest.raises(vo.OnboardingError, match="document key"):
        vo.upload_file(rnd, "w9", pdf())
    with pytest.raises(vo.OnboardingError, match="document key"):
        vo.open_file(vf, actor=w["b1"])
    with pytest.raises(vo.OnboardingError, match="document key"):
        invite(w)


def test_key_rotation_with_fallbacks_and_reencrypt(w, settings):
    _, rnd = invite(w)
    vf = vo.upload_file(rnd, "w9", pdf())
    settings.VENDOR_DOCUMENT_KEY = "the-new-vendor-document-key-" * 3
    settings.VENDOR_DOCUMENT_KEY_FALLBACKS = [KEY]
    with vf.file.open("rb") as fh:
        assert vendor_crypto.key_index(fh.read(), vf.storage_name) == 1
    assert vo.open_file(vf, actor=w["b1"]) == FAKE_W9
    assert vo.reencrypt_all() == 1
    settings.VENDOR_DOCUMENT_KEY_FALLBACKS = []
    vf = VendorFile.objects.get(pk=vf.pk)
    with vf.file.open("rb") as fh:
        assert vendor_crypto.key_index(fh.read(), vf.storage_name) == 0
    assert vo.open_file(vf, actor=w["b1"]) == FAKE_W9


def test_check_vendor_key_command(w, settings):
    from django.core.management import CommandError, call_command

    call_command("check_vendor_key")
    settings.VENDOR_DOCUMENT_KEY = ""
    with pytest.raises(CommandError, match="not set"):
        call_command("check_vendor_key")


# --- Uploads ----------------------------------------------------------------------------


def test_uploads_are_pdf_only_by_content(w):
    _, rnd = invite(w)
    with pytest.raises(vo.OnboardingError, match="pdf"):
        vo.upload_file(rnd, "w9", pdf("w9.png", b"\x89PNG\r\n\x1a\n" + b"0" * 50))
    with pytest.raises(vo.OnboardingError, match="do not match"):
        vo.upload_file(rnd, "w9", pdf("w9.pdf", b"MZ not a pdf at all"))
    with pytest.raises(vo.OnboardingError, match="Unknown"):
        vo.upload_file(rnd, "exe", pdf())


def test_infected_files_are_never_stored(w):
    from documents.models import ScanResult
    from documents.scanning import ScanOutcome

    _, rnd = invite(w)
    with (
        mock.patch(
            "documents.scanning.scan", return_value=ScanOutcome(ScanResult.INFECTED, "clamd")
        ),
        pytest.raises(vo.OnboardingError, match="malware"),
    ):
        vo.upload_file(rnd, "w9", pdf())
    assert not VendorFile.objects.exists()


def test_files_waiting_for_a_scan_cannot_be_opened(w, settings):
    from documents.models import ScanResult
    from documents.scanning import ScanOutcome

    _, rnd = invite(w)
    with mock.patch(
        "documents.scanning.scan", return_value=ScanOutcome(ScanResult.ERROR, "clamd", "timeout")
    ):
        vf = vo.upload_file(rnd, "w9", pdf())
    with pytest.raises(vo.OnboardingError, match="scan"):
        vo.open_file(vf, actor=w["b1"])
    settings.DOCUMENT_REQUIRE_CLEAN_SCAN = True
    with mock.patch("documents.scanning.scan", return_value=ScanOutcome(ScanResult.CLEAN, "clamd")):
        assert vo.rescan(vf.pk) == ScanResult.CLEAN
    assert vo.open_file(vf, actor=w["b1"]) == FAKE_W9


def test_vendor_files_are_append_only(w):
    _, rnd = invite(w)
    vf = vo.upload_file(rnd, "w9", pdf())
    with pytest.raises(AppendOnlyError):
        vf.save()
    with pytest.raises(AppendOnlyError):
        vf.delete()


def test_free_text_refuses_tax_ids_and_account_numbers(w):
    _, rnd = invite(w)
    for text in ("our EIN is 12-3456789", "SSN 123-45-6789", "id 123456789"):
        with pytest.raises(vo.OnboardingError, match="tax ID"):
            vo.save_answers(rnd, {"services": text})
    with pytest.raises(vo.OnboardingError, match="account"):
        vo.save_answers(rnd, {"remit_address": "routing 0210000210 acct 1234567890"})
    vo.save_answers(rnd, {"remit_address": "1 Demo Lane, Nowhere PA 19000-1234"})


# --- Requirements -------------------------------------------------------------------------


def test_required_documents_follow_the_kind_of_work(w):
    assert vo.required_items("home_improvement") == [
        "w9",
        "coi_gl",
        "coi_auto",
        "workers_comp",
        "hic_registration",
    ]
    assert vo.required_items("utility") == []
    assert vo.required_items("supplier") == ["w9"]
    # A 1099-eligible vendor always gives a W-9, whatever the table says.
    vo.set_requirement(
        vendor_type="professional", item="w9", required=False, citation="R-9", actor=w["b1"]
    )
    assert "w9" not in vo.required_items("professional", "c_corp")
    assert "w9" in vo.required_items("professional", "individual")


def test_1099_eligibility_is_data():
    assert vo.eligible_for_1099_types("c_corp", "contractor") is False
    assert vo.eligible_for_1099_types("c_corp", "attorney") is True
    assert vo.eligible_for_1099_types("individual", "contractor") is True
    assert vo.eligible_for_1099_types("individual", "supplier") is False
    assert vo.eligible_for_1099_types("", "contractor") is None


def test_requirement_changes_need_a_citation_and_board(w):
    with pytest.raises(vo.OnboardingError, match="citation"):
        vo.set_requirement(
            vendor_type="other", item="licence", required=True, citation="", actor=w["b1"]
        )
    with pytest.raises(PermissionDenied):
        vo.set_requirement(
            vendor_type="other", item="licence", required=True, citation="R", actor=w["manager"]
        )


def test_submit_refuses_until_required_items_are_there(w):
    _, rnd = invite(w, vendor_type="home_improvement")
    vo.save_answers(rnd, {"legal_name": "Demo"})
    with pytest.raises(vo.OnboardingError, match="Upload"):
        vo.submit(rnd)
    with pytest.raises(vo.OnboardingError, match="Home Improvement"):
        fill_and_submit(rnd, {"hic_registration": "nope"})
    fill_and_submit(rnd, {"hic_registration": "PA123456"})


def test_changed_terms_must_be_accepted_again(w):
    _, rnd = invite(w)
    vo.save_terms(text="New terms", actor=w["b1"])
    with pytest.raises(vo.OnboardingError, match="terms"):
        fill_and_submit(rnd)
    fill_and_submit(rnd, {"terms_version": 1})


# --- Review: two different approvers --------------------------------------------------------


def test_two_different_board_members_activate_the_vendor(w):
    vendor, rnd = invite(w)
    submission = fill_and_submit(rnd)
    assert submission.answers["legal_name"] == "Pond Pros LLC (demo)"
    assert {f["kind"] for f in submission.files} == {"w9", "coi_gl", "coi_auto", "workers_comp"}
    vo.approve(rnd, actor=w["b1"])
    assert not vo.is_active(vendor)
    with pytest.raises(vo.OnboardingError, match="different board member"):
        vo.approve(rnd, actor=w["b1"])
    rnd = vo.approve(rnd, actor=w["b2"])
    vendor.refresh_from_db()
    assert rnd.status == RoundStatus.APPROVED and vo.is_active(vendor)
    assert vendor.w9_on_file and vo.w9_on_file(vendor)
    assert vo.w9_received_on(vendor) == timezone.localdate()
    assert vo.coi_current(vendor) and vendor.insurance_expires_on == datetime.date(2030, 1, 31)
    assert not vo.coi_current(vendor, on=datetime.date(2030, 2, 1))
    assert vo.eligible_for_1099(vendor) is True
    assert vo.required_documents(vendor) == ["w9", "coi_gl", "coi_auto", "workers_comp"]


def test_the_inviter_never_approves(w):
    _, rnd = invite(w, by="b1")
    fill_and_submit(rnd)
    with pytest.raises(vo.OnboardingError, match="sent this vendor"):
        vo.approve(rnd, actor=w["b1"])


def test_return_issues_a_new_link_and_keeps_the_submission(w):
    _, rnd = invite(w)
    first = fill_and_submit(rnd)
    vo.approve(rnd, actor=w["b1"])
    vo.return_to_vendor(rnd, reason="The certificate is unreadable", actor=w["b2"])
    rnd.refresh_from_db()
    assert rnd.status == RoundStatus.RETURNED and rnd.return_reason
    token = send_open_link(rnd)
    assert vo.find_open_link(token) is not None
    second = vo.submit(rnd)
    assert second.number == 2 and OnboardingSubmission.objects.filter(pk=first.pk).exists()
    # The first submission's approval does not count for the second.
    vo.approve(rnd, actor=w["b1"])
    rnd.refresh_from_db()
    assert rnd.status == RoundStatus.SUBMITTED


def test_decline_keeps_the_vendor_inactive(w):
    vendor, rnd = invite(w)
    fill_and_submit(rnd)
    with pytest.raises(vo.OnboardingError, match="reason"):
        vo.decline(rnd, reason="", actor=w["b1"])
    vo.decline(rnd, reason="Not licensed", actor=w["b1"])
    assert not vo.is_active(vendor)
    with pytest.raises(vo.OnboardingError):
        vo.approve(rnd, actor=w["b2"])


def test_a_conflicted_board_member_cannot_approve_the_vendor_or_its_invoices(w):
    vendor, rnd = invite(w)
    fill_and_submit(rnd, {"conflict": "yes", "conflict_details": "Owned by a board member's son"})
    person_b1 = Person.objects.unscoped("t").get(user=w["b1"])
    vo.link_conflict(vendor, person_b1, note="Disclosed", actor=w["manager"])
    with pytest.raises(vo.OnboardingError, match="conflict"):
        vo.approve(rnd, actor=w["b1"])
    assert AuditEvent.objects.filter(action="policy.own_matter_refused").exists()
    vo.approve(rnd, actor=w["b2"])
    vo.approve(rnd, actor=w["b3"])
    vendor.refresh_from_db()
    assert vo.is_active(vendor)
    cat = ExpenseCategory.objects.create(code="5100", name="Pond")
    inv = services.create_invoice(
        vendor=vendor,
        number="1",
        invoice_date=timezone.localdate(),
        amount_cents=10000,
        category=cat,
        actor=w["manager"],
    )
    inv = services.INVOICE.apply(inv, "submit", actor=w["manager"])
    with pytest.raises(services.OperationsError, match="conflict"):
        services.approve(inv, actor=w["b1"])
    inv = services.approve(inv, actor=w["b2"])
    assert inv.state == "approved"
    # Another board member may end the link, with a reason; then they act again.
    vo.link_conflict(vendor, person_b1, removed=True, note="Son sold the firm", actor=w["b2"])
    assert not vo.person_conflicted(w["b1"], vendor.pk)


def test_nobody_removes_their_own_conflict_link_and_the_manager_removes_none(client, w):
    """EXP-002 (D65, D79): the conflicted board member cannot end their own
    link (service or page), nor can another conflicted member or the manager;
    another board member can, with a reason; the rows stay (append-only)."""
    from operations.onboarding_models import VendorConflict

    vendor, rnd = invite(w)
    fill_and_submit(rnd, {"conflict": "yes", "conflict_details": "Board members' firm"})
    person_b1 = Person.objects.unscoped("t").get(user=w["b1"])
    person_b3 = Person.objects.unscoped("t").get(user=w["b3"])
    vo.link_conflict(vendor, person_b1, note="Disclosed", actor=w["manager"])
    vo.link_conflict(vendor, person_b3, note="Disclosed", actor=w["b1"])
    with pytest.raises(vo.OnboardingError, match="names you"):
        vo.link_conflict(vendor, person_b1, removed=True, note="x", actor=w["b1"])
    with pytest.raises(vo.OnboardingError, match="conflict-of-interest"):
        vo.link_conflict(vendor, person_b1, removed=True, note="x", actor=w["b3"])
    assert AuditEvent.objects.filter(
        action="policy.own_matter_refused", metadata__action="conflict_remove"
    ).exists()
    with pytest.raises(PermissionDenied):
        vo.link_conflict(vendor, person_b1, removed=True, note="x", actor=w["manager"])
    with pytest.raises(vo.OnboardingError, match="why"):
        vo.link_conflict(vendor, person_b1, removed=True, actor=w["b2"])
    client.force_login(w["b1"])
    url = reverse("vendor_onboarding_action", args=[rnd.pk, "conflict_remove"])
    client.post(url, {"person": person_b1.pk, "note": "Not conflicted"})
    assert vo.person_conflicted(w["b1"], vendor.pk)
    with pytest.raises(vo.OnboardingError, match="conflict"):
        vo.approve(rnd, actor=w["b1"])
    row = vo.link_conflict(vendor, person_b1, removed=True, note="Resolved", actor=w["b2"])
    assert not vo.person_conflicted(w["b1"], vendor.pk)
    assert VendorConflict.objects.filter(vendor=vendor, person=person_b1).count() == 2
    with pytest.raises(AppendOnlyError):
        row.delete()
    event = AuditEvent.objects.filter(action="vendor.conflict_linked").latest("id")
    assert event.metadata == {"person_id": person_b1.pk, "removed": True}


# --- Invoices ---------------------------------------------------------------------------------


def _invoice(w, vendor, number="1", cents=10000):
    cat, _ = ExpenseCategory.objects.get_or_create(code="5100", defaults={"name": "Pond"})
    inv = services.create_invoice(
        vendor=vendor,
        number=number,
        invoice_date=timezone.localdate(),
        amount_cents=cents,
        category=cat,
        actor=w["manager"],
    )
    return services.INVOICE.apply(inv, "submit", actor=w["manager"])


def test_unapproved_vendors_invoices_are_not_approved_or_paid(w):
    vendor, rnd = invite(w)
    inv = _invoice(w, vendor)
    with pytest.raises(services.OperationsError, match="two board members"):
        services.approve(inv, actor=w["b1"])
    fill_and_submit(rnd)
    vo.approve(rnd, actor=w["b1"])
    vo.approve(rnd, actor=w["b2"])
    inv = services.approve(inv, actor=w["b3"])
    Vendor.objects.filter(pk=vendor.pk).update(active=False)
    with pytest.raises(services.OperationsError, match="inactive"):
        services.record_payment(
            inv, paid_on=timezone.localdate(), method="check", amount_cents=10000, actor=w["b1"]
        )


def test_older_vendors_are_grandfathered_with_a_complete_onboarding_item(w):
    from core.my_items import items_for

    vendor = Vendor.objects.create(name="Old Mowers (demo)", email="old@example.test")
    assert vo.is_grandfathered(vendor) and vo.is_active(vendor)
    inv = services.approve(_invoice(w, vendor), actor=w["b1"])
    assert inv.state == "approved"
    titles = [i.title for i in items_for(w["manager"])]
    assert "Complete onboarding for Old Mowers (demo)" in titles
    detail = next(i.detail for i in items_for(w["manager"]) if "Old Mowers" in i.title)
    assert "W-9" in detail and "insurance certificate" in detail
    # Completing it keeps it active meanwhile.
    rnd = vo.complete_onboarding(vendor, vendor_type="landscaping", actor=w["manager"])
    assert vo.is_active(vendor)
    fill_and_submit(rnd)
    vo.approve(rnd, actor=w["b1"])
    vo.approve(rnd, actor=w["b2"])
    vendor.refresh_from_db()
    assert vo.w9_on_file(vendor)
    assert "Complete onboarding for Old Mowers (demo)" not in [
        i.title for i in items_for(w["manager"])
    ]


def test_update_link_asks_only_for_the_named_items(w):
    vendor, rnd = invite(w)
    fill_and_submit(rnd)
    vo.approve(rnd, actor=w["b1"])
    vo.approve(rnd, actor=w["b2"])
    upd = vo.request_update(vendor, items=["coi_gl"], actor=w["manager"])
    assert vo.round_items(upd) == ["coi_gl"] and vo.optional_items(upd) == []
    with pytest.raises(vo.OnboardingError, match="isn't asked"):
        vo.upload_file(upd, "w9", pdf())
    vo.save_answers(
        upd,
        {
            "coi_gl_carrier": "New Mutual",
            "coi_gl_amount_cents": 200000000,
            "coi_gl_expires": "2031-05-01",
        },
    )
    vo.upload_file(upd, "coi_gl", pdf("coi.pdf"))
    vo.submit(upd)
    assert vo.is_active(vendor)  # stays active during the update
    vo.approve(upd, actor=w["b1"])
    vo.approve(upd, actor=w["b2"])
    vendor.refresh_from_db()
    assert vo.coi_expires_on(vendor) == datetime.date(2031, 5, 1)


def test_board_my_items_ask_for_review(w):
    from core.my_items import items_for

    _, rnd = invite(w)
    fill_and_submit(rnd)
    titles = [i.title for i in items_for(w["b1"])]
    assert "Review vendor onboarding: Pond Pros (demo)" in titles
    assert "Review vendor onboarding: Pond Pros (demo)" not in [
        i.title for i in items_for(w["manager"])
    ]


# --- Viewing W-9s ------------------------------------------------------------------------------


def test_w9_view_is_board_only_and_audited_with_ids(w, client):
    vendor, rnd = invite(w)
    fill_and_submit(rnd)
    w9 = VendorFile.objects.get(round=rnd, kind="w9")
    url = reverse("vendor_onboarding_file", args=[rnd.pk, w9.pk])
    client.force_login(w["manager"])
    assert client.get(url).status_code == 403
    coi = VendorFile.objects.get(round=rnd, kind="coi_gl")
    response = client.get(reverse("vendor_onboarding_file", args=[rnd.pk, coi.pk]))
    assert response.status_code == 200  # the manager reads certificates
    client.force_login(w["b1"])
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == FAKE_W9
    assert response["Content-Disposition"].startswith("attachment;")
    assert "no-store" in response["Cache-Control"]
    event = AuditEvent.objects.filter(action="vendor.w9_viewed").get()
    assert event.metadata == {"file_id": w9.pk, "scan": "unscanned"}
    assert event.actor_id == w["b1"].pk
    history = vo.access_history(vendor)
    assert next(e.action for e in history) == "vendor.w9_viewed"
    page = client.get(reverse("vendor_onboarding_vendor", args=[vendor.pk])).content.decode()
    assert "Document access history" in page
    # The owner gets nothing.
    client.force_login(w["owner"])
    assert client.get(url).status_code == 403
    # A file of another round is 404 under this round.
    _, other = invite(w)
    other_file = vo.upload_file(other, "w9", pdf())
    client.force_login(w["b1"])
    assert (
        client.get(reverse("vendor_onboarding_file", args=[rnd.pk, other_file.pk])).status_code
        == 404
    )


def test_vendor_file_summary_pdf_has_no_tin(w, client):
    vendor, rnd = invite(w)
    fill_and_submit(rnd)
    vo.approve(rnd, actor=w["b1"])
    vo.approve(rnd, actor=w["b2"])
    client.force_login(w["manager"])
    response = client.get(reverse("vendor_file_summary", args=[vendor.pk]))
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert b"000-00-0000" not in response.content
    assert b"Pond Pros LLC" in response.content


def test_no_tin_in_audit_or_answers(w):
    _, rnd = invite(w)
    fill_and_submit(rnd)
    for event in AuditEvent.objects.all():
        assert "000-00-0000" not in str(event.metadata)
    assert "000-00-0000" not in str(OnboardingRound.objects.get(pk=rnd.pk).answers)


# --- Retention -------------------------------------------------------------------------------


def test_w9_retention_listing_and_two_person_disposal(w):
    _, rnd = invite(w)
    fill_and_submit(rnd)
    w9 = VendorFile.objects.get(round=rnd, kind="w9")
    year = timezone.localdate().year
    assert vo.retention_due() == []
    # A declined vendor's W-9 (never paid) falls due 4 years after the year received.
    vo.decline(rnd, reason="No", actor=w["b1"])
    assert vo.retention_due(on=datetime.date(year + 4, 12, 31)) == []
    later = datetime.date(year + 5, 1, 1)
    due = vo.retention_due(on=later)
    assert [r["file"].pk for r in due] == [w9.pk]
    with mock.patch("operations.vendor_onboarding._today", return_value=later):
        with pytest.raises(PermissionDenied):
            vo.propose_disposal(w9, reason="Retention ended", actor=w["manager"])
        vo.propose_disposal(w9, reason="Retention ended", actor=w["b1"])
        with pytest.raises(vo.OnboardingError, match="different board member"):
            vo.confirm_disposal(w9, actor=w["b1"])
        vo.confirm_disposal(w9, actor=w["b2"])
        assert vo.retention_due() == []
    assert not w9.file.storage.exists(w9.file.name)
    assert VendorFile.objects.filter(pk=w9.pk).exists()  # the record stays
    with pytest.raises(vo.OnboardingError, match="disposed"):
        vo.open_file(w9, actor=w["b1"])


def test_payments_extend_w9_retention(w):
    vendor, rnd = invite(w)
    fill_and_submit(rnd)
    vo.approve(rnd, actor=w["b1"])
    vo.approve(rnd, actor=w["b2"])
    inv = services.approve(_invoice(w, vendor), actor=w["b3"])
    year = timezone.localdate().year
    InvoicePayment.objects.create(
        invoice=inv,
        paid_on=datetime.date(year + 2, 3, 1),
        method="check",
        amount_cents=100,
        recorded_by=w["b1"],
    )
    Vendor.objects.filter(pk=vendor.pk).update(active=False)
    assert vo.retention_due(on=datetime.date(year + 5, 1, 1)) == []
    assert len(vo.retention_due(on=datetime.date(year + 7, 1, 1))) == 1


def test_reencrypt_keeps_a_copy_until_the_new_blob_is_written_and_skips_unreadable(
    w, settings, monkeypatch
):
    """Review BUG-007 (0.44.0): the old blob is copied aside before the file
    is rewritten and removed only once the new one opens; a document no key
    opens is skipped and counted, not a stop for the run."""
    _, rnd = invite(w)
    good = vo.upload_file(rnd, "w9", pdf())
    bad = vo.upload_file(rnd, "w9", pdf())
    storage = good.file.storage
    storage.delete(bad.file.name)
    storage.save(bad.file.name, ContentFile(b"not a vendor blob"))
    settings.VENDOR_DOCUMENT_KEY = "the-new-vendor-document-key-" * 3
    settings.VENDOR_DOCUMENT_KEY_FALLBACKS = [KEY]
    saved = []
    real_save = storage.save
    monkeypatch.setattr(
        storage, "save", lambda name, content, **kw: saved.append(name) or real_save(name, content)
    )
    assert vo.reencrypt_all() == 1
    assert saved[0] == f"{good.file.name}.before-reencrypt"  # the copy is written first
    assert not storage.exists(f"{good.file.name}.before-reencrypt")  # then removed
    settings.VENDOR_DOCUMENT_KEY_FALLBACKS = []
    assert vo.open_file(VendorFile.objects.get(pk=good.pk), actor=w["b1"]) == FAKE_W9
    event = AuditEvent.objects.filter(action="vendor.files_reencrypted").latest("id")
    assert event.metadata == {"count": 1, "skipped": 1}


def test_the_portal_vendor_key_is_not_removed_while_documents_are_held(w, settings):
    """Review BUG-003 / EXP-003 (0.44.0)."""
    from core import credentials

    admin = make_member(roles=[Role.SYSADMIN])
    settings.VENDOR_DOCUMENT_KEY = ""
    credentials.make_vendor_key(admin)
    _, rnd = invite(w)
    vo.upload_file(rnd, "w9", pdf())
    assert vo.stored_document_count() == 1
    with pytest.raises(credentials.CredentialError, match="cannot be removed"):
        credentials.clear(admin, "vendor_key")
    assert credentials.get("VENDOR_DOCUMENT_KEY")
