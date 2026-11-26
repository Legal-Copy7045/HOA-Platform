"""Homeowner ARC pages after the UX review: editing a draft, the shorter form,
the draft checklist, several files at once, plain wording."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from arc import services, signoffs
from arc.models import ArcProjectType, ArcRequest, ArcRequestRevision, ArcState
from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from documents.attachments import attach, attachments_for
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db


def png(name="photo.png"):
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\nnot really a png", content_type="image/png")


@pytest.fixture
def w():
    lot, other_lot = LotFactory(street_address="12 Oak Creek Drive"), LotFactory()
    ArcProjectType.objects.update_or_create(
        name="Fence", defaults={"guidance": "Plot plan, height, material and color."}
    )
    owner = make_member(lots=[lot])
    req = services.create_request(
        lot=lot, title="Rear fence", description="4 ft black", actor=owner
    )
    return {
        "lot": lot,
        "owner": owner,
        "req": req,
        "stranger": make_member(lots=[other_lot]),
        "board": make_member(board=True),
        "reviewer": make_member(roles=[Role.ARC_REVIEWER]),
    }


# --- Editing a draft -------------------------------------------------------------------


def test_owner_edits_draft_and_earlier_wording_is_kept(w):
    req = services.edit_request(
        w["req"], actor=w["owner"], title="Rear fence, 5 ft", description="5 ft black aluminum"
    )
    assert (req.title, req.description) == ("Rear fence, 5 ft", "5 ft black aluminum")
    revision = ArcRequestRevision.objects.get(request=req)
    assert (revision.title, revision.description) == ("Rear fence", "4 ft black")
    assert revision.edited_by == w["owner"] and not revision.was_submitted
    event = AuditEvent.objects.filter(action="arc.edited").get()
    assert event.metadata == {"fields": "title description"}
    with pytest.raises(AppendOnlyError):
        revision.delete()
    with pytest.raises(AppendOnlyError):
        ArcRequestRevision.objects.filter(pk=revision.pk).update(title="changed")


def test_no_change_records_nothing(w):
    services.edit_request(w["req"], actor=w["owner"], title="Rear fence")
    assert not ArcRequestRevision.objects.exists()
    assert not AuditEvent.objects.filter(action="arc.edited").exists()


def test_only_an_owner_can_edit_and_only_a_draft(w):
    with pytest.raises(services.ArcError):
        services.edit_request(w["req"], actor=w["stranger"], title="Hijacked")
    services.ARC.apply(w["req"], "submit", actor=w["owner"])
    with pytest.raises(services.ArcError, match="Only a draft"):
        services.edit_request(w["req"], actor=w["owner"], title="Too late")
    w["req"].refresh_from_db()
    assert w["req"].title == "Rear fence"


def test_returned_request_keeps_the_submitted_wording(w):
    req = w["req"]
    services.ARC.apply(req, "submit", actor=w["owner"])
    manager = make_member(roles=[Role.MANAGER])
    services.ARC.apply(req, "return_incomplete", actor=manager, note="Add the height")
    req.refresh_from_db()
    services.edit_request(req, actor=w["owner"], description="4 ft black, 48 in high")
    assert ArcRequestRevision.objects.get(request=req).was_submitted


def test_edit_cannot_touch_other_fields(w):
    with pytest.raises(services.ArcError):
        services.edit_request(w["req"], actor=w["owner"], state=ArcState.APPROVED)


def test_edit_page_owner_only(client, w):
    url = reverse("arc_edit", args=[w["req"].pk])
    client.force_login(w["stranger"])
    assert client.get(url).status_code == 404  # another owner's request is out of scope
    assert client.post(url, {"title": "x"}).status_code == 404
    client.force_login(w["reviewer"])  # sees every request, but is not the owner
    assert client.get(url).status_code == 403
    client.force_login(w["owner"])
    page = client.get(url)
    assert page.status_code == 200 and b"Rear fence" in page.content
    response = client.post(
        url,
        {
            "project_type": "Fence",
            "title": "Rear fence, 5 ft",
            "description": "5 ft",
            "planned_start": "2027-04-01",
            "location": "rear",
            "location_detail": "behind the shed",
        },
    )
    assert response.status_code == 302
    w["req"].refresh_from_db()
    assert w["req"].title == "Rear fence, 5 ft"
    assert w["req"].location_detail == ""  # only kept for "Other"


def test_edit_page_invalid_keeps_input(client, w):
    client.force_login(w["owner"])
    response = client.post(
        reverse("arc_edit", args=[w["req"].pk]),
        {"project_type": "Fence", "title": "Typed title 991", "description": ""},
    )
    assert response.status_code == 200
    assert b"Typed title 991" in response.content
    assert b"Nothing has been saved yet" in response.content
    w["req"].refresh_from_db()
    assert w["req"].title == "Rear fence"


def test_edit_page_refused_after_submit(client, w):
    services.ARC.apply(w["req"], "submit", actor=w["owner"])
    client.force_login(w["owner"])
    assert client.get(reverse("arc_edit", args=[w["req"].pk])).status_code == 403


# --- The list and the new-request form --------------------------------------------------


def test_owner_list_has_the_form_first_and_no_filters(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("arc_list")).content.decode()
    assert "Ask for approval of a project" in page
    assert page.index('id="new-request"') < page.index("<table")
    assert "Filter the list" not in page
    # Kind of project first, with its guidance; US spelling.
    assert page.index('name="project_type"') < page.index('name="title"')
    assert "Plot plan, height, material and color." in page
    assert "Where on your lot" in page and 'data-show-if="location=other"' in page
    assert "colour" not in page and "aluminium" not in page
    client.force_login(w["reviewer"])
    assert "Filter the list" in client.get(reverse("arc_list")).content.decode()


def test_new_request_invalid_keeps_input_and_opens_the_form(client, w):
    client.force_login(w["owner"])
    response = client.post(
        reverse("arc_list") + "?mine=1",
        {"lot": w["lot"].pk, "title": "Typed shed 552", "description": "8x10"},
    )
    page = response.content.decode()
    assert response.status_code == 200
    assert "Typed shed 552" in page and "Nothing has been saved yet" in page
    assert '<details class="panel" id="new-request" open' in page


def test_other_location_detail_only_kept_for_other(w):
    req = services.create_request(
        lot=w["lot"],
        title="Shed",
        description="8x10",
        location="rear",
        location_detail="by the trees",
        actor=w["owner"],
    )
    assert req.location_detail == ""
    req = services.create_request(
        lot=w["lot"],
        title="Arbor",
        description="Cedar",
        location="other",
        location_detail="by the trees",
        actor=w["owner"],
    )
    assert req.location_detail == "by the trees"


# --- The draft checklist ----------------------------------------------------------------


def test_draft_checklist_with_submit_inside_and_no_note_box(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("arc_detail", args=[w["req"].pk])).content.decode()
    assert "Get your request ready" in page
    for step in ("Describe the project", "Add plans or photos", "Neighbors", "Submit"):
        assert step in page
    assert "No neighbors need to be asked." in page
    assert reverse("arc_action", args=[w["req"].pk, "submit"]) in page
    assert "Note for Submit for review" not in page
    assert 'accept="image/*,application/pdf" multiple' in page
    # The breadcrumb leads back to the owner's own list.
    assert f'href="{reverse("arc_list")}?mine=1">Home improvement approvals (ARC)' in page


def test_checklist_sends_to_neighbors_when_there_are_some(client, w):
    neighbor_lot = LotFactory()
    make_member(lots=[neighbor_lot])
    signoffs.set_neighbors(w["lot"], left=neighbor_lot, actor=w["board"])
    req = services.create_request(lot=w["lot"], title="Deck", description="Cedar", actor=w["owner"])
    client.force_login(w["owner"])
    page = client.get(reverse("arc_detail", args=[req.pk])).content.decode()
    assert "Send to my neighbors" in page
    assert "This happens automatically once your neighbors have responded." in page
    assert reverse("arc_action", args=[req.pk, "submit"]) not in page


def test_board_viewer_gets_no_checklist(client, w):
    client.force_login(w["reviewer"])
    page = client.get(reverse("arc_detail", args=[w["req"].pk])).content.decode()
    assert "Get your request ready" not in page
    assert "Architectural review</a> / Request #" in page


def test_blocked_step_is_shown_with_its_reason(client, w):
    neighbor_lot = LotFactory()
    signoffs.set_neighbors(w["lot"], left=neighbor_lot, actor=w["board"])
    req = services.create_request(lot=w["lot"], title="Deck", description="Cedar", actor=w["owner"])
    services.ARC.apply(req, "request_acknowledgements", actor=w["owner"])
    client.force_login(w["owner"])
    page = client.get(reverse("arc_detail", args=[req.pk])).content.decode()
    assert "disabled" in page
    assert "This happens when the last acknowledgement is in." in page


def test_plain_success_messages(client, w):
    client.force_login(w["owner"])
    response = client.post(reverse("arc_action", args=[w["req"].pk, "submit"]), follow=True)
    page = response.content.decode()
    assert "Sent to the committee. We&#x27;ll email you when there&#x27;s a decision." in page
    assert ": done." not in page
    # History shows the step's label, not its code name.
    assert "Submit for review" in page


def test_recusal_in_plain_words(client, w):
    services.ARC.apply(w["req"], "submit", actor=w["owner"])
    board_owner = make_member(board=True, lots=[w["lot"]])
    client.force_login(board_owner)
    page = client.get(reverse("arc_detail", args=[w["req"].pk])).content.decode()
    assert "You can't review this one because it's your lot" in page


# --- Files --------------------------------------------------------------------------------


def test_several_files_at_once_with_thumbnails(client, w):
    client.force_login(w["owner"])
    response = client.post(
        reverse("arc_attach", args=[w["req"].pk]),
        {"file": [pdf("plan.pdf"), png("yard.png"), SimpleUploadedFile("x.exe", b"MZ")]},
        follow=True,
    )
    assert attachments_for(w["req"]).count() == 2
    page = response.content.decode()
    assert "2 files added." in page
    assert "x:" in page  # the refused one is named
    image = attachments_for(w["req"]).get(content_type="image/png")
    assert f'<img src="{reverse("arc_file", args=[w["req"].pk, image.pk])}?view=thumb"' in page


def test_stranger_cannot_upload_or_read_files(client, w):
    attach(w["req"], pdf("plan.pdf"), actor=w["owner"])
    f = attachments_for(w["req"]).get()
    client.force_login(w["stranger"])
    response = client.post(
        reverse("arc_attach", args=[w["req"].pk]), {"file": [pdf("a.pdf"), pdf("b.pdf")]}
    )
    assert response.status_code == 404
    assert attachments_for(w["req"]).count() == 1
    assert client.get(reverse("arc_file", args=[w["req"].pk, f.pk])).status_code == 404


def test_too_many_files_at_once(client, w):
    client.force_login(w["owner"])
    files = [pdf(f"p{i}.pdf") for i in range(12)]
    response = client.post(reverse("arc_attach", args=[w["req"].pk]), {"file": files}, follow=True)
    assert attachments_for(w["req"]).count() == 10
    assert "at most 10 files at a time" in response.content.decode()


def test_owner_cannot_edit_another_owners_request_by_id(client, w):
    client.force_login(w["stranger"])
    client.post(reverse("arc_edit", args=[w["req"].pk]), {"title": "Changed", "description": "y"})
    assert ArcRequest.objects.unscoped("t").get(pk=w["req"].pk).title == "Rear fence"
    assert not ArcRequestRevision.objects.exists()


def test_workflow_blocked_lists_only_steps_the_actor_could_take(w):
    blocked = {t.action: reason for t, reason in services.ARC.blocked(w["req"], w["owner"])}
    assert blocked == {"request_acknowledgements": "There are no neighbors to ask."}
    # A stranger is told why; board-only steps and other states never appear.
    stranger = {t.action for t, _ in services.ARC.blocked(w["req"], w["stranger"])}
    assert stranger == {"submit", "request_acknowledgements", "withdraw"}


def test_history_uses_the_step_label(w):
    from core.templatetags.portal import step_label

    services.ARC.apply(w["req"], "submit", actor=w["owner"])
    event = services.ARC.history(w["req"]).get()
    assert step_label(event) == "Submit for review"


def test_editing_a_returned_draft_asks_the_neighbors_again(w):
    """Review R2-002."""
    from arc.models import SignoffRequest

    signoffs.add_signoff_lot(w["req"], LotFactory(), actor=w["owner"])
    SignoffRequest.objects.unscoped("t").filter(arc_request=w["req"]).update(
        state="approved", sent_at=timezone.now()
    )
    services.edit_request(w["req"], description="Now 6 ft and white", actor=w["owner"])
    rows = SignoffRequest.objects.unscoped("t").filter(arc_request=w["req"])
    assert all(r.state == "pending" and r.sent_at is None for r in rows)


def test_files_added_to_a_returned_draft_ask_the_neighbors_again(client, w):
    """Reviews BUG-001, SEC-003, R2-001: new plans count like a text edit."""
    from arc.models import SignoffRequest, SignoffState

    req = w["req"]
    signoffs.add_signoff_lot(req, LotFactory(), actor=w["owner"])
    SignoffRequest.objects.unscoped("t").filter(arc_request=req).update(
        state=SignoffState.APPROVED, sent_at=timezone.now()
    )
    services.ARC.apply(req, "submit", actor=w["owner"])
    manager = make_member(roles=[Role.MANAGER])
    services.ARC.apply(req, "return_incomplete", actor=manager, note="Add the site plan")
    req.refresh_from_db()
    assert req.state == ArcState.DRAFT

    client.force_login(w["owner"])
    client.post(reverse("arc_attach", args=[req.pk]), {"file": [pdf("site-plan.pdf")]})
    assert attachments_for(req).count() == 1
    rows = SignoffRequest.objects.unscoped("t").filter(arc_request=req)
    assert rows and all(r.state == SignoffState.PENDING and r.sent_at is None for r in rows)
    with pytest.raises(services.WorkflowError, match="acknowledgements first"):
        services.ARC.apply(req, "submit", actor=w["owner"])


def test_owner_file_service_refuses_other_owners_and_non_drafts(w):
    with pytest.raises(services.ArcError):
        services.add_owner_files(w["req"], [png()], actor=w["stranger"], request=None)
    services.ARC.apply(w["req"], "submit", actor=w["owner"])
    with pytest.raises(services.ArcError, match="draft"):
        services.add_owner_files(w["req"], [png()], actor=w["owner"], request=None)
    assert not attachments_for(w["req"]).exists()
