"""Milestone 4: the board switches optional features on and off."""

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import LotFactory, make_member
from core import features

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    return {
        "board": make_member(board=True),
        "owner": make_member(lots=[LotFactory()]),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
    }


def test_defaults_reservations_off_everything_else_on():
    states = features.states()
    assert states["reservations"] is False
    assert all(v for k, v in states.items() if k != "reservations")


def test_switched_off_feature_leaves_menu_and_pages_404(client, w):
    client.force_login(w["owner"])
    assert b'href="/faq/"' in client.get(reverse("home")).content
    features.set_feature("faq", False, actor=w["board"])
    assert client.get(reverse("faq")).status_code == 404
    assert b'href="/faq/"' not in client.get(reverse("home")).content
    features.set_feature("faq", True, actor=w["board"])
    assert client.get(reverse("faq")).status_code == 200


def test_every_gated_url_name_exists():
    from django.urls import get_resolver

    names = {n for n in get_resolver().reverse_dict if isinstance(n, str)}
    missing = {n for f in features.FEATURES for n in f.url_names} - names
    assert not missing, missing


def test_only_the_board_switches_and_it_is_audited(client, w):
    for user in (w["owner"], w["sysadmin"]):
        with pytest.raises(PermissionDenied):
            features.set_feature("faq", False, actor=user)
        client.force_login(user)
        assert client.post(reverse("feature_settings"), {"feature": "faq"}).status_code == 403
    client.force_login(w["board"])
    page = client.get(reverse("feature_settings")).content
    assert b"Common area reservations" in page
    client.post(reverse("feature_settings"), {"feature": "calendar"})
    assert features.enabled("calendar") is False
    assert AuditEvent.objects.filter(action="feature.switched").exists()


def test_my_items_hide_switched_off_features(w):
    from core.my_items import Item, items_for

    features.set_feature("votes", False, actor=w["board"])
    assert all("/votes/" not in i.url for i in items_for(w["owner"]))
    assert Item  # the queue still builds
