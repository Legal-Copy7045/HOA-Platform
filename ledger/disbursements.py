"""Disbursements: money the association pays back to owners (D52).

Two kinds, each a batch of one line per ownership account:

* **Surplus distribution**: the board returns a surplus (what is left of a
  fiscal year's assessments after the common expenses and reserves) to the
  owners who paid. The board enters the total; the portal splits it by the
  governing rule ``disbursement.surplus_by_share_paid`` (default: in
  proportion to the assessments each ownership account paid that fiscal
  year, a seller's old account included; off: equal per home with an owner
  today), the remainder cent by cent in address order
  (``split_largest_remainder``). Each share is a statement credit, which the
  open-item rule applies to the next assessments until it is used up; checks
  only with ``disbursement.surplus_cash_allowed`` (D67). A board resolution
  is required (68 Pa.C.S. § 5313; ``community/pa_law.py``).
* **Owner refund**: one account's credit paid back, typically a seller's
  dormant account after a sale (D33-acctfix-1: money stays with its
  ownership account). Never more than the account's credit, checked when
  proposed, when approved and when the check is issued.

Each line is paid by **statement credit** (a credit entry, applied by the
open-item rule to open and future charges; on a seller's dormant account it
waits there to be refunded) or by **check** (refunds, and surplus shares
when the board allows checks). The portal never knows bank
account numbers: checks are written in the bank's bill pay or accounting
package from the check register, or printed on the association's
pre-printed check stock (the stock carries the bank's numbers).

When money moves (append-only records only, CLAUDE.md rule 5):

* proposing posts nothing;
* the final approval posts the statement credits (one ``credit`` entry per
  line, "Surplus distribution credit - 2026", with an en dash);
* recording a check as issued posts, for a refund, a ``refund`` entry of the
  check's amount on the account ("Refund by check #1042"), which takes up
  the credit; a surplus check changes no owner account (the owner owes
  exactly what they owed) and is booked straight from the fund to the bank;
* voiding a check reverses its refund entry, so the owner's credit is open
  again and a new check can be written; clearing only records the bank date.

Approvals (``approve``): the proposer (board member or manager) never
approves; a different board member must, two at or over
``disbursement.second_approval_cents``. An owner on a refunded account may
not approve that refund. A surplus distribution pays every owner, board
members included, so a board member who receives part of it may approve,
but the batch then needs two approvals besides the proposer and each is
recorded as a recipient's approval.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
from dataclasses import dataclass, field

from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.models import Account, Ownership, home_sort_key
from community.rules import rule, rule_with_citation
from core import conflicts, pdf
from core.lists import safe_cell
from core.money import format_cents
from core.policy import BOARD, SYSTEM, can, require, roles_for
from core.scoping import internal, system_access
from ledger.models import (
    CheckEventKind,
    CheckPrintSettings,
    DisbursementBatch,
    DisbursementCheck,
    DisbursementCheckEvent,
    DisbursementDecision,
    DisbursementDecisionKind,
    DisbursementEntry,
    DisbursementKind,
    DisbursementLine,
    DisbursementMethod,
    EntryKind,
    LedgerEntry,
    ReconciledItem,
)

# The split and how it is paid (D67): in proportion to the assessments each
# owner paid in the fiscal year (off: equal per home), and statement credits
# only unless the board allows checks.
SHARE_PAID_RULE = "disbursement.surplus_by_share_paid"
CASH_RULE = "disbursement.surplus_cash_allowed"
# Whose a share is (D67): the home's (credited to its account today, the
# default) or the ownership account that paid it (a seller's old account).
HOME_RULE = "disbursement.surplus_stays_with_home"
SECOND_APPROVAL_RULE = "disbursement.second_approval_cents"
STALE_DAYS = 90  # an uncashed check gets a board item after this long
MAX_TOTAL_CENTS = 100_000_000_00  # $100 million: anything above is a typing mistake
_BATCH_LOCK = 5_520_001  # pg advisory lock namespace: one batch's decisions and checks
_CHECK_LOCK = 5_520_002  # check numbers, clearing and voiding
UNCLAIMED_NOTE = (
    "Pennsylvania's unclaimed property law applies to checks that stay uncashed: after "
    "the dormancy period the money is reported and paid to the PA Treasury. Have the "
    "treasurer confirm the period and the reporting steps before voiding or reissuing."
)


class DisbursementError(ValueError):
    """A step the service refuses (shown on the page)."""


def _user(actor):
    return None if actor is SYSTEM else actor


def _lock(namespace: int, key: int) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [namespace, key])


def _lock_lot(lot_id: int) -> None:
    """The lot's allocation lock (ledger.allocation): no entry posts on the
    lot while a credit is checked and used."""
    from ledger import allocation

    allocation._lock(allocation._LOCK_NAMESPACE, lot_id)


# --- Splitting a total -----------------------------------------------------------------


def split_largest_remainder(total_cents: int, weights: list[int]) -> list[int]:
    """``total_cents`` split in proportion to ``weights``, in whole cents,
    adding up exactly (the largest remainder method).

    Each share is first rounded down; the cents left over go one each to the
    shares with the largest remainders. Ties (always the case with equal
    weights) go to the earlier position, so callers pass the homes in
    address order and the result is the same every time."""
    if total_cents < 0 or not weights or any(w < 0 for w in weights) or sum(weights) <= 0:
        raise DisbursementError("Nothing to split.")
    whole = sum(weights)
    shares = [total_cents * w // whole for w in weights]
    remainders = [total_cents * w % whole for w in weights]
    left = total_cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:left]:
        shares[i] += 1
    return shares


# --- Amounts in words (checks) ------------------------------------------------------------

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _under_thousand(n: int) -> list[str]:
    words = []
    if n >= 100:
        words += [_ONES[n // 100], "hundred"]
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        words.append(_ONES[n])
    return words


def amount_in_words(cents: int) -> str:
    """ "One thousand two hundred thirty-four and 56/100", as written on a
    check (dollars in words, cents as a fraction)."""
    if cents < 0 or cents >= 1_000_000_000_00:
        raise DisbursementError("A check amount must be under one billion dollars.")
    dollars, rest = divmod(cents, 100)
    words: list[str] = []
    for value, name in ((1_000_000, "million"), (1000, "thousand")):
        if dollars >= value:
            words += [*_under_thousand(dollars // value), name]
            dollars %= value
    words += _under_thousand(dollars)
    text = " ".join(words) or "zero"
    return f"{text[0].upper()}{text[1:]} and {rest:02d}/100"


# --- Recipients, payees, credit ------------------------------------------------------------


@internal("disbursement payee")
def account_owners(account_id: int) -> list:
    """Everyone ever on the deed of an ownership account (current or past,
    removed co-owners included). Used to refuse steps, never for a payee."""
    seen, out = set(), []
    for o in (
        Ownership.objects.filter(account_id=account_id)
        .select_related("person")
        .order_by("start_date", "id")
    ):
        if o.person_id not in seen:
            seen.add(o.person_id)
            out.append(o.person)
    return out


@internal("disbursement payee")
def payee_owners(account: Account) -> list:
    """Who a check to this account is made out to: the owners on the deed
    today while the account is open, or the owners at the sale once it is
    dormant. A co-owner removed from the deed earlier is never named."""
    rows = Ownership.objects.filter(account_id=account.pk).select_related("person")
    if account.is_active:
        rows = rows.active()
    elif account.closed_on is not None:
        at_sale = rows.filter(start_date__lt=account.closed_on, end_date=account.closed_on)
        last_day = account.closed_on - datetime.timedelta(days=1)
        rows = at_sale if at_sale.exists() else rows.active(last_day)
    seen, out = set(), []
    for o in rows.order_by("start_date", "id"):
        if o.person_id not in seen:
            seen.add(o.person_id)
            out.append(o.person)
    return out


def payee_for(account: Account) -> tuple[str, str, str]:
    """(payee name, mailing address, source) for a check to an account's
    owners (``payee_owners``). A former owner's address is where they get
    mail now (their profile, or another home they own), never the home
    they sold."""
    from community.services import postal_address

    people = payee_owners(account)
    if not people:
        return "", "", "none"
    name = " and ".join(p.display_name for p in people)[:120]
    lot = account.lot if account.is_active else None
    address, source = postal_address(people[0], lot)
    return name, address.strip()[:300], source


@internal("disbursement recipients")
def recipient_person_ids(batch: DisbursementBatch) -> set[int]:
    """Everyone ever on the deed of an account the batch pays."""
    accounts = DisbursementLine.objects.filter(batch=batch).values_list("account_id", flat=True)
    return set(
        Ownership.objects.filter(account_id__in=list(accounts)).values_list("person_id", flat=True)
    )


@internal("disbursement recipients")
def line_recipient_ids(line: DisbursementLine) -> set[int]:
    """Everyone ever on the deed of the account one line pays."""
    return set(
        Ownership.objects.filter(account_id=line.account_id).values_list("person_id", flat=True)
    )


@internal("disbursement recipients")
def board_person_ids() -> set[int]:
    """People holding a board seat today."""
    from community.models import BoardSeat

    return set(BoardSeat.objects.active().values_list("person_id", flat=True))


def payment_refusal(actor, line: DisbursementLine, recipients=None, board=None) -> str:
    """Why ``actor`` may not write, void or clear the check for ``line``
    ("" when they may). Nobody processes a payment to an account they were
    ever on the deed of (removed co-owners and sellers included), and a
    payment to a board member's account is processed by another board
    member, never the manager (D66)."""
    if actor is SYSTEM:
        return ""
    recipients = line_recipient_ids(line) if recipients is None else recipients
    roles = roles_for(actor)
    if roles.person_id is not None and roles.person_id in recipients:
        return (
            f"The check for {line.lot.label} pays an account you are or were on the deed of; "
            "another board member handles it."
        )
    board = board_person_ids() if board is None else board
    if recipients & board and BOARD not in roles.groups:
        return (
            f"The check for {line.lot.label} pays a board member; another board member handles it."
        )
    return ""


def _refuse_payment(actor, lines, action: str, request=None) -> None:
    """Refuse writing, voiding or clearing any of these lines' checks that
    ``actor`` may not process (``payment_refusal``), audited as an own
    matter when it is their own."""
    if actor is SYSTEM:
        return
    with system_access("payment refusal"):
        board = board_person_ids()
        person_id = roles_for(actor).person_id
        for line in lines:
            recipients = line_recipient_ids(line)
            reason = payment_refusal(actor, line, recipients, board)
            if reason:
                if person_id is not None and person_id in recipients:
                    conflicts.audit_refusal(
                        actor, line, area="disbursement", action=action, request=request
                    )
                raise DisbursementError(reason)


@internal("disbursement credit")
def account_credit(account: Account) -> int:
    """The credit on an ownership account (0 when it owes, or is even)."""
    total = (
        LedgerEntry.objects.filter(account_id=account.pk).aggregate(s=Sum("amount_cents"))["s"] or 0
    )
    return max(0, -total)


@internal("disbursement credit")
def reserved_cents(account_id: int, *, exclude_batch_id: int | None = None) -> int:
    """Refund lines on this account still waiting to be paid: proposed or
    approved, with no live check yet (once issued, the refund entry has
    taken the credit)."""
    total = 0
    lines = DisbursementLine.objects.filter(
        account_id=account_id, batch__kind=DisbursementKind.REFUND
    ).select_related("batch")
    for line in lines:
        if line.batch_id == exclude_batch_id:
            continue
        if line_status(line) in ("proposed", "to_issue"):
            total += line.amount_cents
    return total


# --- State, derived from the records ---------------------------------------------------


@dataclass
class BatchState:
    status: str  # awaiting | approved | declined
    approvals: list = field(default_factory=list)
    declines: list = field(default_factory=list)
    needed: int = 1
    approved_at: datetime.datetime | None = None

    @property
    def have(self) -> int:
        return len(self.approvals)

    @property
    def label(self) -> str:
        return {
            "awaiting": "Waiting for approval",
            "approved": "Approved",
            "declined": "Declined",
        }[self.status]


def approvals_needed(batch: DisbursementBatch, approvals) -> int:
    """Approvals fixed at proposal; a surplus distribution approved by a
    recipient needs two."""
    needed = batch.required_approvals
    if batch.kind == DisbursementKind.SURPLUS and any(d.is_recipient for d in approvals):
        needed = max(needed, 2)
    return needed


def batch_state(batch: DisbursementBatch) -> BatchState:
    decisions = list(DisbursementDecision.objects.filter(batch=batch).order_by("created_at", "id"))
    approvals = [d for d in decisions if d.decision == DisbursementDecisionKind.APPROVE]
    declines = [d for d in decisions if d.decision == DisbursementDecisionKind.DECLINE]
    state = BatchState("awaiting", approvals, declines, approvals_needed(batch, approvals))
    if declines:
        state.status = "declined"
    elif state.have >= state.needed:
        state.status = "approved"
        state.approved_at = approvals[state.needed - 1].created_at
    return state


@internal("disbursement state")
def check_status(check: DisbursementCheck) -> str:
    """issued | cleared | voided. A check on a reconciled bank statement is
    cleared even without its own cleared record."""
    event = DisbursementCheckEvent.objects.filter(check_record=check).first()
    if event is not None:
        return event.kind
    if ReconciledItem.objects.filter(kind="check", object_id=check.pk).exists():
        return CheckEventKind.CLEARED
    return "issued"


@internal("disbursement state")
def live_check(line: DisbursementLine) -> DisbursementCheck | None:
    """The line's check that is not voided, if any."""
    for check in DisbursementCheck.objects.filter(line=line).order_by("-id"):
        if check_status(check) != CheckEventKind.VOIDED:
            return check
    return None


