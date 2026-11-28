"""People: governance, ARC, violations, requests, community life, home sales,
vendors, insurance claim, capital project, renewals and the own-matter probes.

Every step goes through the service the page uses, as the right person: the
owner for an owner's step, a board member or the manager for a staff step,
never on their own matter (``World.staff_for``), and a different board member
wherever the portal asks for a second one.
"""

from __future__ import annotations

import datetime

from django.utils import timezone

from core.scoping import system_access
from regression.simulation.scenario import setup
from regression.simulation.scenario.money import SKIP, _lot, _receipt, due_now

D = datetime.timedelta


def _now():
    return timezone.now()


def _model(path):
    from django.apps import apps

    return apps.get_model(path)


def _get(path, pk):
    m = _model(path)
    mgr = m.objects
    with system_access("simulation lookup"):
        qs = mgr.unscoped("simulation") if hasattr(mgr, "unscoped") else mgr.all()
        return qs.get(pk=pk)


def annual_months(sc) -> list[int]:
    """The annual meeting (with its election) in month 4, then every 12 months."""
    if sc.o.months < 2:
        return []  # the first day is the set-up day: no room for a meeting
    first = 4 if sc.o.months > 5 else max(1, sc.o.months - 2)
    return list(range(first, sc.o.months, 12))


def plan(sc, day, rng):
    if day == sc.o.start:
        return []
    ev, m, d = sc.ev, sc.month_index(day), day.day
    fast = sc.o.fast
    out = []
    # --- every month ----------------------------------------------------------------------
    if d == 3:
        out.append(ev("announcement.publish", day, 9, _announce(sc, day), area="community"))
    if d == 1:
        out.append(ev("meeting.board_create", day, 19, _board_meeting(sc, day), area="governance"))
    if d == 2:
        out.append(ev("meeting.notice", day, 19, _meeting_notice(sc), area="governance"))
    if d == 16:
        out.append(ev("meeting.held", day, 21, _meeting_held(sc), area="governance"))
    if d == 22:
        out.append(ev("meeting.minutes", day, 19, _minutes(sc), area="governance"))
    if d in (7, 19) and not (fast and d == 19):
        out.append(ev("request.submit", day, 18, _owner_request(sc, rng), area="requests"))
    if d in (9, 21):
        out.append(ev("request.handle", day, 10, _handle_requests(sc), area="requests"))
    if m % 3 == 1 and d == 10:
        out.append(
            ev("records_request.submit", day, 20, _records_request(sc, rng), area="requests")
        )
    if d == 25:
        out.append(ev("records_request.fulfil", day, 10, _fulfil_records(sc), area="requests"))
    if m % 2 == 0 and d == 4:
        out.append(ev("arc.request", day, 19, _arc_request(sc, rng), area="arc"))
    if d in (6, 8):
        out.append(ev("arc.acknowledge", day, 20, _arc_acknowledge(sc), area="arc"))
    if d in (11, 26):
        out.append(ev("arc.accept", day, 10, _arc_accept(sc), area="arc"))
    if d == 18:
        out.append(ev("arc.decide", day, 19, _arc_decide(sc, rng), area="arc"))
    if m % 2 == 1 and d == 6:
        out.append(ev("violation.report", day, 14, _violation(sc, rng), area="compliance"))
    if d in (8, 14, 24):
        out.append(ev("violation.step", day, 15, _violation_steps(sc), area="compliance"))
    if d in (8, 23) and not (fast and d == 23):
        out.append(ev("reservation.request", day, 20, _reserve_pavilion(sc, rng), area="community"))
    if d in (9, 24):
        out.append(ev("reservation.decide", day, 9, _decide_reservations(sc), area="community"))
    # --- once or twice in the run -----------------------------------------------------------------
    if m == 1 and d == 2:
        out.append(ev("survey.create", day, 19, _survey(sc, day), area="governance"))
    if m == 1 and 3 <= d <= 12:
        out.append(ev("survey.respond", day, 20, _survey_answers(sc, rng), area="governance"))
    if m == 2 and d == 5:
        out.append(ev("vendor_onboarding.invite", day, 10, _onboard_invite(sc), area="vendors"))
    if m == 2 and d == 7:
        out.append(
            ev(
                "vendor_onboarding.vendor_submits",
                day,
                11,
                _onboard_submit(sc),
                area="vendors",
                audit=False,
            )
        )
    if m == 2 and d == 9:
        out.append(ev("vendor_onboarding.approve", day, 19, _onboard_approve(sc), area="vendors"))
    if m == 3 and d == 1:
        out.append(ev("event.rsvp_setup", day, 19, _picnic(sc, day), area="community"))
    if m == 3 and 2 <= d <= 12:
        out.append(ev("event.rsvp_answer", day, 20, _rsvp(sc, rng), area="community"))
    for am in annual_months(sc):
        out.extend(_annual(sc, day, m, d, am, rng))
    sm = 6 if sc.o.months > 8 else max(1, sc.o.months - 1)
    if m == sm and d == 1:
        out.append(ev("vote.special_create", day, 19, _special_vote(sc, day), area="governance"))
    if m == sm and 3 <= d <= 14:
        out.append(ev("vote.cast", day, 20, _cast_votes(sc, rng), area="governance"))
    if m == sm and d == 17:
        out.append(
            ev("vote.special_result", day, 9, _special_result(sc), area="governance", audit=False)
        )
    for sale_month in (5, 11):
        if m == sale_month and d == 3:
            out.append(ev("resale.request", day, 18, _resale_request(sc, sale_month), area="sales"))
        if m == sale_month and d == 6:
            out.append(ev("resale.issue", day, 10, _resale_issue(sc), area="sales"))
        if m == sale_month and d == 20:
            out.append(ev("ownership.transfer", day, 10, _sale(sc, sale_month), area="sales"))
    if m == 8 and d == 4:
        out.append(ev("insurance_claim.record", day, 10, _claim(sc, day), area="claims"))
    if m == 8 and d == 18:
        out.append(ev("insurance_claim.settle", day, 10, _claim_settle(sc), area="claims"))
    if m == 9 and d == 5:
        out.append(ev("capital_project.approve", day, 19, _project(sc), area="projects"))
    if m == 9 and d == 27:
        out.append(ev("capital_project.complete", day, 19, _project_complete(sc), area="projects"))
    if d == 12:
        out.append(ev("renewals.review", day, 10, _renewals(sc, day), area="renewals"))
    # --- probes: the portal must refuse these -----------------------------------------------------
    if m == 1 and d == 15:
        out.append(
            ev(
                "probe.manager_approves_invoice",
                day,
                11,
                _probe_manager(sc),
                area="probe",
                probe=True,
            )
        )
    if d == 27 and m in (2, 7):
        out.append(
            ev("probe.own_home_charge", day, 11, _probe_own_home(sc), area="probe", probe=True)
        )
    if m == 2 and d == 18:
        out.append(
            ev(
                "probe.recused_reviewer_decides",
                day,
                11,
                _probe_recused(sc),
                area="probe",
                probe=True,
            )
        )
    return out


# --- communications and meetings --------------------------------------------------------------


def _announce(sc, day):
    def run():
        from comms import services as comms

        comms.publish_announcement(
            title=f"News for {day:%B %Y} (simulation)",
            body="Pool opening, trash schedule and the next board meeting (fictional).",
            actor=sc.world.board[-1],
        )

    return run


def _board_meeting(sc, day):
    def run():
        from governance import services as gov

        mtg = gov.create_meeting(
            title=f"Board meeting {day:%B %Y} (sim)",
            kind="board",
            starts_at=_now() + D(days=15),
            location="Community room (fictional)",
            agenda="1. Minutes\n2. Treasurer's report\n3. ARC\n4. Open forum",
            actor=sc.world.board[-1],
        )
        sc.notes["meeting"] = mtg.pk

    return run


def _meeting_notice(sc):
    def run():
        from governance import services as gov

        if "meeting" not in sc.notes:
            return SKIP
        gov.send_notice(_get("governance.Meeting", sc.notes["meeting"]), actor=sc.world.board[-1])

    return run


def _meeting_held(sc):
    def run():
        from governance import services as gov

        if "meeting" not in sc.notes:
            return SKIP
        mtg = _get("governance.Meeting", sc.notes["meeting"])
        gov.set_meeting_status(mtg, "held", actor=sc.world.board[-1])

    return run


def _minutes(sc):
    def run():
        from governance import services as gov

        if "meeting" not in sc.notes:
            return SKIP
        mtg = _get("governance.Meeting", sc.notes["meeting"])
        gov.upload_minutes(
            mtg,
            file=setup._pdf("Board minutes"),
            title=f"Minutes: {mtg.title}",
            actor=sc.world.board[-1],
        )
        prev = sc.notes.get("minutes_to_approve")
        if prev:
            gov.approve_minutes(
                _get("governance.Meeting", prev), note="Approved", actor=sc.world.board[0]
            )
        sc.notes["minutes_to_approve"] = mtg.pk

    return run


# --- requests ------------------------------------------------------------------------------------


def _pick_owner(sc, rng):
    homes = [h for h in sc.world.owner_homes() if h.owners]
    return homes[int(rng.random() * len(homes))]


def _owner_request(sc, rng):
    home = _pick_owner(sc, rng)

    def run():
        from operations import services as ops

        kind = ["general", "billing", "maintenance", "common_area"][int(rng.random() * 4)]
        ops.submit_request(
            lot=_lot(home),
            request_type=kind,
            subject="Question for the board (simulation)",
            description="A fictional owner's question.",
            actor=home.owners[0],
        )

    return run


