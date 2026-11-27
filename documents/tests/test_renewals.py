"""Renewal and expiry tracking (D72): reminder timing, one email per
threshold, recording by board or manager, deciding by the board only, the
documents filed in the library, and the resale certificate's insurance."""

import datetime

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services as community
from community.models import Role
from community.tests.factories import make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from documents import renewals
from documents.models import (
    Category,
    Document,
    InsurancePolicy,
    RenewalDecision,
    RenewalReminder,
    VendorCertificate,
)
from documents.tests.helpers import pdf
from operations.models import Contract, ExpenseCategory, Vendor

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2027, 3, 1)


def days(n):
    return TODAY + datetime.timedelta(days=n)


@pytest.fixture
def board():
    return make_member(board=True)


@pytest.fixture
def manager():
    return make_member(roles=[Role.MANAGER])


def policy(actor, ends_on, **kw):
    fields = {
        "kind": "general_liability",
        "carrier": "Example Mutual (test)",
        "policy_number": "GL-1",
        "coverage_cents": 100_000_000,
        "starts_on": ends_on - datetime.timedelta(days=365),
        "ends_on": ends_on,
    }
    fields.update(kw)
    return renewals.record_policy(actor=actor, **fields)


def contract(board, ends_on, **kw):
    return Contract.objects.create(
        vendor=Vendor.objects.create(name=kw.pop("vendor", "Green Lawn (test)")),
        title=kw.pop("title", "Mowing"),
        category=ExpenseCategory.objects.get_or_create(code="5100", name="Landscaping")[0],
        schedule="annual",
        amount_cap_cents=50_000,
        starts_on=ends_on - datetime.timedelta(days=365),
        ends_on=ends_on,
        citation="Minutes 2026-01",
        state="active",
        entered_by=board,
    )


def by_key(today=TODAY):
    return {r.key: r for r in renewals.tracked(today)}


def test_policy_due_window_follows_the_rule(board):
    p = policy(board, days(60))
    r = by_key()[("policy", p.pk)]
    assert r.due and not r.expired and r.tone == "warning"
    assert not by_key(days(-1))[("policy", p.pk)].due  # 61 days out: not yet
    community.set_rule(
        key="renewals.insurance_reminder_days", value="30", citation="Res 2027-02", actor=SYSTEM
    )
    assert not by_key()[("policy", p.pk)].due
    assert by_key(days(61))[("policy", p.pk)].expired


def test_recording_the_next_term_stops_tracking_and_versions_the_document(board):
    first = policy(board, days(10), file=pdf("gl-2026.pdf"))
    assert first.document.category == Category.INSURANCE
    assert first.document.visibility == "board"
    second = renewals.record_policy(
        actor=board,
        renews=first,
        kind="d_and_o",  # ignored: a renewal keeps the kind
        carrier="Example Mutual (test)",
        coverage_cents=200_000_000,
        starts_on=days(10),
        ends_on=days(375),
        file=pdf("gl-2027.pdf"),
    )
    assert second.kind == "general_liability"
    assert second.document_id == first.document_id
    assert first.document.versions.count() == 2
    keys = by_key()
    assert ("policy", first.pk) not in keys and ("policy", second.pk) in keys
    with pytest.raises(renewals.RenewalError):
        renewals.record_policy(
            actor=board,
            renews=first,
            carrier="x",
            coverage_cents=1,
            starts_on=days(10),
            ends_on=days(400),
        )


def test_policy_validation(board):
    with pytest.raises(renewals.RenewalError):
        policy(board, days(10), starts_on=days(20))
    with pytest.raises(renewals.RenewalError):
        policy(board, days(10), coverage_cents=0)


def test_manager_records_but_never_decides(board, manager):
    p = policy(manager, days(30))
    assert p.recorded_by == manager
    vendor = Vendor.objects.create(name="Pond Co (test)")
    renewals.record_vendor_certificate(vendor, expires_on=days(20), actor=manager)
    c = contract(board, days(15))
    renewals.save_contract_terms(c, renewal="auto", notice_days=10, actor=manager)
    with pytest.raises(PermissionDenied):
        renewals.decide_contract(c, decision="renew", note="Minutes", actor=manager)
    owner = make_member()
    with pytest.raises(PermissionDenied):
        policy(owner, days(30))


