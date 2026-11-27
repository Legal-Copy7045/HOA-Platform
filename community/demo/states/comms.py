"""Announcements, calendar events with RSVPs and volunteer slots, FAQ,
common-area bookings, paper mail with synthetic USPS scans, and documents
with versions, in every state."""

from __future__ import annotations

import datetime

from community.demo.states import _pdf_upload

CLUBHOUSE = "Clubhouse (fictional)"  # bookable, needs approval
PAVILION = "Pavilion (fictional)"  # bookable at once
FULL_EVENT = "Wine and cheese social (demo)"
OPEN_EVENT = "Community picnic (demo)"  # first part; free volunteer slots
# A synthetic Mailer ID for the demo's barcodes (never a real one).
DEMO_MAILER_ID = "000000"


def seed(c) -> None:
    _announcements_events_faq(c)
    _reservations(c)
    _paper_mail(c)
    _documents(c)


def _announcements_events_faq(c) -> None:
    from comms import rsvp
    from comms import services as comms

    manager, sec = c.u("manager"), c.u("secretary")
    comms.publish_announcement(
        title="Water main break on the lane (demo)",
        body="The township is repairing a water main. Expect low pressure today.",
        urgent=True,
        actor=sec,
    )
    comms.publish_announcement(
        title="Board only: insurance renewal quotes (demo)",
        body="Three quotes are in the board folder.",
        audience="board",
        actor=manager,
    )
    # A full event (cap reached), a past one and a cancelled one.
    social = comms.create_event(
        title=FULL_EVENT,
        starts_at=c.now + datetime.timedelta(days=6),
        location="Clubhouse (fictional)",
        description="Limited to two homes this time (demo).",
        actor=manager,
    )
    rsvp.save_settings(social, enabled=True, cap=2, max_guests=0, remind=False, actor=manager)
    rsvp.answer(social, answer="going", actor=c.u("owner_b"))
    rsvp.answer(social, answer="going", actor=c.u("joint_a"))
    helpers = rsvp.add_slot(social, title="Bring glasses", capacity=1, actor=manager)
    rsvp.sign_up(helpers, actor=c.u("owner_b"))
    comms.create_event(
        title="Fall cleanup day (demo)",
        starts_at=c.now - datetime.timedelta(days=20),
        location="Entrance (fictional)",
        description="Thanks to everyone who came (demo).",
        actor=manager,
    )
    gone = comms.create_event(
        title="Food truck night (demo)",
        starts_at=c.now + datetime.timedelta(days=12),
        location="Pavilion (fictional)",
        actor=manager,
    )
    comms.cancel_event(gone, actor=manager)
    comms.save_faq(
        question="Can I rent the clubhouse? (demo)",
        answer="Yes: Community -> Reservations. Draft answer, not yet published.",
        category="Amenities",
        published=False,
        actor=manager,
    )


def _reservations(c) -> None:
    from community import reservations
    from community import services as community
    from community.models import CommonArea

    pres = c.u("president")
    for name, needs_approval in ((CLUBHOUSE, True), (PAVILION, False)):
        community.save_common_area(name=name, kind="amenity", actor=pres)
        area = CommonArea.objects.get(name=name)
        reservations.save_area_settings(
            area,
            bookable=True,
            needs_approval=needs_approval,
            max_hours=4,
            advance_days=90,
            monthly_limit_per_lot=2,
            allow_authorized=True,
            allow_tenants=True,
            terms="Leave it as you found it (demo).",
            citation="Board rules for common areas (demo)",
            actor=pres,
        )
    club = CommonArea.objects.get(name=CLUBHOUSE)
    pavilion = CommonArea.objects.get(name=PAVILION)

    def book(area, key, lot, days, purpose):
        start = c.at(days, 14)
        return reservations.request_reservation(
            area=area,
            lot_id=c.lots[lot].pk,
            starts_at=start,
            ends_at=start + datetime.timedelta(hours=3),
            purpose=purpose,
            actor=c.u(key),
        )

    book(club, "owner", 3, 5, "Book club (demo)")  # waiting for approval
    res = book(club, "owner_b", 12, 8, "Birthday party (demo)")
    reservations.approve(res, actor=c.u("manager"))
    res = book(club, "joint_a", 7, 12, "Garage sale overflow (demo)")
    reservations.decline(res, note="Not for sales (demo).", actor=c.u("manager"))
    res = book(club, "owner", 3, 15, "Game night (demo)")
    reservations.cancel(res, note="Plans changed (demo).", actor=c.u("owner"))
    book(pavilion, "tenant", 40, 9, "Family lunch (demo)")  # confirmed at once


