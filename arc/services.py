from __future__ import annotations

import datetime

from django.db import models, transaction
from django.utils import timezone

from arc.models import DECIDED, ArcRequest, ArcState, Decision, Position, ReviewerVote
from audit.services import record_event
from community.models import CommunityProfile, Lot, Person
from community.rules import rule, rule_with_citation
from core import pdf
from core.policy import SYSTEM, can, require, roles_for
from core.scoping import system_access
from core.workflow import NotForYou, Transition, Workflow, WorkflowError


class ArcError(ValueError):
    pass


def _person_id(user) -> int | None:
    return None if user is SYSTEM else roles_for(user).person_id


def is_recused(user, req: ArcRequest) -> bool:
    """Applicant, or owner of the request's lot or an affected lot (CLAUDE.md);
    since D65 also anyone whose own matter it is (an account contact of the
    home, or its owner when the request was made), and since D87 the same
    for every affected and neighboring home: its owners, account contacts
    and tenants today, and whoever owned it when the request arose."""
    if user is SYSTEM:
        return False
    person_id = _person_id(user)
    if person_id is None:
        return False
    if person_id == req.applicant_id:
        return True
    from core import conflicts

    if conflicts.own_matter(user, req, hooks=False):
        return True
    from arc.signoffs import neighbors_of

    with system_access("recusal check"):
        lots = {req.lot_id, *req.affected_lots.values_list("pk", flat=True)}
        # The lot's recorded neighbors are affected from the start, not only
        # once submitted (BUG-007).
        lots |= {lot.pk for lot in neighbors_of(req.lot)}
    return conflicts.party_to_lots(user, lots, conflicts.matter_date(req))


def owns_lot(user, req: ArcRequest) -> bool:
    return user is not SYSTEM and req.lot_id in roles_for(user).owned_lot_ids


def _refuse_recused(actor, req: ArcRequest, action: str, request=None, error=None) -> None:
    """Own matters (D65): a recused person's staff step, refused and audited
    before the step's transaction opens."""
    from core import conflicts

    conflicts.refuse(
        actor, req, area="arc", action=action, request=request, error=error or ArcError
    )


def _not_recused(obj, actor):
    return "You are recused from this request." if is_recused(actor, obj) else None


def _lot_owner(obj, actor):
    return None if owns_lot(actor, obj) else NotForYou("Only an owner of this home can do this.")


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
    _add_neighbors_as_affected(obj)


def _add_neighbors_as_affected(req) -> None:
    """The lot's recorded neighbors are affected lots: their owners are
    recused from reviewing it. Only ever adds (security review A2)."""
    from arc.signoffs import neighbors_of

    lots = neighbors_of(req.lot)
    if lots:
        with system_access("neighbors are affected lots"):
            ArcRequest.objects.unscoped("affected lots").get(pk=req.pk).affected_lots.add(*lots)


def _waiting_acknowledgements(req) -> int:
    from arc.models import SignoffRequest, SignoffState

    with system_access("acknowledgements waiting"):
        return SignoffRequest.objects.filter(arc_request=req, state=SignoffState.PENDING).count()


def _may_submit(obj, actor):
    """Straight to the board only when no neighbor acknowledgement is waiting."""
    problem = _lot_owner(obj, actor)
    if problem:
        return problem
    if _waiting_acknowledgements(obj):
        return "Request your neighbors' acknowledgements first."
    return None


def _may_request_acknowledgements(obj, actor):
    problem = _lot_owner(obj, actor)
    if problem:
        return problem
    if not _waiting_acknowledgements(obj):
        return "There are no neighbors to ask."
    return None


def _send_acknowledgement_requests(obj, actor):
    from arc.signoffs import send_signoffs

    send_signoffs(obj, actor=actor)


def _automatic(obj, actor):
    return None if actor is SYSTEM else "This happens when the last acknowledgement is in."


