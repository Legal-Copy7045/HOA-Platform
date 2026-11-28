#!/usr/bin/env python3
"""A full check before a release when one is due (D91, amended 2026-11-28).

A full check is the regression and the walkthrough together, logged in both
regression/RUNS.md and regression/WALKTHROUGHS.md (`/full-check`, then
`python3 regression/run.py log --full-check`): the newest RUNS.md row whose
Audit says "full check" and whose commit also has a WALKTHROUGHS.md row.

A release (VERSION without its v<VERSION> tag) needs a new full check when any
of these is true, otherwise not:

1. Weekly: the newest full check is more than ``max_age_days`` (7) older than
   the release date (the CHANGELOG.md section date of VERSION, else today).
2. Every 5 releases: ``every_releases`` (5) or more releases (CHANGELOG.md
   sections, patches included, the one being released counted) since the
   version on the newest full check.
3. Material security or accounting change: since the commit of the newest
   full check, a file matching ``full_check.material_paths`` in
   regression/profile.yaml changed and matches none of
   ``full_check.not_material`` (templates, static, docs, tests, regression).
   Needs the git history; skipped, and said so, in a shallow clone.
4. Major release: X.0.0 (X >= 1) always needs a full check on that version.

    python3 regression/release_logs.py     # prints the problems; exit 1 if any

Run by `python3 regression/run.py check` (ci stage, CI-014) and by
regression/tests/test_release_logs.py. Standard library only.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess  # nosec B404 - read-only git commands on this checkout
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# The standard tables' headers (regression/standard.py, vendored: not imported
# here so this file needs no PyYAML). Date, commit and version are the first
# three columns in both; Audit is the fourth in RUNS.md.
RUNS_HEADER = (
    "| Date (UTC) | Commit | Version | Audit | Pass | Fail | Skip | Manual due "
    "| Failing IDs | Findings | Report |"
)
WALKS_HEADER = (
    "| Date (UTC) | Commit | Version | Roles | Pages | UX checks failing | Findings | Report |"
)
HOW = "run /full-check and `python3 regression/run.py log --full-check`."
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")

Version = tuple[int, int, int]


@dataclass
class Settings:
    max_age_days: int = 7
    every_releases: int = 5
    material_paths: tuple[str, ...] = ()
    not_material: tuple[str, ...] = ()


@dataclass
class Entry:
    date: dt.date
    commit: str
    version: Version


def parse(version: str) -> Version | None:
    match = _SEMVER.match((version or "").strip().strip("`"))
    return tuple(int(n) for n in match.groups()) if match else None  # type: ignore[return-value]


def show(version: Version) -> str:
    return ".".join(map(str, version))


def rows(text: str, header: str) -> list[list[str]]:
    """The cells of the standard table's rows, newest (first) first."""
    lines = text.splitlines()
    try:
        start = lines.index(header) + 2
    except ValueError:
        return []
    out = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        out.append([c.strip() for c in line.strip().strip("|").split("|")])
    return out


def newest_full_check(runs_md: str, walks_md: str) -> Entry | None:
    """The newest RUNS.md full-check row whose commit is also in WALKTHROUGHS.md."""
    walked = {r[1].strip("`") for r in rows(walks_md, WALKS_HEADER) if len(r) > 1}
    for cells in rows(runs_md, RUNS_HEADER):
        if len(cells) < 4 or "full check" not in cells[3]:
            continue
        commit, version, date = cells[1].strip("`"), parse(cells[2]), _DATE.match(cells[0])
        if commit in walked and version and date:
            return Entry(dt.date.fromisoformat(date.group(1)), commit, version)
    return None


def changelog(text: str) -> dict[Version, dt.date | None]:
    """Each released version in CHANGELOG.md and its section date, if any."""
    found: dict[Version, dt.date | None] = {}
    for match in re.finditer(r"^## \[?(\d+\.\d+\.\d+)\]?(.*)$", text, re.M):
        version, date = parse(match.group(1)), _DATE.search(match.group(2))
        if version:
            found[version] = dt.date.fromisoformat(date.group(1)) if date else None
    return found


# --- material paths -------------------------------------------------------


