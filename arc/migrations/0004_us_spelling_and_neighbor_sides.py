# US spelling (Neighbor), per-lot left/right/other neighbors, and the fields
# from the association's alteration request form (Milestone 4).

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("arc", "0003_neighbour_signoff"),
        ("community", "0012_digest"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="lotneighbour", name="lot_neighbour_once"),
        migrations.RemoveConstraint(model_name="lotneighbour", name="lot_not_own_neighbour"),
        migrations.RenameModel(old_name="LotNeighbour", new_name="LotNeighbor"),
        migrations.RenameField(model_name="lotneighbor", old_name="neighbour", new_name="neighbor"),
        migrations.AddField(
            model_name="lotneighbor",
            name="side",
            field=models.CharField(
                choices=[("left", "Left"), ("right", "Right"), ("other", "Also affected")],
                default="other",
                max_length=8,
            ),
        ),
        migrations.AlterModelOptions(
            name="lotneighbor", options={"ordering": ["lot", "side", "neighbor"]}
        ),
        migrations.AddConstraint(
            model_name="lotneighbor",
            constraint=models.UniqueConstraint(
                fields=("lot", "neighbor"), name="lot_neighbor_once"
            ),
        ),
        migrations.AddConstraint(
            model_name="lotneighbor",
            constraint=models.CheckConstraint(
                condition=models.Q(("lot", models.F("neighbor")), _negated=True),
                name="lot_not_own_neighbor",
            ),
        ),
        migrations.AddConstraint(
            model_name="lotneighbor",
            constraint=models.UniqueConstraint(
                condition=models.Q(("side__in", ["left", "right"])),
                fields=("lot", "side"),
                name="lot_one_left_one_right",
            ),
        ),
    ]
