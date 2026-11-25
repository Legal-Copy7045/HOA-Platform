"""The single authorization policy (CLAUDE.md "Authorization", decision D4).

Every permission check in the portal goes through ``can()`` / ``require()``.
Who holds which role is computed from dated records at the moment of the
check - never cached on the login - so ending an ownership, seat or role
takes effect on the very next request.

Role groups:

* ``owner``        - an active Ownership (access limited to those lots)
* ``board``        - an active BoardSeat
* ``arc_reviewer``, ``auditor``, ``sysadmin`` - active RoleAssignments

Offices (president, ...) are recorded but grant nothing extra yet: the board
has no internal rules, so every board member can perform every board action.
To give an office its own power later, change PERMISSIONS below (and tests).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied

OWNER = "owner"
BOARD = "board"
ARC_REVIEWER = "arc_reviewer"
AUDITOR = "auditor"
SYSADMIN = "sysadmin"
MANAGER = "manager"  # management-company staff: data entry, never approvals or votes

MEMBERS = frozenset({OWNER, BOARD, ARC_REVIEWER, AUDITOR, MANAGER})

# action -> role groups allowed to perform it. Keep this the only such table.
PERMISSIONS: dict[str, frozenset[str]] = {
    # Reading
    "lot.view_own": frozenset({OWNER}),
    "lot.view_all": frozenset({BOARD, AUDITOR, MANAGER}),
    # Homeowner view: see the portal as one lot's owner sees it (read-only).
    "portal.preview_as_owner": frozenset({BOARD, MANAGER}),
    # Sign-in page picture (no owner data): board, or a sysadmin for operations.
    "branding.manage": frozenset({BOARD, SYSADMIN}),
    "audit.view_sysadmin_activity": frozenset({BOARD, AUDITOR}),
    "system.view_status": frozenset({SYSADMIN}),
    # Community register and definition
    "person.manage": frozenset({BOARD}),
    "ownership.manage": frozenset({BOARD}),
    "seat.manage": frozenset({BOARD}),
    "office.manage": frozenset({BOARD}),
    "role.arc_reviewer.manage": frozenset({BOARD}),
    "role.auditor.manage": frozenset({BOARD}),
    "role.manager.manage": frozenset({BOARD}),
    "community.manage_rules": frozenset({BOARD}),
    "directory.view": frozenset({OWNER, BOARD, MANAGER}),
    # Sysadmin: two different board members to grant, one to revoke (D4)
    "role.sysadmin.nominate": frozenset({BOARD}),
    "role.sysadmin.approve": frozenset({BOARD}),
    "role.sysadmin.revoke": frozenset({BOARD}),
    # Accounts
    # Creating an invitation decides whose mailbox controls a person: board only.
    # Sysadmins may re-send or revoke existing ones (review finding A1).
    "invite.send": frozenset({BOARD}),
    "invite.resend": frozenset({BOARD, SYSADMIN}),
    "invite.revoke": frozenset({BOARD, SYSADMIN}),
    "mfa_reset.start": frozenset({BOARD}),
    "mfa_reset.approve": frozenset({BOARD}),
    # Communications
    "announcement.view": MEMBERS,
    "announcement.publish": frozenset({BOARD, MANAGER}),
    "calendar.view": MEMBERS,
    "calendar.manage": frozenset({BOARD, MANAGER}),
    "task.manage": frozenset({BOARD, MANAGER}),
    # Documents
    "document.view_owner": MEMBERS,
    "document.view_board": frozenset({BOARD, AUDITOR, MANAGER}),
    "document.manage": frozenset({BOARD, MANAGER}),
    # Governance
    "meeting.view": MEMBERS,
    "meeting.manage": frozenset({BOARD, MANAGER}),
    "resolution.view": MEMBERS,
    "resolution.record": frozenset({BOARD}),
    "records_request.submit": frozenset({OWNER}),
    "records_request.manage": frozenset({BOARD, MANAGER}),
    "vote.view": MEMBERS,
    "vote.cast": frozenset({OWNER}),
    "vote.manage": frozenset({BOARD}),
    # Architectural review
    "arc.submit": frozenset({OWNER}),
    "arc.view_all": frozenset({BOARD, ARC_REVIEWER, MANAGER, AUDITOR}),
    "arc.intake": frozenset({BOARD, ARC_REVIEWER, MANAGER}),
    "arc.decide": frozenset({ARC_REVIEWER}),
    "arc.appeal_decide": frozenset({BOARD}),
    # Compliance
    "violation.view_all": frozenset({BOARD, MANAGER, AUDITOR}),
    "violation.manage": frozenset({BOARD, MANAGER}),
    "violation.decide": frozenset({BOARD}),
    # Operations and money
    "vendor.manage": frozenset({BOARD, MANAGER}),
    "workorder.manage": frozenset({BOARD, MANAGER}),
    "invoice.enter": frozenset({BOARD, MANAGER}),
    "invoice.approve": frozenset({BOARD}),
    "invoice.record_payment": frozenset({BOARD, MANAGER}),
    "finance.view": frozenset({BOARD, AUDITOR, MANAGER}),
    "ledger.view_own": frozenset({OWNER}),
    "ledger.manage": frozenset({BOARD, MANAGER}),
    "budget.manage": frozenset({BOARD}),
    "collections.manage": frozenset({BOARD, MANAGER}),
    "collections.refer": frozenset({BOARD}),
    "resale.request": frozenset({OWNER, BOARD, MANAGER}),
    "resale.manage": frozenset({BOARD, MANAGER}),
    "reports.view": frozenset({BOARD, AUDITOR, MANAGER}),
}

# Role groups that require MFA... every account requires it (decision D5);
# kept explicit for the coverage check below.
PRIVILEGED_GROUPS = frozenset({BOARD, ARC_REVIEWER, AUDITOR, SYSADMIN, MANAGER})


class _System:
    """Actor for trusted internal callers (bootstrap, seed data, migrations).

    Never available to a request: views always pass ``request.user``.
    """

    def __repr__(self) -> str:
        return "SYSTEM"


SYSTEM = _System()


@dataclass(frozen=True)
class Roles:
    groups: frozenset[str] = frozenset()
    owned_lot_ids: frozenset[int] = frozenset()
    person_id: int | None = None
    offices: frozenset[str] = field(default_factory=frozenset)
    # (lot_id, start date of the current ownership) for each owned lot, so lot
    # records from before this owner's time can be hidden (review A4).
    owned_since: tuple[tuple[int, datetime.date], ...] = ()

    def has(self, group: str) -> bool:
        return group in self.groups


NO_ROLES = Roles()


def _preview_roles(preview, on) -> Roles:
    """Homeowner view (core.preview): the owner of one lot's permissions and
    scope, with the viewer's own person (no impersonation of the owner)."""
    from community.models import Ownership, Person
    from core.scoping import system_access

    with system_access("policy: owner preview"):
        person_id = (
            Person.objects.filter(user_id=preview.user_id).values_list("pk", flat=True).first()
        )
        starts = list(
            Ownership.objects.filter(lot_id=preview.lot_id)
            .active(on)
            .values_list("start_date", flat=True)
        )
    since = min(starts) if starts else (on or datetime.date.today())
    return Roles(
        frozenset({OWNER}),
        frozenset({preview.lot_id}),
        person_id,
        frozenset(),
        ((preview.lot_id, since),),
    )


