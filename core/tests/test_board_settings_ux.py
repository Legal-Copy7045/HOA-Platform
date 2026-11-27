"""UX review: board settings pages - dollars shown back and a confirmation
for big dues changes, "needs attention" rules first, a counted confirmation
before reminding invitees, a named confirmation before switching a feature
off, and "Save all" on Home neighbors."""

import datetime
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts import services as accounts
from accounts.models import Invitation
from arc import signoffs
from audit.models import AuditEvent
from community import services as community
from community.rules import rule
from community.tests.factories import LotFactory, days_ago, make_member
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


@pytest.fixture
def board():
    return make_member(board=True)


def _set_dues(cents):
    community.set_rule(
        key="dues.monthly_cents", value=str(cents), citation="Declaration §7.2", actor=SYSTEM
    )


# --- Community settings ---------------------------------------------------------------------


def test_dues_are_shown_back_in_dollars_per_month(client, board):
    _set_dues(7500)
    client.force_login(board)
    page = client.get(reverse("community_settings")).content.decode()
    assert "= $75.00 per month" in page
    assert 'data-money-preview="per month"' in page


def test_saving_dues_names_the_parsed_amount(client, board):
    _set_dues(7500)
    client.force_login(board)
    response = client.post(
        reverse("community_rule_save"),
        {"key": "dues.monthly_cents", "value": "80", "citation": "Declaration §7.2"},
        follow=True,
    )
    assert rule("dues.monthly_cents") == 8000
    assert "Monthly" in response.content.decode() and "= $80.00 per month" in (
        response.content.decode()
    )


def test_big_dues_change_asks_for_confirmation_first(client, board):
    _set_dues(7500)
    client.force_login(board)
    data = {"key": "dues.monthly_cents", "value": "150", "citation": "Board resolution 2026-4"}
    response = client.post(reverse("community_rule_save"), data)
    page = response.content.decode()
    assert response.status_code == 200
    assert "up by 100%" in page and "$75.00" in page and "$150.00 per month" in page
    assert 'name="value" value="150"' in page  # the typed dollars, not cents
    assert rule("dues.monthly_cents") == 7500  # nothing saved yet
    client.post(reverse("community_rule_save"), {**data, "confirm": "yes"})
    assert rule("dues.monthly_cents") == 15000


def test_small_dues_change_and_first_setting_need_no_confirmation(client, board):
    client.force_login(board)
    data = {"key": "dues.monthly_cents", "value": "75", "citation": "Declaration §7.2"}
    assert client.post(reverse("community_rule_save"), data).status_code == 302
    assert rule("dues.monthly_cents") == 7500
    data["value"] = "90"  # +20%
    assert client.post(reverse("community_rule_save"), data).status_code == 302
    assert rule("dues.monthly_cents") == 9000


def test_rules_needing_attention_are_listed_first(client, board):
    _set_dues(7500)  # has a citation; due_day has only its default, so needs attention
    client.force_login(board)
    page = client.get(reverse("community_settings")).content.decode()
    assert "need attention" in page or "needs attention" in page
    assert page.index('id="rule-dues.due_day"') < page.index('id="rule-dues.monthly_cents"')
    summary = page.split('data-testid="attention-summary"')[1].split("</div>")[0]
    assert "#rule-dues.due_day" in summary and "#rule-dues.monthly_cents" not in summary


def test_owner_cannot_save_rules(client):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    response = client.post(
        reverse("community_rule_save"),
        {"key": "dues.monthly_cents", "value": "1", "citation": "x", "confirm": "yes"},
    )
    assert response.status_code == 403
    assert not rule("dues.monthly_cents")


# --- Onboarding reminders ---------------------------------------------------------------------


