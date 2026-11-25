from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from arc import services
from arc.models import ArcRequest, ArcState, Decision
from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.scoping import system_access
from core.workflow import WorkflowError
from documents.tests.helpers import pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot, neighbour_lot = LotFactory(), LotFactory()
    board = make_member(board=True)
    for key, value, cite in (
        ("arc.review_days", "45", "Declaration §9.3"),
        ("arc.appeal_days", "30", "Declaration §9.5"),
        ("arc.deemed_approval", "true", "Declaration §9.4"),
    ):
        community.set_rule(key=key, value=value, citation=cite, actor=board)
    return {
        "board": board,
        "lot": lot,
        "owner": make_member(lots=[lot]),
        "reviewer": make_member(roles=[Role.ARC_REVIEWER]),
        "neighbour_reviewer": make_member(lots=[neighbour_lot], roles=[Role.ARC_REVIEWER]),
        "neighbour_lot": neighbour_lot,
        "manager": make_member(roles=[Role.MANAGER]),
        "stranger": make_member(lots=[LotFactory()]),
    }


def submitted(w):
    req = services.create_request(
        lot=w["lot"], title="Rear fence", description="4 ft black", actor=w["owner"]
    )
    services.ARC.apply(req, "submit", actor=w["owner"])
    services.set_affected_lots(req, [w["neighbour_lot"]], actor=w["manager"])
    return services.ARC.apply(req, "accept", actor=w["manager"])


def test_full_flow_with_clock_letter_and_appeal(w):
    req = submitted(w)
    assert req.review_due_on == timezone.localdate() + timedelta(days=45)
    services.record_position(req, position="deny", comment="Too tall", actor=w["reviewer"])
    decision = services.decide(
        req, outcome=ArcState.DENIED, reasons="Exceeds height limit", actor=w["reviewer"]
    )
    assert decision.citation == "Declaration §9.3"
    req.refresh_from_db()
    assert req.appeal_due_on == timezone.localdate() + timedelta(days=30)
    letter = services.decision_letter(req, decision)
    assert letter.startswith(b"%PDF") and b"Declaration \\247" not in letter
    services.ARC.apply(req, "appeal", actor=w["owner"], note="Neighbour fences are 5 ft")
    services.decide_appeal(
        req, outcome=ArcState.APPROVED, reasons="Consistent with lots nearby", actor=w["board"]
    )
    req.refresh_from_db()
    assert req.state == ArcState.APPEAL_DECIDED
    assert [d.is_appeal for d in req.decisions.all()] == [False, True]
    with pytest.raises(AppendOnlyError):
        decision.delete()


def test_recusal_applicant_and_affected_lot_owner(w):
    req = submitted(w)
    neighbour = w["neighbour_reviewer"]
    assert services.is_recused(neighbour, req)
    assert services.deliberation(req, neighbour) is None
    with pytest.raises(services.ArcError, match="recused"):
        services.record_position(req, position="approve", actor=neighbour)
    with pytest.raises(WorkflowError, match="recused"):
        services.decide(req, outcome=ArcState.APPROVED, citation="x", actor=neighbour)
    assert services.deliberation(req, w["owner"]) is None  # the applicant never sees it
    assert services.deliberation(req, w["reviewer"]) == []


def test_only_reviewers_decide_and_managers_do_intake(w):
    req = submitted(w)
    with pytest.raises(PermissionDenied):
        services.decide(req, outcome=ArcState.APPROVED, citation="x", actor=w["manager"])
    with pytest.raises(PermissionDenied):
        services.decide(req, outcome=ArcState.APPROVED, citation="x", actor=w["board"])


def test_decision_needs_conditions_reasons_and_citation(w):
    req = submitted(w)
    with pytest.raises(services.ArcError, match="conditions"):
        services.decide(req, outcome=ArcState.APPROVED_WITH_CONDITIONS, actor=w["reviewer"])
    with pytest.raises(services.ArcError, match="reasons"):
        services.decide(req, outcome=ArcState.DENIED, actor=w["reviewer"])


def test_owner_applies_only_for_own_lot(w):
    with pytest.raises(services.ArcError):
        services.create_request(lot=w["lot"], title="x", description="y", actor=w["stranger"])


def test_deemed_approval_after_the_clock_runs_out(w):
    req = submitted(w)
    ArcRequest.objects.unscoped("t").filter(pk=req.pk).update(
        review_due_on=timezone.localdate() - timedelta(days=1)
    )
    assert services.apply_deemed_approvals() == 1
    decision = Decision.objects.get(request=req)
    assert decision.deemed and decision.citation == "Declaration §9.4"
    assert decision.decided_by is None


def test_no_deemed_approval_unless_the_rule_says_so(w):
    community.set_rule(
        key="arc.deemed_approval", value="false", citation="Declaration §9.4", actor=w["board"]
    )
    req = submitted(w)
    ArcRequest.objects.unscoped("t").filter(pk=req.pk).update(
        review_due_on=timezone.localdate() - timedelta(days=1)
    )
    assert services.apply_deemed_approvals() == 0


def test_appeal_window(w):
    req = submitted(w)
    services.decide(req, outcome=ArcState.DENIED, reasons="No", actor=w["reviewer"])
    ArcRequest.objects.unscoped("t").filter(pk=req.pk).update(
        appeal_due_on=timezone.localdate() - timedelta(days=1)
    )
    with pytest.raises(WorkflowError, match="appeal period"):
        services.ARC.apply(req, "appeal", actor=w["owner"], note="late")


