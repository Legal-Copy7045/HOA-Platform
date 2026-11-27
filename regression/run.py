#!/usr/bin/env python3
"""Full regression run: every check in the repo, one result per check, stable IDs.

The owner's standard command set (STANDARD.md 3.3, D90): the stages are this
project's own code; everything after them goes through regression/standard.py
(vendored unchanged from the standards repository):

    python3 regression/run.py check           # ci + unit, quiet: before a PR is marked ready
    python3 regression/run.py                 # every regression stage except live and walkthrough
    python3 regression/run.py --stage unit    # some stages only (repeatable)
    python3 regression/run.py --stage walkthrough      # the walkthrough only
    python3 regression/run.py --with-walkthrough       # regression + walkthrough together
    LIVE_HOST=portal-dev.example.org python3 regression/run.py --stage live
    python3 regression/run.py brief [--full] [--walkthrough]    # reviewer briefs (standard.py)
    python3 regression/run.py report [--walkthrough]   # report.py / walkthrough_report.py
    python3 regression/run.py log [--walkthrough | --full-check]  # RUNS.md / WALKTHROUGHS.md row
    python3 regression/run.py summary         # at most 40 lines (standard.py)
    python3 regression/run.py manual KEY pass|fail --note "..."   # manual-log.yaml (standard.py)

Stages run side by side in three lanes (ci | unit | page); live runs last, and only
when asked. The walkthrough stage (every role and page on a desktop and a phone,
regression/walkthrough.js) is not a default stage: it runs alone with --stage
walkthrough, or in a fourth lane with --with-walkthrough, on its own database
and port so it never collides with the page stage. Each stage writes out/<stage>.json
(the walkthrough: out/walkthrough-checks.json); logs go to out/logs/. See
regression/README.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import signal
import socket
import subprocess  # nosec B404 - fixed command lists only
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # nosec B405 - parses our own pytest output
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standard  # vendored from the standards repository, never edited here)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"
LOGS = OUT / "logs"
IDS = HERE / "ids"
PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv").exists() else sys.executable

PREFIX = {
    "ci": "CI",
    "unit": "UNIT",
    "page": "PAGE",
    "live": "LIVE",
    "manual": "MAN",
    "walkthrough": "WALK",
}
ORDER = ["ci", "unit", "page", "live", "manual", "walkthrough"]
# Plain `run.py`: the regression stages (live only when asked, walkthrough only
# with --stage walkthrough or --with-walkthrough).
DEFAULT_STAGES = ["ci", "unit", "page", "manual"]
PARALLEL = ("ci", "unit", "page", "walkthrough")
UNIT_LANES = 3
# Ports can be moved (REGRESSION_PAGE_PORT, REGRESSION_WALK_PORT) so two checkouts can run at once.
PAGE_PORT = int(os.environ.get("REGRESSION_PAGE_PORT", "8011"))
WALK_PORT = int(os.environ.get("REGRESSION_WALK_PORT", "8012"))
# Stage results go to out/<stage>.json, except where that name is taken:
# out/walkthrough.json is the walkthrough's inventory (role -> width -> pages).
RESULT_FILE = {"walkthrough": "walkthrough-checks.json"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- Stable IDs -----------------------------------------------------------------------

_LINE = re.compile(r'^([A-Z]+-\d{3,})\s*:\s*(".*")\s*$')
_HIGH = re.compile(r"^# highest issued:\s*[A-Z]+-(\d+)\s*$")


class Ids:
    """ids/<stage>.yaml: `PREFIX-NNN: "key"`, append-only. New keys get the next number."""

    def __init__(self, stage: str):
        self.stage, self.prefix = stage, PREFIX[stage]
        self.path = IDS / f"{stage}.yaml"
        self.by_key: dict[str, str] = {}
        self.high = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if m := _HIGH.match(line):
                    self.high = max(self.high, int(m.group(1)))
                elif m := _LINE.match(line):
                    self.by_key[json.loads(m.group(2))] = m.group(1)
                    self.high = max(self.high, int(m.group(1).split("-")[1]))
        self.used: set[str] = set()

    def get(self, key: str) -> str:
        if key not in self.by_key:
            self.high += 1
            self.by_key[key] = f"{self.prefix}-{self.high:03d}"
        self.used.add(key)
        return self.by_key[key]

    def save(self) -> None:
        IDS.mkdir(exist_ok=True)
        lines = [
            f"# Stable check IDs for the {self.stage} stage (regression/run.py). Append-only: never",
            "# renumber or reuse an ID. Renamed a check? Edit its key here to keep its ID.",
            f"# highest issued: {self.prefix}-{self.high:03d}",
        ]
        for key, rid in sorted(self.by_key.items(), key=lambda kv: kv[1]):
            lines.append(f"{rid}: {json.dumps(key, ensure_ascii=False)}")
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Stage:
    def __init__(self, name: str):
        self.name = name
        self.ids = Ids(name)
        self.results: list[dict] = []
        self.started = now()

    def add(self, check: str, status: str, detail: str = "", evidence: str = "") -> None:
        self.results.append(
            {
                "id": self.ids.get(check),
                "key": check,  # the standard's name (standard.py reads "key")
                "check": check,
                "status": status,
                "detail": detail[:2000],
                "evidence": evidence,
            }
        )

    def finish(self, environment: dict | None = None) -> dict:
        # Keys this run no longer produced stay listed as skipped until removed.
        for key, rid in self.ids.by_key.items():
            if key not in self.ids.used:
                self.results.append(
                    {
                        "id": rid,
                        "key": key,
                        "check": key,
                        "status": "skip",
                        "detail": "not produced by this run",
                        "evidence": "",
                    }
                )
        self.ids.save()
        data = {
            "stage": self.name,
            "prefix": PREFIX[self.name],
            "started": self.started,
            "finished": now(),
            "environment": environment or {},
            "results": sorted(self.results, key=lambda r: r["id"]),
        }
        result_file = RESULT_FILE.get(self.name, f"{self.name}.json")
        (OUT / result_file).write_text(json.dumps(data, indent=1), encoding="utf-8")
        return data


def run(cmd, *, log: str, env=None, timeout=1800, cwd=ROOT) -> tuple[int, str]:
    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(  # nosec B603 - fixed command lists
            cmd,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        text, code = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        text, code = f"timed out after {timeout}s\n{exc.stdout or ''}", 124
    except FileNotFoundError as exc:
        text, code = f"not installed: {exc}", 127
    (LOGS / log).write_text(text, encoding="utf-8")
    return code, text


def tail(text: str, n: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


# --- ci ----------------------------------------------------------------------------------

DUMMY_PROD_ENV = {
    "DJANGO_SETTINGS_MODULE": "core.settings.production",
    "DJANGO_SECRET_KEY": "regression-check-only-" + "0123456789abcdef" * 3,
    "DJANGO_ALLOWED_HOSTS": "portal.example.org",
    "DATABASE_URL": "postgres://app:app@127.0.0.1:1/none",
    "DB_APP_ROLE": "app",
    "AUDIT_HASH_KEY": "k" * 40,
    "TRUSTED_PROXY_CIDRS": "172.30.50.10/32",
    "DJANGO_DEBUG": "false",
    # Production refuses to start without ClamAV and clean-scan downloads
    # (D57); HSTS defaults to a year there and mail is never redirected.
    "DEPLOYMENT_STAGE": "production",
    "DOCUMENT_SCANNER": "clamd",
    "DOCUMENT_CLAMD_ADDRESS": "clamav:3310",
    "DOCUMENT_REQUIRE_CLEAN_SCAN": "true",
    "EMAIL_REDIRECT_ALL_TO": "",
}
# check --deploy warnings accepted during the POC (see docs/security-review-2026-11.md).
ACCEPTED_DEPLOY_WARNINGS = {"security.W005", "security.W021", "hoa.W001"}


def stage_ci(st: Stage) -> None:
    tools = Path(PY).parent
    checks = [
        ("ruff lint", [str(tools / "ruff"), "check", "."]),
        ("ruff format", [str(tools / "ruff"), "format", "--check", "."]),
        ("bandit security scan", [str(tools / "bandit"), "-q", "-c", "pyproject.toml", "-r", "."]),
        (
            "migrations up to date",
            [
                PY,
                "manage.py",
                "makemigrations",
                "--check",
                "--dry-run",
                "--settings=core.settings.test",
            ],
        ),
    ]
    for name, cmd in checks:
        code, text = run(cmd, log=f"ci-{name.replace(' ', '-')}.log")
        st.add(
            name,
            "pass" if code == 0 else "fail",
            "" if code == 0 else tail(text),
            f"regression/out/logs/ci-{name.replace(' ', '-')}.log",
        )

    code, text = run(
        [
            str(tools / "pip-audit"),
            "--strict",
            "-r",
            "requirements/base.txt",
            "-r",
            "requirements/dev.txt",
        ],
        log="ci-pip-audit.log",
        timeout=600,
    )
    offline = "ConnectionError" in text or "Max retries" in text or "Name or service" in text
    st.add(
        "dependency audit (pip-audit)",
        "skip" if offline else ("pass" if code == 0 else "fail"),
        "no network for the vulnerability database" if offline else tail(text),
        "regression/out/logs/ci-pip-audit.log",
    )

    code, text = run(
        [PY, "manage.py", "check", "--deploy"], env=DUMMY_PROD_ENV, log="ci-check-deploy.log"
    )
    found = set(re.findall(r"\(([a-z]+\.[A-Z]\d{3})\)", text))
    unexpected = found - ACCEPTED_DEPLOY_WARNINGS
    st.add(
        "production settings pass check --deploy",
        "pass" if code == 0 and not unexpected else "fail",
        f"unexpected: {sorted(unexpected)}" if unexpected else f"accepted: {sorted(found)}",
        "regression/out/logs/ci-check-deploy.log",
    )

    # The standard's version check and privacy guard (.github/scripts, D90).
    for name, script, log in (
        ("versions (VERSION, CHANGELOG.md, versions.yaml)", "check_versions.py", "ci-versions.log"),
        ("privacy guard", "privacy_guard.py", "ci-privacy.log"),
    ):
        code, text = run(
            [PY, str(ROOT / ".github" / "scripts" / script)],
            env={"DEFAULT_BRANCH": "main"},
            log=log,
        )
        st.add(
            name,
            "pass" if code == 0 else "fail",
            "" if code == 0 else tail(text),
            f"regression/out/logs/{log}",
        )

    # Repository hygiene.
    tracked = subprocess.run(  # nosec B603 B607
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.splitlines()
    env_files = [
        f for f in tracked if re.search(r"(^|/)\.env($|\.)", f) and not f.endswith(".env.example")
    ]
    st.add("no .env files tracked", "fail" if env_files else "pass", ", ".join(env_files))
    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    unpinned = [
        f"{w.name}: {u}"
        for w in workflows
        for u in re.findall(r"uses:\s*(\S+)", w.read_text(encoding="utf-8"))
        if not re.search(r"@[0-9a-f]{40}$", u)
    ]
    st.add("CI actions pinned to commits", "fail" if unpinned else "pass", "; ".join(unpinned))
    perms = all("permissions:" in w.read_text(encoding="utf-8") for w in workflows)
    st.add("CI workflows declare permissions", "pass" if perms else "fail")
    no_prt = all("pull_request_target" not in w.read_text(encoding="utf-8") for w in workflows)
    st.add("no pull_request_target", "pass" if no_prt else "fail")
    ignored = (
        subprocess.run(  # nosec B603 B607
            ["git", "check-ignore", "-q", "regression/out/x"], cwd=ROOT, check=False
        ).returncode
        == 0
    )
    st.add("regression output git-ignored", "pass" if ignored else "fail")


# --- unit --------------------------------------------------------------------------------


def _test_files() -> list[Path]:
    return sorted(p for p in ROOT.glob("*/tests/test_*.py") if ".venv" not in p.parts)


def _lanes(files: list[Path], n: int) -> list[list[Path]]:
    """Balance by file size; the cross-owner URL matrix (the slowest) gets a lane of its own."""
    heavy = [f for f in files if f.name == "test_url_matrix.py"]
    rest = sorted((f for f in files if f not in heavy), key=lambda f: -f.stat().st_size)
    shared = [[] for _ in range(n - 1 if heavy else n)]
    weights = [0] * len(shared)
    for f in rest:
        i = weights.index(min(weights))
        shared[i].append(f)
        weights[i] += f.stat().st_size
    return [lane for lane in ([heavy] if heavy else []) + shared if lane]


def stage_unit(st: Stage) -> None:
    lanes = _lanes(_test_files(), UNIT_LANES)
    base = os.environ.get(
        "REGRESSION_DATABASE_URL", "postgres://hoa_owner:hoa_owner@localhost:5432/hoa"
    )

    def lane(i_files):
        i, files = i_files
        xml = OUT / f"unit-lane{i}.xml"
        code, text = run(
            [
                PY,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--junitxml",
                str(xml),
                *[str(f.relative_to(ROOT)) for f in files],
            ],
            env={"DATABASE_URL": f"{base}_reg{i}", "DJANGO_SETTINGS_MODULE": "core.settings.test"},
            log=f"unit-lane{i}.log",
            timeout=1800,
        )
        return i, code, text, xml

    with concurrent.futures.ThreadPoolExecutor(len(lanes)) as pool:
        outcomes = list(pool.map(lane, enumerate(lanes)))
    for i, code, text, xml in outcomes:
        failed_in_xml = 0
        if xml.exists():
            for case in ET.parse(xml).getroot().iter("testcase"):  # nosec B314
                path = (case.get("classname") or "").replace(".", "/")
                key = f"{path}.py::{case.get('name')}"
                if case.find("failure") is not None or case.find("error") is not None:
                    node = (
                        case.find("failure")
                        if case.find("failure") is not None
                        else case.find("error")
                    )
                    st.add(
                        key,
                        "fail",
                        (node.get("message") or "")[:500],
                        f"regression/out/logs/unit-lane{i}.log",
                    )
                    failed_in_xml += 1
                elif case.find("skipped") is not None:
                    st.add(key, "skip", case.find("skipped").get("message") or "")
                else:
                    st.add(key, "pass")
        if code not in (0, 1) or (code == 1 and not failed_in_xml):
            st.add(f"lane {i} ran", "fail", tail(text), f"regression/out/logs/unit-lane{i}.log")


# --- page --------------------------------------------------------------------------------

PAGE_SETTINGS = "regression.page_settings"


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _demo_site(st: Stage, *, db: str, port: int, log: str, sessions: list[str]):
    """Fresh demo database, one session per role, and a server on `port`.

    Returns (env, server process), or None after recording the step that failed.
    """
    env = {
        "DJANGO_SETTINGS_MODULE": PAGE_SETTINGS,
        "PYTHONPATH": str(ROOT),
        "DATABASE_URL": f"postgres://hoa_owner:hoa_owner@localhost:5432/{db}",
    }
    steps = [
        ("fresh database", [PY, "regression/page_setup.py", "reset-db", db]),
        ("cache table", [PY, "manage.py", "createcachetable"]),
        ("migrate", [PY, "manage.py", "migrate", "--noinput"]),
        ("seed demo community", [PY, "manage.py", "seed_demo"]),
        ("sessions for each role", [PY, "regression/page_setup.py", *sessions]),
    ]
    for name, cmd in steps:
        code, text = run(cmd, env=env, log=f"{log}-{name.replace(' ', '-')}.log", timeout=900)
        if code != 0:
            st.add(
                f"setup: {name}",
                "fail",
                tail(text),
                f"regression/out/logs/{log}-{name.replace(' ', '-')}.log",
            )
            return None
    if not _port_free(port):
        st.add("setup: server port free", "fail", f"port {port} is in use")
        return None
    server = subprocess.Popen(  # nosec B603
        [PY, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
        cwd=ROOT,
        env={**os.environ, **env},
        stdout=open(LOGS / f"{log}-server.log", "w"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(60):
        if not _port_free(port):
            break
        time.sleep(0.5)
    return env, server


def stage_page(st: Stage) -> None:
    if shutil.which("node") is None:
        st.add("browser available", "skip", "node not installed")
        return
    site = _demo_site(
        st,
        db=os.environ.get("REGRESSION_PAGES_DB", "hoa_reg_pages"),
        port=PAGE_PORT,
        log="page",
        sessions=["sessions", str(OUT / "page-sessions.json")],
    )
    if site is None:
        return
    env, server = site
    try:
        _code, text = run(
            [
                "node",
                str(HERE / "pages.js"),
                f"http://127.0.0.1:{PAGE_PORT}",
                str(OUT / "page-sessions.json"),
                str(OUT / "pages.json"),
            ],
            log="page-crawl.log",
            timeout=1200,
        )
        if not (OUT / "pages.json").exists():
            st.add("crawl ran", "fail", tail(text), "regression/out/logs/page-crawl.log")
            return
        for item in json.loads((OUT / "pages.json").read_text(encoding="utf-8")):
            st.add(
                item["check"],
                item["status"],
                item.get("detail", ""),
                "regression/out/logs/page-crawl.log",
            )
        # Passkey sign-in end to end (WebAuthn needs a host name, not an IP).
        totp_cmd = f"{PY} {HERE / 'page_setup.py'} totp"
        _code, text = run(
            [
                "node",
                str(HERE / "passkey.js"),
                f"http://localhost:{PAGE_PORT}",
                str(OUT / "passkey-user.json"),
                totp_cmd,
            ],
            env=env,
            log="page-passkey.log",
            timeout=300,
        )
        lines = [ln for ln in text.splitlines() if ln.startswith("{")]
        for line in lines:
            item = json.loads(line)
            st.add(
                item["check"],
                item["status"],
                item.get("detail", ""),
                "regression/out/logs/page-passkey.log",
            )
        if not lines:
            st.add(
                "passkey: flow completed",
                "fail",
                tail(text),
                "regression/out/logs/page-passkey.log",
            )
    finally:
        os.killpg(server.pid, signal.SIGTERM)


# --- walkthrough -------------------------------------------------------------------------


def stage_walkthrough(st: Stage) -> None:
    """Every role, every menu and hub page, desktop and phone: inventory + UX checks."""
    if shutil.which("node") is None:
        st.add("browser available", "skip", "node not installed")
        return
    shots = OUT / "walkthrough"
    shutil.rmtree(shots, ignore_errors=True)
    for name in ("walkthrough.json", "walkthrough-crawl.json"):
        (OUT / name).unlink(missing_ok=True)
    site = _demo_site(
        st,
        db=os.environ.get("REGRESSION_WALK_DB", "hoa_reg_walk"),
        port=WALK_PORT,
        log="walk",
        sessions=["walk-sessions", str(OUT / "walk-sessions.json")],
    )
    if site is None:
        return
    _env, server = site
    try:
        _code, text = run(
            [
                "node",
                str(HERE / "walkthrough.js"),
                f"http://127.0.0.1:{WALK_PORT}",
                str(OUT / "walk-sessions.json"),
                str(OUT / "walkthrough.json"),
                str(shots),
                str(OUT / "walkthrough-crawl.json"),
            ],
            log="walk-crawl.log",
            timeout=5400,
        )
        crawl = OUT / "walkthrough-crawl.json"
        if not crawl.exists():
            st.add("walkthrough ran", "fail", tail(text), "regression/out/logs/walk-crawl.log")
            return
        for item in json.loads(crawl.read_text(encoding="utf-8")):
            st.add(
                item["check"],
                item["status"],
                item.get("detail", ""),
                item.get("evidence") or "regression/out/logs/walk-crawl.log",
            )
    finally:
        os.killpg(server.pid, signal.SIGTERM)


# --- live (read-only) ---------------------------------------------------------------------


def _is_cloudflare_access(location: str) -> bool:
    """A redirect to the Cloudflare Access sign-in: the host itself must be
    cloudflareaccess.com or a subdomain of it (not just contain the name)."""
    host = (urllib.parse.urlsplit(location).hostname or "").lower()
    return host == "cloudflareaccess.com" or host.endswith(".cloudflareaccess.com")


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=20) as resp:  # nosec B310 - https only, fixed hosts
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def stage_live(st: Stage) -> None:
    host = os.environ.get("LIVE_HOST", "")
    if not host:
        st.add("live host configured", "skip", "set LIVE_HOST (never stored in the repo)")
        return
    try:
        status, headers, _ = _get(f"https://{host}/healthz")
    except OSError as exc:
        st.add(
            "site reachable over HTTPS", "skip", f"not reachable from here: {type(exc).__name__}"
        )
    else:
        st.add("site reachable over HTTPS", "pass", f"HTTP {status}")
        location = headers.get("Location", "") or headers.get("location", "")
        behind_access = status in (302, 303) and _is_cloudflare_access(location)
        st.add(
            "POC is behind Cloudflare Access",
            "pass" if behind_access else "fail",
            f"HTTP {status} -> {location[:80]}",
        )
        try:
            status, _, _ = _get(f"http://{host}/healthz")
            st.add(
                "plain HTTP does not serve the portal",
                "pass" if status in (301, 302, 307, 308, 403) else "fail",
                f"HTTP {status}",
            )
        except OSError:
            st.add("plain HTTP does not serve the portal", "pass", "no plain-HTTP listener")
    # Cloudflare API (credentials injected by the session's proxy; GET only).
    api = "https://api.cloudflare.com/client/v4"
    try:
        apex = ".".join(host.split(".")[-2:])
        _, _, body = _get(f"{api}/zones?name={apex}")
        zone = json.loads(body)["result"][0]
        _, _, body = _get(f"{api}/zones/{zone['id']}/dns_records?name={host}")
        record = json.loads(body)["result"][0]
        st.add(
            "DNS record is proxied through Cloudflare",
            "pass" if record.get("proxied") else "fail",
            record.get("type", ""),
        )
        _, _, body = _get(f"{api}/zones/{zone['id']}/settings/min_tls_version")
        tls = json.loads(body)["result"]["value"]
        st.add("minimum TLS 1.2 or higher", "pass" if tls in ("1.2", "1.3") else "fail", tls)
        account = zone["account"]["id"]
        _, _, body = _get(f"{api}/accounts/{account}/cfd_tunnel?is_deleted=false")
        tunnels = json.loads(body)["result"]
        target = record.get("content", "").split(".")[0]
        tunnel = next((t for t in tunnels if t["id"] == target), None)
        st.add(
            "tunnel healthy",
            "pass" if tunnel and tunnel.get("status") == "healthy" else "fail",
            (tunnel or {}).get("status", "tunnel not found"),
        )
        _, _, body = _get(f"{api}/accounts/{account}/access/apps")
        apps = [
            a for a in json.loads(body).get("result") or [] if a.get("domain", "").startswith(host)
        ]
        st.add(
            "Access application covers the host", "pass" if apps else "fail", f"{len(apps)} app(s)"
        )
    except (OSError, KeyError, IndexError, ValueError) as exc:
        st.add(
            "Cloudflare configuration readable",
            "skip",
            f"API not available here: {type(exc).__name__}",
        )


# --- manual ------------------------------------------------------------------------------


def stage_manual(st: Stage) -> None:
    """manual.yaml: "needs a person" unless `run.py manual KEY pass|fail` recorded a
    result for this version in manual-log.yaml (STANDARD.md 3.1)."""
    path = HERE / "manual-log.yaml"
    log = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    for item in yaml.safe_load((HERE / "manual.yaml").read_text(encoding="utf-8")) or []:
        rec = log.get(item["key"]) or {}
        if rec.get("version") == version and rec.get("result") in ("pass", "fail"):
            st.add(item["key"], rec["result"], f"{rec.get('date', '')}: {rec.get('note', '')}")
        else:
            last = item.get("last", "not done yet")
            st.add(item["key"], "manual", f"{item['check']} (last: {last})")


STAGES = {
    "ci": stage_ci,
    "unit": stage_unit,
    "page": stage_page,
    "live": stage_live,
    "manual": stage_manual,
    "walkthrough": stage_walkthrough,
}


def chosen_stages(argv: list[str] | None = None) -> list[str]:
    """The stages a command line asks for, in ORDER.

    The default is the regression stages: no live (needs LIVE_HOST) and no
    walkthrough (`--stage walkthrough` alone, or `--with-walkthrough` to add it).
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--stage", action="append", choices=ORDER)
    parser.add_argument(
        "--with-walkthrough",
        action="store_true",
        help="also run the walkthrough stage, in a lane of its own beside the others",
    )
    args = parser.parse_args(argv)
    chosen = set(args.stage or DEFAULT_STAGES)
    if args.with_walkthrough:
        chosen.add("walkthrough")
    return [s for s in ORDER if s in chosen]


