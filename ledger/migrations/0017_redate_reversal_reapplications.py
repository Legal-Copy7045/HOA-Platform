"""Correct the dates of money re-applied when a reversal freed it (BUG-005).

Migration 0012 dated every existing application row by the later of its two
entries. Rows made in the step of a reversal, which applied the money it
freed to other charges, could so be dated before the reversal, and a view as
of a date between the two counted a payment twice. This adds, for each such
row, a row undoing it from its old date and a row applying it again from the
reversal's date (append-only; 0012 is not changed, no total changes). See
``ledger.allocation.redate_reversal_reapplications``.
"""

from django.db import migrations


def redate(apps, schema_editor):
    from ledger.allocation import redate_reversal_reapplications

    redate_reversal_reapplications(
        apps.get_model("ledger", "LedgerEntry"), apps.get_model("ledger", "PaymentApplication")
    )


class Migration(migrations.Migration):
    dependencies = [("ledger", "0016_entry_moves")]

    operations = [migrations.RunPython(redate, migrations.RunPython.noop)]
