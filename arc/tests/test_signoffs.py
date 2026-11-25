"""Milestone 3 phase 7: ARC neighbor sign-off, printable form, and reading
scanned forms (Gemini replaced by a stand-in)."""

import re
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from arc import services as arc
from arc import signoffs
from arc.models import SignoffRequest, SignoffResponse
from community import services as community
from community.models import Person, Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from core.scoping import system_access
from documents.attachments import attach
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db


def _link_token(message) -> str:
    m = re.search(r"/s/([A-Za-z0-9_-]{20,100})/", message.body)
    assert m, "no single-use link in the email"
    return m.group(1)


@pytest.fixture
def w(settings):
    settings.PORTAL_BASE_URL = "https://portal.example.com"
    applicant_lot, portal_lot, paper_lot, far_lot = (LotFactory() for _ in range(4))
    board = make_member(board=True)
    applicant = make_member(lots=[applicant_lot])
    portal_neighbor = make_member(lots=[portal_lot])
    # A neighbor with no login, only an email on record.
    paper_person = community.record_person(
        display_name="Pat Paper", email="pat.paper@example.com", actor=SYSTEM
    )
    community.record_ownership(
        person=paper_person, lot=paper_lot, start_date=applicant_lot_start(), actor=SYSTEM
    )
    signoffs.set_neighbors(applicant_lot, left=portal_lot, right=paper_lot, actor=board)
    req = arc.create_request(
        lot=applicant_lot, title="Rear fence", description="4 ft aluminum", actor=applicant
    )
    attach(req, pdf("plans.pdf"), actor=applicant)
    return {
        "board": board,
        "applicant": applicant,
        "neighbor": portal_neighbor,
        "outsider": make_member(lots=[far_lot]),
        "req": req,
        "applicant_lot": applicant_lot,
        "portal_lot": portal_lot,
        "paper_lot": paper_lot,
        "far_lot": far_lot,
    }


def applicant_lot_start():
    from community.tests.factories import days_ago

    return days_ago(400)


def _send(w, django_capture_on_commit_callbacks):
    signoffs.prepare_signoffs(w["req"], actor=w["applicant"])
    # As after "Request neighbor acknowledgements": neighbors see only a
    # request that has left draft.
    arc.ArcRequest.objects.unscoped("t").filter(pk=w["req"].pk).update(state="neighbors")
    w["req"].refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        return signoffs.send_signoffs(w["req"], actor=w["applicant"])


# --- Neighbors ---------------------------------------------------------------------------


def test_neighbors_are_defined_per_lot_and_board_managed(w):
    sides = signoffs.neighbor_sides(w["applicant_lot"])
    assert sides["left"] == w["portal_lot"] and sides["right"] == w["paper_lot"]
    # Per lot: the neighbor's own list is separate (corner lots need not mirror).
    assert signoffs.neighbor_sides(w["portal_lot"])["right"] is None
    signoffs.set_neighbors(
        w["applicant_lot"],
        left=w["portal_lot"],
        right=w["paper_lot"],
        others=[w["far_lot"]],
        actor=w["board"],
    )
    assert signoffs.neighbors_of(w["applicant_lot"]) == [
        w["portal_lot"],
        w["paper_lot"],
        w["far_lot"],
    ]
    with pytest.raises(PermissionDenied):
        signoffs.set_neighbors(w["far_lot"], left=w["portal_lot"], actor=w["applicant"])


def test_neighbors_become_affected_lots_on_submit(w):
    with system_access("t"):
        SignoffRequest.objects.filter(arc_request=w["req"]).update(state="approved")
    arc.ARC.apply(w["req"], "submit", actor=w["applicant"])
    with system_access("t"):
        assert set(w["req"].affected_lots.all()) == {w["portal_lot"], w["paper_lot"]}


