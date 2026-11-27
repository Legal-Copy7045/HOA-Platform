import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.services import record_event
from community.account_views import my_account_people
from community.models import Lot
from community.rules import rule, rule_with_citation
from community.terms import term_text
from core import features, policy
from core.money import MoneyField, format_cents
from core.workflow import WorkflowError
from documents import attachments
from documents.attachments import pdf_response
from ledger import accounts as ledger_accounts
from ledger import allocation, services
from ledger.accounts import CURRENT
from ledger.models import (
    Budget,
    CollectionCase,
    CollectionLetter,
    EntryKind,
    IncomeSource,
    LedgerEntry,
    LineKind,
    ResaleRequest,
)
from operations.models import ExpenseCategory
from payments.checkout import payable_lot_ids
from payments.providers import config as payments_config
from payments.services import disclosure, online_payments_enabled, payment_options


def _date(**kw):
    return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kw)


class PaymentForm(forms.Form):
    amount_cents = MoneyField(label="Amount ($)")
    received_on = _date(initial=timezone.localdate)
    method = forms.ChoiceField(
        choices=[
            ("check", "Check"),
            ("ach", "ACH"),
            ("cash", "Cash"),
            ("money_order", "Money order"),
            ("other", "Other"),
        ]
    )
    reference = forms.CharField(
        max_length=40, required=False, help_text="Check number - never account numbers."
    )
    apply_to = forms.TypedMultipleChoiceField(
        coerce=int,
        required=False,
        label="Apply payment to\u2026",
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave all unticked to pay the oldest charges first (the application rule).",
    )

    def __init__(self, *args, open_charges=(), accounts=(), **kwargs):
        super().__init__(*args, **kwargs)
        numbers = {a.pk: a.number for a in accounts}
        self.fields["apply_to"].choices = [
            (
                c.entry.pk,
                f"{c.label}: {c.title} ({format_cents(c.open_cents)} open)"
                + (
                    f" \u00b7 account {numbers.get(c.entry.account_id, '(none)')}"
                    if len(numbers) > 1
                    else ""
                ),
            )
            for c in open_charges
        ]
        if not open_charges:
            del self.fields["apply_to"]
        _account_field(self, accounts)


def _account_field(form, accounts) -> None:
    """An "Account" choice when the lot has had more than one owner: a
    former owner's account stays open to the board for what is still owed
    either way (D33-acctfix-1). Blank: the account the date falls in."""
    if len(accounts) < 2:
        return
    form.fields["account_id"] = forms.TypedChoiceField(
        coerce=int,
        required=False,
        empty_value=None,
        label="Account",
        choices=[("", "The owners' account on that date")]
        + [
            (
                a.pk,
                f"{a.number} \u00b7 {'current owners' if a.is_active else 'former owner'}"
                + (f" ({_signed(a.balance_cents)})" if a.balance_cents else ""),
            )
            for a in accounts
        ],
        help_text="Payments and credits pay only charges of the same account.",
    )


def _signed(cents: int) -> str:
    return f"owes {format_cents(cents)}" if cents > 0 else f"credit {format_cents(-cents)}"


class ChargeForm(forms.Form):
    kind = forms.ChoiceField(
        choices=[
            (EntryKind.OTHER_CHARGE, "Other charge"),
            (EntryKind.LATE_FEE, "Late fee (disabled pending review)"),
        ]
    )
    amount_cents = MoneyField(label="Amount ($)")
    description = forms.CharField(max_length=200)

    def __init__(self, *args, accounts=(), **kwargs):
        super().__init__(*args, **kwargs)
        _account_field(self, accounts)


class CreditForm(forms.Form):
    amount_cents = MoneyField(label="Amount ($)")
    description = forms.CharField(max_length=200, label="Reason")

    def __init__(self, *args, accounts=(), **kwargs):
        super().__init__(*args, **kwargs)
        _account_field(self, accounts)


def _flash(request, fn, ok):
    try:
        result = fn()
    except (services.LedgerError, WorkflowError) as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, ok)
    return result


# --- Owner -----------------------------------------------------------------------------


def _ownership_start(user, lot_id):
    """When this owner's unbroken ownership began (also right for the
    homeowner view, which has the previewed lot's owner scope)."""
    return policy.roles_for(user).lot_since.get(lot_id)


def _online_receipts(user, lot_id, since):
    """Online payments on this account in the viewer's own time (receipts)."""
    from ledger.accounts import current_account_id
    from payments.models import StripePayment

    # The account it was paid on only: never the former owners' (SEC-001).
    rows = StripePayment.objects.for_user(user).filter(
        lot_id=lot_id, entry__account_id=current_account_id(lot_id)
    )
    if since:
        rows = rows.filter(recorded_at__date__gte=since)
    return list(rows[:12])


