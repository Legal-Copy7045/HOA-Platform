import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from audit.models import AuditEvent
from community import services
from community.models import CommonArea, CommunityProfile, Role
from community.rules import cents_display, rule, rule_with_citation, rules_needing_attention
from community.services import CommunityError
from community.tests.factories import LotFactory, make_member

pytestmark = pytest.mark.django_db


@pytest.fixture
def board():
    return make_member(board=True)


def test_rules_are_data_with_citations(board):
    assert rule("dues.monthly_cents") is None
    services.set_rule(
        key="dues.monthly_cents", value="4500", citation="Declaration §7.2", actor=board
    )
    assert rule("dues.monthly_cents") == 4500
    assert rule_with_citation("dues.monthly_cents") == (4500, "Declaration §7.2")
    assert AuditEvent.objects.get(action="community.rule_changed").metadata == {
        "key": "dues.monthly_cents",
        "has_citation": True,
    }


def test_board_decision_default_for_invoice_threshold():
    assert rule("invoice.dual_approval_cents") == 50000


def test_rules_without_value_or_citation_need_attention(board):
    keys = {s.key for s in rules_needing_attention()}
    assert {"dues.monthly_cents", "invoice.dual_approval_cents"} <= keys
    services.set_rule(
        key="invoice.dual_approval_cents",
        value="50000",
        citation="Board resolution 2026-11",
        actor=board,
    )
    assert "invoice.dual_approval_cents" not in {s.key for s in rules_needing_attention()}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("dues.monthly_cents", "45.00"),
        ("vote.quorum_percent", "150"),
        ("arc.deemed_approval", "maybe"),
    ],
)
def test_rule_values_are_validated(board, key, value):
    with pytest.raises(CommunityError):
        services.set_rule(key=key, value=value, actor=board)


def test_unknown_rule_is_refused(board):
    with pytest.raises(CommunityError):
        services.set_rule(key="made.up", value="1", actor=board)


@pytest.mark.parametrize("roles", [[Role.MANAGER], [Role.AUDITOR]])
def test_only_the_board_changes_rules(roles):
    with pytest.raises(PermissionDenied):
        services.set_rule(key="dues.monthly_cents", value="1", actor=make_member(roles=roles))


def test_owner_cannot_change_rules():
    with pytest.raises(PermissionDenied):
        services.set_rule(
            key="dues.monthly_cents", value="1", actor=make_member(lots=[LotFactory()])
        )


def test_profile_and_common_areas(board):
    services.update_profile(legal_name="Fictional HOA", fiscal_year_start_month=1, actor=board)
    assert CommunityProfile.get().legal_name == "Fictional HOA"
    with pytest.raises(CommunityError):
        services.update_profile(legal_name="X", fiscal_year_start_month=13, actor=board)
    services.save_common_area(name="North pond", kind="stormwater", actor=board)
    assert CommonArea.objects.get().name == "North pond"


def test_cents_display():
    assert cents_display(600) == "$6.00"
    assert cents_display(-123456) == "-$1,234.56"


def test_settings_page(client, board):
    client.force_login(board)
    response = client.get(reverse("community_settings"))
    assert response.status_code == 200
    assert b"needs attention" in response.content
    client.post(
        reverse("community_rule_save"),
        {"key": "late_fee.cents", "value": "6.00", "citation": "Collections policy (demo)"},
    )
    assert rule("late_fee.cents") == 600
    client.post(
        reverse("community_rule_save"),
        {"key": "late_fee.cents", "value": "75", "citation": "Collections policy (demo)"},
    )
    assert rule("late_fee.cents") == 7500  # typing 75 means $75, not 75 cents


def test_manager_can_view_but_not_edit_settings(client):
    client.force_login(make_member(roles=[Role.MANAGER]))
    response = client.get(reverse("community_settings"))
    assert response.status_code == 200 and b"rule-form" not in response.content
    assert client.post(reverse("community_rule_save"), {"key": "late_fee.cents"}).status_code == 403
