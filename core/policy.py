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

# action -> role groups allowed to perform it. Keep this the only such table.
PERMISSIONS: dict[str, frozenset[str]] = {
    # Reading
    "lot.view_own": frozenset({OWNER}),
    "lot.view_all": frozenset({BOARD, AUDITOR}),
    "audit.view_sysadmin_activity": frozenset({BOARD, AUDITOR}),
    "system.view_status": frozenset({SYSADMIN}),
    # Community register
    "person.manage": frozenset({BOARD}),
    "ownership.manage": frozenset({BOARD}),
    "seat.manage": frozenset({BOARD}),
    "office.manage": frozenset({BOARD}),
    "role.arc_reviewer.manage": frozenset({BOARD}),
    "role.auditor.manage": frozenset({BOARD}),
    # Sysadmin: two different board members to grant, one to revoke (D4)
    "role.sysadmin.nominate": frozenset({BOARD}),
    "role.sysadmin.approve": frozenset({BOARD}),
    "role.sysadmin.revoke": frozenset({BOARD}),
    # Accounts
    "invite.send": frozenset({BOARD, SYSADMIN}),
    "invite.revoke": frozenset({BOARD, SYSADMIN}),
    "mfa_reset.start": frozenset({BOARD}),
    "mfa_reset.approve": frozenset({BOARD}),
}

# Role groups that require MFA... every account requires it (decision D5);
# kept explicit for the coverage check below.
PRIVILEGED_GROUPS = frozenset({BOARD, ARC_REVIEWER, AUDITOR, SYSADMIN})


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

    def has(self, group: str) -> bool:
        return group in self.groups


NO_ROLES = Roles()


def roles_for(user, on: datetime.date | None = None) -> Roles:
    from community.models import BoardSeat, OfficeHolding, Ownership, Person, RoleAssignment
    from core.scoping import system_access

    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return NO_ROLES
    with system_access("policy: compute roles"):
        person = Person.objects.filter(user=user).only("pk").first()
        if person is None:
            return NO_ROLES
        lots = frozenset(
            Ownership.objects.filter(person=person).active(on).values_list("lot_id", flat=True)
        )
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
    return Roles(frozenset(groups), lots, person.pk, offices)


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
        role_groups = groups & {ARC_REVIEWER, AUDITOR, SYSADMIN}
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
        if groups == {OWNER}:
            continue
        able = people_able_to(action)
        if with_mfa is not None:
            able &= with_mfa
        if len(able) < minimum:
            result[action] = len(able)
    return result
