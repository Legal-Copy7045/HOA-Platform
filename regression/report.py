#!/usr/bin/env python3
"""Turn regression/out/*.json into out/report.html (a self-contained page) and out/report.md.

    REPORT_TZ=America/New_York python3 regression/report.py

Reads the stage results, the feature catalogue, out/findings-bugs.json,
out/findings-security.json and out/notes.json (all optional).
"""

from __future__ import annotations

import html
import json
import os
import subprocess  # nosec B404 - fixed command lists only
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalogue import claims, load_catalogue

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
STAGE_ORDER = ["ci", "unit", "page", "live", "manual"]
STAGE_NAMES = {
    "ci": "Code quality and supply chain",
    "unit": "Automated tests",
    "page": "Every page in a browser",
    "live": "Live POC (read-only)",
    "manual": "Needs a person",
}
SEVERITY = ["critical", "high", "medium", "low", "info"]


def load(name: str, default):
    path = OUT / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def main() -> None:
    tz = ZoneInfo(os.environ.get("REPORT_TZ", "UTC"))
    stages = {s: load(f"{s}.json", None) for s in STAGE_ORDER}
    stages = {k: v for k, v in stages.items() if v}
    results = [(s, r) for s, d in stages.items() for r in d["results"]]
    catalogue = load_catalogue()
    bugs = load("findings-bugs.json", [])
    security = load("findings-security.json", {"findings": [], "claims": []})
    notes = load("notes.json", {"summary": "", "observations": []})
    commit = subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()

    totals = Counter(r["status"] for _, r in results)
    features = []
    for item in catalogue:
        mine = [r for s, r in results if claims(item, s, r["id"], r["check"])]
        statuses = Counter(r["status"] for r in mine)
        if not (item.get("match") or item.get("checks")):
            state = "uncovered"
        elif statuses["fail"]:
            state = "fail"
        elif statuses["pass"]:
            state = "pass"
        elif statuses["manual"]:
            state = "manual"
        else:
            state = "not run"
        features.append(
            {
                **item,
                "state": state,
                "counts": statuses,
                "failing": [r["id"] for r in mine if r["status"] == "fail"],
            }
        )
    unowned = [
        (s, r)
        for s, r in results
        if r["status"] != "skip" and not any(claims(i, s, r["id"], r["check"]) for i in catalogue)
    ]
    failing = [(s, r) for s, r in results if r["status"] == "fail"]
    manual = [r for s, r in results if s == "manual"]
    findings = sorted(
        [{**f, "kind": "bug"} for f in bugs]
        + [{**f, "kind": "security"} for f in security.get("findings", [])],
        key=lambda f: (
            SEVERITY.index(f.get("severity", "info")) if f.get("severity") in SEVERITY else 9
        ),
    )
    when = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")

    # --- Markdown ---------------------------------------------------------------------
    md = [f"# Regression report: {when} (commit {commit})", ""]
    if notes.get("summary"):
        md += [notes["summary"], ""]
    md.append(
        "Totals: " + ", ".join(f"{totals[k]} {k}" for k in ("pass", "fail", "manual", "skip"))
    )
    md.append("")
    for s, d in stages.items():
        c = Counter(r["status"] for r in d["results"])
        md.append(
            f"- **{STAGE_NAMES[s]}** ({s}): " + ", ".join(f"{v} {k}" for k, v in sorted(c.items()))
        )
    if failing:
        md += ["", "## Failing checks", ""]
        md += [f"- `{r['id']}` {r['check']}: {r['detail'][:200]}" for _, r in failing]
    if findings:
        md += ["", "## Review findings", ""]
        md += [f"- `{f.get('id')}` ({f.get('severity')}) {f.get('title')}" for f in findings]
    md += ["", "## Features", ""]
    md += [f"- {f['state'].upper()}: {f['feature']}" for f in features]
    if notes.get("observations"):
        md += ["", "## Observations", ""] + [f"- {o}" for o in notes["observations"]]
    (OUT / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # --- HTML -------------------------------------------------------------------------
    e = html.escape

    def badge(state: str) -> str:
        return f'<span class="b {e(state.replace(" ", "-"))}">{e(state)}</span>'

    rows = []
    for f in features:
        c = f["counts"]
        rows.append(
            f"<tr><td>{badge(f['state'])}</td><td>{e(f['feature'])}<div class=m>{e(f['area'])}"
            f"{' · since ' + e(str(f.get('since'))) if f.get('since') else ''}"
            f"{' · failing ' + e(', '.join(f['failing'])) if f['failing'] else ''}</div></td>"
            f"<td class=n>{c['pass']}</td><td class=n>{c['fail']}</td><td class=n>{c['manual'] + c['skip']}</td></tr>"
        )
    stage_cards = []
    for s, d in stages.items():
        c = Counter(r["status"] for r in d["results"])
        state = (
            "fail"
            if c["fail"]
            else ("pass" if c["pass"] else ("manual" if c["manual"] else "not run"))
        )
        stage_cards.append(
            f'<div class="card">{badge(state)}<h3>{e(STAGE_NAMES[s])}</h3>'
            f"<div class=m>{c['pass']} pass · {c['fail']} fail · {c['manual']} manual · {c['skip']} skip</div></div>"
        )
    fail_rows = (
        "".join(
            f"<tr><td><code>{e(r['id'])}</code></td><td>{e(r['check'])}<div class=m>{e(r['detail'][:400])}</div></td></tr>"
            for _, r in failing
        )
        or "<tr><td colspan=2 class=m>None.</td></tr>"
    )
    finding_rows = (
        "".join(
            f"<tr><td>{badge(f.get('severity', 'info'))}</td><td><code>{e(str(f.get('id')))}</code> {e(str(f.get('title')))}"
            f"<div class=m>{e(str(f.get('file', '')))}{':' + e(str(f.get('line'))) if f.get('line') else ''} · "
            f"{e(str(f.get('description', ''))[:400])}</div></td></tr>"
            for f in findings
        )
        or "<tr><td colspan=2 class=m>No review findings recorded for this run.</td></tr>"
    )
    claim_rows = "".join(
        f"<tr><td>{badge('pass' if c.get('held') else 'fail')}</td><td>{e(str(c.get('claim')))}"
        f"<div class=m>{e(str(c.get('where', '')))} · {e(str(c.get('note', '')))}</div></td></tr>"
        for c in security.get("claims", [])
    )
    manual_rows = "".join(
        f"<tr><td><code>{e(r['id'])}</code></td><td>{e(r['check'])}<div class=m>{e(r['detail'])}</div></td></tr>"
        for r in manual
    )
    unowned_rows = "".join(
        f"<li><code>{e(r['id'])}</code> {e(s)}: {e(r['check'])}</li>" for s, r in unowned[:200]
    )
    obs = "".join(f"<li>{e(o)}</li>" for o in notes.get("observations", []))
    # Page content only (no html/head/body): the artifact host wraps it, and a
    # browser opening the file locally renders it the same way.
    page = f"""<title>Portal regression report</title>
<meta name="claude:viewport-fit-sides" content="cover">
<style>
:root{{--bg:#f6f5f1;--card:#fff;--ink:#1d2320;--muted:#66706b;--line:#e3e1d9;--pass:#1f6b46;--pass-bg:#e3f1e8;
--fail:#a3302a;--fail-bg:#fbe7e5;--warn:#8a5a00;--warn-bg:#fdf1d8;--neutral-bg:#eceae4}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#151917;--card:#1e2421;--ink:#e8ece9;
--muted:#9aa49f;--line:#2f3632;--pass:#7fd1a4;--pass-bg:#1d3327;--fail:#f19a93;--fail-bg:#3a1f1d;--warn:#f0c36a;
--warn-bg:#3a2f17;--neutral-bg:#2a302c;color-scheme:dark}}}}
:root[data-theme=dark]{{--bg:#151917;--card:#1e2421;--ink:#e8ece9;--muted:#9aa49f;--line:#2f3632;--pass:#7fd1a4;
--pass-bg:#1d3327;--fail:#f19a93;--fail-bg:#3a1f1d;--warn:#f0c36a;--warn-bg:#3a2f17;--neutral-bg:#2a302c;color-scheme:dark}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding-block:24px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));
padding-right:max(16px,env(safe-area-inset-right,0px))}}.grid>*{{min-width:0}}h1{{font-size:1.6rem;margin:0 0 4px}}
h2{{font-size:1.15rem;margin:28px 0 10px}}h3{{font-size:1rem;margin:6px 0 2px}}.m{{color:var(--muted);font-size:.85rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.big{{font-size:1.9rem;font-weight:700}}table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}}td,th{{padding:8px 10px;border-top:1px solid var(--line);
vertical-align:top;text-align:left}}th{{font-size:.78rem;text-transform:uppercase;color:var(--muted);border-top:0}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}.b{{display:inline-block;padding:1px 8px;border-radius:99px;
font-size:.75rem;font-weight:600;background:var(--neutral-bg);white-space:nowrap}}.b.pass{{background:var(--pass-bg);color:var(--pass)}}
.b.fail,.b.critical,.b.high{{background:var(--fail-bg);color:var(--fail)}}.b.manual,.b.medium,.b.not-run,.b.uncovered{{background:var(--warn-bg);color:var(--warn)}}
code{{font-size:.85em}}.wrap{{overflow-x:auto}}
</style><main>
<h1>Portal regression report</h1><div class=m>{e(when)} · commit {e(commit)}</div>
{f"<p>{e(notes['summary'])}</p>" if notes.get("summary") else ""}
<div class=grid style="margin-top:14px">
<div class=card><div class=m>Passing</div><div class=big>{totals["pass"]}</div></div>
<div class=card><div class=m>Failing</div><div class=big>{totals["fail"]}</div></div>
<div class=card><div class=m>Need a person</div><div class=big>{totals["manual"]}</div></div>
<div class=card><div class=m>Review findings</div><div class=big>{len(findings)}</div></div></div>
<h2>Stages</h2><div class=grid>{"".join(stage_cards)}</div>
<h2>Failing checks</h2><div class=wrap><table>{fail_rows}</table></div>
<h2>Review findings</h2><div class=wrap><table>{finding_rows}</table></div>
{f"<h2>Security claims checked against the code</h2><div class=wrap><table>{claim_rows}</table></div>" if claim_rows else ""}
<h2>Features</h2><div class=wrap><table><tr><th>State</th><th>Feature</th><th class=n>Pass</th><th class=n>Fail</th><th class=n>Other</th></tr>{"".join(rows)}</table></div>
<h2>Needs a person</h2><div class=wrap><table>{manual_rows or "<tr><td class=m>None.</td></tr>"}</table></div>
{f"<h2>Observations</h2><ul>{obs}</ul>" if obs else ""}
{f"<h2>Checks not in any feature</h2><ul class=m>{unowned_rows}</ul>" if unowned_rows else ""}
</main>"""
    (OUT / "report.html").write_text(page, encoding="utf-8")
    print(f"wrote {OUT / 'report.html'} and report.md: {dict(totals)}; {len(findings)} findings")


if __name__ == "__main__":
    main()