@login_required
def my_account(request):
    from community.models import CommunityProfile
    from payments.autopay import for_payer as autopay_for_payer

    policy.require(request.user, "ledger.view_own")
    online = online_payments_enabled(request)
    payable = payable_lot_ids(request.user) if online else frozenset()
    accounts = []
    roles = policy.roles_for(request.user)
    for lot in Lot.objects.for_user(request.user).filter(pk__in=roles.account_lot_ids):
        since = _ownership_start(request.user, lot.pk)
        statement = services.owner_statement(lot.pk, since)
        accounts.append(
            {
                "lot": lot,
                # Today's ownership account only (D33-acctfix-1).
                "open_charges": allocation.open_charges(lot.pk, since=since, account=CURRENT),
                "open_before": (
                    allocation.open_before(lot.pk, since, account=CURRENT) if since else 0
                ),
                "unapplied": allocation.unapplied_credit(lot.pk, account=CURRENT),
                "statement": statement,
                "rows": services.statement_rows(statement),
                "due": services.amount_due(lot.pk),
                "upcoming": services.upcoming_charges(statement.balance, lot_id=lot.pk),
                "pay_options": payment_options(statement.balance) if online else [],
                "can_pay_online": lot.pk in payable,
                # Resale certificates are asked for by an owner on the deed
                # (WT-001): contacts see a line saying so, not a link to a 403.
                "is_owner": lot.pk in roles.owned_lot_ids,
                "online_receipts": _online_receipts(request.user, lot.pk, since),
                # Checks the association wrote to this account's owners (D52).
                "checks_to_you": _checks_to_you(lot.pk),
                # Automatic payments (D46): payers only, feature on.
                "autopay": autopay_for_payer(request.user, lot) if online else None,
                **my_account_people(request.user, lot.pk),
            }
        )
    profile = CommunityProfile.get()
    return render(
        request,
        "ledger/my_account.html",
        {
            "accounts": accounts,
            "online": online,
            "can_request_resale": policy.can(request.user, "resale.request")
            and any(a["is_owner"] for a in accounts),
            "test_mode": online and not payments_config().livemode,
            "disclosure": disclosure() if online else "",
            "payable_to": profile.legal_name,
            "remit_lines": [line for line in profile.remit_to.splitlines() if line.strip()],
        },
    )


def _checks_to_you(lot_id: int) -> list[dict]:
    from ledger import disbursements

    return disbursements.checks_for_account(ledger_accounts.current_account_id(lot_id))


@login_required
def my_statement_pdf(request, lot_id):
    policy.require(request.user, "ledger.view_own")
    if lot_id not in policy.roles_for(request.user).account_lot_ids:
        raise Http404
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=lot_id)
    record_event(
        "ledger.statement_downloaded",
        request=request,
        actor=request.user,
        target=lot,
        metadata={"lot_id": lot.pk},
    )
    return pdf_response(
        services.statement_pdf(lot, since=_ownership_start(request.user, lot.pk)),
        f"statement-lot-{lot.number}.pdf",
    )


# --- Board ledger ----------------------------------------------------------------------------


def _finance(request):
    if not (policy.can(request.user, "finance.view") or policy.can(request.user, "ledger.manage")):
        policy.require(request.user, "finance.view")


def _month(value: str) -> tuple[int, int]:
    """'2026-12' from a month picker -> (2026, 12); (0, 0) when not valid."""
    try:
        year, month = (int(part) for part in (value or "").strip().split("-"))
    except ValueError:
        return 0, 0
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return 0, 0
    return year, month


@login_required
def ledger_home(request):
    _finance(request)
    if request.method == "POST" and request.POST.get("action") == "late_fees":
        today = timezone.localdate()
        _flash(
            request,
            lambda: services.post_late_fees(
                year=today.year, month=today.month, actor=request.user, request=request
            ),
            "Late fees posted to the homes on the late list.",
        )
        return redirect("ledger_home")
    if request.method == "POST":
        year, month = _month(request.POST.get("period", ""))
        if not year:
            messages.error(request, "Choose the month to charge.")
            return redirect("ledger_home")
        _flash(
            request,
            lambda: services.post_monthly_assessments(
                year=year, month=month, actor=request.user, request=request
            ),
            term_text("Monthly {term} posted (homes already charged for that month were skipped)."),
        )
        return redirect("ledger_home")
    bal = services.balances()
    lots = sorted(Lot.objects.for_user(request.user), key=lambda lot: lot.sort_key)
    today = timezone.localdate()
    next_month = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    dues = rule("dues.monthly_cents") or 0
    with_balance = request.GET.get("balance") == "1"
    # With automatic posting on, the card shows its status instead of the
    # picker, and the button only when this month's run looks missed (D48).
    auto_post = bool(rule("dues.auto_post"))
    auto = services.auto_post_status() if auto_post else None
    if auto is not None:
        from ledger import autopost

        missed = autopost.missed()
        auto["missed"] = missed is not None
        auto["reason_text"] = missed["reason_text"] if missed else ""
    # Months in words, not the browser's month grid (D48): the next month not
    # yet posted is chosen, starting from next month.
    month_options = services.assessment_months(today)
    default_period = f"{next_month:%Y-%m}"
    open_months = [m["value"] for m in month_options if not m["posted"]]
    if default_period not in open_months:
        later = [v for v in open_months if v > default_period]
        default_period = later[0] if later else (open_months[-1] if open_months else "")
    return render(
        request,
        "ledger/home.html",
        {
            # "With a balance" filter (WT-029): homes owing or in credit.
            "rows": [
                (lot, bal.get(lot.pk, 0)) for lot in lots if not with_balance or bal.get(lot.pk, 0)
            ],
            "with_balance": with_balance,
            "home_count": len(lots),
            "total_owed": sum(v for v in bal.values() if v > 0),
            "total_credit": -sum(v for v in bal.values() if v < 0),
            "default_period": default_period,
            "month_options": month_options,
            "auto_post": auto_post,
            "auto": auto,
            "can_change_settings": policy.can(request.user, "community.manage_rules"),
            "dues_cents": dues,
            "dues_lots": len(lots),
            "dues_total": dues * len(lots),
            "can_manage": policy.can(request.user, "ledger.manage"),
            "late": services.late_fee_candidates(year=today.year, month=today.month),
            "late_fee": rule_with_citation("late_fee.cents"),
            "late_auto": rule("late_fee.auto_apply"),
            "can_post_late_fees": policy.can(request.user, "late_fee.post"),
        },
    )