@internal("disbursement state")
def line_status(line: DisbursementLine, state: BatchState | None = None) -> str:
    """proposed | declined | posted (statement credit) | to_issue | issued |
    cleared (check)."""
    state = state or batch_state(line.batch)
    if state.status != "approved":
        return "proposed" if state.status == "awaiting" else "declined"
    if line.method == DisbursementMethod.CREDIT:
        return "posted"
    check = live_check(line)
    if check is None:
        return "to_issue"
    return check_status(check)


LINE_LABELS = {
    "proposed": "Proposed",
    "declined": "Declined",
    "posted": "Credited to the account",
    "to_issue": "Check to write",
    "issued": "Check issued",
    "cleared": "Check cleared",
}


# --- Proposing -------------------------------------------------------------------------


def _refuse_own_refund(actor, batch: DisbursementBatch, action: str, request=None) -> None:
    """Own matters (D65): a refund to one's own home's account is another
    board member's to propose, approve, decline, write or void. A surplus
    distribution pays every home, so its rule stays (a recipient may
    approve, and then two approvals are needed)."""
    if batch.kind != DisbursementKind.REFUND:
        return
    with system_access("own refund check"):
        lines = list(DisbursementLine.objects.filter(batch=batch))
    message = (
        "You are or were an owner on the account this refunds; another board member handles it."
    )
    conflicts.refuse_any(
        actor,
        lines,
        area="disbursement",
        action=action,
        request=request,
        error=DisbursementError,
        message=message,
    )
    # A seller or a removed co-owner is no longer an owner of the home, so
    # the account's deed is checked too (D66).
    person_id = roles_for(actor).person_id if actor is not SYSTEM else None
    if person_id is not None:
        with system_access("own refund check"):
            ever = person_id in recipient_person_ids(batch)
        if ever:
            conflicts.audit_refusal(
                actor, lines[0], area="disbursement", action=action, request=request
            )
            raise DisbursementError(message)


def _clean_text(value: str, limit: int) -> str:
    """One field typed by a person: printable characters only, collapsed."""
    text = "".join(c if c.isprintable() or c == "\n" else " " for c in (value or ""))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def _required_approvals(total_cents: int) -> tuple[int, int, str]:
    threshold, citation = rule_with_citation(SECOND_APPROVAL_RULE)
    threshold = int(threshold if threshold is not None else 50000)
    return (2 if total_cents >= threshold else 1), threshold, citation or ""


