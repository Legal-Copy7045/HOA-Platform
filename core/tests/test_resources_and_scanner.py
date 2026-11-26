"""System status: server resources from the VM agent and malware-scanning
figures (D39). Counts and sizes only."""

import datetime
import re
import shutil
import subprocess  # nosec B404 - runs the repository's own agent snippet
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from community.models import Role
from community.tests.factories import make_member
from core import health
from core.models import UpdateStatus

pytestmark = pytest.mark.django_db
GB = 1024 * 1024  # kB


def _report(avail_kb, low_since=None, seen=None):
    UpdateStatus.objects.update_or_create(
        pk=1,
        defaults={
            "agent_seen_at": seen or timezone.now(),
            "host_resources": {
                "mem_total_kb": 4 * GB,
                "mem_available_kb": avail_kb,
                "swap_total_kb": GB,
                "swap_free_kb": GB - 2048,
                "load1": 0.42,
                "load5": 0.3,
                "load15": 0.2,
                "cpus": 4,
                "low_since": low_since,
            },
        },
    )


def test_no_agent_report_means_no_memory_row():
    assert health._memory() is None
    _report(2 * GB, seen=timezone.now() - datetime.timedelta(minutes=30))  # stale
    assert health._memory() is None


def test_memory_row_reads_in_use_without_the_cache():
    _report(int(1.9 * GB))
    ok, detail = health._memory()
    assert ok and detail.startswith("2.1 of 4.0 GB in use") and "swap 2 MB" in detail
    assert "(4 cores)" in detail


def test_low_memory_alerts_only_when_it_stays_low():
    recent = (timezone.now() - datetime.timedelta(minutes=3)).isoformat()
    _report(GB // 20, low_since=recent)
    assert health._memory()[0] is True  # a short dip (e.g. ClamAV reloading)
    old = (timezone.now() - datetime.timedelta(minutes=12)).isoformat()
    _report(GB // 20, low_since=old)
    ok, detail = health._memory()
    assert not ok and detail.startswith("Less than 10% of memory free for 12 minutes")


def test_status_page_shows_resources_and_scanner(client, settings):
    settings.DOCUMENT_SCANNER = "clamd"
    client.force_login(make_member(roles=[Role.SYSADMIN]))
    _report(2 * GB)
    page = client.get(reverse("system_status")).content.decode()
    assert "Server resources" in page and "2.0 of 4.0 GB in use" in page
    assert "Waiting to be scanned" in page and "Scanned, last 7 days" in page
    assert "Not answering" in page  # no clamd in tests
    client.force_login(make_member(board=True))
    assert client.get(reverse("system_status")).status_code == 403


def test_scan_summary_counts_and_waiting():
    from django.core.files.uploadedfile import SimpleUploadedFile

    from documents import services as docs
    from documents.models import ScanResult, VersionScan

    doc = docs.create_document(
        title="Rules",
        category="governing",
        visibility="owners",
        actor=make_member(board=True),
        file=SimpleUploadedFile("rules.pdf", b"%PDF-1.4\n%%EOF\n"),
    )
    summary = docs.scan_summary()
    assert summary["scanned"] >= 1
    version = doc.versions.first()
    VersionScan.objects.create(version=version, result=ScanResult.ERROR, scanner="clamd")
    after = docs.scan_summary()
    assert after["errors"] == summary["errors"] + 1 and after["waiting"] >= 1


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_agent_reads_proc_numbers_only(tmp_path):
    text = Path("infra/poc/portal-agent.sh").read_text()
    block = text[text.index("meminfo() {") : text.index('psql_run -v hr="$HOST_RESOURCES"')]
    assert "docker stats" not in text  # the costly part stays out (owner: cheap parts only)
    script = tmp_path / "res.sh"
    script.write_text(block + 'printf "%s|%s" "$HOST_RESOURCES" "$LOW"\n')
    out = subprocess.run(  # noqa: S603  # nosec B603
        [shutil.which("bash"), str(script)], capture_output=True, text=True, check=True
    ).stdout
    resources, low = out.rsplit("|", 1)
    import json

    data = json.loads(resources)
    assert data["mem_total_kb"] > 0 and data["cpus"] >= 1 and low in ("0", "1")
    assert re.fullmatch(r"[0-9.]+", str(data["load1"]))
    assert (
        "low_since" in text
        and "|| true" in text[text.index('psql_run -v hr="$HOST_RESOURCES"') :][:600]
    )
