"""A release needs a full check first when one is due (D91, amended 2026-11-28,
regression/release_logs.py): weekly, every 5 releases, after a material security
or accounting change, and on a major release. No database."""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess  # nosec B404 - runs git and the repository's own script
import sys

import pytest
import yaml

from regression import release_logs, run, standard

TODAY = dt.date(2026, 11, 28)
SETTINGS = release_logs.load_settings((run.HERE / "profile.yaml").read_text())
FIXTURE_SETTINGS = release_logs.Settings(
    max_age_days=7,
    every_releases=5,
    material_paths=("core/policy.py", "ledger/**/*.py", "accounts/**"),
    not_material=("**/templates/**", "**/tests/**", "**/*.md"),
)


def _changelog(*sections: tuple[str, str]) -> str:
    return "# Changelog\n\n" + "".join(f"## {v} — {d}\n\n- x\n\n" for v, d in sections)


def _runs(*entries: tuple[str, str, str], audit: str = "full (full check)") -> str:
    rows = "".join(
        f"| {d} 00:38 | `{c}` | {v} | {audit} | 1 | 0 | 0 | 0 | none | none | x |\n"
        for d, c, v in entries
    )
    sep = "|" + "---|" * 11
    return f"# Regression runs\n\n{release_logs.RUNS_HEADER}\n{sep}\n{rows}\n## older\n"


def _walks(*entries: tuple[str, str, str]) -> str:
    rows = "".join(f"| {d} 00:38 | `{c}` | {v} | 11 | 1 | 0 | 0 | x |\n" for d, c, v in entries)
    sep = "|" + "---|" * 8
    return f"# Walkthroughs\n\n{release_logs.WALKS_HEADER}\n{sep}\n{rows}"


def _problems(version, changelog, entry=("2026-11-27", "abc1234", "0.44.0"), changed=()):
    return release_logs.problems(
        version,
        _runs(entry),
        _walks(entry),
        changelog,
        FIXTURE_SETTINGS,
        list(changed),
        TODAY,
    )


def test_headers_match_the_vendored_standard():
    assert release_logs.RUNS_HEADER == standard.RUNS_HEADER
    assert release_logs.WALKS_HEADER == standard.WALKS_HEADER


def test_settings_read_without_yaml_match_the_profile():
    data = yaml.safe_load((run.HERE / "profile.yaml").read_text())["full_check"]
    assert SETTINGS.max_age_days == data["max_age_days"] == 7
    assert SETTINGS.every_releases == data["every_releases"] == 5
    assert list(SETTINGS.material_paths) == data["material_paths"]
    assert list(SETTINGS.not_material) == data["not_material"]
    assert data["weekly_when_changed"] is True


# --- rule 1: weekly ----------------------------------------------------------


@pytest.mark.parametrize(
    ("checked", "released", "ok"),
    [
        ("2026-11-27", "2026-11-28", True),
        ("2026-11-21", "2026-11-28", True),  # exactly 7 days
        ("2026-11-20", "2026-11-28", False),  # 8 days
    ],
)
def test_weekly(checked, released, ok):
    log = _changelog(("0.45.0", released), ("0.44.0", "2026-11-20"))
    found = _problems("0.45.0", log, entry=(checked, "abc1234", "0.44.0"))
    assert (found == []) is ok, found
    if not ok:
        assert "(weekly)" in found[0] and "8 days" in found[0]


def test_weekly_takes_today_without_a_changelog_date():
    log = "## 0.45.0\n\n## 0.44.0 — 2026-11-01\n"
    assert _problems("0.45.0", log, entry=("2026-11-25", "abc1234", "0.44.0")) == []
    found = _problems("0.45.0", log, entry=("2026-11-01", "abc1234", "0.44.0"))
    assert len(found) == 1 and "27 days" in found[0]


# --- rule 2: every 5 releases ------------------------------------------------


def test_every_five_releases_patches_included():
    four = _changelog(
        ("0.46.0", "2026-11-28"),
        ("0.45.2", "2026-11-28"),
        ("0.45.1", "2026-11-28"),
        ("0.45.0", "2026-11-27"),
        ("0.44.0", "2026-11-27"),
    )
    assert _problems("0.46.0", four) == []
    five = _changelog(("0.46.1", "2026-11-28")) + four.split("\n\n", 1)[1]
    found = _problems("0.46.1", five)
    assert found == [
        "A full check is due (every 5 releases): 5 releases since the last one (0.44.0) "
        "— run /full-check and `python3 regression/run.py log --full-check`."
    ]


def test_a_patch_release_is_not_exempt():
    log = _changelog(*[(f"0.44.{n}", "2026-11-28") for n in range(6, -1, -1)])
    found = _problems("0.44.6", log)
    assert len(found) == 1 and "6 releases since the last one (0.44.0)" in found[0]


# --- rule 3: material security or accounting change ---------------------------