ARC = Workflow(
    "arc",
    [
        Transition(
            "submit",
            {ArcState.DRAFT},
            ArcState.SUBMITTED,
            "arc.submit",
            "Submit for review",
            guard=_may_submit,
            confirm=(
                "Submit this request? The board checks it is "
                "complete, then starts the review clock."
            ),
            after=_mark_submitted,
        ),
        Transition(
            "request_acknowledgements",
            {ArcState.DRAFT},
            ArcState.NEIGHBORS,
            "arc.submit",
            "Request neighbor acknowledgements",
            guard=_may_request_acknowledgements,
            confirm=(
                "Email your neighbors and ask them to acknowledge this project? "
                "You can't edit the request while they respond."
            ),
            after=_send_acknowledgement_requests,
        ),
        Transition(
            "acknowledged",
            {ArcState.NEIGHBORS},
            ArcState.SUBMITTED,
            "arc.submit",
            "All acknowledgements in: submitted",
            guard=_automatic,
            after=_mark_submitted,
        ),
        Transition(
            "accept",
            {ArcState.SUBMITTED},
            ArcState.IN_REVIEW,
            "arc.intake",
            "Complete: start review",
            guard=_not_recused,
            confirm=(
                "Accept this request as complete? The review clock "
                "starts now and the owner is told."
            ),
            after=_start_clock,
        ),
        Transition(
            "return_incomplete",
            {ArcState.SUBMITTED},
            ArcState.DRAFT,
            "arc.intake",
            "Return as incomplete",
            guard=_not_recused,
            confirm=(
                "Return this request to the owner as incomplete? They "
                "see your note and can submit again."
            ),
            note_required=True,
        ),
        Transition(
            "withdraw",
            {ArcState.DRAFT, ArcState.NEIGHBORS, ArcState.SUBMITTED, ArcState.IN_REVIEW},
            ArcState.WITHDRAWN,
            "arc.submit",
            "Withdraw",
            guard=_lot_owner,
            confirm="Withdraw this request? The review stops. You can start a new request later.",
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
            confirm="Approve this request? The owner gets a decision letter.",
        ),
        Transition(
            "approve_with_conditions",
            {ArcState.IN_REVIEW},
            ArcState.APPROVED_WITH_CONDITIONS,
            "arc.decide",
            "Approve with conditions",
            guard=_not_recused,
            confirm="Approve with conditions? The owner gets a decision letter listing them.",
        ),
        Transition(
            "deny",
            {ArcState.IN_REVIEW},
            ArcState.DENIED,
            "arc.decide",
            "Deny",
            guard=_not_recused,
            confirm=(
                "Deny this request? The owner gets a decision letter and can appeal to the board."
            ),
        ),
        Transition(
            "appeal",
            DECIDED,
            ArcState.APPEAL,
            "arc.submit",
            "Appeal to the board",
            guard=_within_appeal_window,
            confirm=(
                "Appeal this decision to the board? The board reviews it and sends a new decision."
            ),
            note_required=True,
        ),
        Transition(
            "decide_appeal",
            {ArcState.APPEAL},
            ArcState.APPEAL_DECIDED,
            "arc.appeal_decide",
            "Decide appeal",
            guard=_not_recused,
            confirm="Record the board's decision on this appeal? The owner gets a letter.",
        ),
        Transition(
            "complete",
            {ArcState.APPROVED, ArcState.APPROVED_WITH_CONDITIONS, ArcState.APPEAL_DECIDED},
            ArcState.COMPLETED,
            "arc.intake",
            "Mark project complete",
            guard=_may_complete,
            confirm="Mark the project complete? This closes the request.",
        ),
    ],
)
MAX_OPEN_DRAFTS = 5
DECISION_ACTIONS = {"approve", "approve_with_conditions", "deny", "decide_appeal"}


EDITABLE_FIELDS = (
    "title",
    "description",
    "project_type",
    "planned_start",
    "planned_completion",
    "location",
    "location_detail",
    "materials",
)


def _clean_request_fields(
    *,
    title: str,
    description: str,
    project_type: str = "",
    planned_start=None,
    planned_completion=None,
    location: str = "",
    location_detail: str = "",
    materials: str = "",
) -> dict:
    """The owner's description of the project, checked the same way when it
    is created and when a draft is edited."""
    if not (title or "").strip() or not (description or "").strip():
        raise ArcError("Describe the project.")
    project_type = (project_type or "").strip()
    types = active_project_types()
    # The owner form requires a choice; here an unknown name is refused.
    if project_type and project_type not in {t.name for t in types} | {"Other"}:
        raise ArcError("Choose the kind of project.")
    if planned_start and planned_completion and planned_completion < planned_start:
        raise ArcError("Completion cannot be before the start.")
    location = location if location in ("front", "side", "rear", "other") else ""
    return {
        "title": title.strip()[:200],
        "description": description.strip(),
        "project_type": project_type[:60],
        "planned_start": planned_start,
        "planned_completion": planned_completion,
        "location": location,
        # Only kept when the owner chose "Other".
        "location_detail": " ".join((location_detail or "").split())[:200]
        if location == "other"
        else "",
        "materials": (materials or "").strip()[:5000],
    }


