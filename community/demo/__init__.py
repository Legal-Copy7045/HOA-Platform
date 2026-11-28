"""Demo datasets for ``manage.py seed_demo --dataset NAME`` (decision D53).

Everything here is invented. People are named "... (demo)" (the marker
``demo_make_me_owner`` and ``keep_logins`` rely on) and their addresses use
the reserved example.com domain, which can never deliver.

Adding a dataset: write a module in this package with a ``seed()`` function
returning a :class:`SeedResult`, and add a :class:`Dataset` line to
``DATASETS``. Names are lower-case letters and ``_`` only: the VM agent passes
the name to ``seed_demo`` on a command line (after checking it against this
registry) when a sysadmin reseeds the POC from Settings -> Demo data.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field

DEMO_SUFFIX = "(demo)"
# Reserved for documentation (RFC 2606): never a real mailbox.
DEMO_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
NAME_PATTERN = re.compile(r"^[a-z_]{1,40}$")


class DemoError(Exception):
    """A demo-data step refused (shown to the person as is)."""


@dataclass(frozen=True)
class Dataset:
    name: str
    label: str
    description: str
    module: str  # dotted path of the module with seed()
    # False for a dataset without homes (blank): the reseed cannot make the
    # requester a board member and home owner there.
    has_homes: bool = True


@dataclass
class SeedResult:
    summary: str
    # (email, who) of the seeded logins and of the people worth inviting first.
    logins: list[tuple[str, str]] = field(default_factory=list)
    invite_first: list[tuple[str, str]] = field(default_factory=list)


DATASETS = [
    Dataset(
        "oak_creek",
        "Oak Creek Farms (full demo)",
        "Everything filled in: 44 homes with three years of history: owners, a board of "
        "five, ARC reviewers, "
        "dues and payments, ARC requests, violations, meetings, votes, surveys, vendors, "
        "invoices, documents and more.",
        "community.demo.oak_creek",
    ),
    Dataset(
        "fresh_start",
        "Fresh start (just handed over)",
        "Not empty: 44 homes with their owners, a declarant-appointed board of three, a "
        "sysadmin and a placeholder Declaration, as when the declarant has just handed "
        "over. No history, settings left at their defaults, no invitations sent yet.",
        "community.demo.fresh_start",
    ),
    Dataset(
        "blank",
        "Blank (empty portal)",
        "Nothing at all: no homes, no owners, no board, no documents, no history; only the "
        "real logins this portal keeps (yours and the other non-demo logins). Settings at "
        "their defaults, to start with the setup wizard and the register import (tick the "
        "box to get a board seat for them).",
        "community.demo.blank",
        has_homes=False,
    ),
]
DEFAULT = "oak_creek"
BY_NAME = {d.name: d for d in DATASETS}


def names() -> list[str]:
    return [d.name for d in DATASETS]


def get(name: str) -> Dataset:
    """The dataset called ``name``; KeyError for anything not in the registry."""
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise KeyError(name)
    return BY_NAME[name]


def seed(name: str) -> SeedResult:
    return importlib.import_module(get(name).module).seed()


def is_demo_person(person) -> bool:
    return bool(person) and person.display_name.endswith(DEMO_SUFFIX)


def is_demo_email(email: str) -> bool:
    domain = (email or "").rpartition("@")[2].lower()
    return domain in DEMO_EMAIL_DOMAINS


# --- Fictional names and addresses, shared by every dataset ----------------------------

FIRST = [
    "Avery",
    "Blake",
    "Casey",
    "Dana",
    "Emery",
    "Finley",
    "Gray",
    "Harper",
    "Indy",
    "Jordan",
    "Kai",
    "Logan",
    "Morgan",
    "Noel",
    "Oakley",
    "Parker",
    "Quinn",
    "Reese",
    "Sage",
    "Taylor",
    "Uma",
    "Val",
    "Wren",
    "Yael",
    "Zion",
]
LAST = [
    "Applewood",
    "Birchfield",
    "Cobblestone",
    "Dovetail",
    "Elmsworth",
    "Foxglove",
    "Gristmill",
    "Hollyhock",
    "Ironwood",
    "Juniper",
    "Kestrel",
    "Larkspur",
]


def demo_name(i: int) -> str:
    return f"{FIRST[i % len(FIRST)]} {LAST[(i * 7) % len(LAST)]} {DEMO_SUFFIX}"


def address(number: int, streets: tuple[str, str]) -> str:
    """Two fictional roads of 22 homes each (lots 1-22 even, 23-44 odd)."""
    if number <= 22:
        return f"{100 + (number - 1) * 2} {streets[0]}"
    return f"{151 + (number - 23) * 2} {streets[1]}"
