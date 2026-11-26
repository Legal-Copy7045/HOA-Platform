"""Portal search (core.search): never shows what the person could not open.

For every provider: owner A searching for a word that is only in owner B's
record (or a board-only record) finds nothing; the record's own people find
it. Plus recusal, internal notes, tenants and view-only contacts, former
owners, board/manager results, feature switches, escaping and privacy.
"""

import logging
from datetime import timedelta
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from community import services as community
from community.models import Account, Person, Role
from community.tests.factories import LotFactory, UserFactory, days_ago, enroll_totp, make_member
from core import search
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("test"):
        return Person.objects.get(user=user)


def _account(lot):
    with system_access("test"):
        return Account.objects.get(lot=lot, closed_on__isnull=True)


def _contact_user(account, kind):
    user = UserFactory()
    person = community.record_person(display_name=f"Contact {kind}", actor=SYSTEM)
    with system_access("test"):
        Person.objects.filter(pk=person.pk).update(user=user)
    person.refresh_from_db()
    enroll_totp(user)
    community.add_account_contact(account, person=person, kind=kind, actor=SYSTEM)
    return user


def _found(user, q, area=None) -> list[search.Result]:
    groups = search.search(user, q)
    return [r for g in groups if area is None or g.area.key == area for r in g.results]


def _areas(user, q) -> set[str]:
    return {g.area.key for g in search.search(user, q)}


@pytest.fixture
def w():
    with (
        mock.patch("accounts.tasks.send_invitation.defer"),
        mock.patch("community.tasks.notify_board.defer"),
    ):
        lot_a = LotFactory(street_address="1 Alpha Way")
        lot_b = LotFactory(street_address="2 Bravo Way")
        lot_c = LotFactory(street_address="3 Charlie Way")
        owner_a = make_member(lots=[lot_a])
        owner_b = make_member(lots=[lot_b])
        board = make_member(board=True)
        manager = make_member(roles=[Role.MANAGER])
        reviewer = make_member(roles=[Role.ARC_REVIEWER])
        # Owns a lot affected by B's project: recused from it.
        recused = make_member(lots=[lot_c], roles=[Role.ARC_REVIEWER])
        with system_access("test"):
            Person.objects.filter(user=owner_b).update(display_name="Bertha Quillfeather")

        from comms import services as comms

        comms.publish_announcement(
            title="Board zebrafish memo", body="Confidential", audience="board", actor=board
        )
        comms.publish_announcement(
            title="Pool opening",
            body="The pool opens <script>alert(1)</script> soon",
            actor=board,
        )
        comms.create_event(
            title="Board quokka session",
            starts_at=timezone.now() + timedelta(days=3),
            audience="board",
            actor=board,
        )
        comms.create_event(
            title="Community quokkafest picnic",
            starts_at=timezone.now() + timedelta(days=4),
            actor=board,
        )
        comms.save_faq(question="Narwhal draft?", answer="Hidden", published=False, actor=board)
        comms.save_faq(question="Trash day?", answer="Tuesday walrus pickup", actor=board)

        from documents import services as documents
        from documents.tests.helpers import pdf

        documents.create_document(
            title="Lot B axolotl survey",
            category="lot",
            visibility="lot",
            lot=lot_b,
            file=pdf(),
            actor=board,
        )
        documents.create_document(
            title="Board axolotl counsel letter",
            category="other",
            visibility="board",
            file=pdf(),
            actor=board,
        )
        documents.create_document(
            title="Community axolotl newsletter",
            category="other",
            visibility="owners",
            file=pdf(),
            actor=board,
        )

        from governance import services as governance

        governance.create_meeting(
            title="Executive pangolin session",
            kind="executive",
            location="Clubhouse",
            starts_at=timezone.now() + timedelta(days=5),
            actor=board,
        )
        governance.create_meeting(
            title="Open pangolin meeting",
            kind="board",
            location="Clubhouse",
            starts_at=timezone.now() + timedelta(days=6),
            actor=board,
        )
        governance.record_resolution(
            kind="policy",
            title="Ocelot parking policy",
            text="No ocelots on the street",
            adopted_on=days_ago(3),
            actor=board,
        )
        governance.submit_records_request(description="Capybara audit papers", actor=owner_b)

        from arc import services as arc
        from arc.models import ArcRequest
        from comms import threads

        arc_b = arc.create_request(
            lot=lot_b, title="Lot B hottub", description="Rear marmoset patio", actor=owner_b
        )
        with system_access("test"):
            ArcRequest.objects.get(pk=arc_b.pk).affected_lots.add(lot_c)
        threads.post("arc", arc_b, body="Public tapir question", actor=board)
        threads.post("arc", arc_b, body="Internal gibbon deliberation", internal=True, actor=board)

        from compliance import services as compliance

        violation_b = compliance.report(
            lot=lot_b, category="parking", description="Lot B wombat boat", actor=board
        )
        compliance.VIOLATION.apply(violation_b, "send_notice", actor=board)
        threads.post(
            "violation", violation_b, body="Internal jackal note", internal=True, actor=board
        )
        compliance.report(
            lot=lot_b, category="parking", description="Unsent dingo draft", actor=board
        )

        from operations import services as operations
        from operations.models import ExpenseCategory

        request_b = operations.submit_request(
            lot=lot_b,
            request_type="maintenance",
            subject="Lot B gutter lemur",
            description="Water everywhere",
            actor=owner_b,
        )
        threads.post("request", request_b, body="Public heron reply", actor=board)
        threads.post(
            "request", request_b, body="Internal mongoose note", internal=True, actor=board
        )
        vendor = operations.save_vendor(name="Pond Pros iguana", actor=board)
        work_order = operations.create_work_order(title="Fix iguana pump", actor=board)
        threads.post("work_order", work_order, body="Iguana staff note", actor=board)
        operations.create_invoice(
            vendor=vendor,
            number="INV-IGUANA",
            invoice_date=timezone.localdate(),
            amount_cents=12345,
            category=ExpenseCategory.objects.create(code="5100", name="Pond"),
            actor=board,
        )
    return {
        "lot_a": lot_a,
        "lot_b": lot_b,
        "lot_c": lot_c,
        "owner_a": owner_a,
        "owner_b": owner_b,
        "board": board,
        "manager": manager,
        "reviewer": reviewer,
        "recused": recused,
        "arc_b": arc_b,
        "violation_b": violation_b,
        "request_b": request_b,
    }


# --- Every provider: owner A never finds owner B's or the board's records -------------

OWNER_B_OR_BOARD_ONLY = [
    ("announcements", "zebrafish"),
    ("calendar", "Board quokka"),
    ("faq", "narwhal"),
    ("documents", "Lot B axolotl"),
    ("documents", "counsel"),
    ("meetings", "Executive pangolin"),
    ("records_requests", "capybara"),
    ("rules", "Monthly assessment"),
    ("lots", "Bravo"),
    ("people", "Quillfeather"),
    ("directory", "Quillfeather"),
    ("arc", "marmoset"),
    ("arc", "tapir"),
    ("arc", "gibbon"),
    ("violations", "wombat"),
    ("violations", "jackal"),
    ("requests", "lemur"),
    ("requests", "heron"),
    ("requests", "mongoose"),
    ("work_orders", "iguana"),
    ("vendors", "iguana"),
    ("invoices", "iguana"),
]


# Community-wide by design (every member sees every row): checked below for
# who may search them at all (former owners may not).
COMMUNITY_WIDE = {"resolutions"}


def test_every_provider_is_covered_here():
    covered = {area for area, _ in OWNER_B_OR_BOARD_ONLY} | COMMUNITY_WIDE
    assert covered == set(search.load_providers())


@pytest.mark.parametrize("area,word", OWNER_B_OR_BOARD_ONLY)
def test_owner_a_finds_nothing_of_owner_b_or_the_board(w, area, word):
    assert _found(w["owner_a"], word, area) == []


@pytest.mark.parametrize("area,word", OWNER_B_OR_BOARD_ONLY)
def test_owner_a_search_page_shows_none_of_it(client, w, area, word):
    client.force_login(w["owner_a"])
    page = client.get(reverse("search"), {"q": word}).content.decode()
    assert f'id="area-{area}"' not in page


def test_owner_b_finds_their_own_records(w):
    b = w["owner_b"]
    assert _found(b, "Lot B axolotl", "documents")
    assert _found(b, "capybara", "records_requests")
    assert [r.url for r in _found(b, "marmoset", "arc")] == [
        reverse("arc_detail", args=[w["arc_b"].pk])
    ]
    assert _found(b, "tapir", "arc")[0].url.endswith("#thread")
    assert _found(b, "wombat", "violations")
    assert _found(b, "lemur", "requests") and _found(b, "heron", "requests")


def test_owner_b_never_finds_internal_notes(w):
    b = w["owner_b"]
    for area, word in (("arc", "gibbon"), ("violations", "jackal"), ("requests", "mongoose")):
        assert _found(b, word, area) == [], word


def test_owner_does_not_find_a_violation_before_notice(w):
    assert _found(w["owner_b"], "dingo") == []
    assert _found(w["board"], "dingo", "violations")


