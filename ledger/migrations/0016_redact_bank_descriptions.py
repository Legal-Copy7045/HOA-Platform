"""Mask account and routing numbers already stored in bank-line descriptions
(SEC-004, decision D34-fixinfra-1).

The portal must never hold bank account numbers. Lines imported before the
redaction existed may carry them in the free-text description. Bank lines
are append-only, so the row trigger is lifted for this one UPDATE, run as the
owner role that applies migrations; only ``description`` changes, and only
where masking changes it. Nothing else on the row is touched.

The masking is frozen here (a copy of ``ledger.bankfeeds.base.redact_numbers``
as of this migration), so later changes to the importer never change what
this migration did. A test checks the two agree.
"""

import datetime
import re

from django.db import migrations

DESCRIPTION_MAX = 200
_DIGIT_RUN = re.compile(r"(?<!\d)\d(?:[ -]?\d){5,}(?!\d)")
_AMOUNT_TAIL = re.compile(r"[.,]\d{1,2}(?!\d)")
_DATE_SHAPES = ([6], [8], [4, 2, 2], [2, 2, 4], [2, 2, 2])
MASK = "••••"


def _ok(y, m, d):
    try:
        datetime.date(y, m, d)
    except ValueError:
        return False
    return True


def _plausible_date(digits):
    if len(digits) == 8:
        y1, y2 = int(digits[:4]), int(digits[4:])
        return (1900 <= y1 <= 2100 and _ok(y1, int(digits[4:6]), int(digits[6:]))) or (
            1900 <= y2 <= 2100 and _ok(y2, int(digits[:2]), int(digits[2:4]))
        )
    if len(digits) == 6:
        a, b, c = int(digits[:2]), int(digits[2:4]), int(digits[4:])
        return _ok(2000 + a, b, c) or _ok(2000 + c, a, b)
    return False


def redact_numbers(text, limit=DESCRIPTION_MAX):
    def replace(match):
        raw = match.group(0)
        digits = "".join(c for c in raw if c.isdigit())
        before = text[max(0, match.start() - 1) : match.start()]
        if raw.isdigit() and (before == "$" or _AMOUNT_TAIL.match(text, match.end())):
            return raw
        shape = [len(group) for group in re.split(r"[ -]", raw)]
        if shape in _DATE_SHAPES and _plausible_date(digits):
            return raw
        return MASK + digits[-4:]

    return _DIGIT_RUN.sub(replace, text or "")[:limit]


UPDATE = """
UPDATE ledger_banktransaction t SET description = v.description
  FROM unnest(%s::bigint[], %s::text[]) AS v(id, description)
  WHERE t.id = v.id
"""


def redact(apps, schema_editor):
    BankTransaction = apps.get_model("ledger", "BankTransaction")
    ids, texts = [], []
    for pk, description in BankTransaction.objects.values_list("id", "description").iterator():
        masked = redact_numbers(description)
        if masked != description:
            ids.append(pk)
            texts.append(masked)
    if not ids:
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(
            "ALTER TABLE ledger_banktransaction "
            "DISABLE TRIGGER ledger_banktransaction_append_only_row"
        )
        cursor.execute(UPDATE, [ids, texts])
        cursor.execute(
            "ALTER TABLE ledger_banktransaction "
            "ENABLE TRIGGER ledger_banktransaction_append_only_row"
        )


class Migration(migrations.Migration):
    dependencies = [("ledger", "0015_entry_accounts")]

    operations = [migrations.RunPython(redact, reverse_code=migrations.RunPython.noop)]
