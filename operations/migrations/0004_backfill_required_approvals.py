"""Re-audit of review A6: invoices already submitted before 0003 had no fixed
approval count, so the live threshold rule still applied to them. Fix it now
from the rule as it stands (default $500)."""

from django.db import migrations


def backfill(apps, schema_editor):
    Invoice = apps.get_model("operations", "Invoice")
    GoverningRule = apps.get_model("community", "GoverningRule")
    row = GoverningRule.objects.filter(key="invoice.dual_approval_cents").first()
    raw = (row.value if row else "") or "50000"
    threshold = int(raw) if raw.isdigit() else 50000
    for invoice in Invoice.objects.exclude(state="draft").filter(required_approvals__isnull=True):
        need = 2 if invoice.amount_cents >= threshold else 1
        Invoice.objects.filter(pk=invoice.pk).update(required_approvals=need)


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0003_invoice_frozen_approvals"),
        ("community", "0005_contact_profile"),  # pinned (a moving "latest" broke history)
    ]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
