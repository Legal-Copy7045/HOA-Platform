#!/usr/bin/env python3
"""Run the accelerated simulation (D102).

    .venv/bin/python regression/simulation/sim.py [--months 18] [--seed 47]
        [--dataset fresh_start|blank] [--fast] [--start YYYY-MM-DD] [--keep-going]
        [--db hoa_sim] [--smoke]

Creates (drops first) its own database, migrates it, seeds the dataset on the
first simulated day and plays the community day by day (engine.py). Writes
regression/out/simulation.json; `run.py report --simulation` renders the
HTML. Exit status 1 when an invariant broke or an event crashed.
``--smoke`` is the quick check the unit test uses: one month, fewer events,
no snapshots.

Guards (review fixes, 0.47.0): ``--months`` is 1 to 24 and ``--seed`` at
most 9 digits (EXP-003); the database it drops and re-creates must be a
simulation database, named ``hoa_`` and containing ``sim`` (lower-case
letters, digits and underscores: ``hoa_sim``, ``hoa_bfix47_sim``), and never
the database in ``DATABASE_URL`` or ``HOA_DB_NAME`` or the development
database ``hoa`` (BUG-003, EXP-004). Any outside lookup
or connection is refused and fails the run (EXP-001, ``netguard.py``).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_DB = "hoa_sim"
MONTHS = (1, 24)
SEED_DIGITS = 9
# A simulation database: hoa_ and "sim" in the name (hoa_sim, hoa_sim_smoke,
# hoa_bsim_six, hoa_bfix47_sim), lower-case letters, digits and underscores.
SIM_DB_RE = re.compile(r"hoa_[a-z0-9_]*sim[a-z0-9_]*")
NEVER_DROP = {"hoa", "postgres", "template0", "template1"}


def months_arg(value: str) -> int:
    text = str(value).strip()
    if not (text.isascii() and text.isdigit()) or not MONTHS[0] <= int(text) <= MONTHS[1]:
        raise argparse.ArgumentTypeError(
            f"months must be a whole number from {MONTHS[0]} to {MONTHS[1]}"
        )
    return int(text)


def seed_arg(value: str) -> int:
    text = str(value).strip()
    if not (text.isascii() and text.isdigit()) or len(text) > SEED_DIGITS:
        raise argparse.ArgumentTypeError(
            f"seed must be a whole number of at most {SEED_DIGITS} digits"
        )
    return int(text)


def _db_name(url: str) -> str:
    return (url or "").rsplit("/", 1)[-1].split("?", 1)[0]


def database_problem(name: str, environ=None) -> str:
    """Why ``name`` must not be dropped and re-created, or "" when it is a
    simulation database (review BUG-003 / EXP-004, 0.47.0)."""
    environ = os.environ if environ is None else environ
    if not SIM_DB_RE.fullmatch(name or ""):
        return (
            f"{name!r} is not a simulation database: use a name that starts with hoa_ "
            "and contains sim (lower-case letters, digits and underscores)"
        )
    protected = set(NEVER_DROP)
    protected.add(_db_name(environ.get("DATABASE_URL", "")))
    protected.add(environ.get("HOA_DB_NAME", ""))
    if name in protected - {""}:
        return f"{name!r} is the portal's own database, never the simulation's"
    return ""


def parse(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--months", type=months_arg, default=os.environ.get("SIM_MONTHS", "18"))
    p.add_argument("--seed", type=seed_arg, default=os.environ.get("SIM_SEED", "47"))
    p.add_argument(
        "--dataset",
        choices=["fresh_start", "blank"],
        default=os.environ.get("SIM_DATASET", "fresh_start"),
    )
    p.add_argument("--fast", action="store_true", default=os.environ.get("SIM_FAST") == "1")
    p.add_argument("--start", default=os.environ.get("SIM_START", ""))
    p.add_argument("--keep-going", action="store_true")
    p.add_argument("--db", default=os.environ.get("SIM_DB", DEFAULT_DB))
    p.add_argument("--days", type=int, default=0, help="stop after this many days")
    p.add_argument("--smoke", action="store_true", help="one month, fast, no snapshots")
    p.add_argument(
        "--out", default="", help="results file (default regression/out/simulation.json)"
    )
    return p.parse_args(argv)


def database_url(db: str) -> str:
    base = os.environ.get(
        "SIM_DATABASE_BASE", "postgres://hoa_owner:hoa_owner@localhost:5432"
    ).rstrip("/")
    return f"{base}/{db}"


def fresh_database(url: str, name: str) -> None:
    import psycopg
    from psycopg import sql

    problem = database_problem(name, environ={})  # main() checked the environment
    if problem:
        raise SystemExit(f"simulation: refusing to drop {problem}")
    admin = url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def main(argv=None) -> int:
    args = parse(argv)
    if args.smoke:
        args.months, args.fast = 1, True
    problem = database_problem(args.db)  # checked before DATABASE_URL is replaced
    if problem:
        print(f"simulation: refusing to drop {problem}", file=sys.stderr)
        return 2
    url = database_url(args.db)
    os.environ["SIM_DATABASE_URL"] = url
    os.environ["DATABASE_URL"] = url
    os.environ["DJANGO_SETTINGS_MODULE"] = "regression.simulation.settings"
    fresh_database(url, args.db)

    import shutil

    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    media = Path(settings.MEDIA_DIR)
    if media.is_dir() and media.parent.name == "media":
        shutil.rmtree(media)
    call_command("migrate", verbosity=0, interactive=False)
    call_command("createcachetable", verbosity=0)

    from regression.simulation import engine
    from regression.simulation.scenario import Scenario

    start = (
        datetime.date.fromisoformat(args.start) if args.start else engine.default_start(args.months)
    )
    options = engine.Options(
        months=args.months,
        seed=args.seed,
        dataset=args.dataset,
        fast=args.fast,
        start=start,
        keep_going=args.keep_going,
        snapshots=() if args.smoke else engine.SNAPSHOT_MONTHS,
        database=args.db,
        max_per_day=6 if args.fast else 24,
        days=args.days,
    )
    print(
        f"simulation: {options.months} month(s) from {options.start} (seed {options.seed}, "
        f"{options.dataset}{', fast' if options.fast else ''}) on database {args.db}",
        flush=True,
    )
    eng = engine.Engine(options, Scenario, log=lambda s: print(s, flush=True))
    try:
        run = eng.simulate()
    finally:
        if eng.scenario is not None:
            eng.scenario.fakes.uninstall()
    out = Path(args.out) if args.out else engine.RESULTS
    data = engine.write_results(run, out)
    print(
        f"simulated through {data['simulated_through']} in {data['timings']['total_seconds'] / 60:.1f} "
        f"min: {len(data['failures'])} invariant break(s), {len(data['errors'])} event error(s), "
        f"{len(data['refusals'])} refusal(s), {len(data['job_failures'])} failed job(s), "
        f"{sum(data['outbound_blocked'].values())} outside call(s) refused"
        + (f"; stopped at {data['stopped']}" if data["stopped"] else ""),
        flush=True,
    )
    return 1 if data["failures"] or data["errors"] or data["outbound_blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
