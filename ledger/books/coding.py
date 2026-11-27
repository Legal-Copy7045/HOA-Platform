"""Coding bank lines (D69): money that does not pass through an owner account
or an invoice enters the books by coding its bank line to a category.

- A person codes a line (``code_line``), or a board rule codes it by itself
  (``apply_rules``, run after every import or sync and before each close
  check). The GL entry records which: the rule's id, or the person.
- Owner payments in deposits, Stripe payouts, vendor payments, returned
  payments and owner checks keep using the bank feed's matching; the
  automatic close confirms a match by itself only when exactly one item fits
  a line and that item fits no other line (``auto_match``).
- Anything neither matched nor coded is an exception on the Close page.
- Repeated codings by hand of the same words suggest a rule
  (``rule_suggestions``).

Audit metadata carries ids, counts and kinds only, never a bank description.
"""

from __future__ import annotations

import re
from collections import Counter

from django.db import transaction
from django.utils import timezone

from audit.services import record_event
from community.rules import rule
from core.policy import SYSTEM, require
from ledger.books import funds, periods
from ledger.books.funds import BooksError
from ledger.books.models import CategoryKind, CodingRule, GLCategory, GLEntry, LineSign


def _user(actor):
    return None if actor is SYSTEM else actor


# --- Which lines are handled -----------------------------------------------------------------


def coded_entries():
    """GL entries that stand (not reversed, not reversals)."""
    return GLEntry.objects.filter(reverses__isnull=True, reversed_by__isnull=True)


def coded_line_ids() -> set[int]:
    return set(coded_entries().values_list("transaction_id", flat=True))


def matched_line_ids() -> set[int]:
    from ledger.bankfeeds.matching import active_matches

    return set(active_matches().values_list("transaction_id", flat=True))


def handled_line_ids() -> set[int]:
    return coded_line_ids() | matched_line_ids()


def books_connection_ids() -> set[int]:
    from ledger.books.models import BankAccountSetup

    return set(BankAccountSetup.objects.values_list("connection_id", flat=True))


def unhandled_lines(*, through=None, since=None, connection_ids=None):
    """Lines on the association's accounts in the books neither matched nor
    coded (from the books' start)."""
    from ledger.models import BankTransaction

    ids = books_connection_ids() if connection_ids is None else set(connection_ids)
    qs = BankTransaction.objects.filter(connection_id__in=list(ids)).exclude(
        pk__in=list(handled_line_ids())
    )
    start = funds.books_start()
    if start is not None:
        qs = qs.filter(posted_on__gt=start)
    if through is not None:
        qs = qs.filter(posted_on__lte=through)
    if since is not None:
        qs = qs.filter(posted_on__gte=since)
    return list(qs.select_related("connection").order_by("posted_on", "id"))


# --- Coding one line --------------------------------------------------------------------------


def _correction_reference(day) -> str:
    return f"Correction for {day:%B %Y} (bank line of {day:%Y-%m-%d})"


def code_line(
    line, category: GLCategory, *, actor, rule_obj: CodingRule | None = None, memo="", request=None
) -> GLEntry:
    """Code ``line`` to ``category``: a GL entry dated like the line, or
    today with a reference when the line's month is closed."""
    if actor is SYSTEM:
        if rule_obj is None:
            raise BooksError("Automatic coding needs a rule.")
    else:
        require(actor, "books.code")
    if not category.active:
        raise BooksError("That category is switched off.")
    setup = funds.setup_for(line.connection_id)
    if setup is None:
        raise BooksError(
            "Put this bank account in the books first (Bank accounts and funds): choose its fund."
        )
    start = funds.books_start()
    if start is None or line.posted_on <= start:
        raise BooksError("This line is from before the books' start date.")
    with transaction.atomic():
        from ledger.models import AccountingSetup

        # One lock for matching, coding and closing.
        AccountingSetup.objects.select_for_update().get(pk=AccountingSetup.get().pk)
        if line.pk in handled_line_ids():
            raise BooksError("This bank line is already matched or coded.")
        posted_on = periods.open_date(line.posted_on)
        reference = "" if posted_on == line.posted_on else _correction_reference(line.posted_on)
        entry = GLEntry.objects.create(
            transaction=line,
            posted_on=posted_on,
            category=category,
            fund=category.fund or setup.fund,
            cash_fund=setup.fund,
            cash_account=setup.gl_account,
            category_account=category.account,
            amount_cents=line.amount_cents,
            memo=(memo or "").strip()[:200],
            reference=reference,
            rule=rule_obj,
            coded_by=_user(actor),
        )
    record_event(
        "books.line_coded",
        actor=_user(actor),
        request=request,
        target=entry,
        metadata={
            "line_id": line.pk,
            "category_id": category.pk,
            "rule_id": rule_obj.pk if rule_obj else None,
            "correction": bool(reference),
        },
    )
    return entry


