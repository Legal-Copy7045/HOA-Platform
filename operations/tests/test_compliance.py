"""Milestone 4: compliance calendar, and budget/dues notices to owners."""

import datetime

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from community import services as community
from community.models import ContactProfile, Person, Role
from community.tests.factories import LotFactory, make_member
from core import my_items
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import services as ledger
from operations import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def board():
    return make_member(board=True)


def _item(board, due, recurrence="annual", remind=30):
    return services.save_compliance_item(
        title="Insurance renewal",
        category="insurance",
        due_on=due,
        recurrence=recurrence,
        remind_days=remind,
        actor=board,
    )


def test_recurring_item_moves_to_its_next_due_date(board):
    item = _item(board, datetime.date(2027, 1, 31), recurrence="monthly")
    item = services.complete_compliance_item(item, actor=board)
    assert item.due_on == datetime.date(2027, 2, 28) and item.active
    once = _item(board, datetime.date(2027, 3, 1), recurrence="once")
    once = services.complete_compliance_item(once, actor=board)
    assert not once.active


def test_reminders_go_once_per_due_date(board):
    item = _item(board, datetime.date(2027, 5, 1), remind=30)
    assert services.send_compliance_reminders(on=datetime.date(2027, 3, 1)) == 0
    assert services.send_compliance_reminders(on=datetime.date(2027, 4, 5)) == 1
    assert services.send_compliance_reminders(on=datetime.date(2027, 4, 6)) == 0
    assert mail.outbox and "Insurance renewal" in mail.outbox[-1].body
    services.complete_compliance_item(item, actor=board)
    item.refresh_from_db()
    assert item.reminded_for is None  # the next year's reminder will go


def test_due_items_show_in_my_items(board):
    import django.utils.timezone as tz

    _item(board, tz.localdate() + datetime.timedelta(days=3))
    assert any(i.area == "Recurring deadlines" for i in my_items.items_for(board))


def test_starter_items_and_permissions(client, board):
    assert services.add_starter_items(actor=board) >= 5
    assert services.add_starter_items(actor=board) == 0
    owner = make_member(lots=[LotFactory()])
    with pytest.raises(PermissionDenied):
        services.add_starter_items(actor=owner)
    client.force_login(owner)
    assert client.get(reverse("compliance")).status_code == 403
    client.force_login(make_member(roles=[Role.MANAGER]))
    assert b"Form 1120-H" in client.get(reverse("compliance"), follow=True).content


# --- Recurring deadlines tab of Board tasks (streamlining) -----------------------------


def _deadlines_url():
    return reverse("tasks") + "?tab=deadlines"


def test_compliance_redirects_to_the_recurring_deadlines_tab(client, board):
    client.force_login(board)
    response = client.get(reverse("compliance"))
    assert response.status_code == 302 and response["Location"] == _deadlines_url()
    page = client.get(_deadlines_url()).content.decode()
    assert "<h1>Board tasks</h1>" in page and "Recurring deadlines" in page
    assert 'aria-current="page">Recurring deadlines' in page


def test_deadline_forms_post_to_the_tab_and_stay_audited(client, board):
    from audit.models import AuditEvent

    client.force_login(board)
    response = client.post(
        _deadlines_url(),
        {"title": "Annual report", "due_on": "2027-04-15", "recurrence": "annual"},
    )
    assert response.status_code == 302 and response["Location"] == _deadlines_url()
    from operations.models import BoardTask, ComplianceItem

    item = ComplianceItem.objects.get(title="Annual report")
    assert not BoardTask.objects.filter(title="Annual report").exists()
    before = AuditEvent.objects.count()
    client.post(_deadlines_url(), {"action": "done", "item": item.pk, "due_on": "2027-04-15"})
    item.refresh_from_db()
    assert item.due_on == datetime.date(2028, 4, 15)
    assert AuditEvent.objects.count() > before


def test_deadlines_tab_needs_permission_and_the_feature(client, board):
    from core import features

    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    assert client.get(_deadlines_url()).status_code == 403
    assert client.post(_deadlines_url(), {"title": "x", "due_on": "2027-01-01"}).status_code == 403
    features.set_feature("compliance", False, actor=board)
    client.force_login(board)
    page = client.get(_deadlines_url()).content.decode()
    assert "Recurring deadlines" not in page  # the tasks tab, without the deadlines tab
    assert client.post(_deadlines_url(), {"title": "x", "due_on": "2027-01-01"}).status_code == 404
    assert client.get(reverse("compliance")).status_code == 404