@login_required
def lot_ledger(request, pk):
    _finance(request)
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=pk)
    entries = list(
        LedgerEntry.objects.for_user(request.user).filter(lot=lot).select_related("reversed_by")
    )
    notes = services.moved_notes(entries)
    for e in entries:
        e.moved_note = notes.get(e.pk, "")
    open_charges = allocation.open_charges(lot.pk)
    lot_accounts = ledger_accounts.lot_accounts(lot.pk)
    return render(
        request,
        "ledger/lot.html",
        {
            "lot": lot,
            "entries": entries,
            "balance": services.balance(lot.pk),
            "aging": (aging := services.aging(lot.pk)),
            "aging_columns": services.aging_columns(aging),
            "can_manage": policy.can(request.user, "ledger.manage"),
            "can_reassign": policy.can(request.user, "ledger.reassign") and len(lot_accounts) > 1,
            "lot_accounts": lot_accounts,
            "open_charges": open_charges,
            # Former owners' accounts with money still owed either way.
            "former_accounts": [a for a in lot_accounts if not a.is_active and a.balance_cents],
            "payment_form": PaymentForm(open_charges=open_charges, accounts=lot_accounts),
            "charge_form": ChargeForm(accounts=lot_accounts),
            "credit_form": CreditForm(accounts=lot_accounts),
            "move_notices": services.open_move_notices(lot.pk, user=request.user),
            "open_case": CollectionCase.objects.for_user(request.user)
            .filter(lot=lot)
            .exclude(state="closed")
            .first(),
        },
    )


@login_required
@require_POST
def lot_ledger_action(request, pk, action):
    _finance(request)
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=pk)
    actor = request.user
    lot_accounts = ledger_accounts.lot_accounts(lot.pk)
    if action == "payment":
        form = PaymentForm(
            request.POST, open_charges=allocation.open_charges(lot.pk), accounts=lot_accounts
        )
        if form.is_valid():
            _flash(
                request,
                lambda: services.record_payment(
                    lot=lot, actor=actor, request=request, **form.cleaned_data
                ),
                "Payment recorded.",
            )
    elif action == "charge":
        form = ChargeForm(request.POST, accounts=lot_accounts)
        if form.is_valid():
            _flash(
                request,
                lambda: services.post_charge(
                    lot=lot, actor=actor, request=request, **form.cleaned_data
                ),
                "Charge posted.",
            )
    elif action == "credit":
        form = CreditForm(request.POST, accounts=lot_accounts)
        if form.is_valid():
            _flash(
                request,
                lambda: services.post_credit(
                    lot=lot, actor=actor, request=request, **form.cleaned_data
                ),
                "Credit posted.",
            )
    elif action == "reverse":
        entry = get_object_or_404(
            LedgerEntry.objects.for_user(actor), pk=request.POST.get("entry"), lot=lot
        )
        _flash(
            request,
            lambda: services.reverse_entry(
                entry, reason=request.POST.get("reason", ""), actor=actor, request=request
            ),
            "Reversal posted.",
        )
    elif action == "reassign":
        entry = get_object_or_404(
            LedgerEntry.objects.for_user(actor), pk=request.POST.get("entry"), lot=lot
        )
        try:
            account_id = int(request.POST.get("account") or 0)
        except ValueError:
            account_id = 0
        _flash(
            request,
            lambda: services.reassign_entry(
                entry,
                account_id=account_id,
                reason=request.POST.get("reason", ""),
                actor=actor,
                request=request,
            ),
            "Moved. The original is reversed on its old account and a copy is on the chosen one.",
        )
    elif action == "movechecked":
        from ledger.models import EntryMoveNotice

        notice = get_object_or_404(
            EntryMoveNotice.objects.for_user(actor),
            pk=request.POST.get("notice"),
            lot=lot,
        )
        _flash(
            request,
            lambda: services.check_move_notice(notice, actor=actor, request=request),
            "Moved entries marked checked.",
        )
    elif action == "collection":
        # "Open collection case": go to the lot's open case, creating it if
        # there is none yet.
        policy.require(actor, "collections.manage")
        existing = (
            CollectionCase.objects.for_user(actor).filter(lot=lot).exclude(state="closed").first()
        )
        if existing is not None:
            return redirect("collection_detail", pk=existing.pk)
        case = _flash(
            request,
            lambda: services.open_case(lot=lot, actor=actor, request=request),
            "Collection case opened.",
        )
        if case:
            return redirect("collection_detail", pk=case.pk)
    else:
        raise Http404
    return redirect("lot_ledger", pk=lot.pk)


# Three rows to start (WT-033); "Add another row" adds more without losing
# what was typed (in place with JavaScript, by re-posting the form without).
BATCH_ROWS = 3
BATCH_MAX_ROWS = 60


