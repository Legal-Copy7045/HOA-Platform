"""USPS tracking of paper mail: Informed Visibility scans (D60). A fake USPS
answers every request; nothing reaches the network. Synthetic data only."""

import datetime
import logging
import urllib.error
from pathlib import Path
from unittest import mock

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from comms import papermail, usps_iv, usps_tracking
from comms.models import (
    MailingEvent,
    MailingEventKind,
    MailPiece,
    MailPieceOutcome,
    MailPieceScan,
    UspsPollState,
)
from comms.tests.fake_usps import PASSWORD, USERNAME, FakeClock, FakeUsps, scan
from community.models import ContactProfile, Person, Role
from community.tests.factories import LotFactory, PersonFactory, make_member
from core import health, my_items
from core.append_only import AppendOnlyError
from core.models import FeatureSetting
from core.policy import SYSTEM
from core.scoping import system_access

pytestmark = pytest.mark.django_db


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def usps(settings, monkeypatch, clock):
    settings.USPS_IV_ENVIRONMENT = "cat"
    settings.USPS_IV_USERNAME = USERNAME
    settings.USPS_IV_PASSWORD = PASSWORD
    settings.USPS_IV_API_HOST = ""
    settings.USPS_IV_AUTH_HOST = ""
    fake = FakeUsps(clock)
    monkeypatch.setattr(usps_iv, "transport", fake)
    monkeypatch.setattr(usps_iv, "clock", clock)
    monkeypatch.setattr(usps_iv, "sleep", clock.sleep)
    monkeypatch.setattr(usps_iv, "pacer", usps_iv._Pacer())
    usps_iv.forget_token()
    yield fake
    usps_iv.forget_token()


def _feature(on=True):
    FeatureSetting.objects.update_or_create(key="usps_tracking", defaults={"enabled": on})


def _own(person, lot):
    from community import services as community
    from community.tests.factories import days_ago

    community.record_ownership(
        person=person, lot=lot, start_date=days_ago(100), ownership_type="sole", actor=SYSTEM
    )


@pytest.fixture
def w():
    board = make_member(board=True)
    manager = make_member(roles=[Role.MANAGER])
    sysadmin = make_member(roles=[Role.SYSADMIN], email="sysadmin@example.com")
    lots = [LotFactory(street_address=f"{n} Example Lane\nAnytown, PA 19380") for n in (10, 20, 30)]
    for n, lot in zip(("Pat Alpha", "Lee Bravo", "Kim Charlie"), lots, strict=True):
        _own(PersonFactory(display_name=n), lot)
    owner = make_member(lots=[LotFactory(street_address="99 Owner Way")])
    with system_access("test"):  # agreed to email notices: no paper letter
        ContactProfile.objects.update_or_create(
            person=Person.objects.get(user=owner),
            defaults={"electronic_notice_consent": True, "consent_changed_at": timezone.now()},
        )
    papermail.save_settings(actor=board, mailer_id="123456", imb_enabled=True)
    return {"board": board, "manager": manager, "sysadmin": sysadmin, "lots": lots, "owner": owner}


def _mailing(w, *, mailed_days_ago: int | None = 0, discard=False):
    mailing = papermail.create_mailing(
        actor=w["board"],
        defaults=papermail.source_defaults(),
        subject="Pool opening",
        body="Hello {owner_names}.",
        lot_id=None,
    )
    papermail.letters_pdf(mailing, duplex=False)  # numbers the pieces
    if discard:
        papermail.record_mailed(mailing, actor=w["board"], discard=True)
    elif mailed_days_ago is not None:
        with system_access("test"):
            MailingEvent.objects.create(
                mailing=mailing,
                kind=MailingEventKind.MAILED,
                mailed_on=timezone.localdate() - datetime.timedelta(days=mailed_days_ago),
                recorded_by=w["board"],
            )
    return mailing


def _pieces(mailing):
    with system_access("test"):
        return list(MailPiece.objects.filter(mailing=mailing).order_by("group"))


def _imb(piece):
    return piece.tracking + piece.routing


# --- Client: sign-in, tokens, errors ----------------------------------------------------


