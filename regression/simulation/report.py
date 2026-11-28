#!/usr/bin/env python3
"""regression/out/simulation.json -> regression/out/simulation.html (D102).

    python3 regression/run.py report --simulation

One self-contained page: the run's options and timing, the timeline (events
by kind per month, scheduled tasks per month), every invariant with the days
it held and its breaks (simulated date, event, problem), event errors and
refusals, failed background jobs, the probes, the snapshots and the year-end
figures. IDs, counts and amounts only: the simulation's people are fictional,
and none of their names are written here.
"""

from __future__ import annotations

import html
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "out"


def _e(x) -> str:
    return html.escape(str(x))


def _cents(c) -> str:
    if c is None:
        return ""
    return f"{'-' if c < 0 else ''}${abs(c) / 100:,.2f}"


def _table(head, rows) -> str:
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


CSS = """
:root{--bg:#fbfbf9;--fg:#1d1f21;--muted:#5d6166;--line:#dcdcd6;--ok:#1d7a46;--bad:#b3261e;--warn:#8a5a00;--card:#fff}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#16181a;--fg:#e8e8e4;--muted:#a3a7ab;--line:#33363a;--ok:#5cc58a;--bad:#ff8a80;--warn:#e0b05a;--card:#1e2124}}
:root[data-theme="dark"]{--bg:#16181a;--fg:#e8e8e4;--muted:#a3a7ab;--line:#33363a;--ok:#5cc58a;--bad:#ff8a80;--warn:#e0b05a;--card:#1e2124}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:16px;max-width:1100px;margin:auto}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid var(--line)}
.muted{color:var(--muted)}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
.tiles{display:flex;flex-wrap:wrap;gap:8px}.tile{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px;min-width:120px}
.tile b{display:block;font-size:1.3rem}pre{white-space:pre-wrap;font-size:.8rem;max-height:14em;overflow:auto;background:var(--card);padding:6px;border:1px solid var(--line)}
details summary{cursor:pointer}
"""


