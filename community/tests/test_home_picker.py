"""The home picker (D96): homes picked by street address with a type-ahead.

The suggestions endpoint (sign-in, who gets suggestions, caps, matching),
resolving typed text without JavaScript (exact address, lot number fallback,
ambiguous and unknown text with close matches), and every form that uses it
still saving the right home."""

import datetime
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from community import home_picker
from community.models import Lot, Ownership, Person, Role
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db

ROOT = Path(settings.BASE_DIR)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def w():
    oak = LotFactory(number=12, street_address="165 Imaginary Orchard Way")
    elm = LotFactory(number=14, street_address="167 Imaginary Orchard Way")
    birch = LotFactory(number=30, street_address="9 Birch Court\nOak Creek Farms PA")
    far = LotFactory(number=41, street_address="301 Meadow Lane")
    return {
        "oak": oak,
        "elm": elm,
        "birch": birch,
        "far": far,
        "owner": make_member(lots=[oak]),
        "owner_far": make_member(lots=[far]),
        "board": make_member(board=True),
        "board2": make_member(board=True),
        "manager": make_member(roles=[Role.MANAGER]),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
        "nobody": make_member(),
    }


def _suggest(client, q):
    return client.get(reverse("home_suggest"), {"q": q})


# --- The endpoint ------------------------------------------------------------------------


def test_anonymous_is_sent_to_sign_in(client, w):
    response = _suggest(client, "Orchard")
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


def test_post_is_refused(client, w):
    client.force_login(w["board"])
    assert client.post(reverse("home_suggest"), {"q": "Orchard"}).status_code == 405


@pytest.mark.parametrize("who", ["owner", "owner_far", "board", "manager"])
def test_every_member_sees_every_home_by_address(client, w, who):
    """Owner's decision (D96): homes and owners are public in the community."""
    client.force_login(w[who])
    results = _suggest(client, "imaginary").json()["results"]
    assert [r["label"] for r in results] == [
        "165 Imaginary Orchard Way",
        "167 Imaginary Orchard Way",
    ]
    assert results[0]["owners"]  # the current owner's name, as on the deed
    assert results[1]["owners"] == ""  # nobody on the deed of lot 14


def test_suggestions_carry_no_contact_details_or_money(client, w):
    client.force_login(w["owner_far"])
    response = _suggest(client, "165")
    body = response.content.decode()
    assert "@" not in body and "$" not in body and "balance" not in body.lower()
    with system_access("t"):
        person = Person.objects.get(user=w["owner"])
    assert response.json()["results"][0]["owners"] == person.display_name
    assert person.email not in body


@pytest.mark.parametrize("who", ["sysadmin", "nobody"])
def test_no_community_role_gets_nothing(client, w, who):
    client.force_login(w[who])
    response = _suggest(client, "Orchard")
    assert response.status_code == 200 and response.json() == {"results": []}


def test_a_former_owner_gets_nothing(client, w):
    from community import services

    seller = make_member(lots=[w["birch"]])
    with system_access("t"):
        ownership = Ownership.objects.get(person__user=seller)
    services.end_ownership(ownership, end_date=datetime.date.today(), actor=SYSTEM)
    client.force_login(seller)
    assert _suggest(client, "Birch").json() == {"results": []}


def test_minimum_two_characters_and_ten_results(client, w):
    client.force_login(w["board"])
    assert _suggest(client, "1").json()["results"] == []
    assert _suggest(client, "").json()["results"] == []
    for n in range(15):
        LotFactory(number=100 + n, street_address=f"{n + 1} Crowded Street")
    assert len(_suggest(client, "crowded").json()["results"]) == home_picker.MAX_RESULTS


def test_rate_limited_after_the_cap(client, w):
    client.force_login(w["owner"])
    with mock.patch.object(home_picker, "RATE_LIMIT", 3):
        for _ in range(3):
            assert _suggest(client, "Orchard").status_code == 200
        response = _suggest(client, "Orchard")
    assert response.status_code == 429 and response.json() == {"results": []}


def test_the_query_is_never_audited(client, w):
    from audit.models import AuditEvent

    client.force_login(w["board"])
    before = AuditEvent.objects.count()
    _suggest(client, "Orchard")
    assert AuditEvent.objects.count() == before