def test_vendor_certificate_sets_expiry_and_is_append_only(board):
    vendor = Vendor.objects.create(name="Pond Co (test)", insurance_expires_on=days(-5))
    assert by_key()[("vendor", vendor.pk)].expired
    cert = renewals.record_vendor_certificate(
        vendor, expires_on=days(200), insurer="Example (test)", file=pdf("coi.pdf"), actor=board
    )
    vendor.refresh_from_db()
    assert vendor.insurance_expires_on == days(200)
    assert not by_key()[("vendor", vendor.pk)].due
    again = renewals.record_vendor_certificate(
        vendor, expires_on=days(560), file=pdf("coi2.pdf"), actor=board
    )
    assert again.document_id == cert.document_id
    with pytest.raises(AppendOnlyError):
        cert.save()
    with pytest.raises(AppendOnlyError):
        VendorCertificate.objects.filter(pk=cert.pk).delete()


def test_contract_notice_deadline_and_auto_renewal(board):
    c = contract(board, days(40))
    # No terms: the end itself, 30-day lead by default.
    r = by_key()[("contract", c.pk)]
    assert r.deadline == days(40) and not r.due
    renewals.save_contract_terms(
        c, renewal="auto", notice_days=30, renews_for_months=12, actor=board
    )
    r = by_key()[("contract", c.pk)]
    assert r.deadline == days(10) and r.due and r.what == "Notice to cancel due"
    # Past the end with no decision: the next term is tracked.
    later = by_key(days(41))[("contract", c.pk)]
    assert later.term_ends_on == renewals.add_months(days(40), 12)


def test_board_decision_settles_the_term(board):
    c = contract(board, days(5))
    renewals.decide_contract(c, decision="let_end", note="Minutes 2027-02, item 4", actor=board)
    r = by_key()[("contract", c.pk)]
    assert r.decision is not None and not r.due and r.status.startswith("Decided")
    assert ("contract", c.pk) not in by_key(days(6))  # over
    with pytest.raises(renewals.RenewalError):
        renewals.decide_contract(c, decision="renew", note="again", actor=board)
    with pytest.raises(renewals.RenewalError):
        renewals.decide_contract(
            contract(board, days(9), vendor="B"), decision="renew", note="", actor=board
        )
    event = AuditEvent.objects.filter(action="renewals.contract_decided").get()
    assert set(event.metadata) == {"contract_id", "term_ends_on", "decision"}
    assert RenewalDecision.objects.count() == 1


def test_reminder_emails_once_per_threshold(board, monkeypatch):
    p = policy(board, days(60))
    mail.outbox.clear()
    assert renewals.send_reminders(TODAY) == 1  # 60-day threshold
    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject.startswith("[Board]")
    assert "general liability" in mail.outbox[0].body.lower()
    assert renewals.send_reminders(TODAY) == 0  # not twice
    assert renewals.send_reminders(days(1)) == 0  # 59 days: same threshold
    assert renewals.send_reminders(days(30)) == 1  # 30 days
    assert renewals.send_reminders(days(55)) == 1  # 5 days: the 7-day threshold
    assert renewals.send_reminders(days(56)) == 0
    assert renewals.send_reminders(days(70)) == 1  # past: the day itself, once
    assert renewals.send_reminders(days(80)) == 0
    assert set(RenewalReminder.objects.values_list("threshold", flat=True)) == {60, 30, 7, 0}
    assert RenewalReminder.objects.filter(object_id=p.pk).count() == 4
    event = AuditEvent.objects.filter(action="renewals.reminders_sent").first()
    assert set(event.metadata) == {"items"}


def test_first_run_well_inside_the_window_sends_one_email(board):
    policy(board, days(3))
    assert renewals.send_reminders(TODAY) == 1
    assert list(RenewalReminder.objects.values_list("threshold", flat=True)) == [7]


