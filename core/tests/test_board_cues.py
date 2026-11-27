"""Board cues, no workspace switch (D64), and the second walkthrough's
structural fixes (WT-004, 005, 007, 008, 009, 010, 011, 014, 036, 038, 040).

One login and one menu: the cues only say when someone acts for the
association; permissions are unchanged."""

import re

import pytest
from django.core import mail
from django.urls import reverse

from community import services as community
from community.models import Account, Role
from community.tests.factories import LotFactory, days_ago, make_member
from core import navigation
from core.policy import SYSTEM
from core.scoping import system_access
from core.tests.test_nav_streamline import role_users

pytestmark = pytest.mark.django_db

BADGE = "data-board-badge"


@pytest.fixture
def board():
    return make_member(board=True, lots=[LotFactory()])


def _entries(response):
    return [(s, i) for s in response.context["nav"]["sections"] for i in s["items"]]


# --- 1. The badge and strip come from the menu, for every role ------------------------------


def test_board_badge_only_on_work_section_pages_for_every_role(client, board):
    """Each role's menu entries: a page in Association, Maintenance, Finance
    or Admin carries the badge; My home, Community and the dashboard never
    do. Owners have no work sections, so none of their pages does."""
    wrong = []
    for role, user in role_users(board).items():
        client.force_login(user)
        home = client.get(reverse("home"))
        assert BADGE not in home.content.decode(), role
        for section, item in _entries(home):
            page = client.get(item["url"], follow=True)
            if page.status_code != 200:
                continue
            has = BADGE in page.content.decode()
            if has != section["work"]:
                wrong.append((role, item["label"], has))
    assert not wrong, wrong


def test_badge_on_board_detail_pages_and_says_who_is_acting(client, board):
    client.force_login(board)
    page = client.get(reverse("invoices")).content.decode()
    assert 'class="board-badge" data-board-badge>Board</span>' in page
    assert "You're acting for the association on this page." in page
    assert 'class="content board-page' in page
    # Staff not on the board read "Staff".
    client.force_login(make_member(roles=[Role.MANAGER]))
    page = client.get(reverse("invoices")).content.decode()
    assert "data-board-badge>Staff</span>" in page


def test_owner_cannot_reach_board_pages_and_sees_no_cue(client):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    for name in ("invoices", "ledger_home", "settings_hub", "finance_more", "people"):
        assert client.get(reverse(name)).status_code in (403, 404), name
    page = client.get(reverse("my_account")).content.decode()
    assert BADGE not in page and "nav-work" not in page


# --- 2. One "Board" heading over the work sections ------------------------------------------


def test_work_sections_sit_under_one_board_heading(client, board):
    client.force_login(board)
    response = client.get(reverse("home"))
    nav = response.context["nav"]
    assert [s["title"] for s in nav["work_sections"]] == [
        "Association",
        "Maintenance",
        "Finance",
        "Admin",
    ]
    assert [s["title"] for s in nav["personal_sections"]] == ["", "My home", "Community"]
    html = response.content.decode()
    heading = '<div class="nav-work-heading" id="nav-work-h">Board</div>'
    assert heading in html
    work = html.split(heading)[1].split("</nav>")[0]
    for title in ("Association", "Maintenance", "Finance", "Admin"):
        assert f"<span>{title}</span>" in work
    assert "<span>My home</span>" not in work
    client.force_login(make_member(roles=[Role.AUDITOR]))
    assert '<div class="nav-work-heading" id="nav-work-h">Staff</div>' in (
        client.get(reverse("home")).content.decode()
    )


def test_board_colour_has_light_and_dark_values():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "static/css/site.css").read_text()
    assert "--board: #5b3fa0" in css and ".nav-work-heading" in css
    dark = css.split("/* ---------- Board cues (D64)")[1]
    assert "@media (prefers-color-scheme: dark)" in dark and "--board-bg: #2e2350" in dark


# --- 3. Decisions say who is acting, and own-home decisions say so first --------------------


def _invoice(manager):
    from operations import services as ops
    from operations.models import ExpenseCategory, Vendor

    inv = ops.create_invoice(
        vendor=Vendor.objects.create(name="Pond Pros"),
        number="PP-7",
        invoice_date="2026-11-01",
        amount_cents=100,
        category=ExpenseCategory.objects.create(code="1", name="c"),
        actor=manager,
    )
    ops.INVOICE.apply(inv, "submit", actor=manager)
    return inv


