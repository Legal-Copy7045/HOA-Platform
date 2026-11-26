"""Milestone 3 phase 2: owner requests, SLAs, threads, follow-ups, My items."""

import datetime

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

from comms import threads
from community import services as community
from community.models import Account, Person
from community.tests.factories import LotFactory, UserFactory, days_ago, enroll_totp, make_member
from core import my_items, policy
from core.append_only import AppendOnlyError
from core.dates import add_business_days
from core.policy import SYSTEM
from core.scoping import system_access
from core.workflow import WorkflowError
from operations import services
from operations.models import OwnerRequest

pytestmark = pytest.mark.django_db


def _contact(lot, kind):
    user = UserFactory()
    person = community.record_person(display_name=f"{kind} contact", actor=SYSTEM)
    with system_access("test"):
        Person.objects.filter(pk=person.pk).update(user=user)
        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    person.refresh_from_db()
    enroll_totp(user)
    community.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


@pytest.fixture
def w():
    lot, other = LotFactory(), LotFactory()
    return {
        "lot": lot,
        "other": other,
        "owner": make_member(lots=[lot]),
        "other_owner": make_member(lots=[other]),
        "board": make_member(board=True),
        "board2": make_member(board=True),
    }


def _submit(w, user=None, kind="maintenance", subject="Broken fence"):
    return services.submit_request(
        lot=w["lot"],
        request_type=kind,
        subject=subject,
        description="Details here",
        actor=user or w["owner"],
    )


# --- Business days and SLAs ------------------------------------------------------------


def test_business_days_skip_weekends():
    friday = datetime.date(2026, 11, 27)
    assert add_business_days(friday, 1) == datetime.date(2026, 11, 30)  # Monday
    assert add_business_days(friday, 5) == datetime.date(2026, 12, 4)
    assert add_business_days(friday, 0) == friday


def test_request_gets_its_response_target_from_the_rule(w):
    community.set_rule(
        key="request.sla_days.maintenance",
        value="2",
        citation="Board resolution 2026-4",
        actor=SYSTEM,
    )
    req = _submit(w)
    assert req.respond_by == add_business_days(timezone.localdate(), 2)
    assert req.sla_citation == "Board resolution 2026-4"


def test_overdue_until_the_first_staff_reply(w):
    req = _submit(w)
    OwnerRequest.objects.unscoped("t").filter(pk=req.pk).update(respond_by=days_ago(1))
    req.refresh_from_db()
    assert req.is_overdue
    threads.post("request", req, body="We're on it", actor=w["board"])
    req.refresh_from_db()
    assert req.first_response_at is not None and not req.is_overdue


def test_an_owner_reply_does_not_stop_the_clock(w):
    req = _submit(w)
    threads.post("request", req, body="Any news?", actor=w["owner"])
    req.refresh_from_db()
    assert req.first_response_at is None


# --- Who may raise and see requests ----------------------------------------------------------


def test_owner_cannot_raise_for_another_lot(w):
    with pytest.raises(services.OperationsError):
        services.submit_request(
            lot=w["other"],
            request_type="general",
            subject="x",
            description="y",
            actor=w["owner"],
        )


def test_view_only_contacts_and_former_owners_cannot_raise(w):
    viewer = _contact(w["lot"], "view_only")
    assert not policy.can(viewer, "request.submit")
    with pytest.raises(PermissionDenied):
        _submit(w, user=viewer)


def test_tenant_raises_and_sees_only_their_own(w):
    tenant = _contact(w["lot"], "tenant")
    mine = _submit(w, user=tenant, subject="Tenant report")
    owners = _submit(w, subject="Owner report")
    visible = set(OwnerRequest.objects.for_user(tenant).values_list("pk", flat=True))
    assert visible == {mine.pk}
    assert owners.pk in set(OwnerRequest.objects.for_user(w["owner"]).values_list("pk", flat=True))


def test_authorized_contact_sees_the_lots_requests(w):
    req = _submit(w)
    contact = _contact(w["lot"], "authorized")
    assert OwnerRequest.objects.for_user(contact).filter(pk=req.pk).exists()
    assert policy.can(contact, "request.submit")


