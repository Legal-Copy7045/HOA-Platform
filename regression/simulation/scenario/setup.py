"""The first simulated day: the board sets the community up, as the setup
wizard's steps would (each through its service, as a board member).

With the ``blank`` dataset the day before is the go-live: the first officer
is bootstrapped (``bootstrap_first_officer``) and the register of 44
fictional homes is imported through ``community/register_import.py``.
"""

from __future__ import annotations

import datetime

from django.core.files.uploadedfile import SimpleUploadedFile

from core.policy import SYSTEM
from core.scoping import system_access

CITE = "Declaration (simulation, fictional)"
MONTHLY_CENTS = 4500
RESERVE_MONTHLY_CENTS = 60000
OPENING_OPERATING = 2_500_000
OPENING_RESERVE = 4_000_000

RULES = {
    "dues.monthly_cents": str(MONTHLY_CENTS),
    "dues.due_day": "1",
    "dues.auto_post": "true",
    "late_fee.cents": "2500",
    "late_fee.grace_days": "15",
    "late_fee.auto_apply": "true",
    "collections.courtesy_reminder_day": "10",
    "collections.notice_days": "10",
    "arc.completeness_days": "10",
    "arc.review_days": "45",
    "arc.deemed_approval": "true",
    "arc.appeal_days": "30",
    "arc.signoff_respond_days": "14",
    "violation.cure_days": "14",
    "violation.hearing_notice_days": "10",
    "meeting.owner_notice_days": "10",
    "records.response_days": "30",
    "resale.fee_cents": "15000",
    "resale.deadline_days": "10",
    "vote.quorum_percent": "20",
    "vote.pass_percent": "51",
    "special_assessment.vote_required_cents": "1000000",
    "special_assessment.vote_percent": "51",
    "special_assessment.late_fees": "true",
    "interest.annual_rate_bp": "1000",
    "interest.max_annual_rate_bp": "1500",
    "legal.costs_chargeable": "true",
    "cashflow.collection_rate_percent": "95",
    "books.reserve_monthly_cents": str(RESERVE_MONTHLY_CENTS),
}
FEATURES_ON = [
    "online_payments",
    "autopay",
    "bank_feeds",
    "overdue_interest",
    "collections_legal",
    "capital_projects",
    "insurance_claims",
    "reservations",
    "owner_finance_dashboard",
    "cashflow_forecast",
]
# Vendor onboarding is switched on after the vendors already in use are
# recorded (they are grandfathered); a new vendor is onboarded later.
CATEGORIES = [
    ("5100", "Pond and stormwater"),
    ("5200", "Landscaping"),
    ("5300", "Insurance"),
    ("5400", "Legal and professional"),
    ("5500", "Management"),
    ("5700", "Snow removal"),
    ("5800", "Repairs and maintenance"),
    ("5900", "Office and postage"),
]
VENDORS = [
    ("Greenway Lawn (sim)", "Mowing, beds", "5200"),
    ("Pond Pros (sim)", "Pond maintenance", "5100"),
    ("Example Mutual Insurance (sim)", "Package policy", "5300"),
    ("Fictional Law Office (sim)", "Collections counsel", "5400"),
    ("Example Management Co (sim)", "Community management", "5500"),
    ("Creekside Snow (sim)", "Snow removal", "5700"),
]


def _pdf(title: str) -> SimpleUploadedFile:
    from core import pdf

    body = pdf.render([("heading", f"{title} (FICTIONAL SIMULATION)"), ("normal", "Placeholder.")])
    return SimpleUploadedFile("document.pdf", body, content_type="application/pdf")


