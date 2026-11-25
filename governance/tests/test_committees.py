"""Milestone 3 phases 3-4: committees, their spaces, document folders, search,
and the directory's Board and Committees tabs."""

import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

from comms import services as comms
from comms import threads
from comms.models import Event
from community import services as community
from community.models import Person
from community.tests.factories import LotFactory, days_ago, make_member
from core import policy
from core.policy import SYSTEM
from core.scoping import system_access
from documents import services as documents
from documents.models import Document
from documents.tests.helpers import pdf
from governance import services
from governance.models import CommitteeMembership

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


@pytest.fixture
def w():
    lot_a, lot_b, lot_c = LotFactory(), LotFactory(), LotFactory()
    board = make_member(board=True)
    data = {
        "board": board,
        "board2": make_member(board=True),
        "member": make_member(lots=[lot_a]),
        "chair": make_member(lots=[lot_b]),
        "outsider": make_member(lots=[lot_c]),
        "lot_a": lot_a,
    }
    social = services.create_committee(
        name="Social", purpose="Events", citation="Board resolution 2027-06", actor=board
    )
    services.appoint_committee_member(social, person=_person(data["member"]), actor=board)
    services.appoint_committee_member(
        social, person=_person(data["chair"]), role="chair", actor=board
    )
    data["social"] = social
    return data


# --- Membership and access ----------------------------------------------------------------


def test_members_get_the_committee_role(w):
    roles = policy.roles_for(w["member"])
    assert w["social"].pk in roles.committee_ids
    assert policy.COMMITTEE_MEMBER in roles.groups
    assert w["social"].pk in policy.roles_for(w["chair"]).chair_committee_ids
    assert not policy.roles_for(w["outsider"]).committee_ids


def test_only_the_board_creates_and_appoints(w):
    with pytest.raises(PermissionDenied):
        services.create_committee(name="Landscaping", actor=w["chair"])
    with pytest.raises(PermissionDenied):
        services.appoint_committee_member(
            w["social"], person=_person(w["outsider"]), actor=w["chair"]
        )


def test_no_self_appointment(w):
    with pytest.raises(services.GovernanceError):
        services.appoint_committee_member(w["social"], person=_person(w["board"]), actor=w["board"])


def test_members_must_be_owners_or_board(w):
    nobody = community.record_person(display_name="Non-owner", actor=SYSTEM)
    with pytest.raises(services.GovernanceError):
        services.appoint_committee_member(w["social"], person=nobody, actor=w["board"])


def test_new_chair_demotes_the_old_one(w):
    services.appoint_committee_member(
        w["social"], person=_person(w["outsider"]), role="chair", actor=w["board"]
    )
    chairs = [m for m in services.committee_members(w["social"]) if m.role == "chair"]
    assert [m.person_id for m in chairs] == [_person(w["outsider"]).pk]
    assert any(
        m.person_id == _person(w["chair"]).pk for m in services.committee_members(w["social"])
    )


def test_ending_membership_removes_access(w):
    with system_access("t"):
        m = CommitteeMembership.objects.get(person=_person(w["member"]), end_date__isnull=True)
    services.end_committee_membership(m, actor=w["board"])
    # Ended today: membership is [start, end), and it started today.
    assert w["social"].pk not in policy.roles_for(w["member"]).committee_ids


def test_selling_the_lot_ends_committee_access(w):
    buyer = community.record_person(display_name="Buyer", actor=SYSTEM)
    community.transfer_lot(w["lot_a"], new_owners=[buyer], transfer_date=days_ago(0), actor=SYSTEM)
    assert not policy.roles_for(w["member"]).committee_ids


def test_outsiders_get_404_on_the_space(client, w):
    client.force_login(w["outsider"])
    assert client.get(reverse("committee_detail", args=[w["social"].pk])).status_code == 404
    assert client.get(reverse("committees")).status_code == 403


def test_member_sees_only_their_committees(client, w):
    other = services.create_committee(name="Landscaping", actor=w["board"])
    client.force_login(w["member"])
    page = client.get(reverse("committees")).content
    assert b"Social" in page and b"Landscaping" not in page
    assert client.get(reverse("committee_detail", args=[other.pk])).status_code == 404
    assert client.get(reverse("committee_detail", args=[w["social"].pk])).status_code == 200


def test_retired_committee_is_read_only(w):
    services.set_committee_active(w["social"], active=False, actor=w["board"])
    w["social"].refresh_from_db()
    with pytest.raises(PermissionDenied):
        services.start_topic(w["social"], title="x", body="y", actor=w["member"])


# --- The space -------------------------------------------------------------------------------


def test_topics_are_threads_for_members_only(client, w, django_capture_on_commit_callbacks):
    from django.core import mail

    with django_capture_on_commit_callbacks(execute=True):
        topic = services.start_topic(
            w["social"], title="Summer picnic", body="Ideas?", actor=w["member"]
        )
    # The chair is told by email (link only); the author is not.
    assert [m.to for m in mail.outbox] == [[w["chair"].email]]
    assert "Ideas?" not in mail.outbox[0].body
    threads.post("committee_topic", topic, body="Pizza", actor=w["chair"])
    assert [m.body for m in threads.messages_for("committee_topic", topic, w["member"])] == [
        "Ideas?",
        "Pizza",
    ]
    with pytest.raises(Http404):
        threads.subject_for("committee_topic", topic.pk, w["outsider"])
    client.force_login(w["outsider"])
    response = client.post(
        reverse("thread_post", args=["committee_topic", topic.pk]), {"body": "hi"}
    )
    assert response.status_code == 404
    with pytest.raises(threads.ThreadError):
        threads.post("committee_topic", topic, body="x", internal=True, actor=w["board"])


