"""Owner help (D73): the getting-started checklist derived from real state
per role, the welcome page once after the guided first sign-in, "How it
works" pages that read today's rules, the board's own words (escaped,
append-only), search scoped by role, and "Ask the board" for each role."""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from community import checklist, services
from community import help as helpdocs
from community.models import Account, HelpMark, HelpText, Person
from community.tests.factories import LotFactory, make_member
from core.append_only import AppendOnlyError
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


def _account(lot):
    return Account.objects.unscoped("t").get(lot=lot, closed_on__isnull=True)


def _contact(lot, kind):
    user = make_member()
    person = Person.objects.unscoped("t").get(user=user)
    services.add_account_contact(_account(lot), person=person, kind=kind, actor=SYSTEM)
    return user


@pytest.fixture
def w():
    lot = LotFactory(street_address="1 Alpha Way")
    other = LotFactory(street_address="2 Bravo Way")
    with mock.patch("community.tasks.notify_board.defer"):
        owner = make_member(lots=[lot])
        board = make_member(board=True, lots=[other])
        board2 = make_member(board=True)
        return {
            "lot": lot,
            "owner": owner,
            "board": board,
            "board2": board2,
            "authorized": _contact(lot, "authorized"),
            "view_only": _contact(lot, "view_only"),
            "tenant": _contact(lot, "tenant"),
        }


def _keys(user) -> list[str]:
    return [s.key for s in checklist.steps(user)]


def _step(user, key):
    return next(s for s in checklist.steps(user) if s.key == key)


def _rule(key, value, citation="Declaration §6.1 (test)"):
    services.set_rule(key=key, value=value, citation=citation, actor=SYSTEM)


# --- The checklist: steps per role ---


def test_steps_follow_the_role(w):
    owner = _keys(w["owner"])
    assert owner[:3] == ["signin", "contact", "notices"]
    assert {"statements", "documents", "board", "ask"} <= set(owner)
    assert "autopay" not in owner and "texts" not in owner  # features off
    for who in ("authorized", "view_only"):
        keys = _keys(w[who])
        assert "statements" in keys and "notices" not in keys, who
    tenant = _keys(w["tenant"])
    assert "statements" not in tenant and "notices" not in tenant
    assert {"signin", "contact", "documents", "board", "ask"} <= set(tenant)
    # Staff without a home: no checklist and no page.
    assert checklist.steps(w["board2"]) == []
    assert checklist.for_user(w["board2"]) is None


def test_staff_without_a_home_cannot_open_getting_started(client, w):
    client.force_login(w["board2"])
    assert client.get(reverse("getting_started")).status_code == 403
    assert client.post(reverse("getting_started"), {"action": "docs_read"}).status_code == 403


def test_optional_steps_appear_when_their_features_are_on(w):
    from core.models import FeatureSetting

    FeatureSetting.objects.create(key="sms", enabled=True)
    with mock.patch("payments.autopay.autopay_enabled", return_value=True):
        owner = _keys(w["owner"])
        assert "texts" in owner and "autopay" in owner
        assert "autopay" not in _keys(w["view_only"])  # cannot pay
        assert "texts" not in _keys(w["tenant"])  # texts are for owners
    autopay = _step(w["owner"], "texts")
    assert autopay.optional and autopay.dismissible


def test_steps_are_done_only_by_what_is_recorded(client, w):
    from allauth.mfa.models import Authenticator

    owner = w["owner"]
    assert not any(s.done for s in checklist.steps(owner))
    # A second strong sign-in method.
    Authenticator.objects.create(user=owner, type=Authenticator.Type.WEBAUTHN, data={})
    assert _step(owner, "signin").done
    # The notices choice (D44).
    services.record_notice_choice(user=owner, consent=True)
    assert _step(owner, "notices").done
    # Contact details: changing one on My profile counts.
    with mock.patch("community.tasks.notify_board.defer"):
        services.set_mailing_address(user=owner, to_home=True)
    assert _step(owner, "contact").done
    # The statement email choice, saved on My profile.
    services.update_my_profile(user=owner, statement_emails=True)
    assert _step(owner, "statements").done
    client.force_login(owner)
    # The board's page, opened from the checklist.
    response = client.post(reverse("getting_started"), {"action": "open_board"})
    assert response["Location"] == reverse("board_and_committees")
    assert _step(owner, "board").done
    # Ask the board, opened once.
    client.get(reverse("ask_board"))
    assert _step(owner, "ask").done
    client.post(reverse("getting_started"), {"action": "docs_read"})
    assert _step(owner, "documents").done
    assert checklist.for_user(owner).complete
    assert checklist.for_dashboard(owner) is None  # complete: off the dashboard


