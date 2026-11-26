"""Dates are Eastern local dates (TIME_ZONE America/New_York, owner: "TZ needs to
be EST"). Taking .date() of a stored time gives the UTC date, which is
tomorrow after 7 or 8 pm Eastern; datetime.date.today() follows the server's
clock. Both are banned in product code: use timezone.localdate()."""

import re
from pathlib import Path

from django.conf import settings

BANNED = re.compile(r"\b\w+_at\.date\(\)|\bdate\.today\(\)")
SKIP = (".venv", ".claude", "/tests/", "/migrations/", "regression/")


def test_site_time_zone_is_eastern():
    assert settings.TIME_ZONE == "America/New_York" and settings.USE_TZ


def test_no_utc_or_server_dates_in_product_code():
    offenders = []
    for path in Path(".").rglob("*.py"):
        text = str(path)
        if any(s in "/" + text for s in SKIP):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if BANNED.search(line):
                offenders.append(f"{text}:{n}: {line.strip()}")
    assert not offenders, "Use timezone.localdate():\n" + "\n".join(offenders)
