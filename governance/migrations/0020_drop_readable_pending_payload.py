"""D68, step 3 of 3: the pool keeps no readable choice column."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0019_seal_pending_secret_choices"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="pendingsecretchoice",
            name="payload",
        ),
        migrations.AlterField(
            model_name="pendingsecretchoice",
            name="sealed",
            field=models.BinaryField(),
        ),
    ]