def test_sign_in_sends_the_documented_fields_and_keeps_the_token_in_memory(usps, clock):
    token = usps_iv.access_token()
    call = usps.auth_calls()[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://cat-services.usps.com/oauth/authenticate"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["body"] == {
        "username": USERNAME,
        "password": PASSWORD,
        "grant_type": "authorization",
        "response_type": "token",
        "scope": "user.info.ereg,iv1.apis",
        "client_id": "687b8a36-db61-42f7-83f7-11c79bf7785e",
    }
    # Reused while valid: no second sign-in.
    clock.now += 600
    assert usps_iv.access_token() == token
    assert len(usps.auth_calls()) == 1


def test_an_expiring_token_is_refreshed_then_a_refused_refresh_signs_in_again(usps, clock):
    first = usps_iv.access_token()
    clock.now += 900 - 30  # inside the renewal margin
    second = usps_iv.access_token()
    assert second != first
    refresh = [c for c in usps.calls if c["url"].endswith("/oauth/token")]
    assert len(refresh) == 1 and refresh[0]["body"]["grant_type"] == "authorization"
    assert refresh[0]["body"]["scope"] == "user.info.ereg,iv1.apis"
    assert len(usps.auth_calls()) == 1
    usps.refuse_refresh = True
    clock.now += 900
    third = usps_iv.access_token()
    assert third not in (first, second) and len(usps.auth_calls()) == 2


def test_a_changed_login_or_environment_starts_afresh(usps, settings):
    usps_iv.access_token()
    settings.USPS_IV_ENVIRONMENT = "production"
    usps_iv.access_token()
    assert usps.auth_calls()[-1]["url"] == "https://services.usps.com/oauth/authenticate"
    assert len(usps.auth_calls()) == 2


def test_a_refused_sign_in_is_an_auth_error(usps):
    usps.refuse_login = True
    with pytest.raises(usps_iv.UspsAuthError):
        usps_iv.access_token()


def test_piece_lookups_404_403_429_and_401(usps, clock):
    imb = "0031012345600000000119380"
    url = usps_iv.config().api_host
    assert url == "qiv.usps.com"
    usps.answer(imb, 404)
    assert usps_iv.piece_scans(imb) == usps_iv.PieceResult(found=False)
    assert usps.piece_calls()[0]["url"] == (
        f"https://qiv.usps.com/ivws_api/informedvisapi/api/mt/get/piece/imb/{imb}"
    )
    assert usps.piece_calls()[0]["headers"]["Authorization"].startswith("Bearer access-token-")
    usps.answer(imb, 403)
    assert usps_iv.piece_scans(imb).not_ours
    usps.answer(imb, 429)
    with pytest.raises(usps_iv.UspsRateLimited) as err:
        usps_iv.piece_scans(imb)
    assert err.value.retry_after == 30
    usps.answer(imb, 500)
    with pytest.raises(usps_iv.UspsError) as err:
        usps_iv.piece_scans(imb)
    assert err.value.status == 500
    # A token USPS no longer accepts: one new sign-in, then the answer.
    usps.tokens.clear()
    usps.answer(imb, ("scans", [scan("2026-11-20 08:15:00", "919")]))
    result = usps_iv.piece_scans(imb)
    assert result.found and result.scans[0].opcode == "919"
    assert len(usps.auth_calls()) == 2


@pytest.mark.parametrize(
    "host,ok",
    [
        ("iv.usps.com", True),
        ("cat-services.usps.com", True),
        ("usps.com", False),
        ("usps.com.example.com", False),
        ("evilusps.com", False),
        ("iv.usps.com:8443", False),
        ("IV.USPS.COM", False),
        ("iv.usps.com/", False),
        ("", False),
    ],
)
def test_only_usps_hostnames_are_accepted(host, ok):
    assert usps_iv.valid_host(host) is ok


