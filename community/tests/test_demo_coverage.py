"""The oak_creek demo has records in every state a person can see or act on
(community/demo/states), a login for every test persona, work on each
role's My items, and a tester's guide (docs/demo-test-guide.md) that names
only people and homes the seed creates.

A new area adds a line to STATES (or CHECKS) below. The demo is seeded once
for the module and rolled back at the end."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings

from community.demo.states import PERSONAS
from core.scoping import system_access

pytestmark = pytest.mark.django_db

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "demo-test-guide.md"


@pytest.fixture(scope="module")
def seeded(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        atomic = transaction.atomic()
        atomic.__enter__()
        try:
            with override_settings(SEED_DEMO_ALLOWED=True):
                call_command("seed_demo", stdout=io.StringIO())
            yield
        finally:
            transaction.set_rollback(True)
            atomic.__exit__(None, None, None)


# (model, field, values that must each appear at least once)
STATES = [
    (
        "arc.ArcRequest",
        "state",
        {
            "draft",
            "neighbors",
            "submitted",
            "in_review",
            "approved",
            "approved_with_conditions",
            "denied",
            "appeal",
            "appeal_decided",
            "completed",
            "withdrawn",
        },
    ),
    ("arc.SignoffRequest", "state", {"pending", "approved"}),
    (
        "compliance.Violation",
        "state",
        {"open", "noticed", "responded", "hearing", "cured", "upheld", "dismissed", "closed"},
    ),
    (
        "operations.OwnerRequest",
        "state",
        {"submitted", "in_progress", "waiting", "resolved", "closed", "withdrawn"},
    ),
    (
        "operations.WorkOrder",
        "state",
        {"open", "assigned", "in_progress", "completed", "closed", "cancelled"},
    ),
    ("operations.Contract", "state", {"draft", "active", "discarded"}),
    (
        "operations.Invoice",
        "state",
        {"draft", "pending_approval", "approved", "paid", "rejected", "void"},
    ),
    ("operations.BoardTask", "status", {"open", "done", "dropped"}),
    ("governance.Meeting", "status", {"scheduled", "held", "cancelled"}),
    ("governance.Meeting", "kind", {"board", "annual", "special", "executive"}),
    ("governance.Resolution", "kind", {"resolution", "policy"}),
    ("governance.ConsentRequest", "state", {"open", "adopted", "failed", "withdrawn"}),
    ("governance.Vote", "status", {"draft", "open", "closed"}),
    ("governance.Election", "state", {"nominations", "voting", "closed", "certified"}),
    ("governance.Survey", "status", {"draft", "open", "closed"}),
    (
        "governance.RecordsRequest",
        "state",
        {"submitted", "in_progress", "fulfilled", "denied", "withdrawn"},
    ),
    ("ledger.ResaleRequest", "state", {"submitted", "in_progress", "issued", "cancelled"}),
    (
        "ledger.CollectionCase",
        "state",
        {
            "open",
            "reminder_sent",
            "second_reminder",
            "final_notice",
            "payment_plan",
            "referred",
            "closed",
        },
    ),
    (
        "ledger.LedgerEntry",
        "kind",
        {"assessment", "late_fee", "other_charge", "resale_fee", "payment", "credit", "reversal"},
    ),
    ("ledger.Budget", "status", {"draft", "adopted"}),
    ("ledger.DisbursementBatch", "kind", {"refund", "surplus"}),
    ("ledger.DisbursementDecision", "decision", {"approve", "decline"}),
    ("ledger.DisbursementCheckEvent", "kind", {"cleared", "voided"}),
    ("community.AccountContact", "kind", {"authorized", "view_only", "tenant"}),
    ("community.AccountChangeRequest", "state", {"pending", "approved", "declined", "withdrawn"}),
    ("community.AccountChangeRequest", "action", {"add", "add_owner", "remove_owner"}),
    ("community.SysadminNomination", "outcome", {"", "approved", "declined"}),
    ("community.BoardSeatNomination", "outcome", {"", "declined"}),
    ("community.Reservation", "state", {"pending", "confirmed", "declined", "cancelled"}),
    ("community.RoleAssignment", "role", {"arc_reviewer", "auditor", "sysadmin", "manager"}),
    ("comms.MailingEvent", "kind", {"mailed", "discarded"}),
]


def _values(label, field):
    model = apps.get_model(label)
    with system_access("demo coverage"):
        return set(model._base_manager.values_list(field, flat=True))


@pytest.mark.parametrize(
    ("label", "field", "required"), STATES, ids=[f"{s[0]}.{s[1]}" for s in STATES]
)
def test_every_state_is_seeded(seeded, label, field, required):
    missing = required - _values(label, field)
    assert not missing, f"{label}.{field} has no demo record in {sorted(missing)}"


def _deemed():
    from arc.models import Decision

    return Decision.objects.filter(deemed=True).exists()


def _invoice_waiting_for_second_approval():
    from operations.models import Invoice, InvoiceApproval

    for inv in Invoice.objects.filter(state="pending_approval"):
        if InvoiceApproval.objects.filter(invoice=inv, decision="approve").count() == 1:
            return True
    return False


def _invitations():
    from accounts.models import Invitation

    return {i.status for i in Invitation.objects.all()} >= {"pending", "expired", "revoked"}


def _mfa_resets():
    from accounts.models import MfaReset

    statuses = {r.status for r in MfaReset.objects.unscoped("t")}
    return statuses >= {"awaiting_approval", "awaiting_code", "cancelled"}


def _refund_steps():
    from ledger import disbursements
    from ledger.models import DisbursementBatch

    return {disbursements.batch_state(b).status for b in DisbursementBatch.objects.all()}


def _usps_statuses():
    from comms.models import MailPieceScan

    return {s.opcode for s in MailPieceScan.objects.all()} >= {"336", "918", "517"}


def _rsvp():
    from comms import rsvp
    from comms.models import Event
    from community.demo.states.comms import FULL_EVENT, OPEN_EVENT

    full = rsvp.counts(Event.objects.get(title=FULL_EVENT))
    free = rsvp.counts(Event.objects.get(title=OPEN_EVENT))
    return full["places_left"] == 0 and free["places_left"] > 0


def _overdue_records():
    import datetime

    from django.utils import timezone

    from governance.models import RecordsRequest

    today = timezone.localdate()
    open_ = RecordsRequest.objects.unscoped("t").filter(state__in=("submitted", "in_progress"))
    return (
        open_.filter(due_on__lt=today).exists()
        and open_.filter(due_on__gte=today, due_on__lte=today + datetime.timedelta(days=5)).exists()
    )


def _versions():
    from django.db.models import Count

    from documents.models import Document

    return Document.objects.annotate(n=Count("versions")).filter(n__gte=2).exists() and (
        Document.objects.filter(archived=True).exists()
    )


def _vote_extended():
    from community.demo.states.governance import EXTENDED_VOTE
    from governance.models import Vote

    return Vote.objects.get(title=EXTENDED_VOTE).extended_at is not None


CHECKS = {
    "ARC deemed approval": _deemed,
    "invoice waiting for its second approval": _invoice_waiting_for_second_approval,
    "invitations pending, expired and revoked": _invitations,
    "sign-in resets waiting and cancelled": _mfa_resets,
    "USPS scans processed, sorted and delivered": _usps_statuses,
    "an event that is full and one with room": _rsvp,
    "records requests overdue and due soon": _overdue_records,
    "documents with versions and an archived one": _versions,
    "an extended vote": _vote_extended,
}


@pytest.mark.parametrize("name", list(CHECKS))
def test_special_cases_are_seeded(seeded, name):
    with system_access("demo coverage"):
        assert CHECKS[name](), name


def test_refunds_at_every_step(seeded):
    with system_access("demo coverage"):
        states = _refund_steps()
    assert states >= {"awaiting", "approved", "declined"}, states


# --- Personas ---------------------------------------------------------------------------


def test_every_persona_has_a_login_on_their_own_email_without_any_way_in(seeded):
    from accounts.auth import has_strong_authenticator
    from accounts.services import setup_again_refusal
    from community.models import Person

    with system_access("demo coverage"):
        users = _persona_users()
        assert set(users) == {p.key for p in PERSONAS}
        for key, user in users.items():
            person = Person.objects.get(user=user)
            assert user.email == person.email, key
            assert not user.has_usable_password(), key
            assert not has_strong_authenticator(user), key
            if key not in ("joint_b", "owner_late", "owner_arc"):  # resets on record
                assert setup_again_refusal(person) is None, key


def _persona_users() -> dict:
    """Each persona's login, found the way the guide describes them."""
    from accounts.models import User
    from community.models import AccountContact, OfficeHolding, Ownership, RoleAssignment

    def owner(lot, index=0):
        return list(
            Ownership.objects.active()
            .filter(lot__number=lot)
            .order_by("start_date", "pk")
            .values_list("person__user", flat=True)
        )[index]

    def role(name, lot=None):
        q = RoleAssignment.objects.active().filter(role=name)
        ids = list(q.values_list("person__user", "person_id"))
        if lot is None:
            return ids[0][0]
        owners = set(
            Ownership.objects.active().filter(lot__number=lot).values_list("person_id", flat=True)
        )
        return next(u for u, p in ids if p in owners)

    def office(name):
        return OfficeHolding.objects.active().get(office=name).person.user_id

    def contact(kind):
        return AccountContact.objects.active().get(kind=kind).person.user_id

    seller = Ownership.objects.filter(lot__number=31, end_date__isnull=False).first()
    ids = {
        "president": office("president"),
        "vice_president": office("vice_president"),
        "treasurer": office("treasurer"),
        "secretary": office("secretary"),
        "director": owner(29),
        "manager": role("manager"),
        "arc_reviewer": role("arc_reviewer", 14),
        "arc_reviewer_2": role("arc_reviewer", 38),
        "auditor": role("auditor"),
        "sysadmin": role("sysadmin"),
        "owner": owner(3),
        "owner_b": owner(12),
        "joint_a": owner(7, 0),
        "joint_b": owner(7, 1),
        "owner_arc": owner(15),
        "owner_late": owner(27),
        "authorized": contact("authorized"),
        "view_only": AccountContact.objects.active()
        .filter(kind="view_only", account__lot__number=3)
        .get()
        .person.user_id,
        "tenant": contact("tenant"),
        "former_owner": seller.person.user_id,
    }
    return {k: User.objects.get(pk=v) for k, v in ids.items()}