def test_the_person_s_own_buttons_mark_only_their_own_steps(client, w):
    client.force_login(w["tenant"])
    client.post(reverse("getting_started"), {"action": "confirm_contact"})
    assert _step(w["tenant"], "contact").done
    assert not _step(w["owner"], "contact").done
    assert HelpMark.objects.filter(user=w["tenant"]).count() == 1


def test_asking_the_board_counts_even_without_the_page(w):
    from operations import services as operations

    operations.submit_request(
        lot=w["lot"],
        request_type="general",
        subject="Pool hours",
        description="When does the pool open?",
        actor=w["owner"],
    )
    assert _step(w["owner"], "ask").done


def test_only_optional_steps_can_be_dismissed(client, w):
    from core.models import FeatureSetting

    FeatureSetting.objects.create(key="sms", enabled=True)
    client.force_login(w["owner"])
    client.post(reverse("getting_started"), {"action": "dismiss_documents"})
    assert not _step(w["owner"], "documents").done
    client.post(reverse("getting_started"), {"action": "dismiss_signin"})
    assert not _step(w["owner"], "signin").done
    client.post(reverse("getting_started"), {"action": "dismiss_texts"})
    assert _step(w["owner"], "texts").done


def test_marks_are_append_only(w):
    checklist.mark(w["owner"], HelpMark.Kind.DOCS_READ, "governing")
    assert not checklist.mark(w["owner"], HelpMark.Kind.DOCS_READ, "governing")  # once
    row = HelpMark.objects.get(user=w["owner"])
    with pytest.raises(AppendOnlyError):
        row.delete()
    with pytest.raises(AppendOnlyError):
        HelpMark.objects.filter(pk=row.pk).update(key="x")


# --- Dashboard and My items ---


def test_dashboard_card_and_one_my_items_entry(client, w):
    from core.my_items import items_for

    titles = [i.title for i in items_for(w["owner"])]
    assert titles.count("Finish getting started") == 1
    # The backup method is not repeated: My items asks for it itself.
    assert titles.count("Add a backup sign-in method") == 1
    client.force_login(w["owner"])
    page = client.get(reverse("home")).content.decode()
    assert 'id="getting-started"' in page
    assert "Finish getting started" not in page  # the card stands for it here
    # Hidden by the person: gone from the dashboard and My items, page stays.
    client.post(reverse("getting_started"), {"action": "hide_all"})
    page = client.get(reverse("home")).content.decode()
    assert 'id="getting-started"' not in page
    assert "Finish getting started" not in [i.title for i in items_for(w["owner"])]
    assert client.get(reverse("getting_started")).status_code == 200


def test_the_card_shows_only_in_the_first_days(client, w):
    owner = w["owner"]
    assert checklist.for_dashboard(owner) is not None
    type(owner).objects.filter(pk=owner.pk).update(
        date_joined=timezone.now() - datetime.timedelta(days=91)
    )
    owner.refresh_from_db()
    assert checklist.for_dashboard(owner) is None
    assert checklist.my_items(owner) == []
    _rule("help.welcome_days", "120")
    assert checklist.for_dashboard(owner) is not None
    _rule("help.welcome_days", "0")
    assert checklist.for_dashboard(owner) is None


def test_feature_off_removes_card_pages_and_menu(client, w):
    from core.models import FeatureSetting

    FeatureSetting.objects.create(key="owner_help", enabled=False)
    client.force_login(w["owner"])
    page = client.get(reverse("home")).content.decode()
    assert 'id="getting-started"' not in page
    assert f'href="{reverse("help")}"' not in page
    for name in ("help", "welcome", "getting_started"):
        assert client.get(reverse(name)).status_code == 404, name
    assert client.get(reverse("help_topic", args=["paying"])).status_code == 404
    # Ask the board is not part of the feature.
    assert client.get(reverse("ask_board")).status_code == 302