def create_request(
    *,
    lot: Lot,
    title: str,
    description: str,
    project_type: str = "",
    planned_start=None,
    planned_completion=None,
    location: str = "",
    location_detail: str = "",
    materials: str = "",
    actor,
    request=None,
) -> ArcRequest:
    require(actor, "arc.submit")
    if lot.pk not in roles_for(actor).owned_lot_ids:
        raise ArcError("You can only apply for a home you own.")
    fields = _clean_request_fields(
        title=title,
        description=description,
        project_type=project_type,
        planned_start=planned_start,
        planned_completion=planned_completion,
        location=location,
        location_detail=location_detail,
        materials=materials,
    )
    with system_access("create arc request"):
        applicant = Person.objects.get(user=actor)
        drafts = ArcRequest.objects.filter(applicant=applicant, state=ArcState.DRAFT).count()
    if drafts >= MAX_OPEN_DRAFTS:
        # Files are permanent: cap unsubmitted requests (re-audit of review C4).
        raise ArcError("Submit or withdraw one of your draft requests before starting another.")
    req = ArcRequest.objects.create(lot=lot, applicant=applicant, **fields)
    record_event(
        "arc.created", actor=actor, request=request, target=req, metadata={"lot_id": lot.pk}
    )
    # The lot's recorded neighbors are on the list from the start (locked).
    from arc.signoffs import prepare_signoffs

    prepare_signoffs(req, actor=actor)
    return req


def can_edit(user, req: ArcRequest) -> bool:
    """Only an owner of the lot, and only while the request is a draft."""
    return (
        user is not SYSTEM
        and req.state == ArcState.DRAFT
        and can(user, "arc.submit")
        and owns_lot(user, req)
    )


def edit_request(req: ArcRequest, *, actor, request=None, **changes) -> ArcRequest:
    """The owner corrects their draft. The earlier wording is kept as an
    append-only revision (CLAUDE.md: keep every submitted version)."""
    from arc.models import ArcRequestRevision

    require(actor, "arc.submit")
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ArcError("Those details cannot be changed here.")
    with transaction.atomic(), system_access("edit arc draft"):
        req = ArcRequest.objects.select_for_update().get(pk=req.pk)
        if req.state != ArcState.DRAFT:
            raise ArcError("Only a draft can be edited. Withdraw it and start again if needed.")
        if not owns_lot(actor, req):
            raise ArcError("Only an owner of this home can edit the request.")
        current = {f: getattr(req, f) for f in EDITABLE_FIELDS}
        fields = _clean_request_fields(**{**current, **changes})
        changed = [f for f in EDITABLE_FIELDS if fields[f] != current[f]]
        if not changed:
            return req
        ArcRequestRevision.objects.create(
            request=req, edited_by=actor, was_submitted=req.submitted_at is not None, **current
        )
        for f in changed:
            setattr(req, f, fields[f])
        req.save(update_fields=changed)
        # Neighbors acknowledged the earlier wording: after a change they are
        # asked again (review R2-002). Their earlier answers stay on record.
        _ask_neighbors_again(req)
    record_event(
        "arc.edited",
        actor=actor,
        request=request,
        target=req,
        metadata={"fields": " ".join(changed)},
    )
    return req


def _ask_neighbors_again(req: ArcRequest) -> int:
    """Acknowledgements that were sent or answered go back to waiting, so the
    owner must request them again before the draft can be submitted. Earlier
    answers stay on record as SignoffResponse rows. Call inside
    system_access. Returns the number of rows reset."""
    from arc.models import SignoffRequest, SignoffState

    return (
        SignoffRequest.objects.filter(arc_request=req)
        .exclude(state=SignoffState.WITHDRAWN)
        .filter(models.Q(sent_at__isnull=False) | ~models.Q(state=SignoffState.PENDING))
        .update(
            state=SignoffState.PENDING,
            sent_at=None,
            token_hash="",  # nosec B106 - clearing an old link's hash, not a password
        )
    )


