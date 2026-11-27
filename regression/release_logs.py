#!/usr/bin/env python3
"""The regression and walkthrough logs keep up with releases (D91).

A MINOR or MAJOR release (VERSION X.Y.0) needs the newest row of the standard
table in regression/RUNS.md and in regression/WALKTHROUGHS.md to be for the
previous release line (the newest MAJOR.MINOR in CHANGELOG.md below X.Y) or
newer, so the logs are never more than one release line behind: run /full-check
(and `python3 regression/run.py log --full-check`) on a release before the next
minor one. Patch releases (X.Y.Z, Z > 0) are exempt; so is the first release
line (nothing older in CHANGELOG.md).

    python3 regression/release_logs.py     # prints the problems; exit 1 if any

Run by `python3 regression/run.py check` (ci stage) and by
regression/tests/test_release_logs.py. Standard library only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# The standard tables' headers (regression/standard.py, vendored: not imported
# here so this file needs no PyYAML). The version is the third column in both.
RUNS_HEADER = (
    "| Date (UTC) | Commit | Version | Audit | Pass | Fail | Skip | Manual due "
    "| Failing IDs | Findings | Report |"
)
WALKS_HEADER = (
    "| Date (UTC) | Commit | Version | Roles | Pages | UX checks failing | Findings | Report |"
)
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def parse(version: str) -> tuple[int, int, int] | None:
    match = _SEMVER.match((version or "").strip().strip("`"))
    return tuple(int(n) for n in match.groups()) if match else None  # type: ignore[return-value]


def newest_logged(text: str, header: str) -> tuple[int, int, int] | None:
    """The version on the first (newest) row of the standard table, or None."""
    lines = text.splitlines()
    try:
        start = lines.index(header) + 2
    except ValueError:
        return None
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > 2:
            return parse(cells[2])
    return None


def changelog_versions(text: str) -> list[tuple[int, int, int]]:
    found = []
    for match in re.finditer(r"^## \[?(\d+\.\d+\.\d+)", text, re.M):
        found.append(parse(match.group(1)))
    return [v for v in found if v]


def previous_line(
    version: tuple[int, int, int], released: list[tuple[int, int, int]]
) -> tuple[int, int] | None:
    """The newest MAJOR.MINOR below ``version``'s line among the released versions."""
    older = [v[:2] for v in released if v[:2] < version[:2]]
    return max(older) if older else None


def problems(version: str, runs_md: str, walks_md: str, changelog: str) -> list[str]:
    current = parse(version)
    if current is None:
        return [f"VERSION {version!r} is not MAJOR.MINOR.PATCH"]
    if current[2] != 0:
        return []  # a patch release: exempt
    needed = previous_line(current, changelog_versions(changelog))
    if needed is None:
        return []  # the first release line
    want = f"{needed[0]}.{needed[1]}"
    out = []
    for name, text, header, how in (
        ("regression/RUNS.md", runs_md, RUNS_HEADER, "/regression or /full-check"),
        ("regression/WALKTHROUGHS.md", walks_md, WALKS_HEADER, "/walkthrough or /full-check"),
    ):
        logged = newest_logged(text, header)
        if logged is None or logged[:2] < needed:
            shown = ".".join(map(str, logged)) if logged else "none"
            out.append(
                f"{name}: release {version} needs an entry for {want}.x or newer "
                f"(newest: {shown}); run {how} and `python3 regression/run.py log`"
            )
    return out


def check(root: Path = ROOT) -> list[str]:
    def read(rel: str) -> str:
        path = root / rel
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    return problems(
        read("VERSION").strip(),
        read("regression/RUNS.md"),
        read("regression/WALKTHROUGHS.md"),
        read("CHANGELOG.md"),
    )


def main() -> int:
    found = check()
    for line in found:
        print(line)
    if not found:
        print("regression logs current for this release")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
