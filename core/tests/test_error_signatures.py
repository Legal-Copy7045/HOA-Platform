"""D45: server error signatures (what the errors were, without personal data)."""

import datetime
from unittest import mock

import pytest
from django.core import mail
from django.http import HttpResponse
from django.test import Client
from django.urls import path, reverse
from django.utils import timezone

from community.models import Role
from community.tests.factories import make_member
from core import errors, health
from core.email import EmailUnavailable
from core.models import ErrorSignature, ErrorTally
from core.urls import urlpatterns as core_urlpatterns

pytestmark = [pytest.mark.django_db, pytest.mark.urls("core.tests.test_error_signatures")]

DISTINCTIVE_MESSAGE = "zebra-words-7731 owner@example.org"


def boom(request, pk):
    data = {}
    try:
        return data["missing"]
    except KeyError:
        raise KeyError(DISTINCTIVE_MESSAGE) from None


def returns_500(request):
    return HttpResponse("nope", status=500)


def returns_503(request):
    return HttpResponse("busy", status=503)


def email_down(request):
    raise EmailUnavailable(DISTINCTIVE_MESSAGE)


urlpatterns = [
    path("test-errors/boom/<int:pk>/", boom, name="test_boom"),
    path("test-errors/500/", returns_500, name="test_returns_500"),
    path("test-errors/503/", returns_503, name="test_returns_503"),
    path("test-errors/email/", email_down, name="test_email_down"),
    *core_urlpatterns,
]


@pytest.fixture
def signed_in():
    client = Client(raise_request_exception=False)
    client.force_login(make_member(lots=[]))
    return client


def _row_text(row) -> str:
    return " ".join(
        str(getattr(row, f))
        for f in ("exc_type", "route", "method", "where", "status", "release", "fingerprint")
    )


def test_raised_exception_records_class_route_and_app_frame_but_no_message_or_path(
    signed_in, settings
):
    settings.APP_RELEASE = "9.8.7"
    response = signed_in.get("/test-errors/boom/4242/?token=quokka-99")
    assert response.status_code == 500
    row = ErrorSignature.objects.get()
    assert row.exc_type == "KeyError"
    assert row.route == "test_boom"
    assert row.method == "GET"
    assert row.status == 500
    assert row.release == "9.8.7"
    assert row.where.startswith("core/tests/test_error_signatures.py:")
    assert row.where.endswith(" in boom")
    assert row.count == 1 and row.first_seen == row.last_seen
    text = _row_text(row)
    for leaked in ("zebra-words", "owner@example.org", "4242", "quokka", "test-errors/"):
        assert leaked not in text
    assert sum(ErrorTally.objects.values_list("count", flat=True)) == 1

    # ... and the alert email names it, still without the message or path.
    make_member(roles=[Role.SYSADMIN])
    _ok, detail = health._errors()
    health.alert([health.Result("errors", "Server errors", False, detail)])
    body = mail.outbox[-1].body
    assert "  • 1\u00d7 A page (test_boom) failed because of a bug in the portal's code." in body
    assert "Technical: KeyError in core/tests/test_error_signatures.py:" in body
    assert "in boom (GET test_boom), HTTP 500, release 9.8.7" in body
    for leaked in ("zebra-words", "owner@example.org", "4242", "quokka", "test-errors/"):
        assert leaked not in body
    assert "/ops/status/" in body


@pytest.mark.parametrize(
    ("url", "status", "route"),
    [
        ("/test-errors/500/", 500, "test_returns_500"),
        ("/test-errors/503/", 503, "test_returns_503"),
    ],
    ids=["500", "503"],
)
def test_returned_5xx_without_exception_is_recorded_by_status_and_route(
    signed_in, url, status, route
):
    assert signed_in.get(url).status_code == status
    row = ErrorSignature.objects.get()
    assert (row.exc_type, row.route, row.where, row.status) == (
        f"HTTP {status} response",
        route,
        "",
        status,
    )


