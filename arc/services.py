from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone

from arc.models import DECIDED, ArcRequest, ArcState, Decision, Position, ReviewerVote
from audit.services import record_event
from community.models import CommunityProfile, Lot, Ownership, Person
from community.rules import rule, rule_with_citation
from core import pdf
from core.policy import SYSTEM, can, require, roles_for
from core.scoping import system_access
from core.workflow import Transition, Workflow, WorkflowError


class ArcError(ValueError):
    pass


def _person_id(user) -> int | None:
    return None if user is SYSTEM else roles_for(user).person_id


def is_recused(user, req: ArcRequest) -> bool:
    """Applicant, or owner of the request's lot or an affected lot (CLAUDE.md)."""
    if user is SYSTEM:
        return False
    person_id = _person_id(user)
    if person_id is None:
        return False
    if person_id == req.applicant_id:
        return True
    with system_access("recusal check"):
        lots = {req.lot_id, *req.affected_lots.values_list("pk", flat=True)}
        return Ownership.objects.active().filter(person_id=person_id, lot_id__in=lots).exists()


def owns_lot(user, req: ArcRequest) -> bool:
    return user is not SYSTEM and req.lot_id in roles_for(user).owned_lot_ids


def _not_recused(obj, actor):
    return "You are recused from this request." if is_recused(actor, obj) else None


def _lot_owner(obj, actor):
    return None if owns_lot(actor, obj) else "Only an owner of this lot can do this."


def _may_complete(obj, actor):
    problem = _not_recused(obj, actor)
    if problem:
        return problem
    if obj.state == ArcState.APPEAL_DECIDED:
        latest = obj.decisions.order_by("-decided_at", "-id").first()
        if latest is None or latest.outcome == ArcState.DENIED:
            return "A denied project cannot be marked complete."
    return None


def _within_appeal_window(obj, actor):
    problem = _lot_owner(obj, actor)
    if problem:
        return problem
    if obj.appeal_due_on and timezone.localdate() > obj.appeal_due_on:
        return "The appeal period has ended."
    return None


def _start_clock(obj, actor):
    days = rule("arc.review_days")
    obj.complete_at = timezone.now()
    obj.review_due_on = (timezone.localdate() + datetime.timedelta(days=days)) if days else None
    obj.save(update_fields=["complete_at", "review_due_on"])


def _mark_submitted(obj, actor):
    obj.submitted_at = timezone.now()
    obj.save(update_fields=["submitted_at"])


ARC = Workflow(
    "arc",
    [
        Transition(
            "submit",
            {ArcState.DRAFT},
            ArcState.SUBMITTED,
            "arc.submit",
            "Submit for review",
            guard=_lot_owner,
            after=_mark_submitted,
        ),
        Transition(
            "accept",
            {ArcState.SUBMITTED},
            ArcState.IN_REVIEW,
            "arc.intake",
            "Complete: start review",
            guard=_not_recused,
            after=_start_clock,
        ),
        Transition(
            "return_incomplete",
            {ArcState.SUBMITTED},
            ArcState.DRAFT,
            "arc.intake",
            "Return as incomplete",
            guard=_not_recused,
            note_required=True,
        ),
        Transition(
            "withdraw",
            {ArcState.DRAFT, ArcState.SUBMITTED, ArcState.IN_REVIEW},
            ArcState.WITHDRAWN,
            "arc.submit",
            "Withdraw",
            guard=_lot_owner,
        ),
        # Decisions: applied only through decide() / decide_appeal(), which also
        # write the Decision record.
        Transition(
            "approve",
            {ArcState.IN_REVIEW},
            ArcState.APPROVED,
            "arc.decide",
            "Approve",
            guard=_not_recused,
        ),
        Transition(
            "approve_with_conditions",
            {ArcState.IN_REVIEW},
            ArcState.APPROVED_WITH_CONDITIONS,
            "arc.decide",
            "Approve with conditions",
            guard=_not_recused,
        ),
        Transition(
            "deny", {ArcState.IN_REVIEW}, ArcState.DENIED, "arc.decide", "Deny", guard=_not_recused
        ),
        Transition(
            "appeal",
            DECIDED,
            ArcState.APPEAL,
            "arc.submit",
            "Appeal to the board",
            guard=_within_appeal_window,
            note_required=True,
        ),
        Transition(
            "decide_appeal",
            {ArcState.APPEAL},
            ArcState.APPEAL_DECIDED,
            "arc.appeal_decide",
            "Decide appeal",
            guard=_not_recused,
        ),
        Transition(
            "complete",
            {ArcState.APPROVED, ArcState.APPROVED_WITH_CONDITIONS, ArcState.APPEAL_DECIDED},
            ArcState.COMPLETED,
            "arc.intake",
            "Mark project complete",
            guard=_may_complete,
        ),
    ],
)
DECISION_ACTIONS = {"approve", "approve_with_conditions", "deny", "decide_appeal"}


