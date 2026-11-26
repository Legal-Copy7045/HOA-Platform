#!/usr/bin/env python3
"""Turn the walkthrough's output into out/walkthrough.html (a self-contained page).

    REPORT_TZ=America/New_York python3 regression/walkthrough_report.py [--no-thumbs]

Reads out/walkthrough.json (the inventory: role -> desktop|phone -> menu and
pages), out/walkthrough-checks.json (the stage's WALK- results) and
out/walkthrough-findings.json (the reviewers' merged findings; optional):

    {summary, roles: [{role, desktop_items, phone_items, notes}],
     findings: [{id: WT-001.., group: merge|rename|move|remove|flow|mobile|keep_separate,
                 title, now, proposal, who, effort: S|M|L, risk, evidence: [screenshot paths]}],
     proposed_menus: {role: [sections...]}}

Screenshots are linked by relative path (they sit next to the page in
out/walkthrough/). For publishing, only the failing pages are embedded, as
small JPEG thumbnails, within a size budget that keeps the page under 8 MB.
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import os
import subprocess  # nosec B404 - fixed command lists only
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report import CSS

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
GROUPS = {
    "merge": "Merge",
    "rename": "Rename",
    "move": "Move",
    "remove": "Remove",
    "flow": "Shorter flows",
    "mobile": "Phone",
    "keep_separate": "Keep separate (legal or role reasons)",
}
# Automated check kinds, by the wording of the check key (walkthrough.js).
KINDS = [
    ("matches its page heading", "Menu label differs from the page heading"),
    ("no duplicate labels", "Duplicate labels in a menu"),
    ("opens a page", "Reachable page that does not open (error or redirect only)"),
    ("sideways scroll", "Sideways scroll"),
    ("runs off screen", "Something runs off the screen or out of its box"),
    ("tap targets", "Tap targets smaller than 40x40 px on a phone"),
    ("menu entries are at least", "Menu entries smaller than 40x40 px on a phone"),
    ("menu opens", "Phone menu does not open"),
    ("console or CSP", "Console or Content-Security-Policy errors"),
]
THUMB_BUDGET = 6_000_000  # bytes of base64 thumbnails; the rest of the page stays small


def load(out: Path, name: str, default):
    path = out / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def kind_of(check: str) -> str:
    return next((label for needle, label in KINDS if needle in check), "Other")


def rel(path: str) -> str:
    """A screenshot path (repo-relative, as recorded) relative to the report in out/."""
    path = (path or "").replace("\\", "/")
    for prefix in ("regression/out/", "out/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


class Thumbs:
    """Small JPEG data URIs of screenshots (top of the page), within a byte budget."""

    def __init__(self, out: Path, enabled: bool, budget: int = THUMB_BUDGET):
        self.out, self.budget, self.used, self.skipped = out, budget, 0, 0
        self.cache: dict[str, str] = {}
        try:
            from PIL import Image

            self.Image = Image if enabled else None
        except ImportError:  # pragma: no cover - Pillow is in the project's requirements
            self.Image = None

    def get(self, path: str) -> str:
        if not self.Image or not path:
            return ""
        if path in self.cache:
            return self.cache[path]
        file = self.out / rel(path)
        if not file.is_file():
            return ""
        with self.Image.open(file) as img:
            img = img.convert("RGB")
            width = 240 if img.width > 600 else 130
            crop = img.crop((0, 0, img.width, min(img.height, int(img.width * 1.4))))
            crop.thumbnail((width, width * 2))
            buf = io.BytesIO()
            crop.save(buf, "JPEG", quality=55, optimize=True)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        if self.used + len(uri) > self.budget:
            self.skipped += 1
            return ""
        self.used += len(uri)
        self.cache[path] = uri
        return uri


def render(out: Path, *, tz: ZoneInfo, thumbs: bool = True, commit: str = "") -> str:
    inventory = load(out, "walkthrough.json", {"roles": {}})
    stage = load(out, "walkthrough-checks.json", {"results": []})
    findings = load(
        out,
        "walkthrough-findings.json",
        {"summary": "", "roles": [], "findings": [], "proposed_menus": {}},
    )
    results = stage.get("results", [])
    totals = Counter(r["status"] for r in results)
    failing = [r for r in results if r["status"] == "fail"]
    roles = inventory.get("roles", {})
    notes = {r.get("role"): r for r in findings.get("roles", [])}
    images = Thumbs(out, thumbs)
    e = html.escape
    when = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")

    def badge(state: str) -> str:
        return f'<span class="b {e(state.replace(" ", "-"))}">{e(state)}</span>'

    def shot_link(path: str, label: str = "screenshot") -> str:
        return f'<a href="{e(rel(path))}">{e(label)}</a>' if path else ""

    # --- Roles: menu size desktop vs phone ---------------------------------------------
    fails_by_role = Counter(r["check"].split(" ", 1)[0].rstrip(":") for r in failing)
    role_rows = []
    for role, widths in roles.items():
        cells = []
        for width in ("desktop", "phone"):
            data = widths.get(width) or {}
            menu = data.get("menu") or {}
            items = menu.get("item_count", sum(len(s["items"]) for s in menu.get("sections", [])))
            cells.append(
                f"<td class=n>{items}</td><td class=n>{menu.get('tile_count', 0)}</td>"
                f"<td class=n>{len(data.get('pages', []))}</td>"
            )
        note = notes.get(role, {})
        role_rows.append(
            f"<tr><td><strong>{e(role)}</strong>"
            f"{f'<div class=m>{e(str(note.get("notes", "")))}</div>' if note.get('notes') else ''}</td>"
            f"{''.join(cells)}<td class=n>{fails_by_role[role]}</td></tr>"
        )

    # --- Automated check failures, by kind ---------------------------------------------
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for r in failing:
        by_kind[kind_of(r["check"])].append(r)
    kind_blocks = []
    for _, label in [*KINDS, ("", "Other")]:
        rows = by_kind.get(label)
        if not rows:
            continue
        trs = []
        for r in rows:
            ev = r.get("evidence", "")
            is_shot = ev.endswith(".png")
            uri = images.get(ev) if is_shot else ""
            img = (
                f'<a href="{e(rel(ev))}"><img class=thumb src="{uri}" alt="Screenshot: '
                f'{e(r["check"][:80])}" loading=lazy></a>'
                if uri
                else ""
            )
            trs.append(
                f"<tr><td><code>{e(r['id'])}</code></td><td>{e(r['check'])}"
                f"<div class=m>{e(r.get('detail', '')[:400])}</div>"
                f"{f'<div class=m>{shot_link(ev)}</div>' if is_shot else ''}</td>"
                f"<td class=tcell>{img}</td></tr>"
            )
        kind_blocks.append(
            f"<details{' open' if len(rows) <= 12 else ''}><summary><strong>{e(label)}</strong> "
            f"<span class=m>({len(rows)})</span></summary><div class=wrap><table>{''.join(trs)}"
            f"</table></div></details>"
        )

    # --- Reviewer findings, grouped ----------------------------------------------------
    grouped: dict[str, list[dict]] = defaultdict(list)
    for f in findings.get("findings", []):
        grouped[f.get("group", "")].append(f)
    finding_blocks = []
    for key in [*GROUPS, *sorted(k for k in grouped if k not in GROUPS)]:
        items = grouped.get(key)
        if not items:
            continue
        cards = []
        for f in sorted(items, key=lambda f: str(f.get("id", ""))):
            evidence = " · ".join(
                shot_link(p, Path(rel(p)).as_posix()) for p in f.get("evidence") or []
            )
            cards.append(
                f'<div class="card finding"><div>{badge("effort " + str(f.get("effort", "?")))} '
                f"<code>{e(str(f.get('id', '')))}</code> <strong>{e(str(f.get('title', '')))}</strong></div>"
                f"<dl><dt>Now</dt><dd>{e(str(f.get('now', '')))}</dd>"
                f"<dt>Proposal</dt><dd>{e(str(f.get('proposal', '')))}</dd>"
                f"<dt>Who</dt><dd>{e(str(f.get('who', '')))}</dd>"
                f"<dt>Risk</dt><dd>{e(str(f.get('risk', '')))}</dd></dl>"
                f"{f'<div class=m>Evidence: {evidence}</div>' if evidence else ''}</div>"
            )
        finding_blocks.append(
            f"<h3>{e(GROUPS.get(key, key or 'Ungrouped'))} <span class=m>({len(items)})</span></h3>"
            f"<div class=stack>{''.join(cards)}</div>"
        )

    # --- Menus: now and proposed -------------------------------------------------------
    def menu_html(sections) -> str:
        parts = []
        for sec in sections:
            if isinstance(sec, str):
                parts.append(f"<li>{e(sec)}</li>")
                continue
            items = sec.get("items", [])
            labels = ", ".join(
                e(i if isinstance(i, str) else f"{i.get('label', '')}") for i in items
            )
            parts.append(
                f"<li><strong>{e(str(sec.get('title') or '(top)'))}</strong>"
                f"{': ' + labels if labels else ''}</li>"
            )
        return f"<ul class=menu>{''.join(parts)}</ul>"

    current = []
    for role, widths in roles.items():
        desk = (widths.get("desktop") or {}).get("menu") or {}
        phone = (widths.get("phone") or {}).get("menu") or {}
        same = [
            (s["title"], [i["label"] for i in s["items"]]) for s in desk.get("sections", [])
        ] == [(s["title"], [i["label"] for i in s["items"]]) for s in phone.get("sections", [])]
        current.append(
            f"<div class=card><h3>{e(role)}</h3>{menu_html(desk.get('sections', []))}"
            f"<div class=m>{'Same on a phone.' if same else 'The phone menu differs.'}</div></div>"
        )
    proposed = [
        f"<div class=card><h3>{e(role)}</h3>{menu_html(sections)}</div>"
        for role, sections in (findings.get("proposed_menus") or {}).items()
    ]

    counts = Counter(f.get("group", "") for f in findings.get("findings", []))
    group_line = " · ".join(f"{GROUPS.get(k, k)} {counts[k]}" for k in GROUPS if counts[k])
    pages_total = sum(len((w.get("desktop") or {}).get("pages", [])) for w in roles.values())
    thumb_note = (
        f"<p class=m>{images.skipped} more screenshots were not embedded (size budget); "
        "their links work next to the report.</p>"
        if images.skipped
        else ""
    )
    extra_css = (
        ".thumb{width:120px;border:1px solid var(--line);border-radius:6px;display:block}"
        ".tcell{width:130px}details{margin:8px 0}summary{cursor:pointer;padding:6px 0}"
        ".stack{display:grid;gap:10px}.finding dl{display:grid;grid-template-columns:90px 1fr;"
        "gap:4px 10px;margin:8px 0}.finding dt{color:var(--muted);font-size:.85rem}.finding dd{margin:0}"
        ".menu{margin:6px 0;padding-left:18px}.cols{display:grid;gap:10px;"
        "grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.cols>*{min-width:0}"
        "@media (max-width:600px){.finding dl{grid-template-columns:1fr}.tcell{width:auto}"
        ".thumb{width:90px}}a{color:inherit}"
    )
    return f"""<title>Portal walkthrough</title>