def test_healthz_database_down_is_labelled():
    client = Client()
    broken = mock.MagicMock()
    broken.cursor.side_effect = RuntimeError("db gone")
    with mock.patch("core.views.connection", broken):
        response = client.get("/healthz")
    assert response.status_code == 503
    row = ErrorSignature.objects.get()
    assert row.exc_type == errors.HEALTHZ_DB_DOWN == "Health probe: database unavailable"
    assert row.route == "healthz" and row.status == 503


def test_email_unavailable_page_is_labelled(signed_in):
    response = signed_in.get("/test-errors/email/")
    assert response.status_code == 503
    row = ErrorSignature.objects.get()
    assert row.exc_type == errors.EMAIL_UNAVAILABLE == "Email sending unavailable page"
    assert row.route == "test_email_down"
    assert "zebra-words" not in _row_text(row)


def test_same_error_counts_up_within_the_hour_and_starts_afresh_next_hour(signed_in):
    signed_in.get("/test-errors/500/")
    signed_in.get("/test-errors/500/")
    signed_in.get("/test-errors/503/")
    assert ErrorSignature.objects.count() == 2
    assert ErrorSignature.objects.get(status=500).count == 2

    later = timezone.now() + datetime.timedelta(hours=1)
    request = mock.Mock(method="GET", resolver_match=mock.Mock(view_name="test_returns_500"))
    request._error_label = None
    request._error_capture = None
    errors.record(request, 500, now=later)
    rows = ErrorSignature.objects.filter(status=500)
    assert rows.count() == 2
    merged = errors.summary(timezone.now() - datetime.timedelta(hours=3))
    by_status = {r["status"]: r for r in merged}
    assert by_status[500]["total"] == 3 and by_status[503]["total"] == 1
    assert by_status[500]["last"] == later


def _signature(n, total, now):
    first = now.replace(minute=2)
    return ErrorSignature.objects.create(
        hour=now.replace(minute=0, second=0, microsecond=0),
        fingerprint=f"fp{n}",
        exc_type=f"Error{n}",
        route=f"route_{n}",
        method="GET",
        where=f"ledger/views.py:{100 + n} in view_{n}",
        status=500,
        release="0.8.0",
        count=total,
        first_seen=first,
        last_seen=now.replace(minute=58),
    )


def test_alert_lists_the_top_five_kinds_in_site_time_and_how_many_more():
    make_member(roles=[Role.SYSADMIN])
    now = datetime.datetime(2026, 11, 26, 19, 59, tzinfo=datetime.UTC)
    for n, total in enumerate([1, 18, 2, 7, 3, 4, 5]):
        _signature(n, total, now)
    health.alert(
        [health.Result("errors", "Server errors", False, "40 server error(s) in the last hour")],
        now=now,
    )
    body = mail.outbox[-1].body
    lines = body.splitlines()
    start = lines.index("PROBLEM  Server errors: 40 server error(s) in the last hour")
    listed = lines[start + 1 : start + 12]
    assert listed[0] == (
        "  • 18\u00d7 A page (route_1) failed because of a bug in the portal's code. "
        "People saw an error page. Needs a code fix: the portal's maintainer has the "
        "technical details below."
    )
    assert listed[1] == (
        "      Technical: Error1 in ledger/views.py:101 in view_1 (GET route_1), HTTP 500, "
        "release 0.8.0, 14:02\u201314:58 (America/New_York)"
    )
    counts = [line.split("\u00d7")[0].strip(" •") for line in listed[0:10:2]]
    assert counts == ["18", "7", "5", "4", "3"]
    assert listed[10] == "  • and 2 more kinds (see System status)"
    assert "Details: " in body and "/ops/status/" in body


def test_alert_without_signatures_still_goes_out():
    make_member(roles=[Role.SYSADMIN])
    health.alert([health.Result("errors", "Server errors", False, "21 server error(s)")])
    assert "PROBLEM  Server errors" in mail.outbox[-1].body
    assert "more kind" not in mail.outbox[-1].body


def test_failure_while_recording_never_breaks_the_response(signed_in):
    with mock.patch("core.errors.record", side_effect=RuntimeError("broken")):
        response = signed_in.get("/test-errors/503/")
    assert response.status_code == 503 and response["X-Request-ID"]
    with mock.patch("core.errors.capture_exception", side_effect=RuntimeError("broken")):
        response = signed_in.get("/test-errors/boom/1/")
    assert response.status_code == 500 and response["X-Request-ID"]