def create_request(
    *,
    lot: Lot,
    title: str,
    description: str,
    project_type: str = "",
    planned_start=None,
    actor,
    request=None,
) -> ArcRequest:
    require(actor, "arc.submit")
    if lot.pk not in roles_for(actor).owned_lot_ids:
        raise ArcError("You can only apply for a lot you own.")
    if not title.strip() or not description.strip():
        raise ArcError("Describe the project.")
    with system_access("create arc request"):
        applicant = Person.objects.get(user=actor)
    req = ArcRequest.objects.create(
        lot=lot,
        applicant=applicant,
        title=title.strip()[:200],
        description=description.strip(),
        project_type=project_type[:60],
        planned_start=planned_start,
    )
    record_event(
        "arc.created", actor=actor, request=request, target=req, metadata={"lot_id": lot.pk}
    )
    return req


AFFECTED_LOTS_STATES = {ArcState.SUBMITTED, ArcState.IN_REVIEW, ArcState.APPEAL}


def set_affected_lots(req: ArcRequest, lots, *, actor, request=None) -> None:
    """Neighbouring lots whose owners must not review this request.

    Security review 2026-11 (A2): a reviewer who owned an affected lot could
    clear the list and then decide. Now a recused person can never change it,
    changes are allowed only while the request is open, and once review has
    started lots can only be added (removing one could un-recuse someone).
    """
    require(actor, "arc.intake")
    with transaction.atomic(), system_access("set affected lots"):
        req = ArcRequest.objects.select_for_update().get(pk=req.pk)
        if req.state not in AFFECTED_LOTS_STATES:
            raise ArcError("Affected lots can be changed only while the request is open.")
        if is_recused(actor, req):
            raise ArcError("You are recused from this request.")
        current = set(req.affected_lots.values_list("pk", flat=True))
        wanted = {lot.pk for lot in lots} - {req.lot_id}
        removed = current - wanted
        if removed and req.state != ArcState.SUBMITTED:
            raise ArcError(
                "Once review has started, affected lots can only be added. "
                "Ask the board if one was added by mistake."
            )
        req.affected_lots.set(wanted)
    record_event(
        "arc.affected_lots_set",
        actor=actor,
        request=request,
        target=req,
        metadata={
            "count": len(wanted),
            "added": " ".join(sorted(str(n) for n in wanted - current)),
            "removed": " ".join(sorted(str(n) for n in removed)),
        },
    )


@transaction.atomic
def record_position(
    req: ArcRequest, *, position: str, comment: str = "", actor, request=None
) -> ReviewerVote:
    require(actor, "arc.decide")
    with system_access("lock arc request"):
        req = ArcRequest.objects.select_for_update().get(pk=req.pk)
    if req.state != ArcState.IN_REVIEW:
        raise ArcError("Positions are recorded while a request is in review.")
    if is_recused(actor, req):
        raise ArcError("You are recused from this request.")
    if position not in Position.values:
        raise ArcError("Unknown position.")
    vote = ReviewerVote.objects.create(
        request=req, reviewer=actor, position=position, comment=comment
    )
    record_event("arc.position_recorded", actor=actor, request=request, target=req)
    return vote


def deliberation(req: ArcRequest, user):
    """Reviewer positions, or None if this user may not see them."""
    if not can(user, "arc.view_all") or is_recused(user, req) or owns_lot(user, req):
        return None
    latest = {}
    for v in ReviewerVote.objects.filter(request=req).select_related("reviewer"):
        latest[v.reviewer_id] = v
    return list(latest.values())


def _default_citation() -> str:
    _, citation = rule_with_citation("arc.review_days")
    return citation


def decide(
    req: ArcRequest,
    *,
    outcome: str,
    reasons: str = "",
    conditions: str = "",
    citation: str = "",
    actor,
    request=None,
) -> Decision:
    action = {
        ArcState.APPROVED: "approve",
        ArcState.APPROVED_WITH_CONDITIONS: "approve_with_conditions",
        ArcState.DENIED: "deny",
    }.get(outcome)
    if action is None:
        raise ArcError("Choose approve, approve with conditions, or deny.")
    if outcome == ArcState.APPROVED_WITH_CONDITIONS and not conditions.strip():
        raise ArcError("State the conditions.")
    if outcome == ArcState.DENIED and not reasons.strip():
        raise ArcError("Give the reasons for a denial.")
    citation = citation.strip() or _default_citation()
    if not citation:
        raise ArcError("Cite the governing section (e.g. Declaration §9.3).")
    with transaction.atomic():
        req = ARC.apply(req, action, actor=actor, note=reasons, request=request)
        return _record_decision(
            req,
            outcome=outcome,
            reasons=reasons,
            conditions=conditions,
            citation=citation,
            actor=actor,
        )


