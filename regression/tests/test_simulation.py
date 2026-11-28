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


# --- Review fixes, 0.47.0 (regression/catalogue/fixes_047.yaml) ------------------------------


def _django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.test")
    import django

    django.setup()


def test_every_append_only_table_is_watched_whatever_its_key():
    """BUG-001: the UUID-keyed secret-ballot and survey tables are watched too."""
    _django()
    from django.apps import apps

    from core.append_only import AppendOnlyModel
    from regression.simulation import invariants

    watched = {t: integer for t, _pk, integer in invariants.AppendOnlyWatch().tables}
    for model in apps.get_models():
        if issubclass(model, AppendOnlyModel) and not model._meta.proxy:
            assert model._meta.db_table in watched, model._meta.label
    for table in (
        "governance_secretchoice",
        "governance_electionchoice",
        "governance_surveyresponse",
        "governance_surveyanswer",
    ):
        assert watched[table] is False, table


def test_a_changed_or_deleted_uuid_row_is_a_break(monkeypatch):
    _django()
    import collections

    from regression.simulation import invariants

    watch = invariants.AppendOnlyWatch()
    watch.tables = [("governance_secretchoice", "id", False)]
    states = iter(
        [
            {"a": 1, "b": 1},
            {"a": 1, "b": 1, "c": 1},  # a row added: fine
            {"a": 1, "x": 1, "c": 1},  # b changed
            {"a": 1, "x": 1},  # c deleted
        ]
    )
    monkeypatch.setattr(watch, "_row_hashes", lambda table: collections.Counter(next(states)))
    assert watch.check() == []
    assert watch.check() == []
    assert watch.check() == ["governance_secretchoice: 1 row(s) changed"]
    assert watch.check() == ["governance_secretchoice: 1 row(s) deleted"]


@pytest.mark.django_db
def test_the_append_only_watch_reads_every_table_on_the_database():
    from regression.simulation import invariants

    watch = invariants.AppendOnlyWatch()
    assert watch.check() == [] and watch.check() == []
    assert "governance_secretchoice" in watch.rows


@pytest.mark.django_db
def test_statement_emails_are_checked_account_by_account():
    """BUG-002: one account sent is not enough; every emailable account must be."""
    from django.utils import timezone

    from community import services as community
    from community.models import Account
    from community.tests.factories import LotFactory, make_member
    from core.policy import SYSTEM
    from core.scoping import system_access
    from ledger import services
    from ledger.models import StatementEmail
    from regression.simulation import invariants

    today = timezone.localdate()
    community.set_rule(key="dues.monthly_cents", value="4500", citation="Decl 6", actor=SYSTEM)
    lots = [LotFactory() for _ in range(3)]
    for lot in lots:
        make_member(lots=[lot])
    services.post_monthly_assessments(year=today.year, month=today.month, actor=SYSTEM)
    with system_access("test"):
        accounts = [Account.objects.get(lot=lot, closed_on__isnull=True) for lot in lots]
    day3 = today.replace(day=3)
    period = f"{today:%Y-%m}"

    def check():
        return invariants._guard("reminders.statement_emails", invariants.statements_sent, day3)

    result = check()
    assert len(result.problems) == 1 and "3 account(s)" in result.problems[0]
    with system_access("test"):
        StatementEmail.objects.create(account=accounts[0], period=period, recipients=1)
    result = check()
    assert "2 account(s)" in result.problems[0]
    assert f"#{accounts[0].pk}," not in result.problems[0] + ","
    assert f"#{accounts[1].pk}" in result.problems[0]
    with system_access("test"):
        for account in accounts[1:]:
            StatementEmail.objects.create(account=account, period=period, recipients=1)
    assert check().ok


def test_sim_refuses_to_drop_a_database_that_is_not_a_simulation_one(monkeypatch):
    """BUG-003 / EXP-004: never the development, regression or any other database."""
    from regression.simulation import sim

    env = {"DATABASE_URL": "postgres://u:p@localhost:5432/hoa_dev", "HOA_DB_NAME": "hoa_x_sim"}
    for name in ("hoa", "postgres", "hoa_dev", "hoa_x_sim", "other_sim", "HOA_SIM", "hoa-sim", ""):
        assert sim.database_problem(name, env), name
    for name in ("hoa_sim", "hoa_sim_smoke", "hoa_bfix47_sim", "hoa_bsim_six"):
        assert sim.database_problem(name, env) == "", name

    def boom(*a, **kw):
        raise AssertionError("must not connect")

    monkeypatch.setattr(sim, "fresh_database", boom)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost:5432/hoa_sim_mine")
    assert sim.main(["--db", "hoa"]) == 2
    assert sim.main(["--db", "hoa_owner_data"]) == 2
    assert sim.main(["--db", "hoa_sim_mine"]) == 2  # the portal's own database


def test_sim_months_and_seed_are_range_checked():
    """EXP-003 / BUG-006: 1 to 24 months, a seed of at most 9 digits."""
    from regression.simulation import sim

    assert sim.parse(["--months", "1"]).months == 1
    assert sim.parse(["--months", "24", "--seed", "123456789"]).months == 24
    for bad in (
        ["--months", "0"],
        ["--months", "25"],
        ["--months", chr(0xB2)],
        ["--seed", "1" * 10],
    ):
        with pytest.raises(SystemExit):
            sim.parse(bad)
    for bad in ("0", "25", "-1", "x"):
        with pytest.raises(SystemExit):
            run.chosen_stages(["--stage", "simulation", "--months", bad])
    assert run.sim_months("18") == 18
    run.chosen_stages([])


def test_the_workflow_checks_the_months_range_and_pins_postgres():
    """EXP-003 and SEC-001: the step refuses months outside 1-24; every
    workflow Postgres service is the docker-compose.yml image, by digest."""
    import re

    text = (ROOT / ".github" / "workflows" / "simulation.yml").read_text()
    assert '"$SIM_MONTHS" -gt 24' in text and '"$SIM_MONTHS" -lt 1' in text
    assert '"${#SIM_SEED}" -gt 9' in text
    compose = (ROOT / "docker-compose.yml").read_text()
    pinned = re.search(r"image: (postgres:\S+@sha256:[0-9a-f]{64})", compose).group(1)
    for wf in ("ci.yml", "simulation.yml"):
        found = re.findall(r"image: (postgres:\S+)", (ROOT / ".github/workflows" / wf).read_text())
        assert found and all(i == pinned for i in found), (wf, found)


def test_the_network_guard_refuses_and_records_outside_hosts():
    """EXP-001: an unfaked outside call fails loudly and is counted."""
    import socket

    from regression.simulation import netguard

    guard = netguard.Guard({"db.internal"})
    real = socket.getaddrinfo
    guard.install()
    try:
        with pytest.raises(netguard.OutboundBlocked):
            socket.getaddrinfo("fake-clamd", 3310)
        with pytest.raises(netguard.OutboundBlocked):
            socket.create_connection(("93.184.215.14", 443), timeout=1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(netguard.OutboundBlocked):
                sock.connect(("93.184.215.14", 443))
        finally:
            sock.close()
        assert guard.allows("localhost") and guard.allows("127.0.0.1") and guard.allows("::1")
        assert guard.allows("db.internal") and not guard.allows("smtp.example.org")
        socket.getaddrinfo("localhost", 5432)  # the database is reachable
    finally:
        guard.uninstall()
    assert socket.getaddrinfo is real
    assert guard.blocked == {"fake-clamd:3310": 1, "93.184.215.14:443": 2}


def test_the_fakes_answer_the_scanner_health_check_without_a_socket(settings):
    """EXP-001: the health check's ClamAV version query is faked; USPS (off)
    answers 503 (EXP-006); the guard is in while the fakes are."""
    _django()
    import socket

    from comms import usps_iv
    from core import health
    from regression.simulation import netguard
    from regression.simulation.fakes import Fakes

    settings.DOCUMENT_SCANNER = "clamd"
    settings.DOCUMENT_CLAMD_ADDRESS = "fake-clamd:3310"
    fakes = Fakes()
    real = health.scanner_version
    fakes.install()
    try:
        assert health._signatures()[0] is True
        assert usps_iv.transport("GET", "https://example.org/", None, {})[0] == 503
        with pytest.raises(netguard.OutboundBlocked):
            socket.getaddrinfo("fake-clamd", 3310)
    finally:
        fakes.uninstall()
    assert health.scanner_version is real
    assert fakes.outbound_blocked == {"fake-clamd:3310": 1}


def test_the_stage_fails_on_an_outside_call_or_a_refused_task_or_kind():
    """EXP-001 and BUG-006 in the stage's checks."""
    data = _data(
        timeline=[
            {
                "month": "2025-06",
                "events": {
                    "payment.check": 6,
                    "payment.check (refused)": 4,
                    "arc.request": 9,
                    "arc.request (refused)": 1,
                },
            }
        ],
        errors=[],
        refusals=[
            *[{"event": "payment.check", "detail": "LedgerError: x"}] * 4,
            {"event": "arc.request", "detail": "ArcError: y"},
            {"event": "task ledger.tasks.late_fees", "detail": "AllocationError: z"},
        ],
        outbound_blocked={"fake-clamd:3310": 2},
    )
    rows = {key: (status, detail) for key, status, detail, _e in run.simulation_checks(data)}
    assert rows["no outbound network call"] == ("fail", "fake-clamd:3310 x2")
    assert rows["refusals: payment.check"][0] == "fail"  # 4 of 10
    assert rows["refusals: arc.request"][0] == "pass"  # 1 of 10
    assert rows["refusals: task ledger.tasks.late_fees"][0] == "fail"
    clean = {k: s for k, s, _d, _e in run.simulation_checks(_data(refusals=[]))}
    assert clean["no outbound network call"] == "pass"


def test_error_rows_keep_frames_not_source_lines():
    """EXP-005: the class, the message and file:line frames only."""
    from regression.simulation.engine import failure_detail

    def inner():
        hidden = "sim0001@example.com"
        raise ValueError("boom" if hidden else "")

    try:
        inner()
    except ValueError as exc:
        text = failure_detail(exc)
    assert text.startswith("ValueError: boom\n")
    assert "regression/tests/test_simulation.py:" in text and "in inner" in text
    assert "hidden" not in text and "sim0001" not in text
