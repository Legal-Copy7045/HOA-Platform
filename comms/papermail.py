"""Paper mail: mail merge for letters, envelopes and labels (D55).

Owners on the deed who have not agreed to official notices by email (D44)
get them on paper. This module prepares a mailing (who gets a letter, the
wording with its {placeholders}), prints it on demand as one PDF of letters
laid out for a #10 window envelope, a PDF of #10 envelopes or a sheet of
Avery 5160-style labels, and records that it was mailed. Nothing is stored
but the append-only ``Mailing`` record: the PDFs are made again from it
whenever they are downloaded, and never saved.

Board and manager only (``mail.print``): preparing and printing is data
entry; who gets paper follows each owner's own choice. Audit events carry
counts only, never names or addresses; nothing here is logged.

The optional USPS Intelligent Mail barcode (core/imb.py) goes in the
address block of window letters and envelopes once a Mailer ID is entered on
Paper mail settings; each letter gets a serial number, kept with the ZIP
routing in an append-only ``MailPiece`` so a delivery scan can be matched to
the home. Labels never carry it: the barcode (about 2.9 inches) is longer
than a 2-5/8 inch label.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from comms.models import (
    ImbSerial,
    Mailing,
    MailingAudience,
    MailingEvent,
    MailingEventKind,
    MailingKind,
    MailPiece,
    PaperMailSettings,
    WindowStyle,
)
from core import imb, pdf
from core.policy import require
from core.scoping import system_access


class PaperMailError(ValueError):
    """A mailing that cannot be prepared, printed or recorded as asked."""


PLACEHOLDERS = {
    "owner_names": "Everyone named on this letter, e.g. Pat Example & Sam Example",
    "salutation": 'The same names for "Dear ...:" (the letter adds that line itself)',
    "address": "The home's address, then its lot number in brackets",
    "mailing_address": "Where this letter goes, on one line",
    "account_number": "The home's current account number",
    "date": "The letter's date",
    "association": "The association's legal name",
    "contact": "The contact line from Letters and certificates",
}
MEETING_PLACEHOLDERS = {
    "meeting_title": "The meeting's title",
    "meeting_kind": "Board meeting, annual meeting, ...",
    "meeting_when": "Day, date and time",
    "meeting_where": "The place and the join link, as in the notice email",
    "join_link": "The join link alone (blank for meetings in person only)",
    "agenda_link": "The meeting's page on the portal (sign-in required)",
}
BUDGET_PLACEHOLDERS = {
    "budget": "The budget's name, e.g. FY2027 budget",
    "budget_income": "Total income",
    "budget_expense": "Total expenses",
    "monthly_amount": "The monthly charge and its citation",
    "adopted_on": "The date the board adopted it",
    "budget_link": "Budget and reserves on the portal",
}
TEMPLATE_PLACEHOLDERS = {
    "amount": "The amount past due on this home's current account",
    "days": "The notice period, in words and figures",
}

_TOKEN = re.compile(r"\{([a-z_]+)\}")
LABELS_PER_SHEET = 30
MAX_BODY = 8000

# --- Settings -------------------------------------------------------------------------

_LIMITS = {
    "address_left_in": (Decimal("0.25"), Decimal("4.50")),
    "address_top_in": (Decimal("0.50"), Decimal("5.00")),
    "return_left_in": (Decimal("0.25"), Decimal("4.50")),
    "return_top_in": (Decimal("0.25"), Decimal("3.00")),
    "envelope_shift_x_in": (Decimal("-0.50"), Decimal("0.50")),
    "envelope_shift_y_in": (Decimal("-0.50"), Decimal("0.50")),
    "label_shift_x_in": (Decimal("-0.25"), Decimal("0.25")),
    "label_shift_y_in": (Decimal("-0.25"), Decimal("0.25")),
}
# A few Service Type IDs (USPS table effective 2023-11-30). Which one fits
# the association's mailing is for USPS to confirm; the portal only offers.
SERVICE_TYPES = (
    (
        "310",
        "310: First-Class, basic, with Informed Visibility tracking "
        "(recommended; confirm with USPS)",
    ),
    ("300", "300: First-Class, basic, no tracking"),
    ("040", "040: older First-Class basic code; use only if USPS tells you to"),
)
STID_URL = "https://postalpro.usps.com/mailing/service-type-identifiers"


def settings() -> PaperMailSettings:
    return PaperMailSettings.get()


def save_settings(*, actor, request=None, **values) -> PaperMailSettings:
    """Window style and positions (inches), duplex, and the Intelligent
    Mail barcode settings. Audited without values."""
    require(actor, "mail.print")
    problems = []
    with transaction.atomic():
        current = PaperMailSettings.objects.select_for_update().get(pk=settings().pk)
        if "window" in values:
            if values["window"] not in WindowStyle.values:
                problems.append("Choose the kind of window envelope.")
            else:
                current.window = values["window"]
        for field, (low, high) in _LIMITS.items():
            if field not in values:
                continue
            try:
                value = Decimal(str(values[field]).strip()).quantize(Decimal("0.01"))
            except Exception:
                problems.append(f"Enter a number of inches for {field.replace('_', ' ')}.")
                continue
            if not low <= value <= high:
                problems.append(
                    f"{field.replace('_in', '').replace('_', ' ').capitalize()} must be "
                    f"between {low} and {high} inches."
                )
                continue
            setattr(current, field, value)
        if "duplex" in values:
            current.duplex = bool(values["duplex"])
        if "mailer_id" in values:
            mid = (values["mailer_id"] or "").strip()
            if mid and not re.fullmatch(r"[0-9]{6}|[0-9]{9}", mid):
                problems.append("A USPS Mailer ID is 6 or 9 digits.")
            else:
                current.mailer_id = mid
        if "barcode_id" in values:
            bid = (values["barcode_id"] or "").strip()
            if not re.fullmatch(r"[0-9][0-4]", bid):
                problems.append("The Barcode ID is 2 digits; the second is 0 to 4.")
            else:
                current.barcode_id = bid
        if "service_type" in values:
            stid = (values["service_type"] or "").strip()
            if not re.fullmatch(r"[0-9]{3}", stid):
                problems.append("The Service Type ID is 3 digits.")
            else:
                current.service_type = stid
        if "imb_enabled" in values:
            current.imb_enabled = bool(values["imb_enabled"])
        if current.imb_enabled and len(current.mailer_id) not in (6, 9):
            problems.append("Enter your USPS Mailer ID before switching the barcode on.")
        if problems:
            raise PaperMailError(" ".join(problems))
        current.updated_by = actor if getattr(actor, "pk", None) else None
        current.save()
        record_event(
            "mailing.settings_changed",
            actor=actor,
            request=request,
            target=current,
            metadata={"window": current.window, "barcode": current.barcode_on},
        )
    return current


# --- Recipients -----------------------------------------------------------------------


def who_gets_paper(*, everyone: bool = False, lot_id: int | None = None) -> list[dict]:
    """Letter groups now: one per home and postal address (community
    service). Internal: views check ``mail.print`` first."""
    from community.services import paper_mail_groups

    return paper_mail_groups(everyone=everyone, lot_id=lot_id)


def counts(groups: list[dict]) -> dict[str, int]:
    return {
        "letters": len(groups),
        "homes": len({g["lot"].pk for g in groups}),
        "owners": sum(len(g["people"]) for g in groups),
        "no_address": sum(1 for g in groups if not g["address"]),
    }


# --- Wording --------------------------------------------------------------------------


def fill(text: str, values: dict[str, str]) -> str:
    """Replace known {placeholders} by plain substitution (never
    str.format); anything else is left as written."""
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text or "")


def _long(d: datetime.date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _names_and(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _profile():
    from community.models import CommunityProfile

    return CommunityProfile.get()


def letter_values(letter: dict, mailing: Mailing, profile=None) -> dict[str, str]:
    profile = profile or _profile()
    values = dict(mailing.shared_values or {})
    values.update(
        {
            "owner_names": letter["owner_names"],
            "salutation": _names_and(letter["names"]),
            "address": letter["lot"].legal_label,
            "mailing_address": ", ".join(
                " ".join(line.split()) for line in letter["address"].splitlines() if line.strip()
            ),
            "account_number": letter.get("account_number", ""),
            "date": _long(mailing.letter_date),
            "association": profile.legal_name,
            "contact": profile.letter_contact or "the association's management office",
        }
    )
    if mailing.kind == MailingKind.TEMPLATE:
        from ledger.services import paper_letter_values

        values.update(paper_letter_values(letter["lot"]))
    return {k: str(v) for k, v in values.items()}


def source_defaults(*, meeting=None, budget=None, stage: str = "", template: str = "") -> dict:
    """Prefill for a new mailing: kind, subject, body, the shared values and
    which source it came from. Meetings and budgets come through their own
    services; the caller fetched them through their scoping."""
    if meeting is not None:
        from governance.services import GovernanceError, meeting_notice_values

        try:
            found = meeting_notice_values(meeting)
        except GovernanceError as exc:
            raise PaperMailError(str(exc)) from exc
        return {"kind": MailingKind.MEETING, "meeting_id": meeting.pk, **found}
    if budget is not None:
        from ledger.services import LedgerError, budget_notice_values

        try:
            found = budget_notice_values(budget, stage)
        except LedgerError as exc:
            raise PaperMailError(str(exc)) from exc
        return {"kind": MailingKind.BUDGET, "budget_id": budget.pk, "budget_stage": stage, **found}
    if template:
        from ledger.services import LedgerError, paper_letter_template

        try:
            found = paper_letter_template(template)
        except LedgerError as exc:
            raise PaperMailError(str(exc)) from exc
        return {"kind": MailingKind.TEMPLATE, "template_kind": template, "values": {}, **found}
    return {"kind": MailingKind.LETTER, "values": {}, "subject": "", "body": ""}


def placeholders_for(kind: str) -> dict[str, str]:
    extra = {
        MailingKind.MEETING: MEETING_PLACEHOLDERS,
        MailingKind.BUDGET: BUDGET_PLACEHOLDERS,
        MailingKind.TEMPLATE: TEMPLATE_PLACEHOLDERS,
    }.get(kind, {})
    return {**PLACEHOLDERS, **extra}


# --- Creating and recording -----------------------------------------------------------


def _clean(subject: str, body: str) -> tuple[str, str]:
    from community.terms import term_text

    subject = " ".join((subject or "").split())[:200]
    body = (body or "").replace("\r\n", "\n").strip()
    if not subject:
        raise PaperMailError("Enter a subject.")
    if not body:
        raise PaperMailError("Write the letter.")
    if len(body) > MAX_BODY:
        raise PaperMailError(f"The letter is too long (at most {MAX_BODY} characters).")
    return term_text(subject), term_text(body)


def preview(
    *, actor, defaults: dict, subject: str, body: str, everyone: bool, lot_id: int | None
) -> dict:
    """The groups this would go to and the first letter, filled in, as text
    (nothing is recorded). Board and manager."""
    require(actor, "mail.print")
    subject, body = _clean(subject, body)
    groups = who_gets_paper(everyone=everyone, lot_id=lot_id)
    draft = _unsaved(defaults, subject, body, everyone, lot_id, groups)
    first = None
    if groups:
        from community.services import paper_mail_letters

        letters = paper_mail_letters(draft.groups[:1])
        if letters:
            first = rendered_text(draft, letters[0])
    return {"groups": groups, "counts": counts(groups), "first": first}


def _unsaved(defaults, subject, body, everyone, lot_id, groups) -> Mailing:
    from community.notices import CONSENT_VERSION

    return Mailing(
        kind=defaults["kind"],
        subject=subject,
        body=body,
        audience=MailingAudience.ALL if everyone else MailingAudience.PAPER,
        only_lot_id=lot_id,
        meeting_id=defaults.get("meeting_id"),
        budget_id=defaults.get("budget_id"),
        budget_stage=defaults.get("budget_stage", ""),
        template_kind=defaults.get("template_kind", ""),
        shared_values=defaults.get("values") or {},
        groups=[{"lot": g["lot"].pk, "people": list(g["people"])} for g in groups],
        homes_count=len({g["lot"].pk for g in groups}),
        letters_count=len(groups),
        consent_version=CONSENT_VERSION,
        letter_date=timezone.localdate(),
    )


def create_mailing(
    *,
    actor,
    defaults: dict,
    subject: str,
    body: str,
    everyone: bool = False,
    lot_id: int | None = None,
    request=None,
) -> Mailing:
    """Record a mailing (append-only) with who it goes to as of now."""
    require(actor, "mail.print")
    subject, body = _clean(subject, body)
    groups = who_gets_paper(everyone=everyone, lot_id=lot_id)
    if not groups:
        raise PaperMailError("Nobody to write to: no owner here gets paper.")
    mailing = _unsaved(defaults, subject, body, everyone, lot_id, groups)
    mailing.created_by = actor
    with transaction.atomic():
        mailing.save()
        record_event(
            "mailing.created",
            actor=actor,
            request=request,
            target=mailing,
            metadata={
                "kind": mailing.kind,
                "audience": mailing.audience,
                "letters": mailing.letters_count,
                "homes": mailing.homes_count,
                "consent_version": mailing.consent_version,
            },
        )
    return mailing


def closing_event(mailing: Mailing) -> MailingEvent | None:
    with system_access("mailing status"):
        return MailingEvent.objects.filter(mailing=mailing).first()


def record_mailed(
    mailing: Mailing, *, actor, mailed_on: datetime.date | None = None, discard=False, request=None
) -> MailingEvent:
    """Mailed on a date (not in the future), or not sent: once, append-only."""
    require(actor, "mail.print")
    if not discard:
        if mailed_on is None:
            raise PaperMailError("Enter the date it was mailed.")
        if mailed_on > timezone.localdate():
            raise PaperMailError("The mailing date cannot be in the future.")
        if mailed_on < timezone.localdate(mailing.created_at) - datetime.timedelta(days=1):
            raise PaperMailError("The mailing date is before the mailing was prepared.")
    try:
        with transaction.atomic():
            event = MailingEvent.objects.create(
                mailing=mailing,
                kind=MailingEventKind.DISCARDED if discard else MailingEventKind.MAILED,
                mailed_on=None if discard else mailed_on,
                recorded_by=actor,
            )
            record_event(
                "mailing.mailed",
                actor=actor,
                request=request,
                target=mailing,
                metadata={
                    "kind": event.kind,
                    "letters": mailing.letters_count,
                    "homes": mailing.homes_count,
                },
            )
    except IntegrityError as exc:
        raise PaperMailError("This mailing is already marked.") from exc
    return event


def meeting_status(meeting) -> dict:
    """For the meeting page (board and manager): the latest paper notice
    mailing for this meeting and when it was mailed, counts only."""
    with system_access("meeting paper notices"):
        mailings = list(Mailing.objects.filter(meeting_id=meeting.pk).order_by("-created_at"))
        events = {
            e.mailing_id: e
            for e in MailingEvent.objects.filter(mailing__in=[m.pk for m in mailings])
        }
    mailed = [(m, events[m.pk]) for m in mailings if m.pk in events and events[m.pk].mailed_on]
    open_ = [m for m in mailings if m.pk not in events]
    return {
        "paper_mailed": mailed[0] if mailed else None,
        "paper_open": open_[0] if open_ else None,
    }


def open_mailings() -> list[Mailing]:
    """Mailings not yet marked mailed or not sent (for My items)."""
    with system_access("open mailings"):
        closed = MailingEvent.objects.values("mailing_id")
        return list(Mailing.objects.exclude(pk__in=closed).order_by("created_at"))


def meeting_titles(ids) -> dict[int, str]:
    from governance.models import Meeting

    return dict(Meeting.objects.filter(pk__in=list(ids)).values_list("pk", "title"))


# --- Intelligent Mail barcode ---------------------------------------------------------


def _pieces(mailing: Mailing, letters: list[dict]) -> dict[int, str]:
    """{group index: bars} for this mailing's letters, when the barcode is
    on. Serial numbers are taken once per letter (locked counter), recorded
    as append-only MailPiece rows and reused when the mailing is reprinted."""
    conf = settings()
    with system_access("mail pieces"):
        existing = list(MailPiece.objects.filter(mailing=mailing))
    if not existing and not conf.barcode_on:
        return {}
    if not existing:
        mid = conf.mailer_id
        width = 9 if len(mid) == 6 else 6
        with transaction.atomic():
            ImbSerial.objects.get_or_create(mailer_id=mid)
            counter = ImbSerial.objects.select_for_update().get(mailer_id=mid)
            with system_access("mail pieces"):
                existing = list(MailPiece.objects.filter(mailing=mailing))
                if not existing:
                    first = counter.next_serial
                    if first + len(letters) > 10**width:
                        raise PaperMailError(
                            "This Mailer ID has used all its serial numbers. Ask USPS for help."
                        )
                    rows = []
                    for n, letter in enumerate(letters):
                        routing = imb.routing_from_address(letter["address"].splitlines())
                        rows.append(
                            MailPiece(
                                mailing=mailing,
                                group=letter["index"],
                                lot_id=letter["lot"].pk,
                                mailer_id=mid,
                                serial=first + n,
                                tracking=imb.tracking_code(
                                    conf.barcode_id, conf.service_type, mid, first + n
                                ),
                                routing=routing,
                            )
                        )
                    MailPiece.objects.bulk_create(rows)
                    counter.next_serial = first + len(letters)
                    counter.save(update_fields=["next_serial"])
                    existing = rows
    return {p.group: imb.encode(p.tracking, p.routing) for p in existing}


# --- Layout ---------------------------------------------------------------------------

W, H = pdf.PAGE_W, pdf.PAGE_H
IN = pdf.INCH
ADDRESS_SIZE, ADDRESS_LEAD = 10.5, 12.5
BODY_SIZE, BODY_LEAD = 11, 15
MARGIN = IN
WINDOW_W = 3.9 * IN  # the text width inside a 4.5 inch window, with room


def _letters(mailing: Mailing) -> list[dict]:
    from community.services import paper_mail_letters

    letters = paper_mail_letters(mailing.groups)
    for i, letter in enumerate(letters):
        letter.setdefault("index", i)
    return letters


def _address_lines(letter: dict, width: float, size: float, max_lines: int) -> list[str]:
    names = pdf.wrap_text(letter["owner_names"], size, width)[:2]
    address = [
        pdf.fit_text(line, size, width) for line in letter["address"].splitlines() if line.strip()
    ]
    return (names + address)[:max_lines]


def _return_lines(profile) -> list[str]:
    """The association's return address: Letters and certificates
    letterhead lines without email or web lines, else name and remit-to."""
    lines = [
        line.strip()
        for line in (profile.letterhead or "").splitlines()
        if line.strip() and not re.search(r"@|https?:|www\.", line, re.I)
    ]
    if len(lines) < 2:
        remit = [line.strip() for line in (profile.remit_to or "").splitlines() if line.strip()]
        lines = [profile.legal_name, *remit]
    return lines[:5]


def _address_block(x: float, top: float, lines: list[str], bars: str | None) -> list:
    """Recipient lines from ``top`` down, with the barcode above the first
    line when there is one (USPS: inside the address block, with clear
    space above and below)."""
    items: list = []
    y = top
    if bars:
        clear = 0.03 * IN
        bottom = y - clear - pdf.IMB_FULL
        items.append(pdf.Bars(x, bottom, bars))
        y = bottom - clear
    y -= ADDRESS_SIZE - 1
    for line in lines:
        items.append(pdf.Text(x, y, line, size=ADDRESS_SIZE))
        y -= ADDRESS_LEAD
    return items


def _f(value) -> float:
    return float(value)


def _letter_pages(mailing: Mailing, letter: dict, conf, profile, bars: str | None) -> list:
    values = letter_values(letter, mailing, profile)
    subject = fill(mailing.subject, values)
    body = fill(mailing.body, values)
    first: list = []
    if conf.window == WindowStyle.DOUBLE:
        y = H - _f(conf.return_top_in) * IN - 9
        for i, line in enumerate(_return_lines(profile)):
            first.append(pdf.Text(_f(conf.return_left_in) * IN, y, line, size=9.5, bold=i == 0))
            y -= 11.5
    else:
        head = [line for line in (profile.letterhead or "").splitlines() if line.strip()][:4]
        y = H - 0.6 * IN - 12
        for i, line in enumerate(head or [profile.legal_name]):
            first.append(
                pdf.Text(W / 2, y, line, size=13 if i == 0 else 9.5, bold=i == 0, align="center")
            )
            y -= 15 if i == 0 else 11.5
    first += _address_block(
        _f(conf.address_left_in) * IN,
        H - _f(conf.address_top_in) * IN,
        _address_lines(letter, WINDOW_W, ADDRESS_SIZE, 6),
        bars,
    )
    # Fold marks for a letter folded in thirds.
    for third in (H * 2 / 3, H / 3):
        first.append(pdf.Line(0.15 * IN, third, 0.4 * IN, third))

    lines: list[tuple[str, bool]] = [(_long(mailing.letter_date), False), ("", False)]
    if mailing.kind == MailingKind.TEMPLATE and _certified(mailing.template_kind):
        lines += [("CERTIFIED AND REGULAR MAIL", True), ("", False)]
    lines += [
        (line, True) for line in pdf.wrap_text(f"RE: {subject}", BODY_SIZE, W - 2 * MARGIN, True)
    ]
    lines += [("", False), (f"Dear {values['salutation']}:", False), ("", False)]
    for para in re.split(r"\n\s*\n", body):
        for raw in para.splitlines():
            lines += [(w, False) for w in pdf.wrap_text(raw, BODY_SIZE, W - 2 * MARGIN)]
        lines.append(("", False))
    signature = list((profile.letter_signature or "").splitlines()) or [
        profile.legal_name,
        "Board of Directors",
    ]
    lines += [("Sincerely,", False), ("", False)] + [(s, False) for s in signature]

    pages = [first]
    y = min(H - 3.5 * IN, _lowest(first) - 0.35 * IN)
    for text, bold in lines:
        if y < MARGIN:
            pages.append(
                [
                    pdf.Text(
                        MARGIN,
                        H - MARGIN + 18,
                        f"{letter['owner_names']} - {_long(mailing.letter_date)}"
                        f" - page {len(pages) + 1}",
                        size=8.5,
                    )
                ]
            )
            y = H - MARGIN - BODY_SIZE
        if text:
            pages[-1].append(pdf.Text(MARGIN, y, text, size=BODY_SIZE, bold=bold))
        y -= BODY_LEAD
    return pages


def _lowest(items: list) -> float:
    ys = [i.y for i in items if isinstance(i, pdf.Text)] + [
        i.y for i in items if isinstance(i, pdf.Bars)
    ]
    return min(ys) if ys else H


def _certified(kind: str) -> bool:
    from ledger.services import paper_letter_certified

    return paper_letter_certified(kind)


def rendered_text(mailing: Mailing, letter: dict) -> dict:
    """The first letter as plain text parts, for the preview on the page."""
    profile = _profile()
    values = letter_values(letter, mailing, profile)
    return {
        "recipient": [letter["owner_names"], *letter["address"].splitlines()],
        "date": _long(mailing.letter_date),
        "subject": fill(mailing.subject, values),
        "salutation": values["salutation"],
        "body": fill(mailing.body, values),
        "signature": (profile.letter_signature or "").splitlines()
        or [profile.legal_name, "Board of Directors"],
    }


def letters_pdf(mailing: Mailing, *, duplex: bool, first_only: bool = False) -> tuple[bytes, int]:
    """(PDF, pages): one letter per recipient group, each starting on a new
    sheet; with ``duplex``, a blank back page after any letter with an odd
    number of pages, so every letter starts on a front side."""
    conf, profile = settings(), _profile()
    letters = _letters(mailing)
    if first_only:
        letters = letters[:1]
    bars = {} if first_only else _pieces(mailing, letters)
    pages: list = []
    for letter in letters:
        own = _letter_pages(mailing, letter, conf, profile, bars.get(letter["index"]))
        pages += own
        if duplex and len(own) % 2:
            pages.append([])
    return pdf.render_placed(pages, title=f"Paper mail {mailing.pk}"), max(len(pages), 1)


ENV_W, ENV_H = 9.5 * IN, 4.125 * IN


def _envelope(letter: dict | None, conf, profile, bars: str | None, sample=False) -> list:
    dx, dy = _f(conf.envelope_shift_x_in) * IN, _f(conf.envelope_shift_y_in) * IN
    items: list = []
    y = ENV_H - 0.375 * IN - 9 + dy
    for i, line in enumerate(_return_lines(profile)):
        items.append(
            pdf.Text(
                0.375 * IN + dx, y, pdf.fit_text(line, 9, 3.5 * IN, i == 0), size=9, bold=i == 0
            )
        )
        y -= 11
    lines = (
        _address_lines(letter, 4.5 * IN, 11, 6)
        if letter
        else ["SAMPLE RECIPIENT", "123 EXAMPLE LANE", "ANYTOWN PA 00000"]
    )
    width = max([pdf.text_width(line, 11) for line in lines] + [pdf.IMB_LENGTH if bars else 0])
    height = len(lines) * 13 + ((pdf.IMB_FULL + 0.06 * IN) if bars else 0)
    x = max(3.25 * IN, (ENV_W - width) / 2) + dx
    top = min(2.6 * IN, 1.55 * IN + height / 2) + dy
    items += _address_block(x, top, lines, bars)
    if sample:
        items.append(pdf.Rect(x - 4, top - height - 6, width + 8, height + 8))
    return items


def envelopes_pdf(mailing: Mailing) -> tuple[bytes, int]:
    """#10 envelopes (9.5 x 4.125 in, landscape), one per page."""
    conf, profile = settings(), _profile()
    letters = _letters(mailing)
    bars = _pieces(mailing, letters)
    pages = [_envelope(letter, conf, profile, bars.get(letter["index"])) for letter in letters]
    return (
        pdf.render_placed(pages, width=ENV_W, height=ENV_H, title=f"Envelopes {mailing.pk}"),
        max(len(pages), 1),
    )


