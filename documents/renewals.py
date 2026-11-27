"""Renewal and expiry tracking (D72).

The association is self-managed, so nobody professional watches its
renewals. Three kinds of item are tracked, each with a deadline:

* the association's own insurance policies (``InsurancePolicy``): the end of
  the term, unless a later term has been recorded as its renewal;
* vendors' insurance certificates: vendor onboarding's expiry (D79), else
  the vendor's ``insurance_expires_on`` (set by each new
  ``VendorCertificate``), for active vendors;
* active contracts with an end date: for a contract that renews by itself
  (``ContractTerms.renewal == auto``) the last day to give notice of
  cancellation (end less ``notice_days``); otherwise the end itself. Once a
  self-renewing contract's end passes without a decision to cancel, the next
  term (``renews_for_months``) is tracked.

From rule ``renewals.*_days`` before its deadline an item is on the board's
My items and the Renewals page; past it, it is flagged red ("expired").
The board gets one email per item and threshold (the rule's days, then 30,
7 and on the day), recorded in append-only ``RenewalReminder`` rows so a
threshold is never mailed twice. A contract's term is settled by a board
decision (``RenewalDecision``: renew, cancel or let it end; never the
manager); a policy by recording its next term; a certificate by recording
a new one.

Recording (policies, certificates, contract terms) is data entry: board or
manager (``renewals.record``). Every document attached becomes a version in
the document library (category Insurance or Contracts, board only), so the
retention schedule (documents/retention.py) applies to it.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from community.rules import rule
from core.policy import SYSTEM, require
from core.scoping import system_access
from documents.models import (
    Category,
    ContractTerms,
    InsurancePolicy,
    PolicyKind,
    RenewalChoice,
    RenewalDecision,
    RenewalReminder,
    RenewalType,
    VendorCertificate,
    Visibility,
)

POLICY, VENDOR, CONTRACT = "policy", "vendor", "contract"
RULES = {
    POLICY: "renewals.insurance_reminder_days",
    VENDOR: "renewals.vendor_certificate_reminder_days",
    CONTRACT: "renewals.contract_notice_lead_days",
}
# Emails after the first reminder: this many days before the deadline.
THRESHOLDS = (30, 7, 0)


class RenewalError(ValueError):
    pass


def reminder_days(kind: str) -> int:
    return int(rule(RULES[kind]) or 0)


def add_months(day: datetime.date, months: int) -> datetime.date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass
class Renewal:
    kind: str
    obj: object
    title: str
    what: str  # "Policy ends", "Certificate expires", ...
    deadline: datetime.date
    days: int  # reminder window (rule)
    url: str
    today: datetime.date
    term_ends_on: datetime.date | None = None  # contracts
    decision: RenewalDecision | None = None

    @property
    def days_left(self) -> int:
        return (self.deadline - self.today).days

    @property
    def expired(self) -> bool:
        return self.decision is None and self.deadline < self.today

    @property
    def due(self) -> bool:
        """In the reminder window (or past the deadline) and not settled."""
        return self.decision is None and self.days_left <= self.days

    @property
    def tone(self) -> str:
        if self.expired:
            return "danger"
        return "warning" if self.due else "success"

    @property
    def status(self) -> str:
        if self.decision is not None:
            return f"Decided: {self.decision.get_decision_display().lower()}"
        if self.expired:
            return "Expired" if self.kind != CONTRACT else "Deadline passed"
        if self.days_left == 0:
            return "Due today"
        if self.due:
            return f"Due in {self.days_left} day{'s' if self.days_left != 1 else ''}"
        return "Current"

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.obj.pk)


# --- What is tracked ----------------------------------------------------------------------


def _policies(today) -> list[Renewal]:
    days = reminder_days(POLICY)
    out = []
    for p in InsurancePolicy.objects.filter(renewed_by__isnull=True):
        out.append(
            Renewal(
                POLICY,
                p,
                f"{p.get_kind_display()} insurance ({p.carrier})",
                "Policy ends",
                p.ends_on,
                days,
                reverse("renewal_policy", args=[p.pk]),
                today,
            )
        )
    return out


def vendor_coi_expiry(vendor) -> datetime.date | None:
    """The day the vendor's certificate of insurance expires, or None when
    none is on file. The one place renewals reads it: vendor onboarding's
    ``coi_expires_on`` (D79), which falls back to the vendor's own
    ``insurance_expires_on`` for vendors older than onboarding (and takes the
    later of the two, so a certificate recorded here after onboarding counts)."""
    from operations import vendor_onboarding

    expires = vendor_onboarding.coi_expires_on(vendor)
    return expires if expires is not None else vendor.insurance_expires_on


def _vendors(today) -> list[Renewal]:
    from operations.models import Vendor

    days = reminder_days(VENDOR)
    out = []
    for v in Vendor.objects.filter(active=True):
        expires = vendor_coi_expiry(v)
        if expires is None:
            continue
        out.append(
            Renewal(
                VENDOR,
                v,
                f"{v.name}: insurance certificate",
                "Certificate expires",
                expires,
                days,
                reverse("renewal_vendor", args=[v.pk]),
                today,
            )
        )
    return out


def contract_term(contract, terms: ContractTerms | None, today, decisions: dict) -> tuple:
    """(term end, deadline, decision) of the contract's current term."""
    end = contract.ends_on
    auto = terms is not None and terms.renewal == RenewalType.AUTO
    if auto and terms.renews_for_months:
        # A term that passed without a decision to cancel renewed by itself.
        for _ in range(600):
            decision = decisions.get((contract.pk, end))
            if end >= today or (decision is not None and decision.decision != RenewalChoice.RENEW):
                break
            end = add_months(end, terms.renews_for_months)
    deadline = end - datetime.timedelta(days=terms.notice_days) if auto else end
    return end, deadline, decisions.get((contract.pk, end))