def add_owner_files(req: ArcRequest, uploads, *, actor, request, label: str = "") -> int:
    """The lot owner adds plans or photos to their draft. Every owner path that
    adds files to a draft goes through here: new files change what the
    neighbors were shown, so acknowledgements already sent or answered are
    asked again, as for a text edit (reviews BUG-001, SEC-003, R2-001).
    Returns the number of files added."""
    from django.contrib import messages

    from documents import attachments

    require(actor, "arc.submit")
    with transaction.atomic(), system_access("add files to arc draft"):
        req = ArcRequest.objects.select_for_update().get(pk=req.pk)
        if req.state != ArcState.DRAFT:
            raise ArcError("The owner can add files only while the request is a draft.")
        if not owns_lot(actor, req):
            raise ArcError("Only an owner of this home can add files to the request.")
        added = attachments.attach_many(request, req, uploads, actor=actor, label=label)
        if added and _ask_neighbors_again(req):
            messages.info(
                request,
                "The neighbors acknowledged the earlier plans. "
                "Request their acknowledgements again before you submit.",
            )
    return added


AFFECTED_LOTS_STATES = {ArcState.SUBMITTED, ArcState.IN_REVIEW, ArcState.APPEAL}


def set_affected_lots(req: ArcRequest, lots, *, actor, request=None) -> None:
    """Neighboring lots whose owners must not review this request.

    Security review 2026-11 (A2): a reviewer who owned an affected lot could
    clear the list and then decide. Now a recused person can never change it,
    changes are allowed only while the request is open, and once review has
    started lots can only be added (removing one could un-recuse someone).
    """
    require(actor, "arc.intake")
    _refuse_recused(actor, req, "affected_lots", request)
    with transaction.atomic(), system_access("set affected lots"):
        req = ArcRequest.objects.select_for_update().get(pk=req.pk)
        if req.state not in AFFECTED_LOTS_STATES:
            raise ArcError("Affected homes can be changed only while the request is open.")
        if is_recused(actor, req):
            raise ArcError("You are recused from this request.")
        current = set(req.affected_lots.values_list("pk", flat=True))
        wanted = {lot.pk for lot in lots} - {req.lot_id}
        removed = current - wanted
        if removed and req.state != ArcState.SUBMITTED:
            raise ArcError(
                "Once review has started, affected homes can only be added. "
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


def record_position(
    req: ArcRequest, *, position: str, comment: str = "", actor, request=None
) -> ReviewerVote:
    require(actor, "arc.decide")
    _refuse_recused(actor, req, "position", request)
    with transaction.atomic():
        return _record_position(
            req, position=position, comment=comment, actor=actor, request=request
        )


def _record_position(req, *, position, comment, actor, request) -> ReviewerVote:
    with system_access("lock arc request"):
        req = ArcRequest.objects.select_for_update().get(pk=req.pk)
    if req.state != ArcState.IN_REVIEW:
        raise ArcError("Positions are recorded while a request is in review.")
    if is_recused(actor, req):
        raise ArcError("You are recused from this request.")
    if not position:
        # The form starts on "Choose..." so nothing is recorded by accident (WT-015).
        raise ArcError("Choose your position before recording it.")
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
    _refuse_recused(actor, req, action, request, WorkflowError)
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
    from arc.notify import queue_decision_email

    queue_decision_email(decision)  # the applicant is emailed after commit
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
    _refuse_recused(actor, req, "decide_appeal", request, WorkflowError)
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
        ("normal", lot.legal_label),
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


# --- Project types (board-configured) ---------------------------------------------------

from arc.models import ArcProjectType  # noqa: E402


def active_project_types():
    return list(ArcProjectType.objects.filter(active=True))


def save_project_type(
    *,
    name: str,
    guidance: str = "",
    citation: str = "",
    active: bool = True,
    sort_order: int = 100,
    project_type: ArcProjectType | None = None,
    actor,
    request=None,
) -> ArcProjectType:
    """Add or change a project type. Renaming keeps old requests' recorded name."""
    require(actor, "arc.configure")
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ArcError("A project type needs a name.")
    from django.db import IntegrityError

    fields = {
        "name": name,
        "guidance": (guidance or "").strip()[:4000],
        "citation": " ".join((citation or "").split())[:200],
        "active": bool(active),
        "sort_order": max(0, min(int(sort_order or 100), 999)),
    }
    try:
        with transaction.atomic():
            if project_type is None:
                project_type = ArcProjectType.objects.create(**fields)
            else:
                ArcProjectType.objects.filter(pk=project_type.pk).update(**fields)
                project_type.refresh_from_db()
    except IntegrityError as exc:
        raise ArcError("A project type with that name already exists.") from exc
    record_event(
        "arc.project_type_saved",
        actor=actor,
        request=request,
        target=project_type,
        metadata={"active": project_type.active},
    )
    return project_type
