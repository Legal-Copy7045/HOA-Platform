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
    return render_my_lot(request)


def render_my_lot(request, bound=None):
    """My home. ``bound``: (account pk, form key, form) of a request form that
    came back with errors, shown again open with what was typed."""
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
                    owner_account_panel(request.user, lot.pk, bound),
                )
                for lot in lots
            ],
            "contact_kinds": [(k.value, k.label, CONTACT_KIND_HELP[k]) for k in ContactKind],
        },
    )


@login_required
def board_and_committees(request):
    """Who serves: board members with their offices, and the committees with
    their members. Names and offices only, for every resident (streamlining)."""
    from core import features

    policy.require(request.user, "board_committees.view")
    committees = []
    if features.enabled("committees", request):
        from governance import services as governance

        committees = governance.committees_public(actor=request.user)
    return render(
        request,
        "community/board_and_committees.html",
        {
            "board": services.board_members_public(actor=request.user),
            "committees": committees,
            "show_committees": features.enabled("committees", request),
        },
    )


# --- Lots (board, auditor; owners only their own via the detail page) -------------


@login_required
def lot_list(request):
    """Homes and owners: every lot with its current owners; the roster view
    adds each account's other people, contact details and balance (and the
    CSV export). The old Roster page redirects here."""
    policy.require(request.user, "lot.view_all")
    view = request.GET.get("view")
    roster_view = view == "roster" and policy.can(request.user, "roster.view")
    if roster_view:
        return _roster(request)
    can_signups = policy.can(request.user, "invite.send")
    if view == "signups" and can_signups:
        # Invitations and sign-ups, as a tab (WT-002); the board only.
        from accounts.board_views import signups_context

        return render(
            request,
            "community/lot_list.html",
            {
                **signups_context(request),
                "signups_view": True,
                "can_roster": policy.can(request.user, "roster.view"),
                "can_signups": True,
            },
        )
    lots = sorted(Lot.objects.for_user(request.user), key=lambda lot: lot.sort_key)
    current = Ownership.objects.for_user(request.user).active().select_related("person")
    owners: dict[int, list] = {}
    for ownership in current:
        owners.setdefault(ownership.lot_id, []).append(ownership.person)
    rows = [(lot, owners.get(lot.pk, [])) for lot in lots]
    # Find one home without scrolling every row (WT-029): number, address or
    # an owner's name. Never logged or audited, like portal search.
    q = request.GET.get("q", "").strip()[:100]
    if q:
        needle = q.casefold().removeprefix("lot ").strip()
        rows = [
            (lot, people)
            for lot, people in rows
            if needle == str(lot.number)
            or needle in lot.label.casefold()
            or any(needle in p.display_name.casefold() for p in people)
        ]
    return render(
        request,
        "community/lot_list.html",
        {
            "rows": rows,
            "q": q,
            "total": len(lots),
            "roster_view": False,
            "can_roster": policy.can(request.user, "roster.view"),
            "can_signups": can_signups,
            "can_import": policy.can(request.user, "ownership.manage"),
        },
    )


