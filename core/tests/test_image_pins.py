"""Infrastructure images are pinned only together with a way to test and
upgrade them (external review 2026-11-26, D57): each compose image carries a
version tag and a digest, Dependabot's docker-compose ecosystem proposes the
updates, and CI validates, builds and scans every proposal. Also the CI
security gates (secret scan, image scan, CodeQL, page crawl)."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.tunnel.yml")
WORKFLOWS = ROOT / ".github" / "workflows"
PINNED = re.compile(r"^[a-z0-9./-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


class _ComposeLoader(yaml.SafeLoader):
    """Compose's own tags (!reset, !override) read as plain values."""


for _tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(
        _tag,
        lambda loader, node: (
            loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
            if isinstance(node, yaml.MappingNode)
            else loader.construct_scalar(node)
        ),
    )


def load_compose(name: str) -> dict:
    with open(ROOT / name, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_ComposeLoader)  # noqa: S506  # nosec B506 - SafeLoader subclass


def _images():
    for name in COMPOSE_FILES:
        for service, spec in (load_compose(name).get("services") or {}).items():
            if "image" in spec and spec["image"] != "oak-creek-portal:local":
                yield name, service, spec["image"]


def test_every_infrastructure_image_has_a_version_and_a_digest():
    seen = {}
    for name, service, image in _images():
        seen[service] = image
        assert PINNED.match(image), f"{name} {service}: {image} needs tag@sha256:digest"
        tag = image.split("@")[0].split(":")[1]
        assert tag not in ("latest", "stable", "edge"), f"{service} follows a moving tag"
    assert {"postgres", "seaweedfs", "mailpit", "clamav", "cloudflared"} <= set(seen)
    # The portal's own image is built locally and never pulled.
    assert load_compose("docker-compose.yml")["x-app"]["image"] == "oak-creek-portal:local"


def test_dependabot_proposes_every_pinned_image():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"]: u for u in config["updates"]}
    compose = ecosystems["docker-compose"]
    assert compose["directory"] == "/"
    # Dependabot's docker-compose file pattern (dependabot-core
    # docker/lib/dependabot/docker_compose/file_fetcher.rb) must find both files.
    pattern = re.compile(r"(docker-)?compose(-[\w]+)?(?>\.[\w-]+)?\.ya?ml", re.I)
    for name in COMPOSE_FILES:
        assert pattern.fullmatch(name), name
    # A Postgres major version is a planned dump and restore, not a weekly PR.
    assert {"dependency-name": "postgres", "update-types": ["version-update:semver-major"]} in (
        compose["ignore"]
    )
    for kept in ("pip", "github-actions", "docker"):
        assert kept in ecosystems


def test_no_daily_pull_of_latest_on_the_vm():
    start = (ROOT / "infra" / "poc" / "start-portal.sh").read_text()
    assert "cloudflared:latest" not in start
    assert "pull cloudflared" not in start
    assert "systemctl enable --now hoa-cloudflared-update.timer" not in start
    # VMs that still have the old timer lose it on the next update.
    assert "systemctl disable --now hoa-cloudflared-update.timer" in start
    for path in (ROOT / "infra").rglob("*.sh"):
        assert ":latest" not in path.read_text(), path


def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _uses(workflow):
    return [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]


def test_every_action_is_pinned_by_commit_with_a_version_comment():
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text()
        for line in text.splitlines():
            if re.match(r"\s*(- )?uses:", line):
                assert re.search(r"@[0-9a-f]{40}\s+# v\d", line), f"{path.name}: {line.strip()}"
        assert "pull_request_target" not in text
        assert "permissions:" in text


STANDARDS = "Legal-Copy7045/project-standards/.github/workflows/"


def _triggers(workflow):
    return workflow[True] if True in workflow else workflow["on"]  # YAML 1.1 reads "on" as True


def _reusable(workflow, name):
    """The job calling the standard's reusable workflow `name`, pinned by commit."""
    jobs = [
        job
        for job in workflow["jobs"].values()
        if job.get("uses", "").startswith(STANDARDS + name + "@")
    ]
    assert jobs, f"no job uses the standard's {name}"
    for job in jobs:
        assert re.fullmatch(STANDARDS + re.escape(name) + r"@[0-9a-f]{40}", job["uses"])
    return jobs


