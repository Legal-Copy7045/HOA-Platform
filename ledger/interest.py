"""Interest on overdue balances (D80, optional feature "overdue_interest").

Only where the Declaration allows it: nothing is posted until the board has
recorded the yearly rate with its citation (rule ``interest.annual_rate_bp``)
and switched the feature on. Each month, on the posting day, every ownership
account gets at most one append-only "interest" charge (one per account and
month, enforced by a database constraint), worked out as

    simple interest = past-due principal x yearly rate / 12, rounded half up

on the open part of the {term}s that are more than ``interest.grace_days``
past due on the posting day. Late fees and earlier interest are not part of
the principal unless ``interest.include_fees`` is on; interest is never
compounded otherwise. Nothing is charged below ``interest.min_balance_cents``,
on an account whose home has a payment plan in force (``interest.
suspend_on_payment_plan``), on disputed charges, or on a dormant (sold)
account. The calculation is the entry's description, so the statement line
says it: "Interest 1.5%/mo on $450.00 past due 31+ days (Declaration §6.6)".

Interest charges are ordinary open charges for payments (ledger.allocation).
Reversing one is a board decision with a reason (``reverse_interest``; the
generic reversal refuses the manager too, ledger.services.reverse_entry).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from fractions import Fraction

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit import actions as audit_actions
from audit.services import record_event
from community.models import Lot
from community.rules import rule, rule_with_citation
from community.terms import term_text
from core import conflicts, features
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import system_access
from ledger.models import EntryKind, LedgerEntry

FEATURE = "overdue_interest"

for _name, _text, _fields in (
    (
        "ledger.interest_posted",
        "Interest on overdue balances posted",
        ("period", "accounts", "total_cents", "automatic", "skipped_own"),
    ),
    ("ledger.interest_reversed", "Interest charge reversed", ("lot_id", "reverses")),
):
    audit_actions.register(audit_actions.ActionSpec(_name, _text, frozenset(_fields)))


class InterestError(ValueError):
    """A refusal the page shows."""


# --- Sources in other areas ------------------------------------------------------------


def on_payment_plan(lot_id: int) -> bool:
    """A payment plan in force with its schedule kept (D76,
    ``ledger.payment_plans.plan_kept``): approved, not ended, and no
    installment missed."""
    from ledger import payment_plans

    return payment_plans.plan_kept(lot_id)


def disputed_charge_ids(lot_id: int) -> set[int]:
    """Charges an owner disputes, never charged interest. No dispute flag exists
    on this base; switch when one is added."""
    return set()


def principal_kinds(include_fees: bool) -> set[str]:
    """Kinds of charge whose past-due part earns interest: the monthly
    assessment and special assessment installments (D75), and with
    ``include_fees`` late fees and interest."""
    kinds = {EntryKind.ASSESSMENT, EntryKind.SPECIAL_ASSESSMENT}
    if include_fees:
        kinds |= {EntryKind.LATE_FEE, EntryKind.INTEREST}
    return {str(k) for k in kinds}


# --- The policy and the arithmetic ---------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    rate_bp: int | None
    citation: str
    max_bp: int | None
    grace_days: int
    min_cents: int
    include_fees: bool
    posting_day: int
    suspend_on_plan: bool

    @property
    def problem(self) -> str:
        """Why nothing can be posted, or "" when it can."""
        if not self.rate_bp:
            return "Record the yearly interest rate from the Declaration in Community settings."
        if not self.citation.strip():
            return "The interest rate needs its citation (the Declaration section)."
        if self.max_bp is not None and self.rate_bp > self.max_bp:
            return "The interest rate is above the highest rate the Declaration allows."
        return ""


def current_policy() -> Policy:
    rate, citation = rule_with_citation("interest.annual_rate_bp")
    return Policy(
        rate_bp=rate,
        citation=citation or "",
        max_bp=rule("interest.max_annual_rate_bp"),
        grace_days=rule("interest.grace_days") or 0,
        min_cents=rule("interest.min_balance_cents") or 0,
        include_fees=bool(rule("interest.include_fees")),
        posting_day=min(max(rule("interest.posting_day") or 1, 1), 28),
        suspend_on_plan=bool(rule("interest.suspend_on_payment_plan")),
    )


def interest_cents(principal_cents: int, annual_bp: int) -> int:
    """One month of simple interest, rounded half up to the cent."""
    if principal_cents <= 0 or annual_bp <= 0:
        return 0
    value = Fraction(principal_cents * annual_bp, 10000 * 12)
    return int(value.numerator * 2 + value.denominator) // (2 * value.denominator)


def monthly_rate_text(annual_bp: int) -> str:
    """The monthly rate as people read it: 1800 -> "1.5", 1000 -> "0.833"."""
    value = Fraction(annual_bp, 1200)  # percent per month
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text


def describe(principal_cents: int, policy: Policy) -> str:
    text = (
        f"Interest {monthly_rate_text(policy.rate_bp)}%/mo on {format_cents(principal_cents)} "
        f"past due {policy.grace_days + 1}+ days"
    )
    if policy.citation:
        text += f" ({policy.citation})"
    return text[:200]


@dataclass
class Candidate:
    lot: Lot
    account_id: int
    principal_cents: int
    interest_cents: int
    description: str


def posting_date(year: int, month: int, policy: Policy | None = None) -> datetime.date:
    policy = policy or current_policy()
    return datetime.date(year, month, policy.posting_day)


def principal_for(lot_id: int, account_id: int, on: datetime.date, policy: Policy) -> int:
    """The past-due principal of one account on ``on``."""
    from ledger import allocation

    state = allocation.state_as_of(lot_id, on, account=account_id)
    kinds = principal_kinds(policy.include_fees)
    disputed = disputed_charge_ids(lot_id)
    cutoff = on - datetime.timedelta(days=policy.grace_days)
    total = 0
    for pk, open_cents in state.open.items():
        entry = state.entries[pk]
        if (
            open_cents > 0
            and entry.kind in kinds
            and entry.posted_on < cutoff
            and pk not in disputed
            and entry.reverses_id is None
        ):
            total += open_cents
    return total


def candidates(*, year: int, month: int, policy: Policy | None = None) -> list[Candidate]:
    """Accounts that would be charged interest for this month, not yet charged."""
    from community.models import Account
    from ledger.accounts import account_on

    policy = policy or current_policy()
    if policy.problem:
        return []
    on = posting_date(year, month, policy)
    period = f"{year:04d}-{month:02d}"
    out = []
    with system_access("interest candidates"):
        done = set(
            LedgerEntry.objects.filter(kind=EntryKind.INTEREST, period=period).values_list(
                "account_id", flat=True
            )
        )
        open_accounts = set(
            Account.objects.filter(closed_on__isnull=True).values_list("pk", flat=True)
        )
        lot_ids = set(
            LedgerEntry.objects.filter(kind=EntryKind.ASSESSMENT).values_list("lot_id", flat=True)
        )
        lots = {lot.pk: lot for lot in Lot.objects.filter(pk__in=lot_ids)}
        for lot in sorted(lots.values(), key=lambda lot_: lot_.sort_key):
            account_id = account_on(lot.pk, on)
            if account_id is None or account_id in done or account_id not in open_accounts:
                continue
            if policy.suspend_on_plan and on_payment_plan(lot.pk):
                continue
            principal = principal_for(lot.pk, account_id, on, policy)
            if principal <= 0 or principal < policy.min_cents:
                continue
            cents = interest_cents(principal, policy.rate_bp)
            if cents <= 0:
                continue
            out.append(Candidate(lot, account_id, principal, cents, describe(principal, policy)))
    return out


def post_interest(*, year: int, month: int, actor, request=None) -> int:
    """Post this month's interest (once per account). SYSTEM is the monthly
    task; a board member may post by button. A board member's own home is
    left for another board member or the task (own matters, D65)."""
    if actor is SYSTEM:
        if not features.enabled(FEATURE):
            return 0
    else:
        require(actor, "interest.post")
        if not features.enabled(FEATURE):
            raise InterestError("Interest on overdue balances is switched off.")
    policy = current_policy()
    if policy.problem:
        if actor is SYSTEM:
            return 0
        raise InterestError(policy.problem)
    on = posting_date(year, month, policy)
    if on > timezone.localdate():
        raise InterestError(f"This month's interest is posted on {on:%B} {on.day}, not before.")
    period = f"{year:04d}-{month:02d}"
    created, total, own = 0, 0, 0
    for row in candidates(year=year, month=month, policy=policy):
        if actor is not SYSTEM and conflicts.own_matter(actor, row.lot):
            own += 1
            continue
        try:
            with transaction.atomic():
                LedgerEntry.objects.create(
                    lot=row.lot,
                    account_id=row.account_id,
                    posted_on=on,
                    kind=EntryKind.INTEREST,
                    amount_cents=row.interest_cents,
                    period=period,
                    description=row.description,
                    posted_by=None if actor is SYSTEM else actor,
                )
        except IntegrityError:
            continue  # posted a moment ago by someone else
        created += 1
        total += row.interest_cents
    record_event(
        "ledger.interest_posted",
        actor=None if actor is SYSTEM else actor,
        request=request,
        metadata={
            "period": period,
            "accounts": created,
            "total_cents": total,
            "automatic": actor is SYSTEM,
            "skipped_own": own,
        },
    )
    return created


def run_monthly(today: datetime.date | None = None) -> int:
    """The daily task: on or after the posting day, post this month's interest."""
    today = today or timezone.localdate()
    if not features.enabled(FEATURE):
        return 0
    policy = current_policy()
    if policy.problem or today.day < policy.posting_day:
        return 0
    return post_interest(year=today.year, month=today.month, actor=SYSTEM)


