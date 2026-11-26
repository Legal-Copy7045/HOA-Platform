"""Automatic monthly posting on Owner accounts, and its safety net (D48).

The card shows the status instead of the month picker while ``dues.auto_post``
is on; a missed month shows a warning with the button, a My items entry, a
failing health check and one email to the board a month."""

import datetime

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import health, my_items
from core.policy import SYSTEM
from ledger import autopost
from ledger import services as ledger
from ledger.models import AssessmentPostingCheck
from ledger.tasks import monthly_assessments

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    board = make_member(board=True, lots=[LotFactory()])
    community.set_rule(
        key="dues.monthly_cents", value="7500", citation="Declaration §7.2 (demo)", actor=board
    )
    return {
        "board": board,
        "board2": make_member(board=True),
        "manager": make_member(roles=[Role.MANAGER]),
        "owner": make_member(lots=[LotFactory()]),
    }


def _auto(w, on=True):
    community.set_rule(
        key="dues.auto_post",
        value="true" if on else "false",
        citation="Board resolution 2026-04 (demo)",
        actor=w["board"],
    )


def _card(client, user):
    client.force_login(user)
    page = client.get(reverse("ledger_home")).content.decode()
    return page.split('id="post-monthly"')[1].split("</section>")[0]


def _this_month():
    return timezone.localdate().replace(day=1)


def _post_this_month():
    m = _this_month()
    ledger.post_monthly_assessments(year=m.year, month=m.month, actor=SYSTEM)


def _missed_items(user):
    return [i for i in my_items.items_for(user) if "wasn't posted automatically" in i.title]


def _check():
    return {r.key: r for r in health.run_checks()}.get("assessment_posting")


# --- The card ------------------------------------------------------------------------------


def test_off_shows_the_picker_button_and_the_hint(client, w):
    card = _card(client, w["board"])
    assert '<select id="dperiod"' in card and "Post to every home" in card
    assert "Or switch on automatic posting" in card
    assert _check() is None  # no health check while it is off


def test_on_shows_status_instead_of_the_button(client, w):
    _auto(w)
    _post_this_month()
    card = _card(client, w["board"])
    assert '<select id="dperiod"' not in card and "Post to every home" not in card
    assert "Posted automatically on the 1st of each month" in card
    assert "Community settings → change" in card
    month = _this_month()
    nxt = (month + datetime.timedelta(days=32)).replace(day=1)
    assert f"{month:%B %Y} was posted on {timezone.localdate():%b %-d, %Y} to 2 homes." in card
    assert f"Next: {nxt:%B %Y} on {nxt:%b %-d, %Y}." in card
    assert 'data-testid="auto-post-missed"' not in card


def test_manager_sees_status_without_the_settings_link(client, w):
    _auto(w)
    _post_this_month()
    card = _card(client, w["manager"])
    assert "Posted automatically" in card and "Community settings → change" not in card


def test_on_but_missed_shows_the_warning_and_a_button_for_this_month(client, w):
    _auto(w)
    card = _card(client, w["board"])
    month = _this_month()
    assert f"{month:%B} hasn't been posted yet" in card
    assert f'name="period" value="{month:%Y-%m}"' in card
    assert f"Post {month:%B %Y} to every home" in card
    client.post(reverse("ledger_home"), {"period": f"{month:%Y-%m}"})
    assert 'data-testid="auto-post-missed"' not in _card(client, w["board"])


# --- The safety net ------------------------------------------------------------------------


def test_missed_month_emails_once_and_shows_everywhere(w, django_capture_on_commit_callbacks):
    _auto(w)
    with django_capture_on_commit_callbacks(execute=True):
        assert autopost.check_and_alert() is True
    assert len(mail.outbox) == 2  # both board members, one email each
    message = mail.outbox[0]
    assert "wasn't posted automatically" in message.subject
    assert reverse("ledger_home") + "#post-monthly" in message.body
    assert {m.to[0] for m in mail.outbox} == {w["board"].email, w["board2"].email}
    assert _missed_items(w["board"]) and _missed_items(w["manager"])
    assert not _missed_items(w["owner"])
    assert _missed_items(w["board"])[0].action_label == "Post it now"
    assert _check().ok is False
    # The next day: no second email, the item stays.
    tomorrow = timezone.now() + datetime.timedelta(days=1)
    with django_capture_on_commit_callbacks(execute=True):
        assert autopost.check_and_alert(now=tomorrow) is False
    assert len(mail.outbox) == 2
    assert _missed_items(w["board"])


def test_posting_clears_the_item_and_the_health_check(w, django_capture_on_commit_callbacks):
    _auto(w)
    with django_capture_on_commit_callbacks(execute=True):
        autopost.check_and_alert()
    _post_this_month()
    assert not _missed_items(w["board"])
    assert _check().ok is True
    with django_capture_on_commit_callbacks(execute=True):
        assert autopost.check_and_alert() is False


def test_off_means_no_alert(w, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assert autopost.check_and_alert() is False
    assert not mail.outbox and not _missed_items(w["board"])


def test_task_records_why_it_failed_without_personal_data(w, django_capture_on_commit_callbacks):
    _auto(w)
    community.set_rule(key="dues.monthly_cents", value="", citation="", actor=w["board"])
    with django_capture_on_commit_callbacks(execute=True):
        assert monthly_assessments(0) == 0
    row = AssessmentPostingCheck.objects.get(period=f"{_this_month():%Y-%m}")
    assert row.reason == "no_amount" and row.failed_at is not None
    # Only a period, a reason code and times are kept.
    assert {f.name for f in AssessmentPostingCheck._meta.fields} == {
        "period",
        "reason",
        "failed_at",
        "emailed_at",
    }
    assert "Reason: no monthly amount is set" in mail.outbox[0].body
    assert "no monthly amount" in _check().detail.lower()


def test_task_posts_and_sends_nothing_when_it_works(w, django_capture_on_commit_callbacks):
    _auto(w)
    with django_capture_on_commit_callbacks(execute=True):
        assert monthly_assessments(0) == 2
    assert not mail.outbox
    assert not AssessmentPostingCheck.objects.exists()
