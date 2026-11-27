"""Community setup wizard (D51): who may use it, each step saving through
the owning service with its audit event, skip and back, reruns, the
data-driven rule steps, the review, My items, and no field for keys."""

from __future__ import annotations

import re
from unittest import mock

import pytest
from django.urls import reverse

from audit.models import AuditEvent
from community import services, setup
from community.models import CommunityProfile, GoverningRule, SetupStepRecord
from community.rules import DAYS, REGISTRY, RuleSpec
from community.tests.factories import LotFactory, make_member
from core import features
from core.append_only import AppendOnlyError
from core.models import FeatureSetting
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


@pytest.fixture
def board(client):
    user = make_member(board=True, lots=[LotFactory(street_address="1 Alpha Way")])
    client.force_login(user)
    return user


def step_url(key):
    return reverse("setup_step", args=[key])


def save(client, key, data=None, **extra):
    return client.post(step_url(key), {"action": "save", **(data or {})}, **extra)


def rule_data(*keys, **values):
    """Current values of a step's rules as the form would post them."""
    data = {}
    for key in keys:
        slug = key.replace(".", "-")
        data[f"{slug}__value"] = ""
        data[f"{slug}__citation"] = ""
    for key, value in values.items():
        data[key] = value
    return data


def events(action):
    return list(AuditEvent.objects.filter(action=action).order_by("occurred_at"))


# --- Who may use it ----------------------------------------------------------------------


def test_board_may_open_every_step(client, board):
    # Community setup is the front of Settings (WT-004); its old address opens it.
    assert client.get(reverse("setup_home"))["Location"] == reverse("settings_hub")
    assert client.get(reverse("settings_hub")).status_code == 200
    for key in setup.KEYS:
        response = client.get(step_url(key))
        assert response.status_code == 200, key
        assert f"Step {setup.number(key)} of {len(setup.STEPS)}" in response.content.decode()


def test_people_step_links_straight_to_the_boards_signups(client, board):
    """The board's sign-ups are a tab of Homes and owners; linking the old
    invitations URL only bounced through a redirect (CI page crawl)."""
    page = client.get(step_url("people")).content.decode()
    assert f'href="{reverse("lot_list")}?view=signups"' in page
    assert f'href="{reverse("invitations")}"' not in page


def test_everyone_else_is_refused(client):
    from core.tests.test_nav_streamline import role_users

    users = role_users(make_member(board=True))
    for role, user in users.items():
        if role == "board":
            continue
        client.force_login(user)
        for url in (reverse("setup_home"), step_url("charge"), step_url("review")):
            assert client.get(url).status_code in (403, 404), (role, url)
        response = client.post(reverse("setup_start"))
        assert response.status_code in (403, 404), role
        response = save(client, "charge", rule_data(**{"dues-monthly_cents__value": "99.00"}))
        assert response.status_code in (403, 404), role
    assert not GoverningRule.objects.filter(key="dues.monthly_cents").exists()
    assert not SetupStepRecord.objects.exists()


def test_sysadmin_has_no_access_and_no_hub_tile(client):
    from community.models import Role

    sysadmin = make_member(roles=[Role.SYSADMIN])
    client.force_login(sysadmin)
    assert client.get(reverse("setup_home")).status_code == 403
    assert client.get(step_url("server")).status_code == 403
    assert reverse("setup_home") not in client.get(reverse("settings_hub")).content.decode()


def test_signed_out_visitors_go_to_sign_in(client):
    for url in (reverse("setup_home"), step_url("community")):
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))


def test_settings_hub_offers_community_setup_to_the_board(client, board):
    """WT-004: Settings opens on Community setup (where the run stands and
    every step), then "Change one thing" links."""
    page = client.get(reverse("settings_hub")).content.decode()
    assert "<h1>Settings</h1>" in page and 'id="setup"' in page
    assert page.index('id="setup"') < page.index("Change one thing")
    for key in setup.KEYS:
        assert f'href="{step_url(key)}"' in page, key
    assert f'href="{reverse("community_settings")}"' in page


# --- Steps save through the services ----------------------------------------------------