def test_invoice_approval_is_for_the_board(client, board):
    inv = _invoice(make_member(roles=[Role.MANAGER]))
    client.force_login(board)
    page = client.get(reverse("invoice_detail", args=[inv.pk])).content.decode()
    assert "Approve for the board</button>" in page
    assert "Reject for the board</button>" in page
    assert (
        'data-confirm="Approve this invoice for payment? You&#x27;re acting for the association."'
        in page
    )


def test_arc_decision_is_for_the_board(client, board):
    from arc import services as arc

    lot = LotFactory()
    owner = make_member(lots=[lot])
    manager = make_member(roles=[Role.MANAGER])
    req = arc.create_request(lot=lot, title="Fence", description="4 ft", actor=owner)
    arc.ARC.apply(req, "submit", actor=owner)
    arc.ARC.apply(req, "accept", actor=manager)
    # ARC reviewers decide; one on the board reads "for the board".
    reviewer = make_member(board=True, roles=[Role.ARC_REVIEWER])
    client.force_login(reviewer)
    page = client.get(reverse("arc_detail", args=[req.pk])).content.decode()
    assert "Record decision for the board</button>" in page
    assert "You&#x27;re acting for the association." in page
    client.force_login(make_member(roles=[Role.ARC_REVIEWER]))
    page = client.get(reverse("arc_detail", args=[req.pk])).content.decode()
    assert "Record decision</button>" in page and "You&#x27;re acting for the" in page


def test_workflow_decisions_use_the_same_words(client, board):
    from compliance import services as compliance

    lot = LotFactory()
    make_member(lots=[lot])
    v = compliance.report(lot=lot, category="trash", description="Bins", actor=board)
    compliance.VIOLATION.apply(v, "send_notice", actor=board)
    client.force_login(board)
    page = client.get(reverse("violation_detail", args=[v.pk])).content.decode()
    assert "Mark cured for the board</button>" in page
    confirms = re.findall(r'data-confirm="([^"]*)"', page)
    assert any(c.endswith("You&#x27;re acting for the association.") for c in confirms)
    # A step that is not a decision keeps its plain label.
    from core import workflow

    assert not workflow.is_decision("send_notice") and workflow.is_decision("approve")
    assert workflow.for_board("Approve", make_member(roles=[Role.MANAGER])) == "Approve"


def test_account_change_about_ones_own_request_says_so_first(client, board):
    other = make_member(board=True)
    with system_access("t"):
        lot = board.person.ownerships.first().lot
        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    change = community.request_account_change(
        account, action="add", name="Spouse", kind="authorized", no_login=True, actor=board
    )
    client.force_login(board)
    page = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert 'data-testid="own-home-note"' in page and "another board member must decide" in page
    assert reverse("account_change_decide", args=[change.pk, "approve"]) not in page
    # The service still refuses it.
    with pytest.raises(community.CommunityError):
        community.decide_account_change(change, approve=True, actor=board)
    client.force_login(other)
    page = client.get(reverse("lot_detail", args=[lot.pk])).content.decode()
    assert "Approve for the board</button>" in page and 'data-testid="own-home-note"' not in page


def test_disbursement_refund_to_oneself_says_so_first(client, board):
    from ledger import disbursements as disb
    from ledger import services as ledger

    with system_access("t"):
        lot = board.person.ownerships.first().lot
        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    ledger.record_payment(
        lot=lot, amount_cents=900, received_on=days_ago(3), reference="5", actor=SYSTEM
    )
    batch = disb.propose_refund(
        account=account, amount_cents=900, actor=make_member(roles=[Role.MANAGER])
    )
    client.force_login(board)
    page = client.get(reverse("disbursement_detail", args=[batch.pk])).content.decode()
    assert "This refund is to you." in page and 'data-testid="own-home-note"' in page
    assert reverse("disbursement_action", args=[batch.pk, "approve"]) not in page
    client.force_login(make_member(board=True))
    page = client.get(reverse("disbursement_detail", args=[batch.pk])).content.decode()
    assert "Approve for the board" in page and "You&#x27;re acting for the association." in page


# --- 4. The dashboard in two parts ----------------------------------------------------------


