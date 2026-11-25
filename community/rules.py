"""Registry of governing rules (CLAUDE.md rule 6).

Each rule's *value* and *citation* are data in community.GoverningRule, set
by the board from the Declaration, bylaws or a recorded board decision. Code
reads them through ``rule()``; nothing here hard-codes a community's numbers.
Defaults are used only when noted, and a rule without a citation is listed
as needing attention on the board dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CENTS, DAYS, INT, PERCENT, BOOL, TEXT = "cents", "days", "int", "percent", "bool", "text"


@dataclass(frozen=True)
class RuleSpec:
    key: str
    label: str
    kind: str
    default: str | None = None  # used until the board sets a value
    group: str = "General"
    help: str = ""


REGISTRY: dict[str, RuleSpec] = {
    spec.key: spec
    for spec in [
        RuleSpec(
            "dues.monthly_cents",
            "Monthly dues per lot",
            CENTS,
            None,
            "Dues",
            "Charged on the 1st of each month to every lot's account.",
        ),
        RuleSpec("dues.due_day", "Day of month dues are due", DAYS, "1", "Dues"),
        RuleSpec(
            "dues.auto_post",
            "Post monthly dues automatically on the 1st",
            BOOL,
            "false",
            "Dues",
            "Board decision. Otherwise the board posts each month with a button.",
        ),
        RuleSpec(
            "late_fee.cents",
            "Late fee (recorded only)",
            CENTS,
            None,
            "Dues",
            "Never applied automatically until reviewed against PA law.",
        ),
        RuleSpec(
            "late_fee.grace_days",
            "Days after the due date before a payment is late",
            DAYS,
            None,
            "Dues",
        ),
        RuleSpec(
            "invoice.dual_approval_cents",
            "Invoice amount needing two board approvals",
            CENTS,
            "50000",
            "Invoices",
            "Board decision: two approvals at $500 and over.",
        ),
        RuleSpec(
            "arc.completeness_days",
            "Days to check an ARC request is complete",
            DAYS,
            None,
            "Architectural review",
        ),
        RuleSpec(
            "arc.review_days",
            "Days to decide a complete ARC request",
            DAYS,
            None,
            "Architectural review",
        ),
        RuleSpec(
            "arc.deemed_approval",
            "Request is deemed approved if not decided in time",
            BOOL,
            None,
            "Architectural review",
        ),
        RuleSpec(
            "arc.appeal_days",
            "Days an applicant has to appeal an ARC decision",
            DAYS,
            None,
            "Architectural review",
        ),
        RuleSpec("violation.cure_days", "Days given to cure a violation", DAYS, None, "Violations"),
        RuleSpec(
            "violation.hearing_notice_days",
            "Notice before a violation hearing",
            DAYS,
            None,
            "Violations",
        ),
        RuleSpec(
            "meeting.owner_notice_days", "Notice to owners before a meeting", DAYS, None, "Meetings"
        ),
        RuleSpec(
            "records.response_days", "Days to respond to a records request", DAYS, None, "Records"
        ),
        RuleSpec("resale.fee_cents", "Resale certificate fee", CENTS, None, "Resale"),
        RuleSpec(
            "resale.deadline_days", "Days to provide a resale certificate", DAYS, None, "Resale"
        ),
        RuleSpec(
            "vote.quorum_percent", "Quorum for formal votes (% of lots)", PERCENT, None, "Voting"
        ),
        RuleSpec(
            "vote.pass_percent", "Share of votes cast needed to pass", PERCENT, None, "Voting"
        ),
    ]
}


def _parse(spec: RuleSpec, raw: str | None):
    if raw in (None, ""):
        return None
    if spec.kind in (CENTS, DAYS, INT, PERCENT):
        return int(raw)
    if spec.kind == BOOL:
        return raw.strip().lower() in ("1", "true", "yes")
    return raw


def validate_value(key: str, raw: str) -> str:
    spec = REGISTRY[key]
    raw = (raw or "").strip()
    if raw == "":
        return raw
    if spec.kind in (CENTS, DAYS, INT, PERCENT):
        # ASCII digits only and a sane size (review D9): str.isdigit() also
        # accepts characters like "²" that int() then refuses on every read.
        if not re.fullmatch(r"[0-9]{1,12}", raw):
            raise ValueError(f"{spec.label}: enter a whole number.")
        if spec.kind == PERCENT and int(raw) > 100:
            raise ValueError(f"{spec.label}: must be 0-100.")
    if spec.kind == BOOL and raw.lower() not in ("true", "false", "yes", "no", "1", "0"):
        raise ValueError(f"{spec.label}: enter yes or no.")
    return raw


def rule(key: str):
    """The rule's current value (parsed), the default, or None if unset."""
    from community.models import GoverningRule

    spec = REGISTRY[key]
    row = GoverningRule.objects.filter(key=key).first()
    raw = row.value if row and row.value != "" else spec.default
    return _parse(spec, raw)


def rule_with_citation(key: str) -> tuple[object, str]:
    from community.models import GoverningRule

    row = GoverningRule.objects.filter(key=key).first()
    return rule(key), (row.citation if row else "")


def rules_needing_attention() -> list[RuleSpec]:
    """Rules with no value, or a value but no citation."""
    from community.models import GoverningRule

    rows = {r.key: r for r in GoverningRule.objects.all()}
    missing = []
    for spec in REGISTRY.values():
        row = rows.get(spec.key)
        has_value = (row and row.value != "") or spec.default is not None
        if not has_value or not (row and row.citation):
            missing.append(spec)
    return missing


def cents_display(cents: int | None) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"
