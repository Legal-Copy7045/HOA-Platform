"""Special assessments (D75): a one-off levy for a purpose, such as a capital
project, on top of the monthly charge.

* **Levy** (``levy``, board only): a title and purpose, the total, the
  fund it is designated to (operating or reserve), the board resolution
  (required, recorded in the portal or by reference, as for disbursements,
  D52) and, when the total is above rule
  ``special_assessment.vote_required_cents``, the owners' vote approving it
  (a closed formal vote in the portal, or the reference of one held
  elsewhere; the threshold of ``special_assessment.vote_percent`` is
  recorded with its citation). Each home's share is equal (rule
  ``special_assessment.equal_shares``, the remainder cent by cent in address
  order, ``disbursements.split_largest_remainder``) or a table the board
  enters. It is payable in one sum or in up to 60 installments, each with
  its due date; a home's share is split evenly over them, the odd cents on
  the first.
* **Posting** (``post_due``): on each installment's due date a charge of
  kind ``special_assess`` is posted on every home with a share, named
  "<Title> - installment k of N" (an en dash), dated the due date (so it lands on the
  ownership account of that day) and applied by the open-item allocation
  like any charge. The scheduled task runs daily; a charge is posted at
  most once per home and installment (unique constraint), so re-runs and
  late runs are harmless.
* **Late fees** only when rule ``special_assessment.late_fees`` is on (fixed
  on the levy): an installment still open after ``late_fee.grace_days``
  gets the ``late_fee.cents`` fee once.
* **Notice** (``send_notice``): emailed at once (``comms.notify.send_now``,
  never the weekly digest) to owners who agreed to electronic notice; the
  others are counted for Paper mail.
* **Cancelling** (``cancel_future``): the board stops installments not yet
  posted, with a reason; posted charges are corrected only by a normal
  reversal on the home's account.

The money's fund (``SpecialAssessment.fund``) is where the journal credits
the income (``ledger.accounting.journal_lines``); the owner ledger only knows
the charge.
"""

from __future__ import annotations

import datetime
import itertools

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

import ledger.finance_gaps_audit  # noqa: F401  (registers the audit actions)
from audit.services import record_event
from community.models import Lot, home_sort_key
from community.rules import rule, rule_with_citation
from core.money import format_cents
from core.policy import SYSTEM, can, require
from core.scoping import internal, system_access
from ledger.books.periods import open_date
from ledger.models import (
    EntryKind,
    LedgerEntry,
    SpecialAllocation,
    SpecialAssessment,
    SpecialAssessmentCancel,
    SpecialAssessmentCharge,
    SpecialAssessmentFund,
    SpecialAssessmentInstallment,
    SpecialAssessmentLateFee,
    SpecialAssessmentNotice,
    SpecialAssessmentShare,
)

EQUAL_RULE = "special_assessment.equal_shares"
VOTE_CENTS_RULE = "special_assessment.vote_required_cents"
VOTE_PERCENT_RULE = "special_assessment.vote_percent"
LATE_FEE_RULE = "special_assessment.late_fees"
MAX_INSTALLMENTS = 60
MAX_TOTAL_CENTS = 100_000_000_00  # $100 million: anything above is a typing mistake
DASH = "\N{EN DASH}"
_LOCK = 5_750_001  # pg advisory lock namespace: one assessment's posting


class SpecialAssessmentError(ValueError):
    """A step the service refuses (shown on the page)."""


def _user(actor):
    return None if actor is SYSTEM else actor


def _lock(key: int) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK, key])


# --- Splitting ------------------------------------------------------------------------------


def homes() -> list[Lot]:
    """Every home, in address order (each pays a share, owned or not)."""
    return sorted(Lot.objects.unscoped("special assessment homes"), key=home_sort_key)


def equal_shares(total_cents: int, lots: list[Lot]) -> dict[int, int]:
    """Lot id -> share: equal, the odd cents one each in address order."""
    from ledger.disbursements import split_largest_remainder

    amounts = split_largest_remainder(total_cents, [1] * len(lots))
    return {lot.pk: cents for lot, cents in zip(lots, amounts, strict=True)}


def installment_amounts(share_cents: int, count: int) -> list[int]:
    """A home's share over ``count`` installments: even, the odd cents on
    the first (so later installments are the round figure)."""
    base, rest = divmod(share_cents, count)
    return [base + rest] + [base] * (count - 1)