def render(data: dict) -> str:
    o = data["options"]
    t = data["timings"]
    fails, errs, refusals = data["failures"], data["errors"], data["refusals"]
    jobs, probes = data["job_failures"], data["probes"]
    status = "bad" if fails or errs else "ok"
    verdict = (
        "Every invariant held and no event crashed."
        if status == "ok"
        else f"{len(fails)} invariant break(s) and {len(errs)} event error(s)."
    )
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width, initial-scale=1">',
        f"<title>Simulation report</title><style>{CSS}</style></head><body>",
        "<h1>Accelerated simulation</h1>",
        f'<p class="{status}"><strong>{_e(verdict)}</strong>'
        + (f" Stopped at {_e(data['stopped'])}." if data["stopped"] else "")
        + "</p>",
        f'<p class="muted">{_e(o["months"])} month(s) from {_e(o["start"])} to {_e(o["end"])}, '
        f"seed {_e(o['seed'])}, dataset {_e(o['dataset'])}{', fast' if o['fast'] else ''}; "
        f"simulated through {_e(data['simulated_through'])}; finished {_e(data['finished'])}.</p>",
        '<div class="tiles">',
    ]
    events = sum(sum(m["events"].values()) for m in data["timeline"])
    for label, value in (
        ("Events", events),
        ("Invariant breaks", len(fails)),
        ("Event errors", len(errs)),
        ("Refusals", len(refusals)),
        ("Failed jobs", len(jobs)),
        ("Probes refused", sum(1 for p in probes if p["status"] == "pass")),
        ("Minutes", round(t.get("total_seconds", 0) / 60, 1)),
        ("Seconds a day", t.get("mean_day_seconds", "")),
    ):
        parts.append(
            f'<div class="tile"><span class="muted">{_e(label)}</span><b>{_e(value)}</b></div>'
        )
    parts.append("</div>")

    parts.append("<h2>Invariants</h2>")
    broken = Counter(f["invariant"] for f in fails)
    keys = sorted(set(data["invariants"]) | set(broken))
    parts.append(
        _table(
            ["Invariant", "Days held", "Breaks"],
            [
                [
                    _e(k),
                    _e(data["invariants"].get(k, 0)),
                    f'<span class="{"bad" if broken.get(k) else "ok"}">{broken.get(k, 0)}</span>',
                ]
                for k in keys
            ],
        )
    )
    if fails:
        parts.append("<h3>Breaks</h3>")
        parts.append(
            _table(
                ["Simulated date", "Invariant", "After event", "Problem"],
                [
                    [_e(f["date"]), _e(f["invariant"]), _e(f["event"]), _e(f["problem"])]
                    for f in fails[:300]
                ],
            )
        )

    parts.append("<h2>Timeline</h2>")
    for m in data["timeline"]:
        ev = sorted(m["events"].items(), key=lambda kv: (-kv[1], kv[0]))
        n = sum(m["events"].values())
        parts.append(f"<details><summary><strong>{_e(m['month'])}</strong>: {n} events</summary>")
        parts.append(_table(["Event", "Count"], [[_e(k), _e(v)] for k, v in ev]))
        tk = sorted(m.get("tasks", {}).items())
        if tk:
            parts.append(_table(["Scheduled task or job", "Runs"], [[_e(k), _e(v)] for k, v in tk]))
        parts.append("</details>")

    def rows_section(title, rows, cols):
        if not rows:
            parts.append(f'<h2>{_e(title)}</h2><p class="ok">None.</p>')
            return
        parts.append(f"<h2>{_e(title)} ({len(rows)})</h2>")
        seen, body = Counter(), []
        for r in rows:
            k = (r.get("event") or r.get("job"), (r.get("detail") or "").split("\n")[0][:160])
            seen[k] += 1
            if seen[k] > 1:
                continue
            body.append(
                [_e(r.get(c, "")) for c in cols] + [f"<pre>{_e(r.get('detail', ''))}</pre>"]
            )
        parts.append(_table([*cols, "Detail (first of each kind)"], body))

    rows_section("Event errors", errs, ["date", "event", "area"])
    rows_section("Refusals", refusals, ["date", "event", "area"])
    rows_section("Failed background jobs", jobs, ["date", "job", "after"])
    if probes:
        parts.append("<h2>Probes (the portal must refuse)</h2>")
        parts.append(
            _table(
                ["Simulated date", "Probe", "Result", "Detail"],
                [
                    [
                        _e(p["date"]),
                        _e(p["event"]),
                        f'<span class="{"ok" if p["status"] == "pass" else "bad"}">{_e(p["status"])}</span>',
                        _e(p["detail"][:300]),
                    ]
                    for p in probes
                ],
            )
        )

    parts.append("<h2>Year-end figures</h2>")
    ye = data.get("year_end") or {}
    if ye.get("error"):
        parts.append(f'<p class="bad">{_e(ye["error"])}</p>')
    parts.append(f"<p>Months closed: {_e(ye.get('months_closed', 0))}.</p>")
    for fy, y in (ye.get("years") or {}).items():
        parts.append(
            f"<h3>FY{_e(fy)}</h3><p>Package: {_e(y['package_files'])} files, "
            f"{'year closed' if y['year_closed'] else 'year not closed'}, "
            f"{'built automatically' if y['automatic'] else 'built by a person'}. Tax tests: {_e(y.get('tax'))}.</p>"
        )
        parts.append(
            _table(
                ["Fund", "Assets", "Liabilities", "Fund balance", "Net income"],
                [
                    [
                        _e(f),
                        _cents(v["assets"]),
                        _cents(v["liabilities"]),
                        _cents(v["fund_balance"]),
                        _cents(v["net_income"]),
                    ]
                    for f, v in y["funds"].items()
                ],
            )
        )
    if not ye.get("years"):
        parts.append('<p class="muted">No fiscal year closed in this run.</p>')

    parts.append("<h2>Snapshots</h2>")
    snaps = data.get("snapshots") or []
    parts.append(
        _table(
            ["Month", "Simulated date", "File", "Size"],
            [
                [_e(s["month"]), _e(s["date"]), _e(s["path"] or s["detail"]), _e(s["size"])]
                for s in snaps
            ],
        )
        if snaps
        else '<p class="muted">None in this run.</p>'
    )
    parts.append(
        '<p class="muted">Crawl a snapshot: <code>python3 regression/run.py --stage page '
        "--from-snapshot regression/out/simulation/month-12.dump</code> (or --stage walkthrough).</p>"
    )
    parts.append(
        f'<p class="muted">Timing: setup {_e(t.get("setup_seconds"))} s, invariants '
        f"{_e(t.get('invariant_seconds'))} s in all, {_e(t.get('days'))} days.</p></body></html>"
    )
    return "\n".join(parts)


def main(argv=None) -> int:
    src = OUT / "simulation.json"
    if not src.exists():
        print("no regression/out/simulation.json: run python3 regression/run.py --stage simulation")
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    (OUT / "simulation.html").write_text(render(data), encoding="utf-8")
    print("wrote regression/out/simulation.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
