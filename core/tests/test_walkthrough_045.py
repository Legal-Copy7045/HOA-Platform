"""Walkthrough findings of 0.45.0 that only the stylesheet fixes."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/site.css").read_text()


def _phone_rules(selector: str) -> list[str]:
    pattern = r"@media \(max-width: 640px\) \{[^@]*?" + re.escape(selector) + r" \{([^}]*)\}"
    return re.findall(pattern, CSS)


def test_a_list_row_link_is_not_squeezed_narrow_on_phones():
    """WP-011: "Bank statements 2018 (demo)" (a.grow beside a long file name in a
    list row on Records retention) was 17px wide on a phone. There the row wraps
    and the link takes the whole first line, at least 44px tall and wide."""
    link = " ".join(_phone_rules(".list > li > a.grow"))
    assert "flex: 1 1 100%" in link
    assert int(re.search(r"min-height:\s*(\d+)px", link).group(1)) >= 44
    assert int(re.search(r"min-width:\s*(\d+)px", link).group(1)) >= 44
    row = " ".join(_phone_rules(".list > li:has(> a.grow)"))
    assert "flex-wrap: wrap" in row


def test_desktop_list_rows_are_unchanged():
    # No unconditional rule for these rows: desktop keeps one line per row.
    assert not re.search(r"^\.list > li > a\.grow", CSS, re.M)
    assert not re.search(r"^\.list > li:has\(> a\.grow\)", CSS, re.M)
    template = (ROOT / "templates/documents/records_retention.html").read_text()
    assert '<ul class="list">{% for item in req.items.all %}<li><a class="grow"' in template