def roles_for(user, on: datetime.date | None = None) -> Roles:
    from community.models import BoardSeat, OfficeHolding, Ownership, Person, RoleAssignment
    from core.scoping import system_access

    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return NO_ROLES
    from core.preview import current_for

    preview = current_for(user)
    if preview is not None:
        return _preview_roles(preview, on)
    with system_access("policy: compute roles"):
        person = Person.objects.filter(user=user).only("pk").first()
        if person is None:
            return NO_ROLES
        lots = frozenset(
            Ownership.objects.filter(person=person).active(on).values_list("lot_id", flat=True)
        )
        since = _continuous_ownership_starts(person, lots)
        groups = set()
        if lots:
            groups.add(OWNER)
        if BoardSeat.objects.filter(person=person).active(on).exists():
            groups.add(BOARD)
        groups.update(
            RoleAssignment.objects.filter(person=person).active(on).values_list("role", flat=True)
        )
        offices = frozenset(
            OfficeHolding.objects.filter(person=person).active(on).values_list("office", flat=True)
        )
    return Roles(frozenset(groups), lots, person.pk, offices, tuple(sorted(since.items())))


def _continuous_ownership_starts(person, lots) -> dict[int, datetime.date]:
    """For each currently owned lot, when this person's *unbroken* ownership began.

    Re-recording an ownership (adding a spouse, a deed into a trust) ends one
    record and starts another the same day; the owner's history must not
    disappear because of it (re-audit of the 2026-11 review).
    """
    from community.models import Ownership

    since: dict[int, datetime.date] = {}
    if not lots:
        return since
    periods: dict[int, list[tuple[datetime.date, datetime.date | None]]] = {}
    for lot_id, start, end in Ownership.objects.filter(person=person, lot_id__in=lots).values_list(
        "lot_id", "start_date", "end_date"
    ):
        periods.setdefault(lot_id, []).append((start, end))
    for lot_id, spans in periods.items():
        spans.sort(key=lambda p: p[0], reverse=True)  # newest first
        start = spans[0][0]
        for earlier_start, earlier_end in spans[1:]:
            if earlier_end is None or earlier_end >= start - datetime.timedelta(days=1):
                start = min(start, earlier_start)
            else:
                break
        since[lot_id] = start
    return since