def test_my_items_and_overdue_count(board, manager):
    policy(board, days(-2))
    Vendor.objects.create(name="Late Co (test)", insurance_expires_on=days(3))
    assert renewals.overdue_count(TODAY) == 1
    items = renewals.my_items(board, TODAY)
    assert len(items) == 2 and {i.area for i in items} == {"Renewals"}
    assert items[0].tone == "danger" or items[1].tone == "danger"
    assert len(renewals.my_items(manager, TODAY)) == 2
    assert renewals.my_items(make_member(), TODAY) == []
    auditor = make_member(roles=[Role.AUDITOR])
    assert renewals.my_items(auditor, TODAY) == []  # read-only: no to-dos


def test_resale_certificate_reads_the_current_policy(board):
    from community.models import CommunityProfile

    profile = CommunityProfile(resale_insurance_cents=5, resale_insurance_company="Typed (test)")
    assert renewals.resale_insurance(profile) == (5, "Typed (test)")
    today = timezone.localdate()
    policy(board, today + datetime.timedelta(days=100), coverage_cents=300_000_000)
    assert renewals.resale_insurance(profile) == (300_000_000, "Example Mutual (test)")


def test_pages(client, board, manager):
    p = policy(board, days(60))
    vendor = Vendor.objects.create(name="Pond Co (test)")
    c = contract(board, days(400))
    client.force_login(manager)
    for url in (
        reverse("renewals"),
        reverse("renewal_policy", args=[p.pk]),
        reverse("renewal_policy_new"),
        reverse("renewal_vendor", args=[vendor.pk]),
        reverse("renewal_contract", args=[c.pk]),
    ):
        assert client.get(url).status_code == 200, url
    response = client.post(
        reverse("renewal_vendor", args=[vendor.pk]),
        {"expires_on": "2030-01-01", "insurer": "Ex (test)"},
    )
    assert response.status_code == 302
    # The manager does not decide.
    response = client.post(
        reverse("renewal_contract_decide", args=[c.pk]), {"decision": "renew", "note": "x"}
    )
    assert response.status_code == 403
    client.force_login(board)
    page = client.get(reverse("renewal_contract", args=[c.pk])).content.decode()
    assert "Board decision" in page
    # Invalid decision: the page again with the error, nothing recorded.
    response = client.post(reverse("renewal_contract_decide", args=[c.pk]), {"decision": "renew"})
    assert response.status_code == 200
    assert not RenewalDecision.objects.exists()
    response = client.post(
        reverse("renewal_policy_new"),
        {
            "kind": "fidelity",
            "carrier": "Bond Co (test)",
            "coverage_cents": "250,000",
            "starts_on": "2027-01-01",
            "ends_on": "2028-01-01",
        },
    )
    assert response.status_code == 302
    assert InsurancePolicy.objects.filter(kind="fidelity", coverage_cents=25_000_000).exists()
    # Owners never see renewals.
    client.force_login(make_member())
    assert client.get(reverse("renewals")).status_code == 403


def test_audit_metadata_has_no_names(board):
    vendor = Vendor.objects.create(name="Secret Name Co (test)")
    renewals.record_vendor_certificate(vendor, expires_on=days(9), insurer="Insurer X", actor=board)
    policy(board, days(9), carrier="Carrier Y")
    for event in AuditEvent.objects.filter(action__startswith="renewals."):
        text = str(event.metadata)
        assert "Secret" not in text and "Insurer X" not in text and "Carrier Y" not in text


def test_feature_off_hides_pages_and_items(client, board):
    from core import features

    features.set_feature("renewals", False, actor=SYSTEM)
    client.force_login(board)
    assert client.get(reverse("renewals")).status_code == 404
    Vendor.objects.create(name="Late Co (test)", insurance_expires_on=timezone.localdate())
    from core.my_items import items_for

    areas = {i.area for i in items_for(board)}
    assert "Renewals" not in areas and "Vendors" in areas
    assert Document.objects.unscoped("t").count() == 0


def test_vendor_expiry_is_read_through_one_function(board, monkeypatch):
    """Vendor onboarding (D79) may supply the certificate's expiry: renewals
    reads it only through vendor_coi_expiry."""
    vendor = Vendor.objects.create(name="Seam Co (test)")
    assert ("vendor", vendor.pk) not in by_key()
    monkeypatch.setattr(renewals, "vendor_coi_expiry", lambda v: days(-1))
    assert by_key()[("vendor", vendor.pk)].expired
