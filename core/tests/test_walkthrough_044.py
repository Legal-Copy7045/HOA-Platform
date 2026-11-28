"""Walkthrough findings of 0.44.0 that only the stylesheet fixes."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_row_links_in_lists_are_full_tap_targets_on_phones_only():
    """Records retention: "Bank statements 2018 (demo)" (a.grow in a list row)
    was 17px tall on a phone; at least 40px there, desktop unchanged."""
    css = (ROOT / "static/css/site.css").read_text()
    rule = re.search(r"@media \(max-width: 640px\) \{ \.list > li > a\.grow \{([^}]*)\} \}", css)
    assert rule, "phone rule for list row links"
    height = re.search(r"min-height:\s*(\d+)px", rule.group(1))
    assert height and int(height.group(1)) >= 40
    assert "display: flex" in rule.group(1)
    # Not outside the phone media query.
    assert not re.search(r"^\.list > li > a\.grow", css, re.M)
    template = (ROOT / "templates/documents/records_retention.html").read_text()
    assert '<ul class="list">{% for item in req.items.all %}<li><a class="grow"' in template
