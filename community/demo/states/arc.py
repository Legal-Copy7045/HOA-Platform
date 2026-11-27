"""ARC requests in every state, neighbour acknowledgements, recusal and
deemed approval."""

from __future__ import annotations

from community.demo import DemoError
from community.demo.states import staff

RECUSED_REQUEST = ("owner_arc", 15, "Screened porch (demo)")  # lot 14's reviewer is next door
OWN_MATTER = ("vice_president", 5, "Pergola over the patio (demo)")
APPEALABLE = ("joint_a", 7, "Front-yard fence (demo)")


def seed(c) -> None:
    from arc import services as arc
    from arc import signoffs
    from arc.models import ArcState, SignoffRequest, SignoffState

    A = arc.ARC
    manager = c.u("manager")

    def reviewer(req):
        """Decisions are the ARC reviewers' (never the board's or manager's)."""
        return next(
            u for u in (c.u("arc_reviewer"), c.u("arc_reviewer_2")) if not arc.is_recused(u, req)
        )

    def new(key, lot, title, project_type="Other"):
        return arc.create_request(
            lot=c.lots[lot],
            title=title,
            description=f"{title}: details and drawings to follow.",
            project_type=project_type,
            planned_start=c.days(30),
            planned_completion=c.days(60),
            location="rear",
            materials="As described (demo)",
            actor=c.u(key),
        )

    def answer(req, *, leave_one=False):
        """Neighbours answer on the printed form, entered by the manager."""
        pending = list(SignoffRequest.objects.filter(arc_request=req, state=SignoffState.PENDING))
        for signoff in pending[:-1] if leave_one else pending:
            signoffs.respond(
                signoff,
                position=SignoffState.APPROVED,
                signer_name="Neighbour (demo)",
                channel="paper",
                actor=manager,
            )

    def submitted(key, lot, title, project_type="Other"):
        req = new(key, lot, title, project_type)
        A.apply(req, "request_acknowledgements", actor=c.u(key))
        answer(req)
        req.refresh_from_db()
        if req.state != ArcState.SUBMITTED:
            raise DemoError(f"{title}: still {req.state}")
        return req

    def in_review(key, lot, title, project_type="Other"):
        req = submitted(key, lot, title, project_type)
        return A.apply(req, "accept", actor=staff(c, req))

    def decided(key, lot, title, outcome, **kw):
        req = in_review(key, lot, title)
        arc.decide(req, outcome=outcome, actor=reviewer(req), **kw)
        req.refresh_from_db()
        return req

    # The first part's lot 3 request went into review today: let its review
    # clock run out (the demo's deemed-approval rule is on), so one request
    # shows a deemed approval. Only requests in review now are affected.
    arc.apply_deemed_approvals(c.days(46))

    # Drafts (fresh: the owner edits, then asks the neighbours).
    new("owner", 3, "Garden shed (demo)", "Shed or outbuilding")
    new("owner_b", 12, "Front door color (demo)", "Exterior paint or color change")
    # Awaiting neighbour acknowledgements: one answered, one still waiting.
    req = new("owner_b", 12, "Back patio (demo)", "Deck or patio")
    A.apply(req, "request_acknowledgements", actor=c.u("owner_b"))
    answer(req, leave_one=True)
    # Submitted, waiting for the completeness check (one to accept, one to
    # return as incomplete).
    submitted("joint_a", 7, "Solar panels on the rear roof (demo)", "Solar panels")
    submitted("owner", 3, "Replace the rear deck (demo)", "Deck or patio")
    # In review: the lot 14 ARC reviewer lives next door to lot 15, so is
    # recused; the other reviewer has recorded a position.
    req = in_review(*RECUSED_REQUEST, "Deck or patio")
    arc.record_position(
        req, position="approve", comment="Fits the guidelines (demo).", actor=c.u("arc_reviewer_2")
    )
    in_review("owner_b", 12, "Split-rail fence (demo)", "Fence")
    # Decided: approved, approved with conditions, denied (still appealable).
    decided("joint_a", 7, "Garden beds (demo)", ArcState.APPROVED)
    decided(
        "owner_b",
        12,
        "Driveway widening (demo)",
        ArcState.APPROVED_WITH_CONDITIONS,
        conditions="Keep 5 ft from the property line (demo).",
    )
    decided(*APPEALABLE, ArcState.DENIED, reasons="Front-yard fences are not allowed (demo).")
    # Under appeal, and an appeal decided.
    req = decided("owner", 3, "Detached garage (demo)", ArcState.DENIED, reasons="Too large.")
    A.apply(req, "appeal", actor=c.u("owner"), note="We reduced the size (demo).")
    req = decided("owner_b", 12, "Hot tub (demo)", ArcState.DENIED, reasons="Too close.")
    A.apply(req, "appeal", actor=c.u("owner_b"), note="Moved it 10 ft (demo).")
    arc.decide_appeal(
        req,
        outcome=ArcState.APPROVED,
        reasons="Now within the setback (demo).",
        actor=staff(c, req, decide=True),
    )
    # Completed, and withdrawn.
    req = decided("owner", 3, "New roof shingles (demo)", ArcState.APPROVED)
    A.apply(req, "complete", actor=staff(c, req))
    req = new("owner_b", 12, "Flag pole (demo)")
    A.apply(req, "withdraw", actor=c.u("owner_b"))
    # Own matter (D65): the vice president's own request, submitted.
    key, _, title = OWN_MATTER
    submitted(key, c.home(key), title, "Deck or patio")
