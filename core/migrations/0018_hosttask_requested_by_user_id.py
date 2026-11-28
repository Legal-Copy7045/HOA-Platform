# Review fixes 0.45.0 (BUG-003): the requester's login id as a plain number,
# kept on a restore's row even where that login is not in the restored data.

from django.db import migrations, models


def copy_requesters(apps, schema_editor):
    HostTask = apps.get_model("core", "HostTask")
    HostTask.objects.filter(requested_by__isnull=False).update(
        requested_by_user_id=models.F("requested_by_id")
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0017_restore_backup")]

    operations = [
        migrations.AddField(
            model_name="hosttask",
            name="requested_by_user_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(copy_requesters, migrations.RunPython.noop),
    ]