# --- after the stages: the standard's tooling (STANDARD.md 3.3) ----------------------------

TOOLING = {"brief", "report", "log", "summary", "manual", "schema", "walkthrough-due"}


def report(argv: list[str]) -> int:
    """`run.py report [--walkthrough]`: this project's own reports (they read the
    standard findings files too), in the profile's time zone."""
    parser = argparse.ArgumentParser(prog="run.py report")
    parser.add_argument("--walkthrough", action="store_true")
    parser.add_argument("--no-thumbs", action="store_true")
    args = parser.parse_args(argv)
    env = {"REPORT_TZ": os.environ.get("REPORT_TZ") or standard.profile().get("report_tz", "UTC")}
    script = "walkthrough_report.py" if args.walkthrough else "report.py"
    cmd = [sys.executable, str(HERE / script)] + (["--no-thumbs"] if args.no_thumbs else [])
    code = subprocess.run(cmd, cwd=ROOT, env={**os.environ, **env}, check=False).returncode  # nosec B603
    return code


def _walk_counts() -> tuple[int, int, int, int]:
    """Roles, page types, failing WALK- checks and reviewer findings of the last walkthrough
    (this project's inventory is {"roles": {role: {desktop|phone: {menu, pages}}}})."""
    inventory = (
        json.loads((OUT / "walkthrough.json").read_text())
        if (OUT / "walkthrough.json").exists()
        else {}
    )
    roles = inventory.get("roles", {})
    pages = sum(len((w.get("desktop") or {}).get("pages", [])) for w in roles.values())
    checks = OUT / "walkthrough-checks.json"
    failing = (
        sum(1 for r in json.loads(checks.read_text()).get("results", []) if r["status"] == "fail")
        if checks.exists()
        else 0
    )
    sys.path.insert(0, str(HERE))
    import walkthrough_report

    findings = len(walkthrough_report.merged_findings(OUT).get("findings", []))
    return len(roles), pages, failing, findings