def _batch_row(request, i, lots_by_pk, today):
    """One row of the Record payments form: (values, payment or None, errors)."""
    from core.money import parse_cents

    values = {
        "lot": (request.POST.get(f"lot_{i}") or "").strip(),
        "amount": (request.POST.get(f"amount_{i}") or "").strip(),
        "check": " ".join((request.POST.get(f"check_{i}") or "").split())[:40],
        "date": (request.POST.get(f"date_{i}") or "").strip(),
    }
    if not (values["lot"] or values["amount"] or values["check"]):
        return values, None, []
    errors = []
    lot = lots_by_pk.get(int(values["lot"])) if values["lot"].isdigit() else None
    if lot is None:
        errors.append("Choose the home.")
    try:
        amount = parse_cents(values["amount"])
    except ValueError:
        amount = None
        errors.append("Enter the amount in dollars, e.g. 75.00.")
    try:
        received_on = datetime.date.fromisoformat(values["date"]) if values["date"] else today
    except ValueError:
        received_on = None
        errors.append("Enter the date received.")
    if errors:
        return values, None, errors
    payment = services.BatchPayment(lot, amount, received_on, values["check"])
    return values, payment, services.check_payment(payment, today=today)


@login_required
def record_payments(request):
    """Enter a stack of checks on one page (managers and the board), then
    offer to put them in a deposit."""
    from ledger import accounting as acc

    policy.require(request.user, "ledger.manage")
    today = timezone.localdate()
    if request.method == "POST" and request.POST.get("section") == "deposit":
        policy.require(request.user, "accounting.manage")
        try:
            acc.create_deposit(
                entry_ids=[i for i in request.POST.getlist("entries") if i.isdigit()],
                deposited_on=_date(request.POST.get("deposited_on"), today),
                bank_reference=request.POST.get("bank_reference", ""),
                actor=request.user,
                request=request,
            )
        except acc.AccountingError as exc:
            messages.error(request, str(exc))
            return redirect("record_payments")
        messages.success(request, "Deposit recorded.")
        return redirect("accounting")
    lots = list(Lot.objects.for_user(request.user).order_by("street_address", "number"))
    lots_by_pk = {lot.pk: lot for lot in lots}
    try:
        count = int(request.POST.get("rows") or request.GET.get("rows") or BATCH_ROWS)
    except ValueError:
        count = BATCH_ROWS
    count = max(1, min(count, BATCH_MAX_ROWS))
    rows = [{"values": {"date": today.isoformat()}, "errors": []} for _ in range(count)]
    if request.method == "POST" and request.POST.get("more"):
        # "Add another row" without JavaScript: the same rows as typed, one
        # more empty row, nothing checked or recorded yet.
        for i in range(count):
            values, _payment, _errors = _batch_row(request, i, lots_by_pk, today)
            rows[i] = {"values": values, "errors": []}
        if count < BATCH_MAX_ROWS:
            rows.append({"values": {"date": today.isoformat()}, "errors": []})
            count += 1
    elif request.method == "POST":
        payments, has_errors = [], False
        for i in range(count):
            values, payment, errors = _batch_row(request, i, lots_by_pk, today)
            rows[i] = {"values": values, "errors": errors}
            has_errors = has_errors or bool(errors)
            if payment and not errors:
                payments.append(payment)
        if not has_errors:
            try:
                entries = services.record_payments(payments, actor=request.user, request=request)
            except services.LedgerError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"{len(entries)} payment(s) recorded.")
                ids = ",".join(str(e.pk) for e in entries)
                return redirect(f"{request.path}?recorded={ids}")
        else:
            messages.error(request, "Fix the rows marked below; nothing was recorded.")
    recorded = []
    ids = [int(i) for i in request.GET.get("recorded", "").split(",") if i.isdigit()][:100]
    if ids and request.method == "GET":
        undeposited = {e.pk for e in acc.undeposited_payments()}
        recorded = [
            (e, e.pk in undeposited)
            for e in LedgerEntry.objects.for_user(request.user)
            .filter(pk__in=ids, kind=EntryKind.PAYMENT)
            .select_related("lot")
        ]
    return render(
        request,
        "ledger/record_payments.html",
        {
            "lots": lots,
            "rows": rows,
            "row_count": count,
            "max_rows": BATCH_MAX_ROWS,
            "today": today,
            "recorded": recorded,
            "recorded_total": sum(-e.amount_cents for e, _ in recorded),
            "can_deposit": policy.can(request.user, "accounting.manage")
            and any(open_ for _, open_ in recorded),
        },
    )


@login_required
def lot_statement_pdf(request, pk, kind):
    _finance(request)
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=pk)
    record_event(
        "ledger.statement_downloaded",
        request=request,
        actor=request.user,
        target=lot,
        metadata={"lot_id": lot.pk},
    )
    return pdf_response(
        services.statement_pdf(lot, reminder=kind == "reminder"), f"{kind}-lot-{lot.number}.pdf"
    )


# --- Collections -----------------------------------------------------------------------------


@login_required
def collections(request):
    _finance(request)
    return render(
        request,
        "ledger/collections.html",
        {
            "cases": CollectionCase.objects.for_user(request.user).select_related("lot"),
            "delinquent": [
                (lot, bal, services.aging_columns(a)) for lot, bal, a in services.delinquent_lots()
            ],
            "bucket_labels": list(services.BUCKET_LABELS.values()),
        },
    )


def _next_letter(case):
    from ledger import accounting as acc
    from ledger.models import LetterKind

    kind = acc.next_letter(case)
    if kind is None or case.state in ("closed", "referred"):
        return None
    return {"label": LetterKind(kind).label, "due_on": acc.letter_due_on(case)}


@login_required
def collection_detail(request, pk):
    _finance(request)
    case = get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)
    actions = [
        t
        for t in services.COLLECTION.available(case, request.user)
        if t.action not in ("send_reminder", "send_second_reminder", "send_final_notice")
    ]
    return render(
        request,
        "ledger/collection_detail.html",
        {
            "obj": case,
            "balance": services.balance(case.lot_id),
            "actions": actions,
            "note_required_for": [t.label for t in actions if t.note_required],
            "history": services.COLLECTION.history(case),
            "next_letter": _next_letter(case),
            "letters": CollectionLetter.objects.for_user(request.user).filter(case=case),
            "can_paper_copy": case.state not in ("closed", "referred")
            and policy.can(request.user, "collections.manage")
            and policy.can(request.user, "mail.print"),
            "action_url_name": "collection_action",
        },
    )


