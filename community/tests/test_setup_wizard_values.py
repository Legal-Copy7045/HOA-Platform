"""Setup wizard and Community settings, release 0.43: percents read as
percents everywhere, every box has a greyed example (never the demo's
"DEMO - replace"), and common areas can be removed (taken out of use, never
deleted) from the wizard, which also saves with no areas at all."""

from __future__ import annotations

import datetime
import re

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import services
from community.models import CommonArea, GoverningRule, Reservation, ReservationState
from community.rule_input import (
    DEMO_CITATION,
    citation_example,
    example_value,
    to_stored,
    to_typed,
)
from community.rules import (
    BASIS_POINTS,
    BOOL,
    PERCENT,
    REGISTRY,
    validate_value,
    value_display,
)
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db

SURCHARGE = "payments.credit_card_surcharge_bp"


@pytest.fixture
def board(client):
    user = make_member(board=True, lots=[LotFactory(street_address="1 Alpha Way")])
    client.force_login(user)
    return user


def step(client, key):
    return client.get(reverse("setup_step", args=[key])).content.decode()


def save(client, key, data):
    return client.post(reverse("setup_step", args=[key]), {"action": "save", **data})


def rule_block(html, key):
    return html.split(f'id="rule-{key}"')[1].split('<div class="setup-rule"')[0]


# --- Percent values ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed,stored", [("3", "300"), ("3.0", "300"), ("2.5", "250"), ("3%", "300")]
)
def test_the_card_surcharge_is_typed_as_a_percent(typed, stored):
    spec = REGISTRY[SURCHARGE]
    assert to_stored(spec, typed) == stored
    assert to_typed(spec, stored) == typed.rstrip("%").removesuffix(".0")


@pytest.mark.parametrize("typed,stored", [("51", "51"), ("51%", "51"), ("51.0", "51")])
def test_whole_percent_rules_accept_the_unit(typed, stored):
    assert to_stored(REGISTRY["vote.pass_percent"], typed) == stored


def test_a_fraction_of_a_whole_percent_is_refused_with_a_percent_message():
    spec = REGISTRY["vote.pass_percent"]
    with pytest.raises(ValueError, match="whole percent"):
        validate_value(spec.key, to_stored(spec, "50.5"))
    with pytest.raises(ValueError, match="0-100%"):
        validate_value(spec.key, to_stored(spec, "101"))


def test_every_percent_rule_reads_as_a_percent():
    for spec in REGISTRY.values():
        if spec.kind == PERCENT:
            assert value_display(spec, "51") == "51%", spec.key
        if spec.kind == BASIS_POINTS:
            assert value_display(spec, "250") == "2.5%", spec.key
            assert value_display(spec, "300") == "3%", spec.key


def test_wizard_shows_the_surcharge_as_a_percent_not_basis_points(client, board):
    block = rule_block(step(client, "payments"), SURCHARGE)
    assert 'placeholder="3"' in block and 'placeholder="300"' not in block
    assert '<span class="unit" aria-hidden="true">%</span>' in block
    assert "Empty means: 3%." in block and "Empty means: 300" not in block


def test_wizard_saves_a_typed_surcharge_in_basis_points(client, board):
    from community import setup

    data = {}
    for spec in setup.rules_for("payments"):
        slug = spec.key.replace(".", "-")
        data[f"{slug}__value"] = ""
        data[f"{slug}__citation"] = ""
    data["payments-credit_card_surcharge_bp__value"] = "2.5"
    data["payments-credit_card_surcharge_bp__citation"] = "Board resolution 2026-04"
    assert save(client, "payments", data).status_code == 302
    assert GoverningRule.objects.get(key=SURCHARGE).value == "250"
    block = rule_block(step(client, "payments"), SURCHARGE)
    assert 'value="2.5"' in block


def test_vote_percents_show_a_suffix_and_read_as_percents_in_the_review(client, board):
    services.set_rule(key="vote.pass_percent", value="51", citation="Bylaws §2.7", actor=SYSTEM)
    block = rule_block(step(client, "governance"), "vote.pass_percent")
    assert "Share of votes cast needed to pass (%)" in block
    assert '<span class="unit" aria-hidden="true">%</span>' in block
    assert 'value="51"' in block
    review = step(client, "review")
    assert re.search(r"Share of votes cast needed to pass</dt><dd>51%", review)


def test_community_settings_show_percents(client, board):
    services.set_rule(key="vote.pass_percent", value="51", citation="Bylaws §2.7", actor=SYSTEM)
    html = client.get(reverse("community_settings")).content.decode()
    block = html.split('id="rule-vote.pass_percent"')[1].split("</li>")[0]
    assert '<span class="unit" aria-hidden="true">%</span>' in block
    surcharge = html.split(f'id="rule-{SURCHARGE}"')[1].split("</li>")[0]
    assert 'value="3"' in surcharge


def test_payments_and_tax_pages_read_basis_points_as_percents():
    from django.template import Context, Template

    out = Template("{% load portal %}{{ a|bp_percent }} {{ b|bp_percent }}").render(
        Context({"a": 300, "b": 3000})
    )
    assert out == "3% 30%"


# --- Examples in every box ----------------------------------------------------------------