def test_buyer_does_not_see_the_sellers_requests(w):
    old = _submit(w, subject="Seller question")
    OwnerRequest.objects.unscoped("t").filter(pk=old.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=10)
    )
    buyer = make_member()
    with system_access("t"):
        buyer_person = Person.objects.get(user=buyer)
    community.transfer_lot(
        lot=w["lot"], new_owners=[buyer_person], transfer_date=days_ago(2), actor=SYSTEM
    )
    assert not OwnerRequest.objects.for_user(buyer).filter(pk=old.pk).exists()
    # The seller keeps it, read-only.
    seller = w["owner"]
    assert OwnerRequest.objects.for_user(seller).filter(pk=old.pk).exists()
    acc = threads.access("request", old, seller)
    assert acc.read and not acc.post


def test_other_owner_gets_404_on_detail_and_thread(client, w):
    req = _submit(w)
    client.force_login(w["other_owner"])
    assert client.get(reverse("request_detail", args=[req.pk])).status_code == 404
    response = client.post(reverse("thread_post", args=["request", req.pk]), {"body": "hi"})
    assert response.status_code == 404


def test_owner_raises_a_request_from_the_page(client, w, django_capture_on_commit_callbacks):
    client.force_login(w["owner"])
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("requests"),
            {
                "lot": str(w["lot"].pk),
                "request_type": "general",
                "subject": "Pool hours?",
                "description": "When does the pool open?",
            },
        )
    assert response.status_code == 302
    req = OwnerRequest.objects.for_user(w["owner"]).get()
    assert req.subject == "Pool hours?"
    # Staff are emailed a link only.
    assert mail.outbox and all("Pool hours" not in m.body for m in mail.outbox)


# --- Workflow -------------------------------------------------------------------------------


def test_board_asks_owner_then_resolves_and_owner_can_reopen(w):
    req = _submit(w)
    services.OWNER_REQUEST.apply(req, "ask_owner", actor=w["board"], note="Photo please")
    req.refresh_from_db()
    assert req.state == "waiting" and req.first_response_at
    services.OWNER_REQUEST.apply(req, "resolve", actor=w["board"], note="Fixed")
    services.OWNER_REQUEST.apply(req, "reopen", actor=w["owner"], note="Still broken")
    req.refresh_from_db()
    assert req.state == "in_progress"


def test_only_the_requester_can_withdraw(w):
    req = _submit(w)
    spouse = make_member(lots=[w["lot"]])
    with pytest.raises(WorkflowError):
        services.OWNER_REQUEST.apply(req, "withdraw", actor=spouse)
    services.OWNER_REQUEST.apply(req, "withdraw", actor=w["owner"])


def test_owner_cannot_resolve(w):
    req = _submit(w)
    with pytest.raises(PermissionDenied):
        services.OWNER_REQUEST.apply(req, "resolve", actor=w["owner"], note="done")


def test_maintenance_report_becomes_one_work_order(w):
    req = _submit(w)
    wo = services.request_to_work_order(req, actor=w["board"])
    req.refresh_from_db()
    assert req.work_order_id == wo.pk and req.state == "in_progress"
    with pytest.raises(services.OperationsError):
        services.request_to_work_order(req, actor=w["board"])
    general = _submit(w, kind="general")
    with pytest.raises(services.OperationsError):
        services.request_to_work_order(general, actor=w["board"])


def test_assignment_only_to_staff(w):
    req = _submit(w)
    with system_access("t"):
        board2 = Person.objects.get(user=w["board2"])
        owner = Person.objects.get(user=w["owner"])
    services.assign_request(req, board2, actor=w["board"])
    with pytest.raises(services.OperationsError):
        services.assign_request(req, owner, actor=w["board"])


# --- Threads ---------------------------------------------------------------------------------


def test_internal_notes_are_hidden_from_the_owner(w):
    req = _submit(w)
    threads.post("request", req, body="Vendor quote is high", internal=True, actor=w["board"])
    threads.post("request", req, body="We'll fix it Monday", actor=w["board"])
    owner_view = [m.body for m in threads.messages_for("request", req, w["owner"])]
    assert owner_view == ["We'll fix it Monday"]
    assert len(threads.messages_for("request", req, w["board2"])) == 2