def test_other_hosts_are_refused_before_any_request(usps, settings):
    settings.USPS_IV_API_HOST = "api.example.com"
    assert not usps_iv.config().configured
    with pytest.raises(usps_iv.UspsError):
        usps_iv.piece_scans("0031012345600000000119380")
    settings.USPS_IV_API_HOST = ""
    for url in (
        "https://example.com/oauth/authenticate",
        "http://cat-services.usps.com/oauth/authenticate",
        "https://cat-services.usps.com:444/oauth/authenticate",
        "https://user@cat-services.usps.com/oauth/authenticate",
        "https://iv.usps.com/ivws_api/",  # production host while on CAT
    ):
        with pytest.raises(usps_iv.UspsError) as err:
            usps_iv._send("GET", url, None, {}, "test")
        assert err.value.code == "bad_host"
    assert usps.calls == []


def test_redirects_are_refused():
    handler = usps_iv._NoRedirect()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(mock.Mock(full_url="https://iv.usps.com/x"), None, 302, "", {}, "")


def test_bad_barcodes_are_refused_without_a_request(usps):
    bad = ("123", "0031012345600000000119380/../x", "\u0660" * 20, "00310123456000000001193801")
    for imb in bad:
        with pytest.raises(usps_iv.UspsError):
            usps_iv.piece_scans(imb)
    assert usps.calls == []


def test_requests_are_paced_under_the_rate_limit(usps, clock):
    imb = "0031012345600000000119380"
    for _ in range(5):
        usps_iv.piece_scans(imb)
    # One sign-in and five lookups: every gap at least 60/400 s.
    assert usps_iv.MAX_PER_MINUTE <= 400
    gaps = clock.slept
    assert len(gaps) == 5 and all(abs(g - 60 / usps_iv.MAX_PER_MINUTE) < 1e-9 for g in gaps)


# --- Parsing ------------------------------------------------------------------------------


def test_parse_reads_the_envelope_and_skips_what_it_cannot_read():
    body = (
        b'{"data": [{"imb": "x", "scans": ['
        b'{"scan_date_time": "2026-11-20 08:15:00", "scan_event_code": "919",'
        b' "scan_facility_city": "PHILA<script>", "scan_facility_state": "pa",'
        b' "scan_facility_zip": "19176-9998", "mail_phase": "Phase 4c - Destination",'
        b' "handling_event_type": "L"},'
        b'{"scan_date_time": "not a date", "scan_event_code": "919"},'
        b'{"scan_date_time": "2026-11-19T22:01:00Z", "scan_event_code": ""},'
        b'"nonsense"]}], "message": "ok"}'
    )
    result = usps_iv.parse_piece(body)
    assert result.found and result.skipped == 3 and len(result.scans) == 1
    s = result.scans[0]
    assert s.opcode == "919" and s.facility_city == "PHILAscript" and s.facility_state == "PA"
    assert s.facility_zip == "19176-9998" and s.mail_phase.startswith("Phase 4c")
    assert timezone.is_aware(s.scanned_at)
    assert usps_iv.parse_piece(b'{"data": null, "message": "none"}').found is False
    with pytest.raises(usps_iv.UspsError):
        usps_iv.parse_piece(b"[1, 2]")


@pytest.mark.parametrize(
    "text",
    ["2026-11-20 08:15:00", "2026-11-20T08:15:00", "11/20/2026 08:15:00", "2026-11-20T13:15:00Z"],
)
def test_scan_times(text):
    parsed = usps_iv.parse_time(text)
    assert parsed is not None and timezone.localdate(parsed) == datetime.date(2026, 11, 20)


# --- Status --------------------------------------------------------------------------------


def _event(days_ago, kind="mailed"):
    return MailingEvent(
        kind=kind,
        mailed_on=None if kind != "mailed" else timezone.localdate() - datetime.timedelta(days_ago),
    )


def _s(opcode, day=1):
    return MailPieceScan(opcode=opcode, scanned_at=timezone.now() - datetime.timedelta(days=day))


