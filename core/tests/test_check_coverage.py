import pytest
from django.core.management import call_command

from community.tests.factories import make_member


@pytest.mark.django_db
def test_check_coverage_reports_thin_actions(capsys):
    make_member(board=True)
    call_command("check_coverage")
    assert "ownership.manage: 1 person(s)" in capsys.readouterr().out
