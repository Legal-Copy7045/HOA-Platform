"""Infrastructure images are pinned only together with a way to test and
upgrade them (external review 2026-11-26, D57): each compose image carries a
version tag and a digest, Dependabot's docker-compose ecosystem proposes the
updates, and CI validates, builds and scans every proposal. Also the CI
security gates (secret scan, image scan, CodeQL, page crawl)."""

import re
import tomllib
from pathlib import Path

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


def test_ci_keeps_the_summary_test_check_and_adds_separate_gates():
    ci = _workflow("ci.yml")
    jobs = ci["jobs"]
    assert jobs["test"]["needs"] == ["test-shard"]  # the one required "test" check
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


def test_secret_scan_covers_the_whole_history_and_fails_on_findings():
    wf = _workflow("secrets.yml")
    triggers = wf[True] if True in wf else wf["on"]  # YAML 1.1 reads "on" as True
    assert {"pull_request", "schedule", "workflow_dispatch"} <= set(triggers)
    (job,) = wf["jobs"].values()
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0  # every commit, not just the tip
    assert any("gitleaks/gitleaks-action" in u for u in _uses(wf))
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert config["extend"]["useDefault"] is True
    # Allowances are markers or exact fixtures, never whole directories.
    for allow in config["allowlists"]:
        for path in allow.get("paths", []):
            assert path.startswith("^") and path.endswith("$"), path
        if allow.get("paths"):
            assert allow.get("condition") == "AND" and allow.get("regexes")


def test_codeql_scans_python_and_javascript_weekly():
    # D86 (the owner: "Schedule codeQL weekly"): weekly and on demand only.
    wf = _workflow("codeql.yml")
    triggers = wf[True] if True in wf else wf["on"]
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    assert "pull_request" not in triggers and "push" not in triggers
    (job,) = wf["jobs"].values()
    assert set(job["strategy"]["matrix"]["language"]) == {"python", "javascript-typescript"}
    assert job["permissions"]["security-events"] == "write"


def test_ci_is_economical():
    """D86: no second run on merge to main, nothing on drafts, the image and
    the page crawl only when a pull request touches what they test, a weekly
    full run on main, two test shards."""
    ci = _workflow("ci.yml")
    triggers = ci[True] if True in ci else ci["on"]
    assert "push" not in triggers
    assert {"pull_request", "schedule", "workflow_dispatch"} <= set(triggers)
    jobs = ci["jobs"]
    for name in ("lint", "audit", "test-shard", "docker", "changes"):
        assert "draft" in jobs[name]["if"], name
    for name in ("image-scan", "pages"):
        assert jobs[name]["needs"] == ["changes"]
        assert "needs.changes.outputs" in jobs[name]["if"]
    assert len(jobs["test-shard"]["strategy"]["matrix"]["shard"]) == 2
    docker = " ".join(s.get("run", "") for s in jobs["docker"]["steps"])
    assert "docker build" not in docker  # built once, in image-scan
    deps = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    for update in deps["updates"]:
        assert update["schedule"]["interval"] == "monthly"
        assert update.get("groups"), update["package-ecosystem"]