def blank_start(sc) -> None:
    """Go-live on a blank portal: first officer, then the register import."""
    import io

    from django.core.management import call_command

    from community import register_import
    from community.demo import address, demo_name

    call_command(
        "bootstrap_first_officer",
        name="Morgan Firstofficer (demo)",
        email="first-officer@example.com",
        office="president",
        stdout=io.StringIO(),
    )
    from community.models import Person

    with system_access("simulation cast"):
        officer = Person.objects.get(email="first-officer@example.com")
    from regression.simulation.world import World

    user = World(0).login(officer)
    since = sc.o.start - datetime.timedelta(days=900)
    rows = ["lot,street_address,owner_name,owner_email,owner_since,ownership_type"]
    for n in range(1, 45):
        email = "" if n in (9, 26, 41) else f"reg{n:03d}@example.com"
        street = address(n, ("Makebelieve Creek Drive", "Pretend Hollow Court"))
        rows.append(
            f"{n},{street},{demo_name(n + 3)},{email},{since + datetime.timedelta(days=n * 11)},sole"
        )
    sheet = register_import.read("\n".join(rows).encode(), "csv")
    result = register_import.check(sheet, actor=user)
    register_import.write(result, actor=user)
    # The rest of the board: proposed by the officer, approved by... the officer
    # is alone, so the declarant's appointees are recorded as at go-live.
    from community import services
    from community.models import Office, SeatType

    with system_access("simulation go-live"):
        others = [
            services.record_person(display_name=f"{n} Declarant (demo)", email=e, actor=SYSTEM)
            for n, e in (("Ellis", "ellis-decl@example.com"), ("Marlow", "marlow-decl@example.com"))
        ]
    for person, office in zip(others, (Office.TREASURER, Office.SECRETARY), strict=True):
        services.record_board_seat(
            person=person,
            seat_type=SeatType.DECLARANT_APPOINTED,
            term_start=sc.o.start - datetime.timedelta(days=1),
            citation=CITE,
            actor=SYSTEM,
        )
        services.record_office(person=person, office=office, start_date=sc.o.start, actor=SYSTEM)


def plan(sc, day, rng):
    if day != sc.o.start:
        return []
    w = sc.world
    b0, b1, b2 = [*w.board, None, None, None][:3]
    ev = sc.ev
    out = [
        ev("setup.rules", day, 8, lambda: rules(b0), staff=[b0]),
        ev("setup.features", day, 8, lambda: features(b0), minute=5),
        ev("setup.profile", day, 8, lambda: profile(b0), minute=10),
        ev("setup.staff", day, 8, lambda: staff(sc, b0), minute=15),
        ev("setup.categories", day, 8, lambda: categories(sc, b1), minute=20),
        ev("setup.bank_and_books", day, 8, lambda: books(sc, b1), minute=25),
        ev("setup.coding_rules", day, 8, lambda: coding_rules(b1), minute=30),
        ev("setup.budget", day, 8, lambda: budget(sc, b1), minute=35),
        ev("setup.reserve_plan", day, 8, lambda: reserve_plan(sc, b1), minute=40),
        ev("setup.vendors", day, 8, lambda: vendors(sc, b0), minute=45),
        ev("setup.contracts", day, 8, lambda: contracts(sc, b0, b1, b2), minute=50),
        ev("setup.insurance_policy", day, 8, lambda: insurance(sc, b0), minute=55),
        ev("setup.neighbors", day, 9, lambda: neighbors(sc, b0)),
        ev("setup.common_areas", day, 9, lambda: common_areas(sc, b0), minute=5),
        ev("setup.committees", day, 9, lambda: committees(sc, b0), minute=10),
        ev("setup.compliance_calendar", day, 9, lambda: compliance(b0), minute=15),
        ev("setup.vendor_onboarding_on", day, 9, lambda: onboarding_on(b0), minute=20),
        ev("setup.contacts", day, 9, lambda: contacts(sc, b0), minute=25),
        ev("setup.first_assessments", day, 9, lambda: first_assessments(b1), minute=30),
    ]
    return out


def rules(actor):
    from community import services

    for key, value in RULES.items():
        services.set_rule(key=key, value=value, citation=CITE, actor=actor)
    return f"{len(RULES)} rules"