def test_ci_keeps_the_known_gates_and_ci_result_needs_them_all():
    ci = _workflow("ci.yml")
    jobs = ci["jobs"]
    assert jobs["test"]["needs"] == ["changes", "test-shard"]  # the familiar "test" line
    assert {"lint", "audit", "test-shard", "test", "docker", "image-scan", "pages"} <= set(jobs)
    scan = jobs["image-scan"]["steps"]
    trivy = next(s for s in scan if "aquasecurity/trivy-action" in s.get("uses", ""))
    assert trivy["with"]["severity"] == "HIGH,CRITICAL"
    assert trivy["with"]["ignore-unfixed"] is True and trivy["with"]["exit-code"] == "1"
    assert any("GITHUB_STEP_SUMMARY" in s.get("run", "") for s in scan)
    pages = jobs["pages"]
    assert pages["timeout-minutes"] <= 15
    assert any("run.py --stage page" in s.get("run", "") for s in pages["steps"])
    docker = " ".join(s.get("run", "") for s in jobs["docker"]["steps"])
    assert "docker-compose.tunnel.yml config" in docker
    # D90: ci-result is the one required check; it needs every other job and
    # fails when any of them failed or was cancelled.
    gate = jobs["ci-result"]
    assert set(gate["needs"]) == set(jobs) - {"ci-result"}
    assert gate["if"].startswith("always()")
    assert "failure|cancelled" in gate["steps"][0]["run"]
    lint = " ".join(s.get("run", "") for s in jobs["lint"]["steps"])
    assert "check_versions.py" in lint and "ruff check" in lint and "bandit" in lint


def test_every_job_has_a_timeout_and_the_workflows_least_permissions():
    for path in WORKFLOWS.glob("*.yml"):
        wf = yaml.safe_load(path.read_text())
        assert wf["permissions"] == {"contents": "read"}, path.name
        for name, job in wf["jobs"].items():
            if "uses" not in job:  # reusable workflows set their own timeouts
                assert job.get("timeout-minutes"), f"{path.name}: {name}"
    jobs = _workflow("ci.yml")["jobs"]
    widened = {n for n, j in jobs.items() if j.get("permissions")}
    assert widened == {"release"} and jobs["release"]["permissions"] == {"contents": "write"}


def test_secret_scan_covers_the_whole_history_weekly_and_fails_on_findings():
    # D57 gitleaks, folded into ci.yml through the standard's reusable workflow (D90):
    # a pull request's commits; the whole history on the weekly and manual runs.
    ci = _workflow("ci.yml")
    (job,) = _reusable(ci, "gitleaks.yml")
    assert "needs.changes.outputs.full" in job["with"]["full-history"]
    assert not (WORKFLOWS / "secrets.yml").exists()
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert config["extend"]["useDefault"] is True
    # Allowances are markers or exact fixtures, never whole directories.
    for allow in config["allowlists"]:
        for path in allow.get("paths", []):
            assert path.startswith("^") and path.endswith("$"), path
        if allow.get("paths"):
            assert allow.get("condition") == "AND" and allow.get("regexes")


def test_privacy_guard_runs_on_pull_requests_and_the_private_terms_on_main():
    ci = _workflow("ci.yml")
    jobs = {job.get("with", {}).get("denylist", False): job for job in _reusable(ci, "privacy.yml")}
    assert jobs[False]["if"] == "github.event_name != 'push'"
    assert "github.event_name == 'push'" in jobs[True]["if"]
    assert jobs[True]["secrets"] == "inherit"
    assert (ROOT / ".github" / "scripts" / "privacy_guard.py").is_file()


def test_codeql_scans_actions_python_and_javascript_weekly():
    # D86 (the owner: "Schedule codeQL weekly"): weekly and on demand only.
    wf = _workflow("codeql.yml")
    triggers = _triggers(wf)
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    assert "pull_request" not in triggers and "push" not in triggers
    assert triggers["schedule"] == [{"cron": "17 5 * * 0"}]  # Sunday, two hours after CI
    (job,) = _reusable(wf, "codeql.yml")
    languages = set(yaml.safe_load(job["with"]["languages"]))
    assert languages == {"actions", "python", "javascript-typescript"}
    assert job["permissions"]["security-events"] == "write"