# --- Guided first sign-in: the welcome page once ---


def test_setup_ends_on_the_welcome_page_once(client, w):
    from accounts import enrollment  # noqa: F401  (the guided setup module)

    tenant = w["tenant"]  # no notices step: straight to the end of setup
    client.force_login(tenant)
    response = client.post(reverse("account_setup_done"))
    assert response["Location"] == reverse("welcome")
    page = client.get(reverse("welcome")).content.decode()
    assert "<h1>Welcome</h1>" in page and "Ask the board" in page
    assert 'id="getting-started"' in page
    assert client.post(reverse("account_setup_done"))["Location"] == reverse("home")


def test_old_logins_and_switched_off_help_go_home(client, w):
    owner = w["tenant"]
    type(owner).objects.filter(pk=owner.pk).update(
        date_joined=timezone.now() - datetime.timedelta(days=400)
    )
    owner.refresh_from_db()
    assert helpdocs.after_setup_url(owner) == reverse("home")
    from core.models import FeatureSetting

    fresh = w["authorized"]
    assert helpdocs.after_setup_url(fresh) == reverse("welcome")
    FeatureSetting.objects.create(key="owner_help", enabled=False)
    assert helpdocs.after_setup_url(fresh) == reverse("home")


def test_the_notices_step_leads_to_the_welcome_page(client, w):
    client.force_login(w["owner"])
    response = client.post(reverse("notice_choice"), {"choice": "email"})
    assert response["Location"] == reverse("welcome")


def test_welcome_needs_sign_in_and_an_authenticator(client, w):
    assert client.get(reverse("welcome"))["Location"].startswith(reverse("account_login"))
    no_mfa = make_member(lots=[LotFactory()], mfa=False)
    client.force_login(no_mfa)
    response = client.get(reverse("welcome"))
    assert response.status_code == 302 and reverse("welcome") not in response["Location"]
    assert not HelpMark.objects.filter(user=no_mfa).exists()


# --- How it works pages read today's rules ---


def test_paying_page_shows_the_configured_amounts_with_citations(client, w):
    _rule("dues.monthly_cents", "15500", "Declaration §6.1 (test)")
    _rule("dues.due_day", "5", "Bylaws §8 (test)")
    _rule("late_fee.cents", "2500", "Resolution 2026-3 (test)")
    _rule("late_fee.grace_days", "15", "Declaration §6.4 (test)")
    client.force_login(w["owner"])
    page = client.get(reverse("help_topic", args=["paying"])).content.decode()
    assert "<h1>How paying works</h1>" in page
    assert "$155.00 a month" in page and "Declaration §6.1 (test)" in page
    assert "5th of each month" in page and "Bylaws §8 (test)" in page
    assert "$25.00" in page and "15 days" in page
    assert "Assessment" in page  # the charge's name (D42)
    _rule("charge.term", "Fee")
    page = client.get(reverse("help_topic", args=["paying"])).content.decode()
    assert "The fee is $155.00" in page
    _rule("dues.monthly_cents", "16000")
    assert "$160.00 a month" in client.get(reverse("help_topic", args=["paying"])).content.decode()


def test_arc_and_violation_pages_read_their_deadlines(client, w):
    _rule("arc.review_days", "45", "Declaration §9.4 (test)")
    _rule("arc.deemed_approval", "true", "Declaration §9.5 (test)")
    _rule("arc.appeal_days", "30", "Declaration §9.6 (test)")
    _rule("violation.cure_days", "14", "Rules §2 (test)")
    _rule("violation.hearing_notice_days", "10", "Declaration §12 (test)")
    client.force_login(w["owner"])
    arc = client.get(reverse("help_topic", args=["architectural-review"])).content.decode()
    assert "45 days" in arc and "Declaration §9.4 (test)" in arc
    assert "approved automatically" in arc and "30 days" in arc
    v = client.get(reverse("help_topic", args=["violations"])).content.decode()
    assert "14 days" in v and "Rules §2 (test)" in v and "10 days" in v
    assert "does not charge fines" in v


def test_unset_rules_say_so(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("help_topic", args=["paying"])).content.decode()
    assert "has not recorded this yet" in page