@login_required
@require_POST
def collection_action(request, pk, action):
    case = get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)
    if action not in services.COLLECTION.transitions:
        raise Http404
    _flash(
        request,
        lambda: services.COLLECTION.apply(
            case, action, actor=request.user, note=request.POST.get("note", ""), request=request
        ),
        "Updated.",
    )
    return redirect("collection_detail", pk=case.pk)


# --- Budgets ---------------------------------------------------------------------------------


class LineForm(forms.Form):
    kind = forms.ChoiceField(choices=LineKind.choices)
    name = forms.CharField(max_length=120)
    annual_cents = MoneyField(label="Annual amount ($)")
    category = forms.ModelChoiceField(
        queryset=ExpenseCategory.objects.filter(active=True),
        required=False,
        help_text="Expense lines",
    )
    income_source = forms.ChoiceField(
        choices=[("", "-"), *IncomeSource.choices], required=False, help_text="Income lines"
    )


@login_required
def budgets(request):
    _finance(request)
    if request.method == "POST":
        policy.require(request.user, "budget.manage")
        if "code" in request.POST:
            from operations import services as ops

            try:
                ops.save_category(
                    code=request.POST.get("code", ""),
                    name=request.POST.get("name", ""),
                    actor=request.user,
                    request=request,
                )
                messages.success(request, "Category added.")
            except ops.OperationsError as exc:
                messages.error(request, str(exc))
            return redirect("budgets")
        budget = _flash(
            request,
            lambda: services.create_budget(
                fiscal_year=int(request.POST.get("fiscal_year") or 0),
                actor=request.user,
                request=request,
            ),
            "Budget created.",
        )
        return redirect("budget_detail", pk=budget.pk) if budget else redirect("budgets")
    return render(
        request,
        "ledger/budgets.html",
        {
            "budgets": Budget.objects.all(),
            "categories": ExpenseCategory.objects.all(),
            "year": timezone.localdate().year + 1,
            "can_manage": policy.can(request.user, "budget.manage"),
        },
    )


@login_required
def budget_detail(request, pk):
    _finance(request)
    budget = get_object_or_404(Budget, pk=pk)
    if request.method == "POST":
        policy.require(request.user, "budget.manage")
        if request.POST.get("notice") in ("proposed", "adopted"):
            result = _flash(
                request,
                lambda: services.send_budget_notice(
                    budget, stage=request.POST["notice"], actor=request.user, request=request
                ),
                "Notice sent to owners who accepted email notice.",
            )
            if result and result["paper"]:
                messages.warning(
                    request, f"{len(result['paper'])} owner(s) need a paper copy of this notice."
                )
            return redirect("budget_detail", pk=budget.pk)
        if request.POST.get("adopt"):
            _flash(
                request,
                lambda: services.adopt_budget(budget, actor=request.user, request=request),
                "Budget adopted.",
            )
        else:
            form = LineForm(request.POST)
            if form.is_valid():
                _flash(
                    request,
                    lambda: services.add_line(
                        budget, actor=request.user, request=request, **form.cleaned_data
                    ),
                    "Line added.",
                )
            else:
                messages.error(request, "Check the line.")
        return redirect("budget_detail", pk=budget.pk)
    return render(
        request,
        "ledger/budget_detail.html",
        {
            "budget": budget,
            "report": services.budget_vs_actual(budget),
            "form": LineForm(),
            "can_manage": policy.can(request.user, "budget.manage"),
            "reserve": _reserve_suggestion(request),
            # Paper copies of a notice already emailed (D55).
            "paper_homes": _paper_homes(request.user)
            if budget.proposed_notice_sent_at or budget.adopted_notice_sent_at
            else None,
        },
    )


def _paper_homes(user):
    from comms import services as comms_services

    return comms_services.paper_homes_count(user)


def _reserve_suggestion(request):
    """The latest adopted reserve plan's contribution, for the budget page."""
    from core import features
    from ledger import reserve_plan, reserves

    if not features.enabled("reserves", request):
        return None
    snapshot = reserve_plan.latest_snapshot()
    if snapshot is None:
        return None
    lots = Lot.objects.unscoped("reserve per-lot figure").count()
    return {
        "snapshot": snapshot,
        "per_lot_month": reserves.per_lot_monthly(snapshot.needed_contribution_cents, lots),
        "lots": lots,
    }


# --- Resale certificates -------------------------------------------------------------------


