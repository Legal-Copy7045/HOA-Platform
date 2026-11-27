"""D68, step 1 of 3: room for sealed pooled choices (schema only; the data
step and the drop of the readable column are separate migrations because
PostgreSQL refuses ALTER TABLE after row changes in the same transaction)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0017_voting_window"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pendingsecretchoice",
            name="payload",
            field=models.JSONField(null=True),
        ),
        migrations.AddField(
            model_name="pendingsecretchoice",
            name="sealed",
            field=models.BinaryField(null=True),
        ),
    ]