@pytest.mark.parametrize(
    "closing,scans,outcomes,key,label",
    [
        (None, [], set(), "not_mailed", "Not marked as mailed"),
        (_event(0, "discarded"), [], set(), "not_sent", "Not sent"),
        (_event(1), [], set(), "mailed", "Mailed"),
        (_event(4), [], set(), "mailed", "Mailed"),
        (_event(5), [], set(), "missing", "No scans after 5 days"),
        (_event(2), [_s("895")], set(), "processed", "Processed"),
        (_event(9), [_s("895")], set(), "processed", "Processed"),
        (_event(3), [_s("895"), _s("919")], set(), "sorted", "Sorted for delivery on"),
        (_event(3), [_s("918"), _s("517")], set(), "delivered", "Delivered on"),
        (_event(31), [], set(), "gave_up", "Gave up after 30 days"),
        (_event(31), [_s("895", 25)], set(), "gave_up", "Gave up after 30 days"),
        (_event(30), [], set(), "missing", "No scans after 5 days"),
        (_event(40), [_s("919", 35)], set(), "sorted", "Sorted for delivery on"),
        (_event(2), [], {"not_ours"}, "not_ours", "USPS would not show it"),
    ],
)
def test_status_derivation(closing, scans, outcomes, key, label):
    status = usps_tracking.status_for(closing=closing, scans=scans, outcomes=outcomes)
    assert status.key == key and status.label.startswith(label)
    if scans:
        assert status.last_scan == max(s.scanned_at for s in scans)


def test_sorted_for_delivery_names_the_day():
    when = timezone.make_aware(datetime.datetime(2026, 11, 20, 9, 30))
    status = usps_tracking.status_for(
        closing=_event(3),
        scans=[MailPieceScan(opcode="919", scanned_at=when)],
        outcomes=set(),
    )
    assert status.label == "Sorted for delivery on Nov 20, 2026"


# --- Polling ------------------------------------------------------------------------------


def test_poll_stores_new_scans_once_and_stops_at_a_final_state(w, usps):
    _feature()
    mailing = _mailing(w, mailed_days_ago=1)
    a, b, c = _pieces(mailing)
    usps.answer(_imb(a), ("scans", [scan("2026-11-20 08:15:00", "895")]))
    usps.answer(
        _imb(b),
        ("scans", [scan("2026-11-20 08:15:00", "895"), scan("2026-11-21 03:02:00", "919")]),
    )
    usps.answer(_imb(c), 404)
    result = usps_tracking.poll()
    assert result == {
        "queried": 3,
        "new_scans": 3,
        "not_found": 1,
        "not_ours": 0,
        "outcome": "ok",
    }
    # Same answers again: nothing new is stored; b is sorted, so not asked again.
    usps.calls.clear()
    usps.answer(
        _imb(a),
        ("scans", [scan("2026-11-20 08:15:00", "895"), scan("2026-11-20 08:15:00", "895")]),
    )
    assert usps_tracking.poll()["new_scans"] == 0
    asked = {c["url"].rsplit("/", 1)[1] for c in usps.piece_calls()}
    assert asked == {_imb(a), _imb(c)}
    with system_access("t"):
        assert MailPieceScan.objects.count() == 3
        with pytest.raises(AppendOnlyError):
            MailPieceScan.objects.update(opcode="000")
    rows = usps_tracking.mailing_tracking(mailing)
    assert [r["status"].key for r in rows["rows"]] == ["processed", "sorted", "mailed"]
    assert rows["summary"] == "1 sorted for delivery, 1 processed, 1 not scanned yet, 0 missing"


def test_poll_asks_only_about_eligible_pieces(w, usps):
    _feature()
    eligible = _pieces(_mailing(w, mailed_days_ago=30))
    _mailing(w, mailed_days_ago=31)  # older than USPS keeps scans
    _mailing(w, mailed_days_ago=None)  # not marked as mailed
    _mailing(w, discard=True)  # not sent
    refused = _pieces(_mailing(w, mailed_days_ago=2))
    for p in refused:
        usps.answer(_imb(p), 403)
    usps_tracking.poll()
    with system_access("t"):
        assert MailPieceOutcome.objects.filter(kind="not_ours").count() == 3
    usps.calls.clear()
    usps_tracking.poll()
    asked = {c["url"].rsplit("/", 1)[1] for c in usps.piece_calls()}
    assert asked == {_imb(p) for p in eligible}