def test_community_records_are_found_by_every_owner(w):
    a = w["owner_a"]
    assert [r.title for r in _found(a, "axolotl", "documents")] == ["Community axolotl newsletter"]
    assert [r.title for r in _found(a, "pangolin", "meetings")] == ["Open pangolin meeting"]
    assert [r.title for r in _found(a, "quokka", "calendar")] == ["Community quokkafest picnic"]
    assert _found(a, "walrus", "faq")
    assert _found(a, "ocelot", "resolutions")
    assert _found(a, "pool", "announcements")


# --- Board and manager ----------------------------------------------------------------------


def test_board_finds_board_side_records(w):
    found = _areas(w["board"], "iguana")
    assert {"work_orders", "vendors", "invoices"} <= found
    for area, word in OWNER_B_OR_BOARD_ONLY:
        if area == "directory":
            continue  # nobody opted in yet
        assert _found(w["board"], word, area), (area, word)


def test_board_finds_internal_notes(w):
    board = w["board"]
    assert _found(board, "gibbon", "arc")[0].detail == "Internal note"
    assert _found(board, "jackal", "violations")
    assert _found(board, "mongoose", "requests")


def test_manager_finds_board_side_records_but_not_people(w):
    m = w["manager"]
    assert {"work_orders", "vendors", "invoices"} <= _areas(m, "iguana")
    assert _found(m, "Bravo", "lots")
    assert _found(m, "Quillfeather", "lots")  # owner names on the lots list
    assert _found(m, "Monthly assessment", "rules")
    assert _found(m, "Quillfeather", "people") == []  # the People page is board only


def test_lots_are_found_by_number(w):
    assert [r.title for r in _found(w["board"], str(w["lot_b"].number), "lots")] == [
        w["lot_b"].label
    ]


# --- ARC recusal ----------------------------------------------------------------------------


def test_recused_reviewer_does_not_find_internal_notes(w):
    assert _found(w["recused"], "marmoset", "arc")  # the request itself is on their list
    assert _found(w["recused"], "gibbon", "arc") == []
    assert _found(w["reviewer"], "gibbon", "arc")


# --- Account contacts -----------------------------------------------------------------------


def test_view_only_contact_sees_the_account_but_no_internal_notes(w):
    viewer = _contact_user(_account(w["lot_b"]), "view_only")
    assert _found(viewer, "lemur", "requests")
    assert _found(viewer, "heron", "requests")
    assert _found(viewer, "mongoose", "requests") == []
    assert _found(viewer, "gibbon", "arc") == []


def test_tenant_finds_only_their_own_requests(w):
    from operations import services as operations

    tenant = _contact_user(_account(w["lot_b"]), "tenant")
    operations.submit_request(
        lot=w["lot_b"],
        request_type="maintenance",
        subject="Tenant toucan faucet",
        description="Drips",
        actor=tenant,
    )
    assert _found(tenant, "toucan", "requests")
    assert _found(tenant, "lemur") == []
    assert _found(tenant, "heron") == []
    assert _found(tenant, "Lot B axolotl") == []
    assert _found(tenant, "wombat") == []
    assert _found(tenant, "marmoset") == []
    # Community pages are open to tenants.
    assert _found(tenant, "pool", "announcements")


# --- Former owners -------------------------------------------------------------------------


def test_former_owner_finds_only_their_own_era_on_my_history(w):
    from arc import services as arc
    from arc.models import ArcRequest

    seller = w["owner_b"]
    with system_access("test"):
        ArcRequest.objects.filter(pk=w["arc_b"].pk).update(
            created_at=timezone.now() - timedelta(days=5)
        )
    buyer = make_member()
    community.transfer_lot(
        lot=w["lot_b"], new_owners=[_person(buyer)], transfer_date=days_ago(0), actor=SYSTEM
    )
    arc.create_request(lot=w["lot_b"], title="Buyer flamingo deck", description="x", actor=buyer)
    found = _found(seller, "marmoset")
    assert [r.url for r in found] == [reverse("my_history")]
    assert _found(seller, "tapir") == []  # threads are not on My history
    assert _found(seller, "flamingo") == []
    assert _found(seller, "pool") == []  # no community pages after the sale
    assert _found(seller, "ocelot") == []
    assert _found(seller, "Lot B axolotl") == []
    # The buyer does not see the seller's request.
    assert _found(buyer, "marmoset") == []
    assert _found(buyer, "flamingo", "arc")


# --- Directory -----------------------------------------------------------------------------


def test_directory_finds_only_opted_in_owners_by_shared_fields(w):
    community.update_my_profile(user=w["owner_b"], in_directory=True, directory_show_email=False)
    found = _found(w["owner_a"], "Quillfeather", "directory")
    assert [r.title for r in found] == ["Bertha Quillfeather"]
    assert found[0].url == reverse("directory")
    assert _found(w["owner_a"], w["owner_b"].email, "directory") == []
    # Nobody else is in the directory, so their names are not found.
    assert _found(w["owner_b"], "Test Person", "directory") == []