def reverse_interest(entry: LedgerEntry, *, reason: str, actor, request=None) -> LedgerEntry:
    """Board only, with a reason: an append-only reversal of one interest charge."""
    from ledger import services

    require(actor, "interest.reverse")
    if entry.kind != EntryKind.INTEREST:
        raise InterestError("Only an interest charge can be reversed here.")
    conflicts.refuse(actor, entry, area="ledger", action="reverse_interest", request=request)
    if not reason.strip():
        raise InterestError("Give the reason for the reversal.")
    try:
        reversal = services.reverse_entry(entry, reason=reason, actor=actor, request=request)
    except services.LedgerError as exc:
        raise InterestError(str(exc)) from exc
    record_event(
        "ledger.interest_reversed",
        actor=actor,
        request=request,
        target=reversal,
        metadata={"lot_id": entry.lot_id, "reverses": entry.pk},
    )
    return reversal


def recent_postings(limit: int = 50):
    with system_access("interest list"):
        return list(
            LedgerEntry.objects.filter(kind=EntryKind.INTEREST)
            .select_related("lot", "reversed_by")
            .order_by("-posted_on", "-id")[:limit]
        )


def owner_notice(request=None) -> str:
    """The policy in one sentence for owners' account page, or "" when off."""
    if not features.enabled(FEATURE, request):
        return ""
    policy = current_policy()
    if policy.problem:
        return ""
    text = (
        f"Overdue {{terms}} earn simple interest of {monthly_rate_text(policy.rate_bp)}% a month "
        f"({_percent(policy.rate_bp)}% a year) once they are more than {policy.grace_days} days "
        f"past due, posted on the {_ordinal(policy.posting_day)} of each month"
    )
    if not policy.include_fees:
        text += "; never on late fees or earlier interest"
    text += f" ({policy.citation})."
    return term_text(text)


def _percent(bp: int) -> str:
    from community.rules import bp_display

    return bp_display(bp)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
