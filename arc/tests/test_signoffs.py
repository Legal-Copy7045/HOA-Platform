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
        lot=applicant_lot, title="Rear fence", description="4 ft aluminium", actor=applicant
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
    assert signoffs.prepare_signoffs(w["req"], actor=w["applicant"]) == 2
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


def test_portal_users_get_a_sign_in_link_others_a_single_use_link(
    w, django_capture_on_commit_callbacks
):
    sent = _send(w, django_capture_on_commit_callbacks)
    assert sent == {"portal": 1, "link": 1, "no_contact": 0}
    by_to = {m.to[0]: m for m in mail.outbox}
    assert "/signoffs/" in by_to[w["neighbor"].email].body
    assert _link_token(by_to["pat.paper@example.com"])
    for m in mail.outbox:
        assert "4 ft aluminium" not in m.body  # details only behind the link


def test_single_use_link_shows_the_request_and_works_once(
    client, w, django_capture_on_commit_callbacks
):
    _send(w, django_capture_on_commit_callbacks)
    token = _link_token(next(m for m in mail.outbox if m.to == ["pat.paper@example.com"]))
    page = client.get(reverse("signoff_link", args=[token]))
    assert page.status_code == 200 and b"4 ft aluminium" in page.content
    assert b"plans.pdf" in page.content
    response = client.post(
        reverse("signoff_link", args=[token]),
        {"position": "objected", "signer_name": "Pat Paper", "comment": "Blocks my view"},
    )
    assert b"Thank you" in response.content
    with system_access("t"):
        signoff = SignoffRequest.objects.get(lot=w["paper_lot"])
    assert signoff.state == "objected" and signoff.token_hash == ""  # nosec B105 - asserting a cleared hash
    assert client.get(reverse("signoff_link", args=[token])).status_code == 404
    assert client.get(reverse("signoff_link", args=["x" * 43])).status_code == 404


def test_link_downloads_plans_but_not_signed_forms(client, w, django_capture_on_commit_callbacks):
    from documents.attachments import attachments_for

    _send(w, django_capture_on_commit_callbacks)
    signed = attach(w["req"], pdf("signed.pdf"), actor=w["board"], label="Signed sign-off form")
    token = _link_token(next(m for m in mail.outbox if m.to == ["pat.paper@example.com"]))
    plans = attachments_for(w["req"]).get(original_filename="plans.pdf")
    ok = client.get(reverse("signoff_link_file", args=[token, plans.pk]))
    assert ok.status_code == 200
    assert client.get(reverse("signoff_link_file", args=[token, signed.pk])).status_code == 404


def test_link_expires(client, w, django_capture_on_commit_callbacks):
    import datetime

    from django.utils import timezone

    _send(w, django_capture_on_commit_callbacks)
    token = _link_token(next(m for m in mail.outbox if m.to == ["pat.paper@example.com"]))
    SignoffRequest.objects.unscoped("t").filter(lot=w["paper_lot"]).update(
        token_expires_at=timezone.now() - datetime.timedelta(minutes=1)
    )
    assert client.get(reverse("signoff_link", args=[token])).status_code == 404


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
    assert b"1 approve" in page and b"Nina" in page


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
    arc.ARC.apply(w["req"], "submit", actor=w["applicant"])
    community.record_ownership(
        person=person, lot=w["portal_lot"], start_date=applicant_lot_start(), actor=SYSTEM
    )
    arc.set_affected_lots(w["req"], [w["portal_lot"]], actor=w["board"])
    assert not signoffs.can_arrange(reviewer, w["req"])