def _handle_requests(sc):
    def run():
        from comms import threads
        from operations import services as ops

        Req = _model("operations.OwnerRequest")
        with system_access("simulation lookup"):
            open_ = list(
                Req.objects.exclude(state__in=("resolved", "withdrawn", "closed")).order_by("pk")
            )
        if not open_:
            return SKIP
        for req in open_:
            staff = sc.world.staff_for(req, prefer_manager=True)
            if staff is None:
                continue
            if req.state == "submitted":
                ops.OWNER_REQUEST.apply(req, "start", actor=staff)
                threads.post(
                    "request", req, body="We are looking into it (simulation).", actor=staff
                )
            else:
                ops.OWNER_REQUEST.apply(req, "resolve", actor=staff, note="Answered (simulation)")
        return f"{len(open_)} request(s)"

    return run


def _records_request(sc, rng):
    home = _pick_owner(sc, rng)

    def run():
        from governance import services as gov

        gov.submit_records_request(
            description="Last year's financial statements (sim)", actor=home.owners[0]
        )

    return run


def _fulfil_records(sc):
    def run():
        from governance import services as gov

        RR = _model("governance.RecordsRequest")
        with system_access("simulation lookup"):
            waiting = list(RR.objects.filter(state__in=("submitted", "in_progress")))
        if not waiting:
            return SKIP
        for r in waiting:
            staff = sc.world.staff_for(r, prefer_manager=True)
            gov.RECORDS.apply(r, "fulfil", actor=staff, note="Sent by email (simulation)")
        return f"{len(waiting)} fulfilled"

    return run


# --- ARC -----------------------------------------------------------------------------------------


def _arc_request(sc, rng):
    # Lots next to an ARC reviewer's home first, so recusal is exercised.
    def run():
        from arc import services as arc
        from arc import signoffs

        homes = [h for h in sc.world.owner_homes() if h.owners]
        reviewers = {p.pk for p in _reviewer_people(sc)}
        near = [
            h
            for h in homes
            if any(
                p.pk in reviewers for x in homes if abs(x.number - h.number) == 1 for p in x.people
            )
            and not any(p.pk in reviewers for p in h.people)
        ]
        pool = near if near and rng.random() < 0.6 else homes
        home = pool[int(rng.random() * len(pool))]
        types = arc.active_project_types()
        req = arc.create_request(
            lot=_lot(home),
            title="Rear fence (simulation)",
            description="4 ft black aluminum fence in the rear yard (fictional).",
            project_type=types[0].name if types else "",
            actor=home.owners[0],
        )
        signoffs.prepare_signoffs(req, actor=home.owners[0])
        try:
            arc.ARC.apply(req, "request_acknowledgements", actor=home.owners[0])
        except Exception:
            arc.ARC.apply(req, "submit", actor=home.owners[0])
        return f"lot {home.number}"

    return run


def _reviewer_people(sc):
    from community.models import RoleAssignment

    with system_access("simulation lookup"):
        return [r.person for r in RoleAssignment.objects.active().filter(role="arc_reviewer")]


def _arc_acknowledge(sc):
    def run():
        from arc import signoffs

        SR = _model("arc.SignoffRequest")
        with system_access("simulation lookup"):
            waiting = list(SR.objects.filter(state="pending").select_related("lot"))
        done = 0
        for s in waiting:
            home = sc.world.homes.get(s.lot.number) if s.lot_id else None
            if home is None:
                continue
            if not home.owners:
                # No login (post only): the neighbor signs the printed form and
                # staff enter it.
                staff = _arc_staff(sc, s.arc_request)
                signoffs.respond(
                    s,
                    position="approved",
                    signer_name=home.people[0].display_name if home.people else "Neighbor",
                    channel="paper",
                    signed_on=timezone.localdate(),
                    actor=staff,
                )
                done += 1
                continue
            user = home.owners[0]
            if not signoffs.may_answer_in_portal(user, s):
                continue
            signoffs.respond(
                s,
                position="approved",
                signer_name=user.person.display_name,
                channel="portal",
                actor=user,
            )
            done += 1
        return f"{done} acknowledgement(s)" if done else SKIP

    return run


def _arc_staff(sc, req, not_users=(), *, deciding=False):
    """An ARC reviewer (or for intake, a board member) not recused from ``req``;
    only reviewers decide (``arc.decide``)."""
    from core import conflicts

    pool = [*sc.world.arc_reviewers, *([] if deciding else sc.world.board)]
    for u in pool:
        if u in not_users or conflicts.own_matter(u, req):
            continue
        return u
    return None


def _arc_accept(sc):
    def run():
        from arc import services as arc

        Req = _model("arc.ArcRequest")
        with system_access("simulation lookup"):
            waiting = list(Req.objects.filter(state="submitted"))
        for req in waiting:
            arc.ARC.apply(req, "accept", actor=_arc_staff(sc, req))
        return f"{len(waiting)} accepted" if waiting else SKIP

    return run


def _arc_decide(sc, rng):
    def run():
        from arc import services as arc

        Req = _model("arc.ArcRequest")
        with system_access("simulation lookup"):
            waiting = list(Req.objects.filter(state="in_review"))
        for req in waiting:
            r = rng.random()
            outcome = (
                "approved" if r < 0.6 else ("approved_with_conditions" if r < 0.85 else "denied")
            )
            arc.decide(
                req,
                outcome=outcome,
                reasons="" if outcome != "denied" else "Exceeds the height limit (simulation).",
                conditions="Black finish only (simulation)."
                if outcome == "approved_with_conditions"
                else "",
                citation="Declaration §9.3 (simulation)",
                actor=_arc_staff(sc, req, deciding=True),
            )
        return f"{len(waiting)} decided" if waiting else SKIP

    return run


