"""Year-end tax documents (D71-tax): the Form 1120-H worksheet, the 1099
totals with an IRS IRIS upload file and recipient copies, and the filing
deadlines on Recurring deadlines.

Nothing is filed with any agency, and the IRS form itself is not filled in:
these are worksheets the tax preparer or a board member copies from. The
portal never stores taxpayer identification numbers (the association's EIN
or a vendor's TIN): every TIN column is left empty for the person completing
the filing in IRIS. Every figure comes from the books (``statements``); the
rate, the threshold and what does not count towards the 90% test are rules
with citations. Have the tax preparer confirm the form and every figure.
"""

from __future__ import annotations

import csv
import datetime
import io

from django.utils import timezone

from audit.services import record_event
from community.rules import rule, rule_with_citation
from core import pdf
from core.lists import safe_cell
from core.money import format_cents
from core.policy import SYSTEM, require
from core.scoping import internal
from ledger.books import periods, statements

CAVEAT = (
    "Worksheet only: confirm the form (1120-H or 1120) and every figure with your tax "
    "preparer. The portal files nothing."
)


def _user(actor):
    return None if actor is SYSTEM else actor


def _c(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}" if cents >= 0 else f"-{_c(-cents)}"


def _prefixes(text: str) -> list[str]:
    return [p for p in (text or "").replace(",", " ").split() if p]


def _matches(account: str, prefixes: list[str]) -> bool:
    return any(account.startswith(p) for p in prefixes)


# --- Form 1120-H ---------------------------------------------------------------------------------


@internal("books: form_1120h")
def form_1120h(fiscal_year: int) -> dict:
    """Form 1120-H line by line from the books, with the two qualification
    tests (IRC § 528(c)): at least 60% of gross income is exempt-function
    income, and at least 90% of expenditures are for acquiring, building,
    managing, maintaining and caring for association property (amounts set
    aside in reserves for that count, Treas. Reg. § 1.528-6)."""
    t = statements.tax_worksheet(fiscal_year)
    start, end = periods.year_bounds(fiscal_year)
    stmt = statements.income_statement(start, end)
    from ledger.books.models import GLCategory

    interest_accounts = set(
        GLCategory.objects.filter(name__icontains="interest", kind="income").values_list(
            "account", flat=True
        )
    )
    interest = sum(c for a, c in t["other_income_rows"] if a in interest_accounts)
    other = t["other_income_cents"] - interest
    gross = interest + other  # lines 1-7: income other than exempt-function income
    deductions = 0  # line 15: expenses directly connected with that income
    before = gross - deductions
    specific = min(10000, max(before, 0))  # line 18: $100 specific deduction
    taxable = max(before - specific, 0)
    rate_bp, rate_cite = rule_with_citation("tax.hoa_rate_bp")
    rate_bp = int(rate_bp if rate_bp is not None else 3000)
    tax = (taxable * rate_bp + 5000) // 10000  # rounded to the cent, half up
    # The 90% test.
    excluded = _prefixes(rule("tax.nonqualifying_accounts") or "")
    expense_rows = stmt["expense_rows"]
    reserves = max(0, -statements.income_statement(start, end, "operating")["transfers_total"])
    total_spent = sum(r["total"] for r in expense_rows) + reserves
    not_property = sum(r["total"] for r in expense_rows if _matches(r["account"], excluded))
    qualifying = total_spent - not_property
    receipts = t["exempt_function_cents"] + gross
    sixty = receipts == 0 or t["exempt_function_cents"] * 100 >= 60 * receipts
    ninety = total_spent == 0 or qualifying * 100 >= 90 * total_spent
    lines = [
        ("2", "Taxable interest", interest),
        ("7", "Other income (non-exempt)", other),
        ("8", "Gross income (lines 1 through 7)", gross),
        ("15", "Other deductions directly connected with that income", deductions),
        ("16", "Total deductions", deductions),
        ("17", "Taxable income before the specific deduction", before),
        ("18", "Specific deduction (at most $100)", specific),
        ("19", "Taxable income", taxable),
        ("20", f"Tax ({rate_bp / 100:g}% of line 19)", tax),
    ]
    return {
        "fiscal_year": fiscal_year,
        "closed": periods.is_closed_period(periods.year_key(fiscal_year), "year"),
        "exempt_function_cents": t["exempt_function_cents"],
        "lines": lines,
        "gross_income_cents": gross,
        "taxable_cents": taxable,
        "tax_cents": tax,
        "rate_bp": rate_bp,
        "rate_citation": rate_cite,
        "receipts_cents": receipts,
        "sixty": {
            "passes": sixty,
            "percent": round(t["exempt_function_cents"] * 100 / receipts) if receipts else None,
        },
        "ninety": {
            "passes": ninety,
            "percent": round(qualifying * 100 / total_spent) if total_spent else None,
            "qualifying_cents": qualifying,
            "total_cents": total_spent,
            "reserves_cents": reserves,
            "excluded_cents": not_property,
        },
        "qualifies": sixty and ninety,
        "caveat": CAVEAT,
        "due": filing_due(fiscal_year),
    }


