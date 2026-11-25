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