# --- violations ----------------------------------------------------------------------------------


def _violation(sc, rng):
    home = _pick_owner(sc, rng)

    def run():
        from compliance import services as compliance

        lot = _lot(home)
        compliance.report(
            lot=lot,
            category="trash",
            description="Trash cans left at the curb (simulation).",
            actor=sc.world.staff_for(lot),
        )

    return run


def _violation_steps(sc):
    def run():
        from compliance import services as compliance

        V = _model("compliance.Violation")
        with system_access("simulation lookup"):
            open_ = list(V.objects.exclude(state__in=("closed", "dismissed")).select_related("lot"))
        done = 0
        for v in open_:
            staff = sc.world.staff_for(v)
            home = sc.world.homes.get(v.lot.number)
            if v.state == "open":
                compliance.VIOLATION.apply(v, "send_notice", actor=staff)
            elif v.state == "noticed" and home and home.owners:
                compliance.respond(
                    v, text="Moved them back, sorry (simulation).", actor=home.owners[0]
                )
            elif v.state in ("noticed", "responded"):
                compliance.VIOLATION.apply(v, "mark_cured", actor=staff, note="Cured (simulation)")
            elif v.state == "cured":
                compliance.VIOLATION.apply(v, "close", actor=staff, note="Closed (simulation)")
            else:
                continue
            done += 1
        return f"{done} step(s)" if done else SKIP

    return run


# --- reservations, RSVPs, surveys --------------------------------------------------------------


def _reserve_pavilion(sc, rng):
    home = _pick_owner(sc, rng)
    days_ahead = 5 + int(rng.random() * 20)
    hour = 10 + int(rng.random() * 6)

    def run():
        from community import reservations

        if "pavilion" not in sc.notes:
            return SKIP
        area = _get("community.CommonArea", sc.notes["pavilion"])
        start = (_now() + D(days=days_ahead)).replace(
            hour=hour + 4, minute=0, second=0, microsecond=0
        )
        reservations.request_reservation(
            area=area,
            lot_id=home.lot_id,
            starts_at=start,
            ends_at=start + D(hours=3),
            purpose="Birthday party (simulation)",
            actor=home.owners[0],
        )

    return run


def _decide_reservations(sc):
    def run():
        from community import reservations

        R = _model("community.Reservation")
        with system_access("simulation lookup"):
            waiting = list(R.objects.filter(state="pending"))
        for r in waiting:
            reservations.approve(r, actor=sc.world.staff_for(r, prefer_manager=True))
        return f"{len(waiting)} approved" if waiting else SKIP

    return run


def _picnic(sc, day):
    def run():
        from comms import rsvp
        from comms import services as comms

        picnic = comms.create_event(
            title="Community picnic (sim)",
            starts_at=_now() + D(days=14),
            location="Pavilion (fictional)",
            description="Bring a chair.",
            actor=sc.world.board[-1],
        )
        rsvp.save_settings(
            picnic, enabled=True, cap=80, max_guests=4, remind=True, actor=sc.world.board[-1]
        )
        sc.notes["picnic"] = picnic.pk
        sc.notes["picnic_slot"] = rsvp.add_slot(
            picnic, title="Bring a dessert", capacity=6, actor=sc.world.board[-1]
        ).pk

    return run


def _rsvp(sc, rng):
    home = _pick_owner(sc, rng)

    def run():
        from comms import rsvp

        if "picnic" not in sc.notes:
            return SKIP
        picnic = _get("comms.Event", sc.notes["picnic"])
        rsvp.answer(picnic, answer="going", guests=int(rng.random() * 3), actor=home.owners[0])

    return run


def _survey(sc, day):
    def run():
        from governance import surveys

        s = surveys.create_survey(
            title="Pool hours survey (sim)",
            intro="Informal: nothing is decided by it.",
            audience="owners",
            opens_on=day,
            closes_on=day + D(days=14),
            anonymous=True,
            share_results=True,
            actor=sc.world.board[-1],
        )
        q = surveys.add_question(
            s,
            kind="single",
            prompt="Best opening time?",
            choices=["7 am", "9 am", "11 am"],
            required=True,
            actor=sc.world.board[-1],
        )
        surveys.open_survey(s, actor=sc.world.board[-1])
        sc.notes["survey"], sc.notes["survey_q"] = s.pk, q.pk

    return run


def _survey_answers(sc, rng):
    homes = [h for h in sc.world.owner_homes() if h.owners]
    picks = [homes[int(rng.random() * len(homes))] for _ in range(3)]

    def run():
        from governance import surveys

        if "survey" not in sc.notes:
            return SKIP
        s = _get("governance.Survey", sc.notes["survey"])
        q = _get("governance.SurveyQuestion", sc.notes["survey_q"])
        choices = list(q.choices.all())
        n = 0
        for h in picks:
            user = h.owners[0]
            if surveys.has_responded(s, user):
                continue
            surveys.submit_response(
                s, answers={q.pk: [str(choices[int(rng.random() * len(choices))].pk)]}, actor=user
            )
            n += 1
        return f"{n} answer(s)" if n else SKIP

    return run


# --- annual meeting, election, board turnover --------------------------------------------------


def _annual(sc, day, m, d, am, rng):
    ev = sc.ev
    out = []
    if m == am and d == 1:
        out.append(ev("annual_meeting.create", day, 19, _annual_create(sc, am), area="governance"))
    if m == am and d == 3:
        out.append(ev("election.nominate", day, 19, _nominate(sc, am), area="governance"))
    if m == am and d == 5:
        out.append(ev("election.open", day, 19, _open_election(sc, am), area="governance"))
    if m == am and 6 <= d <= 17:
        out.append(
            ev("election.ballot", day, 20, _election_ballots(sc, am, rng), area="governance")
        )
    if m == am and d == 21:
        out.append(ev("election.certify", day, 19, _certify(sc, am), area="governance"))
    if m == am and d == 22:
        out.append(ev("board.turnover", day, 19, _turnover(sc, am), area="governance"))
    return out


def _annual_create(sc, am):
    def run():
        from governance import elections
        from governance import services as gov

        mtg = gov.create_meeting(
            title=f"Annual owners' meeting (sim, month {am})",
            kind="annual",
            starts_at=_now() + D(days=24),
            location="Community room (fictional)",
            agenda="1. Budget\n2. Election\n3. Open forum",
            actor=sc.world.board[-1],
        )
        gov.send_notice(mtg, actor=sc.world.board[-1])
        e = elections.create_election(
            title=f"Board election (sim, month {am})",
            seats=3,
            meeting=mtg,
            term_years=2,
            citation="Bylaws §4 (simulation)",
            actor=sc.world.board[0],
        )
        sc.notes[f"election{am}"] = e.pk

    return run


def _nominate(sc, am):
    def run():
        from governance import elections

        e = _get("governance.Election", sc.notes[f"election{am}"])
        # Owners not on the board stand (sitting members are mid-term; a candidate
        # takes no step on their own election).
        board = set(sc.world.board)
        homes = [
            h for h in sc.world.owner_homes("on_time") if h.owners and not board & set(h.owners)
        ][am % 3 :][:5]
        for h in homes:
            user = h.owners[0]
            cand = elections.nominate(
                e, person=user.person, statement="I will serve (simulation).", actor=user
            )
            elections.accept_nomination(cand, actor=user)
        return f"{len(homes)} candidates"

    return run


def _open_election(sc, am):
    def run():
        from governance import elections

        e = _get("governance.Election", sc.notes[f"election{am}"])
        candidates = {c.person_id for c in e.candidates.all()}
        actor = next(u for u in sc.world.board if u.person.pk not in candidates)
        elections.open_voting(
            e, opens_at=_now(), closes_at=_now() + D(days=12, hours=2), actor=actor
        )

    return run


def _election_ballots(sc, am, rng):
    homes = [h for h in sc.world.owner_homes() if h.owners]
    voters = [h for h in homes if rng.random() < 0.12]

    def run():
        from governance import elections

        e = _get("governance.Election", sc.notes[f"election{am}"])
        cands = list(elections.ballot_candidates(e))
        n = 0
        for h in voters:
            user = h.owners[0]
            if h.lot_id not in {r["lot"].pk for r in elections.lots_actor_may_vote_for(e, user)}:
                continue
            k = 1 + int(rng.random() * min(3, len(cands)))
            chosen = sorted(cands, key=lambda c: (c.pk * 7 + h.number) % 11)[:k]
            elections.cast_election_ballot(
                e, lot_id=h.lot_id, candidate_ids=[c.pk for c in chosen], actor=user
            )
            n += 1
        return f"{n} ballot(s)" if n else SKIP

    return run


def _certify(sc, am):
    def run():
        from governance import elections

        e = _get("governance.Election", sc.notes[f"election{am}"])
        candidates = (
            {c.person_id for c in e.candidates.all()} if hasattr(e, "candidates") else set()
        )
        actor = next(
            (u for u in sc.world.board if u.person.pk not in candidates), sc.world.board[0]
        )
        result = elections.certify(e, actor=actor)
        sc.notes[f"elected{am}"] = [
            r["candidate"].person_id for r in result["rows"] if r["elected"]
        ]
        return f"{result['ballots']} ballots"

    return run


def _turnover(sc, am):
    """The elected take their seats (each proposal approved by a sitting board
    member other than the proposer); at the first annual meeting the
    declarant's appointees leave and the new board elects its officers. Every
    step is a new member's, never one's own (no self-grants)."""

    def run():
        from community import services
        from community.models import BoardSeat, BoardSeatNomination, OfficeHolding

        today = timezone.localdate()
        with system_access("simulation lookup"):
            pending = list(BoardSeatNomination.objects.filter(decided_at__isnull=True))
        for nom in pending:
            approver = next(
                u
                for u in sc.world.board
                if u.pk != nom.nominated_by_id and u.person.pk != nom.person_id
            )
            services.decide_seat_nomination(nom, approve=True, actor=approver)
        sc.world.refresh()
        elected = set(sc.notes.get(f"elected{am}", []))
        newcomers = [u for u in sc.world.board if u.person.pk in elected]
        if len(newcomers) < 2:
            return f"{len(newcomers)} seated" if pending else SKIP
        ended = 0
        if am == annual_months(sc)[0]:
            with system_access("simulation lookup"):
                outgoing = [
                    s
                    for s in BoardSeat.objects.active().select_related("person")
                    if s.seat_type == "declarant_appointed"
                ]
            for seat in outgoing:
                with system_access("simulation lookup"):
                    offices = list(OfficeHolding.objects.active().filter(person=seat.person))
                for h in offices:
                    services.end_office(h, end_date=today, actor=newcomers[0])
                services.end_board_seat(seat, reason="replaced", ended_on=today, actor=newcomers[0])
                ended += 1
        with system_access("simulation lookup"):
            held = {h.office for h in OfficeHolding.objects.active()}
            officers = set(OfficeHolding.objects.active().values_list("person_id", flat=True))
        free = [o for o in ("president", "treasurer", "secretary") if o not in held]
        recorded = 0
        for user in newcomers:
            if not free or user.person.pk in officers:
                continue
            other = next(u for u in newcomers if u != user)
            services.record_office(
                person=user.person, office=free.pop(0), start_date=today, actor=other
            )
            recorded += 1
        sc.world.refresh()
        return f"{len(newcomers)} seated, {ended} ended, {recorded} office(s)"

    return run


# --- the special assessment vote ----------------------------------------------------------------


def _special_vote(sc, day):
    title = "Entrance road repaving (sim)"
    total = 4400 * 100 * len(sc.world.homes) // 4

    def run():
        from core.money import format_cents
        from governance import services as gov

        mtg = gov.create_meeting(
            title="Special owners' meeting: road repaving (sim)",
            kind="special",
            starts_at=_now() + D(days=20),
            location="Community room (fictional)",
            agenda="1. Special assessment",
            actor=sc.world.board[-1],
        )
        gov.send_notice(mtg, actor=sc.world.board[-1])
        vote = gov.create_vote(
            title=f"Approve the special assessment {title} of {format_cents(total)}",
            kind="formal",
            options=["Approve", "Reject"],
            secret=True,
            meeting=mtg,
            opens_at=_now() + D(hours=1),
            closes_at=_now() + D(days=14),
            actor=sc.world.board[0],
        )
        gov.open_vote(vote, actor=sc.world.board[0])
        sc.notes.update(special_vote=vote.pk, special_title=title, special_total=total)

    return run


def _cast_votes(sc, rng):
    homes = [h for h in sc.world.owner_homes() if h.owners]
    voters = [h for h in homes if rng.random() < 0.15]

    def run():
        from governance import services as gov

        if "special_vote" not in sc.notes:
            return SKIP
        vote = _get("governance.Vote", sc.notes["special_vote"])
        options = list(vote.options.all())
        n = 0
        for h in voters:
            user = h.owners[0]
            mine = gov.my_ballots(vote, user)
            if mine:
                continue
            pick = options[0] if rng.random() < 0.8 else options[1]
            try:
                gov.cast_ballot(vote, option_id=pick.pk, lot_id=h.lot_id, actor=user)
            except Exception as exc:
                if "already" in str(exc).lower():
                    continue
                raise
            n += 1
        return f"{n} ballot(s)" if n else SKIP

    return run


def _special_result(sc):
    def run():
        from ledger import special_assessments as sa

        vote = _get("governance.Vote", sc.notes["special_vote"])
        from community.rules import rule

        problem = sa.vote_approval_problem(
            vote,
            title=sc.notes["special_title"],
            total_cents=sc.notes["special_total"],
            percent=rule("special_assessment.vote_percent"),
        )
        sc.notes["special_vote_problem"] = problem or ""
        if not problem:
            sc.notes["special_vote_passed"] = True
            return "approved"
        return f"not approved: {problem}"

    return run


# --- home sales ----------------------------------------------------------------------------------


def _sale_home(sc, sale_month):
    homes = [
        h
        for h in sc.world.owner_homes("on_time")
        if h.owners and h.number not in sc.world.tenant_lots
    ]
    return homes[-1 - (sale_month % 3)]


def _resale_request(sc, sale_month):
    def run():
        from ledger import services as ledger

        home = _sale_home(sc, sale_month)
        sc.notes[f"sale{sale_month}"] = home.number
        r = ledger.request_resale(
            lot=_lot(home),
            requester_name="Example Title Co (sim)",
            requester_email="title@example.com",
            expected_settlement=timezone.localdate() + D(days=17),
            actor=home.owners[0],
        )
        sc.notes["resale"] = r.pk

    return run


def _resale_issue(sc):
    def run():
        from ledger import services as ledger

        r = _get("ledger.ResaleRequest", sc.notes["resale"])
        staff = sc.world.staff_for(r, prefer_manager=True)
        ledger.RESALE.apply(r, "start", actor=staff)
        ledger.save_disclosures(r, "No pending litigation (simulation).", actor=staff)
        issuer = sc.world.staff_for(r)
        ledger.RESALE.apply(r, "issue", actor=issuer, note="Issued (simulation)")

    return run


def _sale(sc, sale_month):
    def run():
        from community import services
        from ledger import accounts
        from ledger import services as ledger

        number = sc.notes[f"sale{sale_month}"]
        home = sc.world.homes[number]
        lot = _lot(home)
        with system_access("simulation lookup"):
            seller_account = accounts.account_on(lot.pk, timezone.localdate())
            seller_before = ledger.balance(lot.pk, account=seller_account)
        buyer = sc.world.new_person("Buyer")
        actor = sc.world.staff_for(lot)
        services.transfer_lot(
            lot, new_owners=[buyer], transfer_date=timezone.localdate(), actor=actor
        )
        with system_access("simulation lookup"):
            seller_after = ledger.balance(lot.pk, account=seller_account)
            buyer_balance = ledger.balance(lot.pk, account=accounts.CURRENT)
        if seller_after != seller_before:
            raise AssertionError(f"seller balance moved: {seller_before} -> {seller_after}")
        if buyer_balance != 0:
            raise AssertionError(f"buyer starts owing {buyer_balance}")
        sc.world.sold.append((number, timezone.localdate().isoformat()))
        sc.world.refresh()
        return f"lot {number} sold; seller keeps {seller_before}"

    return run


# --- vendor onboarding -------------------------------------------------------------------------


def _onboard_invite(sc):
    def run():
        from operations import vendor_onboarding as vo

        vendor = vo.invite_vendor(
            name="Brightline Electric (sim)",
            email="brightline@example.com",
            services="Entrance lighting",
            vendor_type="contractor",
            actor=sc.world.manager,
        )
        sc.notes["onboard_vendor"] = vendor.pk

    return run


def _onboard_submit(sc):
    """The vendor fills in the form behind its link (the one anonymous page, D79)."""

    def run():
        from operations import vendor_onboarding as vo
        from operations.demo_vendor_onboarding import _answers, _fake_pdf

        vendor = _get("operations.Vendor", sc.notes["onboard_vendor"])
        rnd = vendor.onboarding_rounds.get()
        vo.save_answers(
            rnd, _answers("Brightline Electric (sim)", "smllc", timezone.localdate() + D(days=365))
        )
        rnd.refresh_from_db()
        for key in vo.round_items(rnd):
            item = vo.ITEMS[key]
            if item.file_kind:
                vo.upload_file(rnd, item.file_kind, _fake_pdf(item.file_kind, vendor.name))
        rnd.refresh_from_db()
        vo.submit(rnd)
        sc.notes["onboard_round"] = rnd.pk

    return run


def _onboard_approve(sc):
    def run():
        from operations import vendor_onboarding as vo

        rnd = _get("operations.OnboardingRound", sc.notes["onboard_round"])
        n = 0
        for u in sc.world.board:
            if vo.approval_refusal(rnd, u) is None:
                vo.approve(rnd, actor=u, note="Documents in order (simulation)")
                n += 1
                rnd.refresh_from_db()
            if n == 2:
                break
        return f"{n} approval(s)"

    return run


# --- insurance claim, capital project, renewals ---------------------------------------------------


def _claim(sc, day):
    def run():
        from operations import insurance_claims as ic

        claim = ic.create(
            actor=sc.world.manager,
            carrier="Example Mutual Insurance (sim)",
            policy_number="SIM-0001",
            incident_on=day - D(days=2),
            discovered_on=day - D(days=1),
            description="A storm took down the entrance sign (simulation).",
            location="Entrance",
            estimate_cents=950_000,
            deductible_cents=100_000,
        )
        ic.change_state(claim, "review", actor=sc.world.manager)
        sc.notes["claim"] = claim.pk

    return run


def _claim_settle(sc):
    def run():
        from operations import insurance_claims as ic

        claim = _get("operations.InsuranceClaim", sc.notes["claim"])
        offer = ic.record_offer(
            claim, amount_cents=850_000, note="Adjuster's offer", actor=sc.world.manager
        )
        used = set()
        for _ in range(ic.approvals_needed(850_000)):
            actor = next(u for u in sc.world.board if u not in used)
            ic.approve_offer(offer, actor=actor)
            used.add(actor)
        ic.record_money(
            claim,
            kind="expected",
            amount_cents=850_000,
            on=timezone.localdate(),
            reference="SIM-CLM",
            actor=sc.world.manager,
        )

    return run


def _project(sc):
    def run():
        from operations import capital_projects as cp

        p = cp.create(
            title="Entrance sign rebuild (sim)",
            budget_cents=1_200_000,
            funding="reserve",
            description="Rebuild the sign the storm took down (simulation).",
            actor=sc.world.manager,
        )
        cp.ask_approval(p, resolution_ref="Resolution (simulation)", actor=sc.world.manager)
        used = set()
        for _ in range(cp.approvals_needed(p.budget_cents)):
            actor = next(u for u in sc.world.board if u not in used)
            cp.approve(p, actor=actor)
            used.add(actor)
        p.refresh_from_db()
        cp.change_state(p, "start", actor=sc.world.board[0])
        sc.notes["project"] = p.pk

    return run


def _project_complete(sc):
    def run():
        from operations import capital_projects as cp

        p = _get("operations.CapitalProject", sc.notes["project"])
        inv_ids = sorted(sc.notes.get("reserve_invoices", set()))
        if inv_ids:
            cp.link(
                p, kind="invoice", object_id=inv_ids[-1], committed_cents=0, actor=sc.world.manager
            )
        cp.change_state(p, "complete", actor=sc.world.board[0], note="Done (simulation)")

    return run


def _renewals(sc, day):
    """Contracts and the insurance policy coming up for renewal: the board
    decides each contract term once, the insurance renews before it ends."""

    def run():
        from documents import renewals
        from operations.models import Contract

        done = []
        with system_access("simulation lookup"):
            contracts = list(Contract.objects.filter(pk__in=sc.notes.get("contracts", [])))
        decided = sc.notes.setdefault("renewal_decided", set())
        for c in contracts:
            if c.pk in decided or not c.ends_on or (c.ends_on - day).days > 45:
                continue
            renewals.decide_contract(
                c, decision="renew", note="Renew (simulation)", actor=sc.world.board[0]
            )
            decided.add(c.pk)
            done.append(f"contract {c.pk}")
        pol = (
            _get("documents.InsurancePolicy", sc.notes["policy"])
            if sc.notes.get("policy")
            else None
        )
        if (
            pol is not None
            and (pol.ends_on - day).days <= 20
            and not hasattr(pol, "renewed_by_cached")
        ):
            new = renewals.record_policy(
                actor=sc.world.board[0],
                renews=pol,
                kind=pol.kind,
                carrier=pol.carrier,
                policy_number="SIM-0002",
                coverage_cents=pol.coverage_cents,
                premium_cents=440_000,
                starts_on=pol.ends_on + D(days=1),
                ends_on=pol.ends_on + D(days=365),
                file=setup._pdf("Insurance renewal"),
            )
            sc.notes["policy"] = new.pk
            done.append("insurance renewed")
        return ", ".join(done) if done else SKIP

    return run


# --- probes ------------------------------------------------------------------------------------


def _probe_manager(sc):
    def run():
        from operations import services as ops
        from operations.models import Invoice

        with system_access("simulation lookup"):
            inv = Invoice.objects.filter(state="pending").first()
        if inv is None:
            inv = ops.create_invoice(
                vendor=_get("operations.Vendor", next(iter(sc.notes["vendors"].values()))[0]),
                number=f"PROBE-{timezone.localdate():%y%m%d}",
                invoice_date=timezone.localdate(),
                amount_cents=12_000,
                category=setup._cats()["5800"],
                actor=sc.world.manager,
            )
            ops.INVOICE.apply(inv, "submit", actor=sc.world.manager)
        ops.approve(inv, note="Manager approving (must be refused)", actor=sc.world.manager)

    return run


def _probe_own_home(sc):
    """A board member who owns a home tries a staff step on it: a charge."""

    def run():
        from ledger import services as ledger

        for u in sc.world.board:
            for h in sc.world.homes.values():
                if u in h.owners:
                    ledger.post_charge(
                        lot=_lot(h),
                        kind="other_charge",
                        amount_cents=100,
                        description="Own home (probe)",
                        actor=u,
                    )
                    return "allowed"
        # Before any owner sits on the board, the probe is an owner with no staff powers.
        h = next(h for h in sc.world.owner_homes() if h.owners)
        ledger.post_charge(
            lot=_lot(h),
            kind="other_charge",
            amount_cents=100,
            description="Probe",
            actor=h.owners[0],
        )

    return run


def _probe_recused(sc):
    """An ARC reviewer next door to the applicant tries to decide its request."""

    def run():
        from arc import services as arc
        from core import conflicts

        Req = _model("arc.ArcRequest")
        with system_access("simulation lookup"):
            reqs = list(Req.objects.all())
        for req in reqs:
            for u in sc.world.arc_reviewers:
                if conflicts.own_matter(u, req):
                    arc.decide(req, outcome="approved", citation="Probe", actor=u)
                    return "allowed"
        # No recused reviewer this run: the probe is a neighbor owner deciding.
        req = reqs[0]
        home = sc.world.homes[req.lot.number]
        arc.decide(req, outcome="approved", citation="Probe", actor=home.owners[0])

    return run


__all__ = ["_receipt", "due_now", "plan"]
