"""The ``fresh_start`` demo dataset: a community the declarant has just
handed over (decision D53).

    python manage.py seed_demo --dataset fresh_start

44 homes on two fictional roads with their owners (most with an email, a few
without, one trust, one company, two couples), a declarant-appointed board of
three with offices, a sysadmin, and a placeholder Declaration. Nothing has
happened yet: no charges, payments, requests, ARC, violations, meetings or
other documents; community settings stay at their defaults (so setting the
community up is still to do) and no invitations have been sent.

Everything is invented: names end in "(demo)", addresses use the reserved
example.com domain. The president has a login without a password only
because a document needs an author; it is not an account anyone signs in to.
"""

from __future__ import annotations

import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from community import services
from community.demo import DEMO_SUFFIX, SeedResult, address, demo_name
from community.models import Lot, Office, OwnershipType, Role, SeatType, today
from core.policy import SYSTEM
from core.scoping import system_access

STREETS = ("Makebelieve Creek Drive", "Pretend Hollow Court")
JOINT_LOTS = {7, 23}  # a couple each
NO_EMAIL_LOTS = {9, 26, 41}  # owners who gave no email (post only)
TRUST_LOT = 12
ENTITY_LOT = 40
TRUST_NAME = f"Kestrel Family Trust {DEMO_SUFFIX}"
ENTITY_NAME = f"Fictional Holdings LLC {DEMO_SUFFIX}"
BOARD = [  # declarant's appointees: they work for the declarant and own no home here
    ("Rowan Declarant", Office.PRESIDENT),
    ("Ellis Declarant", Office.TREASURER),
    ("Marlow Declarant", Office.SECRETARY),
]
SYSADMIN_NAME = f"Robin Sysadmin {DEMO_SUFFIX}"


def seed() -> SeedResult:
    counter = iter(range(10_000))

    def person(name: str | None = None, *, email: bool = True):
        n = next(counter)
        return services.record_person(
            display_name=name or demo_name(n + 3),  # other names than oak_creek's
            email=f"fresh{n:03d}@example.com" if email else "",
            actor=SYSTEM,
        )

    # Closings over the last two years, most recent last (the declarant sold
    # every home; the handover followed the last sale).
    first_sale = today() - datetime.timedelta(days=730)
    with transaction.atomic(), system_access("seed_demo fresh_start"):
        for number in range(1, 45):
            lot = Lot.objects.create(number=number, street_address=address(number, STREETS))
            if number == TRUST_LOT:
                owners, kind = [person(TRUST_NAME)], OwnershipType.TRUST
            elif number == ENTITY_LOT:
                owners, kind = [person(ENTITY_NAME)], OwnershipType.ENTITY
            elif number in JOINT_LOTS:
                owners, kind = [person(), person()], OwnershipType.JOINT
            else:
                owners = [person(email=number not in NO_EMAIL_LOTS)]
                kind = OwnershipType.SOLE
            for owner in owners:
                services.record_ownership(
                    person=owner,
                    lot=lot,
                    start_date=first_sale + datetime.timedelta(days=number * 15),
                    ownership_type=kind,
                    actor=SYSTEM,
                )

        board = []
        for name, office in BOARD:
            member = person(f"{name} {DEMO_SUFFIX}")
            services.record_board_seat(
                person=member,
                seat_type=SeatType.DECLARANT_APPOINTED,
                term_start=today() - datetime.timedelta(days=7),
                citation="Declaration §3.1 (demo)",
                actor=SYSTEM,
            )
            services.record_office(person=member, office=office, start_date=today(), actor=SYSTEM)
            board.append(member)

        sysadmin = person(SYSADMIN_NAME)
        services._record_role(
            person=sysadmin,
            role=Role.SYSADMIN,
            start_date=today(),
            appointed_by="",
            citation="demo",
            actor=SYSTEM,
            request=None,
        )

        author = _author(board[0])
        _placeholder_declaration(author)

    return SeedResult(
        summary=(
            "Fresh start created: 44 homes (couples on lots 7 and 23, a trust on lot "
            f"{TRUST_LOT}, a company on lot {ENTITY_LOT}, no email for lots "
            f"{', '.join(str(n) for n in sorted(NO_EMAIL_LOTS))}), a declarant-appointed "
            "board of three, a sysadmin and a placeholder Declaration. No history, settings "
            "at their defaults, no invitations sent."
        ),
        logins=[(author.email, f"{board[0].display_name}, author of the Declaration only")],
        invite_first=[(p.email, f"board, {p.display_name}") for p in board]
        + [(sysadmin.email, f"sysadmin, {sysadmin.display_name}")],
    )


def _author(person):
    """A login without a password, so the placeholder document has an author."""
    from accounts.models import User

    user = User.objects.create_user("fresh-board0@example.com")
    services.link_login(person, user, actor=SYSTEM)
    return user


def _placeholder_declaration(author) -> None:
    from core import pdf
    from documents import services as documents

    body = pdf.render(
        [
            ("heading", "Declaration (FICTIONAL DEMO)"),
            ("normal", "This is placeholder text, not a real document."),
        ]
    )
    documents.create_document(
        title="Declaration (demo placeholder)",
        category="governing",
        visibility="owners",
        actor=author,
        file=SimpleUploadedFile("declaration.pdf", body),
    )
