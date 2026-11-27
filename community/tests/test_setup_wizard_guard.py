"""The Community setup wizard stays current as features are built (D61):
every governing rule group has its own step, every optional feature is on
the Features step with its description, and every server script that takes
a secret has a status line on the server step."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from community import setup
from community.rules import REGISTRY
from community.tests.factories import LotFactory, make_member
from core import features

pytestmark = pytest.mark.django_db


@pytest.fixture
def board(client):
    user = make_member(board=True, lots=[LotFactory(street_address="1 Alpha Way")])
    client.force_login(user)
    return user


def _page(client, key) -> str:
    response = client.get(reverse("setup_step", args=[key]))
    assert response.status_code == 200, key
    return html.unescape(response.content.decode())


def test_every_rule_group_is_named_by_a_step():
    """No group may fall into the catch-all step silently: a new group in
    community/rules.py needs its step in community/setup.py."""
    assert setup.unmapped_groups() == set(), (
        "Add these rule groups to a step's groups in community/setup.py"
    )
    groups = {spec.group for spec in REGISTRY.values()}
    named = {g for s in setup.STEPS for g in s.groups}
    assert groups <= named
    # The catch-all lists its own groups explicitly too.
    catch = next(s for s in setup.STEPS if s.catch_all)
    assert catch.groups


def test_unmapped_groups_are_reported():
    from community.rules import DAYS, RuleSpec

    fake = {"x.y": RuleSpec("x.y", "A rule of a new group", DAYS, None, "Brand new")}
    assert setup.unmapped_groups(fake) == {"Brand new"}


def test_every_optional_feature_is_on_the_features_step(client, board):
    page = _page(client, "features")
    for f in features.FEATURES:
        assert f.description.strip(), f.key
        assert f'name="feature__{f.key}"' in page, f.key
        assert f.label in page, f.key
        assert f.description in page, f.key


def _secret_scripts_from_claude_md() -> list[str]:
    """The scripts CLAUDE.md's deployment section lists as steps that take
    secrets typed by a person."""
    text = (Path(settings.BASE_DIR) / "CLAUDE.md").read_text()
    section = text.split("## Deployment targets", 1)[1]
    line = next(ln for ln in section.splitlines() if "take secrets typed by a person" in ln)
    inside = line.split("take secrets typed by a person", 1)[1]
    scripts = re.findall(r"`([a-z0-9-]+\.sh)`", inside)
    assert scripts, "CLAUDE.md lists no secret-taking scripts"
    return scripts


def test_every_secret_taking_server_script_has_a_status_line(client, board):
    page = _page(client, "server")
    for script in _secret_scripts_from_claude_md():
        assert f"infra/poc/{script}" in page, script


def test_the_new_voting_rules_are_on_the_governance_step(client, board):
    keys = {s.key for s in setup.rules_for("governance")}
    assert {"vote.min_open_days", "vote.allow_early_close", "vote.show_turnout_to_owners"} <= keys
    page = _page(client, "governance")
    assert "Shortest voting period" in page
    assert "close a formal vote or election before its deadline" in page
    assert "Show owners the turnout" in page
