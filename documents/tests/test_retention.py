"""Records retention (D72): what is due by category and basis, two-person
disposal, legal holds, the stored file removed while every record stays,
the nightly scan, and audit metadata with ids and counts only."""

import datetime

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from documents import retention, services
from documents.models import (
    Document,
    DocumentDisposal,
    DocumentVersion,
    RetentionScan,
)
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def board():
    return make_member(board=True)


@pytest.fixture
def board2():
    return make_member(board=True)


def old_doc(actor, category, title, years_ago, visibility="board", lot=None):
    today = timezone.localdate()
    return services.create_document(
        title=title,
        category=category,
        visibility=visibility,
        lot=lot,
        file=pdf(f"{title}.pdf"),
        dated_on=today.replace(year=today.year - years_ago),
        actor=actor,
    )


def due_titles():
    return {(d.document.title, d.version.number) for d in retention.due()}


def test_financial_records_due_after_seven_years_by_default(board):
    old_doc(board, "financial", "2018 bank statements", 8)
    old_doc(board, "financial", "2024 bank statements", 2)
    old_doc(board, "minutes", "2010 minutes", 16)  # kept forever
    old_doc(board, "governing", "Declaration", 20)  # kept forever
    assert due_titles() == {("2018 bank statements", 1)}


def test_superseded_basis_counts_from_the_replacement(board):
    form = old_doc(board, "forms", "ARC application form", 10)
    assert due_titles() == set()  # the current version is never due by supersession
    services.add_version(form, file=pdf("new.pdf"), actor=board)
    assert due_titles() == set()  # replaced today: 3 years to go


def test_superseded_version_due(board):
    form = old_doc(board, "forms", "Pool pass form", 1)
    services.add_version(form, file=pdf("new.pdf"), actor=board)
    later = timezone.localdate() + datetime.timedelta(days=4 * 366)
    assert {(d.document.title, d.version.number) for d in retention.due(later)} == {
        ("Pool pass form", 1)
    }


def test_rule_change_applies(board):
    old_doc(board, "minutes", "2010 minutes", 16)
    community.set_rule(key="records.keep_years.minutes", value="10", citation="t", actor=SYSTEM)
    assert due_titles() == {("2010 minutes", 1)}


def test_two_person_disposal_removes_the_file_and_keeps_the_records(
    board, board2, django_capture_on_commit_callbacks
):
    doc = old_doc(board, "financial", "2015 ledger", 11)
    version = doc.current_version
    name = version.file.name
    storage = version.file.storage
    assert storage.exists(name)
    [item] = retention.preview()["ready"]
    req = retention.propose([item.version.pk], reason="Schedule, resolution 2027-03", actor=board)
    assert storage.exists(name)  # proposing removes nothing
    assert retention.preview()["ready"] == [] and len(retention.preview()["pending"]) == 1
    with pytest.raises(retention.RetentionError):
        retention.decide(req, approve=True, actor=board)  # not the proposer
    with django_capture_on_commit_callbacks(execute=True):
        assert retention.decide(req, approve=True, note="Checked", actor=board2) == 1
        assert storage.exists(name)  # only once the records are committed
    assert not storage.exists(name)
    # Metadata stays: the version row, the document, a disposal record.
    version = DocumentVersion.objects.get(pk=version.pk)
    assert version.original_filename == "2015 ledger.pdf" and version.sha256
    disposal = DocumentDisposal.objects.get(version=version)
    assert disposal.proposed_by == board and disposal.confirmed_by == board2
    assert disposal.reason == "Schedule, resolution 2027-03" and disposal.years == 7
    doc = Document.objects.unscoped("t").get(pk=doc.pk)
    assert doc.archived  # every version gone: hidden from owners
    assert services.disposed(version) and not services.downloadable(version)
    assert retention.due() == []
    with pytest.raises(AppendOnlyError):
        disposal.delete()
    with pytest.raises(retention.RetentionError):
        retention.decide(req, approve=True, actor=board2)  # decided once
    event = AuditEvent.objects.get(action="records.disposal_confirmed")
    assert event.metadata == {"request_id": req.pk, "disposed": 1, "skipped": 0}


