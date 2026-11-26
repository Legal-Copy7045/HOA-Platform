"""My items: one queue of everything waiting on the signed-in person, with a
count per area for the board home. Each item links to the page where the
real workflow step is taken (approve, decide, reply...), not just a comment.

Items come from each area's own scoped queries, so nobody sees an item for
something they could not open.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from core import policy
from core.dates import short_date
from core.scoping import own_records


@dataclass
class Item:
    area: str
    title: str
    detail: str
    url: str
    tone: str = "info"  # info, warning, danger
    due: datetime.date | None = None
    # The sidebar entry that shows this item in its count bubble (a URL name,
    # with "?mine=1" for the homeowner side). Defaults from the area.
    nav: str = ""
    # Icon in lists; defaults from the area (UX review: one icon per area).
    icon: str = ""

    def __post_init__(self):
        if not self.nav:
            self.nav = AREA_NAV.get(self.area, "")
        if not self.icon:
            self.icon = AREA_ICONS.get(self.area, "clock")

    @property
    def overdue(self) -> bool:
        return bool(self.due and self.due < timezone.localdate())


# Who has a queue page (staff); owners see items waiting on them on Home.
MY_ITEMS_PERMISSIONS = (
    "request.manage",
    "compliance.manage",
    "account.approve_change",
    "invoice.approve",
    "arc.intake",
    "arc.decide",
    "task.manage",
)

AREA_NAV = {
    "Requests": "requests",
    "Architectural review": "arc_list",
    "Violations": "violations",
    "Collections": "collections",
    "Work orders": "work_orders",
    "Records requests": "records_requests",
    "Tasks": "tasks",
    "Vendors": "vendors",
    "Setup": "community_settings",
}

AREA_ICONS = {
    "My home": "home",
    "Requests": "mail",
    "Approvals": "check-circle",
    "Architectural review": "pencil",
    "Violations": "flag",
    "Collections": "dollar",
    "Finance": "receipt",
    "Work orders": "wrench",
    "Vendors": "shield",
    "Records requests": "inbox",
    "Compliance": "calendar",
    "Tasks": "list",
    "Setup": "sliders",
}

AREAS = [
    "My home",
    "Requests",
    "Approvals",
    "Architectural review",
    "Violations",
    "Collections",
    "Finance",
    "Work orders",
    "Vendors",
    "Records requests",
    "Compliance",
    "Tasks",
    "Setup",
]


def _due_tone(due, today) -> str:
    if due is None:
        return "info"
    if due < today:
        return "danger"
    return "warning" if due <= today + datetime.timedelta(days=2) else "info"


def items_for(user) -> list[Item]:
    roles = policy.roles_for(user)
    g = roles.groups
    can = policy.can
    today = timezone.localdate()
    out: list[Item] = []

    # Owner requests: assigned to me, or nobody's yet; overdue first.
    if can(user, "request.manage"):
        from django.db.models import Q

        from operations.models import OPEN_REQUEST_STATES, OwnerRequest

        mine = Q(assignee__isnull=True)
        if roles.person_id:
            mine |= Q(assignee_id=roles.person_id)
        for r in (
            OwnerRequest.objects.for_user(user)
            .filter(state__in=OPEN_REQUEST_STATES)
            .filter(mine)
            .exclude(state="waiting")
            .select_related("lot")
            .order_by("respond_by", "created_at")
        ):
            if r.first_response_at is None and r.respond_by:
                lead = "Response overdue since" if r.is_overdue else "Respond by"
                detail = f"{lead} {short_date(r.respond_by)}"
                tone = "danger" if r.is_overdue else _due_tone(r.respond_by, today)
            else:
                detail, tone = r.get_state_display(), "info"
            who = "unassigned" if r.assignee_id is None else "assigned to you"
            out.append(
                Item(
                    "Requests",
                    f"#{r.pk} {r.subject} (lot {r.lot.number})",
                    f"{r.get_request_type_display()} · {who} · {detail}",
                    reverse("request_detail", args=[r.pk]),
                    tone,
                    r.respond_by if r.first_response_at is None else None,
                )
            )

    # Invoices I can approve (not entered by me, not approved by me yet).
    if can(user, "invoice.approve"):
        from operations.models import Invoice, InvoiceApproval

        done = InvoiceApproval.objects.filter(approver=user).values_list("invoice_id", flat=True)
        for inv in (
            Invoice.objects.filter(state="pending_approval")
            .exclude(entered_by=user)
            .exclude(pk__in=done)
            .select_related("vendor")
        ):
            out.append(
                Item(
                    "Approvals",
                    f"Invoice {inv}",
                    "Needs your approval",
                    reverse("invoice_detail", args=[inv.pk]),
                    "warning",
                    inv.due_date,
                    nav="invoices",
                )
            )
    if can(user, "seat.manage"):
        from community.models import BoardSeatNomination

        for n in (
            BoardSeatNomination.objects.for_user(user).filter(outcome="").exclude(nominated_by=user)
        ):
            if n.person.user_id != user.pk:
                out.append(
                    Item(
                        "Approvals",
                        "Proposed board seat",
                        "Needs a second board member",
                        reverse("approvals"),
                        "warning",
                        nav="approvals",
                    )
                )
    if can(user, "role.sysadmin.approve"):
        from community.models import SysadminNomination

        for n in SysadminNomination.objects.for_user(user):
            if n.is_open and n.nominated_by_id != user.pk:
                out.append(
                    Item(
                        "Approvals",
                        "Sysadmin nomination",
                        "Needs a second board member",
                        reverse("approvals"),
                        "warning",
                        nav="approvals",
                    )
                )

    if can(user, "mfa_reset.approve"):
        from accounts.models import MfaReset

        for reset in MfaReset.objects.for_user(user):
            if reset.status == "awaiting_approval" and reset.started_by_id != user.pk:
                out.append(
                    Item(
                        "Approvals",
                        "Sign-in reset",
                        "Needs a second board member",
                        reverse("approvals"),
                        "warning",
                        nav="approvals",
                    )
                )
    if can(user, "invoice.record_payment"):
        from operations.models import Invoice

        to_pay = Invoice.objects.filter(state="approved").count()
        if to_pay:
            out.append(
                Item(
                    "Finance",
                    f"{to_pay} approved invoice{'' if to_pay == 1 else 's'} to pay",
                    "Record the payment when the check is cut",
                    reverse("invoices") + "?state=approved",
                    "info",
                    nav="invoices",
                )
            )

    if can(user, "account.approve_change"):
        from community.models import AccountChangeRequest

        for ch in (
            AccountChangeRequest.objects.for_user(user)
            .filter(state="pending")
            .exclude(requested_by=user)
            .select_related("account__lot")
        ):
            out.append(
                Item(
                    "Approvals",
                    f"Lot {ch.account.lot.number}: {ch.get_action_display().lower()}"
                    " on the account",
                    "An owner asked for this change",
                    reverse("lot_detail", args=[ch.account.lot_id]) + "#accounts",
                    "warning",
                    nav="lot_list",
                )
            )

    # Architectural review: intake, and requests I must still take a position on.
    if can(user, "arc.view_all"):
        from arc.models import ArcRequest, ReviewerVote
        from arc.services import is_recused

        arc = ArcRequest.objects.for_user(user)
        if can(user, "arc.intake"):
            for r in arc.filter(state="submitted"):
                out.append(
                    Item(
                        "Architectural review",
                        f"ARC #{r.pk} {r.title}",
                        "Check it is complete to start the review clock",
                        reverse("arc_detail", args=[r.pk]),
                        "info",
                    )
                )
        if can(user, "arc.decide"):
            taken = set(
                ReviewerVote.objects.filter(reviewer=user).values_list("request_id", flat=True)
            )
            for r in arc.filter(state="in_review").order_by("review_due_on"):
                if r.pk in taken or is_recused(user, r):
                    continue
                out.append(
                    Item(
                        "Architectural review",
                        f"ARC #{r.pk} {r.title}",
                        "Your position is needed"
                        + (
                            f" · decision due {short_date(r.review_due_on)}"
                            if r.review_due_on
                            else ""
                        ),
                        reverse("arc_detail", args=[r.pk]),
                        _due_tone(r.review_due_on, today),
                        r.review_due_on,
                    )
                )

    # Follow-ups due (today or earlier) and owner responses to review.
    if can(user, "violation.manage"):
        from compliance.models import Violation

        v = Violation.objects.for_user(user)
        for x in v.filter(follow_up_on__lte=today).exclude(
            state__in=["cured", "dismissed", "closed"]
        ):
            out.append(
                Item(
                    "Violations",
                    f"{x} (lot {x.lot.number})",
                    f"Follow up due {short_date(x.follow_up_on)}",
                    reverse("violation_detail", args=[x.pk]),
                    _due_tone(x.follow_up_on, today),
                    x.follow_up_on,
                )
            )
        for x in v.filter(state="responded"):
            out.append(
                Item(
                    "Violations",
                    f"{x} (lot {x.lot.number})",
                    "The owner responded",
                    reverse("violation_detail", args=[x.pk]),
                    "info",
                )
            )
    if can(user, "collections.manage"):
        from ledger.models import CollectionCase

        for c in (
            CollectionCase.objects.for_user(user)
            .filter(follow_up_on__lte=today)
            .exclude(state="closed")
            .select_related("lot")
        ):
            out.append(
                Item(
                    "Collections",
                    f"{c} (lot {c.lot.number})",
                    f"Follow up due {short_date(c.follow_up_on)}",
                    reverse("collection_detail", args=[c.pk]),
                    _due_tone(c.follow_up_on, today),
                    c.follow_up_on,
                )
            )
    if can(user, "workorder.manage"):
        from operations.models import WorkOrder

        for w in WorkOrder.objects.filter(follow_up_on__lte=today).exclude(
            state__in=["closed", "cancelled"]
        ):
            out.append(
                Item(
                    "Work orders",
                    str(w),
                    f"Follow up due {short_date(w.follow_up_on)}",
                    reverse("work_order_detail", args=[w.pk]),
                    _due_tone(w.follow_up_on, today),
                    w.follow_up_on,
                )
            )
    if can(user, "vendor.manage"):
        from operations.models import Vendor

        for vendor in Vendor.objects.filter(
            active=True,
            insurance_expires_on__isnull=False,
            insurance_expires_on__lte=today + datetime.timedelta(days=30),
        ):
            gone = vendor.insurance_expires_on < today
            out.append(
                Item(
                    "Vendors",
                    f"{vendor.name}: insurance {'expired' if gone else 'expires'} "
                    f"{short_date(vendor.insurance_expires_on)}",
                    "Ask for a new certificate",
                    reverse("vendor_detail", args=[vendor.pk]),
                    "danger" if gone else "warning",
                    vendor.insurance_expires_on,
                )
            )
    # Recurring deadlines (Board tasks tab): hidden with the "compliance" feature.
    from core.features import enabled as feature_on

    if can(user, "compliance.manage") and feature_on("compliance"):
        from operations.services import due_soon

        for c in due_soon(today):
            out.append(
                Item(
                    "Compliance",
                    c.title,
                    f"{'Overdue since' if c.due_on < today else 'Due'} {short_date(c.due_on)}",
                    reverse("tasks") + "?tab=deadlines",
                    _due_tone(c.due_on, today),
                    c.due_on,
                    nav="tasks",
                )
            )
    if can(user, "records_request.manage"):
        from governance.models import RecordsRequest

        for r in RecordsRequest.objects.for_user(user).filter(
            state__in=["submitted", "in_progress"]
        ):
            out.append(
                Item(
                    "Records requests",
                    f"Records request #{r.pk}",
                    f"Due {short_date(r.due_on)}" if r.due_on else "No due date",
                    reverse("records_request_detail", args=[r.pk]),
                    _due_tone(r.due_on, today),
                    r.due_on,
                )
            )
    if can(user, "task.manage") and roles.person_id:
        from operations.models import BoardTask

        for t in BoardTask.objects.filter(status="open", assignee_id=roles.person_id):
            out.append(
                Item(
                    "Tasks",
                    t.title,
                    f"Due {short_date(t.due_date)}" if t.due_date else "Assigned to you",
                    reverse("tasks"),
                    _due_tone(t.due_date, today),
                    t.due_date,
                )
            )

    # Reservations, access review (Milestone 4).
    if can(user, "reservation.manage"):
        from community.reservations import pending_for_staff

        for r in pending_for_staff(user):
            out.append(
                Item(
                    "Approvals",
                    f"Booking: {r.area.name} (lot {r.lot.number})",
                    f"Confirm or decline · {timezone.localtime(r.starts_at):%b %-d, %-I:%M %p}",
                    reverse("reservation_detail", args=[r.pk]),
                    "warning",
                    timezone.localdate(r.starts_at),
                    nav="reservations",
                )
            )
    if can(user, "access_review.record"):
        from accounts.access_review import REVIEW_EVERY_DAYS, is_due

        if is_due():
            out.append(
                Item(
                    "Compliance",
                    "Quarterly access review",
                    f"Review every login's access (due every {REVIEW_EVERY_DAYS} days)",
                    reverse("access_review"),
                    "warning",
                    nav="access_review",
                )
            )
    if can(user, "payments.manage"):
        out.extend(_online_payment_items(user, roles))
    if can(user, "community.manage_rules"):
        from community.rules import rules_needing_attention

        missing = len(rules_needing_attention())
        if missing:
            out.append(
                Item(
                    "Setup",
                    f"{missing} governing rule{' needs' if missing == 1 else 's need'}"
                    " a value or citation",
                    "Community settings",
                    reverse("community_settings"),
                    "warning",
                )
            )
        thin = policy.thinly_covered_actions()
        if thin:
            out.append(
                Item(
                    "Setup",
                    "Fewer than two people with two-step sign-in can act",
                    f"{len(thin)} action{'' if len(thin) == 1 else 's'}: invite another"
                    " board member",
                    reverse("board_home"),
                    "warning",
                    nav="board_home",
                )
            )

    # Homeowner side: things waiting on them as an owner.
    if g & policy.RESIDENTS:
        from operations.models import OwnerRequest

        for r in own_records(OwnerRequest.objects.for_user(user), user).filter(
            state="waiting", requested_by=user
        ):
            out.append(
                Item(
                    "My home",
                    f"#{r.pk} {r.subject}",
                    "The board asked you for more information",
                    reverse("request_detail", args=[r.pk]),
                    "warning",
                    nav="requests?mine=1",
                )
            )
    if g & {policy.OWNER}:
        from compliance.models import Violation

        for v in own_records(Violation.objects.for_user(user), user).filter(
            state__in=["noticed", "hearing"]
        ):
            if v.state == "hearing":
                detail = "A hearing is scheduled: see the notice"
            elif v.cure_by:
                detail = f"Please fix by {short_date(v.cure_by)}"
            else:
                detail = "Please review and respond"
            out.append(
                Item(
                    "My home",
                    f"Notice: {v.get_category_display()}",
                    detail,
                    reverse("violation_detail", args=[v.pk]),
                    _due_tone(v.cure_by, today),
                    v.cure_by,
                    nav="violations?mine=1",
                )
            )
    if can(user, "arc.submit") and roles.owned_lot_ids:
        from arc.models import NEIGHBOR_VISIBLE_STATES, SignoffRequest

        # The owner side (SEC-001), also for a board member who owns a lot.
        for so in (
            own_records(SignoffRequest.objects.for_user(user), user)
            .filter(
                lot_id__in=roles.owned_lot_ids,
                state="pending",
                sent_at__isnull=False,
                arc_request__state__in=NEIGHBOR_VISIBLE_STATES,
            )
            .select_related("arc_request__lot")
        ):
            out.append(
                Item(
                    "My home",
                    f"Acknowledge: {so.arc_request.title}",
                    f"Your neighbor at {so.arc_request.lot.street_address} asks you to review "
                    "a project",
                    reverse("signoff_detail", args=[so.pk]),
                    "info",
                    nav="signoffs_mine",
                )
            )
    if can(user, "vote.cast"):
        from governance.models import Vote
        from governance.services import ballot_choices_for

        for vote in Vote.objects.filter(status="open"):
            if ballot_choices_for(vote, user)["can_vote"]:
                out.append(
                    Item(
                        "My home",
                        f"Vote: {vote.title}",
                        "Your ballot is open",
                        reverse("vote_detail", args=[vote.pk]),
                        "info",
                        nav="voting",
                        icon="vote",
                    )
                )
    if can(user, "vote.view") and roles.person_id:
        # Own lots and lots whose proxy this person holds.
        from governance import elections
        from governance.models import Election, ElectionState

        for election in Election.objects.filter(state=ElectionState.VOTING):
            lots = elections.lots_actor_may_vote_for(election, user)
            if lots:
                out.append(
                    Item(
                        "My home",
                        f"Election: {election.title}",
                        "Your ballot is open"
                        + (f" ({len(lots)} lots: one ballot each)" if len(lots) > 1 else ""),
                        reverse("election_detail", args=[election.pk]),
                        "info",
                        nav="voting",
                        icon="vote",
                    )
                )
    # --- Surveys (M5): open surveys this person is asked to answer ---
    if can(user, "survey.respond"):
        from governance import surveys

        for survey in surveys.waiting_for(user):
            out.append(
                Item(
                    "My home",
                    f"Survey: {survey.title}",
                    f"Survey waiting for your answer · closes {short_date(survey.closes_on)}",
                    reverse("survey_detail", args=[survey.pk]),
                    "info",
                    survey.closes_on,
                    nav="voting",
                    icon="list",
                )
            )
    if can(user, "ledger.view_own"):
        out.extend(_balance_items(user, roles))

    order = {a: i for i, a in enumerate(AREAS)}
    out.sort(key=lambda i: (order.get(i.area, 99), not i.overdue, i.due or datetime.date.max))
    from core import features

    states = features.states()  # one query, not one per item (optimization A9)
    return [i for i in out if features.path_enabled(i.url, states_=states)]


def _online_payment_items(user, roles) -> list[Item]:
    """Online payment problems for the board (D32-payments): mismatches,
    returned bank payments, refunds and disputes to record, surcharges to
    refund. Reconciliation differences go to the treasurer (or the whole
    board while nobody holds that office)."""
    from payments.models import AlertKind, PaymentAlert

    alerts = list(
        PaymentAlert.objects.for_user(user).filter(resolved_at__isnull=True).select_related("lot")
    )
    if not alerts:
        return []
    treasurer = "treasurer" in roles.offices
    if not treasurer and any(a.kind == AlertKind.RECONCILIATION for a in alerts):
        from community.models import OfficeHolding
        from core.scoping import system_access

        with system_access("treasurer exists"):
            someone = OfficeHolding.objects.filter(office="treasurer").active().exists()
        treasurer = not someone
    out = []
    for a in alerts:
        if a.kind == AlertKind.RECONCILIATION and not treasurer:
            continue
        out.append(
            Item(
                "Finance",
                a.get_kind_display() + (f" (lot {a.lot.number})" if a.lot_id else ""),
                a.detail,
                reverse("online_payments"),
                "danger" if a.kind in (AlertKind.MISMATCH, AlertKind.RECONCILIATION) else "warning",
                timezone.localdate(a.created_at),
                nav="online_payments",
            )
        )
    return out


def oldest_unpaid_on(statement, since) -> datetime.date | None:
    """The posting date of the oldest charge not yet covered by payments
    (payments and credits pay the oldest charges first)."""
    charges = []  # [posted_on, remaining]
    if statement.brought_forward > 0:
        charges.append([since, statement.brought_forward])
    credit = -statement.brought_forward if statement.brought_forward < 0 else 0
    for entry in sorted(statement.entries, key=lambda e: (e.posted_on, e.pk or 0)):
        if entry.amount_cents > 0:
            charges.append([entry.posted_on, entry.amount_cents])
        else:
            credit += -entry.amount_cents
    for charge in charges:
        used = min(credit, charge[1])
        charge[1] -= used
        credit -= used
        if charge[1] > 0:
            return charge[0]
    return None


def _balance_items(user, roles) -> list[Item]:
    """A balance due on one of my accounts, with the amount and since when."""
    from community.models import Lot
    from core.money import format_cents
    from ledger.services import owner_statement

    out = []
    for lot in Lot.objects.for_user(user).filter(pk__in=roles.account_lot_ids).order_by("number"):
        since = roles.lot_since.get(lot.pk)
        statement = owner_statement(lot.pk, since)
        if statement.balance <= 0:
            continue
        oldest = oldest_unpaid_on(statement, since)
        # A charge posted ahead of its month is due by that date, not overdue
        # since it (review BUG-005).
        upcoming = oldest is not None and oldest > timezone.localdate()
        when = f" · due {'by' if upcoming else 'since'} {short_date(oldest)}" if oldest else ""
        out.append(
            Item(
                "My home",
                f"{format_cents(statement.balance)} due on lot {lot.number}",
                f"See your account and how to pay{when}",
                reverse("my_account"),
                "info" if upcoming else "warning",
                oldest,
                nav="my_account",
                icon="wallet",
            )
        )
    return out


def counts(items: list[Item]) -> list[dict]:
    """Per-area counts (with overdue counts) for the board home."""
    by_area: dict[str, dict] = {}
    for it in items:
        row = by_area.setdefault(it.area, {"area": it.area, "count": 0, "overdue": 0})
        row["count"] += 1
        row["overdue"] += it.overdue
    order = {a: i for i, a in enumerate(AREAS)}
    return sorted(by_area.values(), key=lambda r: order.get(r["area"], 99))


def items_for_request(request) -> list[Item]:
    """items_for(request.user), computed once per request: the sidebar counts
    and the page itself (dashboard, My items, Board overview) share it.

    Not cached across requests: a count must drop the moment the step is
    taken, and the items come from many apps' records, so there is no single
    place to invalidate a longer-lived cache."""
    cached = getattr(request, "_my_items", None)
    if cached is None:
        cached = request._my_items = items_for(request.user)
    return cached


def nav_counts(user, request=None) -> dict[str, int]:
    """Items waiting on this person, by sidebar entry, plus the total
    ("my_items"). Computed once per page by core.navigation."""
    counts: dict[str, int] = {}
    items = items_for_request(request) if request is not None else items_for(user)
    for it in items:
        if it.nav:
            counts[it.nav] = counts.get(it.nav, 0) + 1
    counts["my_items"] = len(items)
    return counts