def test_poll_backs_off_once_on_429_then_stops(w, usps, clock):
    _feature()
    a, b, c = _pieces(_mailing(w, mailed_days_ago=1))
    usps.answer(_imb(a), 429, 404)
    usps.answer(_imb(b), 429)
    result = usps_tracking.poll()
    assert 30 in clock.slept  # waited as USPS asked
    assert result["outcome"] == "rate_limited" and result["queried"] == 1
    assert UspsPollState.get().last_failure == "rate_limited"
    assert not any(_imb(c) in call["url"] for call in usps.calls)


def test_a_refused_sign_in_stops_the_run_and_emails_sysadmins_once_a_day(w, usps):
    _feature()
    _mailing(w, mailed_days_ago=1)
    usps.refuse_login = True
    mail.outbox.clear()
    result = usps_tracking.poll()
    assert result["outcome"] == "auth" and result["queried"] == 0
    assert len(usps.auth_calls()) == 1 and not usps.piece_calls()
    usps_tracking.poll()
    sent = [m for m in mail.outbox if "USPS tracking" in m.subject]
    assert len(sent) == 1 and sent[0].to == ["sysadmin@example.com"]
    assert "set-usps.sh" in sent[0].body and PASSWORD not in sent[0].body
    ok, detail = usps_tracking.health()
    assert not ok and "refused the sign-in" in detail
    # The next day it may email again.
    later = timezone.now() + datetime.timedelta(days=1)
    usps_tracking.poll(now=later)
    assert len([m for m in mail.outbox if "USPS tracking" in m.subject]) == 2


def test_feature_off_or_no_login_means_no_requests(w, usps, settings):
    _mailing(w, mailed_days_ago=1)
    assert usps_tracking.poll()["outcome"] == "off"
    _feature()
    settings.USPS_IV_PASSWORD = ""  # nosec B105  # empty: "not set"
    assert usps_tracking.poll()["outcome"] == "off"
    assert usps.calls == []
    from comms import tasks

    settings.USPS_IV_PASSWORD = PASSWORD
    _feature(False)
    assert tasks.usps_tracking(timestamp=0)["outcome"] == "off"
    assert usps.calls == []


def test_the_task_is_periodic_on_the_maintenance_queue():
    from comms import tasks

    assert tasks.usps_tracking.queue == "maintenance"
    periodic = [
        t
        for t in tasks.app.periodic_registry.periodic_tasks.values()
        if t.task.name == tasks.usps_tracking.name
    ]
    assert periodic and periodic[0].cron == "41 */4 * * *"


# --- My items, health, set-up status -------------------------------------------------------


def test_my_items_once_per_letter_with_no_scan_after_5_days(w, usps):
    _feature()
    mailing = _mailing(w, mailed_days_ago=6)
    a, b, _ = _pieces(mailing)
    usps.answer(_imb(a), ("scans", [scan("2026-11-20 08:15:00", "895")]))
    usps_tracking.poll()
    usps_tracking.poll()
    titles = [i.title for i in my_items.items_for(w["manager"]) if "USPS scan" in i.title]
    assert sorted(titles) == [
        "Paper notice to 20 Example Lane has no USPS scan after 5 days — check the address",
        "Paper notice to 30 Example Lane has no USPS scan after 5 days — check the address",
    ]
    assert not [i for i in my_items.items_for(w["owner"]) if "USPS" in i.title]
    usps_tracking.mark_checked(b, actor=w["board"])
    with pytest.raises(ValueError):
        usps_tracking.mark_checked(b, actor=w["board"])
    titles = [i.title for i in my_items.items_for(w["board"]) if "USPS scan" in i.title]
    assert len(titles) == 1 and "30 Example Lane" in titles[0]
    event = AuditEvent.objects.get(action="mailpiece.checked")
    assert event.metadata == {"mailing_id": mailing.pk}
    _feature(False)
    assert not [i for i in my_items.items_for(w["board"]) if "USPS scan" in i.title]


