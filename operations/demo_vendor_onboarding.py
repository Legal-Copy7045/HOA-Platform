"""Fictional vendor onboarding records for the demo community (D79).

Called once from ``seed_demo_operations`` (the oak_creek dataset). Every
vendor, person and document here is made up; the "W-9s" are obviously fake
PDFs that say SAMPLE and carry no real taxpayer number (000-00-0000 SAMPLE).

States: invited (link open), submitted waiting for a first approval,
submitted waiting for a second (with a disclosed conflict linked to a board
member), returned to the vendor, declined, active, active with an expired
insurance certificate, and an older vendor still to complete (the seeded
vendors without a W-9 or email are grandfathered).

The documents are encrypted under the server's vendor document key; without
one (``infra/poc/set-vendor-key.sh`` not run yet) only the records that need
no document are made and the feature stays off.
"""

from __future__ import annotations

import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from core.policy import SYSTEM


def _fake_pdf(kind: str, vendor_name: str) -> SimpleUploadedFile:
    from core import pdf

    title = {
        "w9": "SAMPLE Form W-9 (FICTIONAL DEMO - NOT A REAL FORM)",
        "coi_gl": "SAMPLE certificate of insurance: general liability (FICTIONAL)",
        "coi_auto": "SAMPLE certificate of insurance: auto liability (FICTIONAL)",
        "workers_comp": "SAMPLE workers' compensation exemption statement (FICTIONAL)",
        "hic": "SAMPLE PA HIC registration certificate (FICTIONAL)",
        "licence": "SAMPLE professional licence (FICTIONAL)",
    }[kind]
    lines = [("heading", title), ("normal", f"Name: {vendor_name}")]
    if kind == "w9":
        lines.append(("normal", "Taxpayer identification number: 000-00-0000 SAMPLE"))
    lines.append(("small", "Demo data for the Oak Creek Farms portal. Not a real document."))
    return SimpleUploadedFile(f"{kind}.pdf", pdf.render(lines), content_type="application/pdf")


def _answers(name: str, entity: str, coi_expires: datetime.date, **extra) -> dict:
    from operations import vendor_onboarding as vo

    base = {
        "legal_name": name.replace(" (demo)", " LLC (demo)"),
        "entity_type": entity,
        "contact_name": "Demo Contact (demo)",
        "contact_email": "vendor-contact@example.com",
        "contact_phone": "610-555-0100",
        "remit_address": "100 Fictional Way\nNowhere, PA 19000",
        "services": "Fictional demo services",
        "conflict": "no",
        "terms_accepted": True,
        "terms_version": vo.current_terms()[0],
        "w9_revision_confirmed": True,
        "coi_gl_carrier": "Demo Mutual Insurance (fictional)",
        "coi_gl_amount_cents": 100000000,
        "coi_gl_expires": coi_expires.isoformat(),
        "coi_auto_carrier": "Demo Mutual Insurance (fictional)",
        "coi_auto_amount_cents": 100000000,
        "coi_auto_expires": coi_expires.isoformat(),
        "workers_comp_mode": "exempt",
        "hic_registration": "PA000000",
        "licence_text": "Demo licence 0000 (fictional)",
    }
    base.update(extra)
    return base


def seed(board: list) -> str:
    """Create the demo onboarding states. ``board``: demo board logins."""
    from core import features
    from operations import vendor_crypto
    from operations import vendor_onboarding as vo

    if not vendor_crypto.configured() or len(board) < 3:
        return "Vendor onboarding demo skipped (no vendor document key or fewer than 3 board)."
    features.set_feature(vo.FEATURE, True, actor=SYSTEM)
    inviter, b2, b3 = board[0], board[1], board[2]
    today = timezone.localdate()
    next_year = today + datetime.timedelta(days=365)

    def invite(name, vendor_type):
        vendor = vo.invite_vendor(
            name=name,
            email=f"{name.split()[0].lower()}@example.com",
            services="Fictional demo services",
            vendor_type=vendor_type,
            actor=inviter,
        )
        rnd = vendor.onboarding_rounds.get()
        link = rnd.links.get()
        vo.send_link_email(link.pk)  # POC mail is redirected; the link opens the form
        return vendor, rnd

    def submit(rnd, name, entity="smllc", coi=next_year, **extra):
        vo.save_answers(rnd, _answers(name, entity, coi, **extra))
        rnd.refresh_from_db()
        for key in vo.round_items(rnd):
            item = vo.ITEMS[key]
            if item.file_kind:
                vo.upload_file(rnd, item.file_kind, _fake_pdf(item.file_kind, name))
        rnd.refresh_from_db()
        return vo.submit(rnd)

    invite("Brightline Electric (demo)", "contractor")  # invited, link open

    _, waiting_first = invite("Keystone Paving (demo)", "home_improvement")
    submit(waiting_first, "Keystone Paving (demo)")

    tree_vendor, waiting_second = invite("Valley Tree Care (demo)", "landscaping")
    b3_person = b3.person
    submit(
        waiting_second,
        "Valley Tree Care (demo)",
        conflict="yes",
        conflict_details=f"The owner's cousin is {b3_person.display_name}.",
    )
    vo.link_conflict(tree_vendor, b3_person, note="Named in the disclosure (demo)", actor=inviter)
    vo.approve(waiting_second, actor=b2)

    _, returned = invite("Quick Fix Handyman (demo)", "contractor")
    submit(returned, "Quick Fix Handyman (demo)")
    vo.return_to_vendor(
        returned, reason="The insurance certificate is for another company (demo).", actor=b2
    )
    vo.send_link_email(returned.links.filter(ended_at__isnull=True).get().pk)

    _, declined = invite("Cheap Seal Coating (demo)", "contractor")
    submit(declined, "Cheap Seal Coating (demo)", entity="individual")
    vo.decline(declined, reason="No liability insurance in force (demo).", actor=b2)

    _, active = invite("Summit Reserve Studies (demo)", "professional")
    submit(active, "Summit Reserve Studies (demo)", entity="s_corp")
    vo.approve(active, actor=b2)
    vo.approve(active, actor=b3)

    _, expired = invite("Creekside Snow Removal (demo)", "landscaping")
    submit(expired, "Creekside Snow Removal (demo)", coi=today - datetime.timedelta(days=20))
    vo.approve(expired, actor=b2)
    vo.approve(expired, actor=b3)
    return "Vendor onboarding demo: 7 vendors in every state."