def _contracts(today) -> list[Renewal]:
    from operations.models import Contract

    days = reminder_days(CONTRACT)
    contracts = list(
        Contract.objects.filter(state="active", ends_on__isnull=False).select_related("vendor")
    )
    terms = {t.contract_id: t for t in ContractTerms.objects.filter(contract__in=contracts)}
    decisions = {
        (d.contract_id, d.term_ends_on): d
        for d in RenewalDecision.objects.filter(contract__in=contracts)
    }
    out = []
    for c in contracts:
        t = terms.get(c.pk)
        end, deadline, decision = contract_term(c, t, today, decisions)
        if decision is not None and end < today:
            continue  # decided, and that term is over
        auto = t is not None and t.renewal == RenewalType.AUTO
        out.append(
            Renewal(
                CONTRACT,
                c,
                f"{c.vendor.name}: {c.title}",
                "Notice to cancel due" if auto and t.notice_days else "Contract ends",
                deadline,
                days,
                reverse("renewal_contract", args=[c.pk]),
                today,
                term_ends_on=end,
                decision=decision,
            )
        )
    return out


def tracked(today: datetime.date | None = None) -> list[Renewal]:
    """Every tracked item, soonest deadline first."""
    today = today or timezone.localdate()
    with system_access("renewals: tracked items"):
        items = _policies(today) + _vendors(today) + _contracts(today)
    return sorted(items, key=lambda r: (r.decision is not None, r.deadline, r.title))


def due(today: datetime.date | None = None) -> list[Renewal]:
    return [r for r in tracked(today) if r.due]


def overdue_count(today: datetime.date | None = None) -> int:
    """Items past their deadline with nothing recorded (a policy not renewed,
    a certificate not replaced, a contract term not decided). For a health
    check line (the monitoring work may use it)."""
    return sum(1 for r in tracked(today) if r.expired)


def current_policy(kind: str = PolicyKind.GENERAL_LIABILITY, on=None) -> InsurancePolicy | None:
    on = on or timezone.localdate()
    return (
        InsurancePolicy.objects.filter(kind=kind, starts_on__lte=on, ends_on__gte=on)
        .order_by("-starts_on", "-id")
        .first()
    )