def test_no_reminder_before_5_days_or_after_30(w, usps):
    _feature()
    _mailing(w, mailed_days_ago=4)
    _mailing(w, mailed_days_ago=31)
    assert usps_tracking.missing_pieces() == []


def test_health_check_states(w, usps, settings):
    def check():
        return next((r for r in health.run_checks() if r.key == "usps_tracking"), None)

    settings.USPS_IV_USERNAME = settings.USPS_IV_PASSWORD = ""  # nosec B105
    assert check() is None  # off and not set up: not shown
    _feature()
    assert check().ok and check().detail == "Not set up"
    settings.USPS_IV_USERNAME, settings.USPS_IV_PASSWORD = USERNAME, PASSWORD
    assert check().ok and "not checked yet" in check().detail
    usps_tracking.poll()
    assert check().ok and "checked 0 h ago" in check().detail
    state = UspsPollState.get()
    state.last_success_at = timezone.now() - datetime.timedelta(hours=25)
    state.save()
    assert not check().ok and "No successful check for 25 h" in check().detail
    usps.refuse_login = True
    _mailing(w, mailed_days_ago=1)
    usps_iv.forget_token()
    usps_tracking.poll()
    assert not check().ok and "refused the sign-in" in check().detail
    settings.USPS_IV_API_HOST = "iv.example.com"
    assert not check().ok and "usps.com" in check().detail


def test_every_piece_refused_is_a_failure(w, usps):
    _feature()
    for p in _pieces(_mailing(w, mailed_days_ago=1)):
        usps.answer(_imb(p), 403)
    assert usps_tracking.poll()["outcome"] == "refused"
    ok, detail = usps_tracking.health()
    assert not ok and "Mailer ID" in detail


def test_setup_status_wording(w, usps, settings):
    _feature()
    settings.USPS_IV_PASSWORD = ""  # nosec B105  # empty: "not set"
    status = usps_tracking.setup_status()
    assert status["state"] == "not_set_up" and "sudo bash infra/poc/set-usps.sh" in status["text"]
    settings.USPS_IV_PASSWORD = PASSWORD
    assert usps_tracking.setup_status()["text"].startswith(
        "Connected to the USPS test environment (CAT)."
    )
    settings.USPS_IV_ENVIRONMENT = "production"
    assert "Connected to USPS Informed Visibility" in usps_tracking.setup_status()["text"]


# --- Pages --------------------------------------------------------------------------------


def test_mailing_page_shows_status_per_letter_and_counts(client, w, usps):
    _feature()
    mailing = _mailing(w, mailed_days_ago=6)
    a, b, _ = _pieces(mailing)
    usps.answer(_imb(a), ("scans", [scan("2026-11-20 08:15:00", "919")]))
    usps.answer(_imb(b), ("scans", [scan("2026-11-20 08:15:00", "895")]))
    usps_tracking.poll()
    client.force_login(w["manager"])
    page = client.get(reverse("paper_mail_detail", args=[mailing.pk])).content.decode()
    assert 'data-testid="usps-tracking"' in page
    assert "1 sorted for delivery, 1 processed, 1 missing." in page
    assert "Sorted for delivery on Nov 20, 2026" in page and "No scans after 5 days" in page
    assert "usually delivered that day or the next" in page and "eDoc" in page
    assert "10 Example Lane, Anytown, PA 19380" in page
    # Address checked, from the page.
    response = client.post(
        reverse("paper_mail_piece_checked", args=[mailing.pk]), {"piece": _pieces(mailing)[2].pk}
    )
    assert response.status_code == 302
    page = client.get(reverse("paper_mail_detail", args=[mailing.pk])).content.decode()
    assert "Address checked</span>" in page


def test_a_piece_of_another_mailing_is_404(client, w, usps):
    _feature()
    first, second = _mailing(w), _mailing(w)
    client.force_login(w["board"])
    response = client.post(
        reverse("paper_mail_piece_checked", args=[first.pk]), {"piece": _pieces(second)[0].pk}
    )
    assert response.status_code == 404
    client.force_login(w["owner"])
    response = client.post(
        reverse("paper_mail_piece_checked", args=[first.pk]), {"piece": _pieces(first)[0].pk}
    )
    assert response.status_code in (403, 404)
    assert not MailPieceOutcome.objects.unscoped("t").exists()


