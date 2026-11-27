"""Release numbers (decision D37): VERSION holds MAJOR.MINOR.PATCH, the
changelog has an entry for it, System status shows it, and the VM agent
reports the newer code's number only through a psql variable."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from community.models import Role
from community.tests.factories import make_member

VERSION = Path("VERSION").read_text().strip()


def test_version_file_is_a_release_number_with_a_changelog_entry():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)
    assert settings.APP_RELEASE == VERSION
    changelog = Path("CHANGELOG.md").read_text()
    assert f"## {VERSION} " in changelog or f"## {VERSION}\n" in changelog
    # Newest entry first.
    first = re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.M).group(1)
    assert first == VERSION


def test_version_file_ships_in_the_image():
    ignored = Path(".dockerignore").read_text().split()
    assert "VERSION" not in ignored


@pytest.mark.django_db
def test_status_page_shows_the_release_and_the_update_number(client):
    from core.models import UpdateStatus

    client.force_login(make_member(roles=[Role.SYSADMIN]))
    page = client.get(reverse("system_status")).content.decode()
    assert f">{VERSION}<" in page and "Build " in page
    UpdateStatus.objects.create(
        pk=1,
        latest_commit="bbb2222",
        latest_version="9.9.9",
        behind=3,
        agent_seen_at=timezone.now(),
        checked_at=timezone.now(),
    )
    page = client.get(reverse("system_status")).content.decode()
    assert "Update available: version 9.9.9, 3 changes (build bbb2222)" in page


def test_agent_reads_the_new_number_safely():
    text = Path("infra/poc/portal-agent.sh").read_text()
    assert 'g show "$TARGET:VERSION"' in text and "tr -cd '0-9.'" in text
    assert '-v version="$version"' in text and "latest_version = :'version'" in text
