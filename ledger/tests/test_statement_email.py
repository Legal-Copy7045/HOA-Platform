"""The monthly statement email (D42): who gets it, once per account and
month, what it says, the attached PDF, the autopay hook and the audit."""

import datetime
import sys
import types

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.actions import ACTIONS
from audit.models import AuditEvent
from community import services as community
from community.models import Account, ContactProfile, Person
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import services, statement_email
from ledger.models import StatementEmail

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()
YEAR, MONTH = TODAY.year, TODAY.month
DUES = 4500


@pytest.fixture(autouse=True)
def run_queued_task(monkeypatch):
    """Run the queued statement task at once instead of through the worker."""
    from ledger import tasks

    monkeypatch.setattr(
        tasks.send_statement_emails,
        "defer",
        lambda **kw: statement_email.send_monthly_statements(**kw),
    )


@pytest.fixture
def w():
    board = make_member(board=True)
    community.set_rule(key="dues.monthly_cents", value=str(DUES), citation="Decl §6", actor=SYSTEM)
    lot, other_lot = LotFactory(), LotFactory()
    owner = make_member(lots=[lot])
    return {
        "board": board,
        "lot": lot,
        "other_lot": other_lot,
        "owner": owner,
        "neighbor": make_member(lots=[other_lot]),
        "account": _account(lot),
    }


def _account(lot):
    with system_access("t"):
        return Account.objects.get(lot=lot, closed_on__isnull=True)


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


def _contact(w, kind):
    user = make_member()
    community.add_account_contact(w["account"], person=_person(user), kind=kind, actor=SYSTEM)
    return user


def _set_pref(user, on: bool):
    with system_access("t"):
        profile, _ = ContactProfile.objects.get_or_create(person=_person(user))
        profile.statement_emails = on
        profile.save()


def _post(capture):
    with capture(execute=True):
        services.post_monthly_assessments(year=YEAR, month=MONTH, actor=SYSTEM)


def _to(mail, user):
    return [m for m in mail if m.to == [user.email]]


def test_preference_is_on_by_default_for_new_and_existing_people(w):
    with system_access("t"):
        profile = ContactProfile.objects.create(person=_person(w["owner"]))
    assert profile.statement_emails is True
    # Existing rows were set on by the migration's database default.
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'community_contactprofile' AND column_name = 'statement_emails'"
        )
        assert cur.fetchone()[0] == "true"


def test_owner_gets_the_statement_with_pdf_amount_and_links(
    w, mailoutbox, django_capture_on_commit_callbacks
):
    _post(django_capture_on_commit_callbacks)
    [msg] = _to(mailoutbox, w["owner"])
    month = datetime.date(YEAR, MONTH, 1).strftime("%B")
    assert msg.subject.endswith(f"Your {month} assessment statement")
    assert w["lot"].street_address in msg.body
    assert "Amount due now: $45.00" in msg.body
    assert "Please pay $45.00 by" in msg.body
    assert reverse("my_account") in msg.body and reverse("my_profile") in msg.body
    assert "To stop these emails" in msg.body
    assert "token" not in msg.body.lower()
    [(name, content, mime)] = msg.attachments
    assert name == f"statement-{YEAR:04d}-{MONTH:02d}.pdf" and mime == "application/pdf"
    assert content.startswith(b"%PDF")
    # The file name carries no names, lot numbers or addresses.
    assert w["owner"].email.split("@")[0] not in name and str(w["lot"].number) not in name


def test_attachment_is_the_current_accounts_statement(w, mailoutbox, monkeypatch):
    seen = []

    def fake_pdf(lot, **kw):
        seen.append((lot.pk, kw))
        return b"%PDF-fake"

    monkeypatch.setattr(services, "statement_pdf", fake_pdf)
    statement_email.send_for_account(w["account"], year=YEAR, month=MONTH)
    # services.statement_pdf is the current owners' account only (D33-acctfix-1).
    assert seen == [(w["lot"].pk, {})]
    assert mailoutbox[0].attachments[0][1] == b"%PDF-fake"


def test_only_people_who_see_the_balance_get_it(w, mailoutbox, django_capture_on_commit_callbacks):
    authorized = _contact(w, "authorized")
    view_only = _contact(w, "view_only")
    tenant = _contact(w, "tenant")
    _post(django_capture_on_commit_callbacks)
    assert _to(mailoutbox, authorized) and _to(mailoutbox, view_only)
    assert not _to(mailoutbox, tenant)
    # Another home's owner gets only their own home's statement.
    [theirs] = _to(mailoutbox, w["neighbor"])
    assert w["other_lot"].street_address in theirs.body
    assert w["lot"].street_address not in theirs.body
    # The view-only contact is not offered a pay link (they cannot pay).
    assert "Pay online" not in _to(mailoutbox, view_only)[0].body