def _check_total(total_cents) -> int:
    if not isinstance(total_cents, int) or total_cents <= 0:
        raise DisbursementError("Enter an amount over zero.")
    if total_cents > MAX_TOTAL_CENTS:
        raise DisbursementError("That amount is too large.")
    return total_cents


@internal("surplus distribution")
def surplus_accounts() -> list[Account]:
    """Every home's account open today (homes with an owner), in address
    order."""
    accounts = list(Account.objects.active().select_related("lot"))
    return sorted(accounts, key=lambda a: home_sort_key(a.lot))


def cash_allowed() -> bool:
    """May a surplus share be paid by check (rule ``CASH_RULE``, off by default:
    a Declaration that credits surplus gives no basis for cash)."""
    return rule(CASH_RULE) is True


def by_share_paid() -> bool:
    return rule(SHARE_PAID_RULE) is not False


def stays_with_home() -> bool:
    return rule(HOME_RULE) is not False


def _open_account(lot_id: int) -> Account | None:
    with system_access("home's open account"):
        return Account.objects.active().select_related("lot").filter(lot_id=lot_id).first()


def fiscal_year_range(year: int) -> tuple[datetime.date, datetime.date]:
    """[first day, day after the last) of fiscal year ``year``."""
    from ledger.services import fiscal_months

    months = fiscal_months(int(year))
    first_y, first_m = months[0]
    last_y, last_m = months[-1]
    end = datetime.date(last_y + (last_m == 12), last_m % 12 + 1, 1)
    return datetime.date(first_y, first_m, 1), end


@internal("surplus shares paid")
def assessments_paid(year: int) -> list[tuple[Account, int]]:
    """Each ownership account (open or dormant) with what it paid towards the
    {term}s of fiscal year ``year``, in address order, accounts that paid
    nothing left out. Paid = the charge less what is still open on it
    (open-item allocation); reversed charges count for nothing."""
    from ledger.allocation import charges

    start, end = fiscal_year_range(year)
    accounts = Account.objects.select_related("lot").filter(opened_on__lt=end)
    out = []
    for account in accounts:
        paid = 0
        for c in charges(account.lot_id, open_only=False, account=account.pk):
            if c.entry.kind != EntryKind.ASSESSMENT or c.reversed:
                continue
            if start <= c.entry.posted_on < end:
                paid += max(0, c.original_cents - max(0, c.open_cents))
        if paid > 0:
            out.append((account, paid))
    return sorted(out, key=lambda pair: (home_sort_key(pair[0].lot), pair[0].opened_on))


def surplus_split(total_cents: int, *, year: int | None = None, by_check=()) -> list[dict]:
    """The proposed lines of a surplus distribution: each account's share and
    how it is paid. By share paid (default): in proportion to the {term}s each
    account paid in fiscal ``year``; otherwise equal per home with an owner
    today. Each share is a statement credit (on a seller's old account too)
    unless checks are allowed: then homes in ``by_check`` and sellers with a
    mailing address are paid by check."""
    _check_total(total_cents)
    by_check = {int(x) for x in by_check}
    cash = cash_allowed()
    if by_check and not cash:
        raise DisbursementError(
            "Community settings pay a surplus as statement credits only (the Declaration "
            "credits it against future assessments); paying by check is switched off."
        )
    if by_share_paid():
        if year is None:
            raise DisbursementError("Enter the fiscal year the surplus is from.")
        paid = assessments_paid(year)
        if stays_with_home():
            # Everything paid on each home, credited to its account today.
            per_lot: dict[int, int] = {}
            for account, cents in paid:
                per_lot[account.lot_id] = per_lot.get(account.lot_id, 0) + cents
            paid = []
            for lot_id, cents in per_lot.items():
                account = _open_account(lot_id)
                if account is not None:  # a home with no owner today gets nothing
                    paid.append((account, cents))
            paid.sort(key=lambda pair: home_sort_key(pair[0].lot))
        if not paid:
            raise DisbursementError(
                "Nobody paid assessments in that fiscal year, so there is nothing to split by."
            )
        accounts = [a for a, _ in paid]
        weights = [cents for _, cents in paid]
    else:
        accounts = surplus_accounts()
        if not accounts:
            raise DisbursementError("No home has an owner to distribute to.")
        weights = [1] * len(accounts)
    if total_cents < len(accounts):
        raise DisbursementError("The total must give every share at least one cent.")
    shares = split_largest_remainder(total_cents, weights)
    out = []
    for account, cents, weight in zip(accounts, shares, weights, strict=True):
        if cents <= 0:
            continue  # a share too small to round to a cent
        method = "credit"
        if cash and ((account.is_active and account.lot_id in by_check) or not account.is_active):
            method = DisbursementMethod.CHECK
        payee, address, source = payee_for(account) if method == "check" else ("", "", "")
        if method == "check" and not account.is_active and not address:
            # A seller with no address on file: a credit on the old account,
            # refunded later from Disbursements once an address is known.
            method, payee, source = "credit", "", ""
        out.append(
            {
                "lot": account.lot,
                "account": account,
                "amount_cents": cents,
                "paid_cents": weight if by_share_paid() else None,
                "former": not account.is_active,
                "method": method,
                "payee_name": payee,
                "mail_to": address,
                "address_source": source,
            }
        )
    return out


def _resolution_check(resolution, resolution_ref: str) -> str:
    ref = _clean_text(resolution_ref, 120)
    if resolution is None and not ref:
        raise DisbursementError(
            "A surplus distribution needs the board resolution that decided it: choose it, "
            "or type its reference."
        )
    return ref


def propose_surplus(
    *,
    total_cents: int,
    year: int,
    resolution=None,
    resolution_ref: str = "",
    by_check=(),
    note: str = "",
    actor,
    request=None,
) -> DisbursementBatch:
    """Propose a surplus distribution to every home with an owner."""
    require(actor, "disbursement.propose")
    ref = _resolution_check(resolution, resolution_ref)
    if not 2000 <= int(year) <= 2100:
        raise DisbursementError("Enter the year the surplus is from.")
    lines = surplus_split(total_cents, year=year, by_check=by_check)
    for line in lines:
        if line["method"] == "check" and not line["mail_to"]:
            raise DisbursementError(
                f"{line['lot'].label}: no mailing address for a check; pay it as a credit."
            )
    return _create(
        kind=DisbursementKind.SURPLUS,
        title=f"Surplus distribution \u2013 {int(year)}",
        total_cents=total_cents,
        lines=lines,
        note=note,
        resolution=resolution,
        resolution_ref=ref,
        actor=actor,
        request=request,
    )


def refund_defaults(account: Account) -> dict:
    """What a refund proposal for this account starts from."""
    payee, address, source = payee_for(account)
    credit = account_credit(account)
    return {
        "credit_cents": credit,
        "available_cents": max(0, credit - reserved_cents(account.pk)),
        "payee_name": payee,
        "mail_to": address,
        "address_source": source,
        "former": not account.is_active,
    }


