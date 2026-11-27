"""Community setup wizard (decision D51): the steps, which governing rules
each step shows, and the progress of each run.

The rule steps are driven by data: every ``RuleSpec`` in
``community.rules.REGISTRY`` lands in the first step that names its group
(and, where a step lists key prefixes, whose prefix the key starts with). A
step that names a group without prefixes takes the rest of that group, and
the step marked ``catch_all`` takes rules of any group no step names. So a
rule added later appears in its group's step without editing this file.

Saving never happens here: the views call the same services as Community
settings, Letters and certificates and Features (``set_rule``,
``update_profile``, ``update_letter_settings``, ``features.set_feature``),
which check permission and audit. This module records only progress
(``SetupStepRecord``, append-only) and audits it with the step key and run
number.

Who: board members (``community.manage_rules``). The manager never; a
sysadmin has no access (no owner data by virtue of the role: the server
side is on System status and Server tasks).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction

from core import policy

PERMISSION = "community.manage_rules"

PROFILE, BRANDING, RULES, FEATURES, PEOPLE, SERVER, REVIEW = (
    "profile",
    "branding",
    "rules",
    "features",
    "people",
    "server",
    "review",
)


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    intro: str
    kind: str = RULES
    groups: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    catch_all: bool = False
    features: tuple[str, ...] = ()
    # (label, url name, permission) of related pages; shown when allowed.
    links: tuple[tuple[str, str, str], ...] = ()


STEPS: list[Step] = [
    Step(
        "community",
        "Community details",
        "The association's legal name and the lines printed on letters and statements: "
        "the letterhead, where owners mail checks, how they reach you, and the signature.",
        kind=PROFILE,
    ),
    Step(
        "branding",
        "Branding",
        "The picture behind the sign-in page. It is uploaded on the Branding page; this "
        "step only shows whether one is set.",
        kind=BRANDING,
        links=(("Open Branding", "branding_settings", "branding.manage"),),
    ),
    Step(
        "charge",
        "The monthly charge",
        "What your covenants call the regular monthly charge, how much it is, the day it is "
        "due, and whether the portal posts it on the 1st by itself.",
        groups=("Dues",),
        prefixes=("charge.", "dues."),
    ),
    Step(
        "late_fees",
        "Late fees and collections",
        "When a payment is late, what the late fee is, whether fees post by themselves, "
        "and the notice periods of the collection letters. Also the invoice approval limit.",
        groups=("Dues", "Invoices"),
        links=(("Letters and certificates", "letters_settings", "letters.manage"),),
    ),
    Step(
        "surplus",
        "Surplus and refunds",
        "What happens to a year's surplus, as your Declaration and Bylaws say: whether it is "
        "split by what each owner paid or equally per home, whether the credit stays with the "
        "home or goes with the owner who paid (a seller), and whether the board may pay it by "
        "check instead of a credit against future charges. Also the amount at which a "
        "disbursement needs a second board approval.",
        groups=("Disbursements",),
        features=("disbursements",),
    ),
    Step(
        "books",
        "Books and close",
        "The association's own books: whether months and years close by themselves when "
        "everything balances, the close day, the deposit and check limits, the monthly "
        "reserve contribution and how bank lines are coded. Bank accounts, their funds and "
        "the categories are set on the linked pages.",
        groups=("Books and close",),
        features=("books", "financial_statements"),
        links=(
            ("Bank accounts and funds", "books_accounts", "books.setup"),
            ("Bank categories and rules", "books_coding", "books.view"),
            ("Month and year close", "books_close", "books.view"),
        ),
    ),
    Step(
        "payments",
        "Online payments and automatic payments",
        "Whether owners can pay online (Stripe's hosted page) and set up automatic "
        "payments, the credit card surcharge, and the amount above which an automatic "
        "payment waits for the board's review. Keys are never typed here: a sysadmin "
        "puts them on the server.",
        groups=("Online payments",),
        features=("online_payments", "autopay", "bank_feeds"),
    ),
    Step(
        "notices",
        "Statements and notices",
        "How official notices reach owners, the monthly statement email, the weekly "
        "summary and text alerts.",
        groups=("Notices",),
        features=("sms", "push"),
    ),
    Step(
        "arc",
        "Architectural review",
        "Review deadlines from the Declaration, deemed approval, appeals and neighbor "
        "acknowledgements. Required submittals are set per kind of project.",
        groups=("Architectural review",),
        links=(
            ("ARC request types (required submittals)", "arc_project_types", "arc.configure"),
            ("Home neighbors", "arc_neighbors", "arc.configure"),
        ),
    ),
    Step(
        "violations",
        "Violations",
        "The time owners get to fix a violation and the notice before a hearing.",
        groups=("Violations",),
        features=("violations",),
    ),
    Step(
        "common_areas",
        "Common areas and bookings",
        "The community's common areas, and whether owners can book them.",
        kind=RULES,
        groups=("Common areas",),
        features=("reservations",),
        links=(("Common area booking rules", "reservation_settings", "reservation.configure"),),
    ),
    Step(
        "requests",
        "Owner requests",
        "How quickly the board aims to answer each kind of owner request, and how long new "
        "owners, contacts and tenants see their getting-started checklist (D73).",
        groups=("Owner requests",),
        features=("owner_help",),
    ),
    Step(
        "governance",
        "Meetings, voting, records and resale",
        "Meeting notice; for formal votes and elections the quorum, the passing share, the "
        "shortest voting period, whether the board may close voting early and whether owners "
        "see the turnout while voting is open; records requests and resale certificates.",
        groups=("Meetings", "Voting", "Records", "Resale"),
        # Catch-all only as a safety net: every group in community/rules.py
        # must be named by a step (a guard test fails otherwise).
        catch_all=True,
        features=("votes", "elections", "surveys"),
    ),
    Step(
        "privacy",
        "Privacy and retention",
        "How long contact details of people who have left are kept.",
        groups=("Privacy",),
        links=(("Contact retention", "retention", "retention.manage"),),
    ),
    Step(
        "records",
        "Renewals and records retention",
        "When the board is reminded of insurance renewals, vendors' insurance certificates "
        "and contract notice deadlines, and how long each kind of document is kept before "
        "two board members may dispose of it (D72).",
        groups=("Renewals", "Document retention"),
        features=("renewals", "records_retention"),
        links=(
            ("Renewals", "renewals", "renewals.view"),
            ("Records retention", "records_retention", "records_retention.manage"),
        ),
    ),
    Step(
        "signin",
        # Not "Sign-in and security": that is each person's own page (WT-005).
        "Account recovery rules",
        "How long a self-service account recovery waits before the person can finish it, "
        "and how long votes, proxies, contact details and automatic payments stay locked "
        "afterwards. Staff roles always use the two-board-member reset.",
        groups=("Account recovery",),
    ),
    Step(
        "monitoring",
        "Server warnings",
        "How early the sysadmins are warned that the server's disk is filling up (D74). "
        "Everything else about the server is on the Server and email step.",
        groups=("Server warnings",),
    ),
    Step(
        "features",
        "Features",
        "Every optional part of the portal, on or off. A switched-off feature leaves the "
        "menu; its records are kept.",
        kind=FEATURES,
    ),
    Step(
        "people",
        "People and access",
        "Who holds board seats and offices, whether every action can be done by two people "
        "with two-step sign-in, sysadmins and invitations. Changes are made on the linked "
        "pages.",
        kind=PEOPLE,
    ),
    Step(
        "server",
        "Server and email",
        "Outgoing email, the bounce mailbox, backups, the off-site copy, the payment and bank "
        "keys and the USPS tracking login. These "
        "are done on the server by a sysadmin; this step shows where things stand.",
        kind=SERVER,
    ),
    Step(
        "review",
        "Review",
        "Every decision on one page. Anything still at its default or missing a citation "
        "is flagged; Change takes you back to its step.",
        kind=REVIEW,
    ),
]
BY_KEY = {s.key: s for s in STEPS}
KEYS = [s.key for s in STEPS]
# Steps a run must complete (or skip) before Finish; Review is the finish.
WORK_STEPS = [k for k in KEYS if k != "review"]


def number(step_key: str) -> int:
    return KEYS.index(step_key) + 1


def neighbours(step_key: str) -> tuple[str | None, str | None]:
    i = KEYS.index(step_key)
    return (KEYS[i - 1] if i else None, KEYS[i + 1] if i + 1 < len(KEYS) else None)


# --- Which rules each step shows ---------------------------------------------------------


def assign_rules(registry=None) -> dict[str, list]:
    """Step key -> the RuleSpecs it shows, in the step's group order and then
    registry order. Every rule lands in exactly one step."""
    from community import rules

    registry = rules.REGISTRY if registry is None else registry
    catch = next(s for s in STEPS if s.catch_all)
    out: dict[str, list] = {s.key: [] for s in STEPS}
    for spec in registry.values():
        for step in STEPS:
            if spec.group in step.groups and (
                not step.prefixes or spec.key.startswith(step.prefixes)
            ):
                out[step.key].append(spec)
                break
        else:
            out[catch.key].append(spec)
    for step in STEPS:
        order = {g: i for i, g in enumerate(step.groups)}
        out[step.key].sort(key=lambda s: order.get(s.group, len(order)))
    return out


def unmapped_groups(registry=None) -> set[str]:
    """Rule groups no step names: they would fall into the catch-all step
    silently. Always empty in a correct build (guard test, D61)."""
    from community import rules

    registry = rules.REGISTRY if registry is None else registry
    named = {g for s in STEPS for g in s.groups}
    return {spec.group for spec in registry.values()} - named


def rules_for(step_key: str) -> list:
    return assign_rules()[step_key]


# --- Progress ---------------------------------------------------------------------------


def _records():
    from community.models import SetupStepRecord

    return SetupStepRecord.objects


def current_run() -> int | None:
    """The newest run's number, or None before setup was ever started."""
    from community.models import SetupStepRecord

    row = (
        _records()
        .filter(action=SetupStepRecord.Action.STARTED)
        .order_by("-run")
        .values_list("run", flat=True)
        .first()
    )
    return row


