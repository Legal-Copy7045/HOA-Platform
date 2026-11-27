"""Check numbers across every association check (D77).

The association has one check stock, so a number is used once whatever the
check pays: an owner refund or surplus share (``DisbursementCheck``, D52) or
a vendor invoice or expense reimbursement (``operations.VendorCheck``).
Each table keeps its own unique constraint; this module checks both, under
the same advisory lock the disbursement checks use, so two people recording
checks at once cannot take one number twice.
"""

from __future__ import annotations

import re

from django.db import connection

from core.scoping import system_access

CHECK_LOCK = (5_520_002, 0)  # the same as ledger.disbursements._CHECK_LOCK
_NUMBER = re.compile(r"^[0-9]{1,12}$")


def lock() -> None:
    """Take the check-number lock for this transaction."""
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(CHECK_LOCK))


def taken(numbers) -> set[str]:
    """The numbers among ``numbers`` already on any association check."""
    from ledger.models import DisbursementCheck
    from operations.models import VendorCheck

    numbers = [str(n) for n in numbers]
    with system_access("check numbers in use"):
        used = set(
            DisbursementCheck.objects.filter(number__in=numbers).values_list("number", flat=True)
        )
    used |= set(VendorCheck.objects.filter(number__in=numbers).values_list("number", flat=True))
    return used


def numbers_from(first: str, count: int) -> list[str]:
    """``count`` numbers from ``first`` up, keeping leading zeros."""
    first = (first or "").strip()
    if not _NUMBER.match(first):
        raise ValueError("A check number is digits only, e.g. 1042.")
    width = len(first)
    return [str(int(first) + i).zfill(width) for i in range(count)]


def suggest_next() -> str:
    """One more than the highest number recorded on any check ("" if none)."""
    from ledger.models import DisbursementCheck
    from operations.models import VendorCheck

    with system_access("next check number"):
        numbers = list(DisbursementCheck.objects.values_list("number", flat=True))
    numbers += list(VendorCheck.objects.values_list("number", flat=True))
    digits = [n for n in numbers if n.isdigit()]
    if not digits:
        return ""
    top = max(digits, key=int)
    return str(int(top) + 1).zfill(len(top))