def propose_refund(
    *,
    account: Account,
    amount_cents: int,
    payee_name: str = "",
    forwarding_address: str = "",
    note: str = "",
    actor,
    request=None,
) -> DisbursementBatch:
    """Propose refunding (part of) one account's credit by check."""
    require(actor, "disbursement.propose")
    _check_total(amount_cents)
    with system_access("refund proposal"):
        account = Account.objects.select_related("lot").get(pk=account.pk)
    conflicts.refuse(
        actor,
        account,
        area="disbursement",
        action="propose",
        request=request,
        error=DisbursementError,
    )
    person_id = roles_for(actor).person_id if actor is not SYSTEM else None
    if person_id is not None and person_id in {p.pk for p in account_owners(account.pk)}:
        conflicts.audit_refusal(
            actor, account, area="disbursement", action="propose", request=request
        )
        raise DisbursementError(
            "You are or were an owner on this account; another board member proposes its refund."
        )
    defaults = refund_defaults(account)
    if amount_cents > defaults["available_cents"]:
        raise DisbursementError(
            "The account's credit available to refund is "
            f"{format_cents(defaults['available_cents'])} (its credit less refunds already "
            "proposed)."
        )
    name = _clean_text(payee_name, 120).replace("\n", " ") or defaults["payee_name"]
    address = _clean_text(forwarding_address, 300)
    source = "forwarding" if address else defaults["address_source"]
    address = address or defaults["mail_to"]
    if not name:
        raise DisbursementError("Enter the name to write on the check.")
    if not address:
        raise DisbursementError(
            "Enter the former owner's forwarding address: the portal has no mailing address "
            "for them other than the home they sold."
        )
    line = {
        "lot": account.lot,
        "account": account,
        "amount_cents": amount_cents,
        "method": DisbursementMethod.CHECK,
        "payee_name": name,
        "mail_to": address,
        "address_source": source,
    }
    who = "former owner" if defaults["former"] else "owner"
    return _create(
        kind=DisbursementKind.REFUND,
        title=f"Refund to the {who} of {account.lot.label}"[:120],
        total_cents=amount_cents,
        lines=[line],
        note=note,
        actor=actor,
        request=request,
    )


def _create(
    *, kind, title, total_cents, lines, note, actor, request, resolution=None, resolution_ref=""
):
    required, threshold, citation = _required_approvals(total_cents)
    with transaction.atomic(), system_access("disbursement proposal"):
        batch = DisbursementBatch.objects.create(
            kind=kind,
            title=title,
            total_cents=total_cents,
            note=_clean_text(note, 2000),
            resolution=resolution,
            resolution_ref=resolution_ref,
            required_approvals=required,
            second_approval_cents=threshold,
            rule_citation=citation[:200],
            proposed_by=actor,
        )
        for line in lines:
            DisbursementLine.objects.create(
                batch=batch,
                lot=line["lot"],
                account=line["account"],
                amount_cents=line["amount_cents"],
                method=line["method"],
                payee_name=line["payee_name"],
                mail_to=line["mail_to"],
                address_source=line["address_source"],
            )
    record_event(
        "disbursement.proposed",
        actor=actor,
        request=request,
        target=batch,
        metadata={
            "batch_id": batch.pk,
            "kind": kind,
            "lines": len(lines),
            "amount_cents": total_cents,
            "need": required,
        },
    )
    return batch


# --- Approving and declining -------------------------------------------------------------


def _check_refund_credit(batch: DisbursementBatch, lines: list) -> None:
    for line in lines:
        _lock_lot(line.lot_id)
        available = account_credit(line.account) - reserved_cents(
            line.account_id, exclude_batch_id=batch.pk
        )
        if line.amount_cents > available:
            raise DisbursementError(
                f"The account now has only {format_cents(max(0, available))} of credit to "
                "refund; decline this and propose the right amount."
            )


def approve(batch: DisbursementBatch, *, note: str = "", actor, request=None) -> BatchState:
    """One board member's approval. The last one needed posts the statement
    credits in the same transaction."""
    require(actor, "disbursement.approve")
    _refuse_own_refund(actor, batch, "approve", request)
    person_id = roles_for(actor).person_id
    with transaction.atomic(), system_access("disbursement approval"):
        _lock(_BATCH_LOCK, batch.pk)
        state = batch_state(batch)
        if state.status != "awaiting":
            raise DisbursementError("This disbursement is not waiting for approval.")
        if batch.proposed_by_id == actor.pk:
            raise DisbursementError(
                "You proposed this disbursement; a different board member must approve it."
            )
        if any(d.approver_id == actor.pk for d in state.approvals):
            raise DisbursementError("You have already approved this disbursement.")
        recipient = person_id is not None and person_id in recipient_person_ids(batch)
        if recipient and batch.kind != DisbursementKind.SURPLUS:
            raise DisbursementError(
                "You are an owner on the account this refunds; another board member must approve."
            )
        lines = list(DisbursementLine.objects.filter(batch=batch).select_related("account", "lot"))
        if batch.kind == DisbursementKind.REFUND:
            _check_refund_credit(batch, lines)
        if batch.kind == DisbursementKind.SURPLUS and not (
            batch.resolution_id or batch.resolution_ref
        ):
            raise DisbursementError("A surplus distribution needs its board resolution.")
        DisbursementDecision.objects.create(
            batch=batch,
            decision=DisbursementDecisionKind.APPROVE,
            approver=actor,
            is_recipient=recipient,
            amount_cents=batch.total_cents,
            note=_clean_text(note, 1000),
        )
        state = batch_state(batch)
        record_event(
            "disbursement.approval_given",
            actor=actor,
            request=request,
            target=batch,
            metadata={
                "batch_id": batch.pk,
                "have": state.have,
                "need": state.needed,
                "recipient": recipient,
            },
        )
        if state.status == "approved":
            posted = _post_credits(batch, lines, actor=actor, request=request)
            record_event(
                "disbursement.approved",
                actor=actor,
                request=request,
                target=batch,
                metadata={
                    "batch_id": batch.pk,
                    "kind": batch.kind,
                    "lines": len(lines),
                    "credits": posted,
                    "amount_cents": batch.total_cents,
                },
            )
    return state


def _post_credits(batch, lines, *, actor, request) -> int:
    """Statement credits post when the batch is approved."""
    today = timezone.localdate()
    posted = 0
    for line in lines:
        if line.method != DisbursementMethod.CREDIT:
            continue
        # Sold since it was proposed (D67): a share that stays with the home
        # goes to the home's account today; one that follows the owner to
        # their (now dormant) account, from which the board can refund it.
        account_id = line.account_id
        if stays_with_home():
            account = Account.objects.get(pk=account_id)
            if not account.is_active:
                current = _open_account(line.lot_id)
                if current is None:
                    raise DisbursementError(
                        f"{line.lot.label} has no owner today, so its share cannot be "
                        "credited. Decline this and propose it again."
                    )
                account_id = current.pk
        entry = LedgerEntry.objects.create(
            lot_id=line.lot_id,
            account_id=account_id,
            posted_on=today,
            kind=EntryKind.CREDIT,
            amount_cents=-line.amount_cents,
            description=_credit_description(batch),
            reference=f"Disbursement {batch.pk}"[:40],
            posted_by=_user(actor),
        )
        DisbursementEntry.objects.create(line=line, entry=entry)
        posted += 1
        record_event(
            "disbursement.credit_posted",
            actor=actor,
            request=request,
            target=entry,
            metadata={
                "batch_id": batch.pk,
                "line_id": line.pk,
                "lot_id": line.lot_id,
                "amount_cents": line.amount_cents,
            },
        )
    return posted


def _credit_description(batch: DisbursementBatch) -> str:
    """The line owners read, e.g. "Surplus distribution credit - 2026"."""
    title = batch.title
    if batch.kind == DisbursementKind.SURPLUS:
        title = title.replace("Surplus distribution", "Surplus distribution credit", 1)
    return title[:200]


def decline(batch: DisbursementBatch, *, note: str, actor, request=None) -> BatchState:
    """A board member declines (the proposer may withdraw this way). A reason
    is required; nothing was posted, so nothing is undone."""
    require(actor, "disbursement.approve")
    _refuse_own_refund(actor, batch, "decline", request)
    note = _clean_text(note, 1000)
    if not note:
        raise DisbursementError("Give the reason for declining.")
    with transaction.atomic(), system_access("disbursement decline"):
        _lock(_BATCH_LOCK, batch.pk)
        state = batch_state(batch)
        if state.status != "awaiting":
            raise DisbursementError("Only a disbursement waiting for approval can be declined.")
        if any(d.approver_id == actor.pk for d in state.approvals):
            raise DisbursementError("You approved this disbursement.")
        DisbursementDecision.objects.create(
            batch=batch,
            decision=DisbursementDecisionKind.DECLINE,
            approver=actor,
            amount_cents=batch.total_cents,
            note=note,
        )
    record_event(
        "disbursement.declined",
        actor=actor,
        request=request,
        target=batch,
        metadata={"batch_id": batch.pk},
    )
    return batch_state(batch)


