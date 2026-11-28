"""The ``oak_creek`` demo, part two: records in every state, for clicking
around and for testing the portal by hand (run by seed_demo_operations).

The first part (``oak_creek`` and ``seed_demo_operations``) creates the
community and a few records per area. This adds, through the same services
the pages use, at least one record waiting at each step a person can take
next, the finished states (approved, denied, closed, cancelled, voided...),
and a few untouched "fresh" ones so a tester can repeat a flow after a
reseed. Who does each step follows the services' own rules: a different
board member approves, a recused reviewer stays out, nobody acts on their
own home's matter with board powers (D65).

Test personas (``PERSONAS``) are logins with no password and no
authenticator, whose email is the one on their Person record. A tester gets
in as one the way a real person would: a board member sends a new setup link
from Homes and owners -> Sign-ups (D59; on the POC every email arrives in the
EMAIL_REDIRECT_ALL_TO inbox), then the tester sets a password and an
authenticator app. Nothing here weakens sign-in. See docs/demo-test-guide.md.

Votes, elections and surveys are cast only through their public services
(``cast_ballot``, ``cast_election_ballot``, ``submit_response``).

A few things cannot be shown by services alone, because the services stamp
"now": an invitation that has expired, and records requests due soon or
past their due date. For those the demo moves a timestamp on the record
afterwards (``_backdate``, mutable tables only, never an
append-only one). Online payments and automatic payments are not seeded:
they need Stripe (pull only), which a demo has no key for.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from community import services as community
from community.models import Account, BoardSeat, Lot, Office, OfficeHolding, Ownership, Person
from core import pdf
from core.policy import SYSTEM
from core.scoping import system_access


@dataclass(frozen=True)
class Persona:
    key: str
    who: str  # what the tester signs in as
    lot: int | None  # the home they own or are attached to (None: no home)
    try_this: str  # the first thing worth trying


# The lots the people below come from are fixed by community/demo/oak_creek.py.
PERSONAS = [
    Persona("president", "Board president, owns a home", 2, "Approve invoice GL-60 (second step)."),
    Persona(
        "vice_president",
        "Board vice president with own matters (own request, ARC, refund, contact change)",
        5,
        "Open your own refund or ARC request: the board buttons are replaced by a note.",
    ),
    Persona("treasurer", "Board treasurer", 11, "Approve the refund waiting for a board member."),
    Persona("secretary", "Board secretary", 18, "Approve the minutes of the held meeting."),
    Persona("director", "Board member without an office", 29, "Sign the open written consent."),
    Persona("manager", "Community manager (staff, never approves)", None, "Start a work order."),
    Persona("arc_reviewer", "ARC reviewer, recused next door", 14, "Open Screened porch."),
    Persona("arc_reviewer_2", "ARC reviewer", 38, "Record a position on a request in review."),
    Persona("auditor", "Auditor (read-only)", None, "Finance reports; no buttons anywhere."),
    Persona("sysadmin", "System administrator (operations pages only)", None, "System status."),
    Persona("owner", "Owner (single)", 3, "Vote in the open formal vote; answer the poll."),
    Persona("owner_b", "Owner of another home (cross-owner checks)", 12, "Your own requests."),
    Persona("joint_a", "Joint owner", 7, "Your ARC request was denied: appeal it."),
    Persona("joint_b", "The other joint owner (sign-in reset waiting)", 7, "My home."),
    Persona("owner_arc", "Owner with an ARC request in review", 15, "Architectural review."),
    Persona("owner_late", "Owner behind on assessments (collection case)", 27, "My account."),
    Persona("authorized", "Authorized contact (not on the deed)", 3, "Raise a request."),
    Persona("view_only", "View-only contact", 3, "See the account; no buttons."),
    Persona("tenant", "Tenant", 40, "Community pages and own requests; no balance."),
    Persona("former_owner", "Former owner (sold the home 90 days ago)", 31, "My history only."),
]


@dataclass
class Ctx:
    t: datetime.date
    now: datetime.datetime
    lots: dict[int, Lot]
    users: dict[str, object] = field(default_factory=dict)
    people: dict[str, Person] = field(default_factory=dict)
    board: list = field(default_factory=list)

    def u(self, key):
        return self.users[key]

    def home(self, key) -> int:
        """The number of the home a persona owns (the vice president's is 5,
        unless bootstrap_first_officer ran before the demo)."""
        return (
            Ownership.objects.active()
            .filter(person=self.people[key])
            .values_list("lot__number", flat=True)
            .first()
        )

    def days(self, n: int) -> datetime.date:
        return self.t + datetime.timedelta(days=n)

    def at(self, days: int, hour: int = 19) -> datetime.datetime:
        day = self.days(days)
        tz = timezone.get_current_timezone()
        return datetime.datetime.combine(day, datetime.time(hour, 0), tzinfo=tz)


def _pdf_upload(name: str, heading: str) -> SimpleUploadedFile:
    data = pdf.render([("heading", f"{heading} (FICTIONAL DEMO)"), ("normal", "Placeholder.")])
    return SimpleUploadedFile(name, data, content_type="application/pdf")


def _backdate(model, pk: int, **fields) -> None:
    """Move a timestamp the service stamped "now" (mutable tables only)."""
    from core.append_only import AppendOnlyModel

    if issubclass(model, AppendOnlyModel):
        raise TypeError(f"{model.__name__} is append-only")
    model._base_manager.filter(pk=pk).update(**fields)


def login_for(person: Person):
    """A login without a password or authenticator, on the person's own email
    (so the board can send a new setup link, D59)."""
    from accounts.models import User

    if person.user_id:
        return person.user
    user = User.objects.create_user(person.email)
    community.link_login(person, user, actor=SYSTEM)
    person.refresh_from_db()
    return user


def owner_of(lot_number: int, index: int = 0) -> Person:
    owners = (
        Ownership.objects.active()
        .filter(lot__number=lot_number)
        .select_related("person")
        .order_by("start_date", "pk")
    )
    return list(owners)[index].person


def current_account(lot: Lot) -> Account:
    return Account.objects.get(lot=lot, closed_on__isnull=True)


# Area modules in this package, each with seed(c), run in this order.
AREAS = ("requests", "arc", "violations", "operations", "governance", "ledger", "comms")


def seed() -> None:
    import importlib

    with system_access("seed_demo states"):
        t = timezone.localdate()
        c = Ctx(t=t, now=timezone.now(), lots={lot.number: lot for lot in Lot.objects.all()})
        _personas(c)
        _features(c)
        _people_and_access(c)
        for area in AREAS:
            importlib.import_module(f"{__name__}.{area}").seed(c)


def staff(c: Ctx, matter, *, decide: bool = False):
    """A board member (or, for data entry, the manager first) for whom
    ``matter`` is not their own (D65) and who is not recused from it."""
    from arc.models import ArcRequest
    from arc.services import is_recused
    from core import conflicts

    pool = list(c.board) if decide else [c.u("manager"), *c.board]
    for user in pool:
        if conflicts.own_matter(user, matter):
            continue
        if isinstance(matter, ArcRequest) and is_recused(user, matter):
            continue
        return user
    raise AssertionError("no board member is free to act on this demo record")


# --- Personas ----------------------------------------------------------------------------


def _office_person(office: str) -> Person:
    return OfficeHolding.objects.active().get(office=office).person


def _personas(c: Ctx) -> None:
    from community.models import AccountContact, Role, RoleAssignment

    for key, office in (
        ("president", Office.PRESIDENT),
        ("vice_president", Office.VICE_PRESIDENT),
        ("treasurer", Office.TREASURER),
        ("secretary", Office.SECRETARY),
    ):
        c.people[key] = _office_person(office)
    officers = {p.pk for p in c.people.values()}
    c.people["director"] = next(
        s.person
        for s in BoardSeat.objects.active().select_related("person").order_by("pk")
        if s.person_id not in officers
    )
    c.people["manager"] = community.record_person(
        display_name="Mel Manager (demo)", email="demo-manager@example.com", actor=SYSTEM
    )
    community.grant_role(person=c.people["manager"], role=Role.MANAGER, actor=SYSTEM)
    arc_people = [
        r.person
        for r in RoleAssignment.objects.active()
        .filter(role=Role.ARC_REVIEWER)
        .select_related("person")
    ]
    by_lot = {
        o.lot.number: o.person
        for o in Ownership.objects.active().filter(person__in=arc_people).select_related("lot")
    }
    c.people["arc_reviewer"], c.people["arc_reviewer_2"] = by_lot[14], by_lot[38]
    c.people["auditor"] = (
        RoleAssignment.objects.active().filter(role=Role.AUDITOR).select_related("person")[0].person
    )
    c.people["sysadmin"] = community.record_person(
        display_name="Sid Sysadmin (demo)", email="demo-sysadmin@example.com", actor=SYSTEM
    )
    for key, lot, index in (
        ("owner", 3, 0),
        ("owner_b", 12, 0),
        ("joint_a", 7, 0),
        ("joint_b", 7, 1),
        ("owner_arc", 15, 0),
        ("owner_late", 27, 0),
    ):
        c.people[key] = owner_of(lot, index)
    contacts = AccountContact.objects.active().select_related("person", "account__lot")
    for contact in contacts:
        key = {"authorized": "authorized", "view_only": "view_only", "tenant": "tenant"}[
            contact.kind
        ]
        c.people[key] = contact.person
    seller = (
        Ownership.objects.filter(lot__number=31, end_date__isnull=False)
        .select_related("person")
        .first()
    )
    c.people["former_owner"] = seller.person
    for persona in PERSONAS:
        c.users[persona.key] = login_for(c.people[persona.key])
    _mark_personas(c)
    for p in ("president", "vice_president", "treasurer", "secretary", "director"):
        c.board.append(c.users[p])
    # Sysadmin: nominated by one board member, approved by another.
    nomination = community.nominate_sysadmin(person=c.people["sysadmin"], actor=c.u("president"))
    community.approve_sysadmin(nomination, actor=c.u("treasurer"))


def _mark_personas(c: Ctx) -> None:
    """Demo mode (D97): mark each persona's login so a board member on the POC
    can view the portal as it. A login that already has a way in (a real
    officer's, made before the demo) is never marked."""
    from core.demo_mode import DemoModeError, mark_persona

    for position, persona in enumerate(PERSONAS):
        try:
            mark_persona(
                c.users[persona.key], key=persona.key, label=persona.who, position=position
            )
        except DemoModeError:
            continue


def _features(c: Ctx) -> None:
    from core import features

    # Optional features with demo data that need no outside service.
    for key in ("reservations", "bank_feeds"):
        features.set_feature(key, True, actor=SYSTEM)


# --- People, sign-in and access ----------------------------------------------------------


def _people_and_access(c: Ctx) -> None:
    from accounts import mfa_reset, setup_requests
    from accounts import services as accounts
    from accounts.models import Invitation
    from community.models import SeatType

    pres, vp, treas = c.u("president"), c.u("vice_president"), c.u("treasurer")
    # Invitations: pending (fresh), expired and revoked. None accepted: an
    # accepted one sets a password, and demo logins never have one.
    invited = {n: owner_of(n) for n in (20, 21, 22, 25)}

    def invite(n):
        # Sent at once (on the POC it lands in the EMAIL_REDIRECT_ALL_TO inbox).
        invitation = accounts.create_invitation(person=invited[n], actor=pres, send_email=False)
        accounts.send_invitation_email(invitation.pk)
        invitation.refresh_from_db()
        return invitation

    for n in (20, 25):
        invite(n)
    expired = invite(21)
    _backdate(
        Invitation,
        expired.pk,
        created_at=c.now - datetime.timedelta(days=10),
        expires_at=c.now - datetime.timedelta(days=3),
    )
    accounts.revoke_invitation(invite(22), actor=vp)

    # Sign-in resets (D5): one waiting for a second board member, one
    # approved (code not yet used), one cancelled.
    mfa_reset.start_reset(
        subject=c.u("joint_b"), verification_method="phone_known_number", actor=pres
    )
    approved = mfa_reset.start_reset(
        subject=c.u("owner_late"), verification_method="in_person", actor=vp
    )
    mfa_reset.approve_reset(approved, actor=treas)
    cancelled = mfa_reset.start_reset(
        subject=c.u("owner_arc"), verification_method="video_call", actor=treas
    )
    mfa_reset.cancel_reset(cancelled, actor=treas)

    # "Ask the board" for a new setup link (D62), from a login that never set up.
    setup_requests.record_request(c.u("tenant").pk)

    # Sysadmin nominations: one waiting, one declined.
    community.nominate_sysadmin(
        person=community.record_person(
            display_name="Ira Volunteer (demo)", email="demo-it-volunteer@example.com", actor=SYSTEM
        ),
        actor=pres,
    )
    declined = community.nominate_sysadmin(person=owner_of(33), actor=vp)
    community.decline_sysadmin(declined, actor=treas)

    # Board seat nominations: one waiting, one declined. Only where a seat
    # needs a second approval (BOARD_SEAT_SECOND_APPROVAL: on in production
    # and tests, off by default on a POC, where a proposal records the seat).
    if community.seat_needs_second_approval(pres):
        community.propose_board_seat(
            person=owner_of(34),
            seat_type=SeatType.OWNER_ELECTED,
            term_start=c.t,
            term_end=c.days(730),
            citation="Bylaws §3.2 (demo)",
            actor=pres,
        )
        _, nom = community.propose_board_seat(
            person=owner_of(35),
            seat_type=SeatType.OWNER_ELECTED,
            term_start=c.t,
            term_end=c.days(730),
            citation="Bylaws §3.2 (demo)",
            actor=vp,
        )
        community.decide_seat_nomination(nom, approve=False, actor=treas)

    # Account contacts: one ended (a former tenant), in addition to the three current.
    old_tenant = community.add_account_contact(
        current_account(c.lots[41]),
        person=community.record_person(
            display_name="Toby Formertenant (demo)",
            email="demo-old-tenant@example.com",
            actor=SYSTEM,
        ),
        kind="tenant",
        actor=SYSTEM,
    )
    community.end_account_contact(old_tenant, actor=SYSTEM)

    # Owners' official-notice choices (D44): a few by email, one by post.
    for key in ("owner", "owner_b", "joint_a", "president"):
        community.record_notice_choice(user=c.u(key), consent=True)
    community.record_notice_choice(user=c.u("owner_arc"), consent=False)

    _account_changes(c)


def _account_changes(c: Ctx) -> None:
    owner, owner_b, joint_a, vp = (
        c.u("owner"),
        c.u("owner_b"),
        c.u("joint_a"),
        c.u("vice_president"),
    )
    acct3, acct12 = current_account(c.lots[3]), current_account(c.lots[12])
    acct7, acct5 = current_account(c.lots[7]), current_account(c.lots[c.home("vice_president")])

    def add(account, actor, name, kind="authorized", email="", reason=""):
        return community.request_account_change(
            account,
            action="add",
            kind=kind,
            name=name,
            email=email,
            no_login=not email,
            reason=reason,
            actor=actor,
        )

    # Waiting for the board (two fresh ones to approve or decline).
    add(
        acct3,
        owner,
        "Robin Helper (demo)",
        "authorized",
        "demo-helper@example.com",
        "My sister helps with the bills.",
    )
    add(acct12, owner_b, "Pat Renter (demo)", "tenant", "", "New tenant from next month.")
    # Approved (the accountant is invited), declined, and a withdrawn one.
    approved = add(
        acct12,
        owner_b,
        "Casey Accountant (demo)",
        "view_only",
        "demo-accountant@example.com",
        "Our accountant.",
    )
    community.decide_account_change(
        approved, approve=True, note="OK (demo)", actor=c.u("president")
    )
    declined = add(acct3, owner, "Unknown Person (demo)", "authorized", "", "")
    community.decide_account_change(
        declined, approve=False, note="We need to talk to you first (demo).", actor=c.u("treasurer")
    )
    withdrawn = add(acct7, joint_a, "Dana Neighbour (demo)", "view_only", "", "")
    community.withdraw_account_change(withdrawn, actor=joint_a)
    # Deed changes waiting: add an owner (trust) on 12, remove one of 7's owners.
    community.request_account_change(
        acct12,
        action="add_owner",
        name="Morgan Applewood Family Trust (demo)",
        no_login=True,
        ownership_type="trust",
        deed_date=c.days(-14),
        deed=_pdf_upload("deed.pdf", "Recorded deed"),
        reason="Moved the house into our trust.",
        actor=owner_b,
    )
    joint_b_ownership = Ownership.objects.active().get(lot=c.lots[7], person=c.people["joint_b"])
    community.request_account_change(
        acct7,
        action="remove_owner",
        ownership=joint_b_ownership,
        deed_date=c.days(-10),
        deed=_pdf_upload("deed.pdf", "Recorded deed"),
        reason="Deed now in one name.",
        actor=joint_a,
    )
    # Own matter: a board member's own request (another board member decides).
    add(acct5, vp, "Jordan Sitter (demo)", "authorized", "", "House sitter while we travel.")