def finished(run: int | None) -> bool:
    from community.models import SetupStepRecord

    return (
        run is not None
        and _records().filter(run=run, action=SetupStepRecord.Action.FINISHED).exists()
    )


def last_finished():
    """The newest finished run's FINISHED record (its time marks "changed
    since"), or None."""
    from community.models import SetupStepRecord

    return _records().filter(action=SetupStepRecord.Action.FINISHED).order_by("-run").first()


def step_states(run: int | None) -> dict[str, str]:
    """Step key -> "completed" or "skipped" (the latest record wins)."""
    from community.models import SetupStepRecord

    if run is None:
        return {}
    out: dict[str, str] = {}
    for step, action in (
        _records()
        .filter(
            run=run,
            action__in=[SetupStepRecord.Action.COMPLETED, SetupStepRecord.Action.SKIPPED],
        )
        .order_by("at", "id")
        .values_list("step", "action")
    ):
        out[step] = action
    return out


def steps_left(run: int | None) -> int:
    """Work steps not completed in this run (skipped ones still count)."""
    states = step_states(run)
    return sum(1 for k in WORK_STEPS if states.get(k) != "completed")


def needs_finishing() -> bool:
    """True until a run is finished, and while a newer run is open."""
    return not finished(current_run())


def _audit(action: str, actor, request, metadata: dict) -> None:
    from audit.services import record_event

    record_event(
        action,
        actor=None if actor is policy.SYSTEM else actor,
        request=request,
        metadata=metadata,
    )