# --- Checks ------------------------------------------------------------------------------

_NUMBER = re.compile(r"^[0-9]{1,12}$")


def next_numbers(first: str, count: int) -> list[str]:
    """``count`` check numbers from ``first`` up, keeping leading zeros."""
    first = (first or "").strip()
    if not _NUMBER.match(first):
        raise DisbursementError("A check number is digits only, e.g. 1042.")
    width = len(first)
    return [str(int(first) + i).zfill(width) for i in range(count)]


def lines_to_issue(batch: DisbursementBatch, line_ids=None) -> list[DisbursementLine]:
    with system_access("checks to issue"):
        state = batch_state(batch)
        lines = list(
            DisbursementLine.objects.filter(batch=batch, method=DisbursementMethod.CHECK)
            .select_related("lot", "account")
            .order_by("id")
        )
        lines = [line for line in lines if line_status(line, state) == "to_issue"]
    lines.sort(key=lambda line: home_sort_key(line.lot))
    if line_ids is not None:
        wanted = {int(i) for i in line_ids}
        lines = [line for line in lines if line.pk in wanted]
    return lines


def lines_for_actor(
    actor, batch: DisbursementBatch, line_ids=None
) -> tuple[list[DisbursementLine], list[DisbursementLine]]:
    """(lines ``actor`` may write checks for, lines left for another board
    member) among those still to write (``payment_refusal``, D66)."""
    lines = lines_to_issue(batch, line_ids)
    if actor is SYSTEM:
        return lines, []
    ok, left = [], []
    with system_access("checks for this person"):
        board = board_person_ids()
        for line in lines:
            (left if payment_refusal(actor, line, board=board) else ok).append(line)
    return ok, left


def issue_checks(
    batch: DisbursementBatch,
    *,
    first_number: str,
    issued_on: datetime.date,
    line_ids=None,
    actor,
    request=None,
) -> list[DisbursementCheck]:
    """Record the checks written for the batch's lines (all still to issue,
    or ``line_ids``), numbered from ``first_number`` in address order. A
    refund check posts its refund entry now, taking up the credit."""
    require(actor, "disbursement.issue")
    _refuse_own_refund(actor, batch, "issue", request)
    if line_ids is None:
        # "Mark as issued" for the whole batch writes the checks this person
        # may write; the rest stay for another board member (D66).
        mine, left = lines_for_actor(actor, batch)
        if left and not mine:
            _refuse_payment(actor, left, "issue", request)
        line_ids = [line.pk for line in mine] if left else None
    else:
        with system_access("checks to write"):
            _refuse_payment(actor, lines_to_issue(batch, line_ids), "issue", request)
    today = timezone.localdate()
    if issued_on > today:
        raise DisbursementError("The issue date cannot be in the future.")
    out = []
    with transaction.atomic(), system_access("issue checks"):
        _lock(_BATCH_LOCK, batch.pk)
        _lock(_CHECK_LOCK, 0)
        state = batch_state(batch)
        if state.status != "approved":
            raise DisbursementError("Checks are written only after the disbursement is approved.")
        if issued_on < timezone.localdate(state.approved_at):
            raise DisbursementError("The issue date cannot be before the approval.")
        lines = lines_to_issue(batch, line_ids)
        if not lines:
            raise DisbursementError("There are no checks to write for this disbursement.")
        numbers = next_numbers(first_number, len(lines))
        taken = set(
            DisbursementCheck.objects.filter(number__in=numbers).values_list("number", flat=True)
        )
        if taken:
            raise DisbursementError(f"Check number {sorted(taken)[0]} is already recorded.")
        for line, number in zip(lines, numbers, strict=True):
            account = Account.objects.get(pk=line.account_id)
            if batch.kind == DisbursementKind.REFUND:
                _lock_lot(line.lot_id)
                if line.amount_cents > account_credit(account):
                    raise DisbursementError(
                        f"{line.lot.label}: the account's credit is now "
                        f"{format_cents(account_credit(account))}, less than this check. Nothing "
                        "was recorded."
                    )
            try:
                with transaction.atomic():
                    check = DisbursementCheck.objects.create(
                        line=line,
                        number=number,
                        issued_on=issued_on,
                        amount_cents=line.amount_cents,
                        payee_name=line.payee_name,
                        mail_to=line.mail_to,
                        issued_by=actor,
                    )
            except IntegrityError as exc:
                raise DisbursementError(f"Check number {number} is already recorded.") from exc
            entry = None
            if batch.kind == DisbursementKind.REFUND:
                entry = LedgerEntry.objects.create(
                    lot_id=line.lot_id,
                    account_id=line.account_id,
                    posted_on=issued_on,
                    kind=EntryKind.REFUND,
                    amount_cents=line.amount_cents,
                    description=f"Refund by check #{number}"[:200],
                    reference=f"Refund check {number}"[:40],
                    posted_by=_user(actor),
                )
                DisbursementEntry.objects.create(line=line, entry=entry, check_record=check)
            out.append(check)
            record_event(
                "disbursement.check_issued",
                actor=actor,
                request=request,
                target=check,
                metadata={
                    "batch_id": batch.pk,
                    "line_id": line.pk,
                    "check_id": check.pk,
                    "lot_id": line.lot_id,
                    "amount_cents": line.amount_cents,
                },
            )
    return out


def _event_date(on, check, what: str) -> datetime.date:
    if on is None:
        on = timezone.localdate()
    if on > timezone.localdate():
        raise DisbursementError(f"The {what} date cannot be in the future.")
    if on < check.issued_on:
        raise DisbursementError(f"The {what} date cannot be before the check was issued.")
    return on


def mark_cleared(
    check: DisbursementCheck, *, on=None, bank_transaction=None, actor, request=None
) -> DisbursementCheckEvent:
    """The check cleared the bank (typed from the statement, or confirmed
    from a bank feed line of the same amount and number)."""
    require(actor, "disbursement.issue")
    with system_access("check to clear"):
        line = DisbursementLine.objects.select_related("lot").get(pk=check.line_id)
    _refuse_payment(actor, [line], "clear", request)
    on = _event_date(on, check, "cleared")
    if bank_transaction is not None and bank_transaction.amount_cents != -check.amount_cents:
        raise DisbursementError("That bank line is not this check's amount.")
    with transaction.atomic(), system_access("clear check"):
        _lock(_CHECK_LOCK, 0)
        if check_status(check) != "issued":
            raise DisbursementError("Only an issued check can be marked cleared.")
        event = DisbursementCheckEvent.objects.create(
            check_record=check,
            kind=CheckEventKind.CLEARED,
            on=on,
            bank_transaction=bank_transaction,
            recorded_by=actor,
        )
        lot_id = check.line.lot_id
    record_event(
        "disbursement.check_cleared",
        actor=actor,
        request=request,
        target=check,
        metadata={
            "check_id": check.pk,
            "lot_id": lot_id,
            "amount_cents": check.amount_cents,
            "bank_line": bank_transaction.pk if bank_transaction is not None else None,
        },
    )
    return event


