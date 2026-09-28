from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("maintenance", "0004_inspection_immutability_guards"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="maintenancerequest",
            constraint=models.UniqueConstraint(
                fields=("defect",),
                condition=models.Q(defect__isnull=False)
                & ~models.Q(status__in=["Rejected", "Closed"]),
                name="uniq_active_request_defect",
            ),
        ),
        migrations.AddConstraint(
            model_name="workorder",
            constraint=models.UniqueConstraint(
                fields=("maintenance_plan",),
                condition=models.Q(maintenance_plan__isnull=False)
                & ~models.Q(status__in=["Closed", "Cancelled"]),
                name="uniq_active_work_order_plan",
            ),
        ),
    ]