def _record_decision(
    req, *, outcome, reasons, conditions, citation, actor, is_appeal=False, deemed=False
) -> Decision:
    decision = Decision.objects.create(
        request=req,
        outcome=outcome,
        reasons=reasons,
        conditions=conditions,
        citation=citation[:200],
        is_appeal=is_appeal,
        deemed=deemed,
        decided_by=None if actor is SYSTEM else actor,
    )
    days = rule("arc.appeal_days")
    ArcRequest.objects.unscoped("decision timestamps").filter(pk=req.pk).update(
        decided_at=decision.decided_at,
        appeal_due_on=(timezone.localdate() + datetime.timedelta(days=days))
        if days and not is_appeal
        else None,
    )
    return decision


def decide_appeal(
    req: ArcRequest, *, outcome: str, reasons: str, citation: str = "", actor, request=None
) -> Decision:
    if outcome not in (ArcState.APPROVED, ArcState.APPROVED_WITH_CONDITIONS, ArcState.DENIED):
        raise ArcError("Choose the outcome of the appeal.")
    if not reasons.strip():
        raise ArcError("Give the board's reasons.")
    citation = citation.strip() or _default_citation()
    if not citation:
        raise ArcError("Cite the governing section.")
    with transaction.atomic():
        req = ARC.apply(req, "decide_appeal", actor=actor, note=reasons, request=request)
        return _record_decision(
            req,
            outcome=outcome,
            reasons=reasons,
            conditions="",
            citation=citation,
            actor=actor,
            is_appeal=True,
        )


def apply_deemed_approvals(today: datetime.date | None = None) -> int:
    """Daily: approve requests whose review clock ran out, if the Declaration says so."""
    if not rule("arc.deemed_approval"):
        return 0
    today = today or timezone.localdate()
    _, citation = rule_with_citation("arc.deemed_approval")
    count = 0
    with system_access("deemed approvals"):
        overdue = list(ArcRequest.objects.filter(state=ArcState.IN_REVIEW, review_due_on__lt=today))
    for req in overdue:
        try:
            with transaction.atomic():
                req = ARC.apply(
                    req, "approve", actor=SYSTEM, note="Deemed approved: review period expired."
                )
                _record_decision(
                    req,
                    outcome=ArcState.APPROVED,
                    reasons="Review period expired.",
                    conditions="",
                    citation=citation or "deemed-approval rule",
                    actor=SYSTEM,
                    deemed=True,
                )
        except WorkflowError:
            # Decided by a person in the meantime: leave it, carry on with the rest.
            continue
        count += 1
    return count


def decision_letter(req: ArcRequest, decision: Decision) -> bytes:
    profile = CommunityProfile.get()
    with system_access("decision letter"):
        lot, applicant = req.lot, req.applicant
    outcome = dict(ArcState.choices)[decision.outcome]
    body = [
        ("heading", profile.legal_name),
        (
            "normal",
            "Architectural Review Committee"
            if not decision.is_appeal
            else "Board of Directors - appeal decision",
        ),
        ("normal", ""),
        ("normal", decision.decided_at.strftime("%B %-d, %Y")),
        ("normal", ""),
        ("normal", f"To: {applicant.display_name}"),
        ("normal", f"Lot {lot.number}, {lot.street_address}"),
        ("normal", ""),
        ("heading", f"Re: {req.title} (request #{req.pk})"),
        ("normal", ""),
        (
            "normal",
            f"Decision: {outcome}"
            + (" (deemed approval: the review period expired)" if decision.deemed else ""),
        ),
    ]
    if decision.conditions:
        body += [("normal", ""), ("normal", "Conditions:"), ("normal", decision.conditions)]
    if decision.reasons:
        body += [("normal", ""), ("normal", "Reasons:"), ("normal", decision.reasons)]
    body += [("normal", ""), ("normal", f"Governing provision: {decision.citation}")]
    if req.appeal_due_on and not decision.is_appeal:
        # The deadline fixed when the decision was made, not today's rule.
        body += [
            ("normal", ""),
            (
                "normal",
                f"You may appeal this decision to the board through the owner portal "
                f"until {req.appeal_due_on:%B} {req.appeal_due_on.day}, {req.appeal_due_on.year}.",
            ),
        ]
    body += [("normal", ""), ("small", "Generated by the association's owner portal.")]
    return pdf.render(body, title=f"ARC decision {req.pk}")


def assert_decision_action_not_generic(action: str) -> None:
    if action in DECISION_ACTIONS:
        raise WorkflowError("Use the decision form for this step.")