def test_meeting_page_line_counts_letters_sorted_for_delivery(client, w, usps):
    from governance import services as governance

    _feature()
    meeting = governance.create_meeting(
        title="Fall meeting",
        kind="board",
        location="Clubhouse",
        starts_at=timezone.now() + datetime.timedelta(days=15),
        actor=w["board"],
    )
    defaults = papermail.source_defaults(meeting=meeting)
    mailing = papermail.create_mailing(
        actor=w["board"], defaults=defaults, subject=defaults["subject"], body=defaults["body"]
    )
    papermail.letters_pdf(mailing, duplex=False)
    day = timezone.localdate()
    papermail.record_mailed(mailing, actor=w["board"], mailed_on=day)
    first = _pieces(mailing)[0]
    usps.answer(_imb(first), ("scans", [scan("2026-11-20 08:15:00", "918")]))
    usps_tracking.poll()
    client.force_login(w["board"])
    page = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert f"Paper notices mailed {day:%b} {day.day}, {day.year} to 3 homes — 1 sorted" in page
    _feature(False)
    page = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert "sorted for delivery" not in page and "to 3 homes." in page


def test_feature_off_hides_tracking_everywhere(client, w, usps):
    mailing = _mailing(w, mailed_days_ago=6)
    client.force_login(w["board"])
    page = client.get(reverse("paper_mail_detail", args=[mailing.pk])).content.decode()
    assert 'data-testid="usps-tracking"' not in page and "Sorted for delivery" not in page
    page = client.get(reverse("paper_mail_settings")).content.decode()
    assert 'data-testid="usps-setup"' not in page
    response = client.post(
        reverse("paper_mail_piece_checked", args=[mailing.pk]), {"piece": _pieces(mailing)[0].pk}
    )
    assert response.status_code == 404
    assert usps_tracking.health() is not None  # login set: reported as switched off
    assert usps_tracking.health()[0] is True


def test_settings_page_shows_the_setup_status(client, w, usps, settings):
    _feature()
    client.force_login(w["manager"])
    page = client.get(reverse("paper_mail_settings")).content.decode()
    assert "Connected to the USPS test environment (CAT)." in page
    settings.USPS_IV_USERNAME = ""
    page = client.get(reverse("paper_mail_settings")).content.decode()
    assert "Not set up: a sysadmin runs sudo bash infra/poc/set-usps.sh" in page
    assert PASSWORD not in page


# --- Audit and logs: counts only ------------------------------------------------------------


def test_nothing_personal_or_secret_in_audit_or_logs(w, usps, caplog):
    _feature()
    mailing = _mailing(w, mailed_days_ago=6)
    pieces = _pieces(mailing)
    usps.answer(_imb(pieces[0]), ("scans", [scan("2026-11-20 08:15:00", "919")]))
    usps.answer(_imb(pieces[1]), 429, 403)
    with caplog.at_level(logging.DEBUG):
        usps_tracking.poll()
        usps_tracking.mark_checked(pieces[2], actor=w["board"])
    event = AuditEvent.objects.get(action="usps.polled")
    assert event.metadata == {
        "queried": 3,
        "new_scans": 1,
        "not_found": 1,
        "not_ours": 1,
        "outcome": "ok",
    }
    audit = str(
        list(AuditEvent.objects.filter(action__in=["usps.polled", "mailpiece.checked"]).values())
    )
    logs = caplog.text + str([r.__dict__ for r in caplog.records])
    tokens = list(usps.tokens) + list(usps.refresh_tokens)
    for secret in [PASSWORD, USERNAME, "Example Lane", "Alpha", "Bravo", *tokens] + [
        _imb(p) for p in pieces
    ]:
        assert secret not in audit and secret not in logs, secret
    # The token is held in memory only: not in the status row.
    state = str(UspsPollState.objects.filter(pk=1).values().first())
    assert not any(t in state for t in tokens)