def resale_insurance(profile) -> tuple[int | None, str]:
    """(liability coverage in cents, carrier) for the resale certificate
    (68 Pa.C.S. § 5407): the current general liability policy when one is
    recorded, else the amounts typed on Letters and certificates."""
    policy = current_policy()
    if policy is not None:
        return policy.coverage_cents, policy.carrier
    return profile.resale_insurance_cents, profile.resale_insurance_company


# --- My items and reminder emails -----------------------------------------------------------


def my_items(user, today=None) -> list:
    from core import policy
    from core.dates import short_date
    from core.my_items import Item

    if not policy.can(user, "renewals.record"):
        return []
    out = []
    for r in due(today):
        verb = {
            POLICY: "Renew the policy or record the next term",
            VENDOR: "Ask the vendor for a new certificate",
            CONTRACT: "Board decision: renew, cancel or let it end",
        }[r.kind]
        when = "was" if r.expired else "is"
        out.append(
            Item(
                "Renewals",
                f"{r.title}: {r.what.lower()} {short_date(r.deadline)}",
                f"{verb} (deadline {when} {short_date(r.deadline)})",
                r.url,
                "danger" if r.expired else "warning",
                r.deadline,
                nav="renewals",
                icon="shield",
            )
        )
    return out


def thresholds(days: int) -> list[int]:
    return sorted({days, *(t for t in THRESHOLDS if t <= days)}, reverse=True)


def send_reminders(today: datetime.date | None = None) -> int:
    """Email the board about items that crossed a threshold since the last
    email (one email per run, one reminder per item and threshold)."""
    from django.conf import settings

    from comms.notify import mail_board
    from community.services import board_emails
    from core.dates import short_date

    today = today or timezone.localdate()
    fresh = []
    for r in due(today):
        crossed = [t for t in thresholds(r.days) if r.days_left <= t]
        if not crossed:
            continue
        threshold = min(crossed)
        with system_access("renewal reminders"):
            exists = RenewalReminder.objects.filter(
                kind=r.kind, object_id=r.obj.pk, deadline=r.deadline, threshold=threshold
            ).exists()
        if not exists:
            fresh.append((r, threshold))
    if not fresh:
        return 0
    lines = "\n".join(
        f"- {r.title}: {r.what.lower()} {short_date(r.deadline)}"
        + (" (passed)" if r.expired else f" (in {r.days_left} days)" if r.days_left else " (today)")
        for r, _t in fresh
    )
    body = (
        f"Renewals that need the board:\n\n{lines}\n\n"
        f"{settings.PORTAL_BASE_URL}{reverse('renewals')}\n\n-- \n{settings.SITE_NAME}"
    )
    with transaction.atomic():
        for r, threshold in fresh:
            try:
                with transaction.atomic():
                    RenewalReminder.objects.create(
                        kind=r.kind, object_id=r.obj.pk, deadline=r.deadline, threshold=threshold
                    )
            except IntegrityError:
                continue  # another worker sent it a moment ago
        record_event("renewals.reminders_sent", actor=None, metadata={"items": len(fresh)})
    mail_board(board_emails(), subject="Renewals coming up", body=body)
    return len(fresh)


# --- Recording ---------------------------------------------------------------------------


def _actor(actor):
    return None if actor is SYSTEM else actor


def _attach(*, existing_document, file, title, category, dated_on, note, actor, request):
    """The library document holding this file: a new version of the earlier
    document when there is one (so the older file is "superseded"), else a
    new board-only document."""
    from documents import services

    if file is None:
        return existing_document
    if existing_document is not None:
        services.add_version(existing_document, file=file, note=note, actor=actor, request=request)
        return existing_document
    return services.create_document(
        title=title[:200],
        category=category,
        visibility=Visibility.BOARD,
        file=file,
        dated_on=dated_on,
        actor=actor,
        request=request,
    )


POLICY_FIELDS = (
    "kind",
    "carrier",
    "policy_number",
    "coverage_cents",
    "aggregate_cents",
    "deductible_cents",
    "premium_cents",
    "starts_on",
    "ends_on",
    "notes",
)


