"""Owner requests in every state, with replies and internal notes."""

from __future__ import annotations

# (persona, lot, subject) of the requests below, for the tester's guide.
FRESH = [
    ("authorized", 3, "Loose paver by the mailbox (demo)"),
    ("tenant", 40, "Where do I park a moving truck? (demo)"),
]
OWN_MATTER = ("vice_president", 5, "Tree limb over my driveway (demo)")


def seed(c) -> None:
    from comms import threads
    from operations import services as ops

    W = ops.OWNER_REQUEST
    manager = c.u("manager")

    def submit(key, lot, kind, subject, description="(demo)"):
        return ops.submit_request(
            lot=c.lots[lot],
            request_type=kind,
            subject=subject,
            description=description,
            actor=c.u(key),
        )

    # Fresh, waiting for the board: from an authorized contact and a tenant.
    submit("authorized", 3, "maintenance", FRESH[0][2], "Near our mailbox.")
    submit("tenant", 40, "general", FRESH[1][2], "Moving in next month.")
    # In progress, assigned to the manager, with a reply and an internal note.
    r = submit("joint_a", 7, "common_area", "Streetlight out at the corner (demo)", "Since Monday.")
    W.apply(r, "start", actor=manager)
    ops.assign_request(r, c.people["manager"], actor=manager)
    threads.post("request", r, body="Reported to the utility (demo).", actor=manager)
    threads.post(
        "request", r, body="Utility ticket 0000 (internal demo note).", internal=True, actor=manager
    )
    # Waiting on the owner.
    r = submit("owner_b", 12, "maintenance", "Water pooling by the curb (demo)", "After rain.")
    W.apply(r, "ask_owner", actor=manager, note="Could you add a photo? (demo)")
    # Resolved (the owner may reopen it), and resolved then closed.
    r = submit("owner", 3, "general", "Snow plowing schedule? (demo)", "When is the road plowed?")
    W.apply(r, "resolve", actor=manager, note="After 2 inches of snow (demo).")
    r = submit("joint_a", 7, "billing", "Duplicate charge? (demo)", "I see two charges.")
    W.apply(r, "resolve", actor=c.u("treasurer"), note="One was a reversal (demo).")
    W.apply(r, "close", actor=c.u("treasurer"))
    # Withdrawn by the owner.
    r = submit("owner_b", 12, "general", "Pool pass question (demo)", "Never mind.")
    W.apply(r, "withdraw", actor=c.u("owner_b"))
    # Turned into a work order.
    r = submit("owner_arc", 15, "maintenance", "Cracked sidewalk slab (demo)", "Trip hazard.")
    ops.request_to_work_order(r, actor=manager)
    # Own matter (D65): a board member's own request; another board member acts.
    key, _, subject = OWN_MATTER
    submit(key, c.home(key), "maintenance", subject, "A common-area limb.")
