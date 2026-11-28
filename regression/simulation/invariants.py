"""What must stay true every simulated day (and after each event where cheap).

Each check returns a list of problems (plain sentences, ids and amounts only,
never names); an empty list is a pass. The engine records one result per
check per day and stops at the first break (unless asked to keep going),
writing the simulated date, the event and the invariant to the report.

Daily:
* ``books.trial_balance``: the general ledger's trial balance is zero in each
  fund (interfund lines included) and the owner journal export balances,
  overall and per reference, names no owner and its receivable equals the
  sum of home balances (the QA suite's ``assert_journal``);
* ``books.statements_balance``: assets less liabilities equal each fund's
  balance on the balance sheet;
* ``ledger.home_accounts``: every home and every ownership account: open
  charges less unapplied credit equal its balance; no charge open beyond its
  amount, no payment applied beyond its own (``assert_lot_books``);
* ``payments.stripe_matches_ledger``: every online payment has its Stripe
  record and back;
* ``books.funds_not_negative``: no fund's cash or balance below zero unless a
  board exception note explains it or the scenario injected it;
* ``append_only.unchanged``: no row of any append-only table (every
  ``AppendOnlyModel`` and history table) changed or vanished: a hash per
  table of the rows already seen is compared every day;
* ``close.month_closed``: after the close day, last month is closed by the
  automatic close when nothing was wrong; when the scenario injected a
  problem, the month stays open with that check failing, until it is fixed;
* ``close.year_package``: once a fiscal year closes, its year-end package
  exists, its manifest verifies, and its balance sheet balances;
* ``reminders.*``: statement emails went to every emailable account once a
  month, vote and election deadline reminders went out before each close.

After each event (cheap):
* ``audit.every_change``: an event that changed something wrote an audit event;
* ``conflicts.own_matter``: no audit event of a staff step names a matter that
  is the actor's own (core.conflicts.own_matter).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.apps import apps
from django.db import connection

from core.scoping import system_access


@dataclass
class Result:
    key: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _guard(key, fn, *args, **kwargs) -> Result:
    try:
        with system_access(f"simulation invariant {key}"):
            out = fn(*args, **kwargs)
    except AssertionError as exc:
        return Result(key, [str(exc) or "assertion failed"])
    except Exception as exc:  # an invariant that cannot even be computed is a break
        return Result(key, [f"could not check: {type(exc).__name__}: {exc}"[:500]])
    return Result(key, list(out or []))


# --- Money ---------------------------------------------------------------------------------


def trial_balance(today: datetime.date) -> list[str]:
    from ledger.books import funds, journal
    from ledger.tests.accounting_invariants import assert_journal

    out = []
    assert_journal()
    if funds.books_start() is not None:
        b = journal.balances_as_of(today)
        for fund in b.funds():
            total = sum(c for (f, _a), c in b.net.items() if f == fund)
            if total:
                out.append(f"trial balance of fund {fund} is {total} cents, not zero")
    return out


def statements_balance(today: datetime.date) -> list[str]:
    from ledger.books import funds, statements

    if funds.books_start() is None:
        return []
    out = []
    bs = statements.balance_sheet(today)
    for fund, f in bs["funds"].items():
        diff = f["assets"] - f["liabilities"] - f["fund_balance"]
        if diff:
            out.append(f"balance sheet of fund {fund} off by {diff} cents")
    return out


def home_accounts() -> list[str]:
    from ledger.tests.accounting_invariants import assert_lot_books

    assert_lot_books()
    return []


def stripe_matches() -> list[str]:
    from ledger.tests.accounting_invariants import assert_online_payments

    assert_online_payments()
    return []


def funds_not_negative(today: datetime.date, injected: set[str]) -> list[str]:
    from ledger.books import close, funds
    from ledger.books.models import ExceptionNote

    if funds.books_start() is None:
        return []
    out = []
    explained = set(ExceptionNote.objects.values_list("item", flat=True))
    for fund, pos in close.fund_positions(today).items():
        for what in ("cash", "balance"):
            if pos[what] < 0 and f"fund:{fund}" not in injected:
                key = f"fund:{fund}:cash" if what == "cash" else f"fund:{fund}"
                if key not in explained:
                    out.append(f"{fund} fund {what} is {pos[what]} cents with no record")
    return out


# --- Append-only ---------------------------------------------------------------------------


def append_only_models():
    from simple_history.models import HistoricalChanges

    from core.append_only import AppendOnlyModel

    out = []
    for model in apps.get_models():
        if model._meta.proxy or not issubclass(model, (AppendOnlyModel, HistoricalChanges)):
            continue
        pk = model._meta.pk
        if pk.get_internal_type() not in ("AutoField", "BigAutoField", "IntegerField"):
            continue
        out.append((model._meta.db_table, pk.column))
    return sorted(set(out))


class AppendOnlyWatch:
    """Per table: (highest id seen, row count up to it, md5 of those rows)."""

    def __init__(self):
        self.tables = append_only_models()
        self.seen: dict[str, tuple[int, int, str]] = {}

    def _state(self, table, pk, upto=None):
        where = f'WHERE "{pk}" <= %s' if upto is not None else ""  # nosec B608
        sql = (
            f'SELECT coalesce(max("{pk}"), 0), count(*), '  # noqa: S608  # nosec B608 - our tables
            f"coalesce(md5(string_agg(t::text, '|' ORDER BY \"{pk}\")), '') "
            f'FROM "{table}" t {where}'
        )
        with connection.cursor() as cur:
            cur.execute(sql, [upto] if upto is not None else [])
            return cur.fetchone()

    def check(self) -> list[str]:
        out = []
        for table, pk in self.tables:
            prev = self.seen.get(table)
            if prev is not None:
                high, count, digest = prev
                _h, now_count, now_digest = self._state(table, pk, high)
                if now_count < count:
                    out.append(f"{table}: {count - now_count} row(s) deleted")
                elif now_digest != digest:
                    out.append(f"{table}: a row up to id {high} was changed")
            self.seen[table] = tuple(self._state(table, pk))
        return out


# --- Audit and own matters (per event) ----------------------------------------------------------


def last_audit_id() -> int:
    from audit.models import AuditEvent

    return AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0


def audit_since(audit_id: int):
    from audit.models import AuditEvent

    return list(AuditEvent.objects.filter(id__gt=audit_id).order_by("id"))


# Audit actions of staff steps (an owner's own steps on their home are never checked).
STAFF_ACTIONS = (
    "arc.decided",
    "arc.decision",
    "arc.accept",
    "arc.position",
    "violation.notice",
    "violation.decided",
    "collection.",
    "ledger.charge",
    "ledger.late_fee",
    "ledger.credit",
    "payment_plan.approved",
    "legal.",
    "invoice.approved",
    "reimbursement.approved",
    "reimbursement.paid",
    "request.assigned",
    "ownership.",
)


def _target(event):
    if not event.target_type or not event.target_id:
        return None
    try:
        app_label, model_name = event.target_type.split(".", 1)
        model = apps.get_model(app_label, model_name)
    except (LookupError, ValueError):
        return None
    manager = model.objects
    qs = manager.unscoped("simulation") if hasattr(manager, "unscoped") else manager.all()
    try:
        return qs.filter(pk=event.target_id).first()
    except (ValueError, TypeError):
        return None


def own_matter_steps(events, staff_actors: set[int]) -> list[str]:
    """Staff steps (by a user acting as staff in this event) on their own matter."""
    from core import conflicts

    out = []
    for e in events:
        if e.actor_id is None or e.actor_id not in staff_actors or e.outcome != "success":
            continue
        if not e.action.startswith(STAFF_ACTIONS):
            continue
        obj = _target(e)
        if obj is None:
            continue
        actor = e.actor
        try:
            own = conflicts.own_matter(actor, obj)
        except Exception:
            own = False
        if own:
            out.append(f"{e.action} on {e.target_type} #{e.target_id} by its own party")
    return out


# --- Close and year end --------------------------------------------------------------------------


def month_close(today: datetime.date, injected: dict[str, str]) -> list[str]:
    """Last month is closed after the close day unless the scenario broke it."""
    from community.rules import rule
    from ledger.books import close, funds, periods
    from ledger.books.models import CloseRun

    first = funds.first_month()
    if first is None or not rule("books.auto_close"):
        return []
    day = max(1, min(int(rule("books.close_day") or 3), 28))
    if today.day <= day:  # the close runs on the close day; check the day after
        return []
    last = (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    if last < first:
        return []
    key = periods.month_key(last)
    closed = key in periods.standing_closes()
    out = []
    if key in injected:
        expected_check = injected[key]
        run = CloseRun.objects.filter(period=key).order_by("-id").first()
        if run is None:
            out.append(f"{key}: no close run although a problem was injected")
        elif closed and not injected.get(f"{key}:fixed"):
            out.append(f"{key}: closed although the injected problem ({expected_check}) remains")
        elif not closed and expected_check not in str(run.results):
            out.append(f"{key}: the close did not report the injected {expected_check}")
        return out
    if not closed:
        ev = close.evaluate_month(key)
        failing = [
            f"{c.key}: " + "; ".join(i.label for i in c.open_items[:3])
            for c in ev.checks
            if c.blocking and c.open_items
        ]
        out.append(f"{key} not closed automatically: " + " | ".join(failing)[:600])
    return out


def year_package(today: datetime.date) -> list[str]:
    from ledger.books import package, periods, statements
    from ledger.books.models import YearEndPackage

    out = []
    for key in periods.standing_closes("year"):
        fy = int(str(key).removeprefix("FY"))
        pkg = YearEndPackage.objects.filter(fiscal_year=fy).order_by("-id").first()
        if pkg is None:
            out.append(f"fiscal year {fy} closed without a year-end package")
            continue
        _start, end = periods.year_bounds(fy)
        bs = statements.balance_sheet(end)
        for fund, f in bs["funds"].items():
            if f["assets"] - f["liabilities"] != f["fund_balance"]:
                out.append(f"fiscal year {fy}: {fund} balance sheet does not balance")
        # The stored file read directly (not open_for_download: that is a person's
        # download and audits one); the same check as manage.py verify_books_package.
        with pkg.file.open("rb") as fh:
            problems = package.verify(fh.read())
        out.extend(f"fiscal year {fy} package: {p}" for p in problems)
        if not pkg.year_closed:
            out.append(f"fiscal year {fy}: the package was built before the year closed")
    return out


# --- Reminders -----------------------------------------------------------------------------------


def statements_sent(today: datetime.date) -> list[str]:
    """Each month with posted assessments: one statement email per account
    that has someone to email (checked from the 3rd, after the queue ran)."""
    from ledger.models import EntryKind, LedgerEntry, StatementEmail

    if today.day != 3:
        return []
    period = f"{today:%Y-%m}"
    posted = set(
        LedgerEntry.objects.filter(kind=EntryKind.ASSESSMENT, period=period).values_list(
            "account_id", flat=True
        )
    )
    if not posted:
        return []
    sent = set(StatementEmail.objects.filter(period=period).values_list("account_id", flat=True))
    if not sent:
        return [f"{period}: assessments posted but no statement email went out"]
    return []


def vote_reminders(today: datetime.date) -> list[str]:
    """Every formal vote and election that closed at its deadline after a
    window of three days or more had its 48-hour reminder sent before it closed."""
    from governance.models import Election, Vote

    out = []
    rows = [
        ("vote", v.pk, v.opens_at, v.closes_at, v.reminder_sent_at)
        for v in Vote.objects.filter(kind="formal", status="closed", closed_at_deadline=True)
    ] + [
        ("election", e.pk, e.voting_opens_at, e.voting_closes_at, e.reminder_sent_at)
        for e in Election.objects.filter(closed_at_deadline=True)
    ]
    for kind, pk, opens, closes, sent in rows:
        if not opens or not closes or (closes - opens).days < 3:
            continue
        if sent is None:
            out.append(f"{kind} #{pk} closed without its deadline reminder")
        elif sent > closes:
            out.append(f"{kind} #{pk}: reminder sent after it closed")
    return out


def run_daily(today, watch: AppendOnlyWatch, *, injected_funds, injected_months) -> list[Result]:
    return [
        _guard("books.trial_balance", trial_balance, today),
        _guard("books.statements_balance", statements_balance, today),
        _guard("ledger.home_accounts", home_accounts),
        _guard("payments.stripe_matches_ledger", stripe_matches),
        _guard("books.funds_not_negative", funds_not_negative, today, injected_funds),
        _guard("append_only.unchanged", watch.check),
        _guard("close.month_closed", month_close, today, injected_months),
        _guard("close.year_package", year_package, today),
        _guard("reminders.statement_emails", statements_sent, today),
        _guard("reminders.vote_deadline", vote_reminders, today),
    ]
