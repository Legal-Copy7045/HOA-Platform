"""Account statements and delinquency notices (Milestone 4).

Laid out like the association's existing paper: letterhead, date, the owners'
names and mailing address, a "RE:" line, the letter, the signature block; and
an account statement with the account number, "pay this amount", a dated
running-balance table and a tear-off remittance stub.

Wording is board-edited (LetterTemplate) with a fixed set of placeholders,
replaced by plain string substitution (never str.format, so a template can't
reach into Python objects). Nothing is sent from here: the board downloads,
prints and mails; each generation is audited.
"""

from __future__ import annotations

import datetime
import re

from django.utils import timezone

from community.models import Account, CommunityProfile, ContactProfile, Lot, Ownership
from community.rules import rule
from community.terms import term_text
from core import pdf
from core.money import format_cents
from core.scoping import system_access
from ledger.models import CHARGE_KINDS, LedgerEntry, LetterKind, LetterTemplate

PLACEHOLDERS = {
    "owner_names": "Everyone on the deed, e.g. Pat & Sam Example",
    "salutation": "The first owner's name",
    "amount": "The amount past due",
    "days": "The notice period from Community settings, in words and figures",
    "date": "Today's date",
    "account_number": "The account number",
    "address": "The lot's street address",
    "contact": "The contact line from Community settings",
    "association": "The association's legal name",
}

DEFAULTS = {
    LetterKind.STATEMENT: (
        "",
        "Please note that there is an amount on your account that is currently past due. "
        "If this balance has already been paid, please disregard this notice. If you have any "
        "questions, please contact {contact}.",
        False,
    ),
    LetterKind.FIRST: (
        "DELINQUENT NOTICE",
        "According to the Association's records, your account shows a delinquency of {amount} "
        "for the monthly {terms} and/or late fees. The Association depends on every owner "
        "paying their share of the {terms} on time to keep up the community.\n\n"
        "Please bring your account current within {days} days from the date of this letter.\n\n"
        "If your records show a different amount, please review the attached statement of your "
        "account and contact {contact} right away so that we can look into it.",
        False,
    ),
    LetterKind.SECOND: (
        "DELINQUENT ASSOCIATION {TERMS} - SECOND NOTICE",
        "According to the Association's records, your account shows a delinquency of {amount} "
        "for the monthly {terms} and/or late fees. The Association depends on every owner "
        "paying their share of the {terms} on time.\n\n"
        "If payment is not received within {days} days from the date of this letter, the "
        "Association may refer the account for collection, which may include legal action.\n\n"
        "If your records show a different amount, please review the attached statement of your "
        "account and contact {contact} right away so that we can look into it.",
        True,
    ),
    LetterKind.FINAL: (
        "FINAL NOTICE BEFORE REFERRAL FOR COLLECTION",
        "Despite earlier notices, your account still shows a delinquency of {amount}. Unless "
        "the account is brought current within {days} days from the date of this letter, the "
        "Board intends to refer it for collection. Please contact {contact} now if you would "
        "like to discuss a payment plan.",
        True,
    ),
}

_TOKEN = re.compile(r"\{([a-z_]+)\}")
_NUMBER_WORDS = {
    5: "five",
    7: "seven",
    10: "ten",
    14: "fourteen",
    15: "fifteen",
    21: "twenty-one",
    30: "thirty",
    45: "forty-five",
    60: "sixty",
}


def template(kind: str) -> LetterTemplate:
    """The board's version, or the default wording (not saved until edited)."""
    found = LetterTemplate.objects.filter(kind=kind).first()
    if found:
        return found
    subject, body, certified = DEFAULTS[kind]
    # The default wording names the charge with the board's word (D40).
    subject, body = term_text(subject), term_text(body)
    default = LetterTemplate(kind=kind, subject=subject, body=body, certified=certified)
    default.is_default = True
    return default


def fill(text: str, values: dict[str, str]) -> str:
    """Replace known {placeholders}; anything else is left as written."""
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _days_text(days: int) -> str:
    word = _NUMBER_WORDS.get(days)
    return f"{word} ({days})" if word else str(days)


def _recipients(lot: Lot):
    """Owners' names and the mailing address (an owner's own mailing address
    if they gave one, else the lot)."""
    with system_access("letter recipients"):
        owners = list(
            Ownership.objects.active()
            .filter(lot=lot)
            .select_related("person")
            .order_by("start_date", "pk")
        )
        people = [o.person for o in owners]
        mailing = ""
        for p in people:
            profile = ContactProfile.objects.filter(person=p).first()
            if profile and profile.mailing_address.strip():
                mailing = profile.mailing_address.strip()
                break
        account = Account.objects.filter(lot=lot, closed_on__isnull=True).first()
    names = " & ".join(p.display_name for p in people) or "The Owner"
    salutation = people[0].display_name if people else "Owner"
    address = mailing or lot.street_address
    return names, salutation, address, account.number if account else f"Lot {lot.number}"


