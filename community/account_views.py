"""Accounts (Milestone 3): people on an account, account numbers, extra
contact details, and read-only history for former owners."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from community import services
from community.models import (
    CONTACT_KIND_HELP,
    Account,
    AccountContact,
    ContactKind,
    ContactMethod,
    ContactMethodKind,
    Ownership,
    Person,
)
from core import policy
from core.scoping import system_access


def _flash(request, fn, ok: str):
    try:
        fn()
    except services.CommunityError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, ok)


@login_required
@require_POST
def account_number(request, pk):
    policy.require(request.user, "account.manage")
    account = get_object_or_404(Account.objects.for_user(request.user), pk=pk)
    _flash(
        request,
        lambda: services.set_account_number(
            account, request.POST.get("number", ""), actor=request.user, request=request
        ),
        "Account number saved.",
    )
    return redirect("lot_detail", pk=account.lot_id)


@login_required
@require_POST
def account_contact_add(request, pk):
    policy.require(request.user, "account.manage")
    account = get_object_or_404(Account.objects.for_user(request.user), pk=pk)
    person_id = request.POST.get("person")
    name = (request.POST.get("name") or "").strip()

    def add():
        if person_id:
            person = get_object_or_404(Person.objects.for_user(request.user), pk=person_id)
        elif name:
            person = services.record_person(
                display_name=name,
                email=(request.POST.get("email") or "").strip(),
                actor=request.user,
                request=request,
            )
        else:
            raise services.CommunityError("Choose a person or enter a new name.")
        services.add_account_contact(
            account,
            person=person,
            kind=request.POST.get("kind", ""),
            actor=request.user,
            request=request,
        )

    _flash(request, add, "Added to the account.")
    return redirect("lot_detail", pk=account.lot_id)


@login_required
@require_POST
def account_contact_end(request, pk):
    policy.require(request.user, "account.manage")
    contact = get_object_or_404(AccountContact.objects.for_user(request.user), pk=pk)
    _flash(
        request,
        lambda: services.end_account_contact(contact, actor=request.user, request=request),
        "Removed from the account.",
    )
    return redirect("lot_detail", pk=contact.account.lot_id)


@login_required
@require_POST
def contact_method_add(request):
    person = get_object_or_404(Person.objects.for_user(request.user), user=request.user)
    _flash(
        request,
        lambda: services.add_contact_method(
            person,
            kind=request.POST.get("kind", ""),
            value=request.POST.get("value", ""),
            label=request.POST.get("label", ""),
            notices=request.POST.get("notices") == "on",
            actor=request.user,
        ),
        "Contact detail added.",
    )
    return redirect("my_profile")


@login_required
@require_POST
def contact_method_remove(request, pk):
    method = get_object_or_404(ContactMethod.objects.for_user(request.user), pk=pk)
    _flash(
        request,
        lambda: services.remove_contact_method(method, actor=request.user),
        "Contact detail removed.",
    )
    return redirect("my_profile")


def pending_changes_for_lot(user, lot):
    from community.models import AccountChangeRequest

    changes = list(
        AccountChangeRequest.objects.for_user(user)
        .filter(account__lot=lot, state="pending")
        .select_related("contact__person", "ownership__person", "requested_by")
    )
    _add_deed_files(changes)
    return changes


def account_context(user, lot) -> dict:
    """Current and dormant accounts of a lot, with their people (board view)."""
    with system_access("accounts on the lot page"):
        accounts = list(Account.objects.filter(lot=lot).prefetch_related("ownerships__person"))
        contacts = list(
            AccountContact.objects.filter(account__lot=lot).select_related("person", "account")
        )
        people = list(Person.objects.order_by("display_name"))
    for account in accounts:
        account.contact_rows = [c for c in contacts if c.account_id == account.pk]
        account.owner_rows = list(account.ownerships.all())
    return {
        "accounts": accounts,
        "contact_kinds": [(k.value, k.label, CONTACT_KIND_HELP[k]) for k in ContactKind],
        "people_choices": people,
        "can_manage_accounts": policy.can(user, "account.manage"),
        "pending_changes": pending_changes_for_lot(user, lot),
        "can_approve_changes": policy.can(user, "account.approve_change"),
    }


def my_account_people(user, lot_id) -> dict:
    """What an owner or contact sees about their own account."""
    with system_access("my account people"):
        account = Account.objects.filter(lot_id=lot_id, closed_on__isnull=True).first()
        if account is None:
            return {}
        owners = [o.person for o in account.ownerships.select_related("person") if o.is_active()]
        contacts = list(
            AccountContact.objects.filter(account=account).active().select_related("person")
        )
    return {"account": account, "owners": owners, "contacts": contacts}


def _owner_ownerships(user, account):
    """Current owners on the account, as the owner may see them."""
    return (
        Ownership.objects.for_user(user)
        .filter(account=account, end_date__isnull=True)
        .select_related("person")
        .order_by("start_date", "id")
    )


def _request_forms(user, account, bound=None) -> dict:
    """The owner's three request forms for one account (D41), with the one
    just sent bound when it came back with errors."""
    from community import forms

    def auto_id(kind):
        return f"{kind}-{account.pk}-%s"

    out = {
        "contact_form": forms.ContactAddRequestForm(auto_id=auto_id("contact")),
        "owner_add_form": forms.OwnerAddRequestForm(auto_id=auto_id("owner-add")),
        "owner_remove_form": forms.OwnerRemoveRequestForm(
            auto_id=auto_id("owner-remove"), ownerships=_owner_ownerships(user, account)
        ),
        "open_panel": "",
    }
    if bound is not None and bound[0] == account.pk:
        _pk, key, form = bound
        out[key] = form
        out["open_panel"] = key
    return out


def can_view_deed(user, change) -> bool:
    """The recorded deed on a request: the board, and the owners of that
    account (the change was already fetched through its scoped manager)."""
    if policy.can(user, "account.approve_change"):
        return True
    return change.account.lot_id in policy.roles_for(user).owned_lot_ids and policy.can(
        user, "account.request_change"
    )


@login_required
@require_POST
def account_change_request(request, pk):
    """Owner: ask the board to add or remove someone on their account, or
    (with the recorded deed) an owner on the deed (D41). A form that comes
    back with errors is shown again on My home with what was typed."""
    from community import forms
    from community.models import AccountChangeAction

    policy.require(request.user, "account.request_change")
    account = get_object_or_404(Account.objects.for_user(request.user), pk=pk)
    action = request.POST.get("action", "")
    if action == AccountChangeAction.REMOVE:
        contact = get_object_or_404(
            AccountContact.objects.for_user(request.user), pk=request.POST.get("contact")
        )
        _flash(
            request,
            lambda: services.request_account_change(
                account,
                action=action,
                contact=contact,
                reason=request.POST.get("reason", ""),
                actor=request.user,
                request=request,
            ),
            "Sent to the board for approval. You'll see the result here.",
        )
        return redirect("my_lot")
    if action == AccountChangeAction.ADD:
        key = "contact_form"
        form = forms.ContactAddRequestForm(request.POST, auto_id=f"contact-{account.pk}-%s")
    elif action == AccountChangeAction.ADD_OWNER:
        key = "owner_add_form"
        form = forms.OwnerAddRequestForm(
            request.POST, request.FILES, auto_id=f"owner-add-{account.pk}-%s"
        )
    elif action == AccountChangeAction.REMOVE_OWNER:
        key = "owner_remove_form"
        form = forms.OwnerRemoveRequestForm(
            request.POST,
            request.FILES,
            auto_id=f"owner-remove-{account.pk}-%s",
            ownerships=_owner_ownerships(request.user, account),
        )
    else:
        messages.error(request, "Choose add or remove.")
        return redirect("my_lot")
    if form.is_valid():
        data = form.cleaned_data
        try:
            services.request_account_change(
                account,
                action=action,
                kind=data.get("kind", ""),
                name=data.get("name", ""),
                email=data.get("email", ""),
                no_login=data.get("no_login", False),
                ownership_type=data.get("ownership_type", ""),
                deed_date=data.get("deed_date"),
                ownership=data.get("ownership"),
                deed=data.get("deed"),
                reason=data.get("reason", ""),
                actor=request.user,
                request=request,
            )
        except services.CommunityError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Sent to the board for approval. You'll see the result here.")
            return redirect("my_lot")
    from community.views import render_my_lot

    return render_my_lot(request, bound=(account.pk, key, form))


@login_required
def account_change_deed(request, pk, file_id):
    """The recorded deed attached to a deed-change request: private,
    scan-gated, streamed after the scoped fetch (board and the account's
    owners only)."""
    from community.models import AccountChangeRequest
    from documents import attachments

    change = get_object_or_404(AccountChangeRequest.objects.for_user(request.user), pk=pk)
    if not can_view_deed(request.user, change):
        raise Http404
    return attachments.serve(request, change, file_id)


@login_required
@require_POST
def account_change_withdraw(request, pk):
    from community.models import AccountChangeRequest

    change = get_object_or_404(AccountChangeRequest.objects.for_user(request.user), pk=pk)
    _flash(
        request,
        lambda: services.withdraw_account_change(change, actor=request.user, request=request),
        "Request withdrawn.",
    )
    return redirect("my_lot")


def _approved_message(change) -> tuple[str, str]:
    """(level, text) for the board member who just approved."""
    if change.invitation_sent is True:
        return "success", "Approved: the account is updated and they were emailed an invitation."
    if change.invitation_sent is False:
        return (
            "warning",
            "Approved: the account is updated, but the invitation could not be created"
            " (for example, the email already belongs to a login). Send it from Invitations"
            " and sign-ups.",
        )
    if change.adds_person:
        return (
            "success",
            "Approved. They are on the account as a record only: they cannot sign in until"
            " the board adds an email to their record and invites them.",
        )
    return "success", "Approved: the account is updated."


@login_required
@require_POST
def account_change_decide(request, pk, decision):
    from community.models import AccountChangeRequest

    policy.require(request.user, "account.approve_change")
    change = get_object_or_404(AccountChangeRequest.objects.for_user(request.user), pk=pk)
    try:
        change = services.decide_account_change(
            change,
            approve=decision == "approve",
            note=request.POST.get("note", ""),
            actor=request.user,
            request=request,
        )
    except services.CommunityError as exc:
        messages.error(request, str(exc))
    else:
        if decision == "approve":
            level, text = _approved_message(change)
            getattr(messages, level)(request, text)
        else:
            messages.success(request, "Declined.")
    return redirect("lot_detail", pk=change.account.lot_id)


def owner_account_panel(user, lot_id, bound=None) -> dict:
    """My home: who is on the account, the owner's change requests and the
    forms to ask for more."""
    from community.models import AccountChangeRequest

    data = my_account_people(user, lot_id)
    if not data:
        return {}
    changes = list(
        AccountChangeRequest.objects.for_user(user)
        .filter(account=data["account"])
        .select_related("contact__person", "ownership__person")[:10]
    )
    _add_deed_files(changes)
    data["changes"] = changes
    data["can_request"] = lot_id in policy.roles_for(user).owned_lot_ids and policy.can(
        user, "account.request_change"
    )
    if data["can_request"]:
        data.update(_request_forms(user, data["account"], bound))
    return data


def _add_deed_files(changes) -> None:
    from documents.attachments import attachments_for

    for ch in changes:
        ch.deed_files = list(attachments_for(ch)) if ch.is_owner_change else []


def _period_account(accounts, start, end):
    """Of a former owner's accounts on one lot, the one for the ownership
    period ``start``..``end``."""

    def overlap(a) -> int:
        lo = max(a.opened_on, start)
        hi = min(a.closed_on or end, end)
        return (hi - lo).days

    covering = [
        a for a in accounts if a.opened_on <= start and (a.closed_on is None or a.closed_on > start)
    ]
    if covering:
        return covering[-1]
    return max(accounts, key=overlap, default=None)


@login_required
def my_history(request):
    """Former owners: their own records from their time, read-only."""
    policy.require(request.user, "history.view_own")
    from arc.models import ArcRequest
    from compliance.models import Violation
    from ledger.accounts import ALL
    from ledger.models import ResaleRequest
    from ledger.services import period_statement

    roles = policy.roles_for(request.user)
    periods = []
    for lot_id, start, end in roles.former_periods:
        with system_access("my history"):
            # The account of this period (someone who sold, bought back and
            # sold again has one per period, BUG-010): the one open on the
            # day the period started, else the one overlapping it most.
            mine = list(
                Account.objects.filter(pk__in=roles.former_account_ids, lot_id=lot_id)
                .select_related("lot")
                .order_by("opened_on", "id")
            )
            account = _period_account(mine, start, end)
            lot = account.lot if account else None
        # Their own ownership account only (D33-acctfix-1). Once it is
        # dormant nobody else is on it, so later entries on it (a refund of
        # their credit) are theirs too; the credit still owed is shown.
        dormant = account is not None and not account.is_active
        statement = period_statement(
            lot_id,
            start,
            end,
            account=account.pk if account else ALL,
            include_after_end=dormant,
        )
        sold = (
            period_statement(lot_id, start, end, account=account.pk if account else ALL)
            if dormant
            else statement
        )
        periods.append(
            {
                "lot": lot,
                "account": account,
                "start": start,
                "end": end,
                "statement": statement,
                "sold_balance": sold.balance,
                "credit_owed": -statement.balance if dormant and statement.balance < 0 else 0,
            }
        )
    from django.db.models import Q

    from operations.models import OwnerRequest

    # Requests from their time only (not a home they own or follow now).
    era = Q(pk__in=[])
    for lot_id, start, end in roles.former_periods:
        era |= Q(lot_id=lot_id, created_at__date__gte=start, created_at__date__lt=end)
    return render(
        request,
        "community/my_history.html",
        {
            "periods": periods,
            "requests": OwnerRequest.objects.for_user(request.user)
            .filter(era)
            .select_related("lot"),
            "arc": ArcRequest.objects.for_user(request.user).select_related("lot"),
            "violations": Violation.objects.for_user(request.user).select_related("lot"),
            "resales": ResaleRequest.objects.for_user(request.user).select_related("lot"),
        },
    )


__all__ = ["ContactMethodKind"]
