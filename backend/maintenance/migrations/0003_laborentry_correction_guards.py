import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("maintenance", "0002_inspectionfinding_defect_inspection_finding")]

    operations = [
        migrations.AlterField(
            model_name="laborentry",
            name="corrects",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="correction",
                to="maintenance.laborentry",
            ),
        ),
        migrations.RunSQL(
            """
            CREATE TRIGGER labor_entry_immutable
            BEFORE UPDATE OR DELETE ON maintenance_laborentry
            FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
            """,
            "DROP TRIGGER IF EXISTS labor_entry_immutable ON maintenance_laborentry;",
        ),
    ]
