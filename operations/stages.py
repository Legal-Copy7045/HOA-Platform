"""Stage tracker paths (D99) for the Maintenance and money-out state machines
that are not core.workflow Workflows: reimbursement claims, insurance
claims, capital projects and vendor onboarding rounds. States only."""

from core.workflow import outcome, stage, track
from operations.models_options import ClaimState, ProjectState
from operations.onboarding_models import RoundStatus


def _event_states(events) -> list[str] | None:
    rows = list(events.values_list("from_state", "to_state"))
    if not rows:
        return None
    out = [rows[0][0] or rows[0][1]]
    out += [to for _f, to in rows if to]
    return out


def _reimbursement_state(claim) -> str:
    from operations import reimbursements

    return reimbursements.state(claim)["status"]


def _reimbursement_visited(claim) -> list[str]:
    return ["awaiting", _reimbursement_state(claim)]


REIMBURSEMENT = track(
    "operations.reimbursementclaim",
    [
        stage("Submitted", "awaiting", who="The board: approve or decline"),
        stage("Approved", "approved", who="The board or manager: pay it"),
        stage("Paid", "paid"),
    ],
    [outcome("Declined", "declined"), outcome("Withdrawn", "withdrawn")],
    state=_reimbursement_state,
    visited=_reimbursement_visited,
)

INSURANCE_CLAIM = track(
    "operations.insuranceclaim",
    [
        stage("Reported", ClaimState.REPORTED, who="The board: send the claim to the insurer"),
        stage("Under review", ClaimState.UNDER_REVIEW, who="The insurer: decide"),
        stage("Approved", ClaimState.APPROVED, who="The insurer: pay"),
        stage("Paid", ClaimState.PAID, who="The board: close the claim"),
        stage("Closed", ClaimState.CLOSED),
    ],
    [outcome("Denied", ClaimState.DENIED, who="The board: appeal, reopen or close it")],
    visited=lambda claim: _event_states(claim.events.all()) if claim.pk else None,
    due=lambda claim: (
        claim.proof_of_loss_due
        if claim.state in (ClaimState.REPORTED, ClaimState.UNDER_REVIEW)
        else claim.appeal_deadline
        if claim.state == ClaimState.DENIED
        else None
    ),
)

CAPITAL_PROJECT = track(
    "operations.capitalproject",
    [
        stage("Planned", ProjectState.PLANNED, who="The board: approve the project"),
        stage("Approved", ProjectState.APPROVED, who="The board or manager: start the work"),
        stage("In progress", ProjectState.IN_PROGRESS, who="The board or manager: complete it"),
        stage("Completed", ProjectState.COMPLETED),
    ],
    [outcome("Cancelled", ProjectState.CANCELLED)],
    visited=lambda project: _event_states(project.events.all()) if project.pk else None,
)

ONBOARDING_ROUND = track(
    "operations.onboardinground",
    [
        stage("Vendor filling in", RoundStatus.DRAFT, who="The vendor: fill in and send the form"),
        stage(
            "Returned to the vendor",
            RoundStatus.RETURNED,
            branch=True,
            who="The vendor: correct and send it again",
        ),
        stage("Board review", RoundStatus.SUBMITTED, who="The board: approve, return or decline"),
        stage("Approved", RoundStatus.APPROVED),
    ],
    [outcome("Declined", RoundStatus.DECLINED), outcome("Cancelled", RoundStatus.CANCELLED)],
    state=lambda r: r.status,
)