def _check_policy(fields: dict) -> None:
    if fields["kind"] not in PolicyKind.values:
        raise RenewalError("Choose the kind of policy.")
    if not (fields.get("carrier") or "").strip():
        raise RenewalError("Enter the insurance company.")
    if not fields.get("coverage_cents") or fields["coverage_cents"] <= 0:
        raise RenewalError("Enter the coverage amount.")
    if fields["ends_on"] <= fields["starts_on"]:
        raise RenewalError("The policy must end after it starts.")


def record_policy(
    *, actor, request=None, renews: InsurancePolicy | None = None, file=None, **fields
) -> InsurancePolicy:
    """Record a policy term (or the next term of ``renews``)."""
    require(actor, "renewals.record")
    fields = {k: fields.get(k) for k in POLICY_FIELDS}
    fields["carrier"] = (fields["carrier"] or "").strip()[:200]
    fields["policy_number"] = (fields["policy_number"] or "").strip()[:60]
    fields["notes"] = fields["notes"] or ""
    if renews is not None:
        fields["kind"] = renews.kind
    _check_policy(fields)
    with transaction.atomic():
        if renews is not None:
            renews = InsurancePolicy.objects.select_for_update().get(pk=renews.pk)
            if InsurancePolicy.objects.filter(renews=renews).exists():
                raise RenewalError("That policy's next term is already recorded.")
            if fields["ends_on"] <= renews.ends_on:
                raise RenewalError("The new term must end after the one it renews.")
        label = PolicyKind(fields["kind"]).label
        document = _attach(
            existing_document=renews.document if renews else None,
            file=file,
            title=f"Insurance: {label} ({fields['carrier']})",
            category=Category.INSURANCE,
            dated_on=fields["starts_on"],
            note=f"Term {fields['starts_on']:%b %-d, %Y} to {fields['ends_on']:%b %-d, %Y}",
            actor=actor,
            request=request,
        )
        policy = InsurancePolicy.objects.create(
            **fields, renews=renews, document=document, recorded_by=actor
        )
        record_event(
            "renewals.policy_recorded",
            actor=_actor(actor),
            request=request,
            target=policy,
            metadata={
                "kind": policy.kind,
                "ends_on": policy.ends_on.isoformat(),
                "renews": renews.pk if renews else None,
            },
        )
    return policy


def update_policy(policy: InsurancePolicy, *, actor, request=None, file=None, **fields):
    """Correct a recorded term (the change history keeps the old values), or
    attach its document."""
    require(actor, "renewals.record")
    fields = {k: fields[k] for k in POLICY_FIELDS if k in fields}
    with transaction.atomic():
        policy = InsurancePolicy.objects.select_for_update().get(pk=policy.pk)
        merged = {k: fields.get(k, getattr(policy, k)) for k in POLICY_FIELDS}
        merged["carrier"] = (merged["carrier"] or "").strip()[:200]
        merged["policy_number"] = (merged["policy_number"] or "").strip()[:60]
        merged["notes"] = merged["notes"] or ""
        if policy.renews_id:
            merged["kind"] = policy.kind
        _check_policy(merged)
        changed = [k for k in POLICY_FIELDS if merged[k] != getattr(policy, k)]
        for k in changed:
            setattr(policy, k, merged[k])
        if file is not None:
            policy.document = _attach(
                existing_document=policy.document,
                file=file,
                title=f"Insurance: {policy.get_kind_display()} ({policy.carrier})",
                category=Category.INSURANCE,
                dated_on=policy.starts_on,
                note="",
                actor=actor,
                request=request,
            )
            changed.append("document")
        if not changed:
            return policy
        policy.save()
        record_event(
            "renewals.policy_changed",
            actor=_actor(actor),
            request=request,
            target=policy,
            metadata={"fields": ",".join(changed)[:200]},
        )
    return policy