def test_sign_offs_and_the_form_show_neighbor_names(client, w):
    signoffs.prepare_signoffs(w["req"], actor=w["applicant"])
    rows = signoffs.summary(w["req"])["rows"]
    assert any("Pat Paper" in r.label for r in rows)
    client.force_login(w["applicant"])
    pdf_bytes = client.get(reverse("arc_signoff_form", args=[w["req"].pk])).content
    assert b"ALTERATION REQUEST FORM" in pdf_bytes
    assert b"Left neighbor" in pdf_bytes and b"Right neighbor: Pat Paper" in pdf_bytes


def test_neighbors_are_preselected_and_extra_addresses_added(w):
    # On the list from the moment the request is created.
    assert len(signoffs.summary(w["req"])["rows"]) == 2
    assert signoffs.prepare_signoffs(w["req"], actor=w["applicant"]) == 0
    signoffs.add_signoff_address(
        w["req"],
        name="Across the road",
        address="1 Other St",
        email="x@example.com",
        actor=w["applicant"],
    )
    rows = signoffs.summary(w["req"])["rows"]
    assert len(rows) == 3


def test_only_the_applicant_or_arc_staff_arrange(w):
    with pytest.raises(PermissionDenied):
        signoffs.prepare_signoffs(w["req"], actor=w["outsider"])
    signoffs.prepare_signoffs(w["req"], actor=w["board"])


# --- Sending and answering ----------------------------------------------------------------


def test_only_neighbors_with_a_login_are_emailed_and_the_link_needs_sign_in(
    client, w, django_capture_on_commit_callbacks
):
    sent = _send(w, django_capture_on_commit_callbacks)
    # The neighbor without a login answers on the printed form (D24).
    assert sent == {"portal": 1, "no_contact": 1}
    (message,) = mail.outbox
    assert message.to == [w["neighbor"].email]
    with system_access("t"):
        row = SignoffRequest.objects.get(lot=w["portal_lot"])
    assert reverse("signoff_detail", args=[row.pk]) in message.body
    assert "4 ft aluminum" not in message.body  # details only behind sign-in
    response = client.get(reverse("signoff_detail", args=[row.pk]))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_neighbor_downloads_plans_but_not_signed_forms(
    client, w, django_capture_on_commit_callbacks
):
    from documents.attachments import attachments_for

    _send(w, django_capture_on_commit_callbacks)
    signed = attach(w["req"], pdf("signed.pdf"), actor=w["board"], label="Signed sign-off form")
    with system_access("t"):
        row = SignoffRequest.objects.get(lot=w["portal_lot"])
    plans = attachments_for(w["req"]).get(original_filename="plans.pdf")
    client.force_login(w["neighbor"])
    assert client.get(reverse("signoff_file", args=[row.pk, plans.pk])).status_code == 200
    assert client.get(reverse("signoff_file", args=[row.pk, signed.pk])).status_code == 404


def test_an_old_emailed_link_sends_the_owner_to_sign_in_then_their_page(client, w):
    import secrets

    token = secrets.token_urlsafe(32)
    with system_access("t"):
        row = SignoffRequest.objects.get(lot=w["portal_lot"])
        SignoffRequest.objects.filter(pk=row.pk).update(
            token_hash=signoffs._hash(token),
            token_expires_at=__import__("django.utils.timezone").utils.timezone.now()
            + __import__("datetime").timedelta(days=3),
            sent_at=__import__("django.utils.timezone").utils.timezone.now(),
        )
    arc.ArcRequest.objects.unscoped("t").filter(pk=w["req"].pk).update(state="neighbors")
    anonymous = client.get(reverse("signoff_link", args=[token]))
    assert anonymous.status_code == 302 and "/accounts/login/" in anonymous["Location"]
    client.force_login(w["outsider"])  # forwarded to someone else: nothing
    assert client.get(reverse("signoff_link", args=[token])).status_code == 404
    client.force_login(w["neighbor"])
    response = client.get(reverse("signoff_link", args=[token]))
    assert response.status_code == 302
    assert response["Location"] == reverse("signoff_detail", args=[row.pk])