def test_board_member_with_a_home_sees_board_work_then_your_home(client, board):
    _invoice(make_member(roles=[Role.MANAGER]))
    client.force_login(board)
    html = client.get(reverse("home")).content.decode()
    assert 'id="part-board"' in html and 'id="part-home"' in html
    assert html.index('id="part-board"') < html.index('id="part-home"')
    board_part = html.split('id="part-board"')[1].split('id="part-home"')[0]
    assert "Invoice PP-7" in board_part and "Record payments received" in board_part
    assert 'id="todos"' not in html  # no third list


def test_your_home_first_when_no_board_work_waits(rf, board):
    from core import dashboard

    request = rf.get("/")
    request.user = board
    request._my_items = []
    parts = dashboard.build(board, request)["parts"]
    assert [p["key"] for p in parts] == ["home", "board"]


def test_owners_and_staff_only_keep_one_list(client):
    for user in (make_member(lots=[LotFactory()]), make_member(roles=[Role.MANAGER])):
        client.force_login(user)
        html = client.get(reverse("home")).content.decode()
        assert 'id="part-board"' not in html and 'id="todos"' in html


def test_items_say_whose_work_they_are():
    from core.my_items import BOARD_SIDE, HOME, Item

    assert Item("My home", "t", "d", "/").side == HOME
    assert Item("Sign-in and security", "t", "d", "/").side == HOME
    assert Item("Invoices", "t", "d", "/").side == BOARD_SIDE
    assert Item("Voting", "t", "d", "/", side=HOME).side == HOME


# --- 5. "[Board] " email subjects -----------------------------------------------------------


def test_board_notices_start_with_board_and_personal_mail_does_not(settings):
    from comms import notify
    from community.tasks import notify_board

    assert notify.board_subject(f"[{settings.SITE_NAME}] Compliance reminders") == (
        "[Board] Compliance reminders"
    )
    assert notify.board_subject("[Board] x") == "[Board] x"
    make_member(board=True)
    mail.outbox.clear()
    notify_board("account_change_requested")
    assert mail.outbox and all(
        m.subject == "[Board] Account change waiting for approval" for m in mail.outbox
    )


def test_send_now_and_update_take_the_board_flag(django_capture_on_commit_callbacks):
    from comms import notify

    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        notify.send_now(["a@example.com"], subject="Board thing", body="x", board=True)
        notify.send_now(["b@example.com"], subject="Your statement", body="x")
        notify.send_update(["c@example.com"], subject="Staff", body="x", link="/", board=True)
    subjects = {m.to[0]: m.subject for m in mail.outbox}
    assert subjects == {
        "a@example.com": "[Board] Board thing",
        "b@example.com": "Your statement",
        "c@example.com": "[Board] Staff",
    }


def test_thread_updates_are_board_mail_for_staff_only(django_capture_on_commit_callbacks):
    from comms import threads

    lot = LotFactory()
    owner = make_member(lots=[lot])
    staff = make_member(board=True)
    from operations import services as ops

    req = ops.submit_request(
        lot=lot, request_type="general", subject="Gate", description="Squeaks", actor=owner
    )
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        threads.notify_staff("request", req)
        threads.notify_owner_side("request", req)
    by = {m.to[0]: m.subject for m in mail.outbox}
    assert by[staff.email].startswith("[Board] Update on request #")
    assert by[owner.email].endswith(f"Update on your request #{req.pk}")
    assert not by[owner.email].startswith("[Board]")


# --- Structural fixes -----------------------------------------------------------------------


def test_settings_breadcrumb_skips_a_one_page_hub(client, board):
    """WT-009: the auditor's Settings would be Community settings alone."""
    client.force_login(make_member(roles=[Role.AUDITOR]))
    html = client.get(reverse("community_settings")).content.decode()
    assert '<nav class="crumbs" aria-label="Breadcrumb">Community settings</nav>' in html
    client.force_login(board)
    html = client.get(reverse("community_settings")).content.decode()
    assert f'<a href="{reverse("settings_hub")}">Settings</a> / Community settings' in html


def test_manager_reaches_paper_mail_settings_from_paper_mail(client):
    manager = make_member(roles=[Role.MANAGER])
    client.force_login(manager)
    response = client.get(reverse("paper_mail_settings"))
    current = [i["label"] for _s, i in _entries(response) if i["current"]]
    assert current == ["Paper mail"]
    item = next(i for _s, i in _entries(response) if i["label"] == "Community settings")
    assert item["url"] == reverse("community_settings")


