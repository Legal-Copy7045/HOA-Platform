"""The monthly statement email (D42).

When the month's charge (the board's word for it: "Assessment" by default,
community/terms.py) is posted, each active account's people who may see its
balance get one email: the home's address, the amount due now, a status
line, links to My account (and to Pay online when it works), how to stop
these emails, and the account statement PDF attached.

* Who: owners on the deed and authorized or view-only contacts of the
  account, with an active login, unless they unticked "Email me a statement"
  on My profile (on by default, owner ruling). Never tenants, former owners
  or another home's people.
* Once per account per month: a ``StatementEmail`` row is written first
  (unique per account and month), so a re-run or a second posting sends
  nothing more.
* The owner accepted that the email carries financial details (the amount
  and the PDF); it carries no sign-in tokens, only links to signed-in pages.
* Fail-soft per recipient; logs counts only, never addresses.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.models import Account, AccountContact, ContactKind, ContactProfile, Ownership
from community.rules import rule
from community.terms import Term, charge_term
from core.money import format_cents
from core.scoping import system_access
from ledger.models import EntryKind, LedgerEntry, StatementEmail

logger = logging.getLogger(__name__)


def autopay_line(account) -> str | None:
    """What the email says when autopay is set up for ``account``; None
    when it is not. The autopay work provides
    ``payments.autopay.describe_for_statement(account)``; until then there
    is no autopay, so nothing is said."""
    try:
        from payments.autopay import describe_for_statement
    except ImportError:
        return None
    return describe_for_statement(account)


@dataclass(frozen=True)
class Recipient:
    email: str
    may_pay: bool  # owners and authorized contacts; view-only contacts may not


def recipients(account: Account, on: datetime.date | None = None) -> list[Recipient]:
    """People who may see this account's balance and did not opt out."""
    on = on or timezone.localdate()
    with system_access("statement email recipients"):
        people: dict[int, bool] = {}
        if account.closed_on is None or account.closed_on > on:
            for person_id in (
                Ownership.objects.active(on)
                .filter(lot_id=account.lot_id)
                .values_list("person_id", flat=True)
            ):
                people[person_id] = True
            for person_id, kind in (
                AccountContact.objects.active(on)
                .filter(account=account, kind__in=[ContactKind.AUTHORIZED, ContactKind.VIEW_ONLY])
                .values_list("person_id", "kind")
            ):
                people[person_id] = people.get(person_id, False) or kind == ContactKind.AUTHORIZED
        opted_out = set(
            ContactProfile.objects.filter(
                person_id__in=list(people), statement_emails=False
            ).values_list("person_id", flat=True)
        )
        from community.models import Person

        rows = Person.objects.filter(
            pk__in=[p for p in people if p not in opted_out],
            user__isnull=False,
            user__is_active=True,
        ).values_list("pk", "user__email")
        out = {email: Recipient(email, people[pk]) for pk, email in rows if email}
    return [out[e] for e in sorted(out)]


def due_date(year: int, month: int) -> datetime.date:
    return datetime.date(year, month, min(rule("dues.due_day") or 1, 28))


def status_line(account: Account, *, year: int, month: int, term: Term | None = None) -> str:
    """One line on what to do this month (D30/D33: this account's money only)."""
    from ledger.services import balance

    term = term or charge_term()
    period = f"{year:04d}-{month:02d}"
    owed = balance(account.lot_id, account=account.pk)
    with system_access("statement email charge"):
        charge = (
            LedgerEntry.objects.filter(
                lot_id=account.lot_id,
                account_id=account.pk,
                kind=EntryKind.ASSESSMENT,
                period=period,
                reversed_by__isnull=True,
            )
            .order_by("pk")
            .first()
        )
    if owed <= 0:
        if charge is not None:
            before = balance(
                account.lot_id,
                as_of=charge.posted_on - datetime.timedelta(days=1),
                account=account.pk,
            )
            if before <= -charge.amount_cents:
                return (
                    "Nothing to pay: this month is covered by your prepaid credit "
                    f"(credit left after this month: {format_cents(-owed)})."
                )
        return "Nothing to pay."
    autopay = autopay_line(account)
    if autopay:
        return autopay
    return f"Please pay {format_cents(owed)} by {due_date(year, month):%B %-d, %Y}."