class ResaleForm(forms.Form):
    lot = forms.ModelChoiceField(queryset=Lot.objects.none(), label="Home")
    requester_name = forms.CharField(max_length=120, label="Requested by (name)")
    requester_email = forms.EmailField(required=False)
    expected_settlement = _date(required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        lots = Lot.objects.for_user(user)
        if not policy.can(user, "resale.manage"):
            lots = lots.filter(pk__in=policy.roles_for(user).owned_lot_ids)
        self.fields["lot"].queryset = lots.order_by("street_address", "number")


@login_required
def resales(request):
    policy.require(request.user, "resale.request")
    form = ResaleForm(request.POST or None, user=request.user)
    if request.method == "POST":
        if form.is_valid():
            obj = _flash(
                request,
                lambda: services.request_resale(
                    actor=request.user, request=request, **form.cleaned_data
                ),
                "Request received.",
            )
            if obj:
                return redirect("resale_detail", pk=obj.pk)
        return redirect("resales")
    return render(
        request,
        "ledger/resales.html",
        {
            "rows": ResaleRequest.objects.for_user(request.user).select_related("lot"),
            "form": form,
            "can_manage": policy.can(request.user, "resale.manage"),
        },
    )


@login_required
def resale_detail(request, pk):
    obj = get_object_or_404(ResaleRequest.objects.for_user(request.user), pk=pk)
    can_manage = policy.can(request.user, "resale.manage")
    if request.method == "POST" and can_manage:
        _flash(
            request,
            lambda: services.save_disclosures(
                obj, request.POST.get("other_disclosures", ""), actor=request.user, request=request
            ),
            "Disclosures saved.",
        )
        return redirect("resale_detail", pk=obj.pk)
    return render(
        request,
        "ledger/resale_detail.html",
        {
            "obj": obj,
            "can_manage": can_manage,
            "actions": services.RESALE.available(obj, request.user),
            "history": services.RESALE.history(obj),
            "action_url_name": "resale_action",
        },
    )


@login_required
@require_POST
def resale_action(request, pk, action):
    obj = get_object_or_404(ResaleRequest.objects.for_user(request.user), pk=pk)
    if action not in services.RESALE.transitions:
        raise Http404
    _flash(
        request,
        lambda: services.RESALE.apply(
            obj, action, actor=request.user, note=request.POST.get("note", ""), request=request
        ),
        "Updated.",
    )
    return redirect("resale_detail", pk=obj.pk)


@login_required
def resale_pdf(request, pk):
    obj = get_object_or_404(ResaleRequest.objects.for_user(request.user), pk=pk)
    if obj.state != "issued" and not policy.can(request.user, "resale.manage"):
        raise Http404
    record_event(
        "resale.certificate_downloaded",
        request=request,
        actor=request.user,
        target=obj,
        metadata={"lot_id": obj.lot_id},
    )
    if obj.state == "issued":
        stored = services.issued_certificate(obj)
        if stored is None:
            raise Http404
        return attachments.serve(request, obj, stored.pk)
    return pdf_response(services.resale_certificate(obj), f"resale-certificate-{obj.pk}.pdf")


@login_required
def owner_budget(request):
    """Budget and reserves: the association's budget as owners see it
    (summary lines only) and the adopted reserve plan's summary (no board
    notes). Each part shows while its feature is on (core.features.ANY_OF)."""
    from core import features

    policy.require(request.user, "budget.view_owner")
    show_budget = features.enabled("owner_budget", request)
    show_reserves = features.enabled("reserves", request) and policy.can(
        request.user, "reserves.view_summary"
    )
    budget = services.owner_visible_budget() if show_budget else None
    snapshot = None
    if show_reserves:
        from ledger import reserve_plan

        snapshot = reserve_plan.latest_snapshot()
    return render(
        request,
        "ledger/owner_budget.html",
        {
            "show_budget": show_budget,
            "show_reserves": show_reserves,
            "budget": budget,
            "summary": services.budget_summary(budget) if budget else None,
            "dues": rule_with_citation("dues.monthly_cents"),
            "snapshot": snapshot,
        },
    )


# --- Letters and certificates (Milestone 4) ----------------------------------------------


@login_required
def letters_settings(request):
    from community import services as community
    from community.models import CommunityProfile
    from ledger import letters
    from ledger.models import LetterKind

    policy.require(request.user, "letters.manage")
    if request.method == "POST":
        section = request.POST.get("section")
        try:
            if section == "template":
                services.save_letter_template(
                    request.POST.get("kind", ""),
                    subject=request.POST.get("subject", ""),
                    body=request.POST.get("body", ""),
                    certified=request.POST.get("certified") == "on",
                    actor=request.user,
                    request=request,
                )
            else:
                from core.money import parse_cents

                values = {
                    f: request.POST.get(f, "")
                    for f in community.LETTER_FIELDS + community.RESALE_TEXT_FIELDS
                }
                for f in community.RESALE_MONEY_FIELDS:
                    raw = (request.POST.get(f) or "").strip()
                    values[f] = parse_cents(raw) if raw else None
                community.update_letter_settings(actor=request.user, request=request, **values)
        except (services.LedgerError, community.CommunityError, ValueError) as exc:
            messages.error(
                request,
                str(exc)
                if not isinstance(exc, ValueError)
                else "Check the amounts (dollars, e.g. 125000.00).",
            )
        else:
            messages.success(request, "Saved.")
        return redirect("letters_settings")
    return render(
        request,
        "ledger/letters_settings.html",
        {
            "profile": CommunityProfile.get(),
            "reserve_snapshot": _adopted_reserves(request),
            "templates": [letters.template(k) for k in LetterKind.values],
            "placeholders": letters.PLACEHOLDERS,
        },
    )


def _adopted_reserves(request):
    from core import features
    from ledger import reserve_plan

    return reserve_plan.latest_snapshot() if features.enabled("reserves", request) else None


@login_required
def lot_notice_pdf(request, pk, kind):
    """Old address of the lot-page notice buttons. Delinquency letters now
    come only from the collection case (streamlining 2026-11-26): send people
    to the lot's case, or back to the lot's account to open one."""
    _finance(request)
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=pk)
    if kind not in ("statement", "first", "second", "final"):
        raise Http404
    cases = CollectionCase.objects.for_user(request.user).filter(lot=lot)
    case = cases.exclude(state="closed").first() or cases.order_by("-pk").first()
    if case is not None:
        return redirect("collection_detail", pk=case.pk)
    messages.info(
        request,
        "Delinquency letters are sent from the collection case. "
        'Use "Open collection case" on this page first.',
    )
    return redirect("lot_ledger", pk=lot.pk)