def test_due_deadlines_link_to_the_tab_in_my_items(board):
    from django.utils import timezone

    _item(board, timezone.localdate() + datetime.timedelta(days=3))
    found = [i for i in my_items.items_for(board) if i.area == "Recurring deadlines"]
    assert found and found[0].url == _deadlines_url()


# --- Budget notices ----------------------------------------------------------------------


@pytest.fixture
def budget(board):
    from operations.models import ExpenseCategory

    b = ledger.create_budget(fiscal_year=2027, actor=board)
    ledger.add_line(
        b,
        kind="income",
        name="Dues",
        income_source="assessments",
        annual_cents=2_376_000,
        actor=board,
    )
    ledger.add_line(
        b,
        kind="expense",
        name="Landscaping",
        annual_cents=1_200_000,
        category=ExpenseCategory.objects.create(code="L1", name="Landscaping"),
        actor=board,
    )
    return b


def _consenting_owner():
    owner = make_member(lots=[LotFactory()])
    with system_access("t"):
        person = Person.objects.get(user=owner)
        ContactProfile.objects.update_or_create(
            person=person, defaults={"electronic_notice_consent": True}
        )
    return owner


def test_proposed_then_adopted_notices(client, board, budget):
    consenting = _consenting_owner()
    paper = make_member(lots=[LotFactory()])
    result = ledger.send_budget_notice(budget, stage="proposed", actor=board)
    assert result["emailed"] == 1 and len(result["paper"]) == 1
    assert mail.outbox[-1].to == [consenting.email]
    client.force_login(paper)
    page = client.get(reverse("owner_budget")).content
    assert b"Landscaping" in page and b"Proposed" in page
    with pytest.raises(ledger.LedgerError):
        ledger.send_budget_notice(budget, stage="adopted", actor=board)
    community.set_rule(
        key="dues.monthly_cents", value="4500", citation="FY2027 budget", actor=SYSTEM
    )
    ledger.adopt_budget(budget, actor=board)
    budget.refresh_from_db()
    ledger.send_budget_notice(budget, stage="adopted", actor=board)
    assert "$45.00" in mail.outbox[-1].body


def test_only_the_board_sends_budget_notices(budget):
    with pytest.raises(PermissionDenied):
        ledger.send_budget_notice(budget, stage="proposed", actor=make_member(roles=[Role.MANAGER]))


def test_owners_see_no_draft_budget_before_it_is_sent(client, budget):
    client.force_login(make_member(lots=[LotFactory()]))
    assert b"Landscaping" not in client.get(reverse("owner_budget")).content


# --- Regression review fix BUG-015 ----------------------------------------------------------


def test_month_end_items_stay_at_month_end(board):
    item = _item(board, datetime.date(2027, 1, 31), recurrence="monthly")
    services.complete_compliance_item(item, actor=board)
    item.refresh_from_db()
    assert item.due_on == datetime.date(2027, 2, 28)
    services.complete_compliance_item(item, actor=board)
    item.refresh_from_db()
    assert item.due_on == datetime.date(2027, 3, 31)


def test_double_click_done_does_not_skip_a_period(board):
    item = _item(board, datetime.date(2027, 1, 15), recurrence="annual")
    seen = item.due_on
    services.complete_compliance_item(item, actor=board, seen_due_on=seen)
    with pytest.raises(services.OperationsError):
        services.complete_compliance_item(item, actor=board, seen_due_on=seen)
    item.refresh_from_db()
    assert item.due_on == datetime.date(2028, 1, 15)


@pytest.mark.parametrize(
    "bad", [{"category": "nope"}, {"recurrence": "weekly"}, {"remind_days": -3}]
)
def test_bad_values_are_refused(board, bad):
    values = {
        "title": "Insurance",
        "category": "insurance",
        "due_on": datetime.date(2027, 5, 1),
        "recurrence": "annual",
        "remind_days": 30,
        **bad,
    }
    with pytest.raises(services.OperationsError):
        services.save_compliance_item(actor=board, **values)
