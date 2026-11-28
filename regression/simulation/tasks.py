"""The portal's background work, run on simulated time.

Every periodic task registered with Procrastinate is fired on its own cron
(UTC) for each simulated day, the way the worker's scheduler would; jobs the
portal defers (statement emails, ARC decision emails, the secret-choice
flush, vendor links...) are drained from Procrastinate's own queue table after
every step through Procrastinate's SQL functions, so the job rows move
todo -> doing -> succeeded/failed exactly as with a worker (then deleted, as
the worker's ``remove_old_jobs`` would two weeks later).
"""

from __future__ import annotations

import asyncio
import datetime
import json
from dataclasses import dataclass

from django.db import connection

from regression.simulation.clock import Cron

# Built-in housekeeping that needs a running worker (async, Procrastinate's own
# job tables); the engine removes finished jobs itself.
NOT_SIMULATED = {"core.tasks.remove_old_jobs": "Procrastinate housekeeping (async worker only)"}


@dataclass
class Periodic:
    name: str
    cron: Cron
    func: object


def periodic_tasks() -> list[Periodic]:
    from procrastinate.contrib.django import app

    out = []
    for (name, _pid), p in sorted(app.periodic_registry.periodic_tasks.items()):
        if name in NOT_SIMULATED:
            continue
        out.append(Periodic(name, Cron(p.cron), p.task.func))
    return out


# Checks that only read and alert (no community record changes): a few runs a
# day are enough on simulated time, where nothing happens between them.
THIN = {"core.tasks.health_alerts": 2, "core.tasks.worker_heartbeat": 2}


def schedule_for(day: datetime.date, tasks: list[Periodic], *, max_per_day: int):
    """(UTC time, task) pairs for one UTC day, in time order."""
    rows = [
        (t, p)
        for p in tasks
        for t in p.cron.fire_times(day, max_per_day=min(max_per_day, THIN.get(p.name, 10**6)))
    ]
    return sorted(rows, key=lambda r: (r[0], r[1].name != "core.tasks.worker_heartbeat", r[1].name))


class JobFailure(Exception):
    pass


def _task_by_name():
    from procrastinate.contrib.django import app

    return app.tasks


def drain(*, limit: int = 2000, on_failure=None) -> dict[str, int]:
    """Run every waiting job (deferred by the portal) once, oldest first.

    Returns counts per task name. A job that raises is finished as failed and
    reported to ``on_failure(task_name, text)``; its retries are not replayed
    (a retry would find the same fault).
    """
    tasks = _task_by_name()
    done: dict[str, int] = {}
    for _ in range(limit):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE procrastinate_jobs SET status = 'doing' WHERE id = ("
                "SELECT id FROM procrastinate_jobs WHERE status = 'todo' ORDER BY id LIMIT 1 "
                "FOR UPDATE SKIP LOCKED) RETURNING id, task_name, args"
            )
            row = cur.fetchone()
        if row is None:
            break
        job_id, name, args = row
        if isinstance(args, str):
            args = json.loads(args)
        task = tasks.get(name)
        status = "succeeded"
        try:
            if task is None:
                raise JobFailure(f"no task registered as {name}")
            result = task.func(**(args or {}))
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except Exception as exc:  # a failing job is a finding, not a crash
            status = "failed"
            if on_failure is not None:
                from regression.simulation.engine import failure_detail

                on_failure(name, failure_detail(exc))
        with connection.cursor() as cur:
            cur.execute(
                "SELECT procrastinate_finish_job_v1(%s, %s, %s)",
                [job_id, status, status == "succeeded"],
            )
        done[name] = done.get(name, 0) + 1
    return done


def waiting_jobs() -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'")
        return cur.fetchone()[0]
