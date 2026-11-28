"""A minor or major release keeps regression/RUNS.md and WALKTHROUGHS.md at
most one release line behind (D91, regression/release_logs.py). No database."""

from __future__ import annotations

import subprocess  # nosec B404 - runs the repository's own script
import sys

import pytest

from regression import release_logs, run, standard

CHANGELOG = "# Changelog\n\n## 0.43.0 — x\n\n## 0.42.1 — x\n\n## 0.42.0 — x\n\n## 0.41.0 — x\n"


def _runs(*versions: str) -> str:
    rows = "".join(
        f"| 2026-11-27 | `abc1234` | {v} | full | 1 | 0 | 0 | 0 | none | 0 | x |\n"
        for v in versions
    )
    sep = "|" + "---|" * 11
    return f"# Regression runs\n\n{release_logs.RUNS_HEADER}\n{sep}\n{rows}\n## older\n"


def _walks(*versions: str) -> str:
    rows = "".join(f"| 2026-11-27 | `abc1234` | {v} | 11 | 1 | 0 | 0 | x |\n" for v in versions)
    sep = "|" + "---|" * 8
    return f"# Walkthroughs\n\n{release_logs.WALKS_HEADER}\n{sep}\n{rows}"


def test_headers_match_the_vendored_standard():
    assert release_logs.RUNS_HEADER == standard.RUNS_HEADER
    assert release_logs.WALKS_HEADER == standard.WALKS_HEADER


@pytest.mark.parametrize(
    ("version", "runs", "walks", "ok"),
    [
        # A minor release with both logs on the previous line: passes.
        ("0.43.0", "0.42.0", "0.42.0", True),
        ("0.43.0", "0.42.1", "0.43.0", True),
        # One log a line further behind: fails.
        ("0.43.0", "0.41.0", "0.42.0", False),
        ("0.43.0", "0.42.0", "0.34.0", False),
        ("0.43.0", "0.34.0", "0.34.0", False),
        # Patch releases are exempt.
        ("0.43.2", "0.34.0", "0.34.0", True),
    ],
)
def test_minor_release_needs_logs_for_the_previous_line(version, runs, walks, ok):
    found = release_logs.problems(version, _runs(runs), _walks(walks), CHANGELOG)
    assert (found == []) is ok, found


def test_newest_row_is_the_first():
    assert (
        release_logs.problems("0.43.0", _runs("0.42.0", "0.30.0"), _walks("0.42.0"), CHANGELOG)
        == []
    )
    found = release_logs.problems("0.43.0", _runs("0.30.0", "0.42.0"), _walks("0.42.0"), CHANGELOG)
    assert len(found) == 1 and "RUNS.md" in found[0] and "0.42.x" in found[0]
    assert "newest: 0.30.0" in found[0]


def test_missing_tables_fail_and_name_the_fix():
    found = release_logs.problems("0.43.0", "# Regression runs\n", "", CHANGELOG)
    assert len(found) == 2
    assert "newest: none" in found[0] and "/full-check" in found[0]
    assert "WALKTHROUGHS.md" in found[1]


def test_major_release_takes_the_previous_line_from_the_changelog():
    log = "## 1.0.0 — x\n## 0.57.3 — x\n## 0.57.0 — x\n## 0.56.0 — x\n"
    assert release_logs.problems("1.0.0", _runs("0.57.3"), _walks("0.57.0"), log) == []
    assert release_logs.problems("1.0.0", _runs("0.56.0"), _walks("0.57.0"), log)


def test_first_release_line_and_bad_versions():
    assert release_logs.problems("0.1.0", "", "", "## 0.1.0 — x\n") == []
    assert release_logs.problems("next", "", "", CHANGELOG) == [
        "VERSION 'next' is not MAJOR.MINOR.PATCH"
    ]


def test_check_reads_the_files_under_a_root(tmp_path):
    (tmp_path / "regression").mkdir()
    (tmp_path / "VERSION").write_text("0.43.0\n")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    (tmp_path / "regression" / "RUNS.md").write_text(_runs("0.42.0"))
    (tmp_path / "regression" / "WALKTHROUGHS.md").write_text(_walks("0.42.0"))
    assert release_logs.check(tmp_path) == []
    (tmp_path / "VERSION").write_text("0.44.0\n")
    (tmp_path / "CHANGELOG.md").write_text("## 0.44.0 — x\n" + CHANGELOG)
    assert len(release_logs.check(tmp_path)) == 2


def test_run_py_check_runs_it_in_the_ci_stage():
    text = (run.HERE / "run.py").read_text()
    stage = text.split("def stage_ci(", 1)[1].split("\ndef ", 1)[0]
    assert '"release_logs.py"' in stage and '"regression logs current for the release"' in stage
    ids = (run.IDS / "ci.yaml").read_text()
    assert 'CI-014: "regression logs current for the release"' in ids


def test_this_repository_keeps_its_logs_current():
    """The rule itself, on this checkout (CI runs the suite on every pull request)."""
    result = subprocess.run(  # nosec B603
        [sys.executable, str(run.HERE / "release_logs.py")],
        capture_output=True,
        text=True,
        cwd=run.ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout
