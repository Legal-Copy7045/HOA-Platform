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


def _login_for(person: Person, email: str) -> User:
    """Give a seeded person a login without a password (authorship only)."""
    if person.user_id:
        return person.user
    user = User.objects.create_user(email)
    community.link_login(person, user, actor=SYSTEM)
    person.refresh_from_db()
    return user


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
        board = [_login_for(s.person, f"demo-board{i}@example.com") for i, s in enumerate(seats)]
        b1, b2 = board[0], board[1] if len(board) > 1 else board[0]
        lots = {lot.number: lot for lot in Lot.objects.all()}
        owner_of = {
            o.lot.number: o.person
            for o in Ownership.objects.active().select_related("lot", "person")
        }
        owner3 = _login_for(owner_of[3], "demo-owner3@example.com")
        owner12 = _login_for(owner_of[12], "demo-owner12@example.com")

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

        for month in range(1, t.month + 1):
            ledger.post_monthly_assessments(year=t.year, month=month, actor=b1)
        for number, lot in lots.items():
            if number == 9:  # prepaid the whole year
                ledger.record_payment(
                    lot=lot,
                    amount_cents=4500 * 12,
                    received_on=t.replace(month=1, day=1) - datetime.timedelta(days=5),
                    reference="2001",
                    actor=b1,
                )
            elif number in (15, 27):  # behind on dues
                ledger.record_payment(
                    lot=lot,
                    amount_cents=4500 * max(t.month - 3, 0) or 4500,
                    received_on=t.replace(day=1),
                    reference="3001",
                    actor=b1,
                )
            else:
                ledger.record_payment(
                    lot=lot,
                    amount_cents=4500 * t.month,
                    received_on=t.replace(day=1),
                    reference="4001",
                    actor=b1,
                )
        ledger.open_case(lot=lots[27], actor=b1)

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
        start = timezone.now() + datetime.timedelta(days=21)
        comms.create_event(
            title="Annual owners' meeting (demo)",
            starts_at=start,
            location="Community room (fictional)",
            actor=b1,
        )
        governance.create_meeting(
            title="Annual owners' meeting (demo)",
            kind="annual",
            starts_at=start,
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
        poll = governance.create_vote(
            title="Add a fountain to the north pond? (demo)",
            kind="informal",
            options=["Yes", "No", "No opinion"],
            actor=b1,
        )
        governance.open_vote(poll, actor=b1)
        governance.submit_records_request(description="2025 reserve study (demo)", actor=owner12)

        req = arc.create_request(
            lot=lots[3],
            title="Rear fence (demo)",
            description="4 ft black aluminium, rear yard",
            actor=owner3,
        )
        arc.ARC.apply(req, "submit", actor=owner3)
        arc.ARC.apply(req, "accept", actor=b1)
        v = compliance.report(
            lot=lots[12], category="trash", description="Cans at curb (demo)", actor=b1
        )
        compliance.VIOLATION.apply(v, "send_notice", actor=b1)
        ledger.request_resale(lot=lots[12], requester_name="Demo Seller", actor=owner12)
        ops.create_task(title="Get second pond maintenance quote (demo)", actor=b1)

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
