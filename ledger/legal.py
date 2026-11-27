"""Liens and legal collection on a collection case (D82, optional feature
"collections_legal").

The collection case and its Workflow (ledger.services.COLLECTION) are kept
as they are; once the board refers a case ("Referred for legal collection")
each step with counsel is an append-only ``LegalEvent`` on it: the attorney
referral (firm, attorney, date, counsel's reference, fee arrangement), the
demand letter counsel sent, the lien (in Pennsylvania the lien arises by
itself, 68 Pa.C.S. § 5315: the record says what, if anything, was filed: a
statement of lien or a judgment, with court, docket, date, amount and the
recorded document), the lawsuit and hearing dates, the judgment, costs and
attorney fees (and whether the Declaration lets them be charged to the
owner), money recovered, and the satisfaction or release of the lien.

Legal matters are board-only records ("legal.view"). The owner sees only the
charges on their account and the letters sent to them. Charging a cost to
the owner's account is a board member's step (never the manager's, never on
one's own home, D65) and only with rule ``legal.costs_chargeable`` recorded
with its citation. When a referred case's balance reaches zero and a lien was
filed, My items asks the board to file the release.
"""

from __future__ import annotations

import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit import actions as audit_actions
from audit.services import record_event
from community.rules import rule_with_citation
from core import conflicts
from core.money import format_cents
from core.policy import can, require
from core.scoping import system_access
from ledger.models import CollectionCase, EntryKind, LedgerEntry
from ledger.models_options import Court, LegalEvent, LegalKind, LienFiling

FEATURE = "collections_legal"
FILE_LABEL = "Legal"

for _name, _text, _fields in (
    ("legal.recorded", "Legal collection step recorded", ("case_id", "kind")),
    (
        "legal.cost_posted",
        "Collection cost charged to the owner's account",
        ("case_id", "lot_id", "amount_cents"),
    ),
):
    audit_actions.register(audit_actions.ActionSpec(_name, _text, frozenset(_fields)))


class LegalError(ValueError):
    """A refusal the page shows."""


def _refuse_own(actor, case, action, request):
    conflicts.refuse(
        actor, case, area="collection", action=action, request=request, error=LegalError
    )


def events(case: CollectionCase) -> list[LegalEvent]:
    return list(
        LegalEvent.objects.unscoped("legal timeline")
        .filter(case=case)
        .select_related("recorded_by", "ledger_entry", "cost")
    )


def is_with_counsel(case: CollectionCase) -> bool:
    return case.state == "referred" or (
        LegalEvent.objects.unscoped("legal check")
        .filter(case=case, kind=LegalKind.REFERRAL)
        .exists()
    )


REQUIRED = {
    LegalKind.REFERRAL: ("firm",),
    LegalKind.DEMAND: (),
    LegalKind.LIEN: ("lien_filing",),
    LegalKind.LAWSUIT: ("court", "reference"),
    LegalKind.HEARING: ("due_on",),
    LegalKind.JUDGMENT: ("amount_cents",),
    LegalKind.COST: ("amount_cents", "detail"),
    LegalKind.RECOVERY: ("amount_cents",),
    LegalKind.RELEASE: (),
    LegalKind.NOTE: ("detail",),
}
LABELS = {
    "firm": "the law firm",
    "lien_filing": "what was filed",
    "court": "the court",
    "reference": "the docket number",
    "due_on": "the hearing date",
    "amount_cents": "the amount",
    "detail": "a description",
}
TEXT_FIELDS = ("firm", "attorney", "reference", "fee_arrangement", "detail")


