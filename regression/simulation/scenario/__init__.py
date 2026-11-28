"""The scenario: what the community does, day by day, through the services.

``Scenario.events_for(day, rng)`` returns the day's events. The plan reads the
database (what is open, who owes what) and the day's seeded RNG, so the same
seed gives the same history. Each area is a module with ``plan(sc, day, rng)``:

* ``setup``: the first day: the board sets the community up (rules, features,
  books, bank, budget, reserve plan, vendors, contracts, staff, neighbours);
* ``money``: assessments and payments by profile, deposits, online and
  automatic payments, Stripe payouts, the bank's own lines, invoices and
  vendor checks, reimbursements, collections (letters, legal, lien, release,
  payment plan), interest, special assessment, budget preparation, reserve
  spending, an injected close problem and its fix, the year end;
* ``people``: meetings, the annual meeting and election (secret ballot,
  board turnover), votes, surveys, announcements, ARC requests with neighbour
  acknowledgements and recusal, violations, owner and records requests,
  reservations, RSVPs, home sales with resale certificates, vendor
  onboarding, insurance claim, capital project, renewals, probes.

``fast`` thins the optional, repetitive events (fewer requests, fewer
reservations), never the money or the closes.
"""

from __future__ import annotations

import datetime

from core.policy import SYSTEM
from core.scoping import system_access
from regression.simulation import world as world_mod
from regression.simulation.clock import local
from regression.simulation.engine import Event
from regression.simulation.fakes import Fakes


class Scenario:
    def __init__(self, options, *, log=print):
        self.o = options
        self.log = log
        self.fakes = Fakes()
        self.world = None
        # Problems injected on purpose: the invariants expect them.
        self.injected_funds: set[str] = set()
        self.injected_months: dict[str, str] = {}
        self.notes: dict = {}

    # --- helpers for the area modules ----------------------------------------------------------

    def ev(self, kind, day, hour, fn, *, minute=0, area="", staff=(), audit=True, probe=False):
        return Event(
            kind=kind,
            at=local(day, hour, minute),
            fn=fn,
            area=area or kind.split(".")[0],
            staff=tuple(u for u in staff if u is not None),
            audit=audit,
            expect_refusal=probe,
        )

    def month_index(self, day: datetime.date) -> int:
        return (day.year - self.o.start.year) * 12 + day.month - self.o.start.month

    # --- the engine's interface ----------------------------------------------------------------

    def prepare(self, clock) -> None:
        """Seed the dataset on the first simulated day, cast the people, and
        put the fakes in place. Setting the community up is the first day's events."""
        from community import demo

        self.fakes.install()
        with system_access("simulation seed"):
            result = demo.seed(self.o.dataset)
            from core.models import DemoSeed

            DemoSeed.objects.create(dataset=self.o.dataset)
        self.log(f"seeded {self.o.dataset}: {result.summary[:120]}")
        if self.o.dataset == "blank":
            from regression.simulation.scenario import setup

            setup.blank_start(self)
        self.world = world_mod.build(self.o.seed)
        self.fakes.smtp2go.bouncing = {
            p.email.lower()
            for h in list(self.world.homes.values())[::15]
            for p in h.people
            if p.email
        }
        self.log(
            f"cast: {len(self.world.homes)} homes, {len(self.world.board)} board, "
            f"{len(self.fakes.smtp2go.bouncing)} bouncing address(es)"
        )

    def events_for(self, day: datetime.date, rng) -> list[Event]:
        from regression.simulation.scenario import money, people, setup

        self.world.refresh()
        out: list[Event] = []
        for area in (setup, money, people):
            out.extend(area.plan(self, day, rng))
        return out

    def end_of_day(self, day: datetime.date) -> None:
        from regression.simulation.clock import UTC

        self.fakes.smtp2go.prune(
            datetime.datetime(day.year, day.month, day.day, tzinfo=UTC) - datetime.timedelta(days=1)
        )

    def end_of_month(self, day: datetime.date) -> None:
        pass

    def year_end_figures(self) -> dict:
        from regression.simulation.scenario import money

        try:
            with system_access("simulation year end"):
                return money.year_end_figures(self)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


__all__ = ["SYSTEM", "Scenario"]
