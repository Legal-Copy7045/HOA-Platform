"""Settings -> Demo data (decision D53): the page, the service and the
Settings entry exist only where demo data is allowed; sysadmins only; the
word RESEED; the task row carries a dataset from the registry; audit; the
production warnings; removing the old data's files. The agent's side is in
test_host_tasks_agent.py."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from django.core.checks import run_checks
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import make_member
from core import demo_reseed, health, host_tasks, navigation
from core.models import DemoSeed, HostTask, UpdateStatus

pytestmark = pytest.mark.django_db

URL = "/ops/demo-data/"
CONFIRM = (
    "Everything in the portal is replaced by fictional data. A backup is taken first. "
    "Real logins (yours and other non-demo logins) are kept. The portal is unavailable "
    "for about a minute."
)


@pytest.fixture
def admin():
    return make_member(roles=[Role.SYSADMIN])


@pytest.fixture
def poc(settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.DEPLOYMENT_STAGE = "poc"
    UpdateStatus.objects.update_or_create(pk=1, defaults={"agent_seen_at": timezone.now()})
    return settings


def test_url_name():
    assert reverse("demo_data") == URL


# --- Where it exists ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("allowed", "stage", "ok"),
    [
        (True, "poc", True),
        (True, "local", True),
        (True, "production", False),
        (True, "staging", False),
        (False, "poc", False),
        (False, "local", False),
    ],
)
def test_allowed_only_with_the_flag_on_a_poc_or_local_stage(settings, allowed, stage, ok):
    settings.SEED_DEMO_ALLOWED = allowed
    settings.DEPLOYMENT_STAGE = stage
    assert demo_reseed.allowed() is ok


@pytest.mark.parametrize(("allowed", "stage"), [(False, "poc"), (True, "production")])
def test_page_is_404_and_absent_from_settings_when_not_allowed(
    client, admin, settings, allowed, stage
):
    settings.SEED_DEMO_ALLOWED = allowed
    settings.DEPLOYMENT_STAGE = stage
    client.force_login(admin)
    assert client.get(URL).status_code == 404
    assert client.post(URL, {"dataset": "fresh_start", "confirm": "RESEED"}).status_code == 404
    response = client.get(reverse("settings_hub"), follow=True)
    links = navigation.hub_links(response.wsgi_request, "settings_hub")
    assert "demo_data" not in [link["name"] for link in links]
    assert "Demo data" not in response.content.decode()
    assert not HostTask.objects.exists()
    # Not offered on Server tasks either.
    page = client.get(reverse("server_tasks")).content.decode()
    assert "Reseed with demo data" not in page


def test_page_listed_on_settings_for_sysadmins_when_allowed(client, admin, poc):
    client.force_login(admin)
    response = client.get(reverse("settings_hub"), follow=True)
    links = navigation.hub_links(response.wsgi_request, "settings_hub")
    assert "demo_data" in [link["name"] for link in links]


def test_board_and_owners_never_see_it(client, poc):
    from community.tests.factories import LotFactory

    for user in (make_member(board=True), make_member(lots=[LotFactory()])):
        client.force_login(user)
        assert client.get(URL).status_code == 403
        assert client.post(URL, {"dataset": "fresh_start", "confirm": "RESEED"}).status_code == 403
        response = client.get(reverse("home"))
        links = navigation.hub_links(response.wsgi_request, "settings_hub")
        assert "demo_data" not in [link["name"] for link in links]
    assert not HostTask.objects.exists()


def test_anonymous_is_sent_to_sign_in(client, poc):
    response = client.get(URL)
    assert response.status_code == 302 and "/login" in response["Location"]


# --- The page ----------------------------------------------------------------------------


def test_page_lists_datasets_with_descriptions_and_the_confirmation(client, admin, poc):
    DemoSeed.objects.create(dataset="fresh_start")
    client.force_login(admin)
    page = client.get(URL).content.decode()
    assert "<h1>Demo data</h1>" in page
    for label in ("Oak Creek Farms (full demo)", "Fresh start (just handed over)"):
        assert label in page
    assert "declarant-appointed board of three" in page
    assert f'data-confirm="{CONFIRM}"' in page
    assert page.count("Reseed with this dataset</button>") == 2
    assert "Type RESEED to confirm" in page
    assert "with <strong>Fresh start (just handed over)</strong>" in page


def test_reseed_needs_the_word_and_re_renders_the_form(client, admin, poc):
    client.force_login(admin)
    for word in ("", "reseed", "RESEED!", "yes"):
        response = client.post(URL, {"dataset": "fresh_start", "confirm": word})
        assert response.status_code == 200, word
        assert "Nothing has been saved yet" in response.content.decode(), word
    assert not HostTask.objects.exists()


def test_reseed_refuses_a_dataset_not_in_the_registry(client, admin, poc):
    client.force_login(admin)
    for dataset in ("nope", "fresh_start; rm -rf /", "Oak_Creek", ""):
        response = client.post(URL, {"dataset": dataset, "confirm": "RESEED"})
        assert response.status_code == 200
    assert not HostTask.objects.exists()


def test_sysadmin_requests_a_reseed_and_it_is_audited(client, admin, poc):
    client.force_login(admin)
    response = client.post(URL, {"dataset": "fresh_start", "confirm": "RESEED"})
    assert response.status_code == 302 and response["Location"] == URL
    task = HostTask.objects.get()
    assert (task.kind, task.dataset, task.state, task.requested_by) == (
        "reseed_demo",
        "fresh_start",
        "requested",
        admin,
    )
    event = AuditEvent.objects.get(action="system.task_requested")
    assert event.actor == admin
    assert event.metadata == {"kind": "reseed_demo", "dataset": "fresh_start"}
    page = client.get(URL).content.decode()
    assert "Waiting for the server" in page and "disabled" in page
    # One task at a time.
    response = client.post(URL, {"dataset": "oak_creek", "confirm": "RESEED"})
    assert "already waiting or running" in response.content.decode()
    assert HostTask.objects.count() == 1


# --- The service ---------------------------------------------------------------------------


def test_service_refuses_when_not_allowed_even_with_everything_right(admin, settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.DEPLOYMENT_STAGE = "production"
    with pytest.raises(host_tasks.HostTaskRefused, match="not offered"):
        host_tasks.request_task(admin, "reseed_demo", dataset="fresh_start", confirmation="RESEED")
    settings.DEPLOYMENT_STAGE = "poc"
    settings.SEED_DEMO_ALLOWED = False
    with pytest.raises(host_tasks.HostTaskRefused):
        host_tasks.request_task(admin, "reseed_demo", dataset="fresh_start", confirmation="RESEED")
    # Defence in depth: the check itself, whatever the task list says.
    with pytest.raises(host_tasks.HostTaskRefused, match="cannot be loaded"):
        demo_reseed.check_request("fresh_start", "RESEED")
    assert not HostTask.objects.exists()


def test_service_needs_sysadmin_dataset_and_word(admin, poc):
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        host_tasks.request_task(
            make_member(board=True), "reseed_demo", dataset="oak_creek", confirmation="RESEED"
        )
    with pytest.raises(host_tasks.HostTaskRefused, match="Type RESEED"):
        host_tasks.request_task(admin, "reseed_demo", dataset="oak_creek", confirmation="")
    with pytest.raises(host_tasks.HostTaskRefused, match="datasets listed"):
        host_tasks.request_task(admin, "reseed_demo", dataset="../x", confirmation="RESEED")
    task = host_tasks.request_task(
        admin, "reseed_demo", dataset="oak_creek", confirmation=" RESEED "
    )
    assert task.dataset == "oak_creek"


def test_other_tasks_never_carry_a_dataset(admin, poc):
    task = host_tasks.request_task(admin, "backup", dataset="fresh_start")
    assert task.dataset == ""
    assert AuditEvent.objects.get(action="system.task_requested").metadata == {"kind": "backup"}


def test_the_database_refuses_a_dataset_that_is_not_a_plain_name(admin):
    with pytest.raises(IntegrityError), transaction.atomic():
        HostTask.objects.create(kind="reseed_demo", dataset="x'; DROP", requested_by=admin)


# --- Server tasks --------------------------------------------------------------------------


def test_server_tasks_shows_a_reseed_row_that_leads_to_demo_data(client, admin, poc):
    client.force_login(admin)
    page = client.get(reverse("server_tasks")).content.decode()
    assert "Reseed with demo data" in page and f'href="{URL}"' in page
    # Started only from Demo data (dataset and word): a bare request goes there.
    response = client.post(reverse("server_tasks"), {"kind": "reseed_demo"})
    assert response["Location"] == URL
    assert not HostTask.objects.exists()


def test_reseed_shows_its_dataset_in_server_task_history(client, admin, poc):
    HostTask.objects.create(
        kind="reseed_demo", dataset="fresh_start", requested_by=admin, state="succeeded"
    )
    client.force_login(admin)
    page = client.get(reverse("server_tasks")).content.decode()
    assert "Reseed with demo data (Fresh start (just handed over))" in page


def test_finished_reseed_is_audited_with_its_dataset(admin):
    HostTask.objects.create(
        kind="reseed_demo",
        dataset="fresh_start",
        requested_by=admin,
        state="succeeded",
        finished_at=timezone.now(),
    )
    call_command("record_host_tasks", stdout=io.StringIO())
    event = AuditEvent.objects.get(action="system.task_finished")
    assert event.metadata == {"kind": "reseed_demo", "state": "succeeded", "dataset": "fresh_start"}


def test_agent_reads_the_dataset_through_a_variable(admin):
    """The agent's SELECT, run against the real table."""
    agent = Path("infra/poc/host-tasks.sh").read_text()
    sql = re.search(r"SELECT dataset FROM core_hosttask.*?;\n", agent, re.S).group(0)
    assert re.findall(r":'(\w+)'", sql) == ["id"]
    task = HostTask.objects.create(kind="reseed_demo", dataset="oak_creek", requested_by=admin)
    with connection.cursor() as cur:
        cur.execute(re.sub(r":'(\w+)'", r"%(\1)s", sql), {"id": str(task.pk)})
        assert cur.fetchall() == [("oak_creek",)]


