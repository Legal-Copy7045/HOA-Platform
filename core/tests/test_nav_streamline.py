"""Navigation streamlining (owner-approved 2026-11-26): section names, the
sidebar per role, one-page hubs, Board overview folded into My items, Preview
as owner, the sysadmin home and the owner dashboard."""

from unittest import mock

import pytest
from django.urls import reverse

from community import services
from community.models import Account, BoardSeat, Ownership, Person, Role
from community.tests.factories import LotFactory, UserFactory, days_ago, enroll_totp, make_member
from core import navigation
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def _contact(lot, kind):
    """A login on lot's account as an authorized, view-only or tenant contact."""
    user = UserFactory()
    person = services.record_person(display_name=f"{kind} person", actor=SYSTEM)
    with system_access("test"):
        Person.objects.filter(pk=person.pk).update(user=user)
        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    person.refresh_from_db()
    enroll_totp(user)
    services.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


def _former_owner():
    user = make_member(lots=[LotFactory()])
    with system_access("test"):
        ownership = Ownership.objects.get(person__user=user)
    services.end_ownership(ownership, end_date=days_ago(1), actor=SYSTEM)
    return user


def role_users(board) -> dict:
    """One login per role (board passed in, so tests can reuse theirs)."""
    lot = LotFactory()
    owner = make_member(lots=[lot])
    return {
        "owner": owner,
        "authorized": _contact(lot, "authorized"),
        "view_only": _contact(lot, "view_only"),
        "tenant": _contact(lot, "tenant"),
        "former_owner": _former_owner(),
        "board": board,
        "manager": make_member(roles=[Role.MANAGER]),
        "arc_reviewer": make_member(roles=[Role.ARC_REVIEWER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
    }


def menu(client, user, url=None) -> dict[str, list[str]]:
    client.force_login(user)
    response = client.get(url or reverse("home"))
    return {
        s["title"]: [i["label"] for i in s["items"]] for s in response.context["nav"]["sections"]
    }


def entry(client, user, label, url=None) -> dict:
    client.force_login(user)
    response = client.get(url or reverse("home"))
    items = [i for s in response.context["nav"]["sections"] for i in s["items"]]
    return next(i for i in items if i["label"] == label)


@pytest.fixture
def board():
    return make_member(board=True, lots=[LotFactory()])


# --- The sidebar per role -------------------------------------------------------------------

MY_HOME = [
    "My home",
    "My account",
    "My ARC requests",
    # 0.43.0 (D95): "My violations" (was "Notices about my home"); records
    # requests are a section of My requests, not an entry of their own.
    "My violations",
    "My requests",
]
COMMUNITY = [
    "Announcements",
    "Calendar",
    "Voting",
    "Documents",
    "Resolutions and policies",
    "Budget and reserves",
    "Community info",
]
CONTACT_HOME = ["My home", "My account", "My violations", "My requests"]
NOT_BUDGET = [c for c in COMMUNITY if c != "Budget and reserves"]
# Budgets, Month and year close, Financial statements and the old More
# finance pages open from the Finance overview (D89).


FINANCE = [
    # The Finance overview first (D89); Budgets and the old More finance
    # pages open from it.
    "Finance overview",
    "Owner accounts",
    "Invoices",
    "Collections",
    "Accounting",
    "Reports",
]
MAINTENANCE = ["Work orders", "Vendors", "Contracts", "Renewals"]  # Renewals: D72
# Every role's sidebar with the default features (reservations, online
# payments and texts are off by default).
# Help (D73) is in the first section for everyone who sees the community
# pages; not for former owners or sysadmins.
MENUS = {
    "owner": {"": ["Dashboard", "Help"], "My home": MY_HOME, "Community": COMMUNITY},
    "authorized": {"": ["Dashboard", "Help"], "My home": CONTACT_HOME, "Community": COMMUNITY},
    "view_only": {"": ["Dashboard", "Help"], "My home": CONTACT_HOME, "Community": COMMUNITY},
    # A tenant can only answer surveys, so Voting is named Surveys (WT-018).
    "tenant": {
        "": ["Dashboard", "Help"],
        "My home": ["My requests"],
        "Community": ["Surveys" if c == "Voting" else c for c in NOT_BUDGET],
    },
    # WT-014: the former owner's one section is titled after its one page.
    # WT-027 (D100): the one entry sits in the lead section, not a one-entry group.
    "former_owner": {"": ["Dashboard", "My history"]},
    "board": {
        "": ["Dashboard", "My items", "Help"],
        # My reimbursements opens from the Finance overview (D95).
        "My home": MY_HOME,
        "Community": COMMUNITY,
        "Association": [
            "Owner requests",
            "Architectural review",
            "Violations",
            "Records requests",
            "Tasks and deadlines",
            "People",
            "Homes and owners",
            "Paper mail",
        ],
        "Maintenance": MAINTENANCE,
        "Finance": FINANCE,
        # Server tasks history is on Sysadmin activity for the board (WT-007).
        "Admin": ["Sysadmin activity", "Settings"],
    },
    "manager": {
        "": ["Dashboard", "My items", "Help"],
        "Community": COMMUNITY,
        "Association": [
            "Owner requests",
            "Architectural review",
            "Violations",
            "Records requests",
            "Tasks and deadlines",
            "Homes and owners",
            "Paper mail",
        ],
        "Maintenance": MAINTENANCE,
        "Finance": FINANCE,
        # Paper mail settings opens from Paper mail, so Settings has one page
        # and the menu links straight to it (WT-009).
        "Admin": ["Community settings"],
    },
    "arc_reviewer": {
        "": ["Dashboard", "My items", "Help"],
        "Community": NOT_BUDGET,
        "Association": ["Architectural review"],
    },
    "auditor": {
        "": ["Dashboard", "Help"],
        "Community": COMMUNITY,
        "Association": ["Architectural review", "Violations", "Homes and owners"],
        "Maintenance": MAINTENANCE,
        "Finance": FINANCE,
        "Admin": ["Sysadmin activity", "Community settings"],
    },
    "sysadmin": {
        "": ["Dashboard"],
        "Admin": ["System status", "Server tasks", "Invitations", "Branding"],
    },
}


def _same_community_info(sections):
    """Community info holds FAQ (+ Board and committees once that page exists,
    + directory and committees by role). Where only the FAQ is open the entry
    is the FAQ itself (one-page rule); count it as Community info here."""
    return {
        title: ["Community info" if i == "Frequently asked questions" else i for i in items]
        for title, items in sections.items()
    }


def test_sections_are_renamed(client, board):
    sections = menu(client, board)
    assert list(sections) == [
        "",
        "My home",
        "Community",
        "Association",
        "Maintenance",
        "Finance",
        "Admin",
    ]
    assert "Homeowner" not in sections and "Board" not in sections


def test_sidebar_for_every_role(client, board):
    wrong = {}
    for role, user in role_users(board).items():
        got = _same_community_info(menu(client, user))
        if got != MENUS[role]:
            wrong[role] = got
    assert not wrong, wrong


def test_board_menu_has_every_section_and_no_retired_entries(client, board):
    sections = menu(client, board)
    assert sections["Association"] == [
        "Owner requests",
        "Architectural review",
        "Violations",
        "Records requests",
        "Tasks and deadlines",
        "People",
        "Homes and owners",
        "Paper mail",
    ]
    assert sections["Maintenance"] == MAINTENANCE
    assert sections["Finance"] == FINANCE
    assert sections["Admin"] == ["Sysadmin activity", "Settings"]
    everything = [label for labels in sections.values() for label in labels]
    for gone in (
        "Board overview",
        "Approvals",
        "Invitations and sign-ups",
        "Second approvals",
        "Written consents",
        "Urgent texts",
        "Onboarding",
        "Roster",
        "Neighbor acknowledgements",
        "Lots",
        "Budget",
    ):
        assert gone not in everything, gone
    # Records requests appears once for the board; the owner side is a
    # section of My requests (D95), and My reimbursements left the menu.
    assert everything.count("Records requests") == 1
    for gone in ("My records requests", "My reimbursements", "Notices about my home"):
        assert gone not in everything, gone
    assert len(everything) == len(set(everything)), "duplicate menu labels"


def test_online_payments_only_with_payments_manage(client, board):
    from core import features

    features.set_feature("online_payments", True, actor=board)

    def cards(user):
        client.force_login(user)
        response = client.get(reverse("finance_overview"))
        return [c["label"] for g in response.context["groups"] for c in g["cards"]]

    assert "Online payments" in cards(board)
    for role in (Role.MANAGER, Role.AUDITOR):
        assert "Online payments" not in cards(make_member(roles=[role]))


def test_former_owner_has_history_not_my_requests(client, board):
    users = role_users(board)
    sections = menu(client, users["former_owner"])
    assert sections == {"": ["Dashboard", "My history"]} and "My home" not in sections
    for role in ("view_only", "tenant", "authorized"):
        assert "My requests" in menu(client, users[role])["My home"], role


# --- One visible page: link straight to it --------------------------------------------------


def test_tenant_voting_goes_straight_to_surveys(client, board):
    tenant = role_users(board)["tenant"]
    item = entry(client, tenant, "Surveys")
    # Straight to the filtered Voting list, not the old /surveys/ redirect,
    # named like the page's heading (WT-018).
    assert item["url"] == reverse("voting") + "?type=surveys"
    page = client.get(item["url"])
    assert page.status_code == 200
    assert "<h1>Surveys</h1>" in page.content.decode()


def test_sysadmin_settings_goes_straight_to_branding(client):
    sysadmin = make_member(roles=[Role.SYSADMIN])
    sections = menu(client, sysadmin)
    assert sections["Admin"] == [
        "System status",
        "Server tasks",
        "Invitations",
        "Branding",
    ]
    item = entry(client, sysadmin, "Branding")
    assert item["url"] == reverse("branding_settings")
    assert entry(client, sysadmin, "Branding", reverse("branding_settings"))["current"]


def test_a_hub_with_one_page_uses_that_pages_name(client, board):
    tenant = role_users(board)["tenant"]
    one = [navigation.HUBS["community_info"][0]]  # only the FAQ
    with mock.patch.dict(navigation.HUBS, {"community_info": one}):
        item = entry(client, tenant, "Frequently asked questions")
    assert item["url"] == reverse("faq")


def test_hub_with_several_pages_links_to_the_hub(client, board):
    assert entry(client, board, "Settings")["url"] == reverse("settings_hub")
    assert entry(client, board, "Voting")["url"] == reverse("voting")


def test_subpages_mark_their_entry_current(client, board):
    cases = [
        ("access_review", "People"),
        ("tasks", "Tasks and deadlines"),
        ("signoffs_mine", "My ARC requests"),
    ]
    for name, label in cases:
        url = reverse(name) + ("?tab=deadlines" if name == "tasks" else "")
        assert entry(client, board, label, url)["current"], name


# --- Board overview folded into My items ----------------------------------------------------


def test_board_overview_redirects_to_my_items(client, board):
    client.force_login(board)
    assert client.get(reverse("board_home"))["Location"] == reverse("my_items")
    client.force_login(make_member(lots=[LotFactory()]))
    assert client.get(reverse("board_home")).status_code == 403


def test_two_person_coverage_warning_stays_visible_to_the_board(client, board):
    """CLAUDE.md: every action must stay performable by two people with MFA
    and the app warns when it is not. Board overview is gone, so the warning
    is at the top of My items and on the dashboard."""
    client.force_login(board)  # the only board member: most actions are thin
    page = client.get(reverse("my_items")).content.decode()
    assert 'id="coverage"' in page and "fewer than two people" in page
    assert "Show which" in page
    assert 'id="area-' not in page.split('id="coverage"')[0]  # first, above every list
    home = client.get(reverse("home")).content.decode()
    assert "Fewer than two people with two-step sign-in can act" in home
    assert reverse("my_items") + "#coverage" in home


def test_coverage_warning_gone_when_two_people_can_act(client, board):
    with mock.patch("core.policy.thinly_covered_actions", return_value={}):
        client.force_login(board)
        page = client.get(reverse("my_items")).content.decode()
    assert 'id="coverage"' not in page


def test_past_term_seat_and_missing_rules_alerts_top_my_items(client, board):
    other = make_member(board=True)
    with system_access("test"):
        BoardSeat.objects.filter(person__user=other).update(term_end=days_ago(5))
    with mock.patch("community.rules.rules_needing_attention", return_value=["x", "y"]):
        client.force_login(board)
        page = client.get(reverse("my_items")).content.decode()
    with system_access("test"):
        name = Person.objects.get(user=other).display_name
    assert f"{name}&#x27;s board term has ended" in page
    # One setup banner holds the rules missing a citation (WT-004).
    assert "Finish community setup: " in page and "2 rules missing a value or citation" in page
    above = page.split('id="area-')[0]  # alerts, not rows in the lists below
    assert "board term has ended" in above and "rules missing a value" in above
    client.force_login(other)  # a dashboard to-do as well
    assert "board term has ended" in client.get(reverse("home")).content.decode()


def test_my_items_offers_record_checks_to_board_and_manager_only(client, board):
    for user, shown in (
        (board, True),
        (make_member(roles=[Role.MANAGER]), True),
        (make_member(roles=[Role.AUDITOR]), False),
    ):
        client.force_login(user)
        response = client.get(reverse("my_items"))
        if response.status_code != 200:
            assert not shown
            continue
        assert (reverse("record_payments") in response.content.decode()) is shown


def test_second_approvals_page_name(client, board):
    client.force_login(board)
    assert "<h1>Second approvals</h1>" in client.get(reverse("approvals")).content.decode()


# --- Preview as owner ---------------------------------------------------------------------


def test_preview_as_owner_wording(client, board):
    lot = LotFactory()
    make_member(lots=[lot])
    client.force_login(board)
    assert (
        "Preview as owner</button>"
        in client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    )
    client.post(reverse("owner_preview_start"), {"lot": lot.pk})
    page = client.get(reverse("home")).content.decode()
    assert "<strong>Preview as owner:</strong>" in page and "Homeowner view" not in page


# --- Dashboards ---------------------------------------------------------------------------


def test_sysadmin_home_is_the_system_status_summary(client, board):
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    page = client.get(reverse("home")).content.decode()
    assert 'id="system-summary"' in page and reverse("system_status") in page
    client.force_login(board)
    assert 'id="system-summary"' not in client.get(reverse("home")).content.decode()
    owner_admin = make_member(roles=[Role.SYSADMIN], lots=[LotFactory()])
    client.force_login(owner_admin)  # also an owner: the ordinary dashboard
    assert 'id="system-summary"' not in client.get(reverse("home")).content.decode()


def test_owner_dashboard_keeps_only_the_balance_tile(client, board):
    lot = LotFactory()
    owner = make_member(lots=[lot])
    client.force_login(owner)
    page = client.get(reverse("home")).content.decode()
    assert f'<div class="label">{lot.label}</div>' in page  # the balance tile
    for gone in ("Open votes", "waiting for your ballot", '<div class="hint">open</div>'):
        assert gone not in page, gone


def _charge(lot, actor):
    from ledger import services as ledger

    ledger.post_charge(
        lot=lot,
        kind="other_charge",
        amount_cents=4200,
        description="Key fob",
        posted_on=days_ago(3),
        actor=actor,
    )


def test_balance_todo_stays_visible_with_the_balance_tile(client, board):
    """The count tiles went, the balance to-do (which carries "Pay $X", from
    core.my_items) and the balance tile stay."""
    lot = LotFactory()
    owner = make_member(lots=[lot])
    _charge(lot, board)
    client.force_login(owner)
    page = client.get(reverse("home")).content.decode()
    todos = page.split('id="todos"')[1].split("</section>")[0]
    assert f"$42.00 due for {lot.label}" in todos
    assert f'<div class="label">{lot.label}</div>' in page


def test_record_checks_quick_action_on_the_dashboard(client, board):
    client.force_login(board)
    assert f'href="{reverse("record_payments")}"' in client.get(reverse("home")).content.decode()
    client.force_login(make_member(lots=[LotFactory()]))
    assert reverse("record_payments") not in client.get(reverse("home")).content.decode()


def test_people_page_has_the_access_review_panel(client, board):
    client.force_login(board)
    page = client.get(reverse("people")).content.decode()
    assert 'id="access-review-panel"' in page and reverse("access_review") in page
    assert "Not reviewed yet" in page
