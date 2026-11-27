"""Violations in every state."""

from __future__ import annotations

from community.demo.states import staff

# lot -> state after seeding (lot 12's "noticed" comes from the first part).
STATES = {
    20: "open",
    21: "open",
    7: "responded",
    15: "hearing",
    16: "cured",
    17: "upheld",
    19: "dismissed",
    26: "closed",
}


def seed(c) -> None:
    from compliance import services as compliance

    V = compliance.VIOLATION
    manager = c.u("manager")

    def report(lot, category, text):
        return compliance.report(
            lot=c.lots[lot], category=category, description=text, observed_on=c.t, actor=manager
        )

    def noticed(lot, category, text):
        return V.apply(report(lot, category, text), "send_notice", actor=manager)

    def board(v):
        return staff(c, v, decide=True)

    report(20, "landscaping", "Grass over 8 inches (demo).")  # fresh
    report(21, "parking", "Boat trailer in the driveway (demo).")  # fresh
    v = noticed(7, "pets", "Dog off leash on the common path (demo).")
    compliance.respond(v, text="New leash, fence coming (demo).", actor=c.u("joint_a"))
    v = noticed(15, "structure", "Shed built without approval (demo).")
    V.apply(v, "schedule_hearing", actor=manager, note="At the next board meeting (demo).")
    v = noticed(16, "trash", "Cans left out all week (demo).")
    V.apply(v, "mark_cured", actor=board(v))
    v = noticed(17, "nuisance", "Loud generator at night (demo).")
    V.apply(v, "schedule_hearing", actor=manager, note="Hearing held (demo).")
    V.apply(v, "uphold", actor=board(v), note="Upheld; fine per policy (demo).")
    v = noticed(19, "other", "Basketball hoop in the street (demo).")
    V.apply(v, "dismiss", actor=board(v), note="Removed before the hearing (demo).")
    v = noticed(26, "trash", "Recycling bin at the curb (demo).")
    V.apply(v, "mark_cured", actor=board(v))
    V.apply(v, "close", actor=manager)