@login_required
def collection_notice_pdf(request, pk, kind):
    """Reprint a notice (or the statement) from the collection case."""
    _finance(request)
    case = get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)
    if kind not in ("statement", "first", "second", "final"):
        raise Http404
    data = services.generate_notice(case.lot, kind, actor=request.user, request=request)
    return pdf_response(data, f"{kind}-lot-{case.lot.number}.pdf")


# --- Money tools (Milestone 4 part 3) ------------------------------------------------------


def _date(value, default=None):
    import datetime

    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return default


def _money(value) -> int:
    from core.money import parse_cents

    return parse_cents(value)


class AccountingSetupForm(forms.Form):
    """Accounting setup: one optional account per kind of entry (empty keeps
    the standard account) and the opening bank balance with its date, which
    go together (owner request 2026-11-26)."""

    opening_cash = MoneyField(label="Opening bank balance", required=False, allow_negative=True)
    opening_cash_on = forms.DateField(
        label="As of", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, **kwargs):
        from ledger import accounting as acc

        super().__init__(*args, **kwargs)
        for key in acc.GL_LABELS:
            self.fields[f"gl_{key}"] = forms.CharField(required=False, max_length=80)

    def clean(self):
        data = super().clean()
        amount, on = data.get("opening_cash"), data.get("opening_cash_on")
        if "opening_cash" in self.errors or "opening_cash_on" in self.errors:
            return data
        if amount is not None and on is None:
            self.add_error("opening_cash_on", "Enter the date of the opening balance.")
        if on is not None and amount is None:
            self.add_error("opening_cash", "Enter the opening balance for that date.")
        return data


@login_required
def accounting(request):
    """Export, deposits, reconciliation, 1099 worksheet and setup."""
    import datetime

    from ledger import accounting as acc
    from ledger.models import AccountingSetup, BankReconciliation, DepositBatch

    _finance(request)
    can_manage = policy.can(request.user, "accounting.manage")
    if request.method == "POST":
        policy.require(request.user, "accounting.manage")
        section = request.POST.get("section")
        try:
            if section == "deposit":
                acc.create_deposit(
                    entry_ids=request.POST.getlist("entries"),
                    deposited_on=_date(request.POST.get("deposited_on"), timezone.localdate()),
                    bank_reference=request.POST.get("bank_reference", ""),
                    actor=request.user,
                    request=request,
                )
                messages.success(request, "Deposit recorded.")
            elif section == "void":
                batch = get_object_or_404(DepositBatch, pk=request.POST.get("batch") or 0)
                acc.void_deposit(batch, actor=request.user, request=request)
                messages.success(request, "Deposit voided; its payments are undeposited again.")
            elif section == "setup":
                return _accounting_setup_save(request)
            else:
                raise Http404
        except acc.AccountingError as exc:
            messages.error(request, str(exc))
        except ValueError:
            messages.error(request, "Check the amount (dollars, e.g. 1250.00).")
        return redirect("accounting")
    if request.GET.get("tab") == "setup":
        policy.require(request.user, "accounting.manage")
        return _accounting_setup_page(request, AccountingSetupForm(initial=_setup_initial()))
    today = timezone.localdate()
    year = int(request.GET.get("year") or today.year - (1 if today.month < 4 else 0))
    undeposited = list(acc.undeposited_payments()) if can_manage else []
    return render(
        request,
        "ledger/accounting.html",
        {
            "can_manage": can_manage,
            "bank_feed_on": features.enabled("bank_feeds", request)
            and policy.can(request.user, "bank_feed.view"),
            "undeposited": undeposited,
            "undeposited_total": abs(sum(e.amount_cents for e in undeposited)),
            "batches": DepositBatch.objects.all()[:20],
            "reconciliations": BankReconciliation.objects.all()[:12],
            "setup_summary": acc.setup_summary(AccountingSetup.get()),
            "book_balance": acc.book_balance(today),
            "year": year,
            "vendors_1099": acc.vendor_1099_totals(year),
            "month_start": today.replace(day=1),
            "last_month_start": (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1),
            "today": today,
        },
    )


def _setup_initial() -> dict:
    from core.templatetags.portal import cents_plain
    from ledger.models import AccountingSetup

    setup = AccountingSetup.get()
    if setup.opening_cash_on is None and not setup.opening_cash_cents:
        return {}
    return {
        "opening_cash": cents_plain(setup.opening_cash_cents),
        "opening_cash_on": setup.opening_cash_on,
    }


def _accounting_setup_page(request, form, status=200):
    """Accounting's Setup tab: accounts grouped under plain headings, then
    the opening balance (owner request 2026-11-26)."""
    from ledger import accounting as acc
    from ledger.models import AccountingSetup

    typed = None
    if form.is_bound:
        typed = {key: form.data.get(f"gl_{key}", "") for key in acc.GL_LABELS}
    return render(
        request,
        "ledger/accounting_setup.html",
        {
            "form": form,
            "groups": acc.setup_groups(AccountingSetup.get(), typed),
            "summary": acc.setup_summary(AccountingSetup.get()),
        },
        status=status,
    )


