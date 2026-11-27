"""The 0.43.0 menu trim (D95): My reimbursements leaves My home (board side:
Finance overview, a committee's page, My account, My items), "Notices about
my home" is "My violations", and records requests are a section of My
requests with "Ask for records"."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from community.models import Person, Role
from community.tests.factories import LotFactory, make_member
from core import navigation
from core.scoping import system_access
from core.tests.test_nav_streamline import menu, role_users
from documents.tests.helpers import pdf
from governance import services as gov
from operations import reimbursements as rb
from operations.models import ExpenseCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def board():
    return make_member(board=True, lots=[LotFactory()])


def _labels(sections) -> list[str]:
    return [label for labels in sections.values() for label in labels]


def _current(client, user, url) -> list[str]:
    client.force_login(user)
    response = client.get(url)
    return [
        i["label"] for s in response.context["nav"]["sections"] for i in s["items"] if i["current"]
    ]


# --- Every role's menu ---------------------------------------------------------------------


def test_no_role_has_the_old_entries_or_a_duplicate_label(client, board):
    for role, user in role_users(board).items():
        labels = _labels(menu(client, user))
        for gone in ("My reimbursements", "My records requests", "Notices about my home"):
            assert gone not in labels, (role, gone)
        assert len(labels) == len(set(labels)), (role, labels)


def test_menu_lengths(client, board):
    """The owner's sidebar is 14 entries (was 15), a board member's 35 (was 37)."""
    users = role_users(board)
    assert len(_labels(menu(client, users["owner"]))) == 14
    assert len(_labels(menu(client, board))) == 35


def test_owner_violations_entry_is_named_my_violations(client, board):
    users = role_users(board)
    for role in ("owner", "authorized", "view_only", "board"):
        assert "My violations" in menu(client, users[role])["My home"], role
    # The board's list keeps its name: no two entries read "Violations".
    labels = _labels(menu(client, board))
    assert labels.count("Violations") == 1 and labels.count("My violations") == 1
    client.force_login(users["owner"])
    page = client.get(reverse("violations") + "?mine=1").content.decode()
    assert "<h1>My violations</h1>" in page
    assert "My violations" in client.get(reverse("my_lot")).content.decode()


def test_committee_member_menu_has_no_reimbursements_entry(client, board):
    member = make_member(lots=[LotFactory()])
    committee = gov.create_committee(name="Grounds", actor=board)
    with system_access("t"):
        person = Person.objects.get(user=member)
    gov.appoint_committee_member(committee, person=person, actor=board)
    sections = menu(client, member)
    assert "My reimbursements" not in _labels(sections)
    # ...but the committee's page links to their claims (D95).
    page = client.get(reverse("committee_detail", args=[committee.pk])).content.decode()
    assert f'href="{reverse("reimbursements")}?mine=1"' in page
    # An owner who is on no committee gets no such link there.
    client.force_login(make_member(lots=[LotFactory()]))
    other = client.get(reverse("committee_detail", args=[committee.pk]))
    if other.status_code == 200:
        assert 'data-testid="my-reimbursements-link"' not in other.content.decode()


# --- My reimbursements: reached from the board side ---------------------------------------


def test_finance_overview_links_the_board_members_own_claims(client, board):
    client.force_login(board)
    page = client.get(reverse("finance_overview")).content.decode()
    assert f'href="{reverse("reimbursements")}?mine=1"' in page
    # The manager and auditor make no claims of their own here.
    for role in (Role.MANAGER, Role.AUDITOR):
        client.force_login(make_member(roles=[role]))
        page = client.get(reverse("finance_overview")).content.decode()
        assert 'data-testid="my-reimbursements-link"' not in page, role
    # On the page itself the Finance overview is the current entry.
    assert _current(client, board, reverse("reimbursements") + "?mine=1") == ["Finance overview"]


def test_switched_off_reimbursements_hide_the_link(client, board):
    from core import features

    features.set_feature("reimbursements", False, actor=board)
    client.force_login(board)
    page = client.get(reverse("finance_overview")).content.decode()
    assert 'data-testid="my-reimbursements-link"' not in page


def test_own_claim_waits_in_my_items_without_a_menu_bubble(client, board):
    category = ExpenseCategory.objects.create(code="5400", name="Supplies")
    rb.submit(
        amount_cents=4250,
        spent_on=timezone.localdate() - datetime.timedelta(days=3),
        payee="Hardware store",
        category=category,
        description="Paint for the pavilion",
        files=[pdf("receipt.pdf")],
        actor=board,
    )
    claims = rb.claimant_items(board)
    assert len(claims) == 1 and claims[0].nav == ""
    client.force_login(board)
    page = client.get(reverse("my_items")).content.decode()
    assert "Your reimbursement of" in page
    # My account (anyone who may claim) links the list too.
    page = client.get(reverse("my_account")).content.decode()
    assert f'href="{reverse("reimbursements")}?mine=1"' in page


# --- Records requests inside My requests -------------------------------------------------


def test_my_requests_shows_own_records_requests_with_status(client, board):
    owner = make_member(lots=[LotFactory()])
    other = make_member(lots=[LotFactory()])
    gov.submit_records_request(description="The 2025 audit", actor=owner)
    gov.submit_records_request(description="Someone else's ask", actor=other)
    gov.submit_records_request(description="Board member's own ask", actor=board)
    client.force_login(owner)
    page = client.get(reverse("requests") + "?mine=1").content.decode()
    assert 'data-testid="my-records-requests"' in page
    assert "The 2025 audit" in page and "Submitted" in page
    assert "Someone else" not in page and "Board member" not in page
    assert f'href="{reverse("records_requests")}?mine=1&amp;new=1#new-request"' in page
    assert "Ask for records" in page
    # A board member sees only their own there, never everyone's.
    client.force_login(board)
    page = client.get(reverse("requests") + "?mine=1").content.decode()
    assert "Board member" in page and "The 2025 audit" not in page
    # The board's Owner requests (no mine) has no such section.
    page = client.get(reverse("requests")).content.decode()
    assert 'data-testid="my-records-requests"' not in page


def test_contacts_and_tenants_get_no_records_section(client, board):
    users = role_users(board)
    for role in ("authorized", "view_only", "tenant"):
        client.force_login(users[role])
        page = client.get(reverse("requests") + "?mine=1").content.decode()
        assert 'data-testid="my-records-requests"' not in page, role
        assert "Ask for records" not in page, role


def test_records_request_pages_go_back_to_my_requests(client, board):
    owner = make_member(lots=[LotFactory()])
    obj = gov.submit_records_request(description="Minutes", actor=owner)
    client.force_login(owner)
    mine_url = reverse("requests") + "?mine=1"
    for url in (
        reverse("records_requests") + "?mine=1",
        reverse("records_request_detail", args=[obj.pk]),
    ):
        page = client.get(url).content.decode()
        crumbs = page.split('class="crumbs"', 1)[1].split("</div>", 1)[0]
        assert f'href="{mine_url}"' in crumbs.replace("&amp;", "&"), url
    # My requests is the current entry on the owner's records requests page.
    assert _current(client, owner, reverse("records_requests") + "?mine=1") == ["My requests"]
    # The board reading the owner's request goes back to Records requests.
    client.force_login(board)
    page = client.get(reverse("records_request_detail", args=[obj.pk])).content.decode()
    crumbs = page.split('class="crumbs"', 1)[1].split("</div>", 1)[0]
    assert f'href="{reverse("records_requests")}"' in crumbs


def test_records_request_detail_is_404_for_another_owner(client):
    owner = make_member(lots=[LotFactory()])
    obj = gov.submit_records_request(description="Minutes", actor=owner)
    client.force_login(make_member(lots=[LotFactory()]))
    assert client.get(reverse("records_request_detail", args=[obj.pk])).status_code == 404


def test_sections_list_has_no_mine_entry_for_records_or_reimbursements():
    names = {name for _t, items in navigation.SECTIONS for _l, name, *_ in items}
    assert "records_requests?mine=1" not in names
    assert "reimbursements?mine=1" not in names
    assert "records_requests?mine=1" in navigation.SUBPAGES["requests?mine=1"]
