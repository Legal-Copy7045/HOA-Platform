"""Owner and board pages for lots, people, ownership, seats, offices, roles.

Every object is fetched through ``Model.objects.for_user(request.user)``, so an
object outside the user's scope is a 404. Every change goes through
community.services, which re-checks permission and writes the audit event.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from community import forms, services
from community.models import (
    BoardSeat,
    Lot,
    OfficeHolding,
    Ownership,
    Person,
    RoleAssignment,
    SysadminNomination,
)
from community.rules import rules_needing_attention
from community.services import CommunityError
from core import policy


def _scoped(model, request, pk):
    return get_object_or_404(model.objects.for_user(request.user), pk=pk)


def _done(request, message: str, to: str) -> object:
    messages.success(request, message)
    return redirect(to)


def _failed(request, exc: Exception, to: str) -> object:
    messages.error(request, str(exc))
    return redirect(to)


def _invalid(request, form, to: str) -> object:
    for errors in form.errors.values():
        for error in errors:
            messages.error(request, error)
    return redirect(to)


# --- Owners -----------------------------------------------------------------------


@login_required
def my_lot(request):
    policy.require(request.user, "lot.view_own")
    roles = policy.roles_for(request.user)
    lots = Lot.objects.for_user(request.user).filter(pk__in=roles.owned_lot_ids)
    owners = Ownership.objects.for_user(request.user).select_related("person")
    return render(
        request,
        "community/my_lot.html",
        {
            "lots": [(lot, [o for o in owners if o.lot_id == lot.pk]) for lot in lots],
        },
    )


# --- Lots (board, auditor; owners only their own via the detail page) -------------


@login_required
def lot_list(request):
    policy.require(request.user, "lot.view_all")
    lots = Lot.objects.for_user(request.user)
    current = Ownership.objects.for_user(request.user).active().select_related("person")
    owners: dict[int, list] = {}
    for ownership in current:
        owners.setdefault(ownership.lot_id, []).append(ownership.person)
    return render(
        request,
        "community/lot_list.html",
        {
            "rows": [(lot, owners.get(lot.pk, [])) for lot in lots],
        },
    )


@login_required
def lot_detail(request, pk):
    lot = _scoped(Lot, request, pk)
    ownerships = Ownership.objects.for_user(request.user).filter(lot=lot).select_related("person")
    can_manage = policy.can(request.user, "ownership.manage")
    return render(
        request,
        "community/lot_detail.html",
        {
            "lot": lot,
            "current": [o for o in ownerships if o.is_active()],
            "history": [o for o in ownerships if not o.is_active()] if can_manage else [],
            "can_manage": can_manage,
            "transfer_form": forms.TransferForm(user=request.user) if can_manage else None,
        },
    )


@login_required
@require_POST
def lot_transfer(request, pk):
    policy.require(request.user, "ownership.manage")
    lot = _scoped(Lot, request, pk)
    back = reverse("lot_detail", args=[lot.pk])
    form = forms.TransferForm(request.POST, user=request.user)
    if not form.is_valid():
        return _invalid(request, form, back)
    try:
        services.transfer_lot(
            lot,
            new_owners=list(form.cleaned_data["new_owners"]),
            transfer_date=form.cleaned_data["transfer_date"],
            ownership_type=form.cleaned_data["ownership_type"] or None,
            actor=request.user,
            request=request,
        )
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, f"{lot} transferred.", back)


@login_required
@require_POST
def ownership_end(request, pk):
    policy.require(request.user, "ownership.manage")
    ownership = _scoped(Ownership, request, pk)
    back = (
        request.POST.get("back") == "person"
        and reverse("person_detail", args=[ownership.person_id])
    ) or reverse("lot_detail", args=[ownership.lot_id])
    form = forms.EndDateForm(request.POST)
    if not form.is_valid():
        return _invalid(request, form, back)
    try:
        services.end_ownership(
            ownership, end_date=form.cleaned_data["end_date"], actor=request.user, request=request
        )
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, "Ownership ended.", back)


# --- Board dashboard and people ----------------------------------------------------


@login_required
def board_home(request):
    if not policy.can(request.user, "task.manage"):  # board and manager
        policy.require(request.user, "person.manage")
    from accounts.models import Invitation, MfaReset

    open_nominations = [n for n in SysadminNomination.objects.for_user(request.user) if n.is_open]
    resets_waiting = [
        r for r in MfaReset.objects.for_user(request.user) if r.status == "awaiting_approval"
    ]
    return render(
        request,
        "community/board_home.html",
        {
            "thin": policy.thinly_covered_actions(),
            "past_term": BoardSeat.objects.for_user(request.user)
            .past_term_end()
            .select_related("person"),
            "open_nominations": open_nominations,
            "resets_waiting": resets_waiting,
            "approvals_waiting": len(open_nominations) + len(resets_waiting),
            "invites_open": Invitation.objects.for_user(request.user).pending().count(),
        },
    )


@login_required
def people(request):
    policy.require(request.user, "person.manage")
    form = forms.PersonForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            try:
                person = services.record_person(
                    actor=request.user, request=request, **form.cleaned_data
                )
            except CommunityError as exc:
                return _failed(request, exc, reverse("people"))
            return _done(request, "Person added.", reverse("person_detail", args=[person.pk]))
        return _invalid(request, form, reverse("people"))
    people_ = Person.objects.for_user(request.user).select_related("user")
    return render(request, "community/people.html", {"people": people_, "form": form})


@login_required
def person_detail(request, pk):
    policy.require(request.user, "person.manage")
    from accounts.auth import has_strong_authenticator
    from accounts.board_forms import InviteForm, StartResetForm
    from accounts.models import Invitation

    person = _scoped(Person, request, pk)
    user = request.user
    return render(
        request,
        "community/person_detail.html",
        {
            "person": person,
            "is_me": person.user_id == user.pk,
            "has_mfa": person.user_id and has_strong_authenticator(person.user),
            "ownerships": Ownership.objects.for_user(user)
            .filter(person=person)
            .select_related("lot"),
            "seats": BoardSeat.objects.for_user(user).filter(person=person),
            "offices": OfficeHolding.objects.for_user(user).filter(person=person),
            "roles": RoleAssignment.objects.for_user(user).filter(person=person),
            "invitations": Invitation.objects.for_user(user).filter(person=person)[:5],
            "ownership_form": forms.LotOwnershipForm(user=user),
            "seat_form": forms.SeatForm(),
            "end_seat_form": forms.EndSeatForm(),
            "office_form": forms.OfficeForm(),
            "role_form": forms.RoleForm(),
            "end_form": forms.EndDateForm(),
            "invite_form": InviteForm(),
            "reset_form": StartResetForm(),
        },
    )


def _person_action(form_class, do, success):
    """Build a POST view: validate form, call a service, report the result."""

    @login_required
    @require_POST
    def view(request, pk):
        policy.require(request.user, "person.manage")
        person = _scoped(Person, request, pk)
        back = reverse("person_detail", args=[person.pk])
        form = form_class(
            request.POST, **({"user": request.user} if form_class is forms.LotOwnershipForm else {})
        )
        if not form.is_valid():
            return _invalid(request, form, back)
        try:
            do(request, person, form.cleaned_data)
        except CommunityError as exc:
            return _failed(request, exc, back)
        return _done(request, success, back)

    return view


person_add_ownership = _person_action(
    forms.LotOwnershipForm,
    lambda r, p, d: services.record_ownership(person=p, actor=r.user, request=r, **d),
    "Ownership recorded.",
)
person_add_seat = _person_action(
    forms.SeatForm,
    lambda r, p, d: services.record_board_seat(person=p, actor=r.user, request=r, **d),
    "Board seat recorded.",
)
person_add_office = _person_action(
    forms.OfficeForm,
    lambda r, p, d: services.record_office(person=p, actor=r.user, request=r, **d),
    "Office recorded.",
)
person_add_role = _person_action(
    forms.RoleForm,
    lambda r, p, d: services.grant_role(person=p, actor=r.user, request=r, **d),
    "Role granted.",
)


def _end_action(model, form_class, do, success):
    @login_required
    @require_POST
    def view(request, pk):
        policy.require(request.user, "person.manage")
        record = _scoped(model, request, pk)
        back = reverse("person_detail", args=[record.person_id])
        form = form_class(request.POST)
        if not form.is_valid():
            return _invalid(request, form, back)
        try:
            do(request, record, form.cleaned_data)
        except CommunityError as exc:
            return _failed(request, exc, back)
        return _done(request, success, back)

    return view


seat_end = _end_action(
    BoardSeat,
    forms.EndSeatForm,
    lambda r, s, d: services.end_board_seat(s, actor=r.user, request=r, **d),
    "Board seat ended.",
)
office_end = _end_action(
    OfficeHolding,
    forms.EndDateForm,
    lambda r, o, d: services.end_office(o, end_date=d["end_date"], actor=r.user, request=r),
    "Office ended.",
)
role_end = _end_action(
    RoleAssignment,
    forms.EndDateForm,
    lambda r, a, d: services.end_role(a, end_date=d["end_date"], actor=r.user, request=r),
    "Role ended.",
)


# --- Sysadmin nominations -----------------------------------------------------------


@login_required
@require_POST
def sysadmin_nominate(request, pk):
    policy.require(request.user, "role.sysadmin.nominate")
    person = _scoped(Person, request, pk)
    back = reverse("person_detail", args=[person.pk])
    try:
        services.nominate_sysadmin(person=person, actor=request.user, request=request)
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, "Nominated. A different board member must approve it.", back)


@login_required
@require_POST
def nomination_decide(request, pk, decision):
    pk = int(pk)
    policy.require(request.user, "role.sysadmin.approve")
    nomination = _scoped(SysadminNomination, request, pk)
    back = reverse("approvals")
    try:
        if decision == "approve":
            services.approve_sysadmin(nomination, actor=request.user, request=request)
            message = "Approved. The sysadmin role is now active."
        else:
            services.decline_sysadmin(nomination, actor=request.user, request=request)
            message = "Nomination closed."
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, message, back)


@login_required
def approvals(request):
    policy.require(request.user, "role.sysadmin.approve")
    from accounts.models import MfaReset

    nominations = SysadminNomination.objects.for_user(request.user).select_related(
        "person", "nominated_by"
    )
    resets = MfaReset.objects.for_user(request.user).select_related("subject", "started_by")
    return render(
        request,
        "community/approvals.html",
        {
            "nominations": [n for n in nominations if n.is_open],
            "resets": [r for r in resets[:50] if r.status == "awaiting_approval"],
            "me": request.user,
        },
    )


# --- Community definition ------------------------------------------------------------


@login_required
def community_settings(request):
    policy.require(request.user, "lot.view_all")
    from community.models import CommonArea, CommunityProfile, GoverningRule
    from community.rules import REGISTRY, cents_display

    rows = {r.key: r for r in GoverningRule.objects.all()}
    needing = {s.key for s in rules_needing_attention()}
    groups: dict[str, list] = {}
    for spec in REGISTRY.values():
        row = rows.get(spec.key)
        raw = row.value if row and row.value != "" else (spec.default or "")
        shown = cents_display(int(raw)) if spec.kind == "cents" and raw else raw
        groups.setdefault(spec.group, []).append(
            {
                "spec": spec,
                "row": row,
                "raw": raw,
                "shown": shown,
                "needs_attention": spec.key in needing,
            }
        )
    profile = CommunityProfile.get()
    return render(
        request,
        "community/settings.html",
        {
            "groups": groups,
            "profile_form": forms.ProfileForm(
                initial={
                    "legal_name": profile.legal_name,
                    "fiscal_year_start_month": profile.fiscal_year_start_month,
                }
            ),
            "areas": CommonArea.objects.all(),
            "area_form": forms.CommonAreaForm(),
            "can_edit": policy.can(request.user, "community.manage_rules"),
        },
    )


@login_required
@require_POST
def community_rule_save(request):
    policy.require(request.user, "community.manage_rules")
    form = forms.RuleForm(request.POST)
    back = reverse("community_settings")
    if not form.is_valid():
        return _invalid(request, form, back)
    try:
        services.set_rule(actor=request.user, request=request, **form.cleaned_data)
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, "Rule saved.", back)


@login_required
@require_POST
def community_profile_save(request):
    policy.require(request.user, "community.manage_rules")
    form = forms.ProfileForm(request.POST)
    back = reverse("community_settings")
    if not form.is_valid():
        return _invalid(request, form, back)
    try:
        services.update_profile(actor=request.user, request=request, **form.cleaned_data)
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, "Community profile saved.", back)


@login_required
@require_POST
def common_area_save(request):
    policy.require(request.user, "community.manage_rules")
    form = forms.CommonAreaForm(request.POST)
    back = reverse("community_settings")
    if not form.is_valid():
        return _invalid(request, form, back)
    try:
        services.save_common_area(actor=request.user, request=request, **form.cleaned_data)
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, "Common area saved.", back)
