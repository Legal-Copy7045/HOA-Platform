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

import contextlib
import datetime
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.utils import timezone

OWNER = "owner"
BOARD = "board"
ARC_REVIEWER = "arc_reviewer"
AUDITOR = "auditor"
SYSADMIN = "sysadmin"
MANAGER = "manager"  # management-company staff: data entry, never approvals or votes
# People on an account who are not on the deed (Milestone 3 accounts).
AUTHORIZED = "authorized_contact"  # sees the account, raises requests
VIEW_ONLY = "view_only_contact"  # sees the account and notices, cannot act
TENANT = "tenant"  # community documents and news, their own requests
FORMER_OWNER = "former_owner"  # read-only: their own records from their time
COMMITTEE_MEMBER = "committee_member"  # sees and works in their own committees' spaces
ACCOUNT_VIEWERS = frozenset({OWNER, AUTHORIZED, VIEW_ONLY})
RESIDENTS = ACCOUNT_VIEWERS | {TENANT}
# Groups whose actions concern only their own lot or records.
SELF_SERVICE = RESIDENTS | {FORMER_OWNER}

MEMBERS = frozenset({OWNER, BOARD, ARC_REVIEWER, AUDITOR, MANAGER})
# Community pages (news, calendar, documents, meetings, policies).
COMMUNITY = MEMBERS | RESIDENTS

