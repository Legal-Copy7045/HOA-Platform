"""Fictional renewals and records-retention data for the oak_creek demo (D72).

Called once from community/demo/oak_creek.py after the operations demo data.
Everything is synthetic and marked "(demo)". Adds:

* insurance policies: general liability ending in 45 days (due), D&O ending
  in 250 days (current), a fidelity bond that ended 10 days ago (expired),
  and property insurance with last year's term renewed;
* vendors' insurance certificates: one current, one expired;
* contracts with renewal terms: snow removal renewing by itself with its
  notice deadline passed, trash collection ending in 200 days, and an old
  pond-dredging contract that ended 8 years ago;
* old financial records: one past the retention period, one waiting for a
  second board member's confirmation, one under a legal hold, and one
  already disposed of (file removed, records kept).
"""

from __future__ import annotations

import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def _pdf(name: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


def _board():
    from accounts.models import User
    from community.models import BoardSeat
    from core.scoping import system_access

    with system_access("demo renewals: board logins"):
        ids = list(
            BoardSeat.objects.active()
            .filter(person__user__is_active=True)
            .order_by("person__user_id")
            .values_list("person__user_id", flat=True)
            .distinct()
        )
    return list(User.objects.filter(pk__in=ids).order_by("pk"))


def _years_ago(day: datetime.date, years: int) -> datetime.date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def seed() -> None:
    from documents import renewals, retention
    from documents import services as documents
    from operations import services as ops
    from operations.models import ExpenseCategory, Vendor

    board = _board()
    if len(board) < 3:
        return  # the oak_creek dataset seats three or more board members
    b1, b2, b3 = board[:3]
    t = timezone.localdate()
    day = datetime.timedelta(days=1)

    # --- Insurance policies ---
    renewals.record_policy(
        actor=b1,
        kind="general_liability",
        carrier="Fictional Mutual Insurance (demo)",
        policy_number="GL-0001-DEMO",
        coverage_cents=100_000_000,
        aggregate_cents=200_000_000,
        deductible_cents=100_000,
        premium_cents=185_000,
        starts_on=t + 45 * day - 365 * day,
        ends_on=t + 45 * day,
        file=_pdf("general-liability-declarations-demo.pdf"),
    )
    renewals.record_policy(
        actor=b1,
        kind="d_and_o",
        carrier="Example Specialty Co (demo)",
        policy_number="DO-0002-DEMO",
        coverage_cents=100_000_000,
        premium_cents=92_000,
        starts_on=t + 250 * day - 365 * day,
        ends_on=t + 250 * day,
    )
    renewals.record_policy(
        actor=b1,
        kind="fidelity",
        carrier="Sample Surety (demo)",
        policy_number="FB-0003-DEMO",
        coverage_cents=25_000_000,
        premium_cents=41_000,
        starts_on=t - 10 * day - 365 * day,
        ends_on=t - 10 * day,
        notes="Renewal quote requested (demo).",
    )
    old_property = renewals.record_policy(
        actor=b1,
        kind="property",
        carrier="Fictional Mutual Insurance (demo)",
        policy_number="PR-0004-DEMO",
        coverage_cents=45_000_000,
        premium_cents=63_000,
        starts_on=t - 300 * day - 365 * day,
        ends_on=t - 300 * day,
        file=_pdf("property-policy-last-year-demo.pdf"),
    )
    renewals.record_policy(
        actor=b1,
        renews=old_property,
        kind="property",
        carrier="Fictional Mutual Insurance (demo)",
        policy_number="PR-0004-DEMO",
        coverage_cents=48_000_000,
        premium_cents=66_500,
        starts_on=t - 300 * day,
        ends_on=t + 65 * day,
        file=_pdf("property-policy-demo.pdf"),
    )

    # --- Vendors' insurance certificates ---
    green = Vendor.objects.filter(name="Greenway Lawn (demo)").first()
    if green is not None:
        renewals.record_vendor_certificate(
            green,
            expires_on=t + 200 * day,
            insurer="Example Casualty (demo)",
            coverage_cents=100_000_000,
            file=_pdf("greenway-certificate-demo.pdf"),
            actor=b1,
        )
    electric = ops.save_vendor(
        name="Bright Lights Electric (demo)", services="Entrance lighting", actor=b1
    )
    renewals.record_vendor_certificate(
        electric,
        expires_on=t - 5 * day,
        insurer="Example Casualty (demo)",
        file=_pdf("bright-lights-certificate-demo.pdf"),
        actor=b1,
    )

    # --- Contracts with renewal terms ---
    category = ExpenseCategory.objects.filter(active=True).first()

    def approved_contract(vendor, title, starts_on, ends_on, cap):
        c = ops.create_contract(
            vendor=vendor,
            title=title,
            category=category,
            schedule="annual",
            amount_cap_cents=cap,
            starts_on=starts_on,
            ends_on=ends_on,
            citation="Board resolution (demo)",
            actor=b1,
        )
        for approver in (b2, b3):
            if c.state == "draft":
                c = ops.approve_contract(c, note="Approved at a meeting (demo)", actor=approver)
        return c

    snow = ops.save_vendor(name="North Star Snow (demo)", services="Plowing", actor=b1)
    snow_contract = approved_contract(
        snow, "Snow removal (demo)", t - 300 * day, t + 20 * day, 45_000
    )
    renewals.save_contract_terms(
        snow_contract,
        renewal="auto",
        notice_days=60,
        renews_for_months=12,
        file=_pdf("snow-removal-contract-demo.pdf"),
        actor=b1,
    )
    trash = ops.save_vendor(name="Curbside Hauling (demo)", services="Trash, recycling", actor=b1)
    trash_contract = approved_contract(
        trash, "Trash collection (demo)", t - 165 * day, t + 200 * day, 60_000
    )
    renewals.save_contract_terms(trash_contract, renewal="manual", notice_days=30, actor=b1)
    dredge = ops.save_vendor(name="Deep Water Dredging (demo)", services="Pond", actor=b1)
    dredge_contract = approved_contract(
        dredge,
        "Pond dredging (demo)",
        _years_ago(t, 10),
        _years_ago(t, 8),
        90_000,
    )
    renewals.save_contract_terms(
        dredge_contract,
        renewal="manual",
        file=_pdf("pond-dredging-contract-demo.pdf"),
        actor=b1,
    )
    renewals.decide_contract(
        dredge_contract, decision="let_end", note="Work completed (demo)", actor=b2
    )

    # --- Old financial records under the retention schedule (7 years) ---
    def old_record(title, years):
        return documents.create_document(
            title=title,
            category="financial",
            visibility="board",
            file=_pdf(title.lower().replace(" ", "-") + ".pdf"),
            dated_on=_years_ago(t, years),
            actor=b1,
        )

    old_record("Bank statements 2017 (demo)", 9)
    waiting = old_record("Bank statements 2018 (demo)", 8)
    held = old_record("Audit work papers 2016 (demo)", 10)
    gone = old_record("Bank statements 2015 (demo)", 11)
    retention.place_hold(document=held, reason="Insurance claim review (demo)", actor=b1)
    disposal = retention.propose(
        [gone.current_version_id], reason="Retention schedule (demo)", actor=b1
    )
    retention.decide(disposal, approve=True, note="Checked (demo)", actor=b2)
    retention.propose([waiting.current_version_id], reason="Retention schedule (demo)", actor=b1)