def features(actor):
    from core import features as f

    for key in FEATURES_ON:
        f.set_feature(key, True, actor=actor)
    return f"{len(FEATURES_ON)} features"


def profile(actor):
    from community import services

    services.update_profile(
        legal_name="Oak Creek Farms Homeowners Association (simulation)",
        fiscal_year_start_month=1,
        welcome_message="Simulated community: everything here is fictional.",
        actor=actor,
    )
    services.update_letter_settings(
        letterhead="Oak Creek Farms HOA (simulation)\n1 Example Road, Exampletown, PA 00000",
        remit_to="Oak Creek Farms HOA (simulation)\nPO Box 0000\nExampletown, PA 00000",
        letter_contact="Example Management Co. (sim) at 555-0100",
        letter_signature="For the Board of Directors (simulation)",
        resale_insurance_company="Example Mutual Insurance (sim)",
        resale_insurance_cents=100_000_000,
        actor=actor,
    )


def staff(sc, actor):
    """A management company manager, an auditor and two ARC reviewers (owners)."""
    from community import services
    from community.models import AppointedBy, Role

    w = sc.world
    manager = w.new_person("Example Manager")
    services.grant_role(
        person=manager,
        role=Role.MANAGER,
        appointed_by=AppointedBy.BOARD,
        citation=CITE,
        actor=actor,
    )
    auditor = w.new_person("Example Auditor")
    services.grant_role(
        person=auditor,
        role=Role.AUDITOR,
        appointed_by=AppointedBy.BOARD,
        citation=CITE,
        actor=actor,
    )
    # Two next-door neighbours (one is recused from the other's street) and a third.
    homes = [h for h in w.owner_homes() if h.owners]
    reviewers = [homes[3].people[0], homes[4].people[0], homes[20].people[0]]
    for person in reviewers:
        services.grant_role(
            person=person,
            role=Role.ARC_REVIEWER,
            appointed_by=AppointedBy.BOARD,
            citation=CITE,
            actor=actor,
        )
    w.refresh()


def categories(sc, actor):
    from operations import services as ops
    from operations.models import ExpenseCategory

    with system_access("simulation lookup"):
        have = set(ExpenseCategory.objects.values_list("code", flat=True))
    for code, name in CATEGORIES:
        if code not in have:
            ops.save_category(code=code, name=name, actor=actor)


def books(sc, actor):
    """Accounting setup, the bank connected through Teller (the fake), both
    accounts in the books with their funds and opening balances."""
    from ledger import accounting
    from ledger.bankfeeds import services as bank
    from ledger.books import funds

    opening_on = sc.o.start - datetime.timedelta(days=1)
    accounting.save_setup(
        gl_accounts="",
        opening_cash_cents=OPENING_OPERATING + OPENING_RESERVE,
        opening_cash_on=opening_on,
        actor=actor,
    )
    conns = bank.connect_teller(access_token="sim-teller-token-0001", actor=actor)  # noqa: S106  # nosec B106
    by_ext = {c.external_account_id: c for c in conns}
    funds.save_account(
        by_ext["acc_sim_operating"],
        fund="operating",
        is_primary=True,
        opening_balance_cents=OPENING_OPERATING,
        actor=actor,
    )
    funds.save_account(
        by_ext["acc_sim_reserve"],
        fund="reserve",
        opening_balance_cents=OPENING_RESERVE,
        actor=actor,
    )
    sc.notes["bank"] = {k: c.pk for k, c in by_ext.items()}
    return f"{len(conns)} accounts"


def coding_rules(actor):
    from ledger.books import coding, funds
    from ledger.books.models import LineSign

    funds.ensure_default_categories()
    cats = {c.key: c for c in funds.active_categories()}
    for text, sign, key in (
        ("INTEREST PAID", LineSign.IN, "interest_income"),
        ("MONTHLY SERVICE CHARGE", LineSign.OUT, "bank_fees"),
        ("TRANSFER TO SAVINGS", LineSign.OUT, "transfer"),
        ("TRANSFER FROM CHECKING", LineSign.IN, "transfer"),
    ):
        coding.save_rule(
            name=text.title(), contains=text, sign=sign, category=cats[key], actor=actor
        )