# action -> role groups allowed to perform it. Keep this the only such table.
PERMISSIONS: dict[str, frozenset[str]] = {
    # Reading
    "lot.view_own": ACCOUNT_VIEWERS,
    "lot.view_all": frozenset({BOARD, AUDITOR, MANAGER}),
    # Preview as owner: see the portal as one lot's owner sees it (read-only).
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
    "announcement.view": COMMUNITY,
    "announcement.publish": frozenset({BOARD, MANAGER}),
    "faq.manage": frozenset({BOARD, MANAGER}),
    "calendar.view": COMMUNITY,
    "calendar.manage": frozenset({BOARD, MANAGER}),
    "task.manage": frozenset({BOARD, MANAGER}),
    # Documents
    "document.view_owner": COMMUNITY,
    "document.view_board": frozenset({BOARD, AUDITOR, MANAGER}),
    "document.manage": frozenset({BOARD, MANAGER}),
    # Governance
    "meeting.view": COMMUNITY,
    "meeting.manage": frozenset({BOARD, MANAGER}),
    "resolution.view": COMMUNITY,
    "resolution.record": frozenset({BOARD}),
    "records_request.submit": frozenset({OWNER}),
    "records_request.manage": frozenset({BOARD, MANAGER}),
    "vote.view": MEMBERS | {AUTHORIZED, VIEW_ONLY},
    "vote.cast": frozenset({OWNER}),
    "vote.manage": frozenset({BOARD}),
    # Architectural review
    "arc.submit": frozenset({OWNER}),
    "arc.view_all": frozenset({BOARD, ARC_REVIEWER, MANAGER, AUDITOR}),
    "arc.intake": frozenset({BOARD, ARC_REVIEWER, MANAGER}),
    "arc.decide": frozenset({ARC_REVIEWER}),
    "arc.appeal_decide": frozenset({BOARD}),
    "arc.configure": frozenset({BOARD}),
    # Compliance
    "violation.view_all": frozenset({BOARD, MANAGER, AUDITOR}),
    "violation.manage": frozenset({BOARD, MANAGER}),
    "violation.decide": frozenset({BOARD}),
    # Operations and money
    "vendor.manage": frozenset({BOARD, MANAGER}),
    # Owner requests: owners, authorized contacts and tenants raise them
    # (view-only contacts and former owners cannot).
    "request.submit": frozenset({OWNER, AUTHORIZED, TENANT}),
    "request.view_own": RESIDENTS | {FORMER_OWNER},
    "request.manage": frozenset({BOARD, MANAGER}),
    "followup.view": frozenset({BOARD, MANAGER}),
    "workorder.manage": frozenset({BOARD, MANAGER}),
    "invoice.enter": frozenset({BOARD, MANAGER}),
    "contract.manage": frozenset({BOARD, MANAGER}),
    "compliance.manage": frozenset({BOARD, MANAGER}),
    "invoice.approve": frozenset({BOARD}),
    "invoice.record_payment": frozenset({BOARD, MANAGER}),
    "finance.view": frozenset({BOARD, AUDITOR, MANAGER}),
    "ledger.view_own": ACCOUNT_VIEWERS,
    "history.view_own": frozenset({FORMER_OWNER}),
    "account.manage": frozenset({BOARD, MANAGER}),
    "roster.view": frozenset({BOARD, MANAGER, AUDITOR}),
    # Owners (on the deed) ask to add or remove people; the board approves.
    "account.request_change": frozenset({OWNER}),
    "account.approve_change": frozenset({BOARD}),
    # Committees: the board creates them and appoints members; members work
    # in their own committee's space (checked per committee in governance).
    "committee.view": frozenset({BOARD, MANAGER, AUDITOR, COMMITTEE_MEMBER}),
    "committee.view_all": frozenset({BOARD, MANAGER, AUDITOR}),
    "committee.manage": frozenset({BOARD}),
    "ledger.manage": frozenset({BOARD, MANAGER}),
    # Charging a late fee is a board decision (the manager prepares the list).
    "late_fee.post": frozenset({BOARD}),
    # Letter wording, letterhead and resale certificate facts: the board.
    "letters.manage": frozenset({BOARD}),
    # Switching optional features on and off: the board.
    "features.manage": frozenset({BOARD}),
    # Deposits, reconciliation, chart of accounts: data entry (manager) or board.
    "accounting.manage": frozenset({BOARD, MANAGER}),
    "budget.manage": frozenset({BOARD}),
    "budget.view_owner": ACCOUNT_VIEWERS | {BOARD, MANAGER, AUDITOR},
    "collections.manage": frozenset({BOARD, MANAGER}),
    "collections.refer": frozenset({BOARD}),
    "resale.request": frozenset({OWNER, BOARD, MANAGER}),
    "resale.manage": frozenset({BOARD, MANAGER}),
    "reports.view": frozenset({BOARD, AUDITOR, MANAGER}),
    # Text alerts (Milestone 4): owners opt in; the board sends urgent notices.
    "sms.opt_in": frozenset({OWNER}),
    "sms.send_urgent": frozenset({BOARD}),
    # --- Reservations, access review, retention (Milestone 4) ---
    # Common-area bookings: residents request (per-area rules decide whether
    # authorized contacts and tenants may); board or manager confirm.
    "reservation.view": COMMUNITY,
    "reservation.request": frozenset({OWNER, AUTHORIZED, TENANT}),
    "reservation.manage": frozenset({BOARD, MANAGER}),
    "reservation.configure": frozenset({BOARD}),
    # Quarterly review of every login's access: the board only (sysadmins get
    # operations pages, never owner data).
    "access_review.record": frozenset({BOARD}),
    # Clearing former owners' contact details under the retention rule.
    "retention.manage": frozenset({BOARD}),
    # --- Meeting packets (M5) ---
    # Assembling a packet is meeting preparation, like meeting.manage and
    # document.manage, so the manager may build one (it approves nothing).
    "meeting.packet_build": frozenset({BOARD, MANAGER}),
    # --- Surveys (M5): informal, non-binding opinion surveys ---
    # The survey's own audience setting narrows who sees and answers each one
    # (owners only, or owners and account contacts including tenants).
    "survey.view": COMMUNITY,
    "survey.respond": RESIDENTS,
    "survey.manage": frozenset({BOARD}),
    # --- Event RSVPs and volunteer sign-ups (rsvp) ---
    # Residents who can see an event answer it (view-only contacts and former
    # owners cannot); board or manager set it up; only the board sees names.
    "event.rsvp": frozenset({OWNER, AUTHORIZED, TENANT}),
    "event.rsvp_manage": frozenset({BOARD, MANAGER}),
    "event.rsvp_names": frozenset({BOARD}),
    # --- Reserve plan (reserves) ---
    # The board, manager and auditor see the plan; changing or adopting it
    # is a board decision. Owners see the adopted plan's summary only.
    "reserves.view": frozenset({BOARD, AUDITOR, MANAGER}),
    "reserves.manage": frozenset({BOARD}),
    "reserves.view_summary": ACCOUNT_VIEWERS | {BOARD, MANAGER, AUDITOR},
    # --- Bank feed (bankfeeds, optional feature "bank_feeds") ---
    # Seeing the bank lines: finance readers. Connecting a bank or ending a
    # connection: the board only. Syncing, importing statement files and
    # matching lines to the cash book: data entry (manager) or board.
    "bank_feed.view": frozenset({BOARD, MANAGER, AUDITOR}),
    "bank_feed.connect": frozenset({BOARD}),
    "bank_feed.sync": frozenset({BOARD, MANAGER}),
    "bank_feed.import": frozenset({BOARD, MANAGER}),
    "bank_feed.match": frozenset({BOARD, MANAGER}),
    # --- Online payments (D32-payments) ---
    # Owners on the deed and authorized contacts pay their own account; never
    # view-only contacts, tenants, former owners or (as such) the manager.
    "payments.pay": frozenset({OWNER, AUTHORIZED}),
    # Closing online-payment problems (mismatches, refunds, disputes): board.
    "payments.manage": frozenset({BOARD}),
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
    # Lots whose account this person may see: owners plus authorized and
    # view-only contacts (tenants see community pages only).
    account_lot_ids: frozenset[int] = frozenset()
    # (lot_id, since) for account contacts: the account's opening date.
    contact_since: tuple[tuple[int, datetime.date], ...] = ()
    tenant_lot_ids: frozenset[int] = frozenset()
    # Lots where this person is an authorized contact (may act for the
    # account); view-only contacts are in account_lot_ids but not here.
    authorized_lot_ids: frozenset[int] = frozenset()
    # The login these roles belong to (None in the homeowner view).
    user_id: int | None = None
    committee_ids: frozenset[int] = frozenset()
    chair_committee_ids: frozenset[int] = frozenset()
    # Former owners: (lot_id, start, end) of each past ownership period, and
    # the dormant accounts they were on (read-only history).
    former_periods: tuple[tuple[int, datetime.date, datetime.date], ...] = ()
    former_account_ids: frozenset[int] = frozenset()

    @property
    def lot_since(self) -> dict[int, datetime.date]:
        """When this person's view of each account lot begins."""
        return {**dict(self.contact_since), **dict(self.owned_since)}

    def has(self, group: str) -> bool:
        return group in self.groups


NO_ROLES = Roles()


def _preview_roles(preview, on) -> Roles:
    """Preview as owner (core.preview): the owner of one lot's permissions and
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
    since = min(starts) if starts else (on or timezone.localdate())
    return Roles(
        frozenset({OWNER}),
        frozenset({preview.lot_id}),
        person_id,
        frozenset(),
        ((preview.lot_id, since),),
        account_lot_ids=frozenset({preview.lot_id}),
    )


# Roles are remembered on the user object (one per request) and recomputed
# whenever anything that grants a role changes in this process: the
# generation below is bumped by signals on those models and by the few bulk
# updates that bypass signals (optimization review A1: 500+ queries a page).
_ROLES_GENERATION = [0]


def invalidate_roles(*args, **kwargs) -> None:
    _ROLES_GENERATION[0] += 1


def roles_for(user, on: datetime.date | None = None) -> Roles:
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return NO_ROLES
    from core.preview import current_for

    preview = current_for(user)
    if preview is not None:
        return _preview_roles(preview, on)
    if on is not None:
        return _compute_roles(user, on)
    cached = getattr(user, "_roles_cache", None)
    generation = (_ROLES_GENERATION[0], timezone.localdate())
    if cached is not None and cached[0] == generation:
        return cached[1]
    roles = _compute_roles(user, None)
    with contextlib.suppress(AttributeError):
        user._roles_cache = (generation, roles)
    return roles


def _compute_roles(user, on) -> Roles:
    from community.models import BoardSeat, OfficeHolding, Ownership, Person, RoleAssignment
    from core.scoping import system_access

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
        contacts = _account_contacts(person, on)
        former = _former_ownerships(person, lots | frozenset(contacts["account"]), on)
        committees = _committees(person, on) if (lots or BOARD in groups) else {}
    if contacts["authorized"]:
        groups.add(AUTHORIZED)
    if contacts["view_only"]:
        groups.add(VIEW_ONLY)
    if contacts["tenant"]:
        groups.add(TENANT)
    if former["periods"]:
        groups.add(FORMER_OWNER)
    if committees:
        groups.add(COMMITTEE_MEMBER)
    contact_since = {k: v for k, v in contacts["since"].items() if k not in lots}
    return Roles(
        frozenset(groups),
        lots,
        person.pk,
        offices,
        tuple(sorted(since.items())),
        account_lot_ids=lots | frozenset(contacts["account"]),
        contact_since=tuple(sorted(contact_since.items())),
        tenant_lot_ids=frozenset(contacts["tenant"]),
        authorized_lot_ids=frozenset(contacts["authorized"]),
        former_periods=tuple(sorted(former["periods"])),
        former_account_ids=frozenset(former["accounts"]),
        user_id=user.pk,
        committee_ids=frozenset(committees),
        chair_committee_ids=frozenset(k for k, v in committees.items() if v == "chair"),
    )


def _committees(person, on) -> dict[int, str]:
    """Active committee memberships: committee id -> role. Only counted while
    the person owns a lot or sits on the board (a seller leaves with the lot)."""
    from governance.models import CommitteeMembership

    rows = (
        CommitteeMembership.objects.filter(person=person, committee__active=True)
        .active(on)
        .values_list("committee_id", "role")
    )
    return dict(rows)


def _account_contacts(person, on) -> dict:
    """Lots where this person is an account contact or tenant, by kind."""
    from community.models import AccountContact, ContactKind

    out = {"authorized": set(), "view_only": set(), "tenant": set(), "account": set(), "since": {}}
    rows = (
        AccountContact.objects.filter(person=person)
        .active(on)
        .filter(account__closed_on__isnull=True)
        .values_list("kind", "account__lot_id", "account__opened_on")
    )
    for kind, lot_id, opened_on in rows:
        if kind == ContactKind.TENANT:
            out["tenant"].add(lot_id)
            continue
        out["authorized" if kind == ContactKind.AUTHORIZED else "view_only"].add(lot_id)
        out["account"].add(lot_id)
        out["since"][lot_id] = min(opened_on, out["since"].get(lot_id, opened_on))
    return out


def _former_ownerships(person, current_lots, on) -> dict:
    """Ended ownerships of lots this person no longer sees: read-only history."""
    from community.models import Ownership

    on = on or timezone.localdate()
    periods, accounts = set(), set()
    for lot_id, start, end, account_id in Ownership.objects.filter(
        person=person, end_date__isnull=False, end_date__lte=on
    ).values_list("lot_id", "start_date", "end_date", "account_id"):
        if lot_id in current_lots:
            continue
        periods.add((lot_id, start, end))
        if account_id:
            accounts.add(account_id)
    return {"periods": periods, "accounts": accounts}


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


def _people_by_group() -> dict[str, set[int]]:
    """Everyone holding each group, in three queries (optimization A2)."""
    from community.models import BoardSeat, Ownership, RoleAssignment
    from core.scoping import system_access

    out: dict[str, set[int]] = {}
    with system_access("policy: coverage"):
        out[BOARD] = set(BoardSeat.objects.active().values_list("person_id", flat=True))
        out[OWNER] = set(Ownership.objects.active().values_list("person_id", flat=True))
        for role, person_id in (
            RoleAssignment.objects.active()
            .filter(role__in=[ARC_REVIEWER, AUDITOR, SYSADMIN, MANAGER])
            .values_list("role", "person_id")
        ):
            out.setdefault(role, set()).add(person_id)
    return out


def people_able_to(action: str, by_group: dict[str, set[int]] | None = None) -> set[int]:
    """Person IDs who can currently perform ``action``."""
    by_group = by_group if by_group is not None else _people_by_group()
    groups = PERMISSIONS[action] & {BOARD, OWNER, ARC_REVIEWER, AUDITOR, SYSADMIN, MANAGER}
    people: set[int] = set()
    for group in groups:
        people |= by_group.get(group, set())
    return people


def thinly_covered_actions(minimum: int = 2) -> dict[str, int]:
    """Actions fewer than ``minimum`` people can perform (decision D4 warning).

    Owner-only actions are excluded: they concern one's own lot. From step 5
    on, only people with MFA enrolled count.
    """
    from core.policy_mfa import people_with_mfa

    with_mfa = people_with_mfa()
    by_group = _people_by_group()
    result = {}
    for action, groups in PERMISSIONS.items():
        if groups & SELF_SERVICE:
            continue  # owner/resident actions concern one's own lot or records
        able = people_able_to(action, by_group)
        if with_mfa is not None:
            able &= with_mfa
        if len(able) < minimum:
            result[action] = len(able)
    return result
