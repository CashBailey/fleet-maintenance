from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0003_inventorycountline_unit_cost_snapshot_and_more"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="reservation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    issued_quantity__lte=models.F("requested_quantity")
                    - models.F("released_quantity")
                ),
                name="reservation_consumed_lte_requested",
            ),
        ),
    ]