@pytest.mark.parametrize(
    ("changed", "ok"),
    [
        ([], True),
        (["templates/ledger/my_account.html", "static/css/site.css"], True),
        (
            ["ledger/templates/ledger/x.html", "ledger/tests/test_x.py", "docs/claude/finance.md"],
            True,
        ),
        (["core/views.py", "community/services.py"], True),
        (["core/policy.py"], False),
        (["ledger/books/close.py"], False),
        (["accounts/migrations/0042_x.py"], False),
    ],
)
def test_material_paths(changed, ok):
    log = _changelog(("0.45.0", "2026-11-28"), ("0.44.0", "2026-11-27"))
    found = _problems("0.45.0", log, changed=changed)
    assert (found == []) is ok, found
    if not ok:
        assert "(security or accounting change)" in found[0] and changed[0] in found[0]


@pytest.mark.parametrize(
    ("path", "material"),
    [
        ("core/policy.py", True),
        ("core/credentials.py", True),
        ("core/credential_checks.py", True),
        ("accounts/views.py", True),
        ("accounts/templates/accounts/x.html", False),
        ("accounts/tests/test_mfa.py", False),
        ("payments/services.py", True),
        ("payments/tests/test_autopay.py", False),
        ("ledger/books/close.py", True),
        ("ledger/migrations/0101_x.py", True),
        ("governance/sealing.py", True),
        ("governance/voting_views.py", True),
        ("governance/surveys.py", False),
        ("operations/reimbursements.py", True),
        ("operations/models_payouts.py", True),
        ("audit/actions.py", True),
        ("infra/poc/update-portal.sh", True),
        ("docker-compose.tunnel.yml", True),
        ("templates/core/system_status.html", False),
        ("static/css/site.css", False),
        ("docs/decisions.md", False),
        ("regression/release_logs.py", False),
        ("core/views.py", False),
    ],
)
def test_the_profiles_material_paths(path, material):
    assert (release_logs.material([path], SETTINGS) == [path]) is material


def test_glob_double_star_takes_no_directories_too():
    rx = release_logs.glob_regex("payments/**/*.py")
    assert rx.match("payments/services.py") and rx.match("payments/a/b/c.py")
    assert not rx.match("payments/README")
    assert not release_logs.glob_regex("core/*.py").match("core/a/b.py")


# --- rule 4: major releases ----------------------------------------------------


def test_major_release_needs_a_full_check_on_that_version():
    log = _changelog(("1.0.0", "2026-11-28"), ("0.57.3", "2026-11-27"))
    assert _problems("1.0.0", log, entry=("2026-11-28", "abc1234", "1.0.0")) == []
    found = _problems("1.0.0", log, entry=("2026-11-28", "abc1234", "0.57.3"))
    assert len(found) == 1 and "(major release)" in found[0] and "0.57.3" in found[0]
    # A minor release is not a major one.
    assert (
        _problems(
            "0.58.0", _changelog(("0.58.0", "2026-11-28")), entry=("2026-11-28", "a", "0.57.3")
        )
        == []
    )


# --- what counts as a full check ---------------------------------------------


def test_a_full_check_is_logged_in_both_files():
    log = _changelog(("0.45.0", "2026-11-28"), ("0.44.0", "2026-11-27"))
    full = ("2026-11-27", "abc1234", "0.44.0")
    old = ("2026-10-01", "def5678", "0.30.0")
    ok = release_logs.problems(
        "0.45.0", _runs(full, old), _walks(full), log, FIXTURE_SETTINGS, [], TODAY
    )
    assert ok == []
    # A regression alone (no walkthrough on that commit) is not a full check: the older one counts.
    found = release_logs.problems(
        "0.45.0", _runs(full, old), _walks(old), log, FIXTURE_SETTINGS, [], TODAY
    )
    assert any("(weekly)" in f and "0.30.0" in f for f in found)
    # Nor is a regression without "full check" in its Audit cell.
    entry = release_logs.newest_full_check(_runs(full, audit="incremental"), _walks(full))
    assert entry is None


def test_nothing_logged_and_bad_versions():
    log = _changelog(("0.45.0", "2026-11-28"), ("0.44.0", "2026-11-27"))
    found = release_logs.problems("0.45.0", "# Regression runs\n", "", log)
    assert len(found) == 1 and "none is logged" in found[0] and "/full-check" in found[0]
    assert release_logs.problems("0.1.0", "", "", "## 0.1.0 — 2026-01-01\n") == []
    assert release_logs.problems("next", "", "", log) == ["VERSION 'next' is not MAJOR.MINOR.PATCH"]


# --- the checkout: a temporary git repository ----------------------------------


