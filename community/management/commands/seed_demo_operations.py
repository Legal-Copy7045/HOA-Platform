"""Fictional demo data for the Milestone 2 modules, on top of seed_demo.

    python manage.py seed_demo_operations

Needs SEED_DEMO_ALLOWED=true and an existing (demo) community. Runs once:
refuses if vendors already exist. Every governing rule it sets is cited
"DEMO - replace" so nobody mistakes it for the real Declaration.
"""

import datetime

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from community import services as community
from community.models import BoardSeat, CommonArea, Lot, Ownership, Person, today
from core import pdf
from core.policy import SYSTEM
from core.scoping import system_access

DEMO = "DEMO - replace"


def _login_for(person: Person) -> User:
    """Give a seeded person a login without a password or authenticator, on
    the email of their Person record, so the board can send them a new setup
    link (D59) when a tester wants to sign in as them."""
    from community.demo.states import login_for

    return login_for(person)


class Command(BaseCommand):
    help = "Create fictional demo data for documents, governance, ARC, finance, reports."

    def handle(self, *args, **opts):
        from operations.models import Vendor

        if not settings.SEED_DEMO_ALLOWED:
            raise CommandError("Refusing: set SEED_DEMO_ALLOWED=true (never in production).")
        with system_access("seed checks"):
            if not Lot.objects.exists():
                raise CommandError("Run seed_demo first (no lots yet).")
            from community.models import Ownership

            real = (
                Ownership.objects.active().exclude(person__display_name__endswith="(demo)").exists()
            )
            if real:
                raise CommandError(
                    "Refusing: some lots have owners who are not demo people. Demo data is "
                    "only added to a community created by seed_demo."
                )
        if Vendor.objects.exists():
            raise CommandError("Operations demo data already exists.")
        with transaction.atomic(), system_access("seed_demo_operations"):
            self._seed()
        self.stdout.write(
            self.style.SUCCESS(
                "Demo operations data created. Governing rules are cited 'DEMO - replace'."
            )
        )

    def _seed(self):
        from arc import services as arc
        from comms import services as comms
        from compliance import services as compliance
        from documents import services as documents
        from governance import services as governance
        from ledger import services as ledger
        from operations import services as ops
        from operations.models import ExpenseCategory

        t = today()
        seats = list(BoardSeat.objects.active().select_related("person")[:3])
        board = [_login_for(s.person) for s in seats]
        b1, b2 = board[0], board[1] if len(board) > 1 else board[0]

        def staff_for(matter):
            # Own matters (D65): a board member never takes a staff step on
            # their own home's matter, so the demo has another one do it.
            from core import conflicts

            return next((u for u in board if not conflicts.own_matter(u, matter)), SYSTEM)

        lots = {lot.number: lot for lot in Lot.objects.all()}
        owner_of = {
            o.lot.number: o.person
            for o in Ownership.objects.active().select_related("lot", "person")
        }
        owner3 = _login_for(owner_of[3])
        owner12 = _login_for(owner_of[12])

        rules = {
            "dues.monthly_cents": "4500",
            "late_fee.cents": "600",
            "late_fee.grace_days": "15",
            "arc.completeness_days": "10",
            "arc.review_days": "45",
            "arc.deemed_approval": "true",
            "arc.appeal_days": "30",
            "violation.cure_days": "14",
            "violation.hearing_notice_days": "10",
            "meeting.owner_notice_days": "10",
            "records.response_days": "30",
            "resale.fee_cents": "15000",
            "resale.deadline_days": "10",
            "vote.quorum_percent": "20",
            "vote.pass_percent": "51",
        }
        from community.models import GoverningRule

        for key, value in rules.items():
            if not GoverningRule.objects.filter(key=key).exclude(value="").exists():
                community.set_rule(key=key, value=value, citation=DEMO, actor=SYSTEM)

        for name, kind in (
            ("North detention pond", "stormwater"),
            ("Entrance sign and beds", "landscaping"),
        ):
            if not CommonArea.objects.filter(name=name).exists():
                CommonArea.objects.create(name=name, kind=kind)
        pond = CommonArea.objects.get(name="North detention pond")

        cats = {}
        for code, name in (
            ("5100", "Pond and stormwater"),
            ("5200", "Landscaping"),
            ("5300", "Insurance"),
            ("5400", "Legal and professional"),
            ("5500", "Management"),
            ("5600", "Utilities"),
        ):
            cats[code] = ExpenseCategory.objects.get_or_create(code=code, name=name)[0]

        pond_pros = ops.save_vendor(
            name="Pond Pros (demo)",
            contact_name="Sam Example",
            email="sam@example.com",
            services="Pond maintenance",
            insurance_expires_on=t + datetime.timedelta(days=20),
            w9_on_file=True,
            actor=b1,
        )
        green = ops.save_vendor(
            name="Greenway Lawn (demo)",
            services="Mowing, beds",
            insurance_expires_on=t + datetime.timedelta(days=200),
            w9_on_file=True,
            actor=b1,
        )
        ops.save_vendor(name="Fictional Insurance Co (demo)", services="D&O, GL", actor=b1)

        wo = ops.create_work_order(
            title="Clear north pond outlet",
            common_area=pond,
            vendor=pond_pros,
            priority="urgent",
            actor=b1,
        )
        ops.WORK_ORDER.apply(wo, "assign", actor=b1)
        ops.WORK_ORDER.apply(wo, "complete", actor=b1, note="Outlet cleared (demo)")
        ops.create_work_order(title="Spring mulch for entrance beds", vendor=green, actor=b1)

        def invoice(vendor, number, cents, code, state):
            inv = ops.create_invoice(
                vendor=vendor,
                number=number,
                invoice_date=t.replace(day=1),
                amount_cents=cents,
                category=cats[code],
                actor=b1,
            )
            if state == "draft":
                return inv
            ops.INVOICE.apply(inv, "submit", actor=b1)
            if state in ("approved", "paid"):
                inv = ops.approve(inv, actor=b2)
                if inv.state != "approved" and len(board) > 2:
                    inv = ops.approve(inv, actor=board[2])
            if state == "paid" and inv.state == "approved":
                ops.record_payment(
                    inv, paid_on=t, method="check", amount_cents=cents, reference="1001", actor=b1
                )
            return inv

        invoice(pond_pros, "PP-101", 32000, "5100", "paid")
        invoice(green, "GL-55", 41500, "5200", "approved")
        invoice(pond_pros, "PP-102", 125000, "5100", "pending")
        invoice(green, "GL-56", 9000, "5200", "draft")

        budget = ledger.create_budget(fiscal_year=t.year, actor=b1)
        for kind, name, cents, extra in (
            ("income", "Assessments", 4500 * 12 * len(lots), {"income_source": "assessments"}),
            ("expense", "Pond and stormwater", 600000, {"category": cats["5100"]}),
            ("expense", "Landscaping", 900000, {"category": cats["5200"]}),
            ("expense", "Insurance", 350000, {"category": cats["5300"]}),
            ("expense", "Management", 480000, {"category": cats["5500"]}),
        ):
            ledger.add_line(budget, kind=kind, name=name, annual_cents=cents, actor=b1, **extra)
        ledger.adopt_budget(budget, actor=b1)

        # Reserve plan (synthetic figures): a few components, settings, adopted.
        from ledger import reserve_plan

        reserve_plan.save_settings(
            starting_balance_cents=4200000,
            balance_as_of=datetime.date(t.year, 1, 1),
            annual_contribution_cents=1500000,
            inflation_bp=300,
            interest_bp=150,
            horizon_years=30,
            actor=b1,
        )
        for name, category, quantity, cost, life, left in (
            ("Main road paving (demo)", "roads", "4,800 sq yd", 26000000, 25, 9),
            ("Road sealcoat and crack fill (demo)", "roads", "4,800 sq yd", 1800000, 5, 1),
            ("Pond dredging (demo)", "stormwater", "1 pond", 6000000, 20, 12),
            ("Stormwater inlets (demo)", "stormwater", "14 inlets", 2100000, 30, 18),
            ("Entrance sign (demo)", "signs", "2 signs", 1200000, 20, 4),
            ("Split-rail fence (demo)", "fencing", "900 ft", 1350000, 20, 7),
            ("Entrance lighting (demo)", "lighting", "6 fixtures", 900000, 15, 0),
        ):
            reserve_plan.save_component(
                name=name,
                category=category,
                quantity=quantity,
                replacement_cost_cents=cost,
                useful_life_years=life,
                remaining_life_years=left,
                notes="Demo estimate, not a real bid.",
                actor=b1,
            )
        reserve_plan.adopt(actor=b1)

        for month in range(1, t.month + 1):
            ledger.post_monthly_assessments(year=t.year, month=month, actor=b1)
        for number, lot in lots.items():
            if number == 9:  # prepaid the whole year
                ledger.record_payment(
                    lot=lot,
                    amount_cents=4500 * 12,
                    received_on=t.replace(month=1, day=1) - datetime.timedelta(days=5),
                    reference="2001",
                    actor=staff_for(lot),
                )
            elif number in (15, 27):  # behind on dues
                ledger.record_payment(
                    lot=lot,
                    amount_cents=4500 * max(t.month - 3, 0) or 4500,
                    received_on=t.replace(day=1),
                    reference="3001",
                    actor=staff_for(lot),
                )
            else:
                ledger.record_payment(
                    lot=lot,
                    amount_cents=4500 * t.month,
                    received_on=t.replace(day=1),
                    reference="4001",
                    actor=staff_for(lot),
                )
        # Open-item accounting (D32-payments): an invoice partly paid and directed.
        gate = ledger.post_charge(
            lot=lots[3],
            kind="other_charge",
            amount_cents=6000,
            description="Gate remote replacement (demo)",
            actor=staff_for(lots[3]),
        )
        ledger.record_payment(
            lot=lots[3],
            amount_cents=2500,
            received_on=t,
            reference="5001",
            apply_to=[gate.pk],
            actor=staff_for(lots[3]),
        )
        ledger.open_case(lot=lots[27], actor=staff_for(lots[27]))

        comms.publish_announcement(
            title="Pond outlet cleared (demo)",
            body="The north pond outlet was cleared this week.",
            actor=b1,
        )
        comms.publish_announcement(
            title="Annual meeting date set (demo)",
            body="See the calendar and meeting page.",
            actor=b1,
        )
        # The annual meeting's calendar entry is made with the meeting below.
        start = timezone.now() + datetime.timedelta(days=21)
        # Event RSVPs and volunteer sign-ups (comms.rsvp).
        from comms import rsvp

        picnic = comms.create_event(
            title="Community picnic (demo)",
            starts_at=timezone.now() + datetime.timedelta(days=10),
            location="Pavilion (fictional)",
            description="Burgers and lemonade provided. Bring a chair.",
            actor=b1,
        )
        rsvp.save_settings(picnic, enabled=True, cap=80, max_guests=4, remind=True, actor=b1)
        dessert = rsvp.add_slot(picnic, title="Bring a dessert", capacity=6, actor=b1)
        rsvp.add_slot(picnic, title="Set up tables 9-10am", capacity=3, actor=b1)
        rsvp.answer(picnic, answer="going", guests=2, actor=owner3)
        rsvp.answer(picnic, answer="maybe", actor=owner12)
        rsvp.sign_up(dessert, actor=owner3)
        annual_meeting = governance.create_meeting(
            title="Annual owners' meeting (demo)",
            kind="annual",
            starts_at=start,
            location="Community room (fictional)",
            agenda="1. Budget\n2. Elections\n3. Pond",
            actor=b1,
        )
        governance.record_resolution(
            kind="policy",
            title="Trash can storage (demo)",
            text="Cans out of street view except collection day.",
            adopted_on=t.replace(month=1, day=10),
            votes_for=5,
            actor=b1,
        )
        # A quick poll is a one-question survey (informal polls became surveys).
        from governance import surveys as gov_surveys

        poll = gov_surveys.create_survey(
            title="Add a fountain to the north pond? (demo)",
            audience="owners",
            opens_on=t,
            closes_on=t + datetime.timedelta(days=14),
            actor=b1,
        )
        gov_surveys.add_question(
            poll,
            kind="single",
            prompt="Should the board get quotes for a fountain?",
            choices=["Yes", "No", "No opinion"],
            required=True,
            actor=b1,
        )
        gov_surveys.open_survey(poll, actor=b1)
        # A formal vote for the annual meeting, still a draft, with its
        # voting window planned (D61): the board opens it from its page.
        vote_opens = timezone.now() + datetime.timedelta(days=1)
        governance.create_vote(
            title="Adopt the updated pet rules (demo)",
            kind="formal",
            options=["Yes", "No"],
            meeting=annual_meeting,
            opens_at=vote_opens,
            closes_at=vote_opens + datetime.timedelta(days=14),
            actor=b1,
        )
        # An anonymous survey (M5), open for two weeks, with two answers.
        survey = gov_surveys.create_survey(
            title="Pool and clubhouse survey (demo)",
            intro="Help us plan next season. Informal: nothing is decided by it.",
            audience="residents",
            opens_on=t,
            closes_on=t + datetime.timedelta(days=14),
            anonymous=True,
            share_results=True,
            actor=b1,
        )
        q_time = gov_surveys.add_question(
            survey,
            kind="single",
            prompt="Best pool opening time?",
            choices=["7 am", "9 am", "11 am"],
            required=True,
            actor=b1,
        )
        q_rate = gov_surveys.add_question(
            survey, kind="rating", prompt="How do you rate the clubhouse?", actor=b1
        )
        gov_surveys.add_question(survey, kind="text", prompt="Anything else?", actor=b1)
        gov_surveys.open_survey(survey, actor=b1)
        for who, pick, stars in ((owner3, 0, 4), (owner12, 1, 3)):
            gov_surveys.submit_response(
                survey,
                answers={
                    q_time.pk: [str(list(q_time.choices.all())[pick].pk)],
                    q_rate.pk: [str(stars)],
                },
                actor=who,
            )
        governance.submit_records_request(description="2025 reserve study (demo)", actor=owner12)

        req = arc.create_request(
            lot=lots[3],
            title="Rear fence (demo)",
            description="4 ft black aluminum, rear yard",
            actor=owner3,
        )
        arc.ARC.apply(req, "submit", actor=owner3)
        arc.ARC.apply(req, "accept", actor=staff_for(req))
        v = compliance.report(
            lot=lots[12], category="trash", description="Cans at curb (demo)", actor=b1
        )
        compliance.VIOLATION.apply(v, "send_notice", actor=staff_for(v))
        ledger.request_resale(lot=lots[12], requester_name="Demo Seller", actor=owner12)
        ops.create_task(title="Get second pond maintenance quote (demo)", actor=b1)
        compliance.set_follow_up(v, today() + datetime.timedelta(days=7), actor=staff_for(v))
        ops.set_work_order_follow_up(wo, today(), actor=b1)

        # Owner requests with threads (Milestone 3).
        from comms import threads

        leak = ops.submit_request(
            lot=lots[3],
            request_type="common_area",
            subject="Pond fountain not running (demo)",
            description="The fountain has been off for a week.",
            actor=owner3,
        )
        leak_staff = staff_for(leak)
        threads.post(
            "request", leak, body="Thanks, we've called the vendor (demo).", actor=leak_staff
        )
        threads.post(
            "request",
            leak,
            body="Vendor quote pending (internal demo note).",
            internal=True,
            actor=leak_staff,
        )
        # A board-approved recurring contract (Milestone 3).
        mowing = ops.create_contract(
            vendor=green,
            title="Common area mowing (demo)",
            category=ExpenseCategory.objects.filter(active=True).first(),
            schedule="monthly",
            amount_cap_cents=85000,
            starts_on=t.replace(month=1, day=1),
            citation="Board resolution (demo)",
            actor=b1,
        )
        # Two board members other than the one who entered it (cap over the
        # two-approver threshold, review SEC-002).
        for approver in [u for u in board if u is not b1][:2]:
            if mowing.state == "draft":
                mowing = ops.approve_contract(
                    mowing, note="Approved at the January meeting (demo)", actor=approver
                )

        # FAQ and welcome message (Milestone 3).
        for category, question, answer in [
            (
                "Trash and recycling",
                "When is trash collected? (demo)",
                "Tuesday mornings; recycling every other Tuesday.",
            ),
            (
                "Architectural review",
                "Do I need approval for a fence? (demo)",
                "Yes. Apply under My home improvement requests (ARC) before work starts.",
            ),
            (
                "Assessments",
                "When is the monthly assessment due? (demo)",
                "On the 1st of each month. Paying ahead is kept as a credit.",
            ),
        ]:
            comms.save_faq(question=question, answer=answer, category=category, actor=b1)
        community.update_profile(
            legal_name="Oak Creek Farms Homeowners Association",
            fiscal_year_start_month=1,
            welcome_message="Welcome to the demo portal. Everything here is fictional.",
            arc_form_return_to="Please return the completed form to Example Management Co. "
            "(demo), 1 Example Road, or arc@example.com",
            actor=b1,
        )
        community.update_letter_settings(
            letterhead="Oak Creek Farms Homeowners Association\nc/o Example Management Co. (demo)"
            "\n1 Example Road, Exampletown, PA 00000",
            remit_to="Oak Creek Farms HOA (demo)\nPO Box 0000\nExampletown, PA 00000",
            letter_contact="Example Management Co. (demo) at 555-0100",
            letter_signature="Example Management Co. (demo)\nFor the Board of Directors",
            resale_insurance_company="Example Mutual Insurance (demo)",
            resale_insurance_cents=100_000_000,
            actor=b1,
        )

        # Home neighbors from the (fictional) development plan: next-door lots
        # on each of the two roads (Milestone 3 phase 7).
        from arc import signoffs as arc_signoffs

        for number, lot in lots.items():
            same_road = range(1, 23) if number <= 22 else range(23, 45)
            arc_signoffs.set_neighbors(
                lot,
                left=lots.get(number - 1) if number - 1 in same_road else None,
                right=lots.get(number + 1) if number + 1 in same_road else None,
                actor=b1,
            )

        # Compliance calendar suggestions and an annual election (Milestone 4).
        ops.add_starter_items(actor=b1)
        from governance import elections as gov_elections

        gov_elections.create_election(
            title="Annual board election (demo)",
            seats=2,
            citation="Bylaws (demo)",
            meeting=annual_meeting,
            actor=b1,
        )

        # ARC project types owners choose from (Milestone 3).
        from arc import services as arc_services

        # The migration already adds a starter list; only fill an empty one.
        for order, (name, guidance) in enumerate(
            []
            if arc_services.active_project_types()
            else [
                ("Fence", "Plot plan with location, height, material and color."),
                ("Shed or outbuilding", "Plot plan, size, materials and colors."),
                ("Deck or patio", "Drawing with dimensions, materials and setbacks."),
                ("Exterior paint or color change", "Color samples for body, trim and doors."),
                ("Roofing", "Material and color."),
                ("Landscaping or hardscaping", "Sketch of the area and plant list."),
                ("Solar panels", "Panel layout and mounting details."),
                ("Other", "Describe the project and attach drawings or photos."),
            ]
        ):
            arc_services.save_project_type(
                name=name,
                guidance=guidance,
                citation="ARC guidelines (demo)",
                sort_order=(order + 1) * 10,
                actor=b1,
            )

        # Committees with a space (Milestone 3).
        social = governance.create_committee(
            name="Social",
            purpose="Plans community events (demo).",
            citation="Board resolution (demo)",
            actor=b1,
        )
        governance.appoint_committee_member(social, person=owner_of[3], role="chair", actor=b1)
        governance.appoint_committee_member(social, person=owner_of[12], actor=b1)
        governance.start_topic(
            social, title="Summer picnic (demo)", body="Date ideas for the picnic?", actor=owner3
        )
        governance.add_committee_task(social, title="Book the pavilion (demo)", actor=owner3)
        governance.create_committee(
            name="Landscaping", purpose="Common area plantings (demo).", actor=b1
        )
        documents.create_folder(name="Pool and amenities", visibility="owners", actor=b1)
        documents.create_folder(name="Legal (board)", visibility="board", actor=b1)
        ops.submit_request(
            lot=lots[12],
            request_type="billing",
            subject="Question about my statement (demo)",
            description="Is the March assessment payment on my account?",
            actor=owner12,
        )

        declaration = pdf.render(
            [
                ("heading", "Declaration (FICTIONAL DEMO)"),
                ("normal", "This is placeholder text, not a real document."),
            ]
        )
        documents.create_document(
            title="Declaration (demo placeholder)",
            category="governing",
            visibility="owners",
            actor=b1,
            file=SimpleUploadedFile("declaration.pdf", declaration),
        )

        # Meeting packets (M5): last month's board meeting with minutes, and
        # both packets for this month's.
        from governance import packets

        october = governance.create_meeting(
            title="Last month's board meeting (demo)",
            kind="board",
            location="Community room (fictional)",
            starts_at=timezone.now() - datetime.timedelta(days=30),
            agenda="1. Minutes\n2. Pond",
            actor=b1,
        )
        governance.set_meeting_status(october, "held", actor=b1)
        minutes = documents.create_document(
            title="Board meeting minutes (demo)",
            category="minutes",
            visibility="owners",
            actor=b1,
            file=SimpleUploadedFile(
                "minutes.pdf",
                pdf.render([("heading", "Minutes (FICTIONAL DEMO)"), ("normal", "Placeholder.")]),
            ),
        )
        governance.attach_minutes(october, minutes, actor=b1)
        november = governance.create_meeting(
            title="Board meeting (demo)",
            kind="board",
            location="Community room (fictional)",
            join_url="https://meet.example.com/demo-board-meeting",
            starts_at=timezone.now() + datetime.timedelta(days=10),
            agenda="1. Call to order\n2. Approve last month's minutes\n3. Treasurer's report"
            "\n4. Pond outlet (demo)",
            actor=b1,
        )
        packets.build_packet(november, variant="board", minutes_id=minutes.pk, actor=b1)
        packets.build_packet(november, variant="owner", actor=b1)
        # Bank feed (optional feature "bank_feeds", off until the board turns
        # it on): a statement-file account with fictional lines. The check
        # line matches invoice PP-101's payment and is offered as a match.
        from ledger.bankfeeds import services as bank_feed
        from ledger.bankfeeds.base import TransactionInfo

        bank = bank_feed.add_file_account(
            display_name="Operating checking (demo)",
            institution_name="Demo Community Bank",
            last_four_digits="0000",
            actor=b1,
        )
        bank_feed.store_lines(
            bank,
            [
                TransactionInfo("demo:1", t, -32000, "CHECK 1001 (demo)", "check"),
                TransactionInfo("demo:2", t, -1200, "MONTHLY SERVICE FEE (demo)", "fee"),
                TransactionInfo("demo:3", t, 37, "INTEREST PAID (demo)", "int"),
            ],
            source="csv",
            actor=b1,
        )

        # Records in every state and test personas (community/demo/states/).
        from community.demo import states

        states.seed()
        # Vendor onboarding (D79): every state, in its own module.
        from operations.demo_vendor_onboarding import seed as seed_vendor_onboarding

        seed_vendor_onboarding(board)
