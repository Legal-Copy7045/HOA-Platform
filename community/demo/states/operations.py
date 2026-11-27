"""Vendors, work orders, contracts, invoices, board tasks and recurring
deadlines in every state."""

from __future__ import annotations

import datetime

# Invoice number -> state after seeding (PP-101/GL-55/PP-102/GL-56 come from
# the first part: paid, approved, waiting for its first approval, draft).
INVOICES = {
    "GL-60": "pending_approval",  # one approval of two: waiting for a second board member
    "PP-103": "pending_approval",  # fresh, waiting for its first approval
    "FI-9": "rejected",
    "GL-57": "void",
    "PP-104": "approved",  # partly paid
    "GL-M1": "approved",  # under the mowing contract: approved in advance
}


def seed(c) -> None:
    from operations import services as ops
    from operations.models import Contract, ExpenseCategory, Vendor

    manager, pres, treas = c.u("manager"), c.u("president"), c.u("treasurer")
    cats = {cat.code: cat for cat in ExpenseCategory.objects.all()}

    # Vendors: one whose insurance has lapsed and no W-9, alongside the first part's.
    snow = ops.save_vendor(
        name="Snowbelt Plowing (demo)",
        contact_name="Alex Example",
        email="plow@example.com",
        services="Snow removal",
        insurance_expires_on=c.days(-12),
        w9_on_file=False,
        actor=manager,
    )
    pond = Vendor.objects.get(name="Pond Pros (demo)")
    green = Vendor.objects.get(name="Greenway Lawn (demo)")
    insurer = Vendor.objects.get(name="Fictional Insurance Co (demo)")

    # Work orders: assigned, in progress, closed, cancelled, and one overdue.
    W = ops.WORK_ORDER
    wo = ops.create_work_order(
        title="Replace entrance light bulbs (demo)", vendor=green, actor=manager
    )
    W.apply(wo, "assign", actor=manager)
    wo = ops.create_work_order(
        title="Repair split-rail fence section (demo)",
        vendor=green,
        due_on=c.days(7),
        actor=manager,
    )
    W.apply(wo, "assign", actor=manager)
    W.apply(wo, "start", actor=manager)
    wo = ops.create_work_order(title="Fall leaf cleanup (demo)", vendor=green, actor=manager)
    W.apply(wo, "assign", actor=manager)
    W.apply(wo, "complete", actor=manager, note="Done (demo).")
    W.apply(wo, "close", actor=manager)
    wo = ops.create_work_order(title="Paint the gazebo (demo)", actor=manager)
    W.apply(wo, "cancel", actor=manager, note="Not this year (demo).")
    wo = ops.create_work_order(
        title="Plow contract walk-through (demo)",
        vendor=snow,
        priority="urgent",
        due_on=c.days(-5),
        actor=manager,
    )
    ops.set_work_order_follow_up(wo, c.days(-2), actor=manager)

    # Contracts: one waiting for its second approval, one discarded, one ended.
    plow = ops.create_contract(
        vendor=snow,
        title="Snow plowing, winter (demo)",
        category=cats["5200"],
        schedule="monthly",
        amount_cap_cents=150000,
        starts_on=c.t.replace(day=1),
        citation="Board resolution (demo)",
        actor=manager,
    )
    ops.approve_contract(plow, note="Approved (demo)", actor=pres)
    discard = ops.create_contract(
        vendor=pond,
        title="Fountain service (demo)",
        category=cats["5100"],
        schedule="monthly",
        amount_cap_cents=20000,
        starts_on=c.t.replace(day=1),
        citation="Proposal (demo)",
        actor=manager,
    )
    ops.CONTRACT.apply(discard, "discard", actor=manager)
    old = ops.create_contract(
        vendor=insurer,
        title="Insurance broker retainer (demo)",
        category=cats["5300"],
        schedule="annual",
        amount_cap_cents=30000,
        starts_on=datetime.date(c.t.year - 1, 1, 1),
        citation="Board resolution (demo)",
        actor=manager,
    )
    ops.approve_contract(old, note="Approved (demo)", actor=pres)
    old.refresh_from_db()
    ops.end_contract(old, ends_on=datetime.date(c.t.year - 1, 12, 31), actor=manager)

    # Invoices.
    def invoice(vendor, number, cents, code, **kw):
        inv = ops.create_invoice(
            vendor=vendor,
            number=number,
            invoice_date=c.t.replace(day=1),
            due_date=c.days(20),
            amount_cents=cents,
            category=cats[code],
            actor=manager,
            **kw,
        )
        ops.INVOICE.apply(inv, "submit", actor=manager)
        inv.refresh_from_db()
        return inv

    inv = invoice(green, "GL-60", 80000, "5200")
    ops.approve(inv, note="OK (demo)", actor=pres)
    invoice(pond, "PP-103", 18000, "5100")
    inv = invoice(insurer, "FI-9", 52000, "5300")
    ops.reject(inv, note="Wrong policy period (demo).", actor=treas)
    inv = ops.create_invoice(
        vendor=green,
        number="GL-57",
        invoice_date=c.t.replace(day=1),
        amount_cents=5000,
        category=cats["5200"],
        actor=manager,
    )
    ops.INVOICE.apply(inv, "void", actor=manager, note="Entered twice (demo).")
    inv = invoice(pond, "PP-104", 30000, "5100")
    inv = ops.approve(inv, actor=pres)
    ops.record_payment(
        inv, paid_on=c.t, method="check", amount_cents=10000, reference="1002", actor=manager
    )
    mowing = Contract.objects.get(title="Common area mowing (demo)")
    invoice(green, "GL-M1", 85000, "5200", contract=mowing)

    # Board tasks: open (first part), done, dropped, overdue.
    ops.create_task(title="Renew the D&O policy (demo)", due_date=c.days(-3), actor=manager)
    task = ops.create_task(title="Order new mailbox posts (demo)", actor=manager)
    ops.close_task(task, status="done", actor=manager)
    task = ops.create_task(title="Look into a dog park (demo)", actor=manager)
    ops.close_task(task, status="dropped", actor=manager)

    # Recurring deadlines: an overdue one, one due soon, one done.
    ops.save_compliance_item(
        title="File the annual corporate report (demo)",
        category="corporate",
        due_on=c.days(-4),
        recurrence="annual",
        remind_days=30,
        citation="15 Pa.C.S. (demo)",
        actor=manager,
    )
    item = ops.save_compliance_item(
        title="Pond inspection (demo)",
        category="contract",
        due_on=c.days(10),
        recurrence="quarterly",
        remind_days=14,
        actor=manager,
    )
    ops.complete_compliance_item(item, done_on=c.t, actor=manager)
    ops.save_compliance_item(
        title="Review the insurance certificates (demo)",
        category="insurance",
        due_on=c.days(6),
        recurrence="annual",
        remind_days=30,
        actor=manager,
    )