# --- Feature switches ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "feature,area,word",
    [
        ("announcements", "announcements", "zebrafish"),
        ("calendar", "calendar", "quokka"),
        ("faq", "faq", "narwhal"),
        ("violations", "violations", "wombat"),
        ("work_orders", "work_orders", "iguana"),
        ("work_orders", "vendors", "iguana"),
        ("records_requests", "records_requests", "capybara"),
        ("directory", "directory", "Quillfeather"),
    ],
)
def test_switched_off_feature_hides_its_area(client, w, feature, area, word):
    from core.models import FeatureSetting

    community.update_my_profile(user=w["owner_b"], in_directory=True)
    assert area in _areas(w["board"], word)
    FeatureSetting.objects.create(key=feature, enabled=False)
    assert area not in _areas(w["board"], word)
    client.force_login(w["board"])
    page = client.get(reverse("search"), {"q": word}).content.decode()
    assert f'id="area-{area}"' not in page


def test_every_area_is_gated_by_a_page_that_exists(w):
    for area in search.load_providers().values():
        reverse(area.url_name)


# --- The page ------------------------------------------------------------------------------


def test_search_needs_sign_in(client):
    response = client.get(reverse("search"), {"q": "pool"})
    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]


def test_results_are_grouped_with_counts_and_never_cached(client, w):
    client.force_login(w["board"])
    response = client.get(reverse("search"), {"q": "iguana"})
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    page = response.content.decode()
    assert 'id="area-work_orders"' in page and 'id="area-invoices"' in page
    assert "<h1>Search</h1>" in page
    assert "<mark>iguana</mark>" in page.lower() or "<mark>Iguana</mark>" in page


def test_snippets_are_escaped_and_highlighted(client, w):
    client.force_login(w["owner_a"])
    page = client.get(reverse("search"), {"q": "pool"}).content.decode()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<mark>pool</mark>" in page and "<mark>Pool</mark>" in page


def test_query_with_markup_is_escaped(client, w):
    client.force_login(w["owner_a"])
    page = client.get(reverse("search"), {"q": "<b>pool"}).content.decode()
    assert "<b>pool" not in page
    assert "&lt;b&gt;pool" in page


def test_empty_short_and_long_queries(client, w):
    client.force_login(w["owner_a"])
    assert b"Type a word to search" in client.get(reverse("search")).content
    short = client.get(reverse("search"), {"q": "p"}).content
    assert b"at least 2 characters" in short
    long = client.get(reverse("search"), {"q": "x" * 101}).content
    assert b"too long" in long
    none = client.get(reverse("search"), {"q": "zzqqxx"}).content
    assert b"Nothing matches that search" in none
    assert search.search(w["board"], "x" * 101) == []
    assert search.search(w["board"], "i") == []


def test_at_most_ten_results_per_area_newest_first(client, w):
    from comms import services as comms

    for i in range(12):
        comms.publish_announcement(title=f"Heron news {i:02d}", body="b", actor=w["board"])
    groups = {g.area.key: g for g in search.search(w["owner_a"], "Heron news")}
    group = groups["announcements"]
    assert len(group.results) == search.PER_AREA and group.more
    assert group.results[0].title == "Heron news 11"
    client.force_login(w["owner_a"])
    assert b"10+" in client.get(reverse("search"), {"q": "Heron news"}).content


def test_header_has_a_search_box(client, w):
    client.force_login(w["owner_a"])
    page = client.get(reverse("announcements")).content.decode()
    assert f'action="{reverse("search")}"' in page and 'role="search"' in page


def test_a_login_without_any_role_finds_nothing(w):
    user = UserFactory()
    assert search.search(user, "pool") == []


# --- Privacy -------------------------------------------------------------------------------


def test_query_is_never_audited_or_logged(client, w, caplog):
    from audit.models import AuditEvent

    client.force_login(w["board"])
    before = AuditEvent.objects.count()
    with caplog.at_level(logging.DEBUG):
        client.get(reverse("search"), {"q": "Quillfeather"})
        client.get(reverse("search"), {"q": "Q"})
    assert AuditEvent.objects.count() == before
    assert "Quillfeather" not in caplog.text


def test_sysadmin_search_audit_carries_no_query(client, w):
    from audit.models import AuditEvent

    sysadmin = make_member(roles=[Role.SYSADMIN])
    client.force_login(sysadmin)
    response = client.get(reverse("search"), {"q": "Quillfeather"})
    assert response.status_code == 200
    assert b'id="area-' not in response.content  # operations only, no owner data
    for event in AuditEvent.objects.all():
        assert "Quillfeather" not in str(event.metadata)