def test_proposer_may_withdraw_and_nothing_is_removed(board):
    doc = old_doc(board, "financial", "2012 ledger", 14)
    req = retention.propose([doc.current_version_id], reason="Schedule", actor=board)
    assert retention.decide(req, approve=False, actor=board) == 0
    assert doc.current_version.file.storage.exists(doc.current_version.file.name)
    assert len(retention.preview()["ready"]) == 1  # can be proposed again


def test_legal_hold_blocks_proposal_and_confirmation(board, board2):
    doc = old_doc(board, "financial", "2016 invoices", 10)
    req = retention.propose([doc.current_version_id], reason="Schedule", actor=board)
    hold = retention.place_hold(category="financial", reason="Claim 2027 (test)", actor=board2)
    assert retention.preview()["held"] and not retention.preview()["ready"]
    assert retention.decide(req, approve=True, actor=board2) == 0  # skipped: held
    assert doc.current_version.file.storage.exists(doc.current_version.file.name)
    assert not DocumentDisposal.objects.exists()
    with pytest.raises(retention.RetentionError):
        retention.propose([doc.current_version_id], reason="Schedule", actor=board)
    retention.release_hold(hold, reason="Claim settled", actor=board)
    with pytest.raises(retention.RetentionError):
        retention.release_hold(hold, reason="again", actor=board)
    req = retention.propose([doc.current_version_id], reason="Schedule", actor=board)
    assert retention.decide(req, approve=True, actor=board2) == 1


def test_document_hold(board, board2):
    doc = old_doc(board, "financial", "2016 audit", 10)
    other = old_doc(board, "financial", "2016 receipts", 10)
    retention.place_hold(document=doc, reason="Audit (test)", actor=board)
    ready = {d.document.pk for d in retention.preview()["ready"]}
    assert ready == {other.pk}
    with pytest.raises(retention.RetentionError):
        retention.place_hold(reason="nothing chosen", actor=board)
    with pytest.raises(retention.RetentionError):
        retention.place_hold(document=doc, reason="", actor=board)


def test_only_the_board_manages_retention(board):
    doc = old_doc(board, "financial", "2016 receipts", 10)
    for user in (
        make_member(roles=[Role.MANAGER]),
        make_member(roles=[Role.AUDITOR]),
        make_member(roles=[Role.SYSADMIN]),
        make_member(lots=[LotFactory()]),
    ):
        with pytest.raises(PermissionDenied):
            retention.propose([doc.current_version_id], reason="x", actor=user)
        with pytest.raises(PermissionDenied):
            retention.place_hold(document=doc, reason="x", actor=user)


def test_nightly_scan_lists_and_tells_the_board_but_removes_nothing(board):
    doc = old_doc(board, "financial", "2014 statements", 12)
    mail.outbox.clear()
    row = retention.scan()
    assert row.due == 1 and isinstance(row, RetentionScan)
    assert len(mail.outbox) == 1 and mail.outbox[0].subject.startswith("[Board]")
    assert "2014 statements" not in mail.outbox[0].body  # titles stay behind sign-in
    assert doc.current_version.file.storage.exists(doc.current_version.file.name)
    retention.scan()
    assert len(mail.outbox) == 1  # no new email while the list did not grow
    event = AuditEvent.objects.filter(action="records.retention_scan").first()
    assert set(event.metadata) == {"due", "held", "removed"}


def test_nightly_scan_removes_a_file_a_disposal_left_behind(board, board2):
    doc = old_doc(board, "financial", "2014 statements", 12)
    version = doc.current_version
    req = retention.propose([version.pk], reason="Schedule", actor=board)
    # Recorded, but the after-commit removal never ran (as if the file store
    # was down at that moment).
    retention.decide(req, approve=True, actor=board2)
    assert version.file.storage.exists(version.file.name)
    row = retention.scan()
    assert row.files_removed == 1 and row.due == 0
    assert not version.file.storage.exists(version.file.name)