@login_required
def lot_detail(request, pk):
    lot = _scoped(Lot, request, pk)
    ownerships = Ownership.objects.for_user(request.user).filter(lot=lot).select_related("person")
    can_manage = policy.can(request.user, "ownership.manage")
    from community.account_views import account_context
    from core import conflicts

    # Own matters (D65): one's own home reads as the homeowner's page here;
    # its ownership changes are another board member's to record.
    own = conflicts.page_context(request.user, lot)

    accounts = (
        account_context(request.user, lot) if policy.can(request.user, "account.manage") else {}
    )
    return render(
        request,
        "community/lot_detail.html",
        {
            **accounts,
            **own,
            "lot": lot,
            "current": [o for o in ownerships if o.is_active()],
            "history": [o for o in ownerships if not o.is_active()] if can_manage else [],
            "can_manage": can_manage,
            "can_change": can_manage and not own["own_matter"],
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
    return _done(request, f"{lot.label}: transfer recorded.", back)


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
    """The old Board overview: its alerts are at the top of My items now, so
    bookmarks and emailed links land there."""
    if not policy.can(request.user, "task.manage"):  # board and manager
        policy.require(request.user, "person.manage")
    return redirect("my_items")


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
    total = people_.count()
    # Find a person without scrolling every row (WT-037): name, email or the
    # address of a home they own now. Never logged or audited, like portal search.
    q = request.GET.get("q", "").strip()[:100]
    if q:
        from django.db.models import Q

        by_home = (
            Ownership.objects.for_user(request.user)
            .active()
            .filter(
                Q(lot__street_address__icontains=q) | Q(lot__number=int(q) if q.isdigit() else -1)
            )
        )
        home_people = list(by_home.values_list("person_id", flat=True))
        people_ = people_.filter(
            Q(display_name__icontains=q) | Q(email__icontains=q) | Q(pk__in=home_people)
        )
    # No sign-up filters here: Homes and owners' Sign-ups view is the one
    # place that answers who has not signed up (WT-001, D88).
    context = {"people": people_, "form": form, "q": q, "total": total}
    if policy.can(request.user, "access_review.record"):
        # The quarterly access review lives here, not in Settings (streamlining).
        from accounts import access_review

        last = access_review.latest()
        context["access_review"] = {
            "last_on": last.reviewed_at if last else None,
            "due": access_review.is_due(),
        }
    return render(request, "community/people.html", context)


@login_required
def person_detail(request, pk):
    policy.require(request.user, "person.manage")
    from accounts.auth import has_strong_authenticator
    from accounts.board_forms import StartResetForm
    from accounts.models import Invitation

    person = _scoped(Person, request, pk)
    user = request.user
    has_mfa = bool(person.user_id and has_strong_authenticator(person.user))
    setup_again = None
    if person.user_id and not has_mfa and policy.can(user, "invite.send"):
        # D59: a login that never finished setting up gets a new setup link;
        # any other login without an authenticator needs the reset.
        from accounts.services import setup_again_refusal

        setup_again = {"refusal": setup_again_refusal(person)}
    return render(
        request,
        "community/person_detail.html",
        {
            "person": person,
            "is_me": person.user_id == user.pk,
            "has_mfa": has_mfa,
            "setup_again": setup_again,
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
            "contact": _person_contact(user, person),
            "bouncing": _bouncing(person),
        },
    )


def _bouncing(person) -> list[dict]:
    """Addresses of this person that bounce (D74), while the feature is on."""
    from comms import bounces

    return bounces.bouncing(person) if bounces.feature_on() else []


def _person_contact(user, person) -> dict:
    """How the association reaches this person (D44): notices choice, the
    address paper goes to, extra emails and phones. Board pages only."""
    from community.models import ContactMethod, ContactMethodKind, ContactProfile

    profile = ContactProfile.objects.for_user(user).filter(person=person).first()
    methods = list(ContactMethod.objects.for_user(user).filter(person=person))
    address, source = services.postal_address(person)
    return {
        "profile": profile,
        "answered": bool(profile and profile.consent_changed_at),
        "consent": bool(profile and profile.electronic_notice_consent),
        "address": address,
        "source": source,
        "emails": [m for m in methods if m.kind == ContactMethodKind.EMAIL],
        "phones": [m for m in methods if m.kind == ContactMethodKind.PHONE],
        "needs_check": services.needs_address_check(person.pk),
    }


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
        lots.setdefault(o.person.user_id, []).append(o.lot.label)
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
    from community.rule_input import citation_example, example_value
    from community.rules import (
        REGISTRY,
        group_title,
        rule_origin,
        unit_suffix,
        value_display,
    )
    from community.terms import charge_term, term_text

    rows = {r.key: r for r in GoverningRule.objects.all()}
    needing = {s.key for s in rules_needing_attention()}
    term = charge_term()
    groups: dict[str, list] = {}
    group_notes: dict[str, str] = {}
    for spec in REGISTRY.values():
        row = rows.get(spec.key)
        guide = pa_law.guide(spec.key)
        origin = rule_origin(spec.key)
        raw = row.value if row and row.value != "" else (spec.default or "")
        effective = raw or (guide.fallback if guide and origin == "pa_law" else "")
        # As people read it everywhere: "$75.00", "51%", "3%" (value_display).
        shown = value_display(spec, effective)
        # The "Dues" group is headed with the board's word (D42).
        title = group_title(spec.group, term)
        if spec.group in pa_law.GROUP_NOTES:
            group_notes[title] = pa_law.GROUP_NOTES[spec.group]
        groups.setdefault(title, []).append(
            {
                "spec": spec,
                "label": term_text(spec.label, term),
                "help": term_text(spec.help, term),
                "row": row,
                # Money is typed in dollars, like every other money field.
                "raw": _typed(spec, raw),
                "shown": shown,
                "suffix": unit_suffix(spec),
                "fallback_shown": value_display(spec, guide.fallback) if guide else "",
                "example": example_value(spec),
                "citation_example": citation_example(spec),
                "guide": guide,
                "origin": origin,
                "needs_attention": spec.key in needing,
                "unit": MONEY_UNITS.get(spec.key, ""),
            }
        )
    # Rules that need attention come first: their groups first, and first
    # within each group (UX review).
    for rules in groups.values():
        rules.sort(key=lambda r: not r["needs_attention"])
    groups = dict(
        sorted(groups.items(), key=lambda item: not any(r["needs_attention"] for r in item[1]))
    )
    attention = [r for rules in groups.values() for r in rules if r["needs_attention"]]
    # Each group is a closed panel unless it holds a setting needing attention (WT-030).
    group_attention = {
        title: sum(1 for r in rules if r["needs_attention"]) for title, rules in groups.items()
    }
    profile = CommunityProfile.get()
    return render(
        request,
        "community/settings.html",
        {
            "groups": groups,
            "attention": attention,
            "group_attention": group_attention,
            # Only the first section needing attention starts open; the rest
            # show their count on the closed summary (WT-027).
            "first_attention": next((g for g, n in group_attention.items() if n), None),
            "group_notes": group_notes,
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
            # Areas are edited with their booking rules (streamlining).
            "booking_rules_on": _booking_rules_on(request),
            "area_form": forms.CommonAreaForm(),
            "can_edit": policy.can(request.user, "community.manage_rules"),
        },
    )


def _booking_rules_on(request) -> bool:
    from core import features

    return features.enabled("reservations", request) and policy.can(
        request.user, "reservation.configure"
    )


def _typed(spec, raw: str) -> str:
    """The stored value as people type it: dollars for money, a percent for
    basis points (WT-007)."""
    from community.rules import BASIS_POINTS, bp_display

    if spec.kind == "cents" and raw.lstrip("-").isdigit():
        return _dollars(raw)
    if spec.kind == BASIS_POINTS and raw.isdigit():
        return bp_display(raw)
    return raw


def _dollars(cents_text: str) -> str:
    cents = int(cents_text)
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


@login_required
@require_POST
def community_rule_save(request):
    policy.require(request.user, "community.manage_rules")
    from community.rule_input import to_stored
    from community.rules import REGISTRY

    data = request.POST.copy()
    spec = REGISTRY.get(data.get("key", ""))
    if spec is not None:
        # Shared with the community setup wizard (D51).
        try:
            data["value"] = to_stored(spec, data.get("value", ""))
        except ValueError as exc:
            return _failed(request, CommunityError(str(exc)), reverse("community_settings"))
    form = forms.RuleForm(data)
    back = reverse("community_settings")
    if not form.is_valid():
        return _invalid(request, form, back)
    big = _big_dues_change(form.cleaned_data)
    if big and request.POST.get("confirm") != "yes":
        # Dues changing by more than a quarter is asked about first (UX review).
        return render(
            request,
            "community/settings_confirm.html",
            {"change": big, "post": form.cleaned_data, "typed": request.POST.get("value", "")},
        )
    try:
        services.set_rule(actor=request.user, request=request, **form.cleaned_data)
    except CommunityError as exc:
        return _failed(request, exc, back)
    saved = "Rule saved."
    if spec is not None and spec.kind == "cents" and form.cleaned_data["value"]:
        unit = f" {MONEY_UNITS[spec.key]}" if spec.key in MONEY_UNITS else ""
        saved = f"Saved: {spec.shown_label} = {_money(form.cleaned_data['value'])}{unit}."
    return _done(request, saved, back + f"#rule-{spec.key}" if spec else back)


# Shown after a dollar amount ("= $75.00 per month").
MONEY_UNITS = {"dues.monthly_cents": "per month"}
# Ask before dues change by more than this share of the current amount.
DUES_CONFIRM_RATIO = 0.25


def _money(cents_text: str) -> str:
    from core.money import format_cents

    return format_cents(int(cents_text))


def _big_dues_change(cleaned: dict) -> dict | None:
    """Old and new dues when the change is more than DUES_CONFIRM_RATIO."""
    from community.rules import rule

    if cleaned.get("key") != "dues.monthly_cents" or not (cleaned.get("value") or "").strip():
        return None
    try:
        new = int(cleaned["value"])
    except ValueError:
        return None
    old = int(rule("dues.monthly_cents") or 0)
    if old <= 0 or abs(new - old) <= old * DUES_CONFIRM_RATIO:
        return None
    return {
        "old": old,
        "new": new,
        "percent": round(abs(new - old) * 100 / old),
        "up": new > old,
    }


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
    if request.POST.get("back") == "booking_rules" and _booking_rules_on(request):
        back = reverse("reservation_settings")
    if not form.is_valid():
        return _invalid(request, form, back)
    try:
        services.save_common_area(actor=request.user, request=request, **form.cleaned_data)
    except CommunityError as exc:
        return _failed(request, exc, back)
    return _done(request, "Common area saved.", back)


@login_required
def roster(request):
    """The old Roster page: now the roster view of Homes and owners."""
    policy.require(request.user, "roster.view")
    query = request.GET.copy()
    query["view"] = "roster"
    return redirect(reverse("lot_list") + "?" + query.urlencode())


def _roster(request):
    """Roster view: every account with its people, contact details and balance."""
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
                "Address",
                "Account",
                "Status",
                "Owners",
                "Other people",
                "Emails",
                "Phones",
                "Balance",
                "Lot number",
            ],
            (
                [
                    r["lot"].label,
                    r["account"].number,
                    r["account"].status_label,
                    "; ".join(p.display_name for p in r["owners"]),
                    "; ".join(
                        f"{c.person.display_name} ({c.get_kind_display()})" for c in r["contacts"]
                    ),
                    "; ".join(r["emails"]),
                    "; ".join(r["phones"]),
                    "" if r["balance_cents"] is None else format_cents(r["balance_cents"]),
                    r["lot"].number,
                ]
                for r in rows
            ),
            audit_kind="roster",
        )
    return render(
        request,
        "community/lot_list.html",
        {
            "roster": rows,
            "include_dormant": include_dormant,
            "roster_view": True,
            "can_roster": True,
            "can_signups": policy.can(request.user, "invite.send"),
        },
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
