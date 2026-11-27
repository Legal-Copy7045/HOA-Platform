from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone

from audit.services import record_event
from community.models import CommunityProfile, Person
from community.rules import rule, rule_with_citation
from compliance.models import OwnerResponse, Violation, ViolationCategory, ViolationState
from core import conflicts, pdf
from core.policy import SYSTEM, require, roles_for
from core.scoping import system_access
from core.workflow import Transition, Workflow

S = ViolationState


class ComplianceError(ValueError):
    pass


def _lot_owner(obj, actor):
    if actor is not SYSTEM and obj.lot_id in roles_for(actor).owned_lot_ids:
        return None
    return "Only an owner of this home can respond."


def _set_cure_by(obj, actor):
    days = rule("violation.cure_days")
    obj.noticed_at = timezone.now()
    obj.cure_by = timezone.localdate() + datetime.timedelta(days=days) if days else None
    obj.save(update_fields=["noticed_at", "cure_by"])


VIOLATION = Workflow(
    "violation",
    [
        Transition(
            "send_notice",
            {S.OPEN},
            S.NOTICED,
            "violation.manage",
            "Send notice",
            after=_set_cure_by,
            confirm="Send the notice? The owner gets a letter with the date to fix it by.",
        ),
        Transition(
            "respond",
            {S.NOTICED, S.HEARING},
            S.RESPONDED,
            "lot.view_own",
            "Owner response",
            guard=_lot_owner,
        ),
        Transition(
            "schedule_hearing",
            {S.NOTICED, S.RESPONDED},
            S.HEARING,
            "violation.manage",
            "Schedule hearing",
            note_required=True,
            confirm="Schedule a hearing? The owner sees your note with the date and place.",
        ),
        Transition(
            "mark_cured",
            {S.NOTICED, S.RESPONDED, S.HEARING},
            S.CURED,
            # A decision about the owner, after a hearing or otherwise: the
            # board's alone, never the manager's (owner decision 2026-11-26, D49).
            "violation.decide",
            "Mark cured (board)",
            confirm="Mark this as fixed? The owner is no longer asked to act.",
        ),
        Transition(
            "uphold",
            {S.HEARING, S.RESPONDED},
            S.UPHELD,
            "violation.decide",
            "Uphold (board)",
            note_required=True,
            confirm=(
                "Uphold this violation? The owner is told the board's "
                "decision and any fine that follows."
            ),
        ),
        Transition(
            "dismiss",
            {S.OPEN, S.NOTICED, S.RESPONDED, S.HEARING},
            S.DISMISSED,
            "violation.decide",
            "Dismiss (board)",
            note_required=True,
            confirm="Dismiss this notice? It closes with no action against the owner.",
        ),
        Transition(
            "close",
            {S.CURED, S.UPHELD},
            S.CLOSED,
            "violation.manage",
            "Close",
            confirm="Close this record? It stays in the home's history.",
        ),
    ],
)


def report(
    *, lot, category: str, description: str, observed_on=None, actor, request=None
) -> Violation:
    require(actor, "violation.manage")
    if category not in ViolationCategory.values or not description.strip():
        raise ComplianceError("Choose a category and describe what was observed.")
    v = Violation.objects.create(
        lot=lot,
        category=category,
        description=description.strip(),
        observed_on=observed_on or timezone.localdate(),
        reported_by=actor,
    )
    record_event(
        "violation.reported",
        actor=actor,
        request=request,
        target=v,
        metadata={"lot_id": lot.pk, "category": category},
    )
    return v


def respond(violation: Violation, *, text: str, actor, request=None) -> OwnerResponse:
    if not text.strip():
        raise ComplianceError("Write your response.")
    with transaction.atomic():
        VIOLATION.apply(violation, "respond", actor=actor, request=request)
        with system_access("owner response"):
            person = Person.objects.get(user=actor)
        return OwnerResponse.objects.create(violation=violation, person=person, text=text.strip())


OWNER_OPEN_STATES = (S.NOTICED, S.RESPONDED, S.HEARING)


def owner_may_add_photo(actor, v: Violation) -> bool:
    """An owner of the lot (on the deed, not view-only or a former owner) may
    add a photo showing the fix while the notice is open."""
    from core.policy import can

    if actor is SYSTEM or not getattr(actor, "is_authenticated", False):
        return False
    return (
        v.state in OWNER_OPEN_STATES
        and can(actor, "lot.view_own")
        and v.lot_id in roles_for(actor).owned_lot_ids
    )


def notice_letter(v: Violation) -> bytes:
    profile = CommunityProfile.get()
    with system_access("violation notice letter"):
        lot = v.lot
    days, citation = rule_with_citation("violation.cure_days")
    _hearing_days, hearing_cite = rule_with_citation("violation.hearing_notice_days")
    body = [
        ("heading", profile.legal_name),
        ("normal", "Notice of covenant violation"),
        ("normal", ""),
        ("normal", timezone.localdate().strftime("%B %-d, %Y")),
        ("normal", lot.legal_label),
        ("normal", ""),
        ("heading", v.get_category_display()),
        ("normal", f"Observed on {v.observed_on:%B %-d, %Y}:"),
        ("normal", v.description),
        ("normal", ""),
    ]
    if v.cure_by:
        body.append(
            (
                "normal",
                f"Please correct this by {v.cure_by:%B %-d, %Y}"
                f"{f' ({days} days, {citation})' if citation else ''}.",
            )
        )
    body += [
        ("normal", ""),
        (
            "normal",
            "You may respond in writing through the owner portal, and you may ask "
            "for a hearing before the board as provided in the governing documents"
            + (f" ({hearing_cite})" if hearing_cite else "")
            + ".",
        ),
        ("normal", ""),
        ("small", "No fine has been assessed by this notice."),
    ]
    return pdf.render(body, title=f"Violation notice {v.pk}")


def set_follow_up(violation: Violation, on, *, actor, request=None) -> Violation:
    """Board or manager: the date to check on this violation next."""
    require(actor, "violation.manage")
    conflicts.refuse(
        actor,
        violation,
        area="violation",
        action="follow_up",
        request=request,
        error=ComplianceError,
    )
    Violation.objects.unscoped("follow-up").filter(pk=violation.pk).update(follow_up_on=on)
    violation.follow_up_on = on
    record_event(
        "followup.set",
        actor=actor,
        request=request,
        target=violation,
        metadata={
            "kind": "violation",
            "object_id": violation.pk,
            "on": on.isoformat() if on else None,
        },
    )
    return violation