def void_check(
    check: DisbursementCheck, *, reason: str, actor, request=None
) -> DisbursementCheckEvent:
    """Void an issued, uncleared check: a refund's entry is reversed (the
    owner's credit is open again) and the line can get a new check."""
    require(actor, "disbursement.void")
    with system_access("own refund check"):
        own_batch = DisbursementBatch.objects.get(pk=check.line.batch_id)
    _refuse_own_refund(actor, own_batch, "void", request)
    with system_access("check to void"):
        line = DisbursementLine.objects.select_related("lot").get(pk=check.line_id)
    _refuse_payment(actor, [line], "void", request)
    reason = _clean_text(reason, 150).replace("\n", " ")
    if not reason:
        raise DisbursementError("Give the reason for voiding the check.")
    today = timezone.localdate()
    with transaction.atomic(), system_access("void check"):
        _lock(_CHECK_LOCK, 0)
        if check_status(check) != "issued":
            raise DisbursementError("Only an issued check that has not cleared can be voided.")
        link = DisbursementEntry.objects.filter(check_record=check).select_related("entry").first()
        reversal = None
        if link is not None:
            _lock_lot(link.entry.lot_id)
            reversal = LedgerEntry.objects.create(
                lot_id=link.entry.lot_id,
                posted_on=today,
                kind=EntryKind.REVERSAL,
                amount_cents=-link.entry.amount_cents,
                reverses=link.entry,
                description=f"Check #{check.number} voided, credit restored: {reason}"[:200],
                posted_by=_user(actor),
            )
        event = DisbursementCheckEvent.objects.create(
            check_record=check,
            kind=CheckEventKind.VOIDED,
            on=today,
            reason=reason,
            reversal=reversal,
            recorded_by=actor,
        )
        lot_id = check.line.lot_id
    record_event(
        "disbursement.check_voided",
        actor=actor,
        request=request,
        target=check,
        metadata={
            "check_id": check.pk,
            "lot_id": lot_id,
            "amount_cents": check.amount_cents,
            "reversal_id": reversal.pk if reversal is not None else None,
        },
    )
    return event


# --- Overviews for the pages and My items ---------------------------------------------------


@internal("reserve surplus")
def reserve_surplus() -> dict | None:
    """How far the reserve balance is above the adopted plan's fully funded
    balance (its target), or None (no adopted plan, or not above it)."""
    from ledger.reserve_plan import latest_snapshot, starting_balance

    snapshot = latest_snapshot()
    if snapshot is None:
        return None
    balance = starting_balance()
    above = balance.cents - snapshot.fully_funded_cents
    if above <= 0:
        return None
    return {
        "above_cents": above,
        "balance_cents": balance.cents,
        "balance_as_of": balance.as_of,
        "target_cents": snapshot.fully_funded_cents,
        "adopted_on": snapshot.adopted_on,
        "since": balance.as_of or snapshot.adopted_on,
    }


@internal("dormant credits")
def dormant_credits() -> list[dict]:
    """Closed ownership accounts holding a credit that no refund covers yet,
    in address order."""
    today = timezone.localdate()
    sums = dict(
        LedgerEntry.objects.filter(account__closed_on__lte=today)
        .values_list("account_id")
        .annotate(s=Sum("amount_cents"))
        .values_list("account_id", "s")
    )
    out = []
    accounts = Account.objects.filter(pk__in=[k for k, s in sums.items() if s and s < 0])
    for account in accounts.select_related("lot"):
        credit = -sums[account.pk] - reserved_cents(account.pk)
        if credit > 0:
            out.append({"account": account, "lot": account.lot, "credit_cents": credit})
    return sorted(out, key=lambda r: home_sort_key(r["lot"]))


@internal("stale checks")
def stale_checks(today: datetime.date | None = None) -> list[DisbursementCheck]:
    """Issued checks still uncashed after STALE_DAYS."""
    today = today or timezone.localdate()
    cutoff = today - datetime.timedelta(days=STALE_DAYS)
    return [
        c
        for c in DisbursementCheck.objects.filter(issued_on__lte=cutoff).select_related("line__lot")
        if check_status(c) == "issued"
    ]


def batches(limit: int = 100) -> list[dict]:
    """Recent batches with their state, newest first."""
    out = []
    with system_access("disbursement list"):
        for batch in DisbursementBatch.objects.select_related("proposed_by")[:limit]:
            state = batch_state(batch)
            lines = list(DisbursementLine.objects.filter(batch=batch))
            statuses = [line_status(line, state) for line in lines]
            out.append(
                {
                    "batch": batch,
                    "state": state,
                    "lines": len(lines),
                    "to_issue": statuses.count("to_issue"),
                    "issued": statuses.count("issued"),
                }
            )
    return out


def board_items(user) -> list:
    """My items for disbursements (core.my_items): approvals waiting on this
    board member, checks to write, refunds owed to former owners, a reserve
    surplus to consider, and uncashed checks."""
    from core.dates import short_date
    from core.my_items import Item

    out: list = []
    with system_access("disbursement items"):
        person_id = roles_for(user).person_id
        open_batches = []
        for batch in DisbursementBatch.objects.all()[:200]:
            state = batch_state(batch)
            if state.status == "awaiting":
                open_batches.append((batch, state))
            elif state.status == "approved" and can(user, "disbursement.issue"):
                waiting = len(lines_for_actor(user, batch)[0])
                if waiting:
                    out.append(
                        Item(
                            "Finance",
                            f"{waiting} check{'' if waiting == 1 else 's'} to write: {batch.title}",
                            "Print or write them, then record the check numbers",
                            reverse("disbursement_detail", args=[batch.pk]),
                            "warning",
                            nav="disbursements",
                            # Outgoing checks; "Record payments received" is
                            # for owners' checks coming in (WT-010).
                            action_label="Write checks",
                            action_url=reverse("disbursement_detail", args=[batch.pk])
                            + "#write-checks",
                        )
                    )
        if can(user, "disbursement.approve"):
            for batch, state in open_batches:
                if batch.proposed_by_id == user.pk or any(
                    d.approver_id == user.pk for d in state.approvals
                ):
                    continue
                if batch.kind == DisbursementKind.REFUND and person_id in recipient_person_ids(
                    batch
                ):
                    continue
                out.append(
                    Item(
                        "Approvals",
                        f"Disbursement: {batch.title} ({format_cents(batch.total_cents)})",
                        f"Needs your approval ({state.have} of {state.needed})",
                        reverse("disbursement_detail", args=[batch.pk]),
                        "warning",
                        nav="disbursements",
                    )
                )
        if can(user, "disbursement.propose"):
            for row in dormant_credits():
                out.append(
                    Item(
                        "Finance",
                        f"Refund {format_cents(row['credit_cents'])} to the former owner of "
                        f"{row['lot'].label}",
                        f"Account {row['account'].number} closed with a credit",
                        reverse("disbursement_new") + f"?kind=refund&account={row['account'].pk}",
                        "info",
                        nav="disbursements",
                    )
                )
            surplus = reserve_surplus()
            if (
                surplus
                and not DisbursementBatch.objects.filter(
                    kind=DisbursementKind.SURPLUS, created_at__date__gte=surplus["since"]
                )
                .exclude(pk__in=[b.pk for b in _declined_batches()])
                .exists()
            ):
                out.append(
                    Item(
                        "Finance",
                        "Consider a surplus distribution",
                        f"Reserves are {format_cents(surplus['above_cents'])} above the adopted "
                        "plan's target",
                        reverse("disbursements"),
                        "info",
                        nav="disbursements",
                    )
                )
            for check in stale_checks():
                out.append(
                    Item(
                        "Finance",
                        f"Check #{check.number} uncashed since {short_date(check.issued_on)}",
                        f"{check.line.lot.label} · {format_cents(check.amount_cents)}. "
                        + UNCLAIMED_NOTE,
                        reverse("disbursement_detail", args=[check.line.batch_id]),
                        "warning",
                        check.issued_on + datetime.timedelta(days=STALE_DAYS),
                        nav="disbursements",
                    )
                )
    return out


def _declined_batches() -> list:
    return [
        b
        for b in DisbursementBatch.objects.filter(kind=DisbursementKind.SURPLUS)
        if DisbursementDecision.objects.filter(
            batch=b, decision=DisbursementDecisionKind.DECLINE
        ).exists()
    ]


@internal("checks for owners")
def checks_for_account(account_id: int | None) -> list[dict]:
    """The checks written to one ownership account's owners, for their own
    pages (My account, My history): number, date, amount, address, and
    whether it was voided. Nothing about any other account."""
    if not account_id:
        return []
    out = []
    for check in (
        DisbursementCheck.objects.filter(line__account_id=account_id)
        .select_related("line__batch")
        .order_by("issued_on", "id")
    ):
        status = check_status(check)
        out.append(
            {
                "number": check.number,
                "issued_on": check.issued_on,
                "amount_cents": check.amount_cents,
                "mail_to": check.mail_to.replace("\n", ", "),
                "kind": check.line.batch.get_kind_display(),
                "refund": check.line.batch.kind == DisbursementKind.REFUND,
                "status": status,
            }
        )
    return out