def record(
    case: CollectionCase,
    *,
    kind: str,
    occurred_on: datetime.date | None = None,
    actor,
    request=None,
    **fields,
) -> LegalEvent:
    """Add one step to the case's legal timeline (board only)."""
    require(actor, "legal.record")
    if kind not in LegalKind.values or kind == LegalKind.COST_POSTED:
        raise LegalError("Choose what happened.")
    _refuse_own(actor, case, f"legal_{kind}", request)
    if kind == LegalKind.REFERRAL and case.state != "referred":
        raise LegalError(
            "Refer the case for legal collection first (the board's step on this case)."
        )
    if kind not in (LegalKind.REFERRAL, LegalKind.NOTE) and not is_with_counsel(case):
        raise LegalError("Record the attorney referral first.")
    clean: dict = {}
    for name in TEXT_FIELDS:
        value = (fields.get(name) or "").strip()
        if value:
            clean[name] = value[: LegalEvent._meta.get_field(name).max_length or 5000]
    if fields.get("court"):
        if fields["court"] not in Court.values:
            raise LegalError("Choose the court.")
        clean["court"] = fields["court"]
    if fields.get("lien_filing"):
        if fields["lien_filing"] not in LienFiling.values:
            raise LegalError("Choose what was filed.")
        clean["lien_filing"] = fields["lien_filing"]
    amount = fields.get("amount_cents")
    if amount is not None:
        if amount < 0:
            raise LegalError("Amounts are never negative; record a note to correct one.")
        clean["amount_cents"] = amount
    if fields.get("due_on"):
        clean["due_on"] = fields["due_on"]
    if kind == LegalKind.COST:
        if fields.get("chargeable") is None:
            raise LegalError("Say whether the Declaration lets this cost be charged to the owner.")
        clean["chargeable"] = bool(fields["chargeable"])
    missing = [LABELS[f] for f in REQUIRED[LegalKind(kind)] if clean.get(f) in (None, "")]
    if missing:
        raise LegalError("Add " + ", ".join(missing) + ".")
    if kind in (LegalKind.JUDGMENT, LegalKind.COST, LegalKind.RECOVERY) and not clean.get(
        "amount_cents"
    ):
        raise LegalError("Add the amount.")
    occurred_on = occurred_on or timezone.localdate()
    if occurred_on > timezone.localdate() + datetime.timedelta(days=1):
        raise LegalError("The date it happened cannot be in the future; hearings use their date.")
    with transaction.atomic():
        event = LegalEvent.objects.create(
            case=case, kind=kind, occurred_on=occurred_on, recorded_by=actor, **clean
        )
        record_event(
            "legal.recorded",
            actor=actor,
            request=request,
            target=case,
            metadata={"case_id": case.pk, "kind": kind},
        )
    return event


def post_cost(cost: LegalEvent, *, actor, request=None) -> LedgerEntry:
    """Charge a recorded, chargeable cost to the owner's account: a board member,
    never on their own home, and only as the Declaration allows."""
    from ledger import accounts

    require(actor, "legal.post_costs")
    case = cost.case
    _refuse_own(actor, case, "post_cost", request)
    allowed, citation = rule_with_citation("legal.costs_chargeable")
    if not allowed or not citation:
        raise LegalError(
            "Record in Community settings, with its citation, that the Declaration lets "
            "collection costs be charged to the owner."
        )
    if cost.kind != LegalKind.COST or not cost.chargeable or not cost.amount_cents:
        raise LegalError("Only a cost marked chargeable to the owner can be charged.")
    # The account whose arrears the case is about, even after a sale (BUG-004).
    account_id = accounts.case_account_id(case)
    from ledger.services import LedgerError, _refuse_own_account

    try:
        _refuse_own_account(actor, account_id, "post_cost", request)  # D87
    except LedgerError as exc:
        raise LegalError(str(exc)) from exc
    description = f"Collection costs: {cost.detail} ({citation})"[:200]
    try:
        with transaction.atomic():
            if LegalEvent.objects.unscoped("posted?").filter(cost=cost).exists():
                raise LegalError("This cost is already on the owner's account.")
            entry = LedgerEntry.objects.create(
                lot_id=case.lot_id,
                account_id=account_id,
                posted_on=timezone.localdate(),
                kind=EntryKind.LEGAL_COST,
                amount_cents=cost.amount_cents,
                description=description,
                reference=f"case-{case.pk}",
                posted_by=actor,
            )
            LegalEvent.objects.create(
                case=case,
                kind=LegalKind.COST_POSTED,
                occurred_on=timezone.localdate(),
                amount_cents=cost.amount_cents,
                cost=cost,
                ledger_entry=entry,
                recorded_by=actor,
            )
    except IntegrityError as exc:
        raise LegalError("This cost is already on the owner's account.") from exc
    record_event(
        "legal.cost_posted",
        actor=actor,
        request=request,
        target=entry,
        metadata={"case_id": case.pk, "lot_id": case.lot_id, "amount_cents": cost.amount_cents},
    )
    return entry