<meta name="claude:viewport-fit-sides" content="cover">
<style>
{CSS}{extra_css}
</style><main>
<h1>Portal walkthrough</h1><div class=m>{e(when)}{" · commit " + e(commit) if commit else ""}
 · desktop {e(str((inventory.get("widths") or {}).get("desktop", {}).get("width", 1280)))} px and phone
 {e(str((inventory.get("widths") or {}).get("phone", {}).get("width", 390)))} px</div>
{f"<p>{e(findings['summary'])}</p>" if findings.get("summary") else ""}
<div class=grid style="margin-top:14px">
<div class=card><div class=m>Roles walked</div><div class=big>{len(roles)}</div></div>
<div class=card><div class=m>Page types (all roles)</div><div class=big>{pages_total}</div></div>
<div class=card><div class=m>Checks failing</div><div class=big>{totals["fail"]}</div>
<div class=m>{totals["pass"]} passing</div></div>
<div class=card><div class=m>Reviewer findings</div><div class=big>{len(findings.get("findings", []))}</div>
<div class=m>{e(group_line)}</div></div></div>
<h2>Menus by role</h2><div class=wrap><table><tr><th rowspan=2>Role</th><th colspan=3>Desktop</th>
<th colspan=3>Phone</th><th rowspan=2 class=n>Failing checks</th></tr><tr><th class=n>Menu</th>
<th class=n>Hub tiles</th><th class=n>Pages</th><th class=n>Menu</th><th class=n>Hub tiles</th>
<th class=n>Pages</th></tr>{"".join(role_rows) or "<tr><td colspan=8 class=m>No walkthrough output.</td></tr>"}</table></div>
<h2>Reviewer findings</h2>{"".join(finding_blocks) or "<p class=m>No reviewer findings recorded for this run.</p>"}
<h2>Automated checks that failed</h2>{"".join(kind_blocks) or "<p class=m>None.</p>"}{thumb_note}
{f"<h2>Proposed menus</h2><div class=cols>{''.join(proposed)}</div>" if proposed else ""}
<h2>Menus now</h2><div class=cols>{"".join(current)}</div>
</main>"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-thumbs", action="store_true", help="embed no screenshots")
    args = parser.parse_args(argv)
    tz = ZoneInfo(os.environ.get("REPORT_TZ", "UTC"))
    commit = subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    page = render(OUT, tz=tz, thumbs=not args.no_thumbs, commit=commit)
    (OUT / "walkthrough.html").write_text(page, encoding="utf-8")
    print(f"wrote {OUT / 'walkthrough.html'} ({len(page.encode()) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
