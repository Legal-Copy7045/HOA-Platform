"""Money is integer cents (CLAUDE.md rule 7). Parse and format without floats."""

from __future__ import annotations

import re

from django import forms

_PATTERN = re.compile(r"^-?\d{1,9}(\.\d{1,2})?$")


def parse_cents(text: str) -> int:
    raw = (text or "").strip().replace("$", "").replace(",", "")
    if not _PATTERN.match(raw):
        raise ValueError("Enter an amount like 125.00")
    negative = raw.startswith("-")
    raw = raw.lstrip("-")
    dollars, _, cents = raw.partition(".")
    value = int(dollars) * 100 + int((cents + "00")[:2])
    return -value if negative else value


def format_cents(cents: int | None) -> str:
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


class MoneyField(forms.CharField):
    """Form field: accepts '1,234.56' or '$1234.5'; cleans to integer cents."""

    def __init__(self, *args, allow_negative: bool = False, **kwargs):
        kwargs.setdefault("max_length", 16)
        self.allow_negative = allow_negative
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values:
            return None
        try:
            cents = parse_cents(value)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        if cents < 0 and not self.allow_negative:
            raise forms.ValidationError("Enter a positive amount.")
        return cents