def test_more_finance_hub(client, board):
    client.force_login(board)
    response = client.get(reverse("finance_more"))
    html = response.content.decode()
    assert "<h1>More finance</h1>" in html
    for name in ("reserve_plan", "disbursements", "resales"):
        assert f'href="{reverse(name)}"' in html, name
    current = [i["label"] for _s, i in _entries(client.get(reverse("resales")))]
    assert "Resale certificates" not in current and "More finance" in current
    marked = [i["label"] for _s, i in _entries(client.get(reverse("resales"))) if i["current"]]
    assert marked == ["More finance"]


def test_board_reads_server_tasks_on_sysadmin_activity(client, board):
    from core import host_tasks

    host_tasks.request_task(make_member(roles=[Role.SYSADMIN]), "backup")
    client.force_login(board)
    html = client.get(reverse("sysadmin_activity")).content.decode()
    assert 'id="server-tasks"' in html
    assert "Take a backup" in html.split('id="server-tasks"')[1].split("</section>")[0]
    response = client.get(reverse("server_tasks"))
    assert [i["label"] for _s, i in _entries(response) if i["current"]] == ["Sysadmin activity"]
    page = response.content.decode()
    assert "D35" not in page and "D36" not in page and ".sh" not in page
    client.force_login(make_member(roles=[Role.AUDITOR]))
    assert 'id="server-tasks"' not in client.get(reverse("sysadmin_activity")).content.decode()


def test_committees_once_on_community_info(client, board):
    client.force_login(board)
    html = client.get(reverse("community_info")).content.decode()
    assert f'href="{reverse("committees")}"' not in html
    page = client.get(reverse("board_and_committees")).content.decode()
    assert f'href="{reverse("committees")}#new-committee"' in page
    response = client.get(reverse("committees"))
    assert [i["label"] for _s, i in _entries(response) if i["current"]] == ["Community info"]
    client.force_login(make_member(lots=[LotFactory()]))
    page = client.get(reverse("board_and_committees")).content.decode()
    assert "New committee" not in page


def test_account_recovery_rules_step_name(client, board):
    from community import setup

    assert setup.BY_KEY["signin"].title == "Account recovery rules"
    assert "(D58)" not in setup.BY_KEY["signin"].intro
    client.force_login(board)
    assert "<h1>Sign-in and security</h1>" in client.get(reverse("mfa_index")).content.decode()


def test_my_items_groups_are_one_line_of_links(client, board):
    _invoice(make_member(roles=[Role.MANAGER]))
    client.force_login(board)
    html = client.get(reverse("my_items")).content.decode()
    assert 'data-testid="my-items-summary"' in html
    assert 'class="stat' not in html
    assert "Record payments received" in html


def test_disbursement_checks_to_write_have_write_checks(board):
    from core.my_items import Item

    item = Item("Finance", "2 checks to write", "x", "/d/", action_label="Write checks")
    assert item.action_label == "Write checks"
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "ledger/disbursements.py").read_text()
    assert 'action_label="Write checks"' in source


def test_navigation_helper_marks_work_sections(rf, board):
    request = rf.get(reverse("invoices"))
    request.user = board
    data = navigation.build(request)
    assert data["board_page"] is True and data["work_label"] == "Board"
    request = rf.get(reverse("my_account"))
    request.user = board
    assert navigation.build(request)["board_page"] is False
    request = rf.get("/")
    request.user = make_member(roles=[Role.AUDITOR])
    assert navigation.work_label(navigation.policy.roles_for(request.user).groups) == "Staff"


def test_retention_still_reachable_from_the_privacy_step(client, board):
    client.force_login(board)
    html = client.get(reverse("setup_step", args=["privacy"])).content.decode()
    assert f'href="{reverse("retention")}"' in html
    response = client.get(reverse("retention"))
    assert [i["label"] for _s, i in _entries(response) if i["current"]] == ["Settings"]


def test_acting_confirm_wording():
    from core import workflow

    assert workflow.acting_confirm("Approve?") == "Approve? You're acting for the association."
    assert workflow.acting_confirm() == workflow.ACTING