def log(argv: list[str]) -> int:
    """`run.py log [--walkthrough | --full-check]`: the standard rows in RUNS.md and
    WALKTHROUGHS.md (standard.py's helpers; the walkthrough counts read this
    project's inventory)."""
    parser = argparse.ArgumentParser(prog="run.py log")
    parser.add_argument("--walkthrough", action="store_true")
    parser.add_argument("--full-check", action="store_true")
    args = parser.parse_args(argv)
    if not args.walkthrough or args.full_check:
        # standard.cmd_log writes its own WALKTHROUGHS.md row on a full check, from an
        # inventory shape this project does not use (5 roles, 0 pages); this project
        # writes the walkthrough row below, so only the RUNS.md row comes from there.
        add_row = standard.add_row
        standard.add_row = lambda path, *a, **k: (
            None if Path(path).name == "WALKTHROUGHS.md" else add_row(path, *a, **k)
        )
        try:
            standard.cmd_log(argparse.Namespace(walkthrough=False, full_check=args.full_check))
        finally:
            standard.add_row = add_row
    if args.walkthrough or args.full_check:
        sha, ver = standard.git("rev-parse", "--short", "HEAD"), standard.version()
        date = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        roles, pages, failing, findings = _walk_counts()
        rep = standard.link("walkthrough") + (
            " · " + standard.link("regression") if args.full_check else ""
        )
        row = [date, f"`{sha}`", ver, str(roles), str(pages), str(failing), str(findings), rep]
        standard.add_row(HERE / "WALKTHROUGHS.md", standard.WALKS_HEADER, "Walkthroughs", row)
        print("logged in regression/WALKTHROUGHS.md")
    return 0