def _by(actor):
    return None if actor is policy.SYSTEM else actor


def start_run(*, actor, request=None) -> int:
    """Start run N+1 (the first run is 1). Two board members starting at once
    get one run between them."""
    from community.models import SetupStepRecord

    policy.require(actor, PERMISSION)
    run = (current_run() or 0) + 1
    try:
        with transaction.atomic():
            SetupStepRecord.objects.create(
                run=run, action=SetupStepRecord.Action.STARTED, by=_by(actor)
            )
            _audit("setup.run_started", actor, request, {"run": run})
    except IntegrityError:
        return current_run()
    return run


def ensure_run(*, actor, request=None) -> int:
    """The open run, starting the first one when there is none. A finished
    run stays finished: rerunning is an explicit "Run setup again"."""
    run = current_run()
    return run if run is not None else start_run(actor=actor, request=request)


def record_step(step_key: str, *, skipped: bool = False, actor, request=None) -> int:
    from community.models import SetupStepRecord

    policy.require(actor, PERMISSION)
    if step_key not in WORK_STEPS:
        raise ValueError("Unknown setup step.")
    run = ensure_run(actor=actor, request=request)
    action = SetupStepRecord.Action.SKIPPED if skipped else SetupStepRecord.Action.COMPLETED
    with transaction.atomic():
        SetupStepRecord.objects.create(run=run, step=step_key, action=action, by=_by(actor))
        _audit(
            "setup.step_skipped" if skipped else "setup.step_completed",
            actor,
            request,
            {"step": step_key, "run": run},
        )
    return run


def finish_run(*, actor, request=None) -> int:
    from community.models import SetupStepRecord

    policy.require(actor, PERMISSION)
    run = ensure_run(actor=actor, request=request)
    if finished(run):
        return run
    try:
        with transaction.atomic():
            SetupStepRecord.objects.create(
                run=run, action=SetupStepRecord.Action.FINISHED, by=_by(actor)
            )
            _audit("setup.run_finished", actor, request, {"run": run})
    except IntegrityError:
        pass  # finished by someone else a moment ago
    return run