def uncode(entry: GLEntry, *, reason: str, actor, request=None) -> GLEntry:
    """Undo a coding: a reversing GL entry, in the open month."""
    require(actor, "books.code")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise BooksError("Say why the coding is undone.")
    with transaction.atomic():
        from ledger.models import AccountingSetup

        AccountingSetup.objects.select_for_update().get(pk=AccountingSetup.get().pk)
        entry = GLEntry.objects.get(pk=entry.pk)
        if entry.reverses_id or GLEntry.objects.filter(reverses=entry).exists():
            raise BooksError("This coding is already undone.")
        posted_on = periods.open_date(entry.posted_on)
        reversal = GLEntry.objects.create(
            transaction_id=entry.transaction_id,
            posted_on=posted_on,
            category_id=entry.category_id,
            fund=entry.fund,
            cash_fund=entry.cash_fund,
            cash_account=entry.cash_account,
            category_account=entry.category_account,
            amount_cents=-entry.amount_cents,
            memo=f"Undone: {reason}"[:200],
            reference=(
                "" if posted_on == entry.posted_on else _correction_reference(entry.posted_on)
            ),
            coded_by=_user(actor),
            reverses=entry,
        )
    record_event(
        "books.line_uncoded",
        actor=_user(actor),
        request=request,
        target=reversal,
        metadata={"line_id": entry.transaction_id, "entry_id": entry.pk},
    )
    return reversal


# --- Rules ------------------------------------------------------------------------------------


def _text(value: str) -> str:
    return " ".join((value or "").upper().split())


def rule_matches(r: CodingRule, line) -> bool:
    if not r.active or not r.category.active:
        return False
    if r.connection_id and r.connection_id != line.connection_id:
        return False
    if r.sign == LineSign.IN and line.amount_cents <= 0:
        return False
    if r.sign == LineSign.OUT and line.amount_cents >= 0:
        return False
    size = abs(line.amount_cents)
    if r.min_cents is not None and size < r.min_cents:
        return False
    if r.max_cents is not None and size > r.max_cents:
        return False
    return _text(r.contains) in _text(line.description)


def active_rules():
    return list(CodingRule.objects.filter(active=True).select_related("category"))


def first_rule_for(line, rules=None) -> CodingRule | None:
    for r in rules if rules is not None else active_rules():
        if rule_matches(r, line):
            return r
    return None


def apply_rules(lines=None) -> int:
    """Code every unhandled line a rule matches (as SYSTEM, by that rule)."""
    rules = active_rules()
    if not rules:
        return 0
    done = 0
    for line in unhandled_lines() if lines is None else lines:
        r = first_rule_for(line, rules)
        if r is None:
            continue
        try:
            code_line(line, r.category, actor=SYSTEM, rule_obj=r)
        except BooksError:
            continue
        done += 1
    if done:
        record_event("books.rules_applied", actor=None, metadata={"count": done})
    return done