def test_disposed_file_is_not_downloadable(client, board, board2):
    doc = old_doc(board, "financial", "2013 statements", 13, visibility="owners")
    req = retention.propose([doc.current_version_id], reason="Schedule", actor=board)
    retention.decide(req, approve=True, actor=board2)
    client.force_login(board)
    response = client.get(reverse("document_download", args=[doc.pk]))
    assert response.status_code == 302
    page = client.get(reverse("document_detail", args=[doc.pk])).content.decode()
    assert "disposed of" in page.lower()
    response = client.get(reverse("document_version_download", args=[doc.pk, 1]))
    assert response.status_code == 302


def test_my_items(board, board2):
    doc = old_doc(board, "financial", "2014 statements", 12)
    assert [i.area for i in retention.my_items(board)] == ["Records retention"]
    retention.propose([doc.current_version_id], reason="Schedule", actor=board)
    assert retention.my_items(board) == []  # own proposal: waiting on someone else
    [item] = retention.my_items(board2)
    assert item.title.startswith("Confirm disposal")
    assert retention.my_items(make_member(roles=[Role.MANAGER])) == []


def test_page_and_views(client, board, board2):
    doc = old_doc(board, "financial", "2014 statements", 12)
    client.force_login(board)
    page = client.get(reverse("records_retention")).content.decode()
    assert "2014 statements" in page and "Records retention" in page
    # Missing reason: the page again with the error, nothing proposed.
    response = client.post(
        reverse("records_disposal_propose"), {"version": [doc.current_version_id]}
    )
    assert response.status_code == 200 and "Give a reason" in response.content.decode()
    response = client.post(
        reverse("records_disposal_propose"),
        {"version": [doc.current_version_id], "reason": "Schedule"},
    )
    assert response.status_code == 302
    req = retention.pending_requests().get()
    response = client.post(
        reverse("records_disposal_decide", args=[req.pk]), {"decision": "confirm"}
    )
    assert response.status_code == 200  # refused: own proposal
    assert not DocumentDisposal.objects.exists()
    client.force_login(board2)
    response = client.post(
        reverse("records_disposal_decide", args=[req.pk]), {"decision": "confirm"}
    )
    assert response.status_code == 302
    assert DocumentDisposal.objects.count() == 1
    response = client.post(
        reverse("records_hold_place"), {"category": "minutes", "reason": "Dispute (test)"}
    )
    assert response.status_code == 302
    client.force_login(make_member(roles=[Role.MANAGER]))
    assert client.get(reverse("records_retention")).status_code == 403


def test_contract_documents_count_from_the_contract_end(board):
    from documents import renewals
    from operations.models import Contract, ExpenseCategory, Vendor

    today = timezone.localdate()
    c = Contract.objects.create(
        vendor=Vendor.objects.create(name="Snow Co (test)"),
        title="Plowing",
        category=ExpenseCategory.objects.create(code="5200", name="Snow"),
        schedule="annual",
        amount_cap_cents=10_000,
        starts_on=today.replace(year=today.year - 12),
        ends_on=today.replace(year=today.year - 8),
        citation="Minutes",
        state="active",
        entered_by=board,
    )
    renewals.save_contract_terms(c, renewal="manual", file=pdf("plowing.pdf"), actor=board)
    assert {d.document.title for d in retention.due()} == {"Contract: Snow Co (test), Plowing"}
    running = Contract.objects.create(
        vendor=Vendor.objects.create(name="Pond Co (test)"),
        title="Pond care",
        category=ExpenseCategory.objects.get(code="5200"),
        schedule="annual",
        amount_cap_cents=10_000,
        starts_on=today.replace(year=today.year - 12),
        ends_on=today.replace(year=today.year + 1),
        citation="Minutes",
        state="active",
        entered_by=board,
    )
    renewals.save_contract_terms(running, renewal="manual", file=pdf("pond.pdf"), actor=board)
    assert len(retention.due()) == 1  # the running contract's document is not due