def test_former_owners_never_get_it(w, mailoutbox, django_capture_on_commit_callbacks):
    buyer = make_member()
    community.transfer_lot(
        w["lot"],
        new_owners=[_person(buyer)],
        transfer_date=TODAY - datetime.timedelta(days=3),
        actor=w["board"],
    )
    _post(django_capture_on_commit_callbacks)
    assert not _to(mailoutbox, w["owner"])
    assert _to(mailoutbox, buyer)


def test_unticking_stops_it(w, mailoutbox, django_capture_on_commit_callbacks):
    _set_pref(w["owner"], False)
    _post(django_capture_on_commit_callbacks)
    assert not _to(mailoutbox, w["owner"])
    assert _to(mailoutbox, w["neighbor"])


def test_unticking_on_my_profile(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("my_profile")).content.decode()
    assert "Email me a statement when the monthly assessment is charged" in page
    assert "Untick to stop these emails." in page
    client.post(reverse("my_profile"), {"phone": "", "mailing_address": ""})
    with system_access("t"):
        assert ContactProfile.objects.get(person=_person(w["owner"])).statement_emails is False
    client.post(reverse("my_profile"), {"statement_emails": "on"})
    with system_access("t"):
        assert ContactProfile.objects.get(person=_person(w["owner"])).statement_emails is True


def test_once_per_account_per_month(w, mailoutbox, django_capture_on_commit_callbacks):
    _post(django_capture_on_commit_callbacks)
    first = len(mailoutbox)
    assert first >= 2
    _post(django_capture_on_commit_callbacks)  # the board posts again
    statement_email.send_monthly_statements(year=YEAR, month=MONTH)  # the task re-runs
    assert len(mailoutbox) == first
    with system_access("t"):
        assert StatementEmail.objects.filter(account=w["account"]).count() == 1


def test_prepaid_credit_wording(w, mailoutbox):
    services.record_payment(
        lot=w["lot"],
        amount_cents=10000,
        received_on=TODAY - datetime.timedelta(days=40),
        reference="pre-1",
        actor=w["board"],
    )
    services.post_monthly_assessments(year=YEAR, month=MONTH, actor=SYSTEM)
    statement_email.send_for_account(w["account"], year=YEAR, month=MONTH)
    [msg] = _to(mailoutbox, w["owner"])
    assert (
        "Nothing to pay: this month is covered by your prepaid credit "
        "(credit left after this month: $55.00)." in msg.body
    )
    assert "Amount due now: $0.00" in msg.body


def test_nothing_to_pay_wording(w, mailoutbox):
    # No charge for the month on this account and nothing owed.
    statement_email.send_for_account(w["account"], year=YEAR, month=MONTH)
    [msg] = _to(mailoutbox, w["owner"])
    assert "Nothing to pay." in msg.body and "prepaid" not in msg.body


def test_autopay_hook_is_used_when_present(w, mailoutbox, monkeypatch):
    module = types.ModuleType("payments.autopay")
    module.describe_for_statement = lambda account: f"Autopay will pay this ({account.pk})."
    monkeypatch.setitem(sys.modules, "payments.autopay", module)
    services.post_monthly_assessments(year=YEAR, month=MONTH, actor=SYSTEM)
    statement_email.send_for_account(w["account"], year=YEAR, month=MONTH)
    [msg] = _to(mailoutbox, w["owner"])
    assert f"Autopay will pay this ({w['account'].pk})." in msg.body
    assert "Please pay" not in msg.body


def test_autopay_stub_returns_none_without_the_module(w, monkeypatch):
    monkeypatch.setitem(sys.modules, "payments.autopay", None)  # import fails
    assert statement_email.autopay_line(w["account"]) is None


def test_audit_one_event_per_account_with_allowlisted_counts(w, django_capture_on_commit_callbacks):
    _contact(w, "authorized")
    _post(django_capture_on_commit_callbacks)
    events = list(AuditEvent.objects.filter(action="ledger.statement_emailed"))
    mine = [e for e in events if e.metadata.get("account_id") == w["account"].pk]
    assert len(mine) == 1
    assert mine[0].metadata == {
        "account_id": w["account"].pk,
        "period": f"{YEAR:04d}-{MONTH:02d}",
        "recipients": 2,
    }
    assert set(mine[0].metadata) <= ACTIONS["ledger.statement_emailed"].metadata_fields
    assert "@" not in str(mine[0].metadata)


def test_one_bad_address_never_stops_the_rest(w, mailoutbox, monkeypatch):
    from django.core.mail import EmailMessage

    real = EmailMessage.send
    calls = []

    def flaky(self, *a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("smtp down")
        return real(self, *a, **kw)

    authorized = _contact(w, "authorized")
    monkeypatch.setattr(EmailMessage, "send", flaky)
    sent = statement_email.send_for_account(w["account"], year=YEAR, month=MONTH)
    assert sent == 1 and len(mailoutbox) == 1
    assert mailoutbox[0].to[0] in {w["owner"].email, authorized.email}