# Avery 5160 and compatible: 3 x 10 labels of 1 x 2-5/8 in on US Letter.
LABEL_W, LABEL_H = 2.625 * IN, 1.0 * IN
LABEL_LEFT, LABEL_TOP, LABEL_PITCH_X = 0.1875 * IN, 0.5 * IN, 2.75 * IN
LABEL_SIZE, LABEL_LEAD = 8.5, 10


def _label(col: int, row: int, lines: list[str], conf) -> list:
    x0 = LABEL_LEFT + col * LABEL_PITCH_X + _f(conf.label_shift_x_in) * IN
    top = H - LABEL_TOP - row * LABEL_H - _f(conf.label_shift_y_in) * IN
    lines = [pdf.fit_text(line, LABEL_SIZE, LABEL_W - 0.3 * IN) for line in lines][:5]
    y = top - (LABEL_H - len(lines) * LABEL_LEAD) / 2 - LABEL_SIZE + 1
    out = []
    for line in lines:
        out.append(pdf.Text(x0 + 0.15 * IN, y, line, size=LABEL_SIZE))
        y -= LABEL_LEAD
    return out


def labels_pdf(mailing: Mailing, *, start: int = 1) -> tuple[bytes, int]:
    """Address labels, 30 to a sheet, starting at label ``start`` (1-30,
    across then down) to reuse a partly used sheet."""
    if not 1 <= int(start) <= LABELS_PER_SHEET:
        raise PaperMailError("Start at a label from 1 to 30.")
    conf = settings()
    pages: list = [[]]
    slot = int(start) - 1
    for letter in _letters(mailing):
        if slot == LABELS_PER_SHEET:
            pages.append([])
            slot = 0
        lines = [letter["owner_names"], *[s for s in letter["address"].splitlines() if s.strip()]]
        pages[-1] += _label(slot % 3, slot // 3, lines, conf)
        slot += 1
    return pdf.render_placed(pages, title=f"Labels {mailing.pk}"), len(pages)


def test_pdf(kind: str) -> bytes:
    """An alignment test with sample text only: a letter page with the
    windows outlined, an envelope, or a full label sheet."""
    conf, profile = settings(), _profile()
    sample = {
        "lot": None,
        "names": ["SAMPLE RECIPIENT"],
        "owner_names": "SAMPLE RECIPIENT",
        "address": "123 EXAMPLE LANE\nANYTOWN, PA 00000",
    }
    bars = imb.encode("00310000000000000000", "00000") if conf.barcode_on else None
    if kind == "envelope":
        return pdf.render_placed(
            [_envelope(sample, conf, profile, bars, sample=True)],
            width=ENV_W,
            height=ENV_H,
            title="Envelope alignment test",
        )
    if kind == "labels":
        page = []
        for slot in range(LABELS_PER_SHEET):
            page += _label(slot % 3, slot // 3, [f"LABEL {slot + 1}", "123 EXAMPLE LANE"], conf)
        return pdf.render_placed([page], title="Label alignment test")
    if kind != "letter":
        raise PaperMailError("Unknown test page.")
    left, top = _f(conf.address_left_in) * IN, H - _f(conf.address_top_in) * IN
    items = _address_block(left, top, _address_lines(sample, WINDOW_W, ADDRESS_SIZE, 6), bars)
    # A #10 window is about 4.5 x 1.125 in; the outline shows where the
    # recipient block must fall.
    items.append(pdf.Rect(left - 0.12 * IN, top - 1.125 * IN, 4.5 * IN, 1.125 * IN))
    if conf.window == WindowStyle.DOUBLE:
        rl, rt = _f(conf.return_left_in) * IN, H - _f(conf.return_top_in) * IN
        items.append(pdf.Rect(rl - 0.12 * IN, rt - 0.875 * IN, 3.5 * IN, 0.875 * IN))
        y = rt - 9
        for i, line in enumerate(_return_lines(profile)):
            items.append(pdf.Text(rl, y, line, size=9.5, bold=i == 0))
            y -= 11.5
    for third in (H * 2 / 3, H / 3):
        items.append(pdf.Line(0, third, W, third, dashed=True))
    items.append(
        pdf.Text(
            W / 2,
            3.0 * IN,
            "Alignment test: print at actual size, fold in thirds on the dashed lines,",
            size=10,
            align="center",
        )
    )
    items.append(
        pdf.Text(
            W / 2,
            3.0 * IN - 13,
            "put it in an envelope and check the address shows fully in the window.",
            size=10,
            align="center",
        )
    )
    return pdf.render_placed([items], title="Letter alignment test")


def audit_printed(mailing: Mailing, *, actor, request, fmt: str, pages: int) -> None:
    require(actor, "mail.print")
    with system_access("mail pieces"):
        barcoded = MailPiece.objects.filter(mailing=mailing).exists()
    record_event(
        "mailing.printed",
        actor=actor,
        request=request,
        target=mailing,
        metadata={
            "format": fmt,
            "letters": mailing.letters_count,
            "pages": pages,
            "barcode": barcoded,
        },
    )


def detail_url(mailing: Mailing) -> str:
    return reverse("paper_mail_detail", args=[mailing.pk])
