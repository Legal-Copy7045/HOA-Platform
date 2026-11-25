"""PA law guidance and statutory fallbacks for community settings."""

import pytest
from django.urls import reverse

from community import pa_law, services
from community.rules import REGISTRY, rule, rule_origin, rule_with_citation, rules_needing_attention
from community.tests.factories import make_member
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


def test_every_setting_has_guidance_and_every_group_a_note():
    assert set(REGISTRY) <= set(pa_law.GUIDES)
    assert {s.group for s in REGISTRY.values()} <= set(pa_law.GROUP_NOTES)
    for key, guide in pa_law.GUIDES.items():
        assert guide.source and guide.law and guide.example, key
        if guide.fallback is not None:
            assert "Pa.C.S." in guide.fallback_citation, key


def test_statutory_fallbacks_apply_when_nothing_is_recorded():
    assert rule("meeting.owner_notice_days") == 10
    assert rule("resale.deadline_days") == 10
    assert rule("vote.quorum_percent") == 20
    value, citation = rule_with_citation("vote.quorum_percent")
    assert value == 20 and "5309" in citation
    assert rule_origin("vote.quorum_percent") == "pa_law"
    keys = {s.key for s in rules_needing_attention()}
    assert "meeting.owner_notice_days" not in keys
    assert "arc.review_days" in keys  # no statutory value: the Declaration must say


def test_a_recorded_value_overrides_the_fallback():
    services.set_rule(
        key="meeting.owner_notice_days", value="14", citation="Bylaws §2.4", actor=SYSTEM
    )
    assert rule_with_citation("meeting.owner_notice_days") == (14, "Bylaws §2.4")
    assert rule_origin("meeting.owner_notice_days") == "recorded"


def test_settings_page_shows_the_guidance(client):
    client.force_login(make_member(board=True))
    page = client.get(reverse("community_settings")).content
    assert b"PA law fallback" in page
    assert "§ 5308".encode() in page
    assert b"Usually set in: Declaration" in page
