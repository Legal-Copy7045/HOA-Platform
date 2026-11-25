"""Give every existing ownership period an account (Milestone 3).

Per lot, ownerships are walked in start order: an ownership joins the
current account while that account is still open on its start date (or the
person was already on it); otherwise a new account starts. An account whose
owners have all ended is closed (dormant) on the latest end date.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Lot = apps.get_model("community", "Lot")
    Ownership = apps.get_model("community", "Ownership")
    Account = apps.get_model("community", "Account")
    for lot in Lot.objects.all().order_by("number"):
        rows = list(Ownership.objects.filter(lot=lot).order_by("start_date", "id"))
        seq, account, members, open_until, any_open = 0, None, set(), None, False
        for o in rows:
            joins = account is not None and (
                o.person_id in members or any_open or (open_until and o.start_date < open_until)
            )
            if not joins:
                if account is not None and not any_open:
                    Account.objects.filter(pk=account.pk).update(closed_on=open_until)
                seq += 1
                account = Account.objects.create(
                    lot=lot, number=f"{lot.number:03d}-{seq:02d}", opened_on=o.start_date
                )
                members, open_until, any_open = set(), None, False
            Ownership.objects.filter(pk=o.pk).update(account=account)
            members.add(o.person_id)
            if o.end_date is None:
                any_open = True
            elif open_until is None or o.end_date > open_until:
                open_until = o.end_date
        if account is not None and not any_open:
            Account.objects.filter(pk=account.pk).update(closed_on=open_until)


class Migration(migrations.Migration):
    dependencies = [("community", "0007_accounts")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