def _paper_mail(c) -> None:
    from comms import papermail, usps_tracking
    from comms.models import MailPiece
    from comms.usps_iv import Scan
    from governance.models import Meeting

    manager = c.u("manager")
    papermail.save_settings(mailer_id=DEMO_MAILER_ID, imb_enabled=True, actor=c.u("president"))
    annual = Meeting.objects.get(title="Annual owners' meeting (demo)")
    mailed = papermail.create_mailing(
        actor=manager,
        defaults=papermail.source_defaults(meeting=annual),
        subject="Notice of the annual owners' meeting (demo)",
        body="Dear {owner_names},\n\nThe annual meeting is on {meeting_date}. (demo)",
    )
    papermail.letters_pdf(mailed, duplex=False)  # gives each letter its barcode
    papermail.record_mailed(mailed, mailed_on=c.t, actor=manager)
    # Synthetic scans (no USPS call): processed, sorted for delivery, delivered.
    pieces = list(MailPiece.objects.filter(mailing=mailed).order_by("serial"))
    when = c.now - datetime.timedelta(hours=10)
    for piece, opcodes in zip(
        pieces, (["336"], ["336", "918"], ["336", "918", "517"]), strict=False
    ):
        usps_tracking.store_scans(
            piece,
            [
                Scan(
                    scanned_at=when + datetime.timedelta(hours=i * 3),
                    opcode=op,
                    facility_city="EXAMPLETOWN",
                    facility_state="PA",
                    facility_zip="00000",
                )
                for i, op in enumerate(opcodes)
            ],
        )
    # Prepared but not yet mailed (a delinquency notice to one home), and one not sent.
    for lot, discard in ((27, None), (15, True)):
        mailing = papermail.create_mailing(
            actor=manager,
            defaults=papermail.source_defaults(template="first"),
            subject="Past-due assessments (demo)",
            body="Dear {owner_names},\n\nYour account is past due. (demo)",
            lot_id=c.lots[lot].pk,
        )
        if discard:
            papermail.record_mailed(mailing, discard=True, actor=manager)


def _documents(c) -> None:
    from documents import services as documents
    from documents.models import Document, Folder

    manager = c.u("manager")
    declaration = Document.objects.get(title="Declaration (demo placeholder)")
    documents.add_version(
        declaration,
        file=_pdf_upload("declaration-v2.pdf", "Declaration, amended"),
        note="Amendment 1 recorded (demo).",
        actor=manager,
    )
    legal = Folder.objects.get(name="Legal (board)")
    documents.create_document(
        title="Attorney letter on collections (demo)",
        category="other",
        visibility="board",
        folder=legal,
        file=_pdf_upload("attorney.pdf", "Attorney letter"),
        actor=manager,
    )
    pool = Folder.objects.get(name="Pool and amenities")
    documents.create_document(
        title="Clubhouse rules (demo)",
        category="policies",
        visibility="owners",
        folder=pool,
        file=_pdf_upload("clubhouse.pdf", "Clubhouse rules"),
        actor=manager,
    )
    documents.create_document(
        title="Plot plan (demo)",
        category="lot",
        visibility="lot",
        lot=c.lots[3],
        file=_pdf_upload("plot-plan.pdf", "Plot plan"),
        actor=manager,
    )
    old = documents.create_document(
        title="Old pool hours (demo)",
        category="policies",
        visibility="owners",
        file=_pdf_upload("old-pool.pdf", "Old pool hours"),
        actor=manager,
    )
    documents.set_archived(old, archived=True, actor=manager)
