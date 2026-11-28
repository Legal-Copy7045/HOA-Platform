# Server tasks: restore from a backup and result emails (D98).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def mark_past_results_notified(apps, schema_editor):
    """Tasks and installs that finished before result emails existed (D98)
    are not emailed about now."""
    now = timezone.now()
    HostTask = apps.get_model("core", "HostTask")
    HostTask.objects.filter(finished_at__isnull=False).update(notified_at=now)
    UpdateStatus = apps.get_model("core", "UpdateStatus")
    UpdateStatus.objects.filter(state__in=["succeeded", "failed"]).update(result_notified_at=now)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_demo_persona"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="hosttask",
            name="backup",
            field=models.CharField(blank=True, db_default="", default="", max_length=40),
        ),
        migrations.AddField(
            model_name="hosttask",
            name="notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="updatestatus",
            name="backup_points",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="updatestatus",
            name="result_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="updatestatus",
            name="run_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="hosttask",
            name="kind",
            field=models.CharField(
                choices=[
                    ("backup", "Take a backup"),
                    ("restore_drill", "Restore drill"),
                    ("restore_drill_offsite", "Restore drill from the off-site copy"),
                    ("offsite_lock_check", "Check the off-site lock"),
                    ("harden_host", "Re-apply host security"),
                    ("health_check", "Run the health checks"),
                    ("reseed_demo", "Reseed with demo data"),
                    ("restart_app", "Restart the portal"),
                    ("restore_backup", "Restore from a backup"),
                ],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="hosttask",
            name="requested_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="hosttask",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "backup__regex",
                        "^((local|offsite):[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z)?$",
                    )
                ),
                name="core_hosttask_backup_plain",
            ),
        ),
        migrations.AddConstraint(
            model_name="hosttask",
            constraint=models.CheckConstraint(
                condition=models.Q(("backup", ""), ("kind", "restore_backup"), _connector="OR"),
                name="core_hosttask_backup_only_restore",
            ),
        ),
        migrations.RunPython(mark_past_results_notified, migrations.RunPython.noop),
    ]