def test_portal_neighbor_answers_others_cannot(client, w, django_capture_on_commit_callbacks):
    _send(w, django_capture_on_commit_callbacks)
    with system_access("t"):
        signoff = SignoffRequest.objects.get(lot=w["portal_lot"])
    for intruder in (w["outsider"], w["applicant"]):
        client.force_login(intruder)
        assert client.get(reverse("signoff_detail", args=[signoff.pk])).status_code == 404
    with pytest.raises(PermissionDenied):
        signoffs.respond(
            signoff, position="approved", signer_name="X", channel="portal", actor=w["applicant"]
        )
    client.force_login(w["neighbor"])
    assert b"Rear fence" in client.get(reverse("signoffs_mine")).content
    client.post(
        reverse("signoff_detail", args=[signoff.pk]),
        {"position": "approved", "signer_name": "Nina Neighbor"},
    )
    signoff.refresh_from_db()
    assert signoff.state == "approved"
    response = SignoffResponse.objects.get(signoff=signoff)
    assert response.channel == "portal" and response.recorded_by == w["neighbor"]
    with pytest.raises(AppendOnlyError):
        response.delete()


def test_applicant_sees_answers_on_their_request(client, w, django_capture_on_commit_callbacks):
    _send(w, django_capture_on_commit_callbacks)
    with system_access("t"):
        signoff = SignoffRequest.objects.get(lot=w["portal_lot"])
    signoffs.respond(
        signoff, position="approved", signer_name="Nina", channel="portal", actor=w["neighbor"]
    )
    client.force_login(w["applicant"])
    page = client.get(reverse("arc_detail", args=[w["req"].pk])).content
    assert b"1 no objection" in page and b"Nina" in page


# --- Paper form and reading it ---------------------------------------------------------------


def test_printable_form_lists_each_neighbor(client, w):
    signoffs.prepare_signoffs(w["req"], actor=w["applicant"])
    client.force_login(w["applicant"])
    response = client.get(reverse("arc_signoff_form", args=[w["req"].pk]))
    assert response["Content-Type"] == "application/pdf"
    assert response.content.count(b"SO-") == 2
    client.force_login(w["outsider"])
    assert client.get(reverse("arc_signoff_form", args=[w["req"].pk])).status_code == 404


def test_staff_enter_a_paper_answer(w):
    signoffs.prepare_signoffs(w["req"], actor=w["applicant"])
    with system_access("t"):
        signoff = SignoffRequest.objects.get(lot=w["paper_lot"])
    manager = make_member(roles=[Role.MANAGER])
    signoffs.respond(
        signoff, position="approved", signer_name="Pat Paper", channel="paper", actor=manager
    )
    with pytest.raises(PermissionDenied):
        signoffs.respond(
            signoff, position="approved", signer_name="x", channel="paper", actor=w["outsider"]
        )


def _reading(w, settings, found):
    settings.GEMINI_API_KEY = "test-key"
    signoffs.prepare_signoffs(w["req"], actor=w["applicant"])
    form = attach(w["req"], pdf("signed.pdf"), actor=w["board"], label="Signed sign-off form")
    reading = signoffs.request_reading(w["req"], form.pk, actor=w["board"])
    with system_access("t"):
        refs = {s.lot_id: f"SO-{s.pk}" for s in SignoffRequest.objects.filter(arc_request=w["req"])}
    items = [dict(i, reference=refs[i.pop("lot").pk]) for i in found(w)]
    with mock.patch("core.ai.read_signed_form", return_value=items):
        signoffs.run_reading(reading)
    return reading


def test_reading_proposes_and_a_person_confirms(w, settings):
    reading = _reading(
        w,
        settings,
        lambda w: [
            {
                "lot": w["paper_lot"],
                "position": "approve",
                "signer_name": "Pat Paper",
                "signed_on": "2026-11-20",
                "confidence": 95,
            },
        ],
    )
    proposal = reading.proposals.get()
    assert proposal.state == "pending" and proposal.signoff.lot == w["paper_lot"]
    with pytest.raises(PermissionDenied):
        signoffs.confirm_proposal(proposal, actor=w["outsider"])
    response = signoffs.confirm_proposal(proposal, actor=w["board"])
    assert response.channel == "paper" and response.evidence_attachment_id == reading.attachment_id