def filing_due(fiscal_year: int) -> datetime.date:
    """The 15th day of the 4th month after the fiscal year ends (IRC § 6072(a))."""
    _start, end = periods.year_bounds(fiscal_year)
    day = end.replace(day=1)
    for _ in range(4):
        day = periods.next_month(day)
    return day.replace(day=15)


def worksheet_rows(w: dict) -> list[list[str]]:
    rows = [
        [
            "Form 1120-H worksheet",
            f"Fiscal year {w['fiscal_year']}",
            "closed" if w["closed"] else "not closed",
        ],
        [w["caveat"]],
        [],
        ["Exempt-function income (not reported as gross income)", _c(w["exempt_function_cents"])],
        ["Line", "Description", "Amount"],
    ]
    rows += [[n, label, _c(cents)] for n, label, cents in w["lines"]]
    s, n = w["sixty"], w["ninety"]
    rows += [
        [],
        [
            "60% test: exempt-function income share of gross receipts",
            f"{s['percent']}%" if s["percent"] is not None else "",
            "pass" if s["passes"] else "FAIL",
        ],
        [
            "90% test: spending on association property (reserves included)",
            f"{n['percent']}%" if n["percent"] is not None else "",
            "pass" if n["passes"] else "FAIL",
        ],
        ["Spending counted", _c(n["total_cents"]), ""],
        ["Of which not for association property", _c(n["excluded_cents"]), ""],
        ["Rate", f"{w['rate_bp'] / 100:g}%", w["rate_citation"]],
        ["Due", w["due"].isoformat(), "IRC § 6072(a); Form 1120-H instructions"],
    ]
    if not w["qualifies"]:
        rows.append(["A test fails: Form 1120 may be needed instead of Form 1120-H."])
    return rows


def worksheet_paragraphs(w: dict) -> list[tuple[str, str]]:
    from community.models import CommunityProfile

    def row(label, value=""):
        return pdf.plain(label)[:52].ljust(52) + value.rjust(16)

    out = [
        ("small", pdf.plain(CommunityProfile.get().legal_name)),
        ("heading", f"Form 1120-H worksheet, fiscal year {w['fiscal_year']}"),
        ("normal", "Closed books" if w["closed"] else "NOT CLOSED: figures may still change."),
        ("small", w["caveat"]),
        ("normal", ""),
        ("row", row("Exempt-function income", format_cents(w["exempt_function_cents"]))),
        ("normal", ""),
    ]
    out += [("row", row(f"Line {n}: {label}", format_cents(c))) for n, label, c in w["lines"]]
    s, n = w["sixty"], w["ninety"]
    out += [
        ("normal", ""),
        ("subheading", "Qualification tests (IRC § 528(c))"),
        (
            "row",
            row(
                "60% exempt-function income",
                f"{s['percent']}% " + ("pass" if s["passes"] else "FAIL")
                if s["percent"] is not None
                else "pass",
            ),
        ),
        (
            "row",
            row(
                "90% spending on association property",
                f"{n['percent']}% " + ("pass" if n["passes"] else "FAIL")
                if n["percent"] is not None
                else "pass",
            ),
        ),
        (
            "small",
            f"Due {w['due']:%B %-d, %Y} (IRC § 6072(a)). Rate: {w['rate_bp'] / 100:g}%"
            + (f" ({w['rate_citation']})" if w["rate_citation"] else "")
            + ".",
        ),
    ]
    if not w["qualifies"]:
        out.append(("normal", "A test fails: Form 1120 may be needed instead of Form 1120-H."))
    return out


# --- 1099s ---------------------------------------------------------------------------------------


def w9_on_file(vendor) -> bool:
    """Is the vendor's W-9 on file? The one place the 1099 checks ask.
    Interim: the vendor's "W-9 on file" tick. To be switched to vendor
    onboarding's ``operations.vendor_onboarding.w9_on_file`` (D79)."""
    return bool(vendor.w9_on_file)


def needs_1099(vendor) -> bool:
    """Is the vendor one that gets a 1099 at all? Interim: every vendor (the
    treasurer decides; corporations are generally exempt). To be switched to
    vendor onboarding's entity type (D79)."""
    return True