def _letterhead(profile) -> list[tuple[str, str]]:
    lines = [line for line in (profile.letterhead or "").splitlines() if line.strip()]
    if not lines:
        lines = [profile.legal_name]
    return [("heading", lines[0])] + [("normal", line) for line in lines[1:]]


def statement_paragraphs(lot: Lot, *, since=None, as_of=None) -> tuple[list, int]:
    """The statement page of the current owners' account (D33-acctfix-1: a
    former owner's entries and credit are never on it); returns (paragraphs,
    amount due)."""
    from ledger.accounts import MOVED_AWAY, account_q, current_account_id

    profile = CommunityProfile.get()
    as_of = as_of or timezone.localdate()
    owner_since = since
    since = since or datetime.date(2000, 1, 1)
    names, _salutation, address, account_number = _recipients(lot)
    account = current_account_id(lot.pk)
    with system_access("statement entries"):
        mine = (
            LedgerEntry.objects.filter(lot=lot)
            .filter(account_q(lot.pk, account))
            .exclude(MOVED_AWAY)
        )
        forward = sum(mine.filter(posted_on__lt=since).values_list("amount_cents", flat=True))
        entries = list(mine.filter(posted_on__gte=since, posted_on__lte=as_of))
    balance = forward + sum(e.amount_cents for e in entries)
    due_day = min(rule("dues.due_day") or 1, 28)
    past_due = balance > 0 and any(e.kind in CHARGE_KINDS and e.posted_on < as_of for e in entries)
    due_label = "Past Due" if past_due else ("Now" if balance > 0 else "-")
    message = fill(template(LetterKind.STATEMENT).body, _values(lot, _past_due(lot, as_of)))
    paras = [
        *_letterhead(profile),
        ("normal", ""),
        ("heading", f"ACCOUNT STATEMENT  as of {as_of:%m/%d/%Y}"),
        ("mono", f"{'Account Number':<24}{'Due Date':<16}{'Pay This Amount':>20}"),
        ("mono", f"{account_number:<24}{due_label:<16}{format_cents(max(balance, 0)):>20}"),
        ("normal", ""),
        ("normal", f"**{names}**"),
        ("normal", f"**{address}**"),
        ("normal", ""),
    ]
    if balance > 0:
        paras.append(("small", message))
    paras += [
        ("normal", ""),
        ("mono", f"{'DATE':<11}{'DESCRIPTION':<31}{'CHARGES':>12}{'PAYMENTS':>12}{'TOTAL':>12}"),
        ("mono", f"{'':<11}{f'Property: {account_number} - {lot.street_address}'[:66]}"),
        ("mono", f"{'':<11}{'-- Prior Balance --':<31}{'':>12}{'':>12}{format_cents(forward):>12}"),
    ]
    running = forward
    from ledger.services import moved_notes

    notes = moved_notes(entries)
    for e in entries:
        running += e.amount_cents
        charge = format_cents(e.amount_cents) if e.amount_cents > 0 else ""
        payment = format_cents(-e.amount_cents) if e.amount_cents < 0 else ""
        paras.append(
            (
                "mono",
                f"{e.posted_on:%Y-%m-%d} {e.description[:30]:<31}{charge:>12}{payment:>12}"
                f"{format_cents(running):>12}",
            )
        )
        if e.pk in notes:
            paras.append(("mono", f"{'':<11}({notes[e.pk]})"[:78]))
    paras += open_charges_paragraphs(lot, since=owner_since, account=account)
    remit = [line for line in (profile.remit_to or "").splitlines() if line.strip()] or [
        profile.legal_name
    ]
    paras += [
        ("normal", ""),
        (
            "small",
            "- - - - - - - - - - Please detach and return this portion with your payment. "
            "- - - - - - - - - -",
        ),
        (
            "mono",
            f"{profile.legal_name[:40]:<42}PAY THIS AMOUNT: {format_cents(max(balance, 0)):>15}",
        ),
        ("mono", f"{names[:40]:<42}DUE DATE:        {due_label:>15}"),
        (
            "mono",
            f"RE: {account_number} - {lot.street_address}"[:42].ljust(42)
            + f"Account Number:  {account_number:>15}",
        ),
        ("mono", address.splitlines()[0][:42] if address else ""),
        ("mono", ""),
        ("mono", "Mail checks to:".ljust(42) + "Please write your account number"),
    ]
    tips = [
        "on your check, payable to the",
        "association. Allow 10 to 15",
        "business days for processing.",
    ]
    for i, line in enumerate(remit[:5]):
        paras.append(("mono", line[:40].ljust(42) + (tips[i] if i < len(tips) else "")))
    paras.append(
        ("small", term_text(f"The monthly {{term}} is due on day {due_day} of each month."))
    )
    return paras, balance