# Roles that have work waiting on them in the demo (My items is not empty).
HAS_WORK = [
    "president",
    "vice_president",
    "treasurer",
    "secretary",
    "director",
    "manager",
    "arc_reviewer",
    "arc_reviewer_2",
    "owner",
    "owner_b",
    "joint_a",
    "owner_late",
    "authorized",
]


def test_each_role_with_work_has_my_items(seeded):
    from core.my_items import items_for

    with system_access("demo coverage"):
        users = _persona_users()
    empty = [key for key in HAS_WORK if not items_for(users[key])]
    assert not empty, f"My items is empty for {empty}"


def test_own_matters_are_refused_and_the_recused_reviewer_stays_out(seeded):
    from arc.models import ArcRequest
    from arc.services import is_recused
    from community.demo.states import arc as arc_demo
    from community.demo.states import current_account
    from community.models import Lot
    from ledger import disbursements
    from ledger.models import DisbursementBatch

    with system_access("demo coverage"):
        users = _persona_users()
        porch = ArcRequest.objects.get(title=arc_demo.RECUSED_REQUEST[2])
        own_refund = DisbursementBatch.objects.get(
            kind="refund",
            lines__account=current_account(Lot.objects.get(number=5)),
            decisions__isnull=True,
        )
    assert is_recused(users["arc_reviewer"], porch)
    assert not is_recused(users["arc_reviewer_2"], porch)
    with pytest.raises(Exception, match=r"own|Own"):
        disbursements.approve(own_refund, actor=users["vice_president"])


# --- The tester's guide -----------------------------------------------------------------


def test_the_guide_names_only_seeded_people_and_homes(seeded):
    from accounts.models import User
    from community.models import Lot, Person

    text = GUIDE.read_text()
    emails = set(re.findall(r"[\w.+-]+@example\.(?:com|org|net)", text))
    addresses = set(re.findall(r"\b\d{3} (?:Fictional Meadow Lane|Imaginary Orchard Way)\b", text))
    assert emails and addresses
    with system_access("demo coverage"):
        known_emails = set(User.objects.values_list("email", flat=True)) | set(
            Person.objects.values_list("email", flat=True)
        )
        known_addresses = set(Lot.objects.values_list("street_address", flat=True))
        users = _persona_users()
    assert emails <= known_emails, sorted(emails - known_emails)
    assert addresses <= known_addresses, sorted(addresses - known_addresses)
    for key, user in users.items():
        assert user.email in text, f"the guide does not name the {key} persona ({user.email})"