# --- check_usps_iv and set-usps.sh ----------------------------------------------------------


def test_check_command_signs_in_only(usps, capsys):
    call_command("check_usps_iv")
    out = capsys.readouterr().out
    assert (
        out.strip() == "OK: signed in to the USPS test environment (CAT) (cat-services.usps.com)."
    )
    assert PASSWORD not in out and USERNAME not in out
    assert len(usps.auth_calls()) == 1 and not usps.piece_calls()


def test_check_command_reports_what_failed(usps, settings):
    usps.refuse_login = True
    with pytest.raises(CommandError, match="refused the sign-in") as err:
        call_command("check_usps_iv")
    assert PASSWORD not in str(err.value)
    settings.USPS_IV_PASSWORD = ""  # nosec B105  # empty: "not set"
    with pytest.raises(CommandError, match="not set up"):
        call_command("check_usps_iv")


def test_check_command_unreachable(usps, monkeypatch):
    def offline(*args):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(usps_iv, "transport", offline)
    with pytest.raises(CommandError, match="could not be reached"):
        call_command("check_usps_iv")


def test_set_usps_script_takes_the_password_hidden_and_checks_after_restart():
    script = Path("infra/poc/set-usps.sh").read_text()
    assert 'read -r -s -p "BCG password (hidden): " PASSWORD' in script
    assert 'ENVIRONMENT="${ENVIRONMENT:-cat}"' in script
    assert 'env_update .env USPS_IV_ENVIRONMENT "$ENVIRONMENT" USPS_IV_USERNAME' in script
    assert "USPS_IV_PASSWORD \"'$PASSWORD'\"" in script
    assert 'BACKUP="$(env_backup .env usps)"' in script
    assert script.index("start-portal.sh") < script.index("manage.py check_usps_iv")
    assert 'env_restore_keys .env "$BACKUP"' in script.split("check_usps_iv", 1)[1]
    # The password is never echoed, printed or passed to another program.
    for line in script.splitlines():
        if "PASSWORD" in line and not line.lstrip().startswith("#"):
            assert not line.lstrip().startswith(("echo", "printf")), line
            assert "$PASSWORD" not in line or line.lstrip().startswith(
                ("env_update", "if [[", "[ -n")
            ), line
    assert "unset PASSWORD" in script


def test_usps_settings_reach_the_app():
    compose = Path("docker-compose.yml").read_text()
    example = Path(".env.example").read_text()
    for key in ("USPS_IV_ENVIRONMENT", "USPS_IV_USERNAME", "USPS_IV_PASSWORD"):
        assert f"  {key}: ${{{key}:-" in compose
        assert f"\n{key}=" in example
    assert "\nUSPS_IV_USERNAME=\n" in example and "\nUSPS_IV_PASSWORD=\n" in example


def test_a_network_hiccup_fails_the_health_check_only_after_a_day(w, usps, monkeypatch):
    _feature()
    usps_tracking.poll()  # nothing to ask yet: a success
    _mailing(w, mailed_days_ago=1)

    def offline(*args):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(usps_iv, "transport", offline)
    assert usps_tracking.poll()["outcome"] == "network"
    ok, detail = usps_tracking.health()
    assert ok and "last check failed (USPS could not be reached), retrying" in detail
    state = UspsPollState.get()
    state.last_success_at = timezone.now() - datetime.timedelta(hours=30)
    state.save()
    ok, detail = usps_tracking.health()
    assert not ok and detail.startswith("No successful check for 30 h")


def test_web_container_knows_a_login_is_set_but_never_signs_in(usps, settings):
    """D87 (R2-007): only the worker gets the password; the web container is
    told one is set (status lines) and refuses to sign in without it."""
    settings.USPS_IV_PASSWORD = ""  # nosec B105  # the web container has none
    settings.USPS_IV_PASSWORD_SET = True
    assert usps_iv.config().configured
    with pytest.raises(usps_iv.UspsError):
        usps_iv.authenticate()
    assert usps.calls == []
    settings.USPS_IV_PASSWORD_SET = False
    assert not usps_iv.config().configured