def vote_needed(total_cents: int) -> tuple[bool, int | None, int | None, str]:
    """(needs an owners' vote, the threshold, the percent, citations)."""
    threshold, cite = rule_with_citation(VOTE_CENTS_RULE)
    percent, pcite = rule_with_citation(VOTE_PERCENT_RULE)
    needed = threshold is not None and total_cents > threshold
    citation = "; ".join(c for c in (cite, pcite) if c)
    return needed, threshold, percent, citation


def _clean(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


# --- Levy --------------------------------------------------------------------------------


def levy(
    *,
    title: str,
    purpose: str,
    total_cents: int,
    due_dates: list[datetime.date],
    fund: str = SpecialAssessmentFund.RESERVE,
    resolution=None,
    resolution_ref: str = "",
    vote=None,
    vote_ref: str = "",
    custom: dict[int, int] | None = None,
    actor,
    request=None,
) -> SpecialAssessment:
    """Levy a special assessment (board only). Installments already due
    (only for SYSTEM, the demo data) post at once."""
    if actor is not SYSTEM:
        require(actor, "special_assessment.levy")
    title = _clean(title, 100)
    purpose = (purpose or "").strip()[:4000]
    if not title or not purpose:
        raise SpecialAssessmentError("A special assessment needs a title and its purpose.")
    if not isinstance(total_cents, int) or not 0 < total_cents <= MAX_TOTAL_CENTS:
        raise SpecialAssessmentError("Enter the total to levy, more than zero.")
    if fund not in SpecialAssessmentFund.values:
        raise SpecialAssessmentError("Choose the fund the money is for.")
    dates = list(due_dates or [])
    if not 1 <= len(dates) <= MAX_INSTALLMENTS:
        raise SpecialAssessmentError(f"Give 1 to {MAX_INSTALLMENTS} installment due dates.")
    if any(b <= a for a, b in itertools.pairwise(dates)):
        raise SpecialAssessmentError("Each installment is due after the one before it.")
    today = timezone.localdate()
    if actor is not SYSTEM and dates[0] < today:
        raise SpecialAssessmentError("An installment cannot be due before today.")
    resolution_ref = _clean(resolution_ref, 120)
    if resolution is None and not resolution_ref:
        raise SpecialAssessmentError(
            "A special assessment needs the board resolution levying it: choose it, or type "
            "its reference."
        )
    needed, _threshold, percent, vote_cite = vote_needed(total_cents)
    vote_ref = _clean(vote_ref, 120)
    if vote is not None and vote.status != "closed":
        raise SpecialAssessmentError("Link the owners' vote once it has closed.")
    if needed and vote is None and not vote_ref:
        raise SpecialAssessmentError(
            f"Above {format_cents(_threshold)} the owners must approve a special assessment"
            + (f" ({vote_cite})" if vote_cite else "")
            + ": link the closed vote, or type the reference of the vote held elsewhere."
        )
    lots = homes()
    equal = bool(rule(EQUAL_RULE))
    if equal:
        if custom:
            raise SpecialAssessmentError(
                "Shares are equal per home under the community's rule; switch the rule off to "
                "enter each home's amount."
            )
        shares = equal_shares(total_cents, lots)
    else:
        if not custom:
            raise SpecialAssessmentError("Enter each home's amount.")
        known = {lot.pk for lot in lots}
        if set(custom) - known or any(not isinstance(v, int) or v < 0 for v in custom.values()):
            raise SpecialAssessmentError("Each amount is a home's share, zero or more.")
        if sum(custom.values()) != total_cents:
            raise SpecialAssessmentError(
                f"The homes' amounts add up to {format_cents(sum(custom.values()))}, not the "
                f"total of {format_cents(total_cents)}."
            )
        shares = {lot.pk: custom.get(lot.pk, 0) for lot in lots}
    _eq, eq_cite = rule_with_citation(EQUAL_RULE)
    late, late_cite = rule_with_citation(LATE_FEE_RULE)
    citation = "; ".join(c for c in (eq_cite, vote_cite if needed else "", late_cite) if c)
    with transaction.atomic():
        sa = SpecialAssessment.objects.create(
            title=title,
            purpose=purpose,
            total_cents=total_cents,
            allocation=SpecialAllocation.EQUAL if equal else SpecialAllocation.CUSTOM,
            installments=len(dates),
            fund=fund,
            resolution=resolution,
            resolution_ref=resolution_ref,
            vote_required=needed,
            vote=vote,
            vote_ref=vote_ref,
            vote_threshold_percent=percent if needed else None,
            late_fees=bool(late),
            rule_citation=citation[:400],
            levied_by=_user(actor),
        )
        SpecialAssessmentShare.objects.bulk_create(
            [
                SpecialAssessmentShare(assessment=sa, lot_id=lot_id, amount_cents=cents)
                for lot_id, cents in shares.items()
                if cents > 0
            ]
        )
        SpecialAssessmentInstallment.objects.bulk_create(
            [
                SpecialAssessmentInstallment(assessment=sa, number=i, due_on=d)
                for i, d in enumerate(dates, start=1)
            ]
        )
        record_event(
            "special_assessment.levied",
            actor=_user(actor),
            request=request,
            target=sa,
            metadata={
                "assessment_id": sa.pk,
                "amount_cents": total_cents,
                "homes": sum(1 for c in shares.values() if c > 0),
                "installments": len(dates),
                "allocation": sa.allocation,
                "fund": fund,
                "vote_required": needed,
            },
        )
    post_due(today=today, assessment=sa)
    return sa


# --- Status ---------------------------------------------------------------------------------


def cancelled_from(sa: SpecialAssessment) -> int | None:
    """The first cancelled installment number, or None."""
    numbers = [c.from_number for c in SpecialAssessmentCancel.objects.filter(assessment=sa)]
    return min(numbers) if numbers else None


def _posted_numbers(sa: SpecialAssessment) -> set[int]:
    with system_access("special assessment posted"):
        return set(
            SpecialAssessmentCharge.objects.filter(installment__assessment=sa).values_list(
                "installment__number", flat=True
            )
        )


@internal("special assessment status")
def installments(sa: SpecialAssessment) -> list[dict]:
    """Each installment: due date, total for all homes, status (posted,
    scheduled or cancelled) and, once posted, how much is still open."""
    from ledger import allocation

    stop = cancelled_from(sa)
    shares = list(SpecialAssessmentShare.objects.filter(assessment=sa))
    rows = []
    for inst in SpecialAssessmentInstallment.objects.filter(assessment=sa).order_by("number"):
        total = sum(
            installment_amounts(s.amount_cents, sa.installments)[inst.number - 1] for s in shares
        )
        charges = list(
            SpecialAssessmentCharge.objects.filter(installment=inst).select_related("entry")
        )
        open_cents = None
        if charges:
            open_cents = 0
            books: dict[int, object] = {}
            for c in charges:
                book = books.get(c.lot_id) or allocation.load_book(c.lot_id)
                books[c.lot_id] = book
                open_cents += book.remaining(c.entry_id)
            status = "posted"
        elif stop is not None and inst.number >= stop:
            status = "cancelled"
        else:
            status = "scheduled"
        rows.append(
            {
                "installment": inst,
                "total_cents": total,
                "status": status,
                "charges": len(charges),
                "open_cents": open_cents,
                "paid_cents": (sum(c.entry.amount_cents for c in charges) - open_cents)
                if charges
                else 0,
            }
        )
    return rows


@internal("special assessment installments by month")
def due_by_month(first: datetime.date, last: datetime.date) -> dict[tuple[int, int, str], int]:
    """(year, month, fund) -> all homes' installments due that month, for
    installments from ``first``'s month to ``last``'s month that are not
    cancelled (the cash-flow forecast, D81)."""
    start = first.replace(day=1)
    out: dict[tuple[int, int, str], int] = {}
    rows = SpecialAssessmentInstallment.objects.filter(
        due_on__gte=start, due_on__lt=_month_after(last)
    ).select_related("assessment")
    stops: dict[int, int | None] = {}
    share_totals: dict[int, list[int]] = {}
    for inst in rows:
        sa = inst.assessment
        if sa.pk not in stops:
            stops[sa.pk] = cancelled_from(sa)
            per = [0] * sa.installments
            for share in SpecialAssessmentShare.objects.filter(assessment=sa):
                for i, cents in enumerate(installment_amounts(share.amount_cents, sa.installments)):
                    per[i] += cents
            share_totals[sa.pk] = per
        stop = stops[sa.pk]
        if stop is not None and inst.number >= stop:
            continue
        key = (inst.due_on.year, inst.due_on.month, sa.fund)
        out[key] = out.get(key, 0) + share_totals[sa.pk][inst.number - 1]
    return out


def _month_after(day: datetime.date) -> datetime.date:
    return (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


@internal("special assessment shares")
def shares(sa: SpecialAssessment) -> list[SpecialAssessmentShare]:
    rows = list(SpecialAssessmentShare.objects.filter(assessment=sa).select_related("lot"))
    rows.sort(key=lambda s: home_sort_key(s.lot))
    return rows


def summary(sa: SpecialAssessment) -> dict:
    rows = installments(sa)
    posted = [r for r in rows if r["status"] == "posted"]
    return {
        "installments": rows,
        "posted_cents": sum(r["total_cents"] for r in posted),
        "open_cents": sum(r["open_cents"] or 0 for r in posted),
        "paid_cents": sum(r["paid_cents"] for r in posted),
        "cancelled_from": cancelled_from(sa),
        "next": next((r for r in rows if r["status"] == "scheduled"), None),
        "notice": SpecialAssessmentNotice.objects.filter(assessment=sa).first(),
        "cancels": list(SpecialAssessmentCancel.objects.filter(assessment=sa)),
    }


def describe(sa: SpecialAssessment, number: int) -> str:
    """The charge's name: "<Title> - installment k of N" (an en dash)."""
    return f"{sa.title} {DASH} installment {number} of {sa.installments}"[:200]


# --- Posting ----------------------------------------------------------------------------


@internal("post special assessment installments")
def post_due(
    *,
    today: datetime.date | None = None,
    assessment: SpecialAssessment | None = None,
    actor=SYSTEM,
    request=None,
) -> int:
    """Post every installment due on or before ``today`` that is not posted
    yet (scheduled task, daily; or "Post due installments now"). Returns the
    number of charges posted."""
    if actor is not SYSTEM:
        require(actor, "ledger.manage")
    today = today or timezone.localdate()
    targets = [assessment] if assessment is not None else list(SpecialAssessment.objects.all())
    created = 0
    for sa in targets:
        stop = cancelled_from(sa)
        due = SpecialAssessmentInstallment.objects.filter(assessment=sa, due_on__lte=today)
        if stop is not None:
            due = due.filter(number__lt=stop)
        share_rows = list(SpecialAssessmentShare.objects.filter(assessment=sa))
        for inst in due.order_by("number"):
            done = set(
                SpecialAssessmentCharge.objects.filter(installment=inst).values_list(
                    "lot_id", flat=True
                )
            )
            todo = [s for s in share_rows if s.lot_id not in done]
            if not todo:
                continue
            count = cents_total = 0
            with transaction.atomic():
                _lock(sa.pk)
                if cancelled_from(sa) is not None and inst.number >= cancelled_from(sa):
                    continue
                for share in todo:
                    cents = installment_amounts(share.amount_cents, sa.installments)[
                        inst.number - 1
                    ]
                    if cents <= 0:
                        continue
                    try:
                        with transaction.atomic():
                            entry = LedgerEntry.objects.create(
                                lot_id=share.lot_id,
                                # Never into a closed month (D70): a late
                                # catch-up posts in the open month.
                                posted_on=open_date(inst.due_on),
                                kind=EntryKind.SPECIAL_ASSESSMENT,
                                amount_cents=cents,
                                description=describe(sa, inst.number),
                                reference=f"SA{sa.pk}-{inst.number}"[:40],
                                posted_by=_user(actor),
                            )
                            SpecialAssessmentCharge.objects.create(
                                installment=inst, lot_id=share.lot_id, entry=entry
                            )
                    except IntegrityError:
                        continue  # posted meanwhile: once per home and installment
                    count += 1
                    cents_total += cents
            if count:
                created += count
                record_event(
                    "special_assessment.installments_posted",
                    actor=_user(actor),
                    request=request,
                    target=sa,
                    metadata={
                        "assessment_id": sa.pk,
                        "installment": inst.number,
                        "charges": count,
                        "amount_cents": cents_total,
                        "automatic": actor is SYSTEM,
                    },
                )
    return created


# --- Cancelling future installments ------------------------------------------------------


def cancel_future(
    sa: SpecialAssessment, *, from_number: int, reason: str, actor, request=None
) -> SpecialAssessmentCancel:
    """Stop installments ``from_number`` onwards (none of them posted)."""
    require(actor, "special_assessment.levy")
    reason = _clean(reason, 300)
    if not reason:
        raise SpecialAssessmentError("Give the reason for cancelling the installments.")
    with transaction.atomic():
        _lock(sa.pk)
        posted = _posted_numbers(sa)
        if not 1 <= from_number <= sa.installments:
            raise SpecialAssessmentError("Choose an installment of this assessment.")
        if posted and from_number <= max(posted):
            raise SpecialAssessmentError(
                f"Installment {max(posted)} is already posted; cancel from installment "
                f"{max(posted) + 1}. A posted charge is corrected only by a reversal on the "
                "home's account."
            )
        stop = cancelled_from(sa)
        if stop is not None and from_number >= stop:
            raise SpecialAssessmentError("Those installments are already cancelled.")
        row = SpecialAssessmentCancel.objects.create(
            assessment=sa, from_number=from_number, reason=reason, cancelled_by=actor
        )
    record_event(
        "special_assessment.cancelled",
        actor=actor,
        request=request,
        target=sa,
        metadata={"assessment_id": sa.pk, "from_installment": from_number},
    )
    return row


# --- Owner notice ------------------------------------------------------------------------


def notice_text(sa: SpecialAssessment) -> tuple[str, str]:
    """The notice's subject and body (no personal data: the same for all)."""
    rows = installments(sa)
    equal = sa.allocation == SpecialAllocation.EQUAL
    with system_access("special assessment notice"):
        first = SpecialAssessmentShare.objects.filter(assessment=sa).first()
    per_home = first.amount_cents if (equal and first) else None
    lines = [
        f"The board has levied a special assessment: {sa.title}.",
        "",
        sa.purpose,
        "",
        f"Total for the community: {format_cents(sa.total_cents)}.",
    ]
    if per_home is not None:
        lines.append(f"Each home's share: {format_cents(per_home)} (the same for every home).")
    else:
        lines.append("Each home's share is shown on your account page in the portal.")
    if sa.installments == 1:
        lines.append(f"Due on {rows[0]['installment'].due_on:%B %-d, %Y}.")
    else:
        lines.append(f"Payable in {sa.installments} installments, due on:")
        for r in rows:
            inst = r["installment"]
            amount = ""
            if per_home is not None:
                cents = installment_amounts(per_home, sa.installments)[inst.number - 1]
                amount = f" ({format_cents(cents)})"
            lines.append(f"  {inst.number}. {inst.due_on:%B %-d, %Y}{amount}")
    lines += ["", f"Board resolution: {_resolution_label(sa)}."]
    if sa.vote_required:
        lines.append(f"Approved by the owners: {_vote_label(sa)}.")
    if sa.rule_citation:
        lines.append(f"Authority: {sa.rule_citation}.")
    lines += [
        "",
        "Each installment appears on your account on its due date and can be paid like the "
        "monthly charge.",
        f"Your account: {settings.PORTAL_BASE_URL}{reverse('my_account')}",
        "",
        f"-- \n{settings.SITE_NAME}",
    ]
    return f"[{settings.SITE_NAME}] Special assessment: {sa.title}", "\n".join(lines)


def _resolution_label(sa: SpecialAssessment) -> str:
    if sa.resolution_id:
        return f"{sa.resolution.number}: {sa.resolution.title}"
    return sa.resolution_ref


def _vote_label(sa: SpecialAssessment) -> str:
    if sa.vote_id:
        return sa.vote.title
    return sa.vote_ref


def send_notice(sa: SpecialAssessment, *, actor, request=None) -> dict:
    """Email the notice now to owners who agreed to electronic notice (a
    legal notice: never the digest); count the homes needing paper."""
    require(actor, "special_assessment.levy")
    from comms import notify
    from community.services import owner_notice_recipients

    if SpecialAssessmentNotice.objects.filter(assessment=sa).exists():
        raise SpecialAssessmentError("The notice was already sent.")
    subject, body = notice_text(sa)
    emails, paper = owner_notice_recipients()
    notify.send_now(emails, subject=subject, body=body)
    SpecialAssessmentNotice.objects.create(
        assessment=sa, emailed=len(emails), paper=len(paper), sent_by=actor
    )
    record_event(
        "special_assessment.notice_sent",
        actor=actor,
        request=request,
        target=sa,
        metadata={"assessment_id": sa.pk, "emailed": len(emails), "paper": len(paper)},
    )
    return {"emailed": len(emails), "paper": len(paper)}


# --- Late fees ----------------------------------------------------------------------------


@internal("special assessment late fees")
def late_fee_candidates(on: datetime.date | None = None) -> list[dict]:
    """Posted installments still open after the late-fee grace period, on
    assessments levied with late fees, without their fee yet."""
    from ledger import allocation

    on = on or timezone.localdate()
    fee = rule("late_fee.cents")
    grace = rule("late_fee.grace_days")
    if not fee or grace is None:
        return []
    late_from = on - datetime.timedelta(days=int(grace) + 1)
    done = set(SpecialAssessmentLateFee.objects.values_list("installment_id", "lot_id"))
    out = []
    for c in (
        SpecialAssessmentCharge.objects.filter(
            installment__assessment__late_fees=True, entry__posted_on__lte=late_from
        )
        .select_related("installment__assessment", "entry", "lot")
        .order_by("id")
    ):
        if (c.installment_id, c.lot_id) in done:
            continue
        if allocation.load_book(c.lot_id).remaining(c.entry_id) <= 0:
            continue
        out.append({"charge": c, "lot": c.lot, "account_id": c.entry.account_id})
    return out


@internal("special assessment late fees")
def post_late_fees(*, actor, request=None, on: datetime.date | None = None) -> int:
    """Post the late fee on each late installment (once). The board by
    button; SYSTEM only when late fees post automatically."""
    if actor is SYSTEM:
        if not rule("late_fee.auto_apply"):
            return 0
    else:
        require(actor, "late_fee.post")
    if not settings.LEDGER_LATE_FEES_ENABLED:
        raise SpecialAssessmentError(
            "Late fees are switched off on this server (LEDGER_LATE_FEES_ENABLED)."
        )
    fee, citation = rule_with_citation("late_fee.cents")
    if not fee:
        return 0
    from core import conflicts

    created = 0
    for row in late_fee_candidates(on):
        c = row["charge"]
        if actor is not SYSTEM and conflicts.own_matter(actor, c.lot):
            continue  # own matters (D65): another board member posts it
        try:
            with transaction.atomic():
                entry = LedgerEntry.objects.create(
                    lot_id=c.lot_id,
                    account_id=row["account_id"],
                    posted_on=on or timezone.localdate(),
                    kind=EntryKind.LATE_FEE,
                    amount_cents=fee,
                    description=(
                        f"Late fee {DASH} {c.installment.assessment.title}, installment "
                        f"{c.installment.number}" + (f" ({citation})" if citation else "")
                    )[:200],
                    posted_by=_user(actor),
                )
                SpecialAssessmentLateFee.objects.create(
                    installment=c.installment, lot_id=c.lot_id, entry=entry
                )
        except IntegrityError:
            continue
        created += 1
    if created:
        record_event(
            "special_assessment.late_fees_posted",
            actor=_user(actor),
            request=request,
            metadata={
                "charges": created,
                "amount_cents": fee * created,
                "automatic": actor is SYSTEM,
            },
        )
    return created


# --- Owner view ---------------------------------------------------------------------------


@internal("special assessments for an owner")
def for_lot(lot_id: int) -> list[dict]:
    """The special assessments with a share for this home: the home's share,
    each installment's amount, due date and status for the owner (posted
    charges are on the account like any charge)."""
    from ledger import allocation

    out = []
    for share in SpecialAssessmentShare.objects.filter(lot_id=lot_id).select_related("assessment"):
        sa = share.assessment
        stop = cancelled_from(sa)
        posted = {
            c.installment_id: c
            for c in SpecialAssessmentCharge.objects.filter(
                installment__assessment=sa, lot_id=lot_id
            )
        }
        book = allocation.load_book(lot_id) if posted else None
        rows = []
        for inst in SpecialAssessmentInstallment.objects.filter(assessment=sa).order_by("number"):
            amount = installment_amounts(share.amount_cents, sa.installments)[inst.number - 1]
            charge = posted.get(inst.pk)
            if charge is not None:
                status = "paid" if book.remaining(charge.entry_id) <= 0 else "due"
            elif stop is not None and inst.number >= stop:
                status = "cancelled"
            else:
                status = "upcoming"
            rows.append(
                {
                    "number": inst.number,
                    "due_on": inst.due_on,
                    "amount_cents": amount,
                    "status": status,
                }
            )
        out.append({"assessment": sa, "share_cents": share.amount_cents, "installments": rows})
    return out


# --- Board My items -----------------------------------------------------------------------


def board_items(user) -> list:
    """Special assessments whose owner notice has not been sent."""
    from core.my_items import Item

    if not can(user, "special_assessment.levy"):
        return []
    items = []
    sent = set(SpecialAssessmentNotice.objects.values_list("assessment_id", flat=True))
    for sa in SpecialAssessment.objects.exclude(pk__in=sent)[:20]:
        items.append(
            Item(
                "Finance",
                f"Send the owner notice for the special assessment {sa.title}",
                "Owners who agreed to email get it at once; the others need a paper copy.",
                reverse("special_assessment_detail", args=[sa.pk]),
                "warning",
                nav="special_assessments",
            )
        )
    return items
