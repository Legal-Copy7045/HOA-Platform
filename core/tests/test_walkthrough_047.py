"""0.47.0 walkthrough fix: System status tiles fit a phone (WP-001)."""

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2] / "static/css/site.css").read_text()


def _phone_block():
    # The phone rules that keep amounts on one line also let sentence values wrap.
    start = CSS.index("Amounts never break mid-number")
    return CSS[start : CSS.index("}\n", CSS.index(".grid > .stat", start)) + 2]


def test_small_tile_values_wrap_on_a_phone():
    block = _phone_block()
    assert re.search(r"\.stat \.value\.small-value[^{]*\{[^}]*white-space: normal", block)


def test_a_tile_never_widens_the_page_on_a_phone():
    block = _phone_block()
    assert re.search(r"\.grid > \.stat \{[^}]*min-width: 0", block)
