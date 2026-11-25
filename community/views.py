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
    from community.account_views import owner_account_panel
    from community.models import CONTACT_KIND_HELP, ContactKind

    lots = Lot.objects.for_user(request.user).filter(pk__in=roles.account_lot_ids)
    owners = Ownership.objects.for_user(request.user).active().select_related("person")
    return render(
        request,
        "community/my_lot.html",
        {
            "lots": [
                (
                    lot,
                    [o for o in owners if o.lot_id == lot.pk],
                    owner_account_panel(request.user, lot.pk),
                )
                for lot in lots
            ],
            "contact_kinds": [(k.value, k.label, CONTACT_KIND_HELP[k]) for k in ContactKind],
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
    from community.account_views import account_context

    accounts = (
        account_context(request.user, lot) if policy.can(request.user, "account.manage") else {}
    )
    return render(
        request,
        "community/lot_detail.html",
        {
            **accounts,
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
    from core.my_items import counts, items_for

    return render(
        request,
        "community/board_home.html",
        {
            "item_counts": counts(items_for(request.user)),
            "thin": policy.thinly_covered_actions(),
            "past_term": BoardSeat.objects.for_user(request.user)
            .past_term_end()
            .select_related("person"),
            "open_nominations": open_nominations,
            "resets_waiting": resets_waiting,
            "approvals_waiting": len(open_nominations) + len(resets_waiting),
            "preview_lots": Lot.objects.for_user(request.user).order_by("number")
            if policy.can(request.user, "portal.preview_as_owner")
            else [],
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
    from accounts.board_forms import StartResetForm
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
    lambda r, p, d: services.propose_board_seat(person=p, actor=r.user, request=r, **d),
    "Done. If a second board member's approval is needed, it is waiting on Approvals.",
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


def _named(resets, viewer):
    """Who each reset is for (name, email, current lots) and who started it, so
    the approving board member knows exactly whose authenticators go (UX review)."""
    from community.models import Ownership
    from core.people import display_name

    out = list(resets)
    lots: dict[int, list[str]] = {}
    ownerships = (
        Ownership.objects.for_user(viewer)
        .active()
        .filter(person__user_id__in={r.subject_id for r in out})
        .select_related("lot", "person")
        .order_by("lot__number")
    )
    for o in ownerships:
        lots.setdefault(o.person.user_id, []).append(str(o.lot.number))
    for r in out:
        r.subject_name = display_name(r.subject)
        r.subject_email = r.subject.email
        r.subject_lots = lots.get(r.subject_id, [])
        r.started_by_name = display_name(r.started_by)
    return out


@login_required
def approvals(request):
    policy.require(request.user, "role.sysadmin.approve")
    from accounts.models import MfaReset

    nominations = SysadminNomination.objects.for_user(request.user).select_related(
        "person", "nominated_by"
    )
    resets = MfaReset.objects.for_user(request.user).select_related("subject", "started_by")
    from community.models import BoardSeatNomination

    seat_proposals = (
        BoardSeatNomination.objects.for_user(request.user)
        .filter(outcome="")
        .select_related("person")
    )
    return render(
        request,
        "community/approvals.html",
        {
            "seat_proposals": seat_proposals,
            "nominations": [n for n in nominations if n.is_open],
            "resets": _named(
                (r for r in resets[:50] if r.status == "awaiting_approval"), request.user
            ),
            "me": request.user,
        },
    )


# --- Community definition ------------------------------------------------------------


@login_required
def community_settings(request):
    policy.require(request.user, "lot.view_all")
    from community import pa_law
    from community.models import CommonArea, CommunityProfile, GoverningRule
    from community.rules import REGISTRY, cents_display, rule_origin

    rows = {r.key: r for r in GoverningRule.objects.all()}
    needing = {s.key for s in rules_needing_attention()}
    groups: dict[str, list] = {}
    for spec in REGISTRY.values():
        row = rows.get(spec.key)
        guide = pa_law.guide(spec.key)
        origin = rule_origin(spec.key)
        raw = row.value if row and row.value != "" else (spec.default or "")
        effective = raw or (guide.fallback if guide and origin == "pa_law" else "")
        shown = cents_display(int(effective)) if spec.kind == "cents" and effective else effective
        groups.setdefault(spec.group, []).append(
            {
                "spec": spec,
                "row": row,
                # Money is typed in dollars, like every other money field.
                "raw": _dollars(raw) if spec.kind == "cents" and raw.lstrip("-").isdigit() else raw,
                "shown": shown,
                "guide": guide,
                "origin": origin,
                "needs_attention": spec.key in needing,
            }
        )
    profile = CommunityProfile.get()
    return render(
        request,
        "community/settings.html",
        {
            "groups": groups,
            "group_notes": pa_law.GROUP_NOTES,
            "pa_disclaimer": pa_law.DISCLAIMER,
            "profile_form": forms.ProfileForm(
                initial={
                    "legal_name": profile.legal_name,
                    "fiscal_year_start_month": profile.fiscal_year_start_month,
                    "welcome_message": profile.welcome_message,
                    "arc_form_return_to": profile.arc_form_return_to,
                    "arc_form_conditions": profile.arc_form_conditions,
                    "signin_help_contact": profile.signin_help_contact,
                }
            ),
            "areas": CommonArea.objects.all(),
            "area_form": forms.CommonAreaForm(),
            "can_edit": policy.can(request.user, "community.manage_rules"),
        },
    )


def _dollars(cents_text: str) -> str:
    cents = int(cents_text)
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


@login_required
@require_POST
def community_rule_save(request):
    policy.require(request.user, "community.manage_rules")
    from community.rules import REGISTRY
    from core.money import parse_cents

    data = request.POST.copy()
    spec = REGISTRY.get(data.get("key", ""))
    if spec is not None and spec.kind == "cents" and (data.get("value") or "").strip():
        try:
            data["value"] = str(parse_cents(data["value"]))
        except ValueError:
            return _failed(
                request,
                CommunityError("Enter the amount in dollars, e.g. 75.00."),
                reverse("community_settings"),
            )
    form = forms.RuleForm(data)
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


@login_required
def roster(request):
    """Board roster: every account with its people, contact details and balance."""
    from core.lists import csv_response, wants_csv
    from core.money import format_cents

    policy.require(request.user, "roster.view")
    include_dormant = request.GET.get("previous") == "1"
    rows = services.roster_rows(actor=request.user, include_dormant=include_dormant)
    if wants_csv(request):
        return csv_response(
            request,
            "roster",
            [
                "Account",
                "Lot",
                "Address",
                "Status",
                "Owners",
                "Other people",
                "Emails",
                "Phones",
                "Balance",
            ],
            (
                [
                    r["account"].number,
                    r["lot"].number,
                    r["lot"].street_address,
                    r["account"].status_label,
                    "; ".join(p.display_name for p in r["owners"]),
                    "; ".join(
                        f"{c.person.display_name} ({c.get_kind_display()})" for c in r["contacts"]
                    ),
                    "; ".join(r["emails"]),
                    "; ".join(r["phones"]),
                    "" if r["balance_cents"] is None else format_cents(r["balance_cents"]),
                ]
                for r in rows
            ),
            audit_kind="roster",
        )
    return render(
        request,
        "community/roster.html",
        {"rows": rows, "include_dormant": include_dormant},
    )


@login_required
@require_POST
def seat_nomination_decide(request, pk, decision):
    from community.models import BoardSeatNomination

    policy.require(request.user, "seat.manage")
    nomination = get_object_or_404(BoardSeatNomination.objects.for_user(request.user), pk=pk)
    back = reverse("approvals")
    try:
        services.decide_seat_nomination(
            nomination, approve=decision == "approve", actor=request.user, request=request
        )
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, "Board seat recorded." if decision == "approve" else "Declined.", back)