def test_automatic_confirming_when_the_board_switches_it_on(w, settings):
    community.set_rule(
        key="arc.signoff_auto_confirm", value="true", citation="Board resolution", actor=SYSTEM
    )
    reading = _reading(
        w,
        settings,
        lambda w: [
            {
                "lot": w["paper_lot"],
                "position": "object",
                "signer_name": "Pat Paper",
                "confidence": 92,
            },
            {
                "lot": w["portal_lot"],
                "position": "approve",
                "signer_name": "Smudged",
                "confidence": 40,
            },
        ],
    )
    states = {p.signoff.lot_id: p.state for p in reading.proposals.all()}
    assert states == {w["paper_lot"].pk: "confirmed", w["portal_lot"].pk: "pending"}
    with system_access("t"):
        auto = SignoffResponse.objects.get(signoff__lot=w["paper_lot"])
    assert auto.channel == "ai" and auto.position == "objected"


def test_no_key_means_enter_by_hand(w, settings):
    settings.GEMINI_API_KEY = ""
    form = attach(w["req"], pdf("signed.pdf"), actor=w["board"], label="Signed sign-off form")
    with pytest.raises(signoffs.SignoffError):
        signoffs.request_reading(w["req"], form.pk, actor=w["board"])


def test_recused_reviewer_cannot_handle_sign_offs(w):
    reviewer = make_member(lots=[LotFactory()], roles=[Role.ARC_REVIEWER])
    with system_access("t"):
        person = Person.objects.get(user=reviewer)
        SignoffRequest.objects.filter(arc_request=w["req"]).update(state="approved")
    arc.ARC.apply(w["req"], "submit", actor=w["applicant"])
    community.record_ownership(
        person=person, lot=w["portal_lot"], start_date=applicant_lot_start(), actor=SYSTEM
    )
    arc.set_affected_lots(w["req"], [w["portal_lot"]], actor=w["board"])
    assert not signoffs.can_arrange(reviewer, w["req"])


# --- Acknowledgements before board review (Milestone 4) -----------------------------------


def _req_state(w):
    w["req"].refresh_from_db()
    return w["req"].state


def test_plan_neighbors_are_locked_for_the_homeowner_but_the_board_can_remove(w):
    with system_access("t"):
        rows = {r.lot_id: r for r in SignoffRequest.objects.filter(arc_request=w["req"])}
    left = rows[w["portal_lot"].pk]
    assert left.locked
    with pytest.raises(signoffs.SignoffError):
        signoffs.withdraw_signoff(left, actor=w["applicant"])
    extra = signoffs.add_signoff_lot(w["req"], w["far_lot"], actor=w["applicant"])
    assert not extra.locked
    signoffs.withdraw_signoff(extra, actor=w["applicant"])  # their own addition
    signoffs.withdraw_signoff(left, actor=w["board"])
    left.refresh_from_db()
    assert left.state == "withdrawn"