def glob_regex(pattern: str) -> re.Pattern[str]:
    """``**/`` any directories (or none), ``**`` anything, ``*`` and ``?`` within one name."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


def material(files: list[str], settings: Settings) -> list[str]:
    keep = [glob_regex(p) for p in settings.material_paths]
    drop = [glob_regex(p) for p in settings.not_material]
    return [f for f in files if any(p.match(f) for p in keep) and not any(p.match(f) for p in drop)]


def load_settings(profile_yaml: str) -> Settings:
    """``full_check:`` from regression/profile.yaml: integers and lists of strings
    only, read without PyYAML (the test compares it with yaml.safe_load)."""
    block: dict[str, object] = {}
    inside, key = False, None
    for raw in profile_yaml.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        if not line.startswith(" "):
            inside = line == "full_check:"
            continue
        if not inside:
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and key:
            block.setdefault(key, [])
            block[key].append(stripped[2:].strip().strip("'\""))  # type: ignore[union-attr]
            continue
        name, _, value = stripped.partition(":")
        key, value = name.strip(), value.strip()
        if value:
            block[key] = int(value) if value.isdigit() else value.strip("'\"")
    return Settings(
        max_age_days=int(block.get("max_age_days", 7)),  # type: ignore[arg-type]
        every_releases=int(block.get("every_releases", 5)),  # type: ignore[arg-type]
        material_paths=tuple(block.get("material_paths", ()) or ()),  # type: ignore[arg-type]
        not_material=tuple(block.get("not_material", ()) or ()),  # type: ignore[arg-type]
    )


# --- the rule ---------------------------------------------------------------


def problems(
    version: str,
    runs_md: str,
    walks_md: str,
    changelog_md: str,
    settings: Settings | None = None,
    changed: list[str] | None = None,
    today: dt.date | None = None,
) -> list[str]:
    """Why the release of ``version`` needs a full check first (empty: none due).

    ``changed`` is the list of files changed since the newest full check's commit
    (``git diff --name-only <commit>..HEAD``), or None when it cannot be told.
    """
    settings = settings or Settings()
    current = parse(version)
    if current is None:
        return [f"VERSION {version!r} is not MAJOR.MINOR.PATCH"]
    released = changelog(changelog_md)
    entry = newest_full_check(runs_md, walks_md)
    major = current[0] >= 1 and current[1:] == (0, 0)
    if entry is None:
        if not released or set(released) <= {current}:
            return []  # the first release: nothing to check against yet
        return [
            "A full check is due: none is logged in both regression/RUNS.md and "
            f"regression/WALKTHROUGHS.md — {HOW}"
        ]
    last = show(entry.version)
    out = []
    # 4. A major release: a full check on exactly this version.
    if major and entry.version != current:
        out.append(
            f"A full check is due (major release): {version} needs a full check on "
            f"{version} itself; the last one was on {last} — {HOW}"
        )
    # 1. Weekly.
    release_date = released.get(current) or today or dt.date.today()
    age = (release_date - entry.date).days
    if age > settings.max_age_days:
        out.append(
            f"A full check is due (weekly): the last one ({last}, {entry.date}) is {age} "
            f"days older than this release ({release_date}) — {HOW}"
        )
    # 2. Every N releases, patches included; the one being released counts.
    since = sorted(v for v in set(released) | {current} if entry.version < v <= current)
    if len(since) >= settings.every_releases:
        out.append(
            f"A full check is due (every {settings.every_releases} releases): "
            f"{len(since)} releases since the last one ({last}) — {HOW}"
        )
    # 3. A material security or accounting change since the full check's commit.
    if changed is not None:
        hits = material(changed, settings)
        if hits:
            shown = ", ".join(hits[:5]) + (f" and {len(hits) - 5} more" if len(hits) > 5 else "")
            out.append(
                f"A full check is due (security or accounting change): {shown} changed "
                f"since the last one ({last}, commit {entry.commit}) — {HOW}"
            )
    return out


# --- the checkout -------------------------------------------------------------


def git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed git arguments
            ["git", *args], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError:
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def is_release(root: Path, version: str) -> bool:
    """VERSION differs from the last tag: it has no v<VERSION> tag. Without any
    tags (a fresh or shallow clone) every checkout counts as a release."""
    code, tags = git(root, "tag", "--list", "v*")
    if code != 0 or not tags:
        return True
    return f"v{version}" not in tags.split()


def changed_since(root: Path, commit: str) -> list[str] | None:
    code, out = git(root, "diff", "--name-only", f"{commit}..HEAD")
    return out.splitlines() if code == 0 else None


def check(root: Path = ROOT, today: dt.date | None = None) -> list[str]:
    return report(root, today)[0]


def report(root: Path = ROOT, today: dt.date | None = None) -> tuple[list[str], list[str]]:
    """(problems, notes) for the checkout under ``root``."""

    def read(rel: str) -> str:
        path = root / rel
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    version = read("VERSION").strip()
    if parse(version) and not is_release(root, version):
        return [], [f"{version} is already tagged: not a release, nothing to check"]
    runs_md, walks_md = read("regression/RUNS.md"), read("regression/WALKTHROUGHS.md")
    entry = newest_full_check(runs_md, walks_md)
    notes, changed = [], None
    if entry is not None:
        changed = changed_since(root, entry.commit)
        if changed is None:
            _, shallow = git(root, "rev-parse", "--is-shallow-repository")
            if shallow == "true":
                notes.append(
                    f"commit {entry.commit} is not in this shallow clone: the "
                    "security or accounting change rule was not checked"
                )
            else:
                return [
                    f"A full check is due: commit {entry.commit} of the last one "
                    f"({show(entry.version)}) is not in this repository's history — {HOW}"
                ], notes
    found = problems(
        version,
        runs_md,
        walks_md,
        read("CHANGELOG.md"),
        load_settings(read("regression/profile.yaml")),
        changed,
        today,
    )
    return found, notes


def main() -> int:
    found, notes = report()
    for line in notes + found:
        print(line)
    if not found:
        print("no full check due for this release")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