def _accounting_setup_save(request):
    from ledger import accounting as acc
    from ledger.models import AccountingSetup

    form = AccountingSetupForm(request.POST)
    if not form.is_valid():
        return _accounting_setup_page(request, form)
    data = form.cleaned_data
    try:
        acc.save_setup(
            gl_accounts=acc.gl_text(
                AccountingSetup.get(), {key: data[f"gl_{key}"] for key in acc.GL_LABELS}
            ),
            opening_cash_cents=data["opening_cash"] or 0,
            opening_cash_on=data["opening_cash_on"],
            actor=request.user,
            request=request,
        )
    except acc.AccountingError as exc:
        form.add_error(None, str(exc))
        return _accounting_setup_page(request, form)
    messages.success(request, "Accounting setup saved.")
    return redirect(reverse("accounting") + "?tab=setup")


@login_required
def accounting_export(request):
    from django.http import HttpResponse

    from ledger import accounting as acc

    _finance(request)
    policy.require(request.user, "finance.view")
    start = _date(request.GET.get("start"))
    end = _date(request.GET.get("end"))
    if not start or not end:
        messages.error(request, "Choose the start and end dates.")
        return redirect("accounting")
    try:
        data = acc.export_csv(start, end, actor=request.user, request=request)
    except acc.AccountingError as exc:
        messages.error(request, str(exc))
        return redirect("accounting")
    response = HttpResponse(data, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="journal-{start}-{end}.csv"'
    return response


@login_required
def accounting_1099(request, year):
    from django.http import HttpResponse

    from ledger import accounting as acc

    _finance(request)
    policy.require(request.user, "finance.view")
    response = HttpResponse(
        acc.vendor_1099_csv(int(year), actor=request.user, request=request),
        content_type="text/csv",
    )
    response["Content-Disposition"] = f'attachment; filename="1099-worksheet-{year}.csv"'
    return response


@login_required
def reconcile(request):
    from ledger import accounting as acc

    policy.require(request.user, "accounting.manage")
    statement_date = _date(request.GET.get("date") or request.POST.get("statement_date"))
    if request.method == "POST" and statement_date:
        try:
            rec = acc.reconcile(
                statement_date=statement_date,
                statement_balance_cents=_money(request.POST.get("statement_balance") or ""),
                cleared_deposit_ids=request.POST.getlist("deposits"),
                cleared_payment_ids=request.POST.getlist("payments"),
                cleared_returned_ids=request.POST.getlist("returned"),
                cleared_payout_ids=request.POST.getlist("payouts"),
                cleared_check_ids=request.POST.getlist("checks"),
                note=request.POST.get("note", ""),
                actor=request.user,
                request=request,
            )
        except acc.AccountingError as exc:
            messages.error(request, str(exc))
        except ValueError:
            messages.error(request, "Enter the statement's ending balance, e.g. 12500.00.")
        else:
            if rec.difference_cents:
                messages.warning(
                    request, "Recorded, but it does not balance: see the difference below."
                )
            else:
                messages.success(request, "Reconciled: the statement balances.")
            return redirect("accounting")
        return redirect(f"{request.path}?date={statement_date.isoformat()}")
    feed = _bank_feed_for_statement(request, statement_date)
    return render(
        request,
        "ledger/reconcile.html",
        {
            "statement_date": statement_date,
            "open": acc.uncleared(statement_date) if statement_date else None,
            "book": acc.book_balance(statement_date) if statement_date else None,
            **feed,
        },
    )


def _bank_feed_for_statement(request, statement_date) -> dict:
    """Bank feed help on the reconcile page (feature "bank_feeds"): items
    matched to bank lines are ticked in advance, and the statement balance
    can be filled in from a connected account."""
    from ledger.bankfeeds import matching
    from ledger.bankfeeds import services as bank

    empty = {"ticked": {k: set() for k in ("deposit", "payment", "returned", "payout", "check")}}
    if not statement_date or not bank.feature_on(request):
        return empty
    accounts = list(bank.connections())
    chosen = next((a for a in accounts if str(a.pk) == request.GET.get("feed", "")), None)
    if chosen is None and len(accounts) == 1:
        chosen = accounts[0]
    return {
        "ticked": matching.matched_for_statement(statement_date),
        "feed_accounts": accounts,
        "feed_account": chosen,
        "feed_balance": bank.feed_balance(chosen, statement_date) if chosen else None,
    }


@login_required
@require_POST
def collection_paper_copy(request, pk):
    """ "Also prepare a paper copy" of one of this case's letters (WT-006):
    a Paper mail mailing for this home only (D55 records)."""
    from comms import services as comms

    case = get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)
    policy.require(request.user, "collections.manage")
    policy.require(request.user, "mail.print")
    letter = get_object_or_404(
        CollectionLetter.objects.for_user(request.user).filter(case=case),
        pk=request.POST.get("letter") or 0,
    )
    try:
        mailing = comms.paper_copy_of_case_letter(letter, actor=request.user, request=request)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("collection_detail", pk=case.pk)
    messages.success(request, "Paper copy prepared for this home. Print it below.")
    return redirect("paper_mail_detail", pk=mailing.pk)


@login_required
@require_POST
def collection_letter(request, pk):
    from ledger import accounting as acc

    case = get_object_or_404(CollectionCase.objects.for_user(request.user), pk=pk)
    try:
        data, kind = acc.generate_case_letter(case, actor=request.user, request=request)
    except acc.AccountingError as exc:
        messages.error(request, str(exc))
        return redirect("collection_detail", pk=case.pk)
    return pdf_response(data, f"{kind}-notice-lot-{case.lot.number}.pdf")