def can(user, action: str, on: datetime.date | None = None) -> bool:
    if action not in PERMISSIONS:
        raise KeyError(f"Unknown action {action!r}; add it to core.policy.PERMISSIONS")
    if user is SYSTEM:
        return True
    return bool(PERMISSIONS[action] & roles_for(user, on).groups)


def require(user, action: str) -> None:
    """Raise PermissionDenied (HTTP 403) unless ``user`` may perform ``action``."""
    if not can(user, action):
        raise PermissionDenied(action)


def is_sysadmin(user) -> bool:
    return user is not SYSTEM and roles_for(user).has(SYSADMIN)


def people_able_to(action: str) -> set[int]:
    """Person IDs who can currently perform ``action``."""
    from community.models import BoardSeat, Ownership, RoleAssignment
    from core.scoping import system_access

    groups = PERMISSIONS[action]
    people: set[int] = set()
    with system_access("policy: coverage"):
        if BOARD in groups:
            people.update(BoardSeat.objects.active().values_list("person_id", flat=True))
        if OWNER in groups:
            people.update(Ownership.objects.active().values_list("person_id", flat=True))
        role_groups = groups & {ARC_REVIEWER, AUDITOR, SYSADMIN, MANAGER}
        if role_groups:
            people.update(
                RoleAssignment.objects.active()
                .filter(role__in=role_groups)
                .values_list("person_id", flat=True)
            )
    return people


def thinly_covered_actions(minimum: int = 2) -> dict[str, int]:
    """Actions fewer than ``minimum`` people can perform (decision D4 warning).

    Owner-only actions are excluded: they concern one's own lot. From step 5
    on, only people with MFA enrolled count.
    """
    from core.policy_mfa import people_with_mfa

    with_mfa = people_with_mfa()
    result = {}
    for action, groups in PERMISSIONS.items():
        if groups == {OWNER} or OWNER in groups:
            continue  # owner actions concern one's own lot
        able = people_able_to(action)
        if with_mfa is not None:
            able &= with_mfa
        if len(able) < minimum:
            result[action] = len(able)
    return result