def test_members_add_and_close_their_tasks(client, w):
    from operations.models import BoardTask

    client.force_login(w["member"])
    client.post(
        reverse("committee_action", args=[w["social"].pk, "task"]), {"title": "Book the pavilion"}
    )
    task = BoardTask.objects.get(title="Book the pavilion")
    assert task.committee_id == w["social"].pk
    client.post(reverse("committee_task_close", args=[w["social"].pk, task.pk]), {"status": "done"})
    task.refresh_from_db()
    assert task.status == "done"
    client.force_login(w["outsider"])
    response = client.post(
        reverse("committee_action", args=[w["social"].pk, "task"]), {"title": "Sneaky"}
    )
    assert response.status_code == 404


def test_committee_events_are_seen_by_members_and_board_only(w):
    event = comms.create_event(
        title="Planning meeting",
        starts_at=timezone.now() + datetime.timedelta(days=3),
        committee=w["social"],
        actor=w["member"],
    )
    assert event.audience == "committee"
    assert Event.objects.for_user(w["member"]).filter(pk=event.pk).exists()
    assert Event.objects.for_user(w["board"]).filter(pk=event.pk).exists()
    assert not Event.objects.for_user(w["outsider"]).filter(pk=event.pk).exists()
    with pytest.raises(PermissionDenied):
        comms.create_event(
            title="x",
            starts_at=timezone.now(),
            committee=w["social"],
            actor=w["outsider"],
        )


def test_announcements_cannot_target_a_committee(w):
    with pytest.raises(comms.CommsError):
        comms.publish_announcement(title="x", body="y", audience="committee", actor=w["board"])


# --- Folders and search -------------------------------------------------------------------------


def test_each_committee_gets_a_private_folder(client, w):
    client.force_login(w["member"])
    client.post(
        reverse("committee_upload", args=[w["social"].pk]),
        {"title": "Picnic budget", "file": pdf("budget.pdf")},
    )
    doc = Document.objects.for_user(w["member"]).get(title="Picnic budget")
    assert doc.visibility == "committee" and doc.committee_id == w["social"].pk
    assert not Document.objects.for_user(w["outsider"]).filter(pk=doc.pk).exists()
    assert Document.objects.for_user(w["board"]).filter(pk=doc.pk).exists()
    client.force_login(w["outsider"])
    assert client.get(reverse("document_detail", args=[doc.pk])).status_code == 404
    assert client.get(reverse("document_download", args=[doc.pk])).status_code == 404
    response = client.post(
        reverse("committee_upload", args=[w["social"].pk]), {"file": pdf("x.pdf")}
    )
    assert response.status_code == 404


def test_no_public_folders_and_no_committee_docs_outside_folders(w):
    with pytest.raises(documents.DocumentError):
        documents.create_folder(name="Public", visibility="public", actor=w["board"])
    with pytest.raises(documents.DocumentError):
        documents.create_document(
            title="x", category="other", visibility="committee", file=pdf(), actor=w["board"]
        )


def test_filing_a_document_applies_the_folder_visibility(w):
    board_folder = documents.create_folder(name="Legal", visibility="board", actor=w["board"])
    doc = documents.create_document(
        title="Bylaws", category="governing", visibility="owners", file=pdf(), actor=w["board"]
    )
    assert Document.objects.for_user(w["outsider"]).filter(pk=doc.pk).exists()
    documents.move_document(doc, board_folder, actor=w["board"])
    assert not Document.objects.for_user(w["outsider"]).filter(pk=doc.pk).exists()


def test_owners_cannot_create_folders_or_file(client, w):
    client.force_login(w["outsider"])
    assert (
        client.post(reverse("folder_create"), {"name": "Mine", "visibility": "owners"}).status_code
        == 403
    )


def test_search_finds_by_title_folder_and_category(client, w):
    folder = documents.create_folder(name="Pool", visibility="owners", actor=w["board"])
    documents.create_document(
        title="Opening hours",
        category="policies",
        visibility="owners",
        file=pdf(),
        folder=folder,
        actor=w["board"],
    )
    documents.create_document(
        title="Budget 2027", category="financial", visibility="owners", file=pdf(), actor=w["board"]
    )
    client.force_login(w["outsider"])
    page = client.get(reverse("documents"), {"q": "pool"}).content
    assert b"Opening hours" in page and b"Budget 2027" not in page
    page = client.get(reverse("documents"), {"q": "financial"}).content
    assert b"Budget 2027" in page
    page = client.get(reverse("documents"), {"folder": folder.pk}).content
    assert b"Opening hours" in page and b"Budget 2027" not in page


def test_owner_cannot_open_a_board_folder(client, w):
    folder = documents.create_folder(name="Legal", visibility="board", actor=w["board"])
    client.force_login(w["outsider"])
    assert client.get(reverse("documents"), {"folder": folder.pk}).status_code == 404


# --- Directory tabs ---------------------------------------------------------------------------


def test_directory_has_board_and_committee_tabs(client, w):
    client.force_login(w["outsider"])
    board = client.get(reverse("directory"), {"tab": "board"}).content
    assert _person(w["board"]).display_name.encode() in board
    committees = client.get(reverse("directory"), {"tab": "committees"}).content
    assert b"Social" in committees and b"(chair)" in committees
    assert w["board"].email.encode() not in board