def test_pages_hide_deliberation_from_applicant(client, w):
    req = submitted(w)
    services.record_position(
        req, position="deny", comment="SECRET-REVIEW-NOTE", actor=w["reviewer"]
    )
    client.force_login(w["owner"])
    page = client.get(reverse("arc_detail", args=[req.pk])).content
    assert b"SECRET-REVIEW-NOTE" not in page
    client.force_login(w["reviewer"])
    assert b"SECRET-REVIEW-NOTE" in client.get(reverse("arc_detail", args=[req.pk])).content


def test_generic_action_endpoint_cannot_skip_the_decision_form(client, w):
    req = submitted(w)
    client.force_login(w["reviewer"])
    client.post(reverse("arc_action", args=[req.pk, "approve"]))
    req.refresh_from_db()
    assert req.state == ArcState.IN_REVIEW


def test_files_are_kept_and_served_only_in_scope(client, w):
    req = services.create_request(lot=w["lot"], title="Shed", description="8x10", actor=w["owner"])
    client.force_login(w["owner"])
    client.post(reverse("arc_attach", args=[req.pk]), {"file": pdf("plan.pdf")})
    from documents.attachments import attachments_for

    f = attachments_for(req).get()
    assert client.get(reverse("arc_file", args=[req.pk, f.pk])).status_code == 200
    client.force_login(w["stranger"])
    assert client.get(reverse("arc_file", args=[req.pk, f.pk])).status_code == 404


# --- Security review 2026-11 ------------------------------------------------------


def test_recused_reviewer_cannot_clear_affected_lots_to_escape_recusal(w, client):
    """Finding A2: clearing the list un-recused the reviewer, who then decided."""
    req = submitted(w)
    neighbour = w["neighbour_reviewer"]
    with pytest.raises(services.ArcError, match="recused"):
        services.set_affected_lots(req, [], actor=neighbour)
    client.force_login(neighbour)
    client.post(reverse("arc_affected", args=[req.pk]), {"lots": ""})
    req.refresh_from_db()
    assert services.is_recused(neighbour, req)
    with pytest.raises(WorkflowError, match="recused"):
        services.decide(req, outcome=ArcState.DENIED, reasons="No", citation="x", actor=neighbour)


def test_affected_lots_can_only_be_added_once_review_starts(w):
    req = submitted(w)  # in review, neighbour lot listed
    with pytest.raises(services.ArcError, match="only be added"):
        services.set_affected_lots(req, [], actor=w["manager"])
    extra = LotFactory()
    services.set_affected_lots(req, [w["neighbour_lot"], extra], actor=w["manager"])
    with system_access("t"):
        listed = set(req.affected_lots.values_list("pk", flat=True))
    assert listed == {w["neighbour_lot"].pk, extra.pk}


def test_affected_lots_frozen_after_decision(w):
    req = submitted(w)
    services.decide(req, outcome=ArcState.APPROVED, citation="x", actor=w["reviewer"])
    req.refresh_from_db()
    with pytest.raises(services.ArcError, match="only while the request is open"):
        services.set_affected_lots(req, [LotFactory()], actor=w["manager"])


def test_board_member_applicant_cannot_complete_own_request(w):
    """Finding A5."""
    lot = LotFactory()
    board_owner = make_member(board=True, lots=[lot])
    req = services.create_request(lot=lot, title="Shed", description="8x10", actor=board_owner)
    services.ARC.apply(req, "submit", actor=board_owner)
    req = services.ARC.apply(req, "accept", actor=w["manager"])
    services.decide(req, outcome=ArcState.APPROVED, citation="x", actor=w["reviewer"])
    req.refresh_from_db()
    with pytest.raises(WorkflowError, match="recused"):
        services.ARC.apply(req, "complete", actor=board_owner)
    services.ARC.apply(req, "complete", actor=w["manager"])


def test_denied_appeal_cannot_be_marked_complete(w):
    req = submitted(w)
    services.decide(req, outcome=ArcState.DENIED, reasons="Too tall", actor=w["reviewer"])
    req.refresh_from_db()
    services.ARC.apply(req, "appeal", actor=w["owner"], note="Please reconsider")
    services.decide_appeal(req, outcome=ArcState.DENIED, reasons="Upheld", actor=w["board"])
    req.refresh_from_db()
    with pytest.raises(WorkflowError, match="denied"):
        services.ARC.apply(req, "complete", actor=w["manager"])


def test_deemed_approval_skips_a_request_decided_meanwhile(w, monkeypatch):
    """Finding D11: one race aborted the whole daily run."""
    first, second = submitted(w), submitted(w)
    past = timezone.localdate() - timedelta(days=1)
    ArcRequest.objects.unscoped("t").filter(pk__in=[first.pk, second.pk]).update(review_due_on=past)
    real_apply = services.ARC.apply

    def racing_apply(req, action, **kwargs):
        if req.pk == first.pk and kwargs.get("actor") is services.SYSTEM:
            raise WorkflowError("already decided")
        return real_apply(req, action, **kwargs)

    monkeypatch.setattr(services.ARC, "apply", racing_apply)
    assert services.apply_deemed_approvals() == 1