def test_ci_is_economical():
    """D86, kept by D90: nothing on drafts, no re-test on merge to main (only
    the private-terms check and the release path), the slow groups only when a
    pull request touches what they test, a weekly full run, two test shards,
    one image build per run."""
    ci = _workflow("ci.yml")
    triggers = _triggers(ci)
    assert set(triggers) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["schedule"] == [{"cron": "17 3 * * 0"}]  # Sunday 03:17 UTC
    jobs = ci["jobs"]
    assert "draft" in jobs["changes"]["if"] and "draft" in jobs["ci-result"]["if"]
    # Every other job waits for "changes", so a draft (changes skipped) runs nothing.
    for name, job in jobs.items():
        if name not in ("changes", "ci-result"):
            needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
            assert "changes" in needs, name
    # On a push to main only the private-terms check and the release run.
    for name in ("lint", "privacy", "secrets", "docker"):
        assert jobs[name]["if"] == "github.event_name != 'push'", name
    for name, group in (("test-shard", "tests"), ("image-scan", "image"), ("pages", "pages")):
        assert jobs[name]["if"] == f"needs.changes.outputs.{group} == 'true'", name
    assert "needs.changes.outputs.lockfiles" in jobs["audit"]["if"]
    assert jobs["release"]["if"] == "needs.changes.outputs.release == 'true'"
    assert len(jobs["test-shard"]["strategy"]["matrix"]["shard"]) == 2
    builds = [
        n for n, j in jobs.items() for s in j.get("steps", []) if "docker build" in s.get("run", "")
    ]
    assert builds == ["image-scan"]  # built once, in image-scan
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    deps = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    for update in deps["updates"]:
        assert update["schedule"] == {"interval": "weekly", "day": "sunday", "time": "00:30"}
        (group,) = update["groups"].values()
        assert group["update-types"] == ["minor", "patch"], update["package-ecosystem"]
    auto = _workflow("dependabot-automerge.yml")
    (job,) = _reusable(auto, "dependabot-automerge.yml")
    assert job["if"] == "github.event.pull_request.user.login == 'dependabot[bot]'"


def _groups(path: str) -> set[str]:
    cfg = yaml.safe_load((ROOT / ".github" / "changes.yaml").read_text())

    def hit(patterns):
        return any(re.search(p, path) for p in patterns or [])

    if hit(cfg["ignore"]):
        return set()
    found = {g for g, pats in cfg["groups"].items() if hit(pats)}
    return set(cfg["groups"]) if hit(cfg["shared"]) or not found else found


@pytest.mark.parametrize(
    ("path", "groups"),
    [
        ("docs/decisions.md", set()),
        ("regression/RUNS.md", set()),
        ("README.md", set()),
        ("docs/claude/ledger.md", {"tests"}),  # CLAUDE.md's move check reads it
        ("CLAUDE.md", {"tests"}),
        ("ledger/services.py", {"tests"}),
        ("templates/base.html", {"tests", "pages"}),
        ("core/navigation.py", {"tests", "pages"}),
        ("Dockerfile", {"tests", "image"}),
        ("docker-compose.yml", {"tests", "image"}),
        ("requirements/base.txt", {"tests", "image", "pages", "lockfiles"}),
        (".github/workflows/ci.yml", {"tests", "image", "pages", "lockfiles"}),
        ("somewhere/new.unknown", {"tests", "image", "pages", "lockfiles"}),
    ],
)
def test_changes_start_the_right_groups(path, groups):
    assert _groups(path) == groups


def test_release_path_and_versions_files():
    """STANDARD.md 2: the release notes come from CHANGELOG.md; the version
    check and the upstream list the weekly maintenance reads exist."""
    for name in ("changes.py", "check_versions.py", "release_notes.py", "privacy_guard.py"):
        assert (ROOT / ".github" / "scripts" / name).is_file(), name
    assert yaml.safe_load((ROOT / ".github" / "versions.yaml").read_text()) == []
    ups = yaml.safe_load((ROOT / ".github" / "upstreams.yaml").read_text())
    assert {"Playwright (page crawl and walkthrough browser)", "PostgreSQL major line"} <= {
        u["name"] for u in ups
    }
    std = yaml.safe_load((ROOT / ".github" / "standard.yaml").read_text())
    assert std["standard"] == "1.2.2" and std["beyond"] and std["exceptions"]
