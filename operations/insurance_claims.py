"""Insurance claims (D85, optional feature "insurance_claims").

Claims against the association's own policies. The board and the manager
record a claim and keep it current (dates, the adjuster, the insurer's
answers, deadlines, photos and documents, related work orders, money
expected and received); accepting a settlement and closing or reopening a
claim are board decisions. A settlement at or above rule
``insurance.second_approval_cents`` needs two different board members.

    reported -> under_review -> approved / denied -> paid -> closed
    closed or denied -> reopened (under review again)

Owners never see claims (board, manager and auditor only). Proof of loss
due dates and appeal deadlines put a reminder on My items 14 days ahead.
The books branch codes the bank lines; this records what is expected and
what arrived.
"""

from __future__ import annotations

import datetime

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from audit import actions as audit_actions
from audit.services import record_event
from community.rules import rule
from core.money import format_cents
from core.policy import can, require
from operations.models_options import (
    ClaimState,
    InsuranceClaim,
    InsuranceClaimEvent,
    InsuranceMoney,
    SettlementApproval,
    SettlementOffer,
)

FEATURE = "insurance_claims"
FILE_LABEL = "Claim file"
OPEN_STATES = (ClaimState.REPORTED, ClaimState.UNDER_REVIEW, ClaimState.APPROVED, ClaimState.PAID)

for _name, _text, _fields in (
    ("insurance.claim_recorded", "Insurance claim recorded", ("claim_id",)),
    ("insurance.claim_updated", "Insurance claim details changed", ("claim_id",)),
    ("insurance.claim_state", "Insurance claim status changed", ("claim_id", "from", "to")),
    ("insurance.offer_recorded", "Settlement offer recorded", ("claim_id", "amount_cents")),
    (
        "insurance.offer_approved",
        "Settlement acceptance approved",
        ("claim_id", "approvals", "required"),
    ),
    ("insurance.money_recorded", "Insurance money recorded", ("claim_id", "kind", "amount_cents")),
):
    audit_actions.register(audit_actions.ActionSpec(_name, _text, frozenset(_fields)))


class ClaimError(ValueError):
    """A refusal the page shows."""


# --- Adapter (switch at merge) -------------------------------------------------------------


def policy_label(claim: InsuranceClaim) -> str:
    """Switch to claude/document-renewals at merge: the association Policy
    record (carrier, number, coverage, renewal). Interim: the carrier and
    policy number typed on the claim."""
    return " · ".join(p for p in (claim.carrier, claim.policy_number) if p)


# --- Reading ---------------------------------------------------------------------------------


def money_totals(claim: InsuranceClaim) -> dict[str, int]:
    rows = dict(
        claim.money.values_list("kind").annotate(s=Sum("amount_cents")).values_list("kind", "s")
    )
    return {"expected": rows.get("expected", 0) or 0, "received": rows.get("received", 0) or 0}


def offer_status(offer: SettlementOffer) -> str:
    return "accepted" if offer.approvals.count() >= offer.required_approvals else "pending"


def accepted_offer(claim: InsuranceClaim) -> SettlementOffer | None:
    for offer in reversed(list(claim.offers.prefetch_related("approvals"))):
        if offer_status(offer) == "accepted":
            return offer
    return None


def deadlines(claim: InsuranceClaim) -> list[tuple[str, datetime.date]]:
    out = []
    if claim.proof_of_loss_due and claim.state in (ClaimState.REPORTED, ClaimState.UNDER_REVIEW):
        out.append(("Proof of loss due", claim.proof_of_loss_due))
    if claim.appeal_deadline and claim.state == ClaimState.DENIED:
        out.append(("Appeal deadline", claim.appeal_deadline))
    return out


# --- Changes ---------------------------------------------------------------------------------


def _event(claim, action, actor, *, from_state="", to_state="", note=""):
    InsuranceClaimEvent.objects.create(
        claim=claim,
        action=action,
        from_state=from_state,
        to_state=to_state,
        note=note[:2000],
        by=actor,
    )


FIELDS = (
    "carrier",
    "policy_number",
    "incident_on",
    "discovered_on",
    "description",
    "location",
    "common_area",
    "claim_number",
    "adjuster_name",
    "adjuster_contact",
    "estimate_cents",
    "deductible_cents",
    "proof_of_loss_due",
    "appeal_deadline",
)


def _check(values: dict) -> None:
    if not (values.get("carrier") or "").strip():
        raise ClaimError("Name the insurance company.")
    if not values.get("incident_on"):
        raise ClaimError("Give the date of the loss.")
    if not (values.get("description") or "").strip():
        raise ClaimError("Describe what happened.")
    if values["incident_on"] > timezone.localdate():
        raise ClaimError("The date of the loss cannot be in the future.")
    found = values.get("discovered_on")
    if found and found < values["incident_on"]:
        raise ClaimError("It cannot have been found before it happened.")


def create(*, actor, request=None, work_orders=(), **values) -> InsuranceClaim:
    require(actor, "insurance.record")
    values = {k: v for k, v in values.items() if k in FIELDS}
    _check(values)
    for key, value in list(values.items()):
        if isinstance(value, str):
            values[key] = value.strip()
    with transaction.atomic():
        claim = InsuranceClaim.objects.create(created_by=actor, **values)
        if work_orders:
            claim.work_orders.set(work_orders)
        _event(claim, "reported", actor, to_state=claim.state)
    record_event(
        "insurance.claim_recorded",
        actor=actor,
        request=request,
        target=claim,
        metadata={"claim_id": claim.pk},
    )
    return claim


def update(claim: InsuranceClaim, *, actor, request=None, work_orders=None, **values):
    require(actor, "insurance.record")
    if claim.state == ClaimState.CLOSED:
        raise ClaimError("Reopen the claim to change it.")
    values = {k: v for k, v in values.items() if k in FIELDS}
    merged = {f: getattr(claim, f) for f in FIELDS} | values
    _check(merged)
    for key, value in values.items():
        setattr(claim, key, value.strip() if isinstance(value, str) else value)
    with transaction.atomic():
        claim.save()
        if work_orders is not None:
            claim.work_orders.set(work_orders)
        _event(claim, "updated", actor, note=", ".join(sorted(values)))
    record_event(
        "insurance.claim_updated",
        actor=actor,
        request=request,
        target=claim,
        metadata={"claim_id": claim.pk},
    )
    return claim


TRANSITIONS = {
    # action: (sources, target, permission, label, note required)
    "review": (
        {ClaimState.REPORTED},
        ClaimState.UNDER_REVIEW,
        "insurance.record",
        "Insurer is reviewing",
        False,
    ),
    "deny": (
        {ClaimState.REPORTED, ClaimState.UNDER_REVIEW},
        ClaimState.DENIED,
        "insurance.record",
        "Insurer denied the claim",
        True,
    ),
    "close": (
        {ClaimState.PAID, ClaimState.DENIED, ClaimState.APPROVED},
        ClaimState.CLOSED,
        "insurance.decide",
        "Close the claim",
        True,
    ),
    "reopen": (
        {ClaimState.CLOSED, ClaimState.DENIED},
        ClaimState.UNDER_REVIEW,
        "insurance.decide",
        "Reopen (appeal or new information)",
        True,
    ),
}


def change_state(claim: InsuranceClaim, action: str, *, actor, note: str = "", request=None):
    if action not in TRANSITIONS:
        raise ClaimError("Unknown step.")
    sources, target, permission, _label, note_required = TRANSITIONS[action]
    require(actor, permission)
    if note_required and not note.strip():
        raise ClaimError("Please add a note explaining this step.")
    with transaction.atomic():
        claim = InsuranceClaim.objects.select_for_update().get(pk=claim.pk)
        if claim.state not in sources:
            raise ClaimError(f"Not possible while {claim.get_state_display().lower()}.")
        before = claim.state
        InsuranceClaim.objects.filter(pk=claim.pk).update(
            state=target, state_changed_at=timezone.now()
        )
        _event(claim, action, actor, from_state=before, to_state=target, note=note)
    record_event(
        "insurance.claim_state",
        actor=actor,
        request=request,
        target=claim,
        metadata={"claim_id": claim.pk, "from": before, "to": target},
    )
    claim.refresh_from_db()
    return claim


def available_steps(claim: InsuranceClaim, user) -> list[tuple[str, str, bool]]:
    return [
        (action, label, note_required)
        for action, (sources, _t, permission, label, note_required) in TRANSITIONS.items()
        if claim.state in sources and can(user, permission)
    ]


def approvals_needed(amount_cents: int) -> int:
    threshold = rule("insurance.second_approval_cents") or 0
    return 2 if threshold and amount_cents >= threshold else 1


def record_offer(
    claim: InsuranceClaim, *, amount_cents: int, note: str = "", actor, request=None
) -> SettlementOffer:
    """The insurer's settlement offer, recorded by the board or manager."""
    require(actor, "insurance.record")
    if claim.state not in (ClaimState.REPORTED, ClaimState.UNDER_REVIEW, ClaimState.APPROVED):
        raise ClaimError("Offers are recorded while the claim is open.")
    if not amount_cents or amount_cents <= 0:
        raise ClaimError("The offer must be more than zero.")
    offer = SettlementOffer.objects.create(
        claim=claim,
        amount_cents=amount_cents,
        required_approvals=approvals_needed(amount_cents),
        note=note.strip(),
        recorded_by=actor,
    )
    _event(claim, "offer", actor, note=f"Offer {format_cents(amount_cents)}")
    record_event(
        "insurance.offer_recorded",
        actor=actor,
        request=request,
        target=claim,
        metadata={"claim_id": claim.pk, "amount_cents": amount_cents},
    )
    return offer


def approve_offer(offer: SettlementOffer, *, actor, request=None) -> str:
    """One board member's approval to accept the settlement. When it has the
    approvals it needs, the claim is approved and the amount is expected."""
    require(actor, "insurance.decide")
    claim = offer.claim
    with transaction.atomic():
        claim = InsuranceClaim.objects.select_for_update().get(pk=claim.pk)
        if claim.state not in (ClaimState.REPORTED, ClaimState.UNDER_REVIEW):
            raise ClaimError("This claim is not waiting on a settlement.")
        if offer.claim.offers.order_by("-created_at", "-id").first().pk != offer.pk:
            raise ClaimError("A newer offer was recorded; decide that one.")
        try:
            with transaction.atomic():
                SettlementApproval.objects.create(offer=offer, approver=actor)
        except IntegrityError as exc:
            raise ClaimError("You have already approved accepting this offer.") from exc
        count = offer.approvals.count()
        if count >= offer.required_approvals:
            InsuranceClaim.objects.filter(pk=claim.pk).update(
                state=ClaimState.APPROVED, state_changed_at=timezone.now()
            )
            InsuranceMoney.objects.create(
                claim=claim,
                kind=InsuranceMoney.EXPECTED,
                amount_cents=offer.amount_cents,
                on=timezone.localdate(),
                reference=f"Settlement #{offer.pk}",
                recorded_by=actor,
            )
            _event(
                claim,
                "settlement_accepted",
                actor,
                from_state=claim.state,
                to_state=ClaimState.APPROVED,
                note=format_cents(offer.amount_cents),
            )
        else:
            _event(
                claim, "settlement_approval", actor, note=f"{count} of {offer.required_approvals}"
            )
    record_event(
        "insurance.offer_approved",
        actor=actor,
        request=request,
        target=claim,
        metadata={"claim_id": claim.pk, "approvals": count, "required": offer.required_approvals},
    )
    return offer_status(offer)


def record_money(
    claim: InsuranceClaim,
    *,
    kind: str,
    amount_cents: int,
    on: datetime.date,
    reference: str = "",
    actor,
    request=None,
) -> InsuranceMoney:
    """Money expected or received (negative corrects an earlier line). The
    first receipt on an approved claim marks it paid."""
    require(actor, "insurance.record")
    if kind not in (InsuranceMoney.EXPECTED, InsuranceMoney.RECEIVED):
        raise ClaimError("Expected or received?")
    if not amount_cents:
        raise ClaimError("Give the amount.")
    if claim.state == ClaimState.CLOSED:
        raise ClaimError("Reopen the claim to record money on it.")
    with transaction.atomic():
        row = InsuranceMoney.objects.create(
            claim=claim,
            kind=kind,
            amount_cents=amount_cents,
            on=on,
            reference=reference.strip()[:80],
            recorded_by=actor,
        )
        if kind == InsuranceMoney.RECEIVED and claim.state == ClaimState.APPROVED:
            InsuranceClaim.objects.filter(pk=claim.pk).update(
                state=ClaimState.PAID, state_changed_at=timezone.now()
            )
            _event(claim, "paid", actor, from_state=claim.state, to_state=ClaimState.PAID)
    record_event(
        "insurance.money_recorded",
        actor=actor,
        request=request,
        target=claim,
        metadata={"claim_id": claim.pk, "kind": kind, "amount_cents": amount_cents},
    )
    claim.refresh_from_db()
    return row


# --- My items --------------------------------------------------------------------------------


def my_items(user) -> list:
    from django.urls import reverse

    from core.dates import short_date
    from core.my_items import Item

    out = []
    today = timezone.localdate()
    if can(user, "insurance.record"):
        for claim in InsuranceClaim.objects.filter(
            state__in=[ClaimState.REPORTED, ClaimState.UNDER_REVIEW, ClaimState.DENIED]
        ):
            for label, due in deadlines(claim):
                if due <= today + datetime.timedelta(days=14):
                    out.append(
                        Item(
                            "Finance",
                            f"{label}: claim {claim.claim_number or '#' + str(claim.pk)}",
                            f"{policy_label(claim)} · {label.lower()} {short_date(due)}",
                            reverse("insurance_claim_detail", args=[claim.pk]),
                            "danger" if due < today else "warning",
                            due,
                            nav="insurance_claims",
                            icon="shield",
                        )
                    )
    if can(user, "insurance.decide"):
        for offer in SettlementOffer.objects.filter(
            claim__state__in=[ClaimState.REPORTED, ClaimState.UNDER_REVIEW]
        ).select_related("claim"):
            latest = offer.claim.offers.order_by("-created_at", "-id").first()
            if latest.pk != offer.pk or offer.approvals.filter(approver=user).exists():
                continue
            out.append(
                Item(
                    "Second approvals",
                    f"Accept insurance settlement of {format_cents(offer.amount_cents)}",
                    f"Claim {offer.claim.claim_number or '#' + str(offer.claim_id)} · "
                    f"{policy_label(offer.claim)}",
                    reverse("insurance_claim_detail", args=[offer.claim_id]) + "#settlement",
                    "warning",
                    nav="insurance_claims",
                    icon="shield",
                )
            )
    return out