def test_every_rule_box_has_a_default_or_an_example():
    for spec in REGISTRY.values():
        if spec.kind == BOOL:
            continue
        has_default = spec.default not in (None, "")
        assert has_default or example_value(spec).startswith("e.g. "), spec.key
        assert citation_example(spec).startswith("e.g. "), spec.key


def test_examples_come_from_the_guides_worked_example():
    assert example_value(REGISTRY["vote.pass_percent"]) == "e.g. 51"


def test_wizard_boxes_carry_greyed_examples(client, board):
    html = step(client, "community")
    for name in ("legal_name", "letterhead", "remit_to", "letter_contact", "signin_help_contact"):
        tag = re.search(rf'<(?:input|textarea)[^>]*name="{name}"[^>]*>', html).group(0)
        assert 'placeholder="e.g. ' in tag, name
    governance = step(client, "governance")
    tag = re.search(r'<input[^>]*name="vote-pass_percent__value"[^>]*>', governance).group(0)
    assert 'placeholder="e.g. ' in tag


def test_the_demo_citation_shows_as_an_example_and_is_kept_when_left_empty(client, board):
    from community import setup

    services.set_rule(key="vote.pass_percent", value="51", citation=DEMO_CITATION, actor=SYSTEM)
    block = rule_block(step(client, "governance"), "vote.pass_percent")
    tag = re.search(r'<input[^>]*name="vote-pass_percent__citation"[^>]*>', block).group(0)
    assert DEMO_CITATION not in tag and 'placeholder="e.g. ' in tag
    assert "The demo data has no real citation here" in block
    data = {}
    for spec in setup.rules_for("governance"):
        row = GoverningRule.objects.filter(key=spec.key).first()
        slug = spec.key.replace(".", "-")
        data[f"{slug}__value"] = to_typed(spec, row.value if row else "")
        data[f"{slug}__citation"] = (
            "" if row is None or row.citation == DEMO_CITATION else row.citation
        )
    page = step(client, "governance")
    for name in re.findall(r'name="(feature__[a-z_]+)" value="on" checked', page):
        data[name] = "on"  # leave the step's switches as they are
    before = AuditEvent.objects.filter(action="community.rule_changed").count()
    assert save(client, "governance", data).status_code == 302
    assert GoverningRule.objects.get(key="vote.pass_percent").citation == DEMO_CITATION
    assert AuditEvent.objects.filter(action="community.rule_changed").count() == before


# --- Common areas -------------------------------------------------------------------------


def test_the_add_fields_are_optional_in_the_browser_too(client, board):
    html = step(client, "common_areas")
    tag = re.search(r'<input[^>]*name="area-name"[^>]*>', html).group(0)
    assert "required" not in tag and 'placeholder="e.g. ' in tag
    response = save(client, "common_areas", {"area-name": "", "area-kind": "amenity"})
    assert response.status_code == 302
    assert not CommonArea.objects.exists()


def test_an_area_is_removed_and_put_back_from_the_wizard(client, board):
    area = services.save_common_area(name="Pond (fictional)", kind="stormwater", actor=board)
    html = step(client, "common_areas")
    assert f'name="area_remove" value="{area.pk}"' in html
    assert 'form="setup-area-actions"' in html  # Enter in the step still saves
    url = reverse("setup_step", args=["common_areas"])
    response = client.post(url, {"area_remove": str(area.pk)})
    assert response.status_code == 302 and response["Location"] == url
    area.refresh_from_db()
    assert area.active is False and CommonArea.objects.filter(pk=area.pk).exists()
    events = AuditEvent.objects.filter(action="community.common_area_saved")
    assert events.latest("occurred_at").metadata == {"kind": "stormwater", "active": False}
    assert f'name="area_restore" value="{area.pk}"' in step(client, "common_areas")
    client.post(url, {"area_restore": str(area.pk)})
    area.refresh_from_db()
    assert area.active is True


def test_an_area_with_upcoming_bookings_is_not_removed(client, board):
    area = services.save_common_area(name="Clubhouse (fictional)", kind="amenity", actor=board)
    start = timezone.now() + datetime.timedelta(days=3)
    with system_access("t"):
        Reservation.objects.create(
            area=area,
            lot=LotFactory(street_address="9 Alpha Way"),
            requested_by=board,
            starts_at=start,
            ends_at=start + datetime.timedelta(hours=2),
            state=ReservationState.CONFIRMED,
        )
    response = client.post(
        reverse("setup_step", args=["common_areas"]), {"area_remove": str(area.pk)}, follow=True
    )
    assert "upcoming booking" in response.content.decode()
    area.refresh_from_db()
    assert area.active is True


def test_only_the_board_removes_areas(client):
    area = services.save_common_area(name="Pond (fictional)", kind="stormwater", actor=SYSTEM)
    owner = make_member(lots=[LotFactory(street_address="5 Alpha Way")])
    client.force_login(owner)
    response = client.post(
        reverse("setup_step", args=["common_areas"]), {"area_remove": str(area.pk)}
    )
    assert response.status_code == 403
    area.refresh_from_db()
    assert area.active is True
    with pytest.raises(Exception):  # noqa: B017 - the service refuses too
        services.set_common_area_in_use(area=area, in_use=False, actor=owner)


def test_an_unknown_area_is_a_404(client, board):
    url = reverse("setup_step", args=["common_areas"])
    assert client.post(url, {"area_remove": "999999"}).status_code == 404
    assert client.post(url, {"area_remove": "x"}).status_code == 404