def _cats():
    from operations.models import ExpenseCategory

    with system_access("simulation lookup"):
        return {c.code: c for c in ExpenseCategory.objects.all()}


def budget(sc, actor, fiscal_year=None):
    from ledger import services as ledger

    fy = fiscal_year or sc.o.start.year
    cats = _cats()
    homes = len(sc.world.homes)
    b = ledger.create_budget(fiscal_year=fy, actor=actor)
    lines = [
        ("income", "Assessments", MONTHLY_CENTS * 12 * homes, {"income_source": "assessments"}),
        ("expense", "Landscaping", 1_020_000, {"category": cats["5200"]}),
        ("expense", "Pond and stormwater", 300_000, {"category": cats["5100"]}),
        ("expense", "Insurance", 420_000, {"category": cats["5300"]}),
        ("expense", "Management", 600_000, {"category": cats["5500"]}),
        ("expense", "Snow removal", 400_000, {"category": cats["5700"]}),
        ("expense", "Legal and professional", 150_000, {"category": cats["5400"]}),
    ]
    for kind, name, cents, extra in lines:
        ledger.add_line(b, kind=kind, name=name, annual_cents=cents, actor=actor, **extra)
    ledger.adopt_budget(b, actor=actor)
    return f"FY{fy} adopted"


def reserve_plan(sc, actor):
    from ledger import reserve_plan as rp

    rp.save_settings(
        starting_balance_cents=OPENING_RESERVE,
        balance_as_of=sc.o.start - datetime.timedelta(days=1),
        annual_contribution_cents=RESERVE_MONTHLY_CENTS * 12,
        inflation_bp=300,
        interest_bp=150,
        horizon_years=30,
        actor=actor,
    )
    for name, category, quantity, cost, life, left in (
        ("Main road paving (sim)", "roads", "4,800 sq yd", 26_000_000, 25, 9),
        ("Road sealcoat (sim)", "roads", "4,800 sq yd", 1_800_000, 5, 1),
        ("Pond dredging (sim)", "stormwater", "1 pond", 6_000_000, 20, 12),
        ("Entrance sign (sim)", "signs", "2 signs", 1_200_000, 20, 4),
        ("Entrance lighting (sim)", "lighting", "6 fixtures", 900_000, 15, 0),
    ):
        rp.save_component(
            name=name,
            category=category,
            quantity=quantity,
            replacement_cost_cents=cost,
            useful_life_years=life,
            remaining_life_years=left,
            notes="Simulation estimate.",
            actor=actor,
        )
    rp.adopt(actor=actor)


def vendors(sc, actor):
    from operations import services as ops

    made = {}
    for name, services_, code in VENDORS:
        v = ops.save_vendor(
            name=name,
            services=services_,
            address="1 Fictional Road\nAnytown, PA 00000",
            insurance_expires_on=sc.o.start + datetime.timedelta(days=200),
            w9_on_file=True,
            actor=actor,
        )
        made[name] = (v.pk, code)
    sc.notes["vendors"] = made


def contracts(sc, b0, b1, b2):
    from operations import services as ops
    from operations.models import Vendor

    cats = _cats()
    with system_access("simulation lookup"):
        green = Vendor.objects.get(name="Greenway Lawn (sim)")
        mgmt = Vendor.objects.get(name="Example Management Co (sim)")
    out = []
    for vendor, title, code, cap in (
        (green, "Common area mowing (sim)", "5200", 85_000),
        (mgmt, "Community management (sim)", "5500", 50_000),
    ):
        c = ops.create_contract(
            vendor=vendor,
            title=title,
            category=cats[code],
            schedule="monthly",
            amount_cap_cents=cap,
            starts_on=sc.o.start,
            ends_on=sc.o.start + datetime.timedelta(days=364),
            citation="Board resolution (simulation)",
            actor=b0,
        )
        for approver in (b1, b2):
            if c.state == "draft" and approver is not None:
                c = ops.approve_contract(c, note="Approved (simulation)", actor=approver)
        out.append(c.pk)
    sc.notes["contracts"] = out


