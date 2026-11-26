"""Account statements and delinquency notices (Milestone 4).

Laid out like the association's existing paper: letterhead, date, the owners'
names and mailing address, a "RE:" line, the letter, the signature block; and
an account statement in plain words (D43): the amount due and the date to pay
by, what it is made of, what is paid ahead, how to pay (with a "Pay online"
QR code when online payments are on), the dated running-balance activity and
a tear-off remittance slip.

Wording is board-edited (LetterTemplate) with a fixed set of placeholders,
replaced by plain string substitution (never str.format, so a template can't
reach into Python objects). Nothing is sent from here: the board downloads,
prints and mails; each generation is audited.
"""

from __future__ import annotations

import datetime
import re

from django.utils import timezone

from community.models import Account, CommunityProfile, Lot, Ownership
from community.rules import rule
from community.terms import term_text
from core import pdf
from core.money import format_cents
from core.scoping import system_access
from ledger.models import LedgerEntry, LetterKind, LetterTemplate

PLACEHOLDERS = {
    "owner_names": "Everyone on the deed, e.g. Pat & Sam Example",
    "salutation": "The first owner's name",
    "amount": "The amount past due",
    "days": "The notice period from Community settings, in words and figures",
    "date": "Today's date",
    "account_number": "The account number",
    "address": "The home's address, then its lot number in brackets",
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
    # The default wording names the charge with the board's word (D42).
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
        # D44: an owner's own postal address (structured, else one typed
        # before D44), else the home's address.
        from community.services import postal_address

        mailing = ""
        for p in people:
            address, source = postal_address(p, lot)
            if source in ("structured", "old"):
                mailing = address
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


STATEMENT_MARGIN = 54  # 3/4 inch, so a typical statement fits on one page (D43)
_MADE_OF = "{:<11}{:<32}{:<14}{:>12}{:>13}"
_ACTIVITY = "{:<13}{:<41}{:>14}{:>14}{:>14}"
_SLIP = "{:<50}{:<18}{:>16}"


def _long(d: datetime.date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _short(d: datetime.date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _activity_text(entry) -> str:
    """What a line of the account is, in the words owners use: "Assessment -
    November 2026" rather than the posting's full description; a payment
    with its check number or reference."""
    from ledger.allocation import charge_title

    if entry.kind in ("assessment", "late_fee", "resale_fee"):
        text = charge_title(entry)
    else:
        text = entry.description
    if entry.kind == "payment" and entry.reference:
        text = f"{text} #{entry.reference}"
    return _one_line(text)


def _pay_online(lot: Lot):
    """The "Pay online" QR block, or None when online payments are not
    usable or the site address is unknown (D43)."""
    from core.qr import matrix
    from payments.services import fee_summary, statement_pay_url

    url = statement_pay_url(lot)
    if not url:
        return None
    return pdf.QrBlock(
        matrix(url),
        caption=(
            "**Pay online**",
            "Scan to pay online. You'll sign in first.",
            pdf.plain(url),
            fee_summary(),
            "Card payments usually clear within a day, bank transfers within a few business days.",
        ),
    )


def statement_paragraphs(lot: Lot, *, since=None, as_of=None) -> tuple[list, int]:
    """The statement page of the current owners' account (D33-acctfix-1: a
    former owner's entries and credit are never on it); returns (paragraphs,
    amount due).

    Laid out for someone who is not an accountant (D43): a box with the
    amount due and the date to pay by (or the credit), what that amount is
    made of (each open charge and what is left on it), what is paid ahead,
    how to pay (a "Pay online" QR code when online payments are on, and by
    check), the account activity with a running balance, and the tear-off
    slip at the foot of the last page. Amounts and entries are exactly the
    account's; only the wording and layout are the statement's own."""
    from ledger import allocation
    from ledger.accounts import MOVED_AWAY, account_q, current_account_id
    from ledger.services import moved_notes, upcoming_charges

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
    amount = max(balance, 0)
    street = _one_line(lot.street_address)

    # Open charges of this account: due by today (what the amount is made
    # of), from before this ownership (one line), and not due yet.
    charges = allocation.charges(lot.pk, open_only=False, account=account)
    open_rows = [c for c in charges if c.open_cents > 0 and not c.reversed]
    before_rows = [c for c in open_rows if owner_since and c.due_on < owner_since]
    before = sum(c.open_cents for c in before_rows)
    due_rows = [c for c in open_rows if c not in before_rows and c.due_on <= as_of]
    paid_ahead = [
        c
        for c in charges
        if c.due_on > as_of and c.open_cents < c.original_cents and not c.reversed
    ]
    coming = [c for c in open_rows if c.due_on > as_of and c not in paid_ahead]
    overdue = [c.due_on for c in open_rows if c.due_on < as_of]
    past_due_since = min(overdue) if balance > 0 and overdue else None
    pay_by = min((c.due_on for c in due_rows), default=as_of)
    dues_cents = rule("dues.monthly_cents") or 0
    due_day = min(rule("dues.due_day") or 1, 28)

    paras: list = [*_statement_letterhead(profile), ("gap", "")]
    paras += [
        ("title", f"Account statement as of {_long(as_of)}"),
        ("normal", f"**{pdf.plain(names)}**"),
    ]
    paras += [
        ("normal", f"**{pdf.plain(line.strip())}**")
        for line in address.splitlines()
        if line.strip()
    ]

    # 1. The summary box.
    paras += [("gap", ""), ("box_start", "")]
    if balance > 0:
        paras.append(("big", f"Amount due: {format_cents(amount)}"))
        if past_due_since:
            paras.append(
                (
                    "normal",
                    f"**Past due since {_long(past_due_since)}.** Please pay as soon as you can.",
                )
            )
        else:
            paras.append(("normal", f"Pay by: **{_long(pay_by)}**"))
    elif balance < 0:
        paras.append(("big", f"You have a credit of {format_cents(-balance)}. Nothing to pay."))
        see = " (see Paid ahead below)" if dues_cents else ""
        paras.append(("normal", f"Your credit pays your next charges automatically{see}."))
    else:
        paras.append(("big", "Nothing to pay. Thank you."))
    paras.append(
        (
            "normal",
            f"Account number **{pdf.plain(account_number)}**     "
            f"Home **{pdf.plain(_one_line(lot.legal_label))}**",
        )
    )
    if dues_cents:
        nxt = upcoming_charges(0, lot_id=lot.pk, months=1, on=as_of)
        text = term_text(
            f"The monthly {{term}} of {format_cents(dues_cents)} is due on the {_ordinal(due_day)}"
        )
        text += f" of each month; the next is due {_long(nxt[0].due_on)}." if nxt else "."
        paras.append(("note", text))
    past_due_amount = _past_due(lot, as_of)
    if past_due_amount > 0:
        message = fill(template(LetterKind.STATEMENT).body, _values(lot, past_due_amount))
        paras.append(("note", pdf.plain(message)))
    paras.append(("box_end", ""))

    # 2. What the amount is made of.
    if due_rows or before or balance > 0:
        paras += [
            ("subheading", "What this amount is made of"),
            ("rowhead", _MADE_OF.format("Charge", "What for", "Was due", "Charged", "Left to pay")),
        ]
        if before:
            paras.append(
                (
                    "row",
                    _MADE_OF.format("", "From before your ownership", "", "", format_cents(before)),
                )
            )
        for c in due_rows:
            row = _MADE_OF.format(
                c.number,
                _one_line(c.title)[:31],
                _short(c.due_on),
                format_cents(c.original_cents),
                format_cents(c.open_cents),
            )
            paras.append(("row", row + ("  partly paid" if c.status == "partial" else "")))
        rest = balance - before - sum(c.open_cents for c in due_rows)
        if rest:
            label = "Paid ahead on charges not due yet" if rest < 0 else "Other balance"
            paras.append(("row", _MADE_OF.format("", label, "", "", format_cents(rest))))
        paras += [
            ("rowhead", _MADE_OF.format("", "Amount due", "", "", format_cents(balance))),
            (
                "note",
                "Charged is each charge's full amount; Left to pay is what is still open on it.",
            ),
        ]

    # 3. Paid ahead (a prepayment draws down month by month, D30) and
    # charges not due yet.
    credit_left = allocation.unapplied_credit(lot.pk, account=account) if balance < 0 else 0
    covered = []
    if credit_left and dues_cents:
        months = min(12, credit_left // dues_cents + 1)
        covered = upcoming_charges(-credit_left, lot_id=lot.pk, months=months, on=as_of)
    if paid_ahead or credit_left:
        paras += [
            ("subheading", "Paid ahead"),
            (
                "note",
                term_text(
                    "Your credit pays these monthly {terms} automatically as each one comes due:"
                ),
            ),
        ]
        for c in paid_ahead:
            state = (
                "covered by your credit"
                if c.open_cents <= 0
                else f"partly covered, {format_cents(c.open_cents)} left to pay"
            )
            paras.append(
                (
                    "row",
                    f"{_one_line(c.title)[:31]:<32}{'due ' + _short(c.due_on):<18}"
                    f"{format_cents(c.original_cents):>12}  {state}",
                )
            )
        used = 0
        for u in covered:
            if not u.covered_by_credit:
                break
            used += u.amount_cents
            title = term_text(f"{{Term}} {allocation.DASH} {u.due_on:%B %Y}")
            paras.append(
                (
                    "row",
                    f"{title:<32}{'due ' + _short(u.due_on):<18}"
                    f"{format_cents(u.amount_cents):>12}  covered by your credit",
                )
            )
        left = credit_left - used
        if left > 0:
            nxt = next((u for u in covered if not u.covered_by_credit), None)
            paras.append(
                (
                    "note",
                    f"The remaining {format_cents(left)} of your credit goes toward the "
                    + term_text("{term}")
                    + " due "
                    f"{_long(nxt.due_on)}."
                    if nxt
                    else f"{format_cents(left)} of your credit is still to be used.",
                )
            )
    if coming:
        paras.append(("subheading", "Charges not due yet (not in the amount due)"))
        for c in coming:
            paras.append(
                (
                    "row",
                    f"{c.number:<11}{_one_line(c.title)[:31]:<32}{'due ' + _short(c.due_on):<18}"
                    f"{format_cents(c.open_cents):>12}",
                )
            )

    # 4. How to pay.
    remit = [line.strip() for line in (profile.remit_to or "").splitlines() if line.strip()] or [
        profile.legal_name
    ]
    paras.append(("subheading", "How to pay"))
    qr = _pay_online(lot)
    if qr is not None:
        paras.append(("qr", qr))
    paras.append(
        (
            "note",
            f"**By check:** payable to **{pdf.plain(profile.legal_name)}**, mailed to "
            f"{pdf.plain(', '.join(remit))}. Write your account number "
            f"**{pdf.plain(account_number)}** on the check and send the slip below with it. "
            "Allow 10 to 15 business days for processing.",
        )
    )
    if profile.letter_contact.strip() and not past_due_amount:  # else in the box
        paras.append(
            (
                "note",
                f"Questions about this statement? Contact {pdf.plain(profile.letter_contact)}.",
            )
        )

    # 5. Account activity.
    activity = [
        ("subheading", "Account activity"),
        (
            "note",
            "Charges add to what you owe; payments and credits take it down. Balance is the "
            "total after each line (a minus sign is a credit).",
        ),
        ("rowhead", _ACTIVITY.format("Date", "What happened", "Charges", "Payments", "Balance")),
    ]
    start = owner_since or (entries[0].posted_on if entries else as_of)
    activity.append(
        (
            "row",
            _ACTIVITY.format(
                "", f"Balance brought forward on {_short(start)}", "", "", format_cents(forward)
            ),
        )
    )
    running = forward
    notes = moved_notes(entries)
    for e in entries:
        running += e.amount_cents
        charge = format_cents(e.amount_cents) if e.amount_cents > 0 else ""
        payment = format_cents(-e.amount_cents) if e.amount_cents < 0 else ""
        activity.append(
            (
                "row",
                _ACTIVITY.format(
                    _short(e.posted_on),
                    _activity_text(e)[:40],
                    charge,
                    payment,
                    format_cents(running),
                ),
            )
        )
        if e.pk in notes:
            note = notes[e.pk]
            activity.append(("row", f"{'':<13}Note: this line was {note[:1].lower()}{note[1:]}."))
    if not entries:
        activity.append(("row", f"{'':<13}No activity in this period."))

    # 6. The tear-off slip, at the foot of the last page.
    if amount and past_due_since:
        due_text = "Past due"
    elif amount:
        due_text = _short(pay_by)
    else:
        due_text = "Nothing due"
    slip = [
        ("bottom", ""),
        ("cut", ""),
        ("note", "Please detach and return this part with your payment."),
        ("rowhead", _SLIP.format("REMITTANCE SLIP", "Account number:", account_number[:16])),
        ("row", _SLIP.format(profile.legal_name[:48], "Amount due:", format_cents(amount))),
        ("row", _SLIP.format(names[:48], "Pay by:", due_text)),
        ("row", _SLIP.format(street[:48], "Amount enclosed:", "$__________")),
        ("row", f"Mail to: {', '.join(remit)}"),
    ]
    # One page when it fits; otherwise page 1 keeps the summary, how to pay
    # and the slip, and the activity follows on its own page(s).
    whole = [*paras, *activity, *slip]
    if pdf.page_count(whole, margin=STATEMENT_MARGIN) == 1:
        return whole, balance
    note = ("note", "Your account activity is on the next page.")
    return [*paras, note, *slip, ("pagebreak", ""), *activity], balance


def _statement_letterhead(profile) -> list[tuple[str, str]]:
    lines = [line for line in (profile.letterhead or "").splitlines() if line.strip()]
    if not lines:
        lines = [profile.legal_name]
    return [("heading", lines[0])] + [("note", pdf.plain(line)) for line in lines[1:]]


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
        "address": lot.legal_label,
        "contact": profile.letter_contact or "the association's management office",
        "association": profile.legal_name,
    }


def statement_pdf(lot: Lot, *, since=None) -> bytes:
    paras, _balance = statement_paragraphs(lot, since=since)
    return pdf.render(paras, title=f"Account statement {lot.legal_label}", margin=STATEMENT_MARGIN)


def balance_questions(profile=None) -> dict:
    """Whom a former owner asks about their old balance, and where checks go
    (Letters and certificates). Contact details only: no form (WT-010)."""
    profile = profile or CommunityProfile.get()
    return {
        "contact": (profile.letter_contact or profile.signin_help_contact or "").strip(),
        "payable_to": profile.legal_name,
        "remit_lines": [line for line in (profile.remit_to or "").splitlines() if line.strip()],
    }


def former_statement_pdf(*, name: str, lot: Lot, account_number: str, start, end, statement):
    """A former owner's statement for their own ownership period (WT-010):
    the period's entries with a running balance, the balance when they sold,
    and whom to ask. Built from ``ledger.services.period_statement``, the
    same figures My history shows; nothing from after the sale."""
    profile = CommunityProfile.get()
    paras: list = [*_statement_letterhead(profile), ("gap", "")]
    paras += [
        ("title", f"Account statement for {_short(start)} to {_short(end)}"),
        ("normal", f"**{pdf.plain(name)}**"),
        (
            "normal",
            f"Account number **{pdf.plain(account_number)}**     "
            f"Home **{pdf.plain(_one_line(lot.legal_label))}**",
        ),
        ("gap", ""),
        ("subheading", "Account activity"),
        (
            "note",
            "Charges add to what you owe; payments and credits take it down. Balance is the "
            "total after each line (a minus sign is a credit).",
        ),
        ("rowhead", _ACTIVITY.format("Date", "What happened", "Charges", "Payments", "Balance")),
        (
            "row",
            _ACTIVITY.format(
                "",
                f"Balance brought forward on {_short(start)}",
                "",
                "",
                format_cents(statement.brought_forward),
            ),
        ),
    ]
    running = statement.brought_forward
    for e in statement.entries:
        running += e.amount_cents
        paras.append(
            (
                "row",
                _ACTIVITY.format(
                    _short(e.posted_on),
                    _activity_text(e)[:40],
                    format_cents(e.amount_cents) if e.amount_cents > 0 else "",
                    format_cents(-e.amount_cents) if e.amount_cents < 0 else "",
                    format_cents(running),
                ),
            )
        )
    if not statement.entries:
        paras.append(("row", f"{'':<13}No activity in this period."))
    paras += [("gap", ""), ("normal", f"**Balance: {format_cents(statement.balance)}**")]
    ask = balance_questions(profile)
    if ask["contact"]:
        paras.append(("normal", f"Questions about this balance? {pdf.plain(ask['contact'])}"))
    if ask["remit_lines"]:
        paras.append(
            (
                "normal",
                f"Checks payable to {pdf.plain(ask['payable_to'])}, mailed to "
                + pdf.plain(", ".join(line.strip() for line in ask["remit_lines"])),
            )
        )
    return pdf.render(paras, title=f"Account statement {lot.legal_label}", margin=STATEMENT_MARGIN)


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
        title=f"{tpl.get_kind_display()} {lot.legal_label}",
        margin=STATEMENT_MARGIN,
    )
