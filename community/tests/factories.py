"""Synthetic test data only (CLAUDE.md: no real personal data)."""

import datetime

import factory

from accounts.models import User
from community.models import Lot, Person


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    email = factory.Sequence(lambda n: f"user{n}@example.com")


class LotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lot

    number = factory.Sequence(lambda n: n + 1)
    street_address = factory.Sequence(lambda n: f"{n + 1} Fictional Lane")


class PersonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Person

    display_name = factory.Sequence(lambda n: f"Test Person {n}")
    email = factory.Sequence(lambda n: f"person{n}@example.com")


def days_ago(n: int) -> datetime.date:
    from community.models import today

    return today() - datetime.timedelta(days=n)


def make_member(*, board: bool = False, lots=(), roles=(), email: str | None = None):
    """A User with a linked Person, set up through the services as SYSTEM.

    board: give them an active board seat. lots: Lots they currently own.
    roles: Role values (arc_reviewer, auditor, sysadmin) to hold.
    """
    from community import services
    from community.models import AppointedBy, Role, RoleAssignment, SeatType
    from core.policy import SYSTEM
    from core.scoping import system_access

    user = UserFactory(**({"email": email} if email else {}))
    person = PersonFactory(user=user)
    if board:
        services.record_board_seat(
            person=person, seat_type=SeatType.OWNER_ELECTED, term_start=days_ago(200), actor=SYSTEM
        )
    for lot in lots:
        services.record_ownership(person=person, lot=lot, start_date=days_ago(300), actor=SYSTEM)
    for role in roles:
        if role == Role.SYSADMIN:
            with system_access("test setup"):
                RoleAssignment.objects.create(person=person, role=role, start_date=days_ago(10))
        else:
            services.grant_role(
                person=person,
                role=role,
                start_date=days_ago(10),
                appointed_by=AppointedBy.BOARD,
                actor=SYSTEM,
            )
    return user
