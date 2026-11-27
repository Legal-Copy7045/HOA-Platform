"""Walkthrough 2, part A (D63): template comments, board-meeting proxies,
delinquency paper copies, Voting drafts, setup law, phone layout guards,
Sysadmin activity runs, Record payments rows, People find box and more."""

import datetime
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from community.models import Role
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db

ROOT = Path(settings.BASE_DIR)
TEMPLATES = sorted((ROOT / "templates").rglob("*.html"))
CSS = (ROOT / "static/css/site.css").read_text()
JS = (ROOT / "static/js/app.js").read_text()


@pytest.fixture
def w():
    lot, lot_b = LotFactory(), LotFactory()
    return {
        "lot": lot,
        "lot_b": lot_b,
        "board": make_member(board=True, lots=[LotFactory()]),
        "board2": make_member(board=True),
        "owner": make_member(lots=[lot]),
        "owner_b": make_member(lots=[lot_b]),
        "manager": make_member(roles=[Role.MANAGER]),
        "auditor": make_member(roles=[Role.AUDITOR]),
        "sysadmin": make_member(roles=[Role.SYSADMIN]),
    }


def _meeting(kind, actor, days=7):
    from governance import services as gov

    return gov.create_meeting(
        title=f"{kind} meeting",
        kind=kind,
        location="Clubhouse",
        starts_at=timezone.now() + datetime.timedelta(days=days),
        actor=actor,
    )


def _person(user):
    from community.models import Person
    from core.scoping import system_access

    with system_access("t"):
        return Person.objects.get(user=user)


# --- WT-001 / WT-024: comments never printed --------------------------------------------------


def test_no_template_has_a_comment_split_over_lines():
    """Django's {# #} works on one line only; a split one is printed as text."""
    bad = []
    for path in TEMPLATES:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for start in [m.start() for m in re.finditer(r"\{#", line)]:
                if "#}" not in line[start:]:
                    bad.append(f"{path.relative_to(ROOT)}:{n}")
    assert not bad, bad


@pytest.mark.parametrize("who", ["owner", "board", "manager", "auditor", "sysadmin"])
def test_dashboard_prints_no_template_comment(client, w, who):
    client.force_login(w[who])
    html = client.get(reverse("home")).content.decode()
    assert "{#" not in html and "#}" not in html and "WT-016" not in html


def test_community_settings_prints_no_template_comment(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("community_settings")).content.decode()
    assert "{#" not in html and "WT-014" not in html


# --- WT-002: board meetings take no owner proxies ---------------------------------------------


def test_board_meeting_shows_no_proxy_form_or_owners_quorum(client, w):
    meeting = _meeting("board", w["board"])
    for who in ("owner", "board"):
        client.force_login(w[who])
        html = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
        assert 'id="proxy"' not in html and "Give a proxy" not in html, who
        assert "homes represented" not in html, who
        assert "they do not vote here" in html, who


def test_owners_meeting_still_offers_the_proxy_and_quorum(client, w):
    meeting = _meeting("annual", w["board"])
    client.force_login(w["owner"])
    assert 'id="proxy"' in client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    client.force_login(w["board"])
    html = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert "homes represented" in html


@pytest.mark.parametrize("kind", ["board", "committee"])
def test_grant_proxy_refuses_meetings_where_owners_do_not_vote(w, kind):
    from governance import elections
    from governance.models import Proxy

    meeting = _meeting(kind, w["board"])
    with pytest.raises(elections.ElectionError, match="owners' meetings"):
        elections.grant_proxy(meeting, lot=w["lot"], holder=_person(w["board"]), actor=w["owner"])
    assert not Proxy.objects.unscoped("t").filter(meeting=meeting).exists()


def test_proxy_post_for_a_board_meeting_records_nothing(client, w):
    from governance.models import Proxy

    meeting = _meeting("board", w["board"])
    client.force_login(w["owner"])
    response = client.post(
        reverse("proxy_grant", args=[meeting.pk]),
        {"lot": w["lot"].pk, "holder": _person(w["board"]).pk},
        follow=True,
    )
    assert response.status_code == 200
    assert b"Proxies are only for owners" in response.content
    assert not Proxy.objects.unscoped("t").filter(meeting=meeting).exists()


def test_an_old_proxy_on_a_board_meeting_is_listed_not_deleted(client, w):
    from governance.models import Proxy

    meeting = _meeting("board", w["board"])
    # A proxy recorded before the rule (append-only: it stays).
    Proxy.objects.unscoped("t").create(
        meeting=meeting, lot=w["lot"], grantor=_person(w["owner"]), holder=_person(w["board"])
    )
    client.force_login(w["owner"])
    html = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert "It has no effect here." in html and "Revoke" in html
    client.force_login(w["board"])
    html = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert "1 proxy record for this meeting was made before" in html


# --- WT-006: delinquency notices only from the collection case --------------------------------


def _case_with_letter(w):
    from ledger import accounting
    from ledger import services as ledger
    from ledger.models import CollectionCase, CollectionLetter

    ledger.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=50000,
        description="Unpaid charge",
        posted_on=timezone.localdate() - datetime.timedelta(days=75),
        actor=w["board"],
    )
    ledger.open_case(lot=w["lot"], notes="Owes", actor=w["board"])
    case = CollectionCase.objects.unscoped("t").get(lot=w["lot"])
    accounting.generate_case_letter(case, actor=w["board"])
    return case, CollectionLetter.objects.unscoped("t").get(case=case)


def test_paper_mail_offers_no_delinquency_wording(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("paper_mail")).content.decode()
    assert 'name="template"' not in html
    assert "First delinquency notice" not in html and "Final" not in html
    # An old link with ?template= is refused, nothing prepared.
    from comms.models import Mailing

    response = client.get(reverse("paper_mail_new") + "?template=first&everyone=1", follow=True)
    assert b"collection case" in response.content
    assert not Mailing.objects.unscoped("t").exists()


@pytest.mark.parametrize("who", ["board", "manager"])
def test_collection_case_prepares_a_one_home_paper_copy(client, w, who):
    from audit.models import AuditEvent
    from comms.models import Mailing, MailingKind

    case, letter = _case_with_letter(w)
    client.force_login(w[who])
    page = client.get(reverse("collection_detail", args=[case.pk])).content.decode()
    assert "Also prepare a paper copy" in page
    response = client.post(reverse("collection_paper_copy", args=[case.pk]), {"letter": letter.pk})
    mailing = Mailing.objects.unscoped("t").get()
    assert response.status_code == 302 and response["Location"].endswith(
        reverse("paper_mail_detail", args=[mailing.pk])
    )
    assert mailing.kind == MailingKind.TEMPLATE and mailing.template_kind == letter.kind
    assert mailing.only_lot_id == w["lot"].pk and mailing.homes_count == 1
    assert {g["lot"] for g in mailing.groups} == {w["lot"].pk}
    event = AuditEvent.objects.filter(action="mailing.created").latest("id")
    assert event.metadata["homes"] == 1


def test_paper_copy_refused_for_owners_and_other_cases(client, w):
    from comms.models import Mailing

    case, letter = _case_with_letter(w)
    client.force_login(w["owner"])
    response = client.post(reverse("collection_paper_copy", args=[case.pk]), {"letter": letter.pk})
    assert response.status_code in (403, 404)
    client.force_login(w["auditor"])
    response = client.post(reverse("collection_paper_copy", args=[case.pk]), {"letter": letter.pk})
    assert response.status_code in (403, 404)
    assert not Mailing.objects.unscoped("t").exists()


def test_paper_copy_service_needs_mail_print(w):
    from comms import papermail

    _case, letter = _case_with_letter(w)
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        papermail.prepare_case_copy(letter, actor=w["auditor"])