def save_rule(
    *,
    name: str,
    contains: str,
    category: GLCategory,
    sign: str = LineSign.ANY,
    connection=None,
    min_cents: int | None = None,
    max_cents: int | None = None,
    priority: int = 100,
    apply_now: bool = True,
    actor,
    request=None,
) -> CodingRule:
    """A new coding rule (board only: it decides how money is booked)."""
    require(actor, "books.rules")
    contains = " ".join((contains or "").split())[:80]
    if len(contains) < 3:
        raise BooksError("Give at least three characters the bank's description contains.")
    if sign not in LineSign.values:
        raise BooksError("Choose money in, money out, or either.")
    if not category.active:
        raise BooksError("That category is switched off.")
    if min_cents is not None and max_cents is not None and min_cents > max_cents:
        raise BooksError("The smallest amount is above the largest.")
    if category.kind == CategoryKind.TRANSFER and sign == LineSign.ANY:
        raise BooksError("A transfer rule must say money in or money out.")
    r = CodingRule.objects.create(
        name=(" ".join((name or "").split()) or contains)[:80],
        contains=contains,
        sign=sign,
        connection=connection,
        min_cents=min_cents,
        max_cents=max_cents,
        category=category,
        priority=max(0, min(int(priority or 100), 999)),
        created_by=_user(actor),
    )
    record_event(
        "books.rule_saved",
        actor=_user(actor),
        request=request,
        target=r,
        metadata={"rule_id": r.pk, "category_id": category.pk, "sign": sign},
    )
    if apply_now:
        apply_rules()
    return r


def end_rule(r: CodingRule, *, actor, request=None) -> CodingRule:
    require(actor, "books.rules")
    if not r.active:
        raise BooksError("This rule has already ended.")
    r.active = False
    r.ended_by = _user(actor)
    r.ended_at = timezone.now()
    r.save()
    record_event(
        "books.rule_ended",
        actor=_user(actor),
        request=request,
        target=r,
        metadata={"rule_id": r.pk},
    )
    return r


_WORDS = re.compile(r"[A-Z][A-Z&']{2,}")


def rule_text(description: str) -> str:
    """The words of a bank description a rule would match on: letters only
    (numbers, masked digits and dates dropped), the first three words."""
    words = _WORDS.findall(_text(description))
    return " ".join(words[:3])


def rule_suggestions() -> list[dict]:
    """Words a person has coded to the same category, the same way (in or
    out), at least ``books.rule_suggest_after`` times, with no rule yet."""
    least = int(rule("books.rule_suggest_after") or 3)
    rules = active_rules()
    counts: Counter = Counter()
    for entry in coded_entries().filter(rule__isnull=True).select_related("transaction"):
        text = rule_text(entry.transaction.description)
        if not text:
            continue
        sign = LineSign.IN if entry.amount_cents > 0 else LineSign.OUT
        if first_rule_for(entry.transaction, rules) is not None:
            continue
        counts[(text, sign, entry.category_id)] += 1
    names = dict(GLCategory.objects.values_list("pk", "name"))
    return [
        {
            "contains": text,
            "sign": sign,
            "category_id": cat,
            "category": names.get(cat, ""),
            "count": n,
        }
        for (text, sign, cat), n in counts.most_common()
        if n >= least
    ]


# --- Automatic matching of unambiguous lines ---------------------------------------------------


def auto_match() -> int:
    """Confirm matches the bank feed suggests where exactly one item fits a
    line and that item fits no other line (as SYSTEM). Off with
    ``books.auto_match``."""
    if not rule("books.auto_match"):
        return 0
    from ledger.bankfeeds import matching
    from ledger.bankfeeds.base import BankFeedError

    lines = [line for line in unhandled_lines() if True]
    if not lines:
        return 0
    found = matching.suggestions(lines)
    fits: Counter = Counter()
    for row in found:
        for c in row["candidates"]:
            fits[(c.kind, c.object_id)] += 1
    done = 0
    for row in found:
        cands = row["candidates"]
        if len(cands) != 1 or fits[(cands[0].kind, cands[0].object_id)] != 1:
            continue
        try:
            matching.confirm(
                row["line"], kind=cands[0].kind, object_id=cands[0].object_id, actor=SYSTEM
            )
        except BankFeedError:
            continue
        done += 1
    return done