# --- Matching -------------------------------------------------------------------------------


def test_partial_address_case_insensitive(client, w):
    client.force_login(w["board"])
    labels = [r["label"] for r in _suggest(client, "BIRCH co").json()["results"]]
    assert labels == ["9 Birch Court"]  # the address's first line only
    labels = [r["label"] for r in _suggest(client, "meadow").json()["results"]]
    assert labels == ["301 Meadow Lane"]


def test_lot_number_is_a_fallback_and_ranks_first(client, w):
    client.force_login(w["board"])
    results = _suggest(client, "30").json()["results"]
    assert results[0]["label"] == "9 Birch Court"  # lot 30
    results = _suggest(client, "Lot 41").json()["results"]
    assert [r["label"] for r in results] == ["301 Meadow Lane"]


def test_owner_name_matches(client, w):
    with system_access("t"):
        name = Person.objects.get(user=w["owner"]).display_name
    client.force_login(w["board"])
    labels = [r["label"] for r in _suggest(client, name.lower()).json()["results"]]
    assert labels == ["165 Imaginary Orchard Way"]


# --- Resolving typed text (no JavaScript) --------------------------------------------------


def test_resolve_exact_address_lot_number_and_blank(w):
    lots = list(Lot.objects.unscoped("t"))
    assert home_picker.resolve("165 imaginary  orchard WAY", lots) == w["oak"]
    assert home_picker.resolve("14", lots) == w["elm"]
    assert home_picker.resolve("Lot 30", lots) == w["birch"]
    assert home_picker.resolve("9 Birch Court (Lot 30)", lots) == w["birch"]
    assert home_picker.resolve("  ", lots) is None


def test_resolve_partial_lists_close_matches(w):
    lots = list(Lot.objects.unscoped("t"))
    with pytest.raises(home_picker.HomeNotFound) as exc:
        home_picker.resolve("Imaginary", lots)
    assert "165 Imaginary Orchard Way" in exc.value.message
    assert "167 Imaginary Orchard Way" in exc.value.message
    assert exc.value.message.startswith('No home matches "Imaginary". Did you mean:')


def test_resolve_misspelling_suggests_the_nearest(w):
    lots = list(Lot.objects.unscoped("t"))
    with pytest.raises(home_picker.HomeNotFound) as exc:
        home_picker.resolve("301 Medow Lane", lots)
    assert "301 Meadow Lane" in exc.value.message


def test_resolve_two_homes_with_one_address_is_ambiguous(w):
    twin = LotFactory(number=42, street_address="301 Meadow Lane")
    lots = list(Lot.objects.unscoped("t"))
    with pytest.raises(home_picker.HomeNotFound) as exc:
        home_picker.resolve("301 Meadow Lane", lots)
    assert exc.value.ambiguous and "(Lot 41)" in exc.value.message
    assert home_picker.resolve("301 Meadow Lane (Lot 42)", lots) == twin
    assert home_picker.picker_value(twin) == "301 Meadow Lane (Lot 42)"


def test_resolve_only_within_the_given_homes(w):
    with pytest.raises(home_picker.HomeNotFound):
        home_picker.resolve("301 Meadow Lane", [w["oak"], w["elm"]])


def test_resolve_many_addresses_and_old_numbers(w):
    lots = list(Lot.objects.unscoped("t"))
    found, errors = home_picker.resolve_many("165 Imaginary Orchard Way; 9 birch court", lots)
    assert found == [w["oak"], w["birch"]] and errors == []
    found, errors = home_picker.resolve_many("12 14", lots)  # as typed before D96
    assert found == [w["oak"], w["elm"]]
    found, errors = home_picker.resolve_many("12; Nowhere Road; 12", lots)
    assert found == [w["oak"]] and len(errors) == 1 and "Nowhere Road" in errors[0]


def test_home_field_errors_and_shows_the_address(w):
    from django import forms

    class F(forms.Form):
        lot = home_picker.HomeField(label="Home")

    form = F(data={"lot": "Imaginary"})
    form.fields["lot"].queryset = Lot.objects.unscoped("t")
    assert not form.is_valid()
    assert "Did you mean" in form.errors["lot"][0]
    html = str(form["lot"])
    assert "data-home-picker" in html and 'value="Imaginary"' in html
    form = F(initial={"lot": w["oak"].pk})
    assert 'value="165 Imaginary Orchard Way"' in str(form["lot"])


