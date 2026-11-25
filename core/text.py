"""Small text helpers for values that end up in headers, subjects or filenames."""

from __future__ import annotations

import unicodedata


def single_line(value: str | None) -> str:
    """One line of plain text: every control or line/paragraph separator
    becomes a space (review C6: a newline in a title broke email subjects)."""
    out = []
    for ch in value or "":
        category = unicodedata.category(ch)
        out.append(" " if category in ("Cc", "Cf", "Zl", "Zp") else ch)
    return " ".join("".join(out).split())