def test_topics_follow_the_role(client, w):
    client.force_login(w["tenant"])
    assert client.get(reverse("help_topic", args=["paying"])).status_code == 404
    assert client.get(reverse("help_topic", args=["requests"])).status_code == 200
    assert client.get(reverse("help_topic", args=["nonsense"])).status_code == 404
    titles = [t.slug for t in helpdocs.topics_for(w["tenant"])]
    assert "paying" not in titles and "voting-and-meetings" in titles
    assert [t.slug for t in helpdocs.topics_for(w["owner"])] == [t.slug for t in helpdocs.TOPICS]


def test_section_pages_link_to_their_help_page(client, w):
    client.force_login(w["owner"])
    for name, slug in (
        ("my_account", "paying"),
        ("violations", "violations"),
        ("voting", "voting-and-meetings"),
        ("requests", "requests"),
    ):
        url = reverse(name) + ("?mine=1" if name in ("violations", "requests") else "")
        page = client.get(url).content.decode()
        assert reverse("help_topic", args=[slug]) in page, name
        assert "How this works" in page
    arc = client.get(reverse("arc_list") + "?mine=1").content.decode()
    assert reverse("help_topic", args=["architectural-review"]) in arc


def test_help_page_explains_each_menu_section(client, w):
    client.force_login(w["board"])
    page = client.get(reverse("help")).content.decode()
    assert "<h1>Help</h1>" in page
    assert "What&#x27;s where" in page or "What's where" in page
    assert helpdocs.SECTION_DESCRIPTIONS["My home"] in page
    assert helpdocs.SECTION_DESCRIPTIONS["Finance"] in page
    assert helpdocs.PAGE_DESCRIPTIONS["my_account"] in page
    client.force_login(w["tenant"])
    page = client.get(reverse("help")).content.decode()
    assert helpdocs.SECTION_DESCRIPTIONS["Finance"] not in page  # not their menu


# --- The board's own words ---


def test_board_text_is_escaped_and_append_only(client, w):
    body = (
        "<script>alert(1)</script> Hello **there**\n\n"
        "- [Documents](/documents/)\n- [bad](javascript:alert(1))\n- [ext](https://example.com/x)"
    )
    client.force_login(w["board"])
    response = client.post(reverse("help_text_save", args=["paying"]), {"body": body})
    assert response.status_code == 302
    client.force_login(w["owner"])
    page = client.get(reverse("help_topic", args=["paying"])).content.decode()
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page
    assert "<strong>there</strong>" in page
    assert '<a href="/documents/">Documents</a>' in page
    assert 'href="javascript' not in page
    assert 'href="https://example.com/x" rel="noopener noreferrer"' in page
    # A second save is a new version; the old one stays.
    helpdocs.save_text("paying", "Second version", actor=w["board"])
    assert HelpText.objects.filter(topic="paying").count() == 2
    assert "Second version" in client.get(reverse("help_topic", args=["paying"])).content.decode()
    with pytest.raises(AppendOnlyError):
        HelpText.objects.filter(topic="paying").update(body="x")
    from audit.models import AuditEvent

    events = AuditEvent.objects.filter(action="help.text_saved")
    assert events.count() == 2 and events.first().metadata["topic"] == "paying"


def test_only_the_board_edits_help_text(client, w):
    for who in ("owner", "tenant"):
        client.force_login(w[who])
        response = client.post(reverse("help_text_save", args=["paying"]), {"body": "Hi"})
        assert response.status_code in (403, 404), who
        assert (
            "Add the board&#x27;s own words"
            not in client.get(reverse("help_topic", args=["requests"])).content.decode()
        )
    assert not HelpText.objects.exists()


def test_invalid_text_is_shown_again_with_the_error(client, w):
    client.force_login(w["board"])
    long = "x" * (helpdocs.MAX_TEXT + 1)
    response = client.post(reverse("help_text_save", args=["requests"]), {"body": long})
    assert response.status_code == 400
    page = response.content.decode()
    assert (
        "keep it under" in page
        and long in page
        and '<details class="panel" id="edit-help" open' in page
    )
    assert not HelpText.objects.exists()


def test_render_text_never_passes_html():
    html = helpdocs.render_text('## T<b>\n"quote" & <img src=x onerror=y>\n[x](//evil.example/)')
    assert "<b>" not in html and "<img" not in html
    assert "&lt;img" in html and "&amp;" in html
    assert 'href="//' not in html
    assert html.startswith("<h3>T&lt;b&gt;</h3>")