@mock.patch("accounts.tasks.send_invitation.defer")
def test_remind_button_confirms_with_the_count(_defer, client, board):
    for i, age in enumerate((5, 1)):
        person = community.record_person(
            display_name=f"Invitee {i}", email=f"invitee{i}@example.com", actor=SYSTEM
        )
        community.record_ownership(
            person=person, lot=LotFactory(), start_date=days_ago(10), actor=SYSTEM
        )
        inv = accounts.create_invitation(person=person, actor=board)
        Invitation.objects.unscoped("t").filter(pk=inv.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=age)
        )
    client.force_login(board)
    page = client.get(reverse("lot_list"), {"view": "signups"}).content.decode()
    assert 'data-confirm="Email a new invitation link to 1 person who was invited' in page
    assert "Remind those who haven't signed up (1)" in page
    assert len(accounts.reminder_due()) == 1


# --- Features ---------------------------------------------------------------------------------


def _features_post(**overrides):
    """The Features step's form with every feature as it is now, plus changes."""
    from core import features

    states = features.states()
    data = {f"feature__{f.key}": "on" for f in features.FEATURES if states.get(f.key, f.default)}
    for key, on in overrides.items():
        data.pop(f"feature__{key}", None)
        if on:
            data[f"feature__{key}"] = "on"
    return data


def test_features_has_one_page_the_setup_step(client, board):
    """WT-038: Settings -> Features and the setup step were two editors; the
    old address opens the step."""
    client.force_login(board)
    response = client.get(reverse("feature_settings"))
    assert response["Location"] == reverse("setup_step", args=["features"])


def test_switching_a_feature_off_names_what_disappears(client, board):
    from governance.models import Vote

    Vote.objects.create(title="Pond fence", kind="informal", status="open", created_by=board)
    client.force_login(board)
    page = client.post(
        reverse("setup_step", args=["features"]), _features_post(votes=False)
    ).content.decode()
    assert "Switch off Votes? It leaves the menu for everyone" in page
    assert "1 vote is open." in page
    assert "Its records are kept" in page


def test_switching_on_needs_no_confirmation(client, board):
    from core import features

    features.set_feature("faq", False, actor=board)
    client.force_login(board)
    response = client.post(reverse("setup_step", args=["features"]), _features_post(faq=True))
    assert response.status_code == 302 and features.enabled("faq")


# --- Home neighbors: Save all --------------------------------------------------------------------


@pytest.fixture
def lots():
    return [LotFactory() for _ in range(4)]


def test_neighbors_save_all_saves_only_changed_rows(client, board, lots):
    a, b, c, d = lots
    signoffs.set_neighbors(a, left=b, actor=board)
    client.force_login(board)
    page = client.get(reverse("arc_neighbors")).content.decode()
    # One Save all, in the sticky bar (WT-032).
    assert page.count('name="save_all"') == 1 and 'class="save-bar"' in page
    data = {"save_all": "1"}
    for lot in lots:
        data.update(
            {f"left_{lot.number}": "", f"right_{lot.number}": "", f"others_{lot.number}": ""}
        )
    data[f"left_{a.number}"] = str(b.number)  # unchanged
    data[f"left_{c.number}"] = str(d.number)
    data[f"others_{d.number}"] = f"{a.number}, {b.number}"
    before = AuditEvent.objects.filter(action="arc.neighbors_set").count()
    response = client.post(reverse("arc_neighbors"), data, follow=True)
    assert b"Neighbors saved for 2 homes." in response.content
    assert AuditEvent.objects.filter(action="arc.neighbors_set").count() == before + 2
    assert signoffs.neighbor_sides(c)["left"] == d
    assert sorted(x.pk for x in signoffs.neighbor_sides(d)["other"]) == sorted([a.pk, b.pk])
    assert signoffs.neighbor_sides(a)["left"] == b


def test_neighbors_single_row_post_still_works(client, board, lots):
    a, b, *_ = lots
    client.force_login(board)
    client.post(reverse("arc_neighbors"), {"lot": str(a.number), "right": str(b.number)})
    assert signoffs.neighbor_sides(a)["right"] == b


def test_owner_cannot_save_neighbors(client, lots):
    a, b, *_ = lots
    client.force_login(make_member(lots=[a]))
    response = client.post(
        reverse("arc_neighbors"), {"save_all": "1", f"left_{a.number}": str(b.number)}
    )
    assert response.status_code == 403
    assert signoffs.neighbor_sides(a)["left"] is None
