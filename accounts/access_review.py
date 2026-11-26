"""Quarterly access review (Milestone 4): every login, what it can reach,
and flags worth a second look. Board only: sysadmins get operations pages,
never owner data (CLAUDE.md).

A board member records that they reviewed it: an append-only
``AccessReview`` with counts (no names) and an audit event. Another board
member who also reviews it records their own row and is shown as "also
reviewed". When the last review is older than ``REVIEW_EVERY_DAYS`` it shows
in the board's My items.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import AccessReview, User
from audit.services import record_event
from core import policy
from core.scoping import system_access

REVIEW_EVERY_DAYS = 90
INACTIVE_DAYS = 90
# Reviews by other board members within this many days of the latest one are
# shown as "also reviewed".
ALSO_REVIEWED_DAYS = 30

FLAG_LABELS = {
    "inactive": f"No sign-in for {INACTIVE_DAYS}+ days",
    "never": "Never signed in",
    "no_mfa": "No authenticator",
    "sysadmin": "Sysadmin",
    "manager": "Manager",
    "former_owner": "Former owner",
    "dormant_contact": "Contact on a dormant account",
    "no_access": "Login with no current access",
    "locked": "Login switched off",
}


@dataclass
class Row:
    user: User
    person: object | None
    lots: list[str] = field(default_factory=list)  # addresses (D40)
    board: bool = False
    offices: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    contacts: list[str] = field(default_factory=list)
    mfa: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def flag_labels(self) -> list[str]:
        return [FLAG_LABELS[f] for f in self.flags]


def rows(on: datetime.date | None = None) -> list[Row]:
    """Every login with its access, in a handful of queries."""
    from allauth.mfa.models import Authenticator

    from community.models import (
        AccountContact,
        BoardSeat,
        OfficeHolding,
        Ownership,
        RoleAssignment,
    )

    on = on or timezone.localdate()
    now = timezone.now()
    mfa_users = set(
        Authenticator.objects.filter(
            type__in=[Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN]
        ).values_list("user_id", flat=True)
    )
    with system_access("access review"):
        users = list(User.objects.select_related("person").order_by("email"))
        lots: dict[int, list] = {}
        for o in Ownership.objects.active(on).select_related("lot"):
            lots.setdefault(o.person_id, []).append(o.lot)
        former = set(
            Ownership.objects.filter(end_date__isnull=False, end_date__lte=on).values_list(
                "person_id", flat=True
            )
        )
        seats = set(BoardSeat.objects.active(on).values_list("person_id", flat=True))
        offices: dict[int, list[str]] = {}
        for o in OfficeHolding.objects.active(on):
            offices.setdefault(o.person_id, []).append(o.get_office_display())
        roles: dict[int, list[str]] = {}
        role_codes: dict[int, set[str]] = {}
        for r in RoleAssignment.objects.active(on):
            roles.setdefault(r.person_id, []).append(r.get_role_display())
            role_codes.setdefault(r.person_id, set()).add(r.role)
        contacts: dict[int, list[str]] = {}
        dormant: set[int] = set()
        for c in AccountContact.objects.active(on).select_related("account__lot"):
            closed = c.account.closed_on is not None and c.account.closed_on <= on
            if closed:
                dormant.add(c.person_id)
            else:
                contacts.setdefault(c.person_id, []).append(
                    f"{c.get_kind_display()}, {c.account.lot.label}"
                )
    out = []
    for user in users:
        person = getattr(user, "person", None)
        pid = person.pk if person is not None else None
        row = Row(
            user=user,
            person=person,
            lots=[lot.label for lot in sorted(lots.get(pid, []), key=lambda lot: lot.sort_key)],
            board=pid in seats,
            offices=offices.get(pid, []),
            roles=roles.get(pid, []),
            contacts=contacts.get(pid, []),
            mfa=user.pk in mfa_users,
        )
        codes = role_codes.get(pid, set())
        if not user.is_active:
            row.flags.append("locked")
        if user.last_login is None:
            row.flags.append("never")
        elif now - user.last_login > datetime.timedelta(days=INACTIVE_DAYS):
            row.flags.append("inactive")
        if not row.mfa:
            row.flags.append("no_mfa")
        if "sysadmin" in codes:
            row.flags.append("sysadmin")
        if "manager" in codes:
            row.flags.append("manager")
        current = row.lots or row.board or row.roles or row.contacts
        if pid in former and not row.lots:
            row.flags.append("former_owner")
        if pid in dormant and not row.contacts:
            row.flags.append("dormant_contact")
        if not current and pid not in former and user.is_active:
            row.flags.append("no_access")
        out.append(row)
    out.sort(key=lambda r: (not r.flags, r.user.email))
    return out


def summary(all_rows: list[Row]) -> dict[str, int]:
    def count(flag):
        return sum(1 for r in all_rows if flag in r.flags)

    return {
        "logins": len(all_rows),
        "flagged": sum(1 for r in all_rows if r.flags),
        "inactive": count("inactive") + count("never"),
        "without_mfa": count("no_mfa"),
        "sysadmins": count("sysadmin"),
        "managers": count("manager"),
    }


def record_review(*, actor, request=None) -> AccessReview:
    policy.require(actor, "access_review.record")
    counts = summary(rows())
    review = AccessReview.objects.create(reviewed_by=actor, **counts)
    record_event(
        "access_review.recorded",
        actor=actor,
        request=request,
        target=review,
        metadata={k: counts[k] for k in ("logins", "flagged", "inactive", "without_mfa")},
    )
    return review


def latest() -> AccessReview | None:
    return AccessReview.objects.select_related("reviewed_by__person").first()


def is_due(on: datetime.datetime | None = None) -> bool:
    last = AccessReview.objects.order_by("-reviewed_at").values_list("reviewed_at", flat=True)
    last = last.first()
    on = on or timezone.now()
    return last is None or on - last > datetime.timedelta(days=REVIEW_EVERY_DAYS)


def _name(user) -> str:
    person = getattr(user, "person", None)
    return person.display_name if person is not None else user.email


@login_required
def access_review(request):
    policy.require(request.user, "access_review.record")
    all_rows = rows()
    counts = summary(all_rows)
    last = latest()
    also = []
    if last is not None:
        window = last.reviewed_at - datetime.timedelta(days=ALSO_REVIEWED_DAYS)
        seen = {last.reviewed_by_id}
        for r in AccessReview.objects.filter(reviewed_at__gte=window).select_related(
            "reviewed_by__person"
        ):
            if r.reviewed_by_id not in seen:
                seen.add(r.reviewed_by_id)
                also.append((_name(r.reviewed_by), r.reviewed_at))
    record_event(
        "access_review.viewed",
        request=request,
        metadata={"logins": counts["logins"], "flagged": counts["flagged"]},
    )
    return render(
        request,
        "accounts/access_review.html",
        {
            "rows": all_rows,
            "counts": counts,
            "last": last,
            "last_name": _name(last.reviewed_by) if last else "",
            "also": also,
            "due": is_due(),
            "every_days": REVIEW_EVERY_DAYS,
            "history": AccessReview.objects.select_related("reviewed_by__person")[:8],
        },
    )


@login_required
@require_POST
def access_review_record(request):
    policy.require(request.user, "access_review.record")
    record_review(actor=request.user, request=request)
    messages.success(request, "Access review recorded. Thank you.")
    return redirect("access_review")