# --- Search ---------------------------------------------------------------------------------------


def _help_hits(user, q):
    from core import search

    return [
        r.title
        for group in search.search(user, q)
        if group.area.key == "help"
        for r in group.results
    ]


def test_search_finds_help_pages_by_role(w):
    _rule("late_fee.cents", "2500")
    assert "How paying works" in _help_hits(w["owner"], "late fee")
    assert _help_hits(w["tenant"], "late fee") == []
    assert "How requests work" in _help_hits(w["tenant"], "business day")
    helpdocs.save_text("requests", "The pond key is at the clubhouse.", actor=w["board"])
    assert "How requests work" in _help_hits(w["tenant"], "pond key")
    assert _help_hits(w["board2"], "pond key") == ["How requests work"]


def test_search_leaves_help_out_when_switched_off(w):
    from core.models import FeatureSetting

    FeatureSetting.objects.create(key="owner_help", enabled=False)
    assert _help_hits(w["owner"], "late fee") == []


def test_faq_is_searchable_for_residents(w):
    from comms.models import FaqEntry

    FaqEntry.objects.create(
        question="Where is the mailbox key?", answer="At the clubhouse.", updated_by=w["board"]
    )
    from core import search

    for who in ("owner", "tenant", "view_only"):
        groups = {g.area.key for g in search.search(w[who], "mailbox key")}
        assert "faq" in groups, who


# --- Ask the board ---


@pytest.mark.parametrize("who", ["owner", "authorized", "tenant"])
def test_ask_the_board_opens_a_new_request(client, w, who):
    client.force_login(w[who])
    response = client.get(reverse("ask_board"))
    assert response.status_code == 302
    assert response["Location"] == reverse("requests") + "?mine=1&new=1#new-request"
    page = client.get(response["Location"]).content.decode()
    assert 'id="new-request" open' in page


def test_ask_from_a_help_page_preselects_the_kind(client, w):
    client.force_login(w["owner"])
    page = client.get(reverse("help_topic", args=["paying"])).content.decode()
    assert reverse("ask_board") + "?about=billing" in page
    response = client.get(reverse("ask_board"), {"about": "billing"})
    assert response["Location"].endswith("&about=billing#new-request")
    form = client.get(response["Location"]).content.decode()
    assert 'value="billing" required checked' in form
    assert 'value="general" required checked' not in form
    # Nonsense kinds are ignored.
    response = client.get(reverse("ask_board"), {"about": "<x>"})
    assert "about" not in response["Location"]


def test_view_only_contacts_and_staff_are_told_what_to_do(client, w):
    client.force_login(w["view_only"])
    page = client.get(reverse("ask_board")).content.decode()
    assert "<h1>Ask the board</h1>" in page and "authorized contacts can ask" in page
    client.force_login(w["board2"])
    page = client.get(reverse("ask_board")).content.decode()
    assert "Owner requests" in page


def test_ask_the_board_is_in_every_menu(client, w):
    for who in ("owner", "authorized", "view_only", "tenant", "board"):
        client.force_login(w[who])
        page = client.get(reverse("announcements")).content.decode()
        assert f'href="{reverse("ask_board")}"' in page, who
        assert 'class="topbar-help"' in page, who


@pytest.mark.parametrize(
    "name,args",
    [
        ("help", []),
        ("welcome", []),
        ("getting_started", []),
        ("ask_board", []),
        ("help_topic", ["paying"]),
    ],
)
def test_no_anonymous_access(client, name, args):
    response = client.get(reverse(name, args=args))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))
    assert client.post(reverse("help_text_save", args=["paying"]), {"body": "x"})[
        "Location"
    ].startswith(reverse("account_login"))
    assert not HelpText.objects.exists()


def test_demo_help_seed_is_fictional_and_repeatable(w):
    from core import demo_help

    demo_help.seed()
    assert set(HelpText.objects.values_list("topic", flat=True)) == set(demo_help.TEXTS)
    assert all("demo" in body for body in demo_help.TEXTS.values())
    demo_help.seed()
    assert HelpText.objects.count() == len(demo_help.TEXTS)