def open_charges_paragraphs(lot: Lot, *, since=None, account=None) -> list[tuple[str, str]]:
    """The open charges of the current owners' account (or ``account``),
    each with its number, due date, original and open amount (open-item
    accounting, D32-payments)."""
    from ledger import allocation
    from ledger.accounts import current_account_id

    if account is None:
        account = current_account_id(lot.pk)
    rows = allocation.open_charges(lot.pk, since=since, account=account)
    before = allocation.open_before(lot.pk, since, account=account) if since else 0
    paras = [
        ("normal", ""),
        ("mono", "OPEN CHARGES"),
        ("mono", f"{'NUMBER':<11}{'DESCRIPTION':<31}{'DUE':<12}{'ORIGINAL':>12}{'OPEN':>12}"),
    ]
    if before:
        paras.append(
            (
                "mono",
                f"{'':<11}{'From before your ownership':<31}{'':<12}{'':>12}"
                f"{format_cents(before):>12}",
            )
        )
    for c in rows:
        title = c.title.replace(allocation.DASH, "-")[:30]
        paras.append(
            (
                "mono",
                f"{c.number:<11}{title:<31}{c.due_on:%Y-%m-%d}  "
                f"{format_cents(c.original_cents):>12}{format_cents(c.open_cents):>12}",
            )
        )
    if not rows and not before:
        paras.append(("mono", f"{'':<11}None"))
    return paras


def _past_due(lot: Lot, on=None) -> int:
    """Past due on the current owners' account (their letters are about
    their own account only)."""
    from ledger.accounts import CURRENT
    from ledger.services import past_due

    return past_due(lot.pk, on, account=CURRENT)


def _values(lot: Lot, past_due_cents: int) -> dict[str, str]:
    """Placeholder values. ``{amount}`` is always the amount past due, never
    the whole balance: charges not yet due are not a delinquency
    (D33-acctfix-4). The attached statement shows the full balance under
    "Pay this amount"."""
    profile = CommunityProfile.get()
    names, salutation, _address, account_number = _recipients(lot)
    days = rule("collections.notice_days") or 10
    return {
        "owner_names": names,
        "salutation": salutation,
        "amount": format_cents(max(past_due_cents, 0)),
        "days": _days_text(int(days)),
        "date": timezone.localdate().strftime("%B %-d, %Y"),
        "account_number": account_number,
        "address": lot.street_address,
        "contact": profile.letter_contact or "the association's management office",
        "association": profile.legal_name,
    }


def statement_pdf(lot: Lot, *, since=None) -> bytes:
    paras, _balance = statement_paragraphs(lot, since=since)
    return pdf.render(paras, title=f"Account statement lot {lot.number}")


def notice_pdf(lot: Lot, kind: str) -> bytes:
    """A delinquency notice with the account statement attached (page 2)."""
    if kind not in (LetterKind.FIRST, LetterKind.SECOND, LetterKind.FINAL):
        raise ValueError("Unknown notice.")
    profile = CommunityProfile.get()
    statement, _balance = statement_paragraphs(lot)
    values = _values(lot, _past_due(lot))
    tpl = template(kind)
    names, salutation, address, _account = _recipients(lot)
    letter = [
        *_letterhead(profile),
        ("normal", ""),
        ("normal", timezone.localdate().strftime("%m/%d/%Y")),
        ("normal", ""),
        ("normal", f"**{names}**"),
    ]
    letter += [("normal", f"**{line}**") for line in address.splitlines() if line.strip()]
    if tpl.certified:
        letter.append(("normal", " " * 60 + "CERTIFIED AND REGULAR MAIL"))
    letter += [
        ("normal", ""),
        ("normal", f"RE:    **{fill(tpl.subject, values)}**") if tpl.subject else ("normal", ""),
        ("normal", ""),
        ("normal", f"Dear {salutation}:"),
        ("normal", ""),
    ]
    for para in fill(tpl.body, values).split("\n\n"):
        letter += [("normal", para.strip()), ("normal", "")]
    letter.append(("normal", "Sincerely,"))
    signature = list((profile.letter_signature or "").splitlines())
    letter += [
        ("normal", line) for line in (signature or [profile.legal_name, "Board of Directors"])
    ]
    return pdf.render(
        [*letter, ("pagebreak", ""), *statement],
        title=f"{tpl.get_kind_display()} lot {lot.number}",
    )