def tooling(argv: list[str]) -> int:
    if argv[0] == "report":
        return report(argv[1:])
    if argv[0] == "log":
        return log(argv[1:])
    return standard.main(argv)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in TOOLING:
        return tooling(sys.argv[1:])
    check = len(sys.argv) > 1 and sys.argv[1] == "check"
    chosen = ["ci", "unit"] if check else chosen_stages()
    OUT.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    started = time.time()

    def go(name):
        st = Stage(name)
        try:
            STAGES[name](st)
        except Exception as exc:  # a broken stage is a result, not a crash
            st.add(f"{PREFIX[name]}-RUN", "fail", f"{type(exc).__name__}: {exc}")
        data = st.finish({"python": sys.version.split()[0]})
        # One line per stage and the failing checks (full output in out/logs/).
        standard.print_stage_line(data, quiet=True)
        if name == "walkthrough":
            print("walkthrough done", flush=True)  # /full-check starts its reviewers on this
        return data

    lanes = [[s] for s in PARALLEL if s in chosen]
    with concurrent.futures.ThreadPoolExecutor(max(1, len(lanes))) as pool:
        results = list(pool.map(lambda lane: [go(s) for s in lane], lanes))
    for name in ("live", "manual"):
        if name in chosen:
            results.append([go(name)])
    failed = sum(1 for lane in results for d in lane for r in d["results"] if r["status"] == "fail")
    print(f"done in {(time.time() - started) / 60:.1f} min; {failed} failing check(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