def test_community_details_save_through_profile_and_letter_services(client, board):
    response = save(
        client,
        "community",
        {
            "legal_name": "Fictional Farms HOA",
            "fiscal_year_start_month": "1",
            "letterhead": "Fictional Farms HOA\nPO Box 1",
            "remit_to": "PO Box 1, Nowhere PA",
            "letter_contact": "office@example.com",
            "letter_signature": "",
        },
    )
    assert response.status_code == 302 and response["Location"] == step_url("branding")
    profile = CommunityProfile.get()
    assert profile.legal_name == "Fictional Farms HOA"
    assert profile.remit_to == "PO Box 1, Nowhere PA"
    assert len(events("community.profile_changed")) == 1
    assert len(events("community.letters_changed")) == 1
    done = events("setup.step_completed")
    assert [e.metadata for e in done] == [{"step": "community", "run": 1}]
    assert events("setup.run_started")[0].metadata == {"run": 1}


def test_unchanged_profile_is_not_saved_again(client, board):
    profile = CommunityProfile.get()
    save(
        client,
        "community",
        {"legal_name": profile.legal_name, "fiscal_year_start_month": "1"},
    )
    assert not events("community.profile_changed")
    assert not events("community.letters_changed")


def test_invalid_details_re_render_with_errors_linked(client, board):
    response = save(
        client, "community", {"legal_name": "Typed-7731", "fiscal_year_start_month": "13"}
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert "Nothing has been saved yet" in html and "Typed-7731" in html
    assert 'aria-invalid="true"' in html and "id_fiscal_year_start_month_error" in html
    assert not SetupStepRecord.objects.filter(step="community").exists()


def test_rule_step_saves_value_and_citation_with_audit(client, board):
    data = rule_data(
        *[s.key for s in setup.rules_for("charge")],
        **{
            "dues-monthly_cents__value": "45.00",
            "dues-monthly_cents__citation": "Declaration §6.1",
            "dues-due_day__value": "5",
            "dues-due_day__citation": "Declaration §6.3",
        },
    )
    response = save(client, "charge", data)
    assert response["Location"] == step_url("late_fees")
    row = GoverningRule.objects.get(key="dues.monthly_cents")
    assert (row.value, row.citation) == ("4500", "Declaration §6.1")
    assert GoverningRule.objects.get(key="dues.due_day").value == "5"
    changed = {e.metadata["key"]: e.metadata for e in events("community.rule_changed")}
    assert set(changed) == {"dues.monthly_cents", "dues.due_day"}  # only what changed
    assert changed["dues.monthly_cents"]["has_citation"] is True


def test_rule_step_refuses_bad_input_without_saving_anything(client, board):
    data = rule_data(
        *[s.key for s in setup.rules_for("charge")],
        **{"dues-monthly_cents__value": "forty", "dues-due_day__value": "7"},
    )
    response = save(client, "charge", data)
    assert response.status_code == 200
    html = response.content.decode()
    assert "Enter the amount in dollars" in html and 'value="forty"' in html
    assert not GoverningRule.objects.filter(key="dues.due_day").exists()


def test_big_change_to_the_monthly_charge_asks_first(client, board):
    services.set_rule(key="dues.monthly_cents", value="4500", citation="Decl §6", actor=SYSTEM)
    data = rule_data(
        *[s.key for s in setup.rules_for("charge")],
        **{"dues-monthly_cents__value": "90.00", "dues-monthly_cents__citation": "Decl §6"},
    )
    response = save(client, "charge", data)
    assert response.status_code == 200 and b"Please confirm" in response.content
    assert GoverningRule.objects.get(key="dues.monthly_cents").value == "4500"
    response = save(client, "charge", {**data, "confirm": "yes"})
    assert response.status_code == 302
    assert GoverningRule.objects.get(key="dues.monthly_cents").value == "9000"


def test_rule_notes_are_kept(client, board):
    services.set_rule(
        key="dues.due_day", value="1", citation="Decl", notes="Adopted 2026", actor=SYSTEM
    )
    data = rule_data(
        *[s.key for s in setup.rules_for("charge")],
        **{"dues-due_day__value": "2", "dues-due_day__citation": "Decl"},
    )
    save(client, "charge", data)
    row = GoverningRule.objects.get(key="dues.due_day")
    assert (row.value, row.notes) == ("2", "Adopted 2026")


def test_each_rule_shows_its_pa_guidance_and_citation_field(client, board):
    from community.pa_law import guide

    for key in ("charge", "late_fees", "arc", "violations", "privacy", "signin", "notices"):
        html = client.get(step_url(key)).content.decode()
        for spec in setup.rules_for(key):
            slug = spec.key.replace(".", "-")
            assert f'name="{slug}__citation"' in html, spec.key
            g = guide(spec.key)
            if g:
                assert g.example.replace("'", "&#x27;").replace('"', "&quot;") in html, spec.key


def test_feature_switches_go_through_set_feature(client, board):
    keys = [f.key for f in features.FEATURES]
    data = {f"feature__{k}": "on" for k in keys if features.enabled(k)}
    data["feature__reservations"] = "on"
    response = save(client, "features", data)
    assert response.status_code == 302
    assert features.enabled("reservations")
    switched = events("feature.switched")
    assert [e.metadata for e in switched] == [{"feature": "reservations", "enabled": True}]


def test_switching_a_feature_off_asks_first(client, board):
    keys = [f.key for f in features.FEATURES]
    data = {f"feature__{k}": "on" for k in keys if features.enabled(k) and k != "violations"}
    response = save(client, "features", data)
    assert response.status_code == 200 and b"Switch off Violations?" in response.content
    assert features.enabled("violations")
    save(client, "features", {**data, "confirm": "yes"})
    assert not features.enabled("violations")


def test_step_features_are_only_those_in_core_features(client, board):
    for step in setup.STEPS:
        for key in step.features:
            assert key in features.BY_KEY, (step.key, key)
    html = client.get(step_url("features")).content.decode()
    for f in features.FEATURES:
        assert f'name="feature__{f.key}"' in html


def test_common_areas_step_saves_without_adding_an_area(client, board):
    response = save(client, "common_areas", {"area-name": "", "area-kind": ""})
    assert response.status_code == 302
    assert setup.step_states(1) == {"common_areas": "completed"}


def test_common_area_added_through_the_service(client, board):
    save(client, "common_areas", {"area-name": "Pond", "area-kind": "stormwater"})
    from community.models import CommonArea

    assert CommonArea.objects.filter(name="Pond").exists()
    assert events("community.common_area_saved")


# --- Skip, back, progress ----------------------------------------------------------------


def test_skip_records_and_moves_on_without_saving(client, board):
    response = client.post(
        step_url("charge"), {"action": "skip", "dues-monthly_cents__value": "12.00"}
    )
    assert response["Location"] == step_url("late_fees")
    assert not GoverningRule.objects.exists()
    assert setup.step_states(1) == {"charge": "skipped"}
    assert events("setup.step_skipped")[0].metadata == {"step": "charge", "run": 1}


def test_back_links_to_the_previous_step(client, board):
    html = client.get(step_url("late_fees")).content.decode()
    assert f'href="{step_url("charge")}">Back' in html
    html = client.get(step_url("community")).content.decode()
    assert f'href="{reverse("settings_hub")}#setup">Back' in html


def test_progress_bar_and_sticky_actions(client, board):
    html = client.get(step_url("arc")).content.decode()
    n = setup.number("arc")
    assert f'<progress max="{len(setup.STEPS)}" value="{n}"' in html
    assert 'class="setup-actions"' in html and "Skip for now" in html
    assert "<fieldset" in html and "<legend>" in html


def test_records_are_append_only(board):
    run = setup.start_run(actor=board)
    record = SetupStepRecord.objects.get(run=run)
    with pytest.raises(AppendOnlyError):
        record.save()
    with pytest.raises(AppendOnlyError):
        SetupStepRecord.objects.all().delete()


# --- Reruns ------------------------------------------------------------------------------


def test_rerun_counts_up_and_prefills_current_values(client, board):
    services.set_rule(key="arc.review_days", value="30", citation="Decl §9.2", actor=SYSTEM)
    client.post(reverse("setup_start"))
    client.post(step_url("review"))
    assert setup.finished(1)
    # Changed after run 1 finished:
    services.set_rule(key="arc.appeal_days", value="15", citation="Decl §9.4", actor=SYSTEM)
    page = client.get(reverse("settings_hub")).content.decode()
    assert "Run setup again" in page
    response = client.post(reverse("setup_start"))
    assert response["Location"] == step_url("community")
    assert setup.current_run() == 2
    assert events("setup.run_started")[-1].metadata == {"run": 2}
    html = client.get(step_url("arc")).content.decode()
    assert 'value="30"' in html and 'value="Decl §9.2"' in html
    appeal = html.split('id="rule-arc.appeal_days"')[1].split('class="setup-rule"')[0]
    review = html.split('id="rule-arc.review_days"')[1].split('class="setup-rule"')[0]
    assert "Changed since the last setup" in appeal
    assert "Changed since the last setup" not in review


def test_start_continues_an_open_run(client, board):
    client.post(reverse("setup_start"))
    client.post(reverse("setup_start"))
    assert setup.current_run() == 1
    assert len(events("setup.run_started")) == 1


# --- Data-driven grouping ----------------------------------------------------------------


def test_every_rule_lands_in_exactly_one_step():
    assigned = setup.assign_rules()
    keys = [s.key for specs in assigned.values() for s in specs]
    assert sorted(keys) == sorted(REGISTRY)
    assert {s.key for s in assigned["charge"]} >= {
        "charge.term",
        "dues.monthly_cents",
        "dues.due_day",
        "dues.auto_post",
    }
    assert "late_fee.cents" in {s.key for s in assigned["late_fees"]}
    assert "payments.credit_card_surcharge_bp" in {s.key for s in assigned["payments"]}
    assert "notices.electronic_consent_required" in {s.key for s in assigned["notices"]}
    assert {s.key for s in assigned["signin"]} == {"recovery.wait_hours", "recovery.lock_hours"}


def test_a_new_rule_shows_up_in_its_groups_step(client, board):
    fake = {
        "payments.autopay_fake_limit": RuleSpec(
            "payments.autopay_fake_limit", "Fake autopay limit", DAYS, None, "Online payments"
        ),
        "dues.fake_alert": RuleSpec("dues.fake_alert", "Fake posting alert", DAYS, None, "Dues"),
        "late_fee.fake": RuleSpec("late_fee.fake", "Fake late rule", DAYS, None, "Dues"),
        "brand.new": RuleSpec("brand.new", "Rule of a brand-new group", DAYS, None, "Brand new"),
    }
    with mock.patch.dict(REGISTRY, fake):
        assigned = {k: {s.key for s in v} for k, v in setup.assign_rules().items()}
        assert "payments.autopay_fake_limit" in assigned["payments"]
        assert "dues.fake_alert" in assigned["charge"]
        assert "late_fee.fake" in assigned["late_fees"]
        assert "brand.new" in assigned["governance"]  # the catch-all step
        html = client.get(step_url("payments")).content.decode()
        assert "Fake autopay limit" in html
        response = save(
            client,
            "payments",
            {
                **rule_data(*assigned["payments"]),
                "payments-autopay_fake_limit__value": "9",
                "payments-autopay_fake_limit__citation": "Board resolution 1",
            },
        )
        assert response.status_code == 302
    assert GoverningRule.objects.get(key="payments.autopay_fake_limit").value == "9"


# --- Review ------------------------------------------------------------------------------


def test_review_flags_defaults_missing_values_and_citations(client, board):
    services.set_rule(key="arc.review_days", value="30", citation="", actor=SYSTEM)
    services.set_rule(key="arc.appeal_days", value="15", citation="Decl §9.4", actor=SYSTEM)
    html = client.get(step_url("review")).content.decode()
    arc = html.split('id="review-arc"')[1].split("</section>")[0]
    assert re.search(r"Days to decide a complete ARC request</dt><dd>30 .*No citation", arc)
    assert re.search(r"appeal an ARC decision</dt><dd>15 <span[^>]*>\(Decl §9.4\)</span></dd>", arc)
    assert "Not set" in arc  # arc.completeness_days
    charge = html.split('id="review-charge"')[1].split("</section>")[0]
    assert "PA law fallback" in charge  # charge.term
    assert "Default" in charge  # dues.auto_post
    assert f'href="{step_url("arc")}"' in arc
    details = html.split('id="review-community"')[1].split("</section>")[0]
    assert "Missing" in details  # no letterhead yet


def test_finish_marks_the_run_finished(client, board):
    response = client.post(step_url("review"))
    assert response["Location"] == reverse("settings_hub") + "#setup"
    assert setup.finished(1)
    assert events("setup.run_finished")[0].metadata == {"run": 1}


# --- My items ----------------------------------------------------------------------------


def _setup_items(user):
    from core.my_items import items_for

    return [i for i in items_for(user) if i.title.startswith("Finish community setup")]


def test_my_items_until_setup_is_finished(client, board):
    items = _setup_items(board)
    assert len(items) == 1
    # One banner with the rules still missing a citation in it (WT-004).
    assert items[0].title.startswith(f"Finish community setup: {len(setup.WORK_STEPS)} steps left")
    assert items[0].url == reverse("settings_hub") + "#setup"
    client.post(reverse("setup_start"))
    client.post(step_url("branding"), {"action": "save"})
    client.post(step_url("people"), {"action": "skip"})
    assert _setup_items(board)[0].title.startswith(
        f"Finish community setup: {len(setup.WORK_STEPS) - 1} steps left"
    )
    client.post(step_url("review"))
    assert _setup_items(board) == []
    client.post(reverse("setup_start"))  # a rerun is open again
    assert len(_setup_items(board)) == 1


def test_my_items_entry_is_board_only():
    from community.models import Role

    manager = make_member(roles=[Role.MANAGER])
    owner = make_member(lots=[LotFactory()])
    assert _setup_items(manager) == [] and _setup_items(owner) == []


# --- Secrets ------------------------------------------------------------------------------


def test_no_field_accepts_keys_or_secrets(client, board):
    for key in setup.KEYS:
        html = client.get(step_url(key)).content.decode().lower()
        names = re.findall(r'name="([^"]+)"', html)
        for name in names:
            if name == "csrfmiddlewaretoken":
                continue
            assert not re.search(r"key|secret|token|password|vapid|stripe|teller", name), (
                key,
                name,
            )
    html = client.get(step_url("payments")).content.decode()
    assert "sudo bash infra/poc/set-stripe-key.sh" in html


def test_server_step_shows_usps_setup_and_features_lists_usps_tracking(client, board):
    html = client.get(step_url("server")).content.decode()
    assert "USPS tracking" in html
    assert "sudo bash infra/poc/set-usps.sh" in html  # not set up in tests
    html = client.get(step_url("features")).content.decode()
    assert 'name="feature__usps_tracking"' in html


def test_posted_secrets_are_ignored(client, board, settings):
    fake = "sk_test_" + "x" * 8  # synthetic, never a real key
    settings.STRIPE_SECRET_KEY = None
    save(client, "server", {"STRIPE_SECRET_KEY": fake, "stripe_key": fake})
    save(client, "payments", {"stripe_key": fake})
    from django.conf import settings as live

    assert live.STRIPE_SECRET_KEY is None
    assert fake not in str(list(AuditEvent.objects.values_list("metadata", flat=True)))
    assert not FeatureSetting.objects.filter(key__icontains="stripe").exists()


def test_audit_metadata_is_step_and_run_only(client, board):
    client.post(reverse("setup_start"))
    client.post(step_url("branding"), {"action": "save"})
    client.post(step_url("people"), {"action": "skip"})
    client.post(step_url("review"))
    for action in (
        "setup.run_started",
        "setup.step_completed",
        "setup.step_skipped",
        "setup.run_finished",
    ):
        for event in events(action):
            assert set(event.metadata) <= {"step", "run"}, action


# --- What the law says --------------------------------------------------------------------


def test_every_step_has_law_or_an_explicit_no_specific_law_note():
    from community import us_law
    from community.setup_views import law_panel

    for step in setup.STEPS:
        panel = law_panel(step)
        assert panel["pa"] or panel["notes"] or panel["none"], step.key
    assert set(us_law.STEP_LAW) | set(us_law.NO_SPECIFIC_LAW) <= set(setup.KEYS)


def test_every_law_entry_has_a_citation_and_the_counsel_caveat():
    from community import us_law

    for step, notes in us_law.STEP_LAW.items():
        for note in notes:
            assert note.citation.strip(), (step, note.title)
            assert note.summary and note.meaning, (step, note.title)
            assert "counsel" in note.caveat.lower(), (step, note.title)


def test_law_panel_and_footer_on_every_page(client, board):
    from community.us_law import FOOTER

    for key in setup.KEYS:
        html = client.get(step_url(key)).content.decode()
        assert "What the law says" in html, key
        assert FOOTER.replace("'", "&#x27;") in html, key
    html = client.get(step_url("notices")).content.decode()
    assert "15 U.S.C. § 7001(c)" in html and "47 U.S.C. § 227" in html
    html = client.get(step_url("payments")).content.decode()
    assert "12 C.F.R. § 1005.10(b)" in html
    html = client.get(step_url("arc")).content.decode()
    assert "42 U.S.C. § 3604(f)(3)" in html and "47 C.F.R. § 1.4000" in html
