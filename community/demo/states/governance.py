"""Meetings, minutes, resolutions, written consents, votes, elections,
surveys, proxies, records requests and committees in every state.

Ballots and survey answers go only through the public casting services
(``cast_ballot``, ``cast_election_ballot``, ``submit_response``); secret
choices are pooled and mixed by those services as for any owner."""

from __future__ import annotations

import datetime

from community.demo.states import _backdate, _pdf_upload, owner_of

# Titles the tester's guide points to.
OPEN_VOTE = "Repave the walking path (demo)"
SPEED_HUMP_VOTE = "Install speed humps (demo)"
EXTENDED_VOTE = "Allow backyard chickens (demo)"
CLOSED_VOTE = "Adopt the 2026 reserve study (demo)"
VOTING_ELECTION = "Special election: one board seat (demo)"
CERTIFIED_ELECTION = "Last year's board election (demo)"
CLOSED_ELECTION = "Pond committee liaison election (demo)"
DRAFT_SURVEY = "Holiday lights contest (demo)"
CLOSED_SURVEY = "Spring yard sale date (demo)"


def seed(c) -> None:
    from governance import elections, surveys, window
    from governance import services as gov
    from governance.models import Meeting, RecordsRequest

    pres, vp, treas, sec = (
        c.u(k) for k in ("president", "vice_president", "treasurer", "secretary")
    )
    manager = c.u("manager")
    now = c.now

    # --- Meetings ------------------------------------------------------------------------
    annual = Meeting.objects.get(title="Annual owners' meeting (demo)")
    gov.send_notice(annual, actor=manager)
    gov.create_meeting(
        title="Executive session: collections (demo)",
        kind="executive",
        starts_at=now + datetime.timedelta(days=10, hours=1),
        location="Community room (fictional)",
        agenda="1. Delinquent accounts (board only)",
        actor=sec,
    )
    gov.create_meeting(
        title="Special owners' meeting: pond fountain (demo)",
        kind="special",
        starts_at=now + datetime.timedelta(days=35),
        location="Pavilion (fictional)",
        agenda="1. Fountain proposal",
        actor=sec,
    )
    cancelled = gov.create_meeting(
        title="Board workshop (demo)",
        kind="board",
        starts_at=now + datetime.timedelta(days=17),
        location="Community room (fictional)",
        agenda="1. Long-range plan",
        actor=sec,
    )
    gov.set_meeting_status(cancelled, "cancelled", actor=sec)
    # Held two months ago, minutes uploaded and approved (owners see them).
    september = gov.create_meeting(
        title="Board meeting two months ago (demo)",
        kind="board",
        starts_at=now - datetime.timedelta(days=60),
        location="Community room (fictional)",
        agenda="1. Minutes\n2. Budget draft",
        actor=sec,
    )
    gov.set_meeting_status(september, "held", actor=sec)
    for key in ("president", "vice_president", "treasurer", "secretary"):
        gov.record_attendance(september, c.people[key], present=True, actor=sec)
    gov.upload_minutes(
        september,
        file=_pdf_upload("minutes-sept.pdf", "Minutes, two months ago"),
        title="Board minutes, two months ago (demo)",
        actor=sec,
    )
    september.refresh_from_db()
    gov.approve_minutes(september, approved_on=c.days(-30), note="Approved (demo).", actor=sec)
    # Proxies for the annual meeting: one given, one revoked.
    elections.grant_proxy(annual, lot=c.lots[3], holder=owner_of(4), actor=c.u("owner"))
    proxy = elections.grant_proxy(annual, lot=c.lots[12], holder=owner_of(13), actor=c.u("owner_b"))
    elections.revoke_proxy(proxy, actor=c.u("owner_b"))

    # --- Resolutions and written consents ------------------------------------------------
    from governance.models import Resolution

    gov.record_resolution(
        kind="resolution",
        title="Engage Example Management Co. (demo)",
        text="The board engages Example Management Co. as community manager.",
        adopted_on=c.days(-200),
        votes_for=5,
        actor=sec,
    )
    old_policy = Resolution.objects.get(title="Trash can storage (demo)")
    gov.record_resolution(
        kind="policy",
        title="Trash can storage, revised (demo)",
        text="Cans out of street view except from 6 pm the evening before collection.",
        adopted_on=c.days(-20),
        votes_for=4,
        votes_against=1,
        supersedes=old_policy,
        actor=sec,
    )
    open_consent = elections.propose_consent(
        title="Approve the snow plowing contract (demo)",
        text="Resolved, that the board approves the winter plowing contract.",
        actor=pres,
    )
    elections.sign_consent(open_consent, approve=True, actor=pres)
    adopted = elections.propose_consent(
        title="Open a reserve savings account (demo)",
        text="Resolved, that the treasurer opens a reserve savings account.",
        actor=treas,
    )
    for user in c.board:
        elections.sign_consent(adopted, approve=True, actor=user)
    failed = elections.propose_consent(
        title="Buy a holiday wreath (demo)",
        text="Resolved, that the board buys a wreath.",
        actor=vp,
    )
    elections.sign_consent(failed, approve=False, actor=treas)
    withdrawn = elections.propose_consent(
        title="Change the meeting night (demo)", text="Resolved, Tuesdays.", actor=sec
    )
    elections.withdraw_consent(withdrawn, actor=sec)

    # --- Votes ---------------------------------------------------------------------------
    def formal(title, *, secret=False, days=14, meeting=None):
        vote = gov.create_vote(
            title=title,
            kind="formal",
            options=["Yes", "No"],
            description="A formal vote of the owners (demo).",
            secret=secret,
            opens_at=now,
            closes_at=now + datetime.timedelta(days=days),
            meeting=meeting,
            actor=pres,
        )
        return gov.open_vote(vote, actor=pres)

    def cast(vote, key, lot, pick=0):
        option = list(vote.options.order_by("pk"))[pick]
        gov.cast_ballot(vote, option_id=option.pk, lot_id=c.lots[lot].pk, actor=c.u(key))

    # Open: the owner of lot 3 has not voted (try it); lot 12 has.
    vote = formal(OPEN_VOTE)
    cast(vote, "owner_b", 12)
    vote = formal(SPEED_HUMP_VOTE, secret=True)
    cast(vote, "joint_a", 7, pick=1)
    vote = formal(EXTENDED_VOTE, days=8)
    window.extend(
        vote,
        closes_at=vote.closes_at + datetime.timedelta(days=7),
        reason="More time for owners away for the holiday (demo).",
        actor=pres,
    )
    # Closed with results (closed by the system, as at the deadline).
    from core.policy import SYSTEM

    vote = formal(CLOSED_VOTE, days=8)
    for key, lot, pick in (("owner", 3, 0), ("owner_b", 12, 0), ("joint_a", 7, 1)):
        cast(vote, key, lot, pick)
    gov.close_vote(vote, actor=SYSTEM)

    # --- Elections -----------------------------------------------------------------------
    from governance.models import Election

    nominating = Election.objects.get(title="Annual board election (demo)")
    accepted = elections.nominate(
        nominating, person=owner_of(36), statement="Ten years here (demo).", actor=pres
    )
    elections.accept_nomination(accepted, actor=pres)
    elections.nominate(nominating, person=owner_of(37), actor=c.u("owner"))  # not yet accepted

    def election(title, candidates, *, seats=1, days=10):
        e = elections.create_election(
            title=title, seats=seats, citation="Bylaws (demo)", actor=pres
        )
        for lot in candidates:
            cand = elections.nominate(e, person=owner_of(lot), statement="(demo)", actor=pres)
            elections.accept_nomination(cand, actor=pres)
        return elections.open_voting(
            e, opens_at=now, closes_at=now + datetime.timedelta(days=days), actor=pres
        )

    def ballot(e, key, lot, index=0):
        cands = list(elections.ballot_candidates(e).order_by("pk"))
        elections.cast_election_ballot(
            e, lot_id=c.lots[lot].pk, candidate_ids=[cands[index].pk], actor=c.u(key)
        )

    e = election(VOTING_ELECTION, [39, 41, 42])
    ballot(e, "owner_b", 12, 1)
    e = election(CERTIFIED_ELECTION, [43, 44])
    ballot(e, "owner", 3, 0)
    ballot(e, "owner_b", 12, 0)
    elections.close_voting(e, actor=SYSTEM)
    elections.certify(e, actor=sec)
    e = election(CLOSED_ELECTION, [16, 17])  # closed, waiting to be certified
    ballot(e, "joint_a", 7, 1)
    elections.close_voting(e, actor=SYSTEM)

    # --- Surveys -------------------------------------------------------------------------
    draft = surveys.create_survey(
        title=DRAFT_SURVEY,
        audience="residents",
        opens_on=c.days(3),
        closes_on=c.days(20),
        actor=sec,
    )
    surveys.add_question(
        draft, kind="single", prompt="Enter the contest?", choices=["Yes", "No"], actor=sec
    )
    closed = surveys.create_survey(
        title=CLOSED_SURVEY,
        audience="owners",
        opens_on=c.t,
        closes_on=c.days(7),
        share_results=True,
        actor=sec,
    )
    q = surveys.add_question(
        closed,
        kind="single",
        prompt="Which Saturday?",
        choices=["First", "Second"],
        required=True,
        actor=sec,
    )
    surveys.open_survey(closed, notify=False, actor=sec)
    first = str(next(iter(q.choices.all())).pk)
    for key in ("owner", "owner_b", "joint_a"):
        surveys.submit_response(closed, answers={q.pk: [first]}, actor=c.u(key))
    surveys.close_survey(closed, actor=SYSTEM)

    # --- Records requests ----------------------------------------------------------------
    R = gov.RECORDS

    def records(key, text):
        return gov.submit_records_request(description=text, actor=c.u(key))

    r = records("owner", "Pond maintenance contracts, last 3 years (demo)")
    R.apply(r, "acknowledge", actor=manager)
    r = records("joint_a", "Last year's audited financials (demo)")
    R.apply(r, "fulfil", actor=manager, note="Sent through the document library (demo).")
    r = records("owner_b", "Other owners' phone numbers (demo)")
    R.apply(r, "deny", actor=manager, note="Not an association record we may share (demo).")
    r = records("owner_arc", "Board emails (demo)")
    R.apply(r, "withdraw", actor=c.u("owner_arc"))
    # Due in three days (asked 27 days ago).
    r = records("joint_b", "Insurance certificate for the common areas (demo)")
    _backdate(
        RecordsRequest, r.pk, submitted_at=now - datetime.timedelta(days=27), due_on=c.days(3)
    )
    # Overdue: asked 40 days ago, answer was due 10 days ago.
    r = records("owner_late", "Collection policy and my account history (demo)")
    _backdate(
        RecordsRequest,
        r.pk,
        submitted_at=now - datetime.timedelta(days=40),
        due_on=c.days(-10),
    )

    # --- Committees ----------------------------------------------------------------------
    from governance.models import Committee

    social = Committee.objects.get(name="Social")
    task = gov.add_committee_task(
        social, title="Order picnic supplies (demo)", due_date=c.days(5), actor=c.u("owner")
    )
    gov.close_committee_task(social, task, status="done", actor=c.u("owner"))
    welcome = gov.create_committee(name="Welcome", purpose="Greets new owners (demo).", actor=pres)
    gov.set_committee_active(welcome, active=False, actor=pres)
