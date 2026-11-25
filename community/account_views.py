"""Accounts (Milestone 3): people on an account, account numbers, extra
contact details, and read-only history for former owners."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
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

    return (
        AccountChangeRequest.objects.for_user(user)
        .filter(account__lot=lot, state="pending")
        .select_related("contact__person", "requested_by")
    )


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


@login_required
@require_POST
def account_change_request(request, pk):
    """Owner: ask the board to add or remove someone on their account."""
    from community.models import AccountChangeAction

    policy.require(request.user, "account.request_change")
    account = get_object_or_404(Account.objects.for_user(request.user), pk=pk)
    contact = None
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
            kind=request.POST.get("kind", ""),
            name=request.POST.get("name", ""),
            email=request.POST.get("email", ""),
            contact=contact,
            reason=request.POST.get("reason", ""),
            actor=request.user,
            request=request,
        ),
        "Sent to the board for approval. You'll see the result here.",
    )
    return redirect("my_lot")


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


@login_required
@require_POST
def account_change_decide(request, pk, decision):
    from community.models import AccountChangeRequest

    policy.require(request.user, "account.approve_change")
    change = get_object_or_404(AccountChangeRequest.objects.for_user(request.user), pk=pk)
    _flash(
        request,
        lambda: services.decide_account_change(
            change,
            approve=decision == "approve",
            note=request.POST.get("note", ""),
            actor=request.user,
            request=request,
        ),
        "Approved: the account is updated." if decision == "approve" else "Declined.",
    )
    return redirect("lot_detail", pk=change.account.lot_id)


def owner_account_panel(user, lot_id) -> dict:
    """My home: who is on the account and the owner's change requests."""
    from community.models import AccountChangeRequest

    data = my_account_people(user, lot_id)
    if not data:
        return {}
    data["changes"] = AccountChangeRequest.objects.for_user(user).filter(account=data["account"])[
        :10
    ]
    data["can_request"] = lot_id in policy.roles_for(user).owned_lot_ids and policy.can(
        user, "account.request_change"
    )
    return data


@login_required
def my_history(request):
    """Former owners: their own records from their time, read-only."""
    policy.require(request.user, "history.view_own")
    from arc.models import ArcRequest
    from compliance.models import Violation
    from ledger.models import ResaleRequest
    from ledger.services import period_statement

    roles = policy.roles_for(request.user)
    periods = []
    for lot_id, start, end in roles.former_periods:
        with system_access("my history"):
            account = (
                Account.objects.filter(pk__in=roles.former_account_ids, lot_id=lot_id)
                .select_related("lot")
                .first()
            )
            lot = account.lot if account else None
        periods.append(
            {
                "lot": lot,
                "account": account,
                "start": start,
                "end": end,
                "statement": period_statement(lot_id, start, end),
            }
        )
    return render(
        request,
        "community/my_history.html",
        {
            "periods": periods,
            "arc": ArcRequest.objects.for_user(request.user).select_related("lot"),
            "violations": Violation.objects.for_user(request.user).select_related("lot"),
            "resales": ResaleRequest.objects.for_user(request.user).select_related("lot"),
        },
    )


__all__ = ["ContactMethodKind"]