@internal("bank feed for checks")
def clearing_suggestions(checks) -> dict[int, object]:
    """For each issued check, an outgoing bank feed line of the same amount
    whose description carries the check number, not matched to anything."""
    from ledger.bankfeeds.matching import unmatched_lines

    out: dict[int, object] = {}
    open_checks = [c for c in checks if check_status(c) == "issued"]
    if not open_checks:
        return out
    lines = [line for line in unmatched_lines(limit=500) if line.amount_cents < 0]
    used: set[int] = set()
    for check in open_checks:
        for line in lines:
            if line.pk in used or not bank_line_fits(line, check):
                continue
            out[check.pk] = line
            used.add(line.pk)
            break
    return out


def bank_line_fits(line, check) -> bool:
    """Same amount out, the check number in the bank's description, and
    posted on or after the issue date."""
    if line.amount_cents != -check.amount_cents or line.posted_on < check.issued_on:
        return False
    digits = re.findall(r"[0-9]+", line.description or "")
    return any(d.lstrip("0") == check.number.lstrip("0") for d in digits)


# --- Accounting hooks (ledger/accounting.py) ----------------------------------------------

SURPLUS_CREDIT, REFUND_CHECK = "surplus_credit", "refund_check"


@internal("disbursement journal")
def journal_roles() -> dict[int, str]:
    """Ledger entries a disbursement posted, by the journal lines they need:
    a surplus statement credit (fund to owner receivables) or a refund check
    (owner receivables to the bank). Reversals follow their entry (the
    caller looks up ``reverses_id``); a copy moved to another ownership
    account (D34-fixmoney-1, D35) keeps the role of the entry it copies."""
    out = {}
    for entry_id, kind, method in DisbursementEntry.objects.values_list(
        "entry_id", "line__batch__kind", "line__method"
    ):
        if method == DisbursementMethod.CREDIT:
            out[entry_id] = SURPLUS_CREDIT if kind == DisbursementKind.SURPLUS else "credit"
        else:
            out[entry_id] = REFUND_CHECK
    frontier = list(out)
    while frontier:
        copies = LedgerEntry.objects.filter(moved_from_id__in=frontier).values_list(
            "pk", "moved_from_id"
        )
        frontier = []
        for pk, source in copies:
            if pk not in out:
                out[pk] = out[source]
                frontier.append(pk)
    return out


@internal("disbursement journal")
def surplus_check_journal(start: datetime.date, end: datetime.date) -> list[tuple]:
    """Surplus distribution checks, which post no owner entry: (date, ref,
    cents, lot, memo); a void is the same check with a negative amount on the
    void date."""
    out = []
    checks = DisbursementCheck.objects.filter(
        line__batch__kind=DisbursementKind.SURPLUS
    ).select_related("line__lot")
    for c in checks.filter(issued_on__range=(start, end)):
        out.append(
            (
                c.issued_on,
                f"K{c.pk}",
                c.amount_cents,
                c.line.lot,
                f"{c.line.lot.label}: surplus check #{c.number}",
            )
        )
    for ev in DisbursementCheckEvent.objects.filter(
        kind=CheckEventKind.VOIDED,
        on__range=(start, end),
        check_record__line__batch__kind=DisbursementKind.SURPLUS,
    ).select_related("check_record__line__lot"):
        c = ev.check_record
        out.append(
            (
                ev.on,
                f"K{c.pk}V",
                -c.amount_cents,
                c.line.lot,
                f"{c.line.lot.label}: surplus check #{c.number} voided",
            )
        )
    return sorted(out, key=lambda r: (r[0], r[1]))


@dataclass(frozen=True)
class CheckItem:
    """An owner check as a cash-book payment, for bank reconciliation."""

    pk: int
    number: str
    issued_on: datetime.date
    amount_cents: int
    lot_label: str
    cleared_on: datetime.date | None


@internal("checks in the cash book")
def book_cents(since: datetime.date, as_of: datetime.date) -> int:
    """Money the checks took out of the bank through ``as_of``: issued checks
    less those voided by then (both after ``since``)."""
    issued = (
        DisbursementCheck.objects.filter(issued_on__gt=since, issued_on__lte=as_of).aggregate(
            s=Sum("amount_cents")
        )["s"]
        or 0
    )
    voided = (
        DisbursementCheckEvent.objects.filter(
            kind=CheckEventKind.VOIDED,
            on__lte=as_of,
            check_record__issued_on__gt=since,
            check_record__issued_on__lte=as_of,
        ).aggregate(s=Sum("check_record__amount_cents"))["s"]
        or 0
    )
    return issued - voided


@internal("checks in the cash book")
def outstanding_checks(since: datetime.date, as_of: datetime.date) -> list[CheckItem]:
    """Checks issued through ``as_of``, not voided by then and not yet on a
    reconciled statement (outstanding at the bank until they clear)."""
    reconciled = set(
        ReconciledItem.objects.filter(kind="check").values_list("object_id", flat=True)
    )
    events = {
        e.check_record_id: e
        for e in DisbursementCheckEvent.objects.filter(
            check_record__issued_on__gt=since, check_record__issued_on__lte=as_of
        )
    }
    out = []
    for c in (
        DisbursementCheck.objects.filter(issued_on__gt=since, issued_on__lte=as_of)
        .select_related("line__lot")
        .order_by("issued_on", "id")
    ):
        event = events.get(c.pk)
        if c.pk in reconciled or (event and event.kind == "voided" and event.on <= as_of):
            continue
        cleared = event.on if event and event.kind == "cleared" else None
        out.append(
            CheckItem(c.pk, c.number, c.issued_on, c.amount_cents, c.line.lot.label, cleared)
        )
    return out


# --- Check register and printing ----------------------------------------------------------

REGISTER_HEADER = [
    "Disbursement",
    "Payee",
    "Mailing address",
    "Amount",
    "Memo",
    "Home",
    "Lot number",
    "Account",
    "Check number",
    "Issued on",
    "Status",
]


def memo_for(line: DisbursementLine) -> str:
    if line.batch.kind == DisbursementKind.SURPLUS:
        return line.batch.title
    return f"Refund of credit, account {line.account.number}"


