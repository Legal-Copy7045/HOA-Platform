"""Typed rule values <-> stored rule values, shared by Community settings and
the community setup wizard (D51) so both accept and refuse the same input.

Money rules are typed in dollars ("75.00") and stored in cents ("7500");
basis-point rules (the card surcharge, D50) are typed as a percent ("2.5")
and stored as whole basis points ("250"); whole-percent rules accept "51",
"51%" or "51.0" and store "51"; everything else is stored as typed.
``community.rules.validate_value`` (run again by ``services.set_rule``)
does the rest of the checking.
"""

from __future__ import annotations

import re

from community.rules import (
    BASIS_POINTS,
    BOOL,
    CENTS,
    DAYS,
    INT,
    PERCENT,
    TEXT,
    RuleSpec,
    bp_display,
    percent_to_bp,
)

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
    if spec.kind == BASIS_POINTS and typed:
        # The cap is checked on the stored number (validate_value, spec.maximum).
        return str(percent_to_bp(typed))
    if spec.kind == PERCENT and typed:
        # The unit is shown after the box; typing it too is fine. "51.0" is
        # 51, but "50.5" stays as typed so validate_value refuses it.
        bare = typed.rstrip("%").strip()
        m = re.fullmatch(r"([0-9]+)(?:\.0*)?", bare)
        return m.group(1) if m else bare
    return typed


def to_typed(spec: RuleSpec, stored: str | None) -> str:
    """What a form shows for a stored value (money back in dollars)."""
    stored = stored or ""
    if spec.kind == CENTS and stored.lstrip("-").isdigit():
        cents = int(stored)
        sign = "-" if cents < 0 else ""
        return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"
    if spec.kind == BASIS_POINTS and stored.isdigit():
        return bp_display(stored)
    return stored


# What a wizard or settings box suggests (a greyed placeholder) when nothing
# applies by default: a realistic example of the kind of value, never data.
KIND_EXAMPLES = {
    CENTS: "e.g. 75.00",
    PERCENT: "e.g. 51",
    BASIS_POINTS: "e.g. 2.5",
    DAYS: "e.g. 30",
    INT: "e.g. 3",
    TEXT: "e.g. Assessment",
}
_EXAMPLE_PATTERNS = {
    CENTS: r"\$([0-9][0-9,]*(?:\.[0-9]{2})?)",
    PERCENT: r"([0-9]{1,3})\s*%",
    BASIS_POINTS: r"([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%",
    DAYS: r"([0-9]{1,4})\s*(?:business |calendar )?days?",
}
# Citation examples by where a value usually comes from (community/pa_law.py).
CITATION_EXAMPLES = {
    "Declaration": "e.g. Declaration §6.3",
    "Bylaws": "e.g. Bylaws §2.7",
    "Rules and Regulations": "e.g. Rules and Regulations §4.2",
    "Board resolution": "e.g. Board resolution 2026-04",
}
# What the demo data cites instead of a real document (seed_demo_operations):
# a form shows it as an empty box with an example, never as a value to keep.
DEMO_CITATION = "DEMO - replace"


def example_value(spec: RuleSpec) -> str:
    """A typed example for a rule's box ("e.g. 30"), taken from the
    Pennsylvania guide's worked example where it has one; "" for yes/no."""
    from community.pa_law import guide

    if spec.kind == BOOL:
        return ""
    g = guide(spec.key)
    pattern = _EXAMPLE_PATTERNS.get(spec.kind)
    if g and pattern:
        m = re.search(pattern, g.example)
        if m:
            found = m.group(1).replace(",", "")
            if spec.kind == CENTS and "." not in found:
                found += ".00"
            return f"e.g. {found}"
    if spec.kind == TEXT and spec.default:
        return f"e.g. {spec.default}"
    return KIND_EXAMPLES.get(spec.kind, "")


def citation_example(spec: RuleSpec) -> str:
    """A citation example matching where the value usually comes from."""
    from community.pa_law import guide

    g = guide(spec.key)
    if g and g.source in CITATION_EXAMPLES:
        return CITATION_EXAMPLES[g.source]
    if g and g.source:
        return f"e.g. {g.source}"
    return CITATION_EXAMPLES["Declaration"]


def is_demo_citation(text: str | None) -> bool:
    """True for the demo data's placeholder citation."""
    return (text or "").strip().lower().startswith(DEMO_CITATION.lower())
