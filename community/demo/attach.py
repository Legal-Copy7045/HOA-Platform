"""Give a demo reseed's requester a board seat and a demo home (decision D53).

The person who reseeds the POC usually uses one login as sysadmin, board
member and demo owner. keep_logins brings back their login, person and
sysadmin role only; this adds, in the new data and as SYSTEM through the
community services: an active declarant-appointed board seat from today
(unless they already hold one) and joint ownership of the first demo home
with a single demo owner (unless they already own a home). Audited
``system.demo_requester_attached`` with ids only. The command that calls this
refuses outside DEPLOYMENT_STAGE=poc with SEED_DEMO_ALLOWED.
"""

from __future__ import annotations

from collections import defaultdict

from audit.models import Channel
from audit.services import record_event
from community import demo, services
from community.models import BoardSeat, Lot, Ownership, OwnershipType, Person, SeatType, today
from core.policy import SYSTEM
from core.scoping import system_access

CITATION = "Demo: the reseed requester (D53)"


def first_single_owner_demo_lot() -> Lot | None:
    """The lowest-numbered home whose only current owner is a demo person."""
    owners = defaultdict(list)
    for o in Ownership.objects.active().select_related("person", "lot"):
        owners[o.lot].append(o.person)
    for lot in sorted(owners, key=lambda lot: lot.number):
        people = owners[lot]
        if len(people) == 1 and demo.is_demo_person(people[0]):
            return lot
    return None


def attach_requester(task_id: int) -> dict:
    from core.models import HostTask

    task = (
        HostTask.objects.filter(pk=task_id, kind=HostTask.Kind.RESEED_DEMO, attach_requester=True)
        .select_related("requested_by")
        .first()
    )
    if task is None:
        raise demo.DemoError("No reseed task with that id asked for this.")
    with system_access("demo_attach_requester"):
        person = Person.objects.filter(user=task.requested_by).first()
        if person is None or demo.is_demo_person(person):
            raise demo.DemoError("The requester has no kept (non-demo) person to attach.")
        seat = BoardSeat.objects.active().filter(person=person).first()
        if seat is None:
            seat = services.record_board_seat(
                person=person,
                seat_type=SeatType.DECLARANT_APPOINTED,
                term_start=today(),
                citation=CITATION,
                actor=SYSTEM,
            )
        ownership = Ownership.objects.active().filter(person=person).first()
        if ownership is None:
            lot = first_single_owner_demo_lot()
            if lot is None:
                raise demo.DemoError("No demo home with a single demo owner to join.")
            ownership = services.record_ownership(
                person=person,
                lot=lot,
                start_date=today(),
                ownership_type=OwnershipType.JOINT,
                actor=SYSTEM,
            )
        lot = ownership.lot
    record_event(
        "system.demo_requester_attached",
        actor=None,
        channel=Channel.CLI,
        target=task,
        metadata={
            "task_id": task.pk,
            "person_id": person.pk,
            "seat_id": seat.pk,
            "ownership_id": ownership.pk,
            "lot_id": lot.pk,
        },
    )
    return {"seat": seat, "ownership": ownership, "lot_number": lot.number}