def test_owner_cannot_post_internal(w):
    req = _submit(w)
    with pytest.raises(threads.ThreadError):
        threads.post("request", req, body="x", internal=True, actor=w["owner"])


def test_thread_email_carries_a_link_not_the_message(w, django_capture_on_commit_callbacks):
    req = _submit(w)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        threads.post("request", req, body="Secret repair details", actor=w["board"])
    assert [m.to for m in mail.outbox] == [[w["owner"].email]]
    assert "Secret repair details" not in mail.outbox[0].body
    assert reverse("request_detail", args=[req.pk]) in mail.outbox[0].body


def test_internal_note_emails_nobody(w, django_capture_on_commit_callbacks):
    req = _submit(w)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        threads.post("request", req, body="note", internal=True, actor=w["board"])
    assert mail.outbox == []


def test_messages_are_append_only(w):

    req = _submit(w)
    message = threads.post("request", req, body="first", actor=w["board"])
    with pytest.raises(AppendOnlyError):
        message.body = "changed"
        message.save()


def test_work_order_threads_are_staff_only(w):
    wo = services.create_work_order(title="Pump", actor=w["board"])
    with pytest.raises(Http404):
        threads.subject_for("work_order", wo.pk, w["owner"])
    threads.post("work_order", wo, body="Vendor booked", actor=w["board"])


def test_violation_thread_open_to_owner_after_notice(w):
    from compliance import services as compliance

    v = compliance.report(lot=w["lot"], category="trash", description="Bins out", actor=w["board"])
    with pytest.raises(Http404):
        threads.subject_for("violation", v.pk, w["owner"])
    compliance.VIOLATION.apply(v, "send_notice", actor=w["board"])
    v = threads.subject_for("violation", v.pk, w["owner"])
    threads.post("violation", v, body="Moved them", actor=w["owner"])


def test_recused_arc_reviewer_sees_no_internal_notes(w):
    from arc import services as arc
    from community.models import AppointedBy, Role

    req = arc.create_request(lot=w["lot"], title="Deck", description="Rear", actor=w["owner"])
    neighbor = make_member(lots=[w["other"]])
    with system_access("t"):
        person = Person.objects.get(user=neighbor)
    community.grant_role(
        person=person, role=Role.ARC_REVIEWER, appointed_by=AppointedBy.BOARD, actor=SYSTEM
    )
    reviewer = make_member(roles=[Role.ARC_REVIEWER])
    arc.ARC.apply(req, "submit", actor=w["owner"])
    arc.set_affected_lots(req, [w["other"]], actor=w["board"])
    threads.post("arc", req, body="Deliberation", internal=True, actor=reviewer)
    assert threads.messages_for("arc", req, neighbor) == []
    assert not threads.access("arc", req, neighbor).post_internal


# --- Follow-ups and My items -----------------------------------------------------------------


def test_follow_up_due_shows_in_my_items(client, w):
    from compliance import services as compliance

    v = compliance.report(lot=w["lot"], category="trash", description="Bins", actor=w["board"])
    client.force_login(w["board"])
    client.post(reverse("follow_up_set", args=["violation", v.pk]), {"on": days_ago(0).isoformat()})
    v.refresh_from_db()
    assert v.follow_up_on == days_ago(0)
    titles = [i.title for i in my_items.items_for(w["board"]) if i.area == "Violations"]
    assert any(str(v) in t for t in titles)


def test_owners_cannot_set_follow_ups(client, w):
    from compliance import services as compliance

    v = compliance.report(lot=w["lot"], category="trash", description="Bins", actor=w["board"])
    client.force_login(w["owner"])
    response = client.post(reverse("follow_up_set", args=["violation", v.pk]), {"on": "2027-01-01"})
    assert response.status_code == 403