def insurance(sc, actor):
    from documents import renewals

    policy = renewals.record_policy(
        actor=actor,
        kind="property",
        carrier="Example Mutual Insurance (sim)",
        policy_number="SIM-0001",
        coverage_cents=100_000_000,
        premium_cents=420_000,
        starts_on=sc.o.start,
        ends_on=sc.o.start + datetime.timedelta(days=364),
        file=_pdf("Insurance declarations page"),
    )
    sc.notes["policy"] = policy.pk


def neighbors(sc, actor):
    from arc import signoffs
    from community.models import Lot

    with system_access("simulation lookup"):
        lots = {lot.number: lot for lot in Lot.objects.all()}
    for number, lot in lots.items():
        road = range(1, 23) if number <= 22 else range(23, 45)
        signoffs.set_neighbors(
            lot,
            left=lots.get(number - 1) if number - 1 in road else None,
            right=lots.get(number + 1) if number + 1 in road else None,
            actor=actor,
        )


def common_areas(sc, actor):
    from community import reservations, services

    services.save_common_area(name="Pavilion (sim)", kind="amenity", actor=actor)
    services.save_common_area(name="North detention pond (sim)", kind="stormwater", actor=actor)
    from community.models import CommonArea

    with system_access("simulation lookup"):
        pavilion = CommonArea.objects.get(name="Pavilion (sim)")
    reservations.save_area_settings(
        pavilion,
        actor=actor,
        bookable=True,
        needs_approval=True,
        max_hours=6,
        advance_days=90,
        monthly_limit_per_lot=2,
        fee_cents=0,
        deposit_cents=0,
        allow_authorized=True,
        allow_tenants=True,
        terms="Clean up after use (simulation).",
        citation=CITE,
    )
    sc.notes["pavilion"] = pavilion.pk


def committees(sc, actor):
    from governance import services as gov

    social = gov.create_committee(
        name="Social (sim)", purpose="Community events.", citation=CITE, actor=actor
    )
    homes = sc.world.owner_homes()
    gov.appoint_committee_member(social, person=homes[5].people[0], role="chair", actor=actor)
    gov.appoint_committee_member(social, person=homes[6].people[0], actor=actor)
    sc.notes["committee"] = social.pk


def compliance(actor):
    from operations import services as ops

    ops.add_starter_items(actor=actor)


def onboarding_on(actor):
    from core import features as f

    f.set_feature("vendor_onboarding", True, actor=actor)


def contacts(sc, actor):
    """A tenant on one home and an authorized contact on another (the owners'
    account change requests, approved by a board member)."""
    from community import services
    from community.models import Account

    homes = [h for h in sc.world.owner_homes() if h.owners]
    made = 0
    for home, kind in ((homes[10], "tenant"), (homes[11], "authorized")):
        person = sc.world.new_person("Contact")
        with system_access("simulation lookup"):
            account = Account.objects.get(lot_id=home.lot_id, closed_on__isnull=True)
        services.add_account_contact(account, person=person, kind=kind, actor=actor)
        made += 1
        if kind == "tenant":
            sc.world.tenant_lots.add(home.number)
    return f"{made} contacts"


def first_assessments(actor):
    """The task posts on the 1st before anyone is up; the first month is
    posted by hand once the amount is set (the button on Owner accounts)."""
    from ledger import services as ledger

    today = timezone_today()
    n = ledger.post_monthly_assessments(year=today.year, month=today.month, actor=actor)
    return f"{n} posted"


def timezone_today():
    from django.utils import timezone

    return timezone.localdate()