def test_retention_purge_removes_only_old_rows():
    now = timezone.now()
    old = now - datetime.timedelta(days=errors.RETENTION_DAYS + 1)
    _signature(1, 3, old)
    _signature(2, 3, now)
    ErrorTally.objects.create(hour=old.replace(minute=0, second=0, microsecond=0), count=3)
    ErrorTally.objects.create(hour=now.replace(minute=0, second=0, microsecond=0), count=3)
    assert errors.purge(now) == 2
    assert list(ErrorSignature.objects.values_list("fingerprint", flat=True)) == ["fp2"]
    assert ErrorTally.objects.count() == 1

    from core.tasks import purge_server_errors

    assert purge_server_errors.name == "core.tasks.purge_server_errors"


def test_clearing_the_errors_check_forgets_recent_signatures():
    _signature(1, 30, timezone.now())
    health.clear("errors")
    assert not ErrorSignature.objects.exists()


def test_status_page_shows_recent_errors_card_to_sysadmins_only():
    _signature(1, 5, timezone.now())
    client = Client()
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    page = client.get(reverse("system_status")).content.decode()
    assert "Recent server errors" in page
    assert "A page (route_1) failed because of a bug in the portal" in page
    assert "Technical: Error1" in page and "ledger/views.py:101 in view_1" in page
    assert "route_1" in page and "release 0.8.0" in page
    assert 'class="list health-list error-list"' in page  # wraps on phones
    for who in (make_member(board=True), make_member(lots=[])):
        client.force_login(who)
        assert client.get(reverse("system_status")).status_code == 403


def test_status_page_says_when_there_were_none():
    client = Client()
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    page = client.get(reverse("system_status")).content.decode()
    assert "No server errors in the last 24 hours." in page


def test_where_prefers_our_code_and_never_carries_source_or_locals():
    def inner():
        hidden_local = "quokka-local"  # noqa: F841
        raise ValueError("message-never-stored")

    try:
        inner()
    except ValueError as exc:
        where = errors.where_from_traceback(exc.__traceback__)
    assert where.startswith("core/tests/test_error_signatures.py:") and where.endswith(" in inner")
    assert "quokka" not in where and "message" not in where


def _plain(exc_type, route, status=500):
    from core.error_words import sentence

    return sentence({"exc_type": exc_type, "route": route, "status": status})


def test_plain_words_name_the_page_the_cause_who_noticed_and_what_to_do():
    assert _plain("ValueError", "pwa_service_worker") == (
        "The installable-app update check failed because of a bug in the portal's code. "
        "It runs in the background on signed-in pages, so people didn't see an error page. "
        "Needs a code fix: the portal's maintainer has the technical details below."
    )
    db = _plain("OperationalError", "lot_ledger")
    assert db.startswith("A home's account page failed because the database could not be reached.")
    assert "People saw an error page." in db and "Check that the database is running" in db
    probe = _plain(errors.HEALTHZ_DB_DOWN, "healthz", 503)
    assert probe.startswith("The server's health probe failed because the database could not")
    assert "people didn't see an error page" in probe
    mail_down = _plain(errors.EMAIL_UNAVAILABLE, "some_form", 503)
    assert "because email sending is down" in mail_down
    assert "People saw a page saying their email could not be sent." in mail_down
    busy = _plain("HTTP 503 response", "ledger_home", 503)
    assert busy.startswith('The "Owner accounts" page failed because the portal reported itself')
    assert "No action if it stops by itself" in busy
    assert "did not answer" in _plain("TimeoutError", "home")


def test_unknown_pages_and_errors_fall_back_to_a_sensible_sentence():
    assert _plain("FrobnicationError", "mystery_view").startswith(
        "A page (mystery_view) failed because of a bug in the portal's code. "
        "People saw an error page. Needs a code fix"
    )
    assert _plain("HTTP 500 response", "").startswith(
        "A page failed because of a bug in the portal's code."
    )
