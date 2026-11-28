"""The cast: the community's fictional people and how each of them behaves.

Built once at the start, after the dataset is seeded. The only direct writes
the simulation makes are here: a login (and an authenticator app, as every
real account has) for each fictional person, so the services have a real
``actor``. Everything else goes through the portal's services.

Each home gets a payer profile from the seeded RNG:

* ``on_time``: pays by check before the due day;
* ``online``: pays online (Stripe, card or bank) a few days after posting;
* ``autopay``: enrols in automatic payments in the first months;
* ``late``: pays most months, after the grace period (late fees, reminders);
* ``never``: stops paying (collection case, letters, legal, lien, payment plan).
"""

from __future__ import annotations

import datetime
import random
from dataclasses import dataclass, field

from core.policy import SYSTEM
from core.scoping import system_access

PROFILES = [("on_time", 18), ("online", 12), ("autopay", 6), ("late", 6), ("never", 2)]
FIRST = ["Ash", "Bay", "Cedar", "Dale", "Eden", "Fern", "Glen", "Hollis", "Ivy", "Jules"]
LAST = ["Brookline", "Copperleaf", "Driftwood", "Evergreen", "Fieldstone", "Graystone"]


@dataclass
class Home:
    lot_id: int
    number: int
    profile: str
    owners: list = field(default_factory=list)  # logins of today's owners (with an email)
    people: list = field(default_factory=list)  # today's owners (Person)

    @property
    def payer(self):
        return self.owners[0] if self.owners else None


@dataclass
class World:
    rng_seed: int
    homes: dict[int, Home] = field(default_factory=dict)  # by lot number
    board: list = field(default_factory=list)  # logins with an active seat, officers first
    manager: object = None
    arc_reviewers: list = field(default_factory=list)
    auditor: object = None
    sysadmin: object = None
    tenant_lots: set = field(default_factory=set)
    sold: list = field(default_factory=list)  # (lot number, date)
    bouncing: set = field(default_factory=set)  # owner emails that hard-bounce
    people_made: int = 0
    notes: dict = field(default_factory=dict)  # scenario bookkeeping (ids of records in play)

    # --- people ------------------------------------------------------------------------------

    def login(self, person):
        """The person's login (created once, with an authenticator app)."""
        from community.demo.states import login_for
        from community.tests.factories import enroll_totp

        if person.user_id:
            return person.user
        if not person.email:
            return None  # post only: such an owner pays by check, the manager records it
        with system_access("simulation cast"):
            user = login_for(person)
            enroll_totp(user)
        return user

    def new_person(self, label: str = ""):
        from community import services

        self.people_made += 1
        n = self.people_made
        name = f"{FIRST[n % len(FIRST)]} {LAST[(n * 5) % len(LAST)]} {n} (demo)"
        if label:
            name = f"{label} {n} (demo)"
        with system_access("simulation cast"):
            return services.record_person(
                display_name=name, email=f"sim{n:04d}@example.com", actor=SYSTEM
            )

    def refresh(self) -> None:
        """Today's owners, board, staff (after sales, elections, appointments)."""
        from community.models import (
            BoardSeat,
            Lot,
            OfficeHolding,
            Ownership,
            Role,
            RoleAssignment,
        )

        with system_access("simulation cast"):
            owners: dict[int, list] = {}
            for o in Ownership.objects.active().select_related("person", "lot").order_by("pk"):
                owners.setdefault(o.lot.number, []).append(o.person)
            for lot in Lot.objects.all():
                home = self.homes.get(lot.number)
                if home is None:
                    continue
                home.people = owners.get(lot.number, [])
                home.owners = [u for u in (self.login(p) for p in home.people) if u]
            offices = {
                h.person_id: h.office for h in OfficeHolding.objects.active().select_related()
            }
            seats = list(BoardSeat.objects.active().select_related("person"))
            order = {"president": 0, "treasurer": 1, "secretary": 2, "vice_president": 3}
            seats.sort(key=lambda s: (order.get(offices.get(s.person_id), 9), s.pk))
            self.board = [self.login(s.person) for s in seats]
            staff = RoleAssignment.objects.active().select_related("person")
            managers = [r.person for r in staff if r.role == Role.MANAGER]
            self.manager = self.login(managers[0]) if managers else None
            self.arc_reviewers = [
                self.login(r.person) for r in staff if r.role == Role.ARC_REVIEWER
            ]
            auditors = [r.person for r in staff if r.role == Role.AUDITOR]
            self.auditor = self.login(auditors[0]) if auditors else None

    def staff_for(self, matter, *, not_users=(), prefer_manager=False):
        """A board member (or the manager) who may take a staff step on
        ``matter``: never one whose own matter it is (D65), never one in
        ``not_users``. None when nobody can."""
        from core import conflicts

        pool = list(self.board)
        if prefer_manager and self.manager is not None:
            pool = [self.manager, *pool]
        for user in pool:
            if user in not_users:
                continue
            if matter is not None and conflicts.own_matter(user, matter):
                continue
            return user
        return None

    def board_others(self, *exclude):
        return [u for u in self.board if u not in exclude]

    def owner_homes(self, profile: str | None = None) -> list[Home]:
        return [
            h
            for h in sorted(self.homes.values(), key=lambda h: h.number)
            if h.people and (profile is None or h.profile == profile)
        ]


def build(seed: int) -> World:
    """The cast for the community the dataset seeded (or the register import made)."""
    from community.models import Lot

    rng = random.Random(f"{seed}:cast")  # noqa: S311  # nosec B311 - a reproducible cast
    world = World(rng_seed=seed)
    with system_access("simulation cast"):
        lots = list(Lot.objects.order_by("number"))
    bag = [name for name, n in PROFILES for _ in range(n)]
    while len(bag) < len(lots):
        bag.append("on_time")
    rng.shuffle(bag)
    for lot, profile in zip(lots, bag, strict=False):
        world.homes[lot.number] = Home(lot.pk, lot.number, profile)
    world.refresh()
    return world


def days(n: int) -> datetime.timedelta:
    return datetime.timedelta(days=n)