def test_board_review_waits_for_every_acknowledgement(
    client, w, django_capture_on_commit_callbacks
):
    with pytest.raises(arc.WorkflowError):
        arc.ARC.apply(w["req"], "submit", actor=w["applicant"])
    labels = [t.label for t in arc.ARC.available(w["req"], w["applicant"])]
    assert "Request neighbor acknowledgements" in labels and "Submit for review" not in labels
    with django_capture_on_commit_callbacks(execute=True):
        arc.ARC.apply(w["req"], "request_acknowledgements", actor=w["applicant"])
    assert _req_state(w) == "neighbors"
    by_to = {m.to[0]: m for m in mail.outbox}
    with system_access("t"):
        portal_row = SignoffRequest.objects.get(lot=w["portal_lot"])
    # Portal owners get a link to that very review page.
    assert reverse("signoff_detail", args=[portal_row.pk]) in by_to[w["neighbor"].email].body
    signoffs.respond(
        portal_row, position="approved", signer_name="Nina", channel="portal", actor=w["neighbor"]
    )
    assert _req_state(w) == "neighbors"
    assert "pat.paper@example.com" not in by_to  # no login: the printed form
    with system_access("t"):
        paper_row = SignoffRequest.objects.get(lot=w["paper_lot"])
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        signoffs.respond(
            paper_row,
            position="objected",
            signer_name="Pat Paper",
            comment="Too tall",
            channel="paper",
            actor=w["board"],
        )
    assert _req_state(w) == "submitted"
    assert any(m.to == [w["applicant"].email] for m in mail.outbox)
    with system_access("t"):
        assert set(w["req"].affected_lots.all()) == {w["portal_lot"], w["paper_lot"]}