def _body(account, *, year, month, term, owed, line, may_pay, pay_url) -> str:
    v = term.variants()
    base = settings.PORTAL_BASE_URL
    month_name = datetime.date(year, month, 1).strftime("%B %Y")
    lines = [
        f"Your {month_name} {v['term']} statement for {account.lot.street_address}.",
        "",
        f"Amount due now: {format_cents(max(owed, 0))}",
        line,
        "",
        "See your account (sign in first):",
        base + reverse("my_account"),
    ]
    if may_pay and pay_url and owed > 0:
        lines += ["", "Pay online (sign in first):", base + pay_url]
    lines += [
        "",
        "Your statement is attached (PDF).",
        "",
        f'You get this email because "Email me a statement when the monthly {v["term"]} is '
        'charged" is ticked on My profile. To stop these emails, untick it there:',
        base + reverse("my_profile"),
        "",
        f"-- \n{settings.SITE_NAME}",
    ]
    return "\n".join(lines)


def send_for_account(account: Account, *, year: int, month: int, term: Term | None = None) -> int:
    """Email this month's statement to the account's people, once. Returns
    how many emails were sent (0 when it was sent before or nobody wants it)."""
    from ledger import services

    term = term or charge_term()
    period = f"{year:04d}-{month:02d}"
    people = recipients(account)
    if not people:
        return 0
    with system_access("statement email marker"):
        if StatementEmail.objects.filter(account=account, period=period).exists():
            return 0
        try:
            with transaction.atomic():
                StatementEmail.objects.create(
                    account=account, period=period, recipients=len(people)
                )
        except IntegrityError:
            return 0  # another run got here first
    owed = services.balance(account.lot_id, account=account.pk)
    line = status_line(account, year=year, month=month, term=term)
    pay_url = _pay_url(account) if owed > 0 else ""
    pdf = services.statement_pdf(account.lot)
    v = term.variants()
    subject = (
        f"[{settings.SITE_NAME}] Your {datetime.date(year, month, 1):%B} {v['term']} statement"
    )
    sent = failed = 0
    connection = get_connection(fail_silently=False)
    for person in people:
        message = EmailMessage(
            subject,
            _body(
                account,
                year=year,
                month=month,
                term=term,
                owed=owed,
                line=line,
                may_pay=person.may_pay,
                pay_url=pay_url,
            ),
            None,
            [person.email],
            connection=connection,
        )
        # No names or numbers in the file name.
        message.attach(f"statement-{period}.pdf", pdf, "application/pdf")
        try:
            message.send()
            sent += 1
        except Exception:
            failed += 1
    record_event(
        "ledger.statement_emailed",
        actor=None,
        target=account,
        metadata={"account_id": account.pk, "period": period, "recipients": sent},
    )
    if failed:
        logger.warning("ledger.statement_email_failed", extra={"failed": failed})
    return sent


def _pay_url(account) -> str:
    """The signed-in pay page, when online payments work (no tokens)."""
    try:
        from payments.services import online_payments_enabled

        if not online_payments_enabled():
            return ""
        return reverse("pay_online", args=[account.lot_id]) + "?all=1"
    except Exception:
        return ""


def send_monthly_statements(*, year: int, month: int) -> dict[str, int]:
    """Every active account's statement for the month, each at most once."""
    term = charge_term()
    accounts = emails = 0
    with system_access("statement emails"):
        rows = list(Account.objects.active().select_related("lot").order_by("lot__number"))
    for account in rows:
        try:
            n = send_for_account(account, year=year, month=month, term=term)
        except Exception:
            logger.warning("ledger.statement_email_account_failed", extra={"count": 1})
            continue
        if n:
            accounts += 1
            emails += n
    logger.info("ledger.statement_emails", extra={"accounts": accounts, "emails": emails})
    return {"accounts": accounts, "emails": emails}