def summary(case: CollectionCase, rows: list[LegalEvent] | None = None) -> dict:
    rows = events(case) if rows is None else rows
    posted_costs = {r.cost_id for r in rows if r.kind == LegalKind.COST_POSTED}
    costs = [r for r in rows if r.kind == LegalKind.COST]
    liens = [r for r in rows if r.kind == LegalKind.LIEN]
    filed = [r for r in liens if r.lien_filing in (LienFiling.STATEMENT, LienFiling.JUDGMENT)]
    released = [r for r in rows if r.kind == LegalKind.RELEASE]
    last_filed = filed[-1] if filed else None
    open_lien = bool(
        last_filed and not any(r.occurred_on >= last_filed.occurred_on for r in released)
    )
    today = timezone.localdate()
    hearings = sorted(
        r.due_on for r in rows if r.kind == LegalKind.HEARING and r.due_on and r.due_on >= today
    )
    return {
        "costs_cents": sum(r.amount_cents or 0 for r in costs),
        "chargeable_cents": sum(r.amount_cents or 0 for r in costs if r.chargeable),
        "posted_cents": sum(r.amount_cents or 0 for r in rows if r.kind == LegalKind.COST_POSTED),
        "recovered_cents": sum(r.amount_cents or 0 for r in rows if r.kind == LegalKind.RECOVERY),
        "unposted": [r for r in costs if r.chargeable and r.pk not in posted_costs],
        "open_lien": open_lien,
        "lien": last_filed,
        "next_hearing": hearings[0] if hearings else None,
        "referral": next((r for r in rows if r.kind == LegalKind.REFERRAL), None),
    }


def release_due(case: CollectionCase, info: dict | None = None) -> bool:
    """A filed lien, not yet released, on a case whose balance is paid."""
    from ledger.services import balance

    info = info or summary(case)
    return info["open_lien"] and balance(case.lot_id) <= 0


def my_items(user) -> list:
    """Board: file the release of a paid lien; hearings in the next 14 days."""
    from django.urls import reverse

    from core.dates import short_date
    from core.my_items import Item

    if not can(user, "legal.view"):
        return []
    out = []
    today = timezone.localdate()
    with system_access("legal my items"):
        case_ids = set(LegalEvent.objects.values_list("case_id", flat=True))
        cases = list(CollectionCase.objects.filter(pk__in=case_ids).select_related("lot"))
    for case in cases:
        if conflicts.own_matter(user, case):
            continue
        info = summary(case)
        url = reverse("collection_detail", args=[case.pk]) + "#legal"
        if release_due(case, info):
            out.append(
                Item(
                    "Collections",
                    f"File the lien release for {case.lot.label}",
                    "The account is paid: ask counsel to record the satisfaction or release, "
                    "then record it on the case",
                    url,
                    "warning",
                    nav="collections",
                )
            )
        hearing = info["next_hearing"]
        if hearing and hearing <= today + datetime.timedelta(days=14):
            out.append(
                Item(
                    "Collections",
                    f"Hearing for {case.lot.label}",
                    f"Hearing on {short_date(hearing)}",
                    url,
                    "warning" if hearing > today + datetime.timedelta(days=2) else "danger",
                    hearing,
                    nav="collections",
                )
            )
    return out


def describe(event: LegalEvent) -> str:
    """One line for the timeline (the template shows the fields too)."""
    parts = [event.get_kind_display()]
    if event.firm:
        parts.append(event.firm)
    if event.court:
        parts.append(event.get_court_display())
    if event.lien_filing:
        parts.append(event.get_lien_filing_display())
    if event.amount_cents is not None:
        parts.append(format_cents(event.amount_cents))
    return " · ".join(parts)
