"""Invalid forms are shown again with what was typed (UX review, top 10 #1).

A form that fails validation must not redirect and throw the input away: the
page comes back with the bound form, a summary at the top naming the fields to
fix, the errors beside each field, and its collapsed panel open.
"""

import pytest
from django.urls import reverse

from community.tests.factories import LotFactory, make_member

pytestmark = pytest.mark.django_db

TYPED = "Typed-but-not-lost-7731"

# url name -> POST data that misses a required field but carries TYPED text.
CASES = {
    "meetings": {"title": TYPED, "kind": "board"},  # no start time
    "resolutions": {"title": TYPED, "kind": "resolution"},  # no text or date
    "votes": {"title": TYPED},  # no options (formal votes only; polls are surveys)
    "vendors": {"contact_name": TYPED},  # no name
    "work_orders": {"description": TYPED},  # no title
    "invoices": {"number": TYPED},  # no vendor, amount...
    "contracts": {"title": TYPED},  # no vendor, dates...
    "violations": {"description": TYPED},  # no lot, category, date
}


@pytest.fixture
def board(client):
    from operations.models import ExpenseCategory

    LotFactory()
    ExpenseCategory.objects.create(code="5100", name="Grounds")
    user = make_member(board=True)
    client.force_login(user)
    return user


@pytest.mark.parametrize("name", sorted(CASES))
def test_invalid_form_comes_back_with_the_input_and_errors(client, board, name):
    response = client.post(reverse(name), CASES[name])
    assert response.status_code == 200, name
    page = response.content.decode()
    assert TYPED in page  # nothing typed is lost
    assert "Nothing has been saved yet" in page  # the summary at the top
    assert 'role="alert"' in page
    assert "has-error" in page  # the error beside the field
    assert '<details class="panel" open' in page  # the form's panel opens itself


def test_vendor_detail_invalid_edit_is_kept(client, board):
    from operations.models import Vendor

    vendor = Vendor.objects.create(name="Pond Co")
    response = client.post(
        reverse("vendor_detail", args=[vendor.pk]), {"name": "", "contact_name": TYPED}
    )
    assert response.status_code == 200
    assert TYPED in response.content.decode()
    vendor.refresh_from_db()
    assert vendor.name == "Pond Co"


def test_service_refusal_is_shown_on_the_form(client, board, monkeypatch):
    """A refusal from the service (not a field error) keeps the input too."""
    from governance import services

    def refuse(**kwargs):
        raise services.GovernanceError("That date is not allowed.")

    monkeypatch.setattr(services, "create_meeting", refuse)
    response = client.post(
        reverse("meetings"),
        {"title": TYPED, "kind": "board", "starts_at": "2027-01-05T19:00"},
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert TYPED in page and "That date is not allowed." in page


def test_owner_cannot_use_the_board_forms(client):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    for name in ("meetings", "resolutions", "votes", "violations", "contracts"):
        response = client.post(reverse(name), CASES[name])
        assert response.status_code in (403, 404), name