def test_board_removing_the_last_waiting_neighbor_sends_it_on(
    w, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        arc.ARC.apply(w["req"], "request_acknowledgements", actor=w["applicant"])
    with system_access("t"):
        rows = list(SignoffRequest.objects.filter(arc_request=w["req"]))
    signoffs.withdraw_signoff(rows[0], actor=w["board"])
    assert _req_state(w) == "neighbors"
    signoffs.withdraw_signoff(rows[1], actor=w["board"])
    assert _req_state(w) == "submitted"


def test_no_neighbors_means_submit_directly(w):
    lonely = LotFactory()
    owner = make_member(lots=[lonely])
    req = arc.create_request(lot=lonely, title="Shed", description="8x10", actor=owner)
    labels = [t.label for t in arc.ARC.available(req, owner)]
    assert "Submit for review" in labels and "Request neighbor acknowledgements" not in labels
    arc.ARC.apply(req, "submit", actor=owner)


def test_the_automatic_step_is_not_a_button(w):
    with system_access("t"):
        SignoffRequest.objects.filter(arc_request=w["req"]).update(state="approved")
    for user in (w["applicant"], w["board"]):
        assert "acknowledged" not in [t.action for t in arc.ARC.available(w["req"], user)]


def test_an_objection_online_needs_a_comment(w, django_capture_on_commit_callbacks):
    _send(w, django_capture_on_commit_callbacks)
    with system_access("t"):
        row = SignoffRequest.objects.get(lot=w["portal_lot"])
    with pytest.raises(signoffs.SignoffError):
        signoffs.respond(
            row, position="objected", signer_name="N", channel="portal", actor=w["neighbor"]
        )
    signoffs.respond(
        row,
        position="objected",
        signer_name="N",
        comment="Shades my garden",
        channel="portal",
        actor=w["neighbor"],
    )


def test_add_a_homeowner_by_address_and_name_never_email(client, w):
    from arc.signoff_views import _lot_choices

    labels = dict(_lot_choices(w["req"]))
    far = labels[w["far_lot"].pk]
    assert far.startswith(w["far_lot"].street_address.splitlines()[0])
    assert "Pat Paper" in labels[w["paper_lot"].pk]
    assert "@" not in " ".join(labels.values())
    assert w["applicant_lot"].pk not in labels
    client.force_login(w["applicant"])
    page = client.get(reverse("arc_detail", args=[w["req"].pk])).content.decode()
    assert "Ask another homeowner" in page and "pat.paper@example.com" not in page
    client.post(
        reverse("arc_signoff_action", args=[w["req"].pk, "add_lot"]), {"lot": w["far_lot"].pk}
    )
    with system_access("t"):
        assert SignoffRequest.objects.filter(arc_request=w["req"], lot=w["far_lot"]).exists()


def test_forms_uploaded_under_the_old_label_stay_hidden_from_neighbors(
    client, w, django_capture_on_commit_callbacks
):
    _send(w, django_capture_on_commit_callbacks)
    old = attach(w["req"], pdf("old.pdf"), actor=w["board"], label="Signed sign-off form")
    with system_access("t"):
        row = SignoffRequest.objects.get(lot=w["portal_lot"])
    client.force_login(w["neighbor"])
    page = client.get(reverse("signoff_detail", args=[row.pk])).content
    assert b"old.pdf" not in page
    assert client.get(reverse("signoff_file", args=[row.pk, old.pk])).status_code == 404


# --- Regression review fixes (SEC-002, SEC-005, SEC-006, BUG-001/002/007/008/009) ----------


def test_recused_staff_cannot_record_paper_answers_on_their_own_request(w):
    from arc.models import ScanReading, SignoffProposal

    own_lot = LotFactory()
    applicant_board = make_member(board=True, lots=[own_lot])
    signoffs.set_neighbors(own_lot, left=w["far_lot"], actor=w["board"])
    req = arc.create_request(lot=own_lot, title="Shed", description="8x10", actor=applicant_board)
    with system_access("t"):
        row = SignoffRequest.objects.get(arc_request=req)
    with pytest.raises(PermissionDenied):
        signoffs.respond(
            row, position="approved", signer_name="X", channel="paper", actor=applicant_board
        )
    reading = ScanReading.objects.create(arc_request=req, attachment_id=0, requested_by=w["board"])
    proposal = SignoffProposal.objects.create(reading=reading, signoff=None, position="approved")
    with pytest.raises(PermissionDenied):
        signoffs.confirm_proposal(proposal, actor=applicant_board, retarget=row)
    proposal.refresh_from_db()
    assert proposal.signoff_id is None  # the retarget did not stick


def test_neighbors_do_not_see_a_draft_or_a_closed_request(
    client, w, django_capture_on_commit_callbacks
):
    with system_access("t"):
        row = SignoffRequest.objects.get(arc_request=w["req"], lot=w["portal_lot"])
    client.force_login(w["neighbor"])
    assert client.get(reverse("signoff_detail", args=[row.pk])).status_code == 404
    assert b"Rear fence" not in client.get(reverse("signoffs_mine")).content
    with django_capture_on_commit_callbacks(execute=True):
        arc.ARC.apply(w["req"], "request_acknowledgements", actor=w["applicant"])
    assert client.get(reverse("signoff_detail", args=[row.pk])).status_code == 200
    arc.ARC.apply(w["req"], "withdraw", actor=w["applicant"])
    assert client.get(reverse("signoff_detail", args=[row.pk])).status_code == 404


def test_reminders_at_most_daily(w, django_capture_on_commit_callbacks):
    from comms.models import DigestItem

    _send(w, django_capture_on_commit_callbacks)
    first = len(mail.outbox)
    with django_capture_on_commit_callbacks(execute=True):
        again = signoffs.send_signoffs(w["req"], actor=w["applicant"])
    assert again == {"portal": 0, "no_contact": 1} and len(mail.outbox) == first
    assert not DigestItem.objects.filter(link__contains="/s/").exists()


def test_a_neighbor_on_the_board_is_recused_before_submission(w):
    board_neighbor = make_member(board=True)
    with system_access("t"):
        bn = Person.objects.get(user=board_neighbor)
    community.record_ownership(
        person=bn, lot=w["portal_lot"], start_date=applicant_lot_start(), actor=SYSTEM
    )
    assert arc.is_recused(board_neighbor, w["req"])


def test_simultaneous_last_answers_do_not_error(w, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        arc.ARC.apply(w["req"], "request_acknowledgements", actor=w["applicant"])
    with system_access("t"):
        SignoffRequest.objects.filter(arc_request=w["req"]).update(state="approved")
    assert signoffs.submit_if_acknowledged(w["req"]) is True
    arc.ArcRequest.objects.unscoped("t").filter(pk=w["req"].pk).update(state="neighbors")
    # A second call that finds the state already moved simply returns False.
    arc.ArcRequest.objects.unscoped("t").filter(pk=w["req"].pk).update(state="submitted")
    assert signoffs.submit_if_acknowledged(w["req"]) is False