def record_vendor_certificate(
    vendor, *, expires_on, insurer="", coverage_cents=None, file=None, actor, request=None
) -> VendorCertificate:
    """A new certificate of insurance; it becomes the vendor's expiry."""
    from operations import services as operations

    require(actor, "renewals.record")
    if expires_on is None:
        raise RenewalError("Enter the date the certificate expires.")
    with transaction.atomic():
        earlier = (
            VendorCertificate.objects.filter(vendor=vendor, document__isnull=False)
            .order_by("-recorded_at", "-id")
            .first()
        )
        document = _attach(
            existing_document=earlier.document if earlier else None,
            file=file,
            title=f"Insurance certificate: {vendor.name}",
            category=Category.INSURANCE,
            dated_on=None,
            note=f"Expires {expires_on:%b %-d, %Y}",
            actor=actor,
            request=request,
        )
        certificate = VendorCertificate.objects.create(
            vendor=vendor,
            insurer=(insurer or "").strip()[:200],
            coverage_cents=coverage_cents,
            expires_on=expires_on,
            document=document,
            recorded_by=actor,
        )
        operations.save_vendor(
            vendor=vendor,
            name=vendor.name,
            insurance_expires_on=expires_on,
            actor=actor,
            request=request,
        )
        record_event(
            "renewals.certificate_recorded",
            actor=_actor(actor),
            request=request,
            target=certificate,
            metadata={"vendor_id": vendor.pk, "expires_on": expires_on.isoformat()},
        )
    return certificate


def save_contract_terms(
    contract,
    *,
    renewal: str,
    notice_days: int = 0,
    renews_for_months: int | None = None,
    file=None,
    actor,
    request=None,
) -> ContractTerms:
    require(actor, "renewals.record")
    if renewal not in RenewalType.values:
        raise RenewalError("Choose how the contract renews.")
    if contract.state not in ("draft", "active"):
        raise RenewalError("This contract is no longer in use.")
    notice_days = int(notice_days or 0)
    if notice_days > 730:
        raise RenewalError("Enter the notice period in days (at most 730).")
    if renewal == RenewalType.MANUAL:
        renews_for_months = None
    elif renews_for_months is not None and not 1 <= renews_for_months <= 120:
        raise RenewalError("Each new term is 1 to 120 months.")
    with transaction.atomic():
        terms = ContractTerms.objects.select_for_update().filter(contract=contract).first()
        document = _attach(
            existing_document=terms.document if terms else None,
            file=file,
            title=f"Contract: {contract.vendor.name}, {contract.title}",
            category=Category.CONTRACTS,
            dated_on=contract.starts_on,
            note="",
            actor=actor,
            request=request,
        )
        if terms is None:
            terms = ContractTerms(contract=contract)
        terms.renewal = renewal
        terms.notice_days = notice_days
        terms.renews_for_months = renews_for_months
        terms.document = document
        terms.updated_by = actor
        terms.updated_at = timezone.now()
        terms.save()
        record_event(
            "renewals.contract_terms_saved",
            actor=_actor(actor),
            request=request,
            target=contract,
            metadata={
                "contract_id": contract.pk,
                "renewal": renewal,
                "notice_days": notice_days,
            },
        )
    return terms


def current_contract_term(contract, today=None) -> Renewal | None:
    today = today or timezone.localdate()
    return next((r for r in _contracts(today) if r.obj.pk == contract.pk), None)


def decide_contract(contract, *, decision: str, note: str, actor, request=None):
    """The board's decision on the contract's current term (never the
    manager): renew, cancel (notice sent) or let it end."""
    require(actor, "renewals.decide")
    if decision not in RenewalChoice.values:
        raise RenewalError("Choose renew, cancel or let it end.")
    note = " ".join((note or "").split())[:300]
    if not note:
        raise RenewalError("Add a note: the minutes or resolution, or when notice was sent.")
    term = current_contract_term(contract)
    if term is None:
        raise RenewalError("This contract has no end date to decide on.")
    if term.decision is not None:
        raise RenewalError("This term has already been decided.")
    try:
        with transaction.atomic():
            row = RenewalDecision.objects.create(
                contract=contract,
                term_ends_on=term.term_ends_on,
                decision=decision,
                note=note,
                decided_by=actor,
            )
            record_event(
                "renewals.contract_decided",
                actor=_actor(actor),
                request=request,
                target=contract,
                metadata={
                    "contract_id": contract.pk,
                    "term_ends_on": term.term_ends_on.isoformat(),
                    "decision": decision,
                },
            )
    except IntegrityError as exc:
        raise RenewalError("This term has already been decided.") from exc
    return row
