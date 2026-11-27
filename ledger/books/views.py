"""Pages of the books and close (D69-D71): Month and year close, Bank
accounts and funds, Bank categories and rules, Financial statements and the
year-end package. Board, manager and auditor read; each service checks its
own permission (the manager codes and prepares; the board decides)."""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from community.rules import REGISTRY, rule_with_citation
from core import policy
from core.money import parse_cents
from ledger.books import close as bclose
from ledger.books import coding, funds, package, periods, statements
from ledger.books.funds import BooksError
from ledger.books.models import (
    CategoryKind,
    CodingRule,
    ExceptionNote,
    Fund,
    GLCategory,
    GLEntry,
    LineSign,
    PeriodClose,
    PeriodKind,
    PeriodReopenRequest,
    YearEndPackage,
)

ERRORS = (BooksError, periods.PeriodError, package.PackageError, ValueError)


def _money(value, *, allow_blank=False):
    text = (value or "").strip()
    if not text and allow_blank:
        return None
    negative = text.startswith("-")
    cents = parse_cents(text.lstrip("-"))
    return -cents if negative else cents


def _period_choice(request) -> tuple[str, str]:
    """(kind, key) from ?period=YYYY-MM or FY2026; default the month the
    close is working on."""
    raw = (request.GET.get("period") or request.POST.get("period") or "").strip()
    if raw.startswith("FY") and raw[2:].isdigit():
        return PeriodKind.YEAR, raw
    if raw:
        periods.month_bounds(raw)  # validates
        return PeriodKind.MONTH, raw
    due = bclose.due_months()
    if due:
        return PeriodKind.MONTH, due[0]
    last = (timezone.localdate().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    return PeriodKind.MONTH, periods.month_key(last)


def _month_list() -> list[dict]:
    first = funds.first_month()
    if first is None:
        return []
    today = timezone.localdate()
    standing = periods.standing_closes()
    runs = {r.period: r for r in bclose.open_runs()}
    out = []
    for key in reversed(periods.months_between(first, today)):
        start, end = periods.month_bounds(key)
        close = standing.get(key)
        out.append(
            {
                "key": key,
                "label": f"{start:%B %Y}",
                "close": close,
                "run": runs.get(key),
                "current": end >= today,
            }
        )
    return out[:18]


def _year_list() -> list[dict]:
    first = funds.first_month()
    if first is None:
        return []
    today = timezone.localdate()
    standing = periods.standing_closes(PeriodKind.YEAR)
    out = []
    for fy in range(periods.fiscal_year_of(today), periods.fiscal_year_of(first) - 1, -1):
        key = periods.year_key(fy)
        out.append(
            {
                "key": key,
                "fy": fy,
                "close": standing.get(key),
                "packages": list(YearEndPackage.objects.filter(fiscal_year=fy)[:3]),
            }
        )
    return out


@login_required
def books_close(request):
    policy.require(request.user, "books.view")
    try:
        kind, key = _period_choice(request)
    except periods.PeriodError as exc:
        raise Http404 from exc
    if kind == PeriodKind.MONTH:
        ev = bclose.evaluate_month(key)
    else:
        ev = bclose.evaluate_year(int(key[2:]))
    close = ev.close
    reopen = periods.open_request(close) if close else None
    user = request.user
    return render(
        request,
        "ledger/books/close.html",
        {
            "ev": ev,
            "months": _month_list(),
            "years": _year_list(),
            "history": periods.history(key, kind),
            "reopen": reopen,
            "categories": funds.active_categories(),
            "pending": [n for n in bclose.pending_agreements() if n.period == key],
            "auto_close": rule_with_citation("books.auto_close"),
            "close_day": rule_with_citation("books.close_day"),
            "can": {
                "code": policy.can(user, "books.code"),
                "rules": policy.can(user, "books.rules"),
                "balance": policy.can(user, "books.balance"),
                "note": policy.can(user, "books.difference"),
                "agree": policy.can(user, "books.difference_agree"),
                "waive": policy.can(user, "books.waive_late_fee"),
                "late_fee": policy.can(user, "late_fee.post"),
                "close": policy.can(user, "books.close"),
                "reopen": policy.can(user, "books.reopen_request"),
                "decide": policy.can(user, "books.reopen_approve"),
                "package": policy.can(user, "books.package"),
            },
        },
    )


@login_required
@require_POST
def books_action(request):
    policy.require(request.user, "books.view")
    action = request.POST.get("action", "")
    period = (request.POST.get("period") or "").strip()[:8]
    back = reverse("books_close") + (f"?period={period}" if period else "")
    user = request.user
    try:
        if action == "code":
            from ledger.models import BankTransaction

            line = get_object_or_404(BankTransaction, pk=request.POST.get("line") or 0)
            category = get_object_or_404(GLCategory, pk=request.POST.get("category") or 0)
            coding.code_line(
                line, category, actor=user, memo=request.POST.get("memo", ""), request=request
            )
            if request.POST.get("make_rule") and policy.can(user, "books.rules"):
                text = coding.rule_text(line.description)
                if text:
                    coding.save_rule(
                        name=text.title(),
                        contains=text,
                        category=category,
                        sign=LineSign.IN if line.amount_cents > 0 else LineSign.OUT,
                        actor=user,
                        request=request,
                    )
                    messages.success(request, f'Coded, and a rule now codes "{text}" lines.')
                else:
                    messages.success(request, "Coded. The description has no words for a rule.")
            else:
                messages.success(request, "Coded.")
        elif action == "uncode":
            entry = get_object_or_404(GLEntry, pk=request.POST.get("entry") or 0)
            coding.uncode(entry, reason=request.POST.get("reason", ""), actor=user, request=request)
            messages.success(request, "Coding undone; the line is waiting again.")
        elif action == "balance":
            from ledger.models import BankConnection

            conn = get_object_or_404(BankConnection, pk=request.POST.get("connection") or 0)
            as_of = datetime.date.fromisoformat(request.POST.get("as_of", ""))
            bclose.enter_balance(
                conn,
                as_of=as_of,
                balance_cents=_money(request.POST.get("balance")),
                actor=user,
                request=request,
            )
            messages.success(request, "Statement balance recorded.")
        elif action == "note":
            raw = (request.POST.get("amount_cents") or "").strip()
            amount = int(raw) if raw else _money(request.POST.get("amount"), allow_blank=True) or 0
            bclose.record_note(
                period=period,
                check_key=request.POST.get("check", ""),
                item=request.POST.get("item", ""),
                reason=request.POST.get("reason", ""),
                amount_cents=amount,
                actor=user,
                request=request,
            )
            messages.success(
                request,
                "Noted. A second board member must agree before it counts."
                if amount or not policy.can(user, "books.difference_agree")
                else "Noted.",
            )
        elif action == "waive":
            bclose.record_note(
                period=period,
                check_key="late_fees",
                item=request.POST.get("item", ""),
                reason=request.POST.get("reason", ""),
                actor=user,
                request=request,
            )
            messages.success(request, "Late fee waived for that month.")
        elif action == "agree":
            note = get_object_or_404(ExceptionNote, pk=request.POST.get("note") or 0)
            bclose.agree(note, actor=user, request=request)
            messages.success(request, "Agreed.")
        elif action == "post_late_fees":
            from ledger.services import post_late_fees

            start, _end = periods.month_bounds(period)
            n = post_late_fees(year=start.year, month=start.month, actor=user, request=request)
            messages.success(request, f"{n} late fee(s) posted.")
        elif action == "close":
            if period.startswith("FY"):
                bclose.close_year(int(period[2:]), actor=user, request=request)
            else:
                bclose.close_month(period, actor=user, request=request)
            messages.success(request, "Closed.")
        elif action == "reopen_request":
            close = get_object_or_404(PeriodClose, pk=request.POST.get("close") or 0)
            periods.request_reopen(
                close, reason=request.POST.get("reason", ""), actor=user, request=request
            )
            messages.success(request, "Reopen requested; another board member decides.")
        elif action in ("reopen_approve", "reopen_decline"):
            req = get_object_or_404(PeriodReopenRequest, pk=request.POST.get("request_id") or 0)
            periods.decide_reopen(
                req,
                approve=action == "reopen_approve",
                note=request.POST.get("note", ""),
                actor=user,
                request=request,
            )
            messages.success(request, "Reopened." if action == "reopen_approve" else "Declined.")
        elif action == "run":
            policy.require(user, "books.close")
            result = bclose.run()
            messages.success(
                request,
                f"Checked: {result['coded']} line(s) coded by rules, {result['matched']} "
                f"matched, {len(result['closed'])} period(s) closed.",
            )
        else:
            raise Http404
    except PermissionDenied:
        raise
    except ERRORS as exc:
        messages.error(request, str(exc) or "Check the values.")
    except Exception as exc:  # the ledger's and late fees' own refusals
        from ledger.services import LedgerError

        if isinstance(exc, LedgerError):
            messages.error(request, str(exc))
        else:
            raise
    if action in ("code", "uncode") and request.POST.get("next") == "coding":
        return redirect("books_coding")
    return redirect(back)


@login_required
def books_accounts(request):
    from ledger.bankfeeds import services as bank_services
    from ledger.models import AccountingSetup, BankConnection

    policy.require(request.user, "books.view")
    can_setup = policy.can(request.user, "books.setup")
    if request.method == "POST":
        policy.require(request.user, "books.setup")
        try:
            if request.POST.get("action") == "add":
                policy.require(request.user, "bank_feed.import")
                conn = bank_services.add_file_account(
                    display_name=request.POST.get("display_name", ""),
                    institution_name=request.POST.get("institution_name", ""),
                    last_four_digits=request.POST.get("last_four", ""),
                    actor=request.user,
                    request=request,
                )
                funds.save_account(
                    conn,
                    fund=request.POST.get("fund", Fund.OPERATING),
                    is_primary=not funds.accounts(),
                    opening_balance_cents=_money(request.POST.get("opening"), allow_blank=True),
                    actor=request.user,
                    request=request,
                )
                messages.success(request, "Account added to the books.")
            else:
                conn = get_object_or_404(BankConnection, pk=request.POST.get("connection") or 0)
                funds.save_account(
                    conn,
                    fund=request.POST.get("fund", ""),
                    gl_account=request.POST.get("gl_account", ""),
                    is_primary=bool(request.POST.get("is_primary")),
                    opening_balance_cents=_money(request.POST.get("opening"), allow_blank=True),
                    actor=request.user,
                    request=request,
                )
                messages.success(request, "Saved.")
        except (BooksError, ValueError) as exc:
            messages.error(request, str(exc) or "Check the amount (dollars, e.g. 1250.00).")
        except Exception as exc:
            from ledger.bankfeeds.base import BankFeedError

            if isinstance(exc, BankFeedError):
                messages.error(request, str(exc))
            else:
                raise
        return redirect("books_accounts")
    setups = {s.connection_id: s for s in funds.accounts()}
    rows = [
        {"conn": c, "setup": setups.get(c.pk)}
        for c in BankConnection.objects.order_by("display_name", "id")
    ]
    return render(
        request,
        "ledger/books/accounts.html",
        {
            "rows": rows,
            "funds": Fund.choices,
            "can_setup": can_setup,
            "start": funds.books_start(),
            "locked": funds.books_start() is not None
            and periods.any_closed_after(funds.books_start()),
            "positions": bclose.fund_positions(timezone.localdate()) if funds.books_start() else {},
            "cash_default": AccountingSetup.get().accounts()["cash"],
        },
    )


@login_required
def books_coding(request):
    policy.require(request.user, "books.view")
    user = request.user
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "category":
                pk = request.POST.get("category")
                funds.save_category(
                    name=request.POST.get("name", ""),
                    account=request.POST.get("account", ""),
                    kind=request.POST.get("kind", ""),
                    fund=request.POST.get("fund", ""),
                    exempt_function=bool(request.POST.get("exempt_function")),
                    active=bool(request.POST.get("active")) if pk else True,
                    category=get_object_or_404(GLCategory, pk=pk) if pk else None,
                    actor=user,
                    request=request,
                )
                messages.success(request, "Category saved.")
            elif action == "rule":
                from ledger.models import BankConnection

                conn_id = request.POST.get("connection")
                coding.save_rule(
                    name=request.POST.get("name", ""),
                    contains=request.POST.get("contains", ""),
                    category=get_object_or_404(GLCategory, pk=request.POST.get("category") or 0),
                    sign=request.POST.get("sign", LineSign.ANY),
                    connection=get_object_or_404(BankConnection, pk=conn_id) if conn_id else None,
                    min_cents=_money(request.POST.get("min"), allow_blank=True),
                    max_cents=_money(request.POST.get("max"), allow_blank=True),
                    actor=user,
                    request=request,
                )
                messages.success(request, "Rule saved and applied to waiting lines.")
            elif action == "end_rule":
                coding.end_rule(
                    get_object_or_404(CodingRule, pk=request.POST.get("rule") or 0),
                    actor=user,
                    request=request,
                )
                messages.success(request, "Rule ended.")
            else:
                raise Http404
        except (BooksError, ValueError) as exc:
            messages.error(request, str(exc) or "Check the values.")
        return redirect("books_coding")
    from ledger.models import BankConnection

    return render(
        request,
        "ledger/books/coding.html",
        {
            "categories": GLCategory.objects.all(),
            "kinds": CategoryKind.choices,
            "funds": Fund.choices,
            "signs": LineSign.choices,
            "rules": CodingRule.objects.select_related("category", "connection"),
            "waiting": coding.unhandled_lines()[:100],
            "suggestions": coding.rule_suggestions(),
            "recent": GLEntry.objects.select_related(
                "transaction__connection", "category", "rule"
            ).order_by("-id")[:30],
            "connections": BankConnection.objects.all(),
            "can_setup": policy.can(user, "books.setup"),
            "can_rules": policy.can(user, "books.rules"),
            "can_code": policy.can(user, "books.code"),
        },
    )


# --- Financial statements ------------------------------------------------------------------------


def _statement_period(request) -> dict:
    month = (request.GET.get("month") or "").strip()
    year = (request.GET.get("year") or "").strip()
    try:
        if month:
            periods.month_bounds(month)
            return statements.period_for(month=month)
        if year:
            return statements.period_for(year=int(year))
    except (periods.PeriodError, ValueError) as exc:
        raise Http404 from exc
    today = timezone.localdate()
    return statements.period_for(year=periods.fiscal_year_of(today))


@login_required
def financial_statements(request):
    policy.require(request.user, "statements.view")
    period = _statement_period(request)
    data = None if funds.books_start() is None else statements.statement_set(period)
    fund = request.GET.get("fund", "")
    today = timezone.localdate()
    first = funds.first_month()
    return render(
        request,
        "ledger/books/statements.html",
        {
            "s": data,
            "period": period,
            "fund": fund if fund in dict(Fund.choices) else "",
            "funds": Fund.choices,
            "query": request.GET.urlencode(),
            "years": list(
                range(periods.fiscal_year_of(today), periods.fiscal_year_of(first or today) - 1, -1)
            ),
            "months": list(reversed(periods.months_between(first, today)))[:24] if first else [],
            "packages": YearEndPackage.objects.filter(fiscal_year=period["fiscal_year"])[:10],
            "can_package": policy.can(request.user, "books.package"),
            "key_configured": package.key_is_configured(),
        },
    )


def _statement_download(request, fmt: str):
    policy.require(request.user, "statements.view")
    if funds.books_start() is None:
        raise Http404
    period = _statement_period(request)
    s = statements.statement_set(period)
    name = f"financial-statements-{period['key']}"
    if fmt == "csv":
        data = statements.statements_csv(s, actor=request.user, request=request)
        response = HttpResponse(data, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
        return response
    from documents.attachments import pdf_response

    return pdf_response(
        statements.statements_pdf(s, actor=request.user, request=request), f"{name}.pdf"
    )


@login_required
def financial_statements_csv(request):
    return _statement_download(request, "csv")


@login_required
def financial_statements_pdf(request):
    return _statement_download(request, "pdf")


@login_required
def year_end_package(request):
    """POST builds a package for ?year (board, auditor); GET ?id streams one."""
    policy.require(request.user, "books.package")
    if request.method == "POST":
        try:
            year = int(request.POST.get("year") or 0)
            pkg = package.build(year, actor=request.user, request=request)
            messages.success(request, f"Package built: {pkg.file_count} files.")
        except (package.PackageError, ValueError) as exc:
            messages.error(request, str(exc) or "Choose a fiscal year.")
            year = periods.fiscal_year_of(timezone.localdate())
        return redirect(reverse("financial_statements") + f"?year={year}")
    pkg = get_object_or_404(YearEndPackage, pk=request.GET.get("id") or 0)
    try:
        fh = package.open_for_download(pkg, actor=request.user, request=request)
    except package.PackageError as exc:
        messages.error(request, str(exc))
        return redirect(reverse("financial_statements") + f"?year={pkg.fiscal_year}")
    name = f"year-end-package-FY{pkg.fiscal_year}-{pkg.pk}.zip"
    return FileResponse(fh, as_attachment=True, filename=name, content_type="application/zip")


@login_required
def accounting_quickbooks(request):
    """Optional: the journal in QuickBooks Online's import layout."""
    from ledger import accounting as acc
    from ledger.books import quickbooks

    policy.require(request.user, "finance.view")
    try:
        start = datetime.date.fromisoformat(request.GET.get("start", ""))
        end = datetime.date.fromisoformat(request.GET.get("end", ""))
        data = quickbooks.export(start, end, actor=request.user, request=request)
    except (ValueError, acc.AccountingError):
        messages.error(request, "Choose a start and an end date.")
        return redirect("accounting")
    response = HttpResponse(data, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="quickbooks-journal-{start}-{end}.csv"'
    return response


def rule_rows(prefix: str = "books.") -> list[dict]:
    out = []
    for key, spec in REGISTRY.items():
        if key.startswith(prefix):
            value, citation = rule_with_citation(key)
            out.append({"spec": spec, "value": value, "citation": citation})
    return out


# --- Tax worksheet (D71-tax) ---------------------------------------------------------------------


def _tax_year(request) -> int:
    try:
        return int(request.GET.get("year") or request.POST.get("year") or 0) or (
            periods.fiscal_year_of(timezone.localdate()) - 1
        )
    except ValueError as exc:
        raise Http404 from exc


@login_required
def tax_worksheet(request):
    from ledger.books import tax
    from operations.services import OperationsError

    policy.require(request.user, "tax.view")
    year = _tax_year(request)
    if request.method == "POST":
        try:
            n = tax.ensure_deadlines(actor=request.user, fiscal_year=year, request=request)
            messages.success(request, f"{n} filing deadline(s) added to Recurring deadlines.")
        except OperationsError as exc:
            messages.error(request, str(exc))
        return redirect(reverse("tax_worksheet") + f"?year={year}")
    _start, end = periods.year_bounds(year)
    ready = funds.books_start() is not None
    return render(
        request,
        "ledger/books/tax.html",
        {
            "year": year,
            "calendar_year": end.year,
            "w": tax.form_1120h(year) if ready else None,
            "vendors": tax.vendor_1099s(end.year),
            "threshold": rule_with_citation("accounting.1099_threshold_cents"),
            "deadlines": tax.deadline_rows(),
            "years": list(range(periods.fiscal_year_of(timezone.localdate()), year - 5, -1)),
            "can_deadlines": policy.can(request.user, "compliance.manage"),
        },
    )


@login_required
def tax_download(request):
    from documents.attachments import pdf_response
    from ledger.books import tax

    policy.require(request.user, "tax.view")
    year = _tax_year(request)
    fmt = request.GET.get("format", "")
    if fmt == "worksheet_pdf":
        data = tax.worksheet_pdf(year, actor=request.user, request=request)
        return pdf_response(data, f"form-1120-h-worksheet-{year}.pdf")
    if fmt == "1099_pdf":
        data = tax.recipient_pdf(year, actor=request.user, request=request)
        return pdf_response(data, f"1099-recipient-copies-{year}.pdf")
    if fmt == "worksheet_csv":
        data = tax.worksheet_csv(year, actor=request.user, request=request)
        name = f"form-1120-h-worksheet-{year}.csv"
    elif fmt == "iris":
        data = tax.iris_csv(year, actor=request.user, request=request)
        name = f"1099-iris-upload-{year}.csv"
    else:
        raise Http404
    response = HttpResponse(data, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response