def _git(path, *args):
    subprocess.run(  # nosec B603 B607
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "HOME": str(path),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _head(path):
    return subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    (tmp_path / "regression").mkdir()
    (tmp_path / "core" / "templates").mkdir(parents=True)
    shutil.copy(run.HERE / "profile.yaml", tmp_path / "regression" / "profile.yaml")
    (tmp_path / "core" / "policy.py").write_text("RULES = {}\n")
    (tmp_path / "core" / "templates" / "page.html").write_text("<p>x</p>\n")
    (tmp_path / "VERSION").write_text("0.44.0\n")
    (tmp_path / "CHANGELOG.md").write_text(_changelog(("0.44.0", "2026-11-27")))
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "0.44.0")
    _git(tmp_path, "tag", "v0.44.0")
    sha = _head(tmp_path)
    entry = ("2026-11-27", sha, "0.44.0")
    (tmp_path / "regression" / "RUNS.md").write_text(_runs(entry))
    (tmp_path / "regression" / "WALKTHROUGHS.md").write_text(_walks(entry))
    (tmp_path / "VERSION").write_text("0.45.0\n")
    (tmp_path / "CHANGELOG.md").write_text(
        _changelog(("0.45.0", "2026-11-28"), ("0.44.0", "2026-11-27"))
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "0.45.0 and the logs")
    return tmp_path


def test_checkout_template_change_passes_policy_change_fails(repo):
    (repo / "core" / "templates" / "page.html").write_text("<p>reworded</p>\n")
    _git(repo, "commit", "-qam", "wording")
    assert release_logs.check(repo, TODAY) == []
    (repo / "core" / "policy.py").write_text("RULES = {'x': 1}\n")
    _git(repo, "commit", "-qam", "policy")
    found = release_logs.check(repo, TODAY)
    assert (
        len(found) == 1 and "core/policy.py" in found[0] and "(security or accounting" in found[0]
    )


def test_checkout_already_tagged_is_not_a_release(repo):
    (repo / "core" / "policy.py").write_text("RULES = {'x': 1}\n")
    _git(repo, "commit", "-qam", "policy")
    assert release_logs.check(repo, TODAY)
    _git(repo, "tag", "v0.45.0")
    found, notes = release_logs.report(repo, TODAY)
    assert found == [] and "already tagged" in notes[0]
    # Without this version's tag it is a release again.
    _git(repo, "tag", "-d", "v0.45.0")
    assert release_logs.check(repo, TODAY)


def test_checkout_fixes_after_this_versions_full_check_pass(repo):
    """A full check on the version being released covers the fixes made after it
    (often its own findings): no loop of full checks."""
    entry = ("2026-11-28", _head(repo), "0.45.0")
    (repo / "regression" / "RUNS.md").write_text(_runs(entry))
    (repo / "regression" / "WALKTHROUGHS.md").write_text(_walks(entry))
    _git(repo, "commit", "-qam", "log the 0.45.0 full check")
    (repo / "core" / "policy.py").write_text("RULES = {'fixed': 1}\n")
    _git(repo, "commit", "-qam", "fix a finding")
    assert release_logs.check(repo, TODAY) == []


def test_checkout_without_a_release_tag_skips_the_material_rule(repo):
    _git(repo, "tag", "-d", "v0.44.0")
    (repo / "core" / "policy.py").write_text("RULES = {'x': 1}\n")
    _git(repo, "commit", "-qam", "policy")
    found, notes = release_logs.report(repo, TODAY)
    assert found == [] and "no release tag" in notes[0]


def test_checkout_weekly_and_count_through_the_files(repo):
    assert release_logs.check(repo, TODAY) == []
    (repo / "CHANGELOG.md").write_text(
        _changelog(("0.45.0", "2026-12-06"), ("0.44.0", "2026-11-27"))
    )
    found = release_logs.check(repo, TODAY)
    assert len(found) == 1 and "(weekly)" in found[0]


def test_checkout_shallow_clone_skips_the_material_rule(repo, monkeypatch):
    """CI's test job checks out one commit with no tags: rules 1, 2 and 4 still apply."""
    (repo / "core" / "policy.py").write_text("RULES = {'x': 1}\n")
    _git(repo, "commit", "-qam", "policy")
    real = release_logs.git

    def shallow(root, *args):
        if args[0] == "diff":
            return 128, ""
        if args[:2] == ("rev-parse", "--is-shallow-repository"):
            return 0, "true"
        return real(root, *args)

    monkeypatch.setattr(release_logs, "git", shallow)
    found, notes = release_logs.report(repo, TODAY)
    assert found == [] and "not checked" in notes[0]
    (repo / "CHANGELOG.md").write_text(
        _changelog(("0.45.0", "2026-12-06"), ("0.44.0", "2026-11-27"))
    )
    assert "(weekly)" in release_logs.check(repo, TODAY)[0]


def test_run_py_check_runs_it_in_the_ci_stage():
    text = (run.HERE / "run.py").read_text()
    stage = text.split("def stage_ci(", 1)[1].split("\ndef ", 1)[0]
    assert '"release_logs.py"' in stage and '"regression logs current for the release"' in stage
    ids = (run.IDS / "ci.yaml").read_text()
    assert 'CI-014: "regression logs current for the release"' in ids


def test_this_repository_keeps_its_logs_current():
    """The rule itself, on this checkout (CI runs the suite on every pull request;
    its shallow clone skips the material-change rule, run.py check does not)."""
    result = subprocess.run(  # nosec B603
        [sys.executable, str(run.HERE / "release_logs.py")],
        capture_output=True,
        text=True,
        cwd=run.ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout
