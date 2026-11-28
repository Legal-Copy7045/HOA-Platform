"""The simulation engine: one simulated day at a time, on a moved clock.

For each day (UTC):

1. the scenario plans the day's events (seeded RNG: the same seed gives the
   same history) at local business hours;
2. every periodic task fires at its cron times, merged with the events in
   time order; the clock moves to each moment first;
3. each event runs in its own transaction, as its person, through the
   portal's services; jobs it deferred are drained; per-event checks run
   (an audit event for every change, no staff step on one's own matter);
4. after the day: the daily invariants (``invariants.py``). On a break the
   run stops (unless ``keep_going``) and the report names the simulated date,
   the event and the invariant;
5. at months 6, 12 and 18: a ``pg_dump`` snapshot of the database.

Results go to ``regression/out/simulation.json``; ``report.py`` renders the
HTML report from it.
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import random
import subprocess  # nosec B404 - fixed pg_dump command
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from regression.simulation import invariants, tasks
from regression.simulation.clock import UTC, Clock, local

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "out"
SIM_OUT = OUT / "simulation"
RESULTS = OUT / "simulation.json"
SNAPSHOT_MONTHS = (6, 12, 18)


@dataclass
class Event:
    kind: str
    at: datetime.datetime
    fn: object
    area: str = ""
    staff: tuple = ()  # logins acting with staff powers (own-matter check)
    audit: bool = True  # a successful run must write an audit event
    expect_refusal: bool = False  # a probe: the portal must refuse it


@dataclass
class Options:
    months: int = 18
    seed: int = 47
    dataset: str = "fresh_start"
    fast: bool = False
    start: datetime.date | None = None
    keep_going: bool = False
    snapshots: tuple = SNAPSHOT_MONTHS
    database: str = ""
    max_per_day: int = 24
    days: int = 0  # stop after this many days (a short check), 0 for the whole run

    @property
    def end(self) -> datetime.date:
        y, m = divmod(self.start.month - 1 + self.months, 12)
        last = datetime.date(self.start.year + y, m + 1, 1) - datetime.timedelta(days=1)
        if self.days:
            return min(last, self.start + datetime.timedelta(days=self.days - 1))
        return last


def default_start(months: int, today: datetime.date | None = None) -> datetime.date:
    """The first of the month ``months`` months before this one: the run ends
    at the end of last month, so snapshots are never in the future."""
    today = today or datetime.date.today()
    y, m = divmod(today.month - 1 - months, 12)
    return datetime.date(today.year + y, m + 1, 1)


REFUSALS = (PermissionDenied, ValidationError)


def failure_detail(exc: BaseException) -> str:
    """What an error row keeps (review EXP-005, 0.47.0): the exception's
    class and message, and the frames as file:line in function only, never
    the source lines or local values."""
    root = HERE.parent.parent
    frames = []
    for f in traceback.extract_tb(exc.__traceback__)[-12:]:
        name = Path(f.filename)
        try:
            name = name.resolve().relative_to(root)
        except ValueError:
            name = Path(name.name)
        frames.append(f"  {name}:{f.lineno} in {f.name}")
    return f"{type(exc).__name__}: {str(exc)[:300]}\n" + "\n".join(frames)


def is_refusal(exc: Exception) -> bool:
    """A refusal the portal shows a person (its own error classes), not a crash."""
    if isinstance(exc, REFUSALS):
        return True
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    ours = module.split(".")[0] in {
        "accounts",
        "arc",
        "audit",
        "comms",
        "community",
        "compliance",
        "core",
        "documents",
        "governance",
        "ledger",
        "operations",
        "payments",
        "reports",
    }
    return ours and (name.endswith("Error") or name.endswith("Refused"))


@dataclass
class Run:
    options: Options
    started: float = field(default_factory=time.perf_counter)
    day: datetime.date | None = None
    counts: dict = field(default_factory=lambda: collections.defaultdict(collections.Counter))
    task_counts: dict = field(default_factory=lambda: collections.defaultdict(collections.Counter))
    invariant_days: dict = field(default_factory=lambda: collections.defaultdict(int))
    failures: list = field(default_factory=list)  # invariant breaks
    errors: list = field(default_factory=list)  # events that crashed
    refusals: list = field(default_factory=list)  # events the portal refused
    probes: list = field(default_factory=list)  # deliberate refusals and their outcome
    job_failures: list = field(default_factory=list)
    outbound_blocked: dict = field(default_factory=dict)  # host:port -> calls refused
    snapshots: list = field(default_factory=list)
    year_end: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    stopped: str = ""
    day_seconds: list = field(default_factory=list)
    seconds: collections.Counter = field(default_factory=collections.Counter)  # by task/event

    def month(self) -> str:
        return f"{self.day:%Y-%m}"


class Engine:
    def __init__(self, options: Options, scenario_cls, *, log=print):
        self.o = options
        self.scenario_cls = scenario_cls
        self.log = log
        self.run = Run(options)
        self.watch = None
        self.scenario = None

    # --- per event ---------------------------------------------------------------------------

    def _run_event(self, ev: Event) -> None:
        t0 = time.perf_counter()
        try:
            self._run_event_timed(ev)
        finally:
            self.run.seconds[f"event {ev.kind}"] += time.perf_counter() - t0

    def _run_event_timed(self, ev: Event) -> None:
        run = self.run
        before = invariants.last_audit_id()
        outcome = "ok"
        detail = ""
        try:
            with transaction.atomic():
                detail = ev.fn() or ""
        except Exception as exc:
            if ev.expect_refusal and is_refusal(exc):
                outcome = "refused as expected"
                run.probes.append(self._row(ev, "pass", f"{type(exc).__name__}: {exc}"))
            elif is_refusal(exc):
                outcome = "refused"
                run.refusals.append(self._row(ev, "refused", f"{type(exc).__name__}: {exc}"))
            else:
                outcome = "error"
                run.errors.append(self._row(ev, "error", failure_detail(exc)))
        else:
            if ev.expect_refusal:
                outcome = "probe not refused"
                run.probes.append(self._row(ev, "fail", "the portal allowed it"))
                self._break("conflicts.probe_refused", ev, f"{ev.kind}: the portal allowed it")
        if detail == "skip":
            run.counts[run.month()][f"{ev.kind} (nothing to do)"] += 1
            self._drain(ev)
            return
        run.counts[run.month()][ev.kind if outcome == "ok" else f"{ev.kind} ({outcome})"] += 1
        self._drain(ev)
        if outcome != "ok":
            return
        events = invariants.audit_since(before)
        if ev.audit and not events:
            self._break("audit.every_change", ev, f"{ev.kind} changed state with no audit event")
        staff_ids = {u.pk for u in ev.staff if u is not None}
        if staff_ids:
            problems = invariants.own_matter_steps(events, staff_ids)
            for p in problems:
                self._break("conflicts.own_matter", ev, p)

    def _row(self, ev: Event, status: str, detail: str) -> dict:
        return {
            "date": self.run.day.isoformat(),
            "at": ev.at.isoformat(),
            "event": ev.kind,
            "area": ev.area,
            "status": status,
            "detail": detail[:3000],
        }

    def _drain(self, ev: Event | None = None) -> None:
        def failed(name, text):
            self.run.job_failures.append(
                {
                    "date": self.run.day.isoformat(),
                    "job": name,
                    "after": ev.kind if ev else "",
                    "detail": text[:3000],
                }
            )

        for name, n in tasks.drain(on_failure=failed).items():
            self.run.task_counts[self.run.month()][f"job {name}"] += n

    def _break(self, key: str, ev: Event | None, problem: str) -> None:
        row = {
            "date": self.run.day.isoformat(),
            "invariant": key,
            "event": ev.kind if ev else "(end of day)",
            "problem": problem[:2000],
        }
        self.run.failures.append(row)
        self.log(f"  BREAK {row['date']} {key} after {row['event']}: {problem[:300]}")
        if not self.o.keep_going and not self.run.stopped:
            self.run.stopped = f"{row['date']}: {key} after {row['event']}"

    # --- the loop ----------------------------------------------------------------------------

    def periodic(self, name, func, at):
        t0 = time.perf_counter()
        try:
            self._periodic(name, func, at)
        finally:
            self.run.seconds[f"task {name}"] += time.perf_counter() - t0

    def _periodic(self, name, func, at):
        before = invariants.last_audit_id()
        try:
            result = func(timestamp=int(at.timestamp()))
        except Exception as exc:
            if is_refusal(exc):
                self.run.refusals.append(
                    {
                        "date": self.run.day.isoformat(),
                        "at": at.isoformat(),
                        "event": f"task {name}",
                        "area": "tasks",
                        "status": "refused",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                self.run.errors.append(
                    {
                        "date": self.run.day.isoformat(),
                        "at": at.isoformat(),
                        "event": f"task {name}",
                        "area": "tasks",
                        "status": "error",
                        "detail": failure_detail(exc),
                    }
                )
            return
        self.run.task_counts[self.run.month()][name.rsplit(".", 1)[-1]] += 1
        self._drain()
        worked = result if isinstance(result, int) and not isinstance(result, bool) else 0
        if worked and name in AUDITED_TASKS and invariants.last_audit_id() == before:
            self._break(
                "audit.every_change",
                Event(f"task {name}", at, None),
                f"task {name} did {worked} thing(s) with no audit event",
            )

    def simulate(self) -> Run:
        o, run = self.o, self.run
        start_at = local(o.start, 6)
        with Clock(start_at) as clock:
            run.day = o.start
            self.scenario = self.scenario_cls(o, log=self.log)
            t0 = time.perf_counter()
            self.scenario.prepare(clock)
            self._drain()
            run.timings["setup_seconds"] = round(time.perf_counter() - t0, 1)
            self.watch = invariants.AppendOnlyWatch()
            self.watch.check()
            periodic = tasks.periodic_tasks()
            day = o.start
            while day <= o.end and not run.stopped:
                t_day = time.perf_counter()
                run.day = day
                rng = random.Random(f"{o.seed}:{day.isoformat()}")  # noqa: S311  # nosec B311 - a reproducible scenario
                events = self.scenario.events_for(day, rng)
                steps = [(e.at, 1, e) for e in events]
                for utc_day in (day, day + datetime.timedelta(days=1)):
                    for at, p in tasks.schedule_for(utc_day, periodic, max_per_day=o.max_per_day):
                        # the local day's tasks: from 00:00 local to 24:00 local
                        if local(day, 0) <= at < local(day + datetime.timedelta(days=1), 0):
                            steps.append((at, 0, p))
                steps.sort(key=lambda s: (s[0], s[1]))
                for at, kind, item in steps:
                    if run.stopped:
                        break
                    clock.move_to(at)
                    if kind == 1:
                        self._run_event(item)
                    else:
                        self.periodic(item.name, item.func, at)
                clock.move_to(local(day, 23, 30))
                self.scenario.end_of_day(day)
                self._drain()
                t_inv = time.perf_counter()
                for r in invariants.run_daily(
                    day,
                    self.watch,
                    injected_funds=self.scenario.injected_funds,
                    injected_months=self.scenario.injected_months,
                ):
                    if r.ok:
                        run.invariant_days[r.key] += 1
                    else:
                        for p in r.problems:
                            self._break(r.key, None, p)
                run.timings.setdefault("invariant_seconds", 0)
                run.timings["invariant_seconds"] += time.perf_counter() - t_inv
                run.day_seconds.append(round(time.perf_counter() - t_day, 2))
                nxt = day + datetime.timedelta(days=1)
                if nxt.day == 1:
                    months_done = (nxt.year - o.start.year) * 12 + nxt.month - o.start.month
                    self.log(
                        f"month {months_done} ({day:%Y-%m}) done: "
                        f"{sum(run.counts[f'{day:%Y-%m}'].values())} events, "
                        f"{len(run.failures)} break(s), {len(run.errors)} error(s), "
                        f"{(time.perf_counter() - run.started) / 60:.1f} min"
                    )
                    if months_done in o.snapshots:
                        self.snapshot(months_done)
                    self.scenario.end_of_month(day)
                day = nxt
            run.year_end = self.scenario.year_end_figures()
            run.outbound_blocked = dict(getattr(self.scenario.fakes, "outbound_blocked", {}))
            run.timings["total_seconds"] = round(time.perf_counter() - run.started, 1)
        return run

    def snapshot(self, month: int) -> None:
        from django.db import connection

        SIM_OUT.mkdir(parents=True, exist_ok=True)
        path = SIM_OUT / f"month-{month:02d}.dump"
        params = connection.settings_dict
        env = {**os.environ, "PGPASSWORD": params.get("PASSWORD") or ""}
        cmd = [
            "pg_dump",
            "-Fc",
            "-h",
            params.get("HOST") or "localhost",
            "-p",
            str(params.get("PORT") or 5432),
            "-U",
            params["USER"],
            "-f",
            str(path),
            params["NAME"],
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)  # nosec B603
        ok = proc.returncode == 0 and path.exists()
        if ok:
            # The stored files go with the snapshot (page_setup.py restore copies them).
            import shutil

            from django.conf import settings

            media = path.with_name(path.stem + "-media")
            shutil.rmtree(media, ignore_errors=True)
            if Path(settings.MEDIA_DIR).is_dir():
                shutil.copytree(settings.MEDIA_DIR, media)
        self.run.snapshots.append(
            {
                "month": month,
                "date": self.run.day.isoformat(),
                "path": str(path.relative_to(HERE.parent.parent)) if ok else "",
                "size": path.stat().st_size if ok else 0,
                "ok": ok,
                "detail": "" if ok else proc.stderr[-500:],
            }
        )
        self.log(f"  snapshot month {month}: {'ok' if ok else 'FAILED'} {path.name}")


# Periodic tasks whose work changes records and must leave audit events.
AUDITED_TASKS = {
    "ledger.tasks.monthly_assessments",
    "ledger.tasks.late_fees",
    "ledger.tasks.books_auto_close",
    "ledger.tasks.overdue_interest",
    "ledger.tasks.special_assessments",
    "arc.tasks.arc_deemed_approvals",
    "governance.tasks.close_due_votes",
}


def results_json(run: Run) -> dict:
    o = run.options
    months = sorted(set(run.counts) | set(run.task_counts))
    return {
        "options": {
            "months": o.months,
            "seed": o.seed,
            "dataset": o.dataset,
            "fast": o.fast,
            "start": o.start.isoformat(),
            "end": o.end.isoformat(),
            "keep_going": o.keep_going,
        },
        "finished": datetime.datetime.now(UTC).isoformat(timespec="seconds"),
        "simulated_through": run.day.isoformat() if run.day else "",
        "stopped": run.stopped,
        "timeline": [
            {
                "month": m,
                "events": dict(run.counts.get(m, {})),
                "tasks": dict(run.task_counts.get(m, {})),
            }
            for m in months
        ],
        "invariants": dict(run.invariant_days),
        "failures": run.failures,
        "errors": run.errors,
        "refusals": run.refusals,
        "probes": run.probes,
        "job_failures": run.job_failures,
        "outbound_blocked": run.outbound_blocked,
        "snapshots": run.snapshots,
        "year_end": run.year_end,
        "timings": {
            **{k: round(v, 1) if isinstance(v, float) else v for k, v in run.timings.items()},
            "days": len(run.day_seconds),
            "mean_day_seconds": round(sum(run.day_seconds) / max(1, len(run.day_seconds)), 2),
            "slowest": [[k, round(v, 1)] for k, v in run.seconds.most_common(15)],
        },
    }


def write_results(run: Run, path: Path = RESULTS) -> dict:
    data = results_json(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return data