# --- Production: always off, and loud if not ----------------------------------------------


def test_production_settings_force_demo_data_off(monkeypatch):
    from core.tests.test_production_settings import _load

    module = _load(monkeypatch, SEED_DEMO_ALLOWED="true", DEPLOYMENT_STAGE="production")
    assert module.SEED_DEMO_ALLOWED is False
    monkeypatch.delenv("DEPLOYMENT_STAGE")
    module = _load(monkeypatch, SEED_DEMO_ALLOWED="true")
    assert module.DEPLOYMENT_STAGE == "production" and module.SEED_DEMO_ALLOWED is False


def test_startup_check_and_health_check_warn_if_it_is_ever_on_in_production(settings):
    settings.SEED_DEMO_ALLOWED = True
    settings.DEPLOYMENT_STAGE = "production"
    assert "hoa.W002" in [m.id for m in run_checks()]
    result = next(r for r in health.run_checks() if r.key == "demo_data")
    assert not result.ok and "production" in result.detail
    settings.DEPLOYMENT_STAGE = "poc"
    assert "hoa.W002" not in [m.id for m in run_checks()]
    assert "demo_data" not in [r.key for r in health.run_checks()]


# --- Old files ----------------------------------------------------------------------------


def test_clean_demo_files_removes_only_unreferenced_files(settings):
    from community.tests.factories import make_member as member
    from documents import services as documents

    settings.SEED_DEMO_ALLOWED = True
    board = member(board=True)
    from django.core.files.uploadedfile import SimpleUploadedFile

    from core import pdf

    doc = documents.create_document(
        title="Kept",
        category="governing",
        visibility="owners",
        actor=board,
        file=SimpleUploadedFile("kept.pdf", pdf.render([("normal", "kept")])),
    )
    kept = doc.current_version.file.name
    orphan = default_storage.save("documents/orphan-from-old-data", ContentFile(b"old"))
    probe = default_storage.save("healthcheck/probe", ContentFile(b"ok"))
    # The in-memory store may still hold other tests' files: count them too.
    from core.backups import unreferenced_files

    orphans = unreferenced_files()
    assert orphan in orphans and kept not in orphans and probe not in orphans
    out = io.StringIO()
    call_command("clean_demo_files", stdout=out)
    assert f"Removed {len(orphans)} file(s)" in out.getvalue()
    assert default_storage.exists(kept) and default_storage.exists(probe)
    assert not default_storage.exists(orphan)
    assert unreferenced_files() == []
    event = AuditEvent.objects.get(action="system.demo_files_cleaned")
    assert event.metadata == {"count": len(orphans)}
    settings.SEED_DEMO_ALLOWED = False
    with pytest.raises(CommandError, match="SEED_DEMO_ALLOWED"):
        call_command("clean_demo_files")
