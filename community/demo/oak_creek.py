"""The ``oak_creek`` demo dataset (the default): 44 lots, owners, a board,
ARC members and an auditor, then everything seed_demo_operations adds
(three years of dues and payments, ARC, violations, meetings, votes...).

    python manage.py seed_demo                      # this dataset
    python manage.py seed_demo --dataset oak_creek

Everything is invented. Email addresses use the reserved example.com domain,
which can never deliver. Seeded people have no logins except the few that
author demo records (no password); a board member can invite any of them
from the portal (with EMAIL_REDIRECT_ALL_TO set on the POC, those
invitations arrive in one test inbox). Safe to run after
bootstrap_first_officer: it fills only empty offices.
"""

import datetime

from django.db import transaction

from community import services
from community.demo import SeedResult, address, demo_name
from community.models import (
    AppointedBy,
    BoardSeat,
    Lot,
    Office,
    OfficeHolding,
    OwnershipType,
    Role,
    SeatType,
    today,
)
from core.policy import SYSTEM
from core.scoping import system_access

# Two fictional roads of 22 homes each, the same shape as the real
# development (no real names or addresses: POC data is synthetic).
STREETS = ("Fictional Meadow Lane", "Imaginary Orchard Way")

JOINT_LOTS = {7, 23}  # two owners each
TRANSFERRED_LOT = 31  # sold 90 days ago


def seed() -> SeedResult:
    counter = iter(range(10_000))

    def person():
        n = next(counter)
        return services.record_person(
            display_name=demo_name(n), email=f"demo{n:03d}@example.com", actor=SYSTEM
        )

    long_ago = today() - datetime.timedelta(days=3 * 365)
    with transaction.atomic(), system_access("seed_demo"):
        owners_by_lot = {}
        for number in range(1, 45):
            lot = Lot.objects.create(
                number=number,
                street_address=address(number, STREETS),
            )
            count = 2 if number in JOINT_LOTS else 1
            kind = OwnershipType.JOINT if count == 2 else OwnershipType.SOLE
            owners = [person() for _ in range(count)]
            for owner in owners:
                services.record_ownership(
                    person=owner,
                    lot=lot,
                    start_date=long_ago,
                    ownership_type=kind,
                    actor=SYSTEM,
                )
            owners_by_lot[number] = owners
            if number == TRANSFERRED_LOT:
                buyer = person()
                services.transfer_lot(
                    lot,
                    new_owners=[buyer],
                    actor=SYSTEM,
                    transfer_date=today() - datetime.timedelta(days=90),
                )
                owners_by_lot[number] = [buyer]

        # Board: five seats in total (fewer if bootstrap already created one).
        existing = BoardSeat.objects.active().count()
        plan = [
            (2, SeatType.DECLARANT_APPOINTED),
            (5, SeatType.DECLARANT_APPOINTED),
            (11, SeatType.OWNER_ELECTED),
            (18, SeatType.OWNER_ELECTED),
            (29, SeatType.OWNER_ELECTED),
        ][: max(0, 5 - existing)]
        board = []
        for lot_number, seat_type in plan:
            member = owners_by_lot[lot_number][0]
            services.record_board_seat(
                person=member,
                seat_type=seat_type,
                term_start=today() - datetime.timedelta(days=200),
                term_end=today() + datetime.timedelta(days=530),
                citation="Bylaws §3.2 (demo)",
                actor=SYSTEM,
            )
            board.append(member)
        held = set(OfficeHolding.objects.active().values_list("office", flat=True))
        free_offices = [o for o in Office.values if o not in held]
        for member, office in zip(board, free_offices, strict=False):
            services.record_office(
                person=member,
                office=office,
                start_date=today() - datetime.timedelta(days=190),
                actor=SYSTEM,
            )

        for lot_number in (14, 38):
            services.grant_role(
                person=owners_by_lot[lot_number][0],
                role=Role.ARC_REVIEWER,
                appointed_by=AppointedBy.BOARD,
                citation="Declaration §9.1 (demo)",
                actor=SYSTEM,
            )
        services.grant_role(person=person(), role=Role.AUDITOR, actor=SYSTEM)

        # People on accounts who are not on the deed (Milestone 3).
        from community.models import Account

        for lot_number, kind in ((3, "authorized"), (3, "view_only"), (40, "tenant")):
            account = Account.objects.get(lot__number=lot_number, closed_on__isnull=True)
            services.add_account_contact(account, person=person(), kind=kind, actor=SYSTEM)

    import io

    from django.core.management import call_command

    # seed_demo prints one summary for the whole dataset.
    call_command("seed_demo_operations", stdout=io.StringIO())
    call_command("seed_demo_books", stdout=io.StringIO())  # books and close (D69-D71)
    return SeedResult(
        summary=(
            "Demo data created: 44 lots, joint owners on lots 7 and 23, lot 31 "
            f"transferred (its seller is a former owner), {len(board)} board seat(s), "
            "2 ARC reviewers, 1 auditor, contacts on lots 3 and 40 (tenant). "
            "Demo operations data created. Governing rules are cited 'DEMO - replace'."
        ),
        logins=_seeded_logins(),
        invite_first=[(p.email, f"board, {p.display_name}") for p in board if p.email],
    )


def _seeded_logins() -> list[tuple[str, str]]:
    from accounts.models import User

    users = User.objects.filter(person__display_name__endswith="(demo)").select_related("person")
    return [(u.email, u.person.display_name) for u in users.order_by("email")]