# --- WT-012: drafts are not "Open" ------------------------------------------------------------


def _draft_vote(actor):
    from governance import services as gov
    from governance.models import VoteKind

    return gov.create_vote(
        kind=VoteKind.FORMAL,
        title="Adopt the pet rules (draft)",
        options=["Yes", "No"],
        actor=actor,
    )


def test_voting_open_excludes_drafts_which_have_their_own_filter(client, w):
    _draft_vote(w["board"])
    client.force_login(w["board"])
    html = client.get(reverse("voting")).content.decode()
    assert "Adopt the pet rules (draft)" not in html
    assert "?status=drafts" in html
    html = client.get(reverse("voting") + "?status=drafts").content.decode()
    assert "Adopt the pet rules (draft)" in html


def test_owners_get_no_drafts_filter(client, w):
    _draft_vote(w["board"])
    client.force_login(w["owner"])
    html = client.get(reverse("voting") + "?status=drafts").content.decode()
    assert "?status=drafts" not in html and "Adopt the pet rules (draft)" not in html


# --- WT-013 / WT-032: setup law ---------------------------------------------------------------


def test_setup_steps_never_show_the_same_law_twice():
    from community import setup
    from community.setup_views import _SECTION, law_panel

    for step in setup.STEPS:
        panel = law_panel(step)
        citations = [n.citation for n in panel["notes"]]
        assert len(citations) == len(set(citations)), step.key
        texts = [p["text"] for p in panel["pa"]]
        assert len(texts) == len(set(texts)), step.key
        for note in panel["notes"]:
            if "68 Pa.C.S." in note.citation:
                wanted = set(_SECTION.findall(note.citation))
                for p in panel["pa"]:
                    assert not wanted <= set(_SECTION.findall(p["text"])), (step.key, note.title)


def test_merged_law_entry_keeps_citation_and_caveat():
    from community import setup
    from community.setup_views import law_panel

    panel = law_panel(next(s for s in setup.STEPS if s.key == "charge"))
    entry = next(p for p in panel["pa"] if p.get("citation"))
    assert "§ 5302(a)" in entry["citation"] and "counsel" in entry["caveat"].lower()
    assert entry["meaning"]


def test_setup_law_panel_is_closed_on_phones_open_on_computers(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("setup_step", args=["charge"])).content.decode()
    assert '<details class="card setup-law" open data-collapse-narrow' in html
    assert "details[data-collapse-narrow]" in JS


# --- CSS at the source (WT-015, 022, 023, 025, 026) -------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        "[hidden] { display: none !important; }",
        ".kv { grid-template-columns: minmax(0, 1fr);",
        ".shell:has(.tabbar) .save-bar, .shell:has(.tabbar) .setup-actions { bottom: calc(56px",
        ".stat .value { font-size: clamp(1rem, 5vw, 1.2rem); white-space: nowrap; }",
        ".list.task-rows > li { flex-wrap: wrap; }",
    ],
)
def test_site_css_fixes(rule):
    assert rule in CSS


def test_summary_tiles_are_not_turned_into_inline_tap_links():
    # app.js gives a lone link the 44px inline-flex tap style; a .stat tile
    # would then lay its label, amount and hint side by side (WT-026).
    assert 'a.classList.contains("stat")' in JS


def test_kv_is_defined_once_and_stat_values_never_break_anywhere():
    assert CSS.count(".kv { display: grid;") == 1
    assert ".stat .value { font-size: 1.2rem; overflow-wrap: anywhere; }" not in CSS


# --- WT-029: every table stacks on phones or says why not -------------------------------------


def test_every_table_stacks_on_phones_or_opts_out_with_a_reason():
    bad = []
    for path in TEMPLATES:
        text = path.read_text()
        for m in re.finditer(r"<table\b[^>]*>", text):
            tag = m.group(0)
            if "stack-sm" in tag:
                continue
            before = text[max(0, m.start() - 300) : m.start()]
            if "data-wide" in tag and re.search(r"\{#\s*data-wide: \S.+?#\}", before):
                continue
            bad.append(f"{path.relative_to(ROOT)}: {tag}")
    assert not bad, bad


def test_access_review_and_disbursements_label_their_cells(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("access_review")).content.decode()
    assert '<table class="stack-sm">' in html and 'data-label="Flags"' in html


# --- WT-027: Community settings on phones -----------------------------------------------------


def test_settings_page_has_a_jump_select_and_opens_one_attention_section(client, w):
    client.force_login(w["board"])
    response = client.get(reverse("community_settings"))
    html = response.content.decode()
    assert 'select id="jump-section" data-jump' in html
    assert 'class="page-index desktop-only"' in html
    first = response.context["first_attention"]
    opened = re.findall(r'<details class="card rules collapsible" id="sec-\d+" open>', html)
    assert len(opened) == (1 if first else 0)
    assert "select[data-jump]" in JS


# --- WT-028: Sysadmin activity runs -----------------------------------------------------------


def _events(action, n, actor, minutes_apart=1):
    from audit.models import AuditEvent

    now = timezone.now() - datetime.timedelta(hours=1)
    for i in range(n):
        AuditEvent.objects.create(
            action=action,
            actor=actor,
            actor_is_sysadmin=True,
            occurred_at=now - datetime.timedelta(seconds=minutes_apart * i),
            metadata={"view": f"v{i}"},
        )


def test_sysadmin_activity_collapses_runs_and_keeps_the_csv_whole(client, w):
    _events("sysadmin.request", 150, w["sysadmin"])
    client.force_login(w["board"])
    response = client.get(reverse("sysadmin_activity"), {"action": "sysadmin.request"})
    html = response.content.decode()
    assert response.context["count"] == 150 and response.context["row_count"] == 1
    assert "\u00d7150" in html and "Show the 150 events" in html
    assert "and 130 more" in html
    export = client.get(reverse("sysadmin_activity") + "?export=csv&action=sysadmin.request")
    body = b"".join(export.streaming_content) if export.streaming else export.content
    csv_text = body.decode()
    assert csv_text.count("sysadmin.request") == 150


def test_sysadmin_activity_pages_rows(client, w, monkeypatch):
    from audit import views

    monkeypatch.setattr(views, "ROWS_PER_PAGE", 2)
    other = make_member(roles=[Role.SYSADMIN])
    for i in range(5):  # alternating people: five rows
        _events("system.update_requested", 1, w["sysadmin"] if i % 2 else other)
    client.force_login(w["auditor"])
    url = reverse("sysadmin_activity") + "?action=system.update_requested"
    response = client.get(url)
    assert response.context["pages"] == 3 and response.context["next_page_url"]
    last = client.get(url + "&page=3")
    assert last.context["page"] == 3 and not last.context["next_page_url"]


# --- WT-030: Calendar one control per form ----------------------------------------------------


def test_calendar_header_buttons_open_hidden_panels(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("calendar")).content.decode()
    assert 'data-opens="new-event"' in html and 'data-opens="new-meeting"' in html
    assert 'hidden id="new-event" data-hide-closed' in html
    assert 'hidden id="new-meeting" data-hide-closed' in html
    html = client.get(reverse("calendar") + "?new=event").content.decode()
    assert 'data-opens="new-event"' not in html and 'open id="new-event" data-hide-closed' in html


# --- WT-031: Home neighbors -------------------------------------------------------------------


def test_home_neighbors_has_one_save_all_and_no_row_saves(client, w):
    client.force_login(w["board"])
    html = client.get(reverse("arc_neighbors")).content.decode()
    form = html[
        html.index('id="neighbors-all"') : html.index("</form>", html.index('id="neighbors-all"'))
    ]
    assert form.count('type="submit"') == 1 and 'name="save_all"' in form
    assert 'name="lot" value=' not in html


# --- WT-033: Record payments ------------------------------------------------------------------


def test_record_payments_starts_with_three_rows(client, w):
    client.force_login(w["manager"])
    response = client.get(reverse("record_payments"))
    assert response.context["row_count"] == 3
    html = response.content.decode()
    assert 'name="more"' in html and "data-add-row" in html and "?rows=" not in html


def test_add_another_row_without_javascript_keeps_what_was_typed(client, w):
    from ledger.models import LedgerEntry

    client.force_login(w["manager"])
    data = {
        "rows": "3",
        "lot_0": str(w["lot"].pk),
        "amount_0": "75.00",
        "check_0": "1042",
        "date_0": timezone.localdate().isoformat(),
        "more": "1",
    }
    response = client.post(reverse("record_payments"), data)
    assert response.status_code == 200
    assert response.context["row_count"] == 4
    html = response.content.decode()
    assert 'value="75.00"' in html and 'value="1042"' in html
    assert f'<option value="{w["lot"].pk}" selected>' in html
    assert 'name="lot_3"' in html
    assert not LedgerEntry.objects.unscoped("t").filter(kind="payment").exists()


def test_add_row_script_is_in_app_js():
    assert "[data-add-row]" in JS and "data-batch-rows" in JS


# --- WT-034: surplus distribution form --------------------------------------------------------


def test_surplus_homes_sit_in_a_closed_panel_after_the_main_fields(client, w):
    from community import services as community_services
    from core import features

    features.set_feature("disbursements", True, actor=SYSTEM)
    # The check panel exists only when the board allows surplus checks (D67).
    community_services.set_rule(
        key="disbursement.surplus_cash_allowed", value="true", citation="t", actor=SYSTEM
    )
    client.force_login(w["board"])
    html = client.get(reverse("disbursement_new") + "?kind=surplus").content.decode()
    assert '<details class="panel" id="by-check" data-testid="by-check">' in html
    assert html.index('id="by-check"') > html.index('name="total_cents"')
    assert "Pay some homes by check instead (0 chosen)" in html


# --- WT-035: Home account entry actions -------------------------------------------------------


def test_reverse_is_inside_one_small_actions_menu(client, w):
    from ledger import services as ledger

    ledger.post_charge(
        lot=w["lot"],
        kind="other_charge",
        amount_cents=1000,
        description="Charge",
        posted_on=timezone.localdate(),
        actor=w["board"],
    )
    client.force_login(w["board"])
    html = client.get(reverse("lot_ledger", args=[w["lot"].pk])).content.decode()
    assert '<details class="row-menu row-more">' in html
    assert "<summary>Reverse</summary>" not in html
    assert "Post a reversing entry? The original stays on the record." in html
    client.force_login(w["owner"])
    assert client.get(reverse("lot_ledger", args=[w["lot"].pk])).status_code in (403, 404)


# --- WT-037: People find box ------------------------------------------------------------------


def test_people_find_by_name_or_home_and_sign_up_filter(client, w):
    from community import services as community

    community.record_person(display_name="Zion Example", actor=SYSTEM)
    client.force_login(w["board"])
    page = client.get(reverse("people")).content.decode()
    assert 'name="q"' in page and "?login=none" in page
    response = client.get(reverse("people"), {"q": "zion"})
    names = [p.display_name for p in response.context["people"]]
    assert names == ["Zion Example"]
    owner = _person(w["owner"])
    by_home = client.get(reverse("people"), {"q": w["lot"].street_address})
    assert owner in list(by_home.context["people"])
    pending = client.get(reverse("people"), {"login": "none"})
    assert all(p.user_id is None for p in pending.context["people"])


def test_people_find_is_board_only(client, w):
    client.force_login(w["owner"])
    assert client.get(reverse("people"), {"q": "a"}).status_code == 403
