"""Typed rule values <-> stored rule values, shared by Community settings and
the community setup wizard (D51) so both accept and refuse the same input.

Money rules are typed in dollars ("75.00") and stored in cents ("7500");
everything else is stored as typed. ``community.rules.validate_value`` (run
again by ``services.set_rule``) does the rest of the checking.
"""

from __future__ import annotations

from community.rules import CENTS, RuleSpec

MONEY_HINT = "Enter the amount in dollars, e.g. 75.00."


def to_stored(spec: RuleSpec, typed: str) -> str:
    """The value to store for what was typed; ValueError (a message people
    can read) when a money amount is not dollars and cents."""
    from core.money import parse_cents

    typed = (typed or "").strip()
    if spec.kind == CENTS and typed:
        try:
            return str(parse_cents(typed))
        except ValueError as exc:
            raise ValueError(MONEY_HINT) from exc
    return typed


def to_typed(spec: RuleSpec, stored: str | None) -> str:
    """What a form shows for a stored value (money back in dollars)."""
    stored = stored or ""
    if spec.kind == CENTS and stored.lstrip("-").isdigit():
        cents = int(stored)
        sign = "-" if cents < 0 else ""
        return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"
    return stored