def _cents_plain(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def register_rows(batch: DisbursementBatch) -> list[dict]:
    """Every check line of the batch (statement credits are not checks)."""
    with system_access("check register"):
        state = batch_state(batch)
        lines = list(
            DisbursementLine.objects.filter(
                batch=batch, method=DisbursementMethod.CHECK
            ).select_related("lot", "account", "batch")
        )
        rows = []
        for line in sorted(lines, key=lambda x: home_sort_key(x.lot)):
            check = live_check(line)
            rows.append(
                {
                    "line": line,
                    "check": check,
                    "status": LINE_LABELS[line_status(line, state)],
                    "memo": memo_for(line),
                }
            )
    return rows


def register_csv(batch: DisbursementBatch, *, actor, request=None) -> bytes:
    """The check requests for the treasurer (bank bill pay or accounting
    package): one row per check, formula-safe. Audited."""
    require(actor, "disbursement.view")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(REGISTER_HEADER)
    rows = register_rows(batch)
    for r in rows:
        line, check = r["line"], r["check"]
        writer.writerow(
            [
                safe_cell(batch.title),
                safe_cell(line.payee_name),
                safe_cell(line.mail_to.replace("\n", ", ")),
                _cents_plain(line.amount_cents),
                safe_cell(r["memo"]),
                safe_cell(line.lot.label),
                str(line.lot.number),
                safe_cell(line.account.number),
                safe_cell(check.number if check else ""),
                check.issued_on.isoformat() if check else "",
                r["status"],
            ]
        )
    record_event(
        "disbursement.register_exported",
        actor=actor,
        request=request,
        target=batch,
        metadata={"batch_id": batch.pk, "rows": len(rows), "format": "csv"},
    )
    return buf.getvalue().encode()


def register_pdf(batch: DisbursementBatch, *, actor, request=None) -> bytes:
    """The same check requests as a printable list, with a signature line."""
    require(actor, "disbursement.view")
    from community.models import CommunityProfile

    rows = register_rows(batch)
    state = batch_state(batch)
    paras = [
        ("title", "Check register / check requests"),
        ("normal", pdf.plain(CommunityProfile.get().legal_name or "")),
        ("normal", f"**{pdf.plain(batch.title)}** · total {format_cents(batch.total_cents)}"),
        (
            "small",
            f"Status: {state.label}. Approved by {state.have} of {state.needed} board members.",
        ),
        ("rule", ""),
    ]
    total = 0
    for r in rows:
        line, check = r["line"], r["check"]
        total += line.amount_cents
        number = (
            f"Check #{check.number}, {check.issued_on.isoformat()}" if check else "Check # ______"
        )
        paras += [
            ("normal", f"**{pdf.plain(line.payee_name)}** · {format_cents(line.amount_cents)}"),
            ("small", pdf.plain(line.mail_to.replace("\n", ", "))),
            ("small", f"Memo: {pdf.plain(r['memo'])} · Home: {pdf.plain(line.lot.legal_label)}"),
            ("small", f"{number} · {r['status']}"),
            ("gap", ""),
        ]
    paras += [
        ("rule", ""),
        ("normal", f"{len(rows)} checks, {format_cents(total)}"),
        ("gap", ""),
        ("normal", "Written by: ______________________  Date: __________"),
        ("normal", "Signed by: _______________________  Signed by: _______________________"),
    ]
    record_event(
        "disbursement.register_exported",
        actor=actor,
        request=request,
        target=batch,
        metadata={"batch_id": batch.pk, "rows": len(rows), "format": "pdf"},
    )
    return pdf.render(paras, title="Check register")


_POINTS_PER_HUNDREDTH = 0.72  # 72 points per inch


def _pt(hundredths: int, nudge: int) -> float:
    value = (hundredths + nudge) * _POINTS_PER_HUNDREDTH
    return min(max(value, 0.0), 790.0)


def check_page(
    settings: CheckPrintSettings,
    *,
    date: datetime.date,
    payee: str,
    amount_cents: int,
    memo: str,
    address: str,
    stub: str,
) -> list:
    """The pieces of one check page on pre-printed stock."""
    nx, ny = settings.nudge_x, settings.nudge_y

    def at(x, y, text, **kw):
        return pdf.Placed(min(_pt(x, nx), 600.0), _pt(y, ny), text, **kw)

    dollars = f"{amount_cents // 100:,}.{amount_cents % 100:02d}"
    words = amount_in_words(amount_cents)
    items = [
        at(settings.date_x, settings.date_y, date.strftime("%m/%d/%Y"), size=10),
        at(settings.payee_x, settings.payee_y, payee[:60], size=10),
        at(settings.amount_x, settings.amount_y, f"**{dollars}", size=10, mono=True),
        at(settings.words_x, settings.words_y, f"{words} " + "*" * max(3, 80 - len(words)), size=9),
        at(settings.memo_x, settings.memo_y, memo[:60], size=9),
    ]
    address_lines = [payee[:60], *[a for a in address.splitlines() if a.strip()][:4]]
    for i, text in enumerate(address_lines):
        items.append(at(settings.address_x, settings.address_y + i * 17, text[:60], size=10))
    for stub_y in (settings.stub1_y, settings.stub2_y):
        if stub_y:
            items.append(at(50, stub_y, stub[:110], size=9))
            items.append(at(50, stub_y + 17, f"{payee[:50]} · ${dollars} · {memo[:40]}", size=9))
    return items


def checks_pdf(
    batch: DisbursementBatch, *, on: datetime.date, line_ids=None, actor, request=None
) -> bytes:
    """The checks still to write, one per page, for the association's
    pre-printed check stock. The stock already carries the bank's numbers;
    nothing here is a bank number. Audited."""
    require(actor, "disbursement.issue")
    _refuse_own_refund(actor, batch, "print", request)
    if line_ids is None:
        lines, left = lines_for_actor(actor, batch)
        if left and not lines:
            _refuse_payment(actor, left, "print", request)
    else:
        lines = lines_to_issue(batch, line_ids)
        _refuse_payment(actor, lines, "print", request)
    if not lines:
        raise DisbursementError("There are no checks to print for this disbursement.")
    settings = CheckPrintSettings.get()
    pages = []
    for line in lines:
        memo = memo_for(line)
        stub = f"{on.isoformat()} · {line.lot.label} · account {line.account.number}"
        pages.append(
            check_page(
                settings,
                date=on,
                payee=line.payee_name,
                amount_cents=line.amount_cents,
                memo=memo,
                address=line.mail_to,
                stub=stub,
            )
        )
    record_event(
        "disbursement.checks_printed",
        actor=actor,
        request=request,
        target=batch,
        metadata={"batch_id": batch.pk, "count": len(lines)},
    )
    return pdf.render_positioned(pages, title="Checks")


def alignment_pdf(*, actor) -> bytes:
    """A test page for plain paper: a quarter-inch grid with inch labels and a
    sample check at the saved positions. Hold it over the check stock against
    the light to see what to move."""
    require(actor, "disbursement.issue")
    settings = CheckPrintSettings.get()
    items: list = []
    for i in range(0, 35):  # every quarter inch across 8.5in
        x = i * 18.0
        if x > 612:
            break
        items.append(
            pdf.Stroke(
                x, 0, x, 792, width=0.5 if i % 4 == 0 else 0.2, gray=0.5 if i % 4 == 0 else 0.8
            )
        )
        if i % 4 == 0 and i:
            items.append(pdf.Placed(min(x + 2, 600.0), 10, f"{i // 4}in", size=6))
    for j in range(0, 45):  # every quarter inch down 11in
        y = j * 18.0
        if y > 792:
            break
        items.append(
            pdf.Stroke(
                0, y, 612, y, width=0.5 if j % 4 == 0 else 0.2, gray=0.5 if j % 4 == 0 else 0.8
            )
        )
        if j % 4 == 0 and j:
            items.append(pdf.Placed(2, min(y - 2, 790.0), f"{j // 4}in", size=6))
    items += check_page(
        settings,
        date=timezone.localdate(),
        payee="SAMPLE PAYEE NAME",
        amount_cents=123456,
        memo="Alignment test - not a check",
        address="123 Sample Street\nAnytown, PA 00000",
        stub="Stub line: date · home · account",
    )
    return pdf.render_positioned([items], title="Check alignment test")


PRINT_FIELDS = [
    ("date", "Date"),
    ("payee", "Pay to the order of"),
    ("amount", "Amount in figures"),
    ("words", "Amount in words"),
    ("address", "Payee address (window envelope)"),
    ("memo", "Memo"),
]


def save_print_settings(values: dict[str, int], *, actor, request=None) -> CheckPrintSettings:
    """Save the positions (hundredths of an inch; nudges may be negative)."""
    require(actor, "disbursement.issue")
    settings = CheckPrintSettings.get()
    for name in [f"{f}_{a}" for f, _ in PRINT_FIELDS for a in ("x", "y")] + ["stub1_y", "stub2_y"]:
        value = int(values[name])
        limit = 850 if name.endswith("_x") else 1100
        if not 0 <= value <= limit:
            raise DisbursementError(
                "Positions are 0 to 8.50 inches across and 0 to 11 inches down."
            )
        setattr(settings, name, value)
    for name in ("nudge_x", "nudge_y"):
        value = int(values[name])
        if not -100 <= value <= 100:
            raise DisbursementError("A nudge is at most one inch either way.")
        setattr(settings, name, value)
    settings.updated_by = actor
    settings.updated_at = timezone.now()
    settings.save()
    record_event("disbursement.print_settings_saved", actor=actor, request=request, metadata={})
    return settings