def test_my_items_lists_unassigned_and_my_requests_with_counts(client, w):
    req = _submit(w)
    items = my_items.items_for(w["board"])
    assert any(f"#{req.pk}" in i.title for i in items if i.area == "Requests")
    with system_access("t"):
        board2 = Person.objects.get(user=w["board2"])
    services.assign_request(req, board2, actor=w["board"])
    assert not any(f"#{req.pk}" in i.title for i in my_items.items_for(w["board"]))
    assert any(f"#{req.pk}" in i.title for i in my_items.items_for(w["board2"]))
    counts = my_items.counts(my_items.items_for(w["board2"]))
    assert {"area": "Requests", "count": 1, "overdue": 0} in counts
    client.force_login(w["board2"])
    assert b"Broken fence" in client.get(reverse("my_items")).content
    assert b"My items" in client.get(reverse("board_home")).content


def test_owner_sees_requests_waiting_on_them(w):
    req = _submit(w)
    services.OWNER_REQUEST.apply(req, "ask_owner", actor=w["board"], note="Photo please")
    items = my_items.items_for(w["owner"])
    assert [i.area for i in items] == ["My home"]
    assert items[0].nav == "requests?mine=1"
    from core.dashboard import build

    assert any(
        "Photo" in t.detail or "more information" in t.detail for t in build(w["owner"])["todos"]
    )


def test_invoice_approval_is_in_my_items_for_others_only(w):
    from operations.models import ExpenseCategory

    cat = ExpenseCategory.objects.create(code="100", name="Repairs")
    vendor = services.save_vendor(name="Fixers", actor=w["board"])
    invoice = services.create_invoice(
        vendor=vendor,
        number="1",
        invoice_date=days_ago(1),
        amount_cents=1000,
        category=cat,
        actor=w["board"],
    )
    services.INVOICE.apply(invoice, "submit", actor=w["board"])
    assert not [i for i in my_items.items_for(w["board"]) if i.area == "Approvals"]
    assert [i for i in my_items.items_for(w["board2"]) if i.area == "Approvals"]


# --- R2-009: view-only contacts never act, even when they own another home -------


def _add_contact(user, lot, kind):
    with system_access("test"):
        person = Person.objects.get(user=user)
        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    community.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)


def _png(name="p.png"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\nnot really a png", content_type="image/png")


def test_owner_who_is_view_only_elsewhere_cannot_raise_there(client, w):
    viewer = w["other_owner"]  # owns "other"; view-only contact on "lot"
    _add_contact(viewer, w["lot"], "view_only")
    assert policy.can(viewer, "request.submit")  # because they own their own home
    with pytest.raises(services.OperationsError):
        _submit(w, user=viewer)
    assert [lot.pk for lot in services.request_lots(viewer)] == [w["other"].pk]
    # Their own home still works.
    services.submit_request(
        lot=w["other"], request_type="general", subject="Mine", description="y", actor=viewer
    )
    # Through the page as well.
    client.force_login(viewer)
    client.post(
        reverse("requests") + "?mine=1",
        {"lot": w["lot"].pk, "request_type": "general", "subject": "Sneaky", "description": "y"},
    )
    assert not OwnerRequest.objects.unscoped("t").filter(subject="Sneaky").exists()


def test_view_only_contact_cannot_add_request_photos(client, w):
    from documents.attachments import attachments_for

    viewer = w["other_owner"]
    req = _submit(w)
    # A request in their name on the view-only home (e.g. from an earlier role).
    OwnerRequest.objects.unscoped("t").filter(pk=req.pk).update(requested_by=viewer)
    req.refresh_from_db()
    _add_contact(viewer, w["lot"], "view_only")
    assert not services.can_add_request_photos(viewer, req)
    client.force_login(viewer)
    response = client.post(reverse("request_attach", args=[req.pk]), {"photos": [_png()]})
    assert response.status_code in (403, 404)
    assert not attachments_for(req).exists()


def test_authorized_contact_and_tenant_still_raise_for_the_lot(w):
    authorized = w["other_owner"]
    _add_contact(authorized, w["lot"], "authorized")
    assert _submit(w, user=authorized, subject="By authorized").lot_id == w["lot"].pk
    tenant = _contact(w["lot"], "tenant")
    assert _submit(w, user=tenant, subject="By tenant").lot_id == w["lot"].pk