# --- The widget's markup and script ---------------------------------------------------------


def test_script_is_static_and_csp_safe():
    js = (ROOT / "static/js/home-picker.js").read_text()
    assert "innerHTML" not in js and "eval(" not in js
    assert 'setAttribute("role", "combobox")' in js and "aria-activedescendant" in js
    for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
        assert key in js
    for name in ("partials/home_picker.html", "widgets/home_picker.html"):
        html = (ROOT / "templates" / name).read_text()
        assert "<script" not in html and " on" not in html.split("#}")[-1].replace(" only", "")
    css = (ROOT / "static/css/site.css").read_text()
    rules = {
        line.split("{")[0].strip(): line
        for line in css.splitlines()
        if line.startswith(".home-picker")
    }
    assert "min-height: 44px" in rules[".home-picker-option, .home-picker-empty"]
    assert "min-width: 44px" in rules[".home-picker-remove"]


def test_base_loads_the_script_only_when_signed_in(client, w):
    assert "home-picker.js" not in client.get(reverse("account_login")).content.decode()
    client.force_login(w["board"])
    assert "home-picker.js" in client.get(reverse("home")).content.decode()


# --- Every changed form still saves the right home -----------------------------------------


def test_neighbors_one_home_by_address(client, w):
    from arc import signoffs

    client.force_login(w["board"])
    response = client.post(
        reverse("arc_neighbors"),
        {
            "lot": str(w["oak"].number),
            "left": "9 Birch Court",
            "right": "167 imaginary orchard way",
            "others": "301 Meadow Lane",
        },
    )
    assert response.status_code == 302
    sides = signoffs.neighbor_sides(w["oak"])
    assert sides["left"] == w["birch"] and sides["right"] == w["elm"]
    assert sides["other"] == [w["far"]]


def test_neighbors_page_shows_addresses_not_numbers(client, w):
    from arc import signoffs

    signoffs.set_neighbors(
        w["oak"], left=w["birch"], right=w["elm"], others=[w["far"]], actor=w["board"]
    )
    client.force_login(w["board"])
    page = client.get(reverse("arc_neighbors")).content.decode()
    assert "Left: 9 Birch Court · Right: 167 Imaginary Orchard Way · Also: 301 Meadow Lane" in page
    assert 'name="left_12" id="nl-12" value="9 Birch Court"' in page
    assert 'name="others_12" id="no-12" value="301 Meadow Lane"' in page
    assert "plat lot number" not in page
    page = client.get(reverse("arc_neighbors"), {"home": 12}).content.decode()
    assert 'id="edit-left" value="9 Birch Court"' in page and "data-multiple" in page


def test_neighbors_ambiguous_text_saves_nothing_and_keeps_the_typing(client, w):
    from arc import signoffs

    client.force_login(w["board"])
    response = client.post(
        reverse("arc_neighbors"),
        {
            "save_all": "1",
            "left_12": "Imaginary",
            "right_12": "",
            "others_12": "",
            "left_14": "9 Birch Court",
            "right_14": "",
            "others_14": "",
        },
    )
    assert response.status_code == 400
    page = response.content.decode()
    assert "Nothing was saved" in page
    assert "Left neighbor: No home matches" in page and "Did you mean" in page
    assert 'name="left_12" id="nl-12" value="Imaginary"' in page
    assert signoffs.neighbor_sides(w["elm"])["left"] is None  # the good row waits too
    assert signoffs.neighbor_sides(w["oak"])["left"] is None


def test_neighbors_save_all_by_lot_number_still_works(client, w):
    from arc import signoffs

    client.force_login(w["board"])
    client.post(
        reverse("arc_neighbors"),
        {"save_all": "1", "left_12": "30", "right_12": "14", "others_12": "41"},
    )
    sides = signoffs.neighbor_sides(w["oak"])
    assert (sides["left"], sides["right"], sides["other"]) == (w["birch"], w["elm"], [w["far"]])


def test_neighbors_page_is_board_only(client, w):
    client.force_login(w["owner"])
    assert client.get(reverse("arc_neighbors")).status_code == 403
    assert client.post(reverse("arc_neighbors"), {"lot": "12", "left": "30"}).status_code == 403


def test_violation_report_by_address(client, w):
    from compliance.models import Violation

    client.force_login(w["board"])
    response = client.post(
        reverse("violations"),
        {
            "lot": "301 meadow lane",
            "category": "other",
            "description": "Trailer parked on the lawn",
            "observed_on": datetime.date.today().isoformat(),
        },
    )
    assert response.status_code == 302
    assert Violation.objects.unscoped("t").get().lot == w["far"]
    response = client.post(
        reverse("violations"),
        {
            "lot": "Imaginary",
            "category": "other",
            "description": "x",
            "observed_on": datetime.date.today().isoformat(),
        },
    )
    assert response.status_code == 200 and "Did you mean" in response.content.decode()


def test_paper_mail_one_home_by_address(client, w):
    from comms.models import Mailing

    client.force_login(w["board"])
    page = client.get(reverse("paper_mail_new"), {"home": w["oak"].pk}).content.decode()
    assert 'value="165 Imaginary Orchard Way"' in page and "data-home-picker" in page
    client.post(
        reverse("paper_mail_new"),
        {
            "subject": "Pond",
            "body": "The pond is being dredged.",
            "audience": "all",
            "home": "165 Imaginary Orchard Way",
            "step": "create",
        },
    )
    assert Mailing.objects.unscoped("t").get().only_lot_id == w["oak"].pk


def test_resale_request_by_address_for_the_board(client, w):
    from ledger.models import ResaleRequest

    client.force_login(w["manager"])
    page = client.get(reverse("resales")).content.decode()
    assert "data-home-picker" in page
    client.post(reverse("resales"), {"lot": "167 Imaginary Orchard Way", "requester_name": "Agent"})
    assert ResaleRequest.objects.unscoped("t").get().lot == w["elm"]


def test_owner_resale_form_stays_a_list_of_their_homes(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("resales")).content.decode()
    assert "<select" in page and "165 Imaginary Orchard Way" in page
    assert "301 Meadow Lane" not in page


def test_owner_cannot_use_another_home_in_the_owner_resale_form(client, w):
    from ledger.models import ResaleRequest

    client.force_login(w["owner"])
    client.post(reverse("resales"), {"lot": w["far"].pk, "requester_name": "Agent"})
    assert not ResaleRequest.objects.unscoped("t").exists()


def test_add_ownership_by_address(client, w):
    with system_access("t"):
        person = Person.objects.create(display_name="Casey Example")
    client.force_login(w["board"])
    client.post(
        reverse("person_add_ownership", args=[person.pk]),
        {
            "lot": "167 Imaginary Orchard Way",
            "ownership_type": "sole",
            "start_date": datetime.date.today().isoformat(),
        },
    )
    with system_access("t"):
        assert Ownership.objects.filter(person=person, lot=w["elm"]).exists()


def test_arc_affected_homes_by_address(client, w):
    from arc import services as arc_services
    from arc.models import ArcRequest

    req = _arc_request(w)
    client.force_login(w["board"])
    page = client.get(reverse("arc_detail", args=[req.pk])).content.decode()
    assert "plat lot numbers" not in page
    response = client.post(
        reverse("arc_affected", args=[req.pk]), {"lots": "167 Imaginary Orchard Way; 41"}
    )
    assert response.status_code == 302
    with system_access("t"):
        affected = set(ArcRequest.objects.get(pk=req.pk).affected_lots.all())
    assert affected == {w["elm"], w["far"]}
    assert arc_services.is_recused(w["owner_far"], req)
    # Text that names no home changes nothing and says why.
    response = client.post(reverse("arc_affected", args=[req.pk]), {"lots": "Nowhere"}, follow=True)
    assert "Affected homes not saved" in response.content.decode()
    with system_access("t"):
        assert set(ArcRequest.objects.get(pk=req.pk).affected_lots.all()) == affected


def _arc_request(w):
    from arc import services as arc_services

    req = arc_services.create_request(
        lot=w["oak"], title="New fence", description="Four-foot fence", actor=w["owner"]
    )
    return arc_services.ARC.apply(req, "submit", actor=w["owner"])
