"""The regression suite answers the owner's standard (STANDARD.md 3, D90): the
files the global skills read, the command set, and the standard findings files
in this project's own reports. No database needed."""

from __future__ import annotations

import json
import subprocess  # nosec B404 - fixed command lists only
import sys
from zoneinfo import ZoneInfo

import pytest
import yaml

from regression import run, walkthrough_report

HERE = run.HERE


def test_profile_and_contract_files():
    profile = yaml.safe_load((HERE / "profile.yaml").read_text())
    assert profile["type"] == "server" and profile["ui"] is True
    assert profile["report_tz"] == "America/New_York"
    for stage, prefix in run.PREFIX.items():
        assert profile["stages"][stage]["prefix"] == prefix, stage
    assert profile["stages"]["live"]["env"] == ["LIVE_HOST"]
    assert set(profile["reviewers"]) == {"bugs", "claims", "exposure", "desktop", "phone"}
    published = yaml.safe_load((HERE / "published.yaml").read_text())
    assert published["regression"].startswith("https://claude.ai/artifact/")
    assert published["walkthrough"].startswith("https://claude.ai/artifact/")
    claims = yaml.safe_load((HERE / "claims.yaml").read_text())
    ids = [c["id"] for c in claims]
    assert len(ids) == len(set(ids)) and len(ids) >= 40
    for c in claims:
        assert c["claim"] and c["where"] and c["code"], c["id"]
        for where in c["where"]:
            path = where.split()[0]
            if "/" in path or path.endswith(".md"):
                assert (run.ROOT / path).is_file(), f"{c['id']}: {path}"
    # The global skills replace the project copies.
    assert not (run.ROOT / ".claude" / "skills" / "regression").exists()


def test_vendored_standard_names_its_kit_version():
    out = subprocess.run(  # nosec B603
        [sys.executable, str(HERE / "standard.py"), "--version"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("regression-kit ")


def test_the_standard_log_tables_have_a_row_for_the_last_run():
    from regression import standard

    assert standard.runs_rows(HERE / "RUNS.md", standard.RUNS_HEADER)
    assert standard.runs_rows(HERE / "WALKTHROUGHS.md", standard.WALKS_HEADER)
    assert standard.last_commit(HERE / "WALKTHROUGHS.md", standard.WALKS_HEADER)


def test_results_carry_the_standard_key_and_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "OUT", tmp_path)
    monkeypatch.setattr(run, "IDS", tmp_path)
    st = run.Stage("manual")
    st.add("a check", "pass")
    data = st.finish()
    (result,) = data["results"]
    assert result["key"] == result["check"] == "a check"
    assert data["prefix"] == "MAN"
    assert json.loads((tmp_path / "manual.json").read_text())["results"][0]["key"] == "a check"


def test_manual_results_count_only_for_this_version(tmp_path, monkeypatch):
    here = tmp_path / "regression"
    here.mkdir()
    (here / "manual.yaml").write_text(
        yaml.safe_dump(
            [
                {"key": "done", "check": "x"},
                {"key": "old", "check": "y"},
                {"key": "never", "check": "z"},
            ]
        )
    )
    (tmp_path / "VERSION").write_text("9.9.9\n")
    (here / "manual-log.yaml").write_text(
        yaml.safe_dump(
            {
                "done": {"result": "pass", "version": "9.9.9", "date": "2026-11-27", "note": "ok"},
                "old": {"result": "pass", "version": "9.9.8"},
            }
        )
    )
    monkeypatch.setattr(run, "HERE", here)
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "OUT", tmp_path)
    monkeypatch.setattr(run, "IDS", tmp_path)
    st = run.Stage("manual")
    run.stage_manual(st)
    status = {r["key"]: r["status"] for r in st.results}
    assert status == {"done": "pass", "old": "manual", "never": "manual"}


@pytest.mark.parametrize("command", sorted(run.TOOLING))
def test_every_standard_command_is_answered(command, monkeypatch):
    called = {}
    monkeypatch.setattr(
        run.standard, "main", lambda argv: called.setdefault("standard", argv) and 0
    )
    monkeypatch.setattr(run, "report", lambda argv: called.setdefault("report", argv) and 0)
    monkeypatch.setattr(run, "log", lambda argv: called.setdefault("log", argv) and 0)
    run.tooling([command, "--x"])
    expected = command if command in ("report", "log") else "standard"
    assert expected in called


def test_check_runs_ci_and_unit_quietly(monkeypatch):
    ran = []
    monkeypatch.setattr(sys, "argv", ["run.py", "check"])
    monkeypatch.setattr(run, "STAGES", {n: (lambda st, n=n: ran.append(n)) for n in run.ORDER})
    monkeypatch.setattr(
        run.Stage, "finish", lambda self, env=None: {"stage": self.name, "results": []}
    )
    assert run.main() == 0
    assert sorted(ran) == ["ci", "unit"]


def test_walkthrough_report_reads_the_standard_reviewer_files(tmp_path):
    (tmp_path / "findings").mkdir()
    (tmp_path / "findings" / "walkthrough-desktop.json").write_text(
        json.dumps(
            {
                "reviewer": "desktop",
                "status": "done",
                "findings": [
                    {
                        "id": "WD-001",
                        "group": "naming",
                        "title": "Two names for one page",
                        "detail": "now",
                        "proposal": "rename",
                        "roles": ["owner"],
                        "value": "high",
                    }
                ],
                "proposed_menus": {"owner": [{"title": "My home", "items": ["Dashboard"]}]},
            }
        )
    )
    (tmp_path / "findings" / "walkthrough-phone.json").write_text(
        json.dumps(
            {
                "reviewer": "phone",
                "status": "done",
                "findings": [
                    {"id": "WP-001", "group": "layout", "title": "Crowded", "detail": "d"}
                ],
            }
        )
    )
    merged = walkthrough_report.merged_findings(tmp_path)
    assert [(f["id"], f["group"]) for f in merged["findings"]] == [
        ("WD-001", "rename"),
        ("WP-001", "mobile"),
    ]
    assert merged["proposed_menus"]["owner"][0]["title"] == "My home"
    page = walkthrough_report.render(tmp_path, tz=ZoneInfo("UTC"), thumbs=False)
    assert "Two names for one page" in page and "Crowded" in page


def test_regression_report_reads_the_standard_reviewer_files(tmp_path, monkeypatch):
    from regression import report

    monkeypatch.setattr(report, "OUT", tmp_path)
    (tmp_path / "findings").mkdir()
    (tmp_path / "findings" / "claims.json").write_text(
        json.dumps(
            {
                "reviewer": "claims",
                "status": "done",
                "findings": [
                    {
                        "id": "SEC-001",
                        "severity": "medium",
                        "title": "t",
                        "description": "d",
                        "verified": True,
                    }
                ],
                "claims": [{"id": "CLM-001", "held": False, "note": "n"}],
            }
        )
    )
    (tmp_path / "findings" / "bugs.json").write_text(
        json.dumps(
            {
                "reviewer": "bugs",
                "status": "done",
                "findings": [{"id": "BUG-001", "severity": "low"}],
            }
        )
    )
    bugs, security = report.review_findings()
    assert [f["id"] for f in bugs] == ["BUG-001"]
    assert [f["id"] for f in security["findings"]] == ["SEC-001"]
    (claim,) = security["claims"]
    assert claim["claim"].startswith("CLM-001: No anonymous access") and claim["held"] is False
