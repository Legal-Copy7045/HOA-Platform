"""The accelerated simulation's tooling (D102): the cron reader, the stage's
checks from a results file, the run.py options, and a one-month smoke run.

The smoke run is a subprocess on a database of its own (``SIM_SMOKE_DB``,
default ``hoa_sim_smoke``, on the server in DATABASE_URL): the simulation sets
Django up itself, on its own settings, with its clock moved by time-machine.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess  # nosec B404 - the simulation's own script
import sys
from pathlib import Path

import pytest

from regression import run
from regression.simulation.clock import Cron

ROOT = Path(__file__).resolve().parents[2]


def test_cron_fields_and_days():
    c = Cron("23 5 1 * *")
    assert c.runs_on(datetime.date(2026, 3, 1)) and not c.runs_on(datetime.date(2026, 3, 2))
    assert [t.strftime("%H:%M") for t in c.fire_times(datetime.date(2026, 3, 1))] == ["05:23"]
    monday = Cron("13 7 * * 1")
    assert monday.runs_on(datetime.date(2026, 11, 30)) and not monday.runs_on(
        datetime.date(2026, 11, 29)
    )
    every5 = Cron("2-59/5 * * * *")
    assert every5.per_day() == 288
    assert len(every5.fire_times(datetime.date(2026, 1, 1), max_per_day=24)) == 24
    assert Cron("7,27,47 * * * *").minutes == [7, 27, 47]
    assert Cron("41 */4 * * *").hours == [0, 4, 8, 12, 16, 20]


def test_every_periodic_task_has_a_cron_the_simulation_reads():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.test")
    from regression.simulation import tasks

    names = {p.name for p in tasks.periodic_tasks()}
    assert "ledger.tasks.books_auto_close" in names and "ledger.tasks.late_fees" in names
    assert "core.tasks.remove_old_jobs" not in names  # Procrastinate's own housekeeping
    for p in tasks.periodic_tasks():
        assert p.cron.per_day() >= 1


def _data(**over):
    data = {
        "options": {"start": "2025-06-01", "months": 2},
        "stopped": "",
        "simulated_through": "2025-07-31",
        "invariants": {"books.trial_balance": 61, "close.month_closed": 61},
        "failures": [
            {
                "date": "2025-07-04",
                "invariant": "close.month_closed",
                "event": "(end of day)",
                "problem": "x",
            }
        ],
        "timeline": [
            {"month": "2025-06", "events": {"payment.check": 3, "deposit.made (nothing to do)": 1}}
        ],
        "errors": [{"date": "2025-06-09", "event": "arc.request", "detail": "TypeError: y\ntrace"}],
        "job_failures": [],
        "probes": [
            {
                "event": "probe.manager_approves_invoice",
                "status": "pass",
                "detail": "PermissionDenied",
            }
        ],
        "snapshots": [
            {
                "month": 6,
                "ok": True,
                "detail": "",
                "path": "regression/out/simulation/month-06.dump",
            }
        ],
        "year_end": {"years": {"2025": {"year_closed": True, "package_files": 12}}},
    }
    data.update(over)
    return data


def test_the_stage_checks_from_a_results_file():
    rows = {key: (status, detail) for key, status, detail, _e in run.simulation_checks(_data())}
    assert rows["invariant: books.trial_balance"][0] == "pass"
    assert rows["invariant: close.month_closed"] == ("fail", "2025-07-04 after (end of day): x")
    assert rows["event: payment.check"][0] == "pass"
    assert rows["event: deposit.made"][0] == "pass"  # "(nothing to do)" is the same kind
    assert rows["event: arc.request"] == ("fail", "2025-06-09: TypeError: y")
    assert rows["probe refused: probe.manager_approves_invoice"][0] == "pass"
    assert rows["snapshot month 6"][0] == "pass"
    assert rows["year-end package, fiscal year 1 of the run"][0] == "pass"
    assert rows["no background job failed"][0] == "pass"


def test_simulation_is_never_a_default_stage_nor_in_check():
    assert "simulation" not in run.DEFAULT_STAGES
    assert run.chosen_stages([]) == ["ci", "unit", "page", "manual"]
    assert run.chosen_stages(["--stage", "simulation", "--months", "3", "--fast"]) == ["simulation"]
    assert run.OPTIONS["months"] == 3 and run.OPTIONS["fast"]
    run.chosen_stages([])


def test_the_workflow_is_manual_only():
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "simulation.yml").read_text())
    triggers = wf.get("on") or wf.get(True)
    assert set(triggers) == {"workflow_dispatch"}
    job = wf["jobs"]["simulate"]
    assert job["timeout-minutes"] <= 350 and wf["permissions"] == {"contents": "read"}
    text = (ROOT / ".github" / "workflows" / "simulation.yml").read_text()
    assert "retention-days: 3" in text and "${{ inputs" not in text.split("run: |", 1)[1]


def _smoke_url() -> str:
    base = os.environ.get("DATABASE_URL", "postgres://hoa_owner:hoa_owner@localhost:5432/hoa")
    return base.rsplit("/", 1)[0]


@pytest.mark.skipif(os.environ.get("SIM_SMOKE", "1") == "0", reason="SIM_SMOKE=0")
def test_a_one_month_smoke_run_keeps_every_invariant(tmp_path):
    out = tmp_path / "simulation.json"
    env = {
        **os.environ,
        "SIM_DATABASE_BASE": _smoke_url(),
        "PYTHONPATH": str(ROOT),
    }
    env.pop("DJANGO_SETTINGS_MODULE", None)
    proc = subprocess.run(  # nosec B603
        [
            sys.executable,
            str(ROOT / "regression" / "simulation" / "sim.py"),
            "--smoke",
            "--db",
            os.environ.get("SIM_SMOKE_DB", "hoa_sim_smoke"),
            "--out",
            str(out),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
    )
    assert out.exists(), proc.stdout[-2000:] + proc.stderr[-2000:]
    data = json.loads(out.read_text())
    assert not data["stopped"], data["failures"][:3]
    assert not data["failures"], data["failures"][:3]
    assert not data["errors"], [e["detail"][:400] for e in data["errors"][:3]]
    assert data["invariants"]["ledger.home_accounts"] >= 28
    events = data["timeline"][0]["events"]
    assert events.get("payment.check", 0) > 5 and events.get("setup.bank_and_books") == 1
