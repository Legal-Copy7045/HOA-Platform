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
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_DB = "hoa_sim"


def parse(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--months", type=int, default=int(os.environ.get("SIM_MONTHS", "18")))
    p.add_argument("--seed", type=int, default=int(os.environ.get("SIM_SEED", "47")))
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
        f"{len(data['refusals'])} refusal(s), {len(data['job_failures'])} failed job(s)"
        + (f"; stopped at {data['stopped']}" if data["stopped"] else ""),
        flush=True,
    )
    return 1 if data["failures"] or data["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