@internal("books: vendor_1099s")
def vendor_1099s(year: int) -> list[dict]:
    """Vendors paid at or over the threshold in the calendar year, split into
    1099-NEC (services) and 1099-MISC (rents and other, by the expense
    category codes in ``tax.1099_misc_categories``)."""
    from django.db.models import Sum

    from ledger.accounting import vendor_1099_totals
    from operations.models import InvoicePayment

    misc_codes = set(_prefixes(rule("tax.1099_misc_categories") or ""))
    out = []
    for row in vendor_1099_totals(year):
        if not row["over"] or not needs_1099(row["vendor"]):
            continue
        misc = (
            InvoicePayment.objects.filter(
                paid_on__year=year,
                invoice__vendor=row["vendor"],
                invoice__category__code__in=list(misc_codes),
            ).aggregate(s=Sum("amount_cents"))["s"]
            or 0
        )
        out.append(
            {
                "vendor": row["vendor"],
                "total_cents": row["total_cents"],
                "nec_cents": row["total_cents"] - misc,
                "misc_cents": misc,
                "w9": w9_on_file(row["vendor"]),
            }
        )
    return out


# IRS IRIS (Information Returns Intake System) CSV bulk upload: one row per
# recipient. The TIN columns are left empty: the portal stores no TINs.
IRIS_HEADER = [
    "Form Type",
    "Tax Year",
    "Payer Name",
    "Payer TIN",
    "Recipient TIN Type",
    "Recipient TIN",
    "Recipient Business Name",
    "Recipient Address",
    "Account Number",
    "Box 1 Amount",
    "Box 4 Federal Income Tax Withheld",
]


def _address(vendor) -> str:
    return " ".join((vendor.address or "").split())[:120]


@internal("books: iris_rows")
def iris_rows(year: int) -> list[list[str]]:
    from community.models import CommunityProfile

    payer = CommunityProfile.get().legal_name
    out = []
    for r in vendor_1099s(year):
        v = r["vendor"]
        for form, cents in (("1099-NEC", r["nec_cents"]), ("1099-MISC", r["misc_cents"])):
            if cents > 0:
                out.append(
                    [
                        form,
                        str(year),
                        payer,
                        "",
                        "",
                        "",
                        v.name,
                        _address(v),
                        f"V{v.pk}",
                        _c(cents),
                        "",
                    ]
                )
    return out


def iris_csv(year: int, *, actor, request=None) -> bytes:
    require(actor, "tax.view")
    rows = iris_rows(year)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(IRIS_HEADER)
    for row in rows:
        w.writerow([c if i == 9 else safe_cell(c) for i, c in enumerate(row)])
    _audit("iris_csv", year, len(rows), actor, request)
    return buf.getvalue().encode()


@internal("books: recipient_paragraphs")
def recipient_paragraphs(year: int) -> list[tuple[str, str]]:
    from community.models import CommunityProfile

    profile = CommunityProfile.get()
    out: list[tuple[str, str]] = []
    for r in vendor_1099s(year):
        v = r["vendor"]
        for form, box, cents in (
            ("1099-NEC", "Box 1 Nonemployee compensation", r["nec_cents"]),
            ("1099-MISC", "Box 3 Other income (or the box your preparer names)", r["misc_cents"]),
        ):
            if cents <= 0:
                continue
            out += [
                ("heading", f"Form {form} information, {year}: copy for the recipient"),
                (
                    "small",
                    "Not the official IRS form. Taxpayer identification numbers are not "
                    "kept by the portal and are completed by the preparer.",
                ),
                ("normal", ""),
                ("normal", pdf.plain(f"Payer: {profile.legal_name}")),
                ("normal", "Payer's TIN: (completed by the preparer)"),
                ("normal", pdf.plain(f"Recipient: {v.name}")),
                ("normal", pdf.plain(f"Recipient's address: {_address(v) or '(none on file)'}")),
                ("normal", "Recipient's TIN: (from the W-9; not kept by the portal)"),
                ("normal", f"Account number: V{v.pk}"),
                ("big", f"{box}: {format_cents(cents)}"),
                ("normal", ""),
                ("rule", ""),
            ]
    if not out:
        out = [
            ("heading", f"Form 1099 copies, {year}"),
            ("normal", "No vendor at or over the threshold."),
        ]
    return out


def recipient_pdf(year: int, *, actor, request=None) -> bytes:
    require(actor, "tax.view")
    data = pdf.render(recipient_paragraphs(year), title=f"1099 recipient copies {year}")
    _audit("1099_pdf", year, len(vendor_1099s(year)), actor, request)
    return data


# --- Filing deadlines ----------------------------------------------------------------------------

