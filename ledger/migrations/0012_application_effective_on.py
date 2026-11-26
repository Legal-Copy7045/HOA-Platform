"""When each payment application took effect (D33-acctfix-3).

Adds ``effective_on`` and fills it for existing rows from what the history
shows: an undo row (negative) took effect when the reversal of one of its
sides posted; any other row when the later of its two entries posted. The
append-only trigger is lifted only for this one backfill; no amounts change.
"""

from django.db import migrations, models

BACKFILL = """
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE ledger_paymentapplication
  DISABLE TRIGGER ledger_paymentapplication_append_only_row;
UPDATE ledger_paymentapplication a SET effective_on = d.effective_on
  FROM (
    SELECT x.id, CASE
        WHEN x.amount_cents < 0 THEN COALESCE(
          GREATEST(rs.posted_on, rc.posted_on), GREATEST(s.posted_on, c.posted_on))
        ELSE GREATEST(s.posted_on, c.posted_on)
      END AS effective_on
    FROM ledger_paymentapplication x
    JOIN ledger_ledgerentry s ON s.id = x.source_id
    JOIN ledger_ledgerentry c ON c.id = x.charge_id
    LEFT JOIN ledger_ledgerentry rs ON rs.reverses_id = s.id
    LEFT JOIN ledger_ledgerentry rc ON rc.reverses_id = c.id
  ) d
  WHERE d.id = a.id AND a.effective_on IS NULL;
ALTER TABLE ledger_paymentapplication
  ENABLE TRIGGER ledger_paymentapplication_append_only_row;
"""


class Migration(migrations.Migration):
    dependencies = [("ledger", "0011_merge_20261126_0047")]

    operations = [
        migrations.AddField(
            model_name="paymentapplication",
            name="effective_on",
            field=models.DateField(null=True),
        ),
        migrations.RunSQL(BACKFILL, reverse_sql=migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="paymentapplication",
            name="effective_on",
            field=models.DateField(),
        ),
    ]
