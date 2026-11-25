"""What each person sees on their home page: their numbers and their to-dos.

Everything goes through the same permission table and scoped managers as the
pages it links to, so a dashboard never shows more than the user could open.
"""

from __future__ import annotations

import datetime

from django.urls import reverse
from django.utils import timezone

from core import policy


def _can(groups, action) -> bool:
    return bool(policy.PERMISSIONS[action] & groups)


def _n(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _todo(icon, title, detail, url, tone="info"):
    return {"icon": icon, "title": title, "detail": detail, "url": url, "tone": tone}


def owner_panel(user, roles) -> dict:
    from arc.models import ArcRequest
    from comms.models import Announcement, AnnouncementRead
    from comms.services import upcoming
    from community.models import Lot
    from compliance.models import Violation
    from governance.models import Vote
    from governance.services import ballot_choices_for
    from ledger.services import owner_statement

    accounts = []
    since = roles.lot_since
    for lot in Lot.objects.for_user(user).filter(pk__in=roles.account_lot_ids):
        start = since.get(lot.pk)
        accounts.append({"lot": lot, "balance": owner_statement(lot.pk, start).balance})
    open_arc = (
        ArcRequest.objects.for_user(user)
        .filter(lot_id__in=roles.account_lot_ids)
        .exclude(state__in=["completed", "withdrawn", "denied", "appeal_decided"])
        .count()
    )
    notices = Violation.objects.for_user(user).filter(state__in=["noticed", "responded", "hearing"])
    votes = [
        v for v in Vote.objects.filter(status="open") if ballot_choices_for(v, user)["can_vote"]
    ]
    read = set(AnnouncementRead.objects.filter(user=user).values_list("announcement_id", flat=True))
    news = [(a, a.pk in read) for a in Announcement.objects.for_user(user)[:4]]
    todos = []
    for v in votes:
        todos.append(
            _todo(
                "vote",
                f"Vote: {v.title}",
                "Your ballot is open",
                reverse("vote_detail", args=[v.pk]),
                "info",
            )
        )
    for n in notices:
        todos.append(
            _todo(
                "flag",
                f"Notice: {n.get_category_display()}",
                f"Cure by {n.cure_by:%b %-d}" if n.cure_by else "Please review",
                reverse("violation_detail", args=[n.pk]),
                "warning",
            )
        )
    from operations.models import OwnerRequest

    for r in OwnerRequest.objects.for_user(user).filter(state="waiting", requested_by=user):
        todos.append(
            _todo(
                "mail",
                f"Request #{r.pk}: {r.subject}",
                "The board asked you for more information",
                reverse("request_detail", args=[r.pk]),
                "warning",
            )
        )
    for a in accounts:
        if a["balance"] > 0:
            todos.append(
                _todo(
                    "wallet",
                    f"Lot {a['lot'].number} has a balance due",
                    "See your account",
                    reverse("my_account"),
                    "warning",
                )
            )
    return {
        "accounts": accounts,
        "open_arc": open_arc,
        "notices": notices.count(),
        "votes": len(votes),
        "news": news,
        "events": list(upcoming(user)[:4]),
        "todos": todos,
    }


def staff_panel(user, roles) -> dict:
    from accounts.models import MfaReset
    from arc.models import ArcRequest
    from community.models import SysadminNomination
    from community.rules import rules_needing_attention
    from compliance.models import Violation
    from governance.models import RecordsRequest
    from ledger.models import CollectionCase
    from ledger.services import balances
    from operations.models import BoardTask, Invoice, InvoiceApproval, Vendor, WorkOrder

    g = roles.groups
    today = timezone.localdate()
    soon = today + datetime.timedelta(days=7)
    todos, stats = [], []

    if _can(g, "invoice.approve"):
        mine = set(
            InvoiceApproval.objects.filter(approver=user).values_list("invoice_id", flat=True)
        )
        waiting = (
            Invoice.objects.filter(state="pending_approval")
            .exclude(entered_by=user)
            .exclude(pk__in=mine)
        )
        if waiting.exists():
            todos.append(
                _todo(
                    "receipt",
                    f"{_n(waiting.count(), 'invoice needs', 'invoices need')} your approval",
                    "You did not enter these, so you can approve them",
                    reverse("invoices") + "?state=pending_approval",
                    "warning",
                )
            )
    if _can(g, "invoice.record_payment"):
        to_pay = Invoice.objects.filter(state="approved").count()
        if to_pay:
            todos.append(
                _todo(
                    "dollar",
                    f"{_n(to_pay, 'approved invoice', 'approved invoices')} to pay",
                    "Record the payment when the check is cut",
                    reverse("invoices") + "?state=approved",
                    "info",
                )
            )
    if _can(g, "role.sysadmin.approve"):
        nominations = [n for n in SysadminNomination.objects.for_user(user) if n.is_open]
        resets = [r for r in MfaReset.objects.for_user(user) if r.status == "awaiting_approval"]
        waiting = [x for x in nominations if x.nominated_by_id != user.pk] + [
            r for r in resets if r.started_by_id != user.pk
        ]
        if waiting:
            todos.append(
                _todo(
                    "check-circle",
                    _n(len(waiting), "approval", "approvals")
                    + " waiting for a second board member",
                    "Sysadmin or sign-in reset",
                    reverse("approvals"),
                    "warning",
                )
            )
    if _can(g, "arc.view_all"):
        arc = ArcRequest.objects.for_user(user)
        intake = arc.filter(state="submitted").count()
        if intake and _can(g, "arc.intake"):
            todos.append(
                _todo(
                    "pencil",
                    f"{_n(intake, 'ARC request', 'ARC requests')} to check for completeness",
                    "Start the review clock",
                    reverse("arc_list"),
                    "info",
                )
            )
        due = arc.filter(state="in_review", review_due_on__lte=soon).order_by("review_due_on")
        for r in due[:5]:
            late = r.review_due_on and r.review_due_on < today
            todos.append(
                _todo(
                    "clock",
                    f"ARC #{r.pk} {r.title}",
                    f"{'Overdue since' if late else 'Decision due'} {r.review_due_on:%b %-d}",
                    reverse("arc_detail", args=[r.pk]),
                    "danger" if late else "warning",
                )
            )
    if _can(g, "violation.view_all"):
        responded = Violation.objects.for_user(user).filter(state="responded").count()
        if responded:
            todos.append(
                _todo(
                    "flag",
                    f"{_n(responded, 'owner response', 'owner responses')} to review",
                    "Violations",
                    reverse("violations"),
                    "info",
                )
            )
    if _can(g, "records_request.manage"):
        for r in (
            RecordsRequest.objects.for_user(user)
            .filter(state__in=["submitted", "in_progress"])
            .order_by("due_on")[:5]
        ):
            late = r.due_on and r.due_on < today
            todos.append(
                _todo(
                    "inbox",
                    f"Records request #{r.pk}",
                    f"Due {r.due_on:%b %-d}" if r.due_on else "No due date",
                    reverse("records_request_detail", args=[r.pk]),
                    "danger" if late else "info",
                )
            )
    if _can(g, "vendor.manage"):
        expiring = Vendor.objects.filter(
            active=True,
            insurance_expires_on__isnull=False,
            insurance_expires_on__lte=today + datetime.timedelta(days=30),
        )
        for v in expiring[:3]:
            todos.append(
                _todo(
                    "shield",
                    f"{v.name}: insurance "
                    f"{'expired' if v.insurance_expires_on < today else 'expires'}"
                    f" {v.insurance_expires_on:%b %-d}",
                    "Ask for a new certificate",
                    reverse("vendor_detail", args=[v.pk]),
                    "warning",
                )
            )
    if _can(g, "task.manage") and roles.person_id:
        mine = BoardTask.objects.filter(status="open", assignee_id=roles.person_id)
        for t in mine[:3]:
            todos.append(
                _todo(
                    "list",
                    t.title,
                    f"Due {t.due_date:%b %-d}" if t.due_date else "Assigned to you",
                    reverse("tasks"),
                    "info",
                )
            )
    if _can(g, "community.manage_rules"):
        missing = len(rules_needing_attention())
        if missing:
            todos.append(
                _todo(
                    "sliders",
                    _n(missing, "governing rule needs", "governing rules need")
                    + " a value or citation",
                    "Community settings",
                    reverse("community_settings"),
                    "warning",
                )
            )
        thin = policy.thinly_covered_actions()
        if thin:
            todos.append(
                _todo(
                    "users",
                    "Fewer than two people with MFA can act",
                    f"{_n(len(thin), 'action', 'actions')} - invite another board member",
                    reverse("board_home"),
                    "warning",
                )
            )

    if _can(g, "finance.view"):
        bal = balances()
        owed = sum(v for v in bal.values() if v > 0)
        behind = sum(1 for v in bal.values() if v > 0)
        stats.append(
            {
                "label": "Owed by owners",
                "value": owed,
                "money": True,
                "hint": f"{_n(behind, 'lot', 'lots')} with a balance",
                "url": reverse("ledger_home"),
                "tone": "warning" if owed else "success",
            }
        )
        stats.append(
            {
                "label": "Invoices awaiting approval",
                "value": Invoice.objects.filter(state="pending_approval").count(),
                "url": reverse("invoices") + "?state=pending_approval",
            }
        )
        stats.append(
            {
                "label": "Open work orders",
                "value": WorkOrder.objects.exclude(state__in=["closed", "cancelled"]).count(),
                "url": reverse("work_orders"),
            }
        )
        stats.append(
            {
                "label": "Collection cases",
                "value": CollectionCase.objects.for_user(user).exclude(state="closed").count(),
                "url": reverse("collections"),
            }
        )
    return {"todos": todos, "stats": stats}


def build(user) -> dict:
    roles = policy.roles_for(user)
    data = {"roles": roles}
    if roles.groups & policy.ACCOUNT_VIEWERS:
        data["owner"] = owner_panel(user, roles)
    if roles.groups & {policy.BOARD, policy.MANAGER, policy.AUDITOR, policy.ARC_REVIEWER}:
        data["staff"] = staff_panel(user, roles)
    from comms.models import Announcement, AnnouncementRead
    from comms.services import upcoming

    data["news"], data["events"] = [], []
    if policy.can(user, "announcement.view"):
        read = set(
            AnnouncementRead.objects.filter(user=user).values_list("announcement_id", flat=True)
        )
        data["news"] = [(a, a.pk in read) for a in Announcement.objects.for_user(user)[:4]]
        data["events"] = list(upcoming(user)[:4])
    return data