DEADLINES = [
    # title, category, (month, day) or None for the 1120-H due date, citation, notes
    (
        "Federal Form 1120-H (or 1120) for the association",
        "tax",
        None,
        "IRC § 528, § 6072(a); Form 1120-H instructions",
        "Due the 15th day of the 4th month after the fiscal year ends. Figures: Finance > Tax "
        "worksheet. Confirm with your tax preparer.",
    ),
    (
        "Forms 1099-NEC and 1099-MISC to vendors and the IRS (IRIS)",
        "tax",
        (1, 31),
        "IRC § 6041, § 6071(c); IRS Instructions for Forms 1099-MISC and 1099-NEC",
        "Recipient copies and the IRIS upload from Finance > Tax worksheet; add the TINs from "
        "the W-9s in IRIS. Confirm with your tax preparer.",
    ),
    (
        "PA Department of State annual report (nonprofit corporation)",
        "corporate",
        (6, 30),
        "15 Pa.C.S. § 146 (annual report)",
        "Filed online with the PA Department of State. Confirm the due date and any other "
        "state filing with your preparer.",
    ),
]


def ensure_deadlines(*, actor, fiscal_year: int | None = None, request=None) -> int:
    """Put the filings on Recurring deadlines (once; idempotent by title).
    An existing Pennsylvania annual report item is kept."""
    from operations.models import ComplianceItem
    from operations.services import save_compliance_item

    require(actor, "compliance.manage")
    today = timezone.localdate()
    fy = fiscal_year if fiscal_year is not None else periods.fiscal_year_of(today) - 1
    added = 0
    for title, category, when, citation, notes in DEADLINES:
        if ComplianceItem.objects.filter(title=title).exists():
            continue
        if (
            category == "corporate"
            and ComplianceItem.objects.filter(
                category="corporate", title__icontains="annual report"
            ).exists()
        ):
            continue
        if when is None:
            due = filing_due(fy)
        else:
            due = datetime.date(today.year, *when)
            if due < today:
                due = datetime.date(today.year + 1, *when)
        fields = {
            "title": title,
            "category": category,
            "due_on": due,
            "recurrence": "annual",
            "remind_days": 45,
            "notes": notes,
            "citation": citation,
            "active": True,
        }
        if actor is SYSTEM:  # the year close: audited below as one event
            ComplianceItem.objects.create(**fields, anchor_day=due.day)
        else:
            save_compliance_item(actor=actor, request=request, **fields)
        added += 1
    if added:
        record_event(
            "tax.deadlines_added", actor=_user(actor), request=request, metadata={"count": added}
        )
    return added


@internal("books: deadline_rows")
def deadline_rows() -> list[list[str]]:
    from operations.models import ComplianceItem

    return [
        [i.title, i.due_on.isoformat(), i.get_recurrence_display(), i.citation]
        for i in ComplianceItem.objects.filter(active=True, category__in=["tax", "corporate"])
    ]


def _audit(fmt: str, year: int, count: int, actor, request) -> None:
    record_event(
        "tax.exported",
        actor=_user(actor),
        request=request,
        metadata={"year": year, "format": fmt, "count": count},
    )


def worksheet_csv(fiscal_year: int, *, actor, request=None) -> bytes:
    require(actor, "tax.view")
    buf = io.StringIO()
    w = csv.writer(buf)
    for row in worksheet_rows(form_1120h(fiscal_year)):
        w.writerow([safe_cell(c) for c in row])
    _audit("worksheet_csv", fiscal_year, 1, actor, request)
    return buf.getvalue().encode()


def worksheet_pdf(fiscal_year: int, *, actor, request=None) -> bytes:
    require(actor, "tax.view")
    data = pdf.render(
        worksheet_paragraphs(form_1120h(fiscal_year)), title=f"Form 1120-H worksheet {fiscal_year}"
    )
    _audit("worksheet_pdf", fiscal_year, 1, actor, request)
    return data


@internal("books: package_files")
def package_files(fiscal_year: int) -> dict[str, bytes]:
    """The tax/ folder of the year-end package (no audit of its own: the
    package build is audited)."""
    w = form_1120h(fiscal_year)
    _start, end = periods.year_bounds(fiscal_year)
    year = end.year  # 1099s are by calendar year: the one the fiscal year ends in

    def to_csv(header, rows):
        buf = io.StringIO()
        wr = csv.writer(buf)
        if header:
            wr.writerow(header)
        for r in rows:
            wr.writerow([safe_cell(str(c)) for c in r])
        return buf.getvalue().encode()

    return {
        "tax/form-1120-h-worksheet.csv": to_csv(None, worksheet_rows(w)),
        "tax/form-1120-h-worksheet.pdf": pdf.render(
            worksheet_paragraphs(w), title="Form 1120-H worksheet"
        ),
        f"tax/1099-iris-upload-{year}.csv": to_csv(IRIS_HEADER, iris_rows(year)),
        f"tax/1099-recipient-copies-{year}.pdf": pdf.render(
            recipient_paragraphs(year), title="1099 recipient copies"
        ),
        "tax/filing-deadlines.csv": to_csv(
            ["Filing", "Due", "Repeats", "Citation"], deadline_rows()
        ),
    }
