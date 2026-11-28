"""The walkthrough tooling: stage selection, the report, and the crawl script parses.

No browser and no database: the walkthrough stage itself is run by
`python3 regression/run.py --stage walkthrough`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 - fixed command lists only
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from regression import run, walkthrough_report

HERE = Path(__file__).resolve().parent.parent


# --- Stage selection --------------------------------------------------------------------


def test_default_run_is_the_regression_stages_only():
    chosen = run.chosen_stages([])
    assert chosen == ["ci", "unit", "page", "manual"]
    assert "walkthrough" not in chosen
    assert "live" not in chosen


def test_with_walkthrough_adds_the_walkthrough_to_the_regression_stages():
    assert run.chosen_stages(["--with-walkthrough"]) == [
        "ci",
        "unit",
        "page",
        "manual",
        "walkthrough",
    ]


def test_walkthrough_stage_alone():
    assert run.chosen_stages(["--stage", "walkthrough"]) == ["walkthrough"]


def test_walkthrough_runs_in_its_own_lane_database_and_port():
    assert "walkthrough" in run.PARALLEL
    assert run.WALK_PORT != run.PAGE_PORT
    assert run.PREFIX["walkthrough"] == "WALK"
    # Its results never overwrite the inventory (out/walkthrough.json).
    assert run.RESULT_FILE["walkthrough"] != "walkthrough.json"


def test_walkthrough_ids_are_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "IDS", tmp_path)
    ids = run.Ids("walkthrough")
    first = ids.get("owner desktop: / has no sideways scroll")
    second = ids.get("owner phone: / has no sideways scroll")
    ids.save()
    again = run.Ids("walkthrough")
    assert (first, second) == ("WALK-001", "WALK-002")
    assert again.get("owner phone: / has no sideways scroll") == "WALK-002"
    assert again.get("a new check") == "WALK-003"


# --- The crawl script --------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_walkthrough_script_parses():
    proc = subprocess.run(  # nosec B603 B607
        ["node", "--check", str(HERE / "walkthrough.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


# --- The report --------------------------------------------------------------------------


def _png(path: Path, size=(390, 1200)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 220, 210)).save(path)


def _menu(items):
    return {
        "sections": [
            {"key": "", "title": "", "count": 0, "open": True, "items": items[:1]},
            {"key": "home", "title": "My home", "count": 2, "open": True, "items": items[1:]},
        ],
        "foot": [],
        "item_count": len(items),
        "tile_count": 1,
    }


@pytest.fixture
def out(tmp_path):
    items = [
        {"label": "Dashboard", "href": "/", "count": 0, "current": True},
        {"label": "Requests", "href": "/requests/?mine=1", "count": 2, "current": False},
    ]
    shot = "regression/out/walkthrough/owner/phone/requests-mine.png"
    _png(tmp_path / "walkthrough/owner/phone/requests-mine.png")
    _png(tmp_path / "walkthrough/owner/desktop/home.png", (1280, 900))
    page = {"key": "/", "path": "/", "final": "/", "status": 200, "h1": "Good morning"}
    inventory = {
        "widths": {"desktop": {"width": 1280}, "phone": {"width": 390}},
        "roles": {
            "owner": {
                "desktop": {"menu": _menu(items), "pages": [page], "hubs": {}},
                "phone": {"menu": _menu(items), "pages": [page], "hubs": {}},
            },
            "tenant": {
                "desktop": {"menu": _menu(items[:1]), "pages": [page], "hubs": {}},
                "phone": {"menu": _menu(items[:1]), "pages": [], "hubs": {}},
            },
        },
    }
    checks = {
        "stage": "walkthrough",
        "results": [
            {
                "id": "WALK-001",
                "check": "owner phone: /requests/?mine=1 tap targets at least 40x40 px",
                "status": "fail",
                "detail": "2 small: a.small 59x22px",
                "evidence": shot,
            },
            {
                "id": "WALK-002",
                "check": 'owner desktop: menu "Requests" (/requests/?mine=1) matches its page heading',
                "status": "fail",
                "detail": 'opens a page titled "My requests"',
                "evidence": "regression/out/walkthrough/owner/desktop/home.png",
            },
            {
                "id": "WALK-003",
                "check": "tenant desktop: / has no sideways scroll",
                "status": "pass",
                "detail": "",
                "evidence": "",
            },
        ],
    }
    findings = {
        "summary": "Two menus could be shorter.",
        "roles": [{"role": "owner", "desktop_items": 2, "phone_items": 2, "notes": "Tidy."}],
        "findings": [
            {
                "id": "WT-002",
                "group": "keep_separate",
                "title": "Votes stay apart from surveys",
                "now": "Separate lists.",
                "proposal": "Keep them separate.",
                "who": "owners",
                "effort": "S",
                "risk": "Binding votes must not look informal.",
                "evidence": [],
            },
            {
                "id": "WT-001",
                "group": "rename",
                "title": "Rename <Requests>",
                "now": "Menu says Requests.",
                "proposal": "Call it My requests.",
                "who": "owners",
                "effort": "S",
                "risk": "low",
                "evidence": [shot],
            },
        ],
        "proposed_menus": {
            "owner": [{"title": "My home", "items": ["Requests", "Account"]}, "Community"]
        },
    }
    (tmp_path / "walkthrough.json").write_text(json.dumps(inventory))
    (tmp_path / "walkthrough-checks.json").write_text(json.dumps(checks))
    (tmp_path / "walkthrough-findings.json").write_text(json.dumps(findings))
    return tmp_path


def test_report_renders_roles_checks_findings_and_menus(out):
    page = walkthrough_report.render(out, tz=ZoneInfo("America/New_York"))
    assert "<title>Portal walkthrough</title>" in page
    assert "Two menus could be shorter." in page
    # Roles with desktop and phone counts.
    assert "<strong>owner</strong>" in page and "<strong>tenant</strong>" in page
    # Failing checks grouped by kind, passing ones left out.
    assert "Tap targets smaller than 40x40 px on a phone" in page
    assert "Menu label differs from the page heading" in page
    assert "WALK-001" in page and "WALK-002" in page and "WALK-003" not in page
    # Screenshots linked relative to the report (which sits in out/).
    assert 'href="walkthrough/owner/phone/requests-mine.png"' in page
    assert "regression/out/" not in page
    # Failing pages embedded as small JPEG thumbnails.
    assert page.count("data:image/jpeg;base64,") == 2
    # Findings grouped in the fixed order, escaped.
    assert page.index(">Rename <") < page.index(">Keep separate")
    assert "Rename &lt;Requests&gt;" in page
    assert "Proposed menus" in page and "Requests, Account" in page
    # Theme-aware like the regression report.
    assert "prefers-color-scheme:dark" in page


def test_report_without_thumbnails_or_findings(out):
    (out / "walkthrough-findings.json").unlink()
    page = walkthrough_report.render(out, tz=ZoneInfo("UTC"), thumbs=False)
    assert "data:image/jpeg" not in page
    assert "No reviewer findings recorded for this run." in page
    assert "Proposed menus" not in page


def test_report_thumbnails_stay_within_the_budget(out):
    images = walkthrough_report.Thumbs(out, True, budget=10)
    assert images.get("regression/out/walkthrough/owner/phone/requests-mine.png") == ""
    assert images.skipped == 1


def test_report_respects_report_tz(out):
    page = walkthrough_report.render(out, tz=ZoneInfo("America/New_York"))
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d E[SD]T", page)


def test_full_check_log_writes_one_walkthrough_row(monkeypatch):
    """`run.py log --full-check` adds one RUNS.md row and exactly one WALKTHROUGHS.md row."""
    rows = []
    monkeypatch.setattr(run.standard, "add_row", lambda path, *a, **k: rows.append(Path(path).name))
    monkeypatch.setattr(run.standard, "load_stages", lambda: [])
    monkeypatch.setattr(run.standard, "load_findings", lambda names: [])
    monkeypatch.setattr(run, "_walk_counts", lambda: (11, 700, 0, 0))
    assert run.log(["--full-check"]) == 0
    assert rows == ["RUNS.md", "WALKTHROUGHS.md"]


# --- Files seeded in one process, served by another (walkthrough 0.44.0) ----------------


def test_the_page_and_walkthrough_sites_keep_seeded_files_on_disk_per_database():
    """seed_demo and runserver are separate processes: in-memory files were
    gone when a page opened them (every demo receipt was a 404)."""
    import ast

    text = (HERE / "page_settings.py").read_text()
    assert "InMemoryStorage" not in text
    assert "django.core.files.storage.FileSystemStorage" in text
    tree = ast.parse(text)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "media_dir")
    namespace: dict = {"Path": Path, "__file__": str(HERE / "page_settings.py")}
    exec(compile(ast.Module([fn], []), "page_settings", "exec"), namespace)  # noqa: S102  # nosec B102
    media_dir = namespace["media_dir"]
    walk = media_dir("postgres://u:p@localhost:5432/hoa_reg_walk")
    page = media_dir("postgres://u:p@localhost:5432/hoa_reg_pages")
    assert walk != page and walk.parent == HERE / "out" / "media"
    assert media_dir("postgres://x/../../etc").name == "etc"  # never outside out/media
    # A fresh database starts with no files of an earlier run.
    assert '"out" / "media" / name' in (HERE / "page_setup.py").read_text()
