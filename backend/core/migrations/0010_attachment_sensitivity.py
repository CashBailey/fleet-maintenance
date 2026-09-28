from django.db import migrations, models

HISTORIC_CLASSIFICATION_SQL = r"""
ALTER TABLE core_attachment DISABLE TRIGGER core_attachment_immutable;
UPDATE core_attachment
SET sensitivity = 'operational'
WHERE lower(replace(resource_type, '-', '_')) NOT IN (
    'part', 'vendor', 'purchase_order', 'purchaseorder', 'receipt'
);
ALTER TABLE core_attachment ENABLE TRIGGER core_attachment_immutable;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0009_document_workflow_and_attachment_lineage_guards")]

    operations = [
        # Existing commercial attachments are opaque and cannot be safely inferred from names.
        migrations.AddField(
            model_name="attachment",
            name="sensitivity",
            field=models.CharField(
                choices=[("operational", "Operational"), ("financial", "Financial")],
                default="financial",
                max_length=16,
            ),
        ),
        migrations.RunSQL(HISTORIC_CLASSIFICATION_SQL, migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="attachment",
            name="sensitivity",
            field=models.CharField(
                choices=[("operational", "Operational"), ("financial", "Financial")],
                default="operational",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="attachment",
            constraint=models.CheckConstraint(
                condition=models.Q(("sensitivity__in", ("operational", "financial"))),
                name="attachment_sensitivity_valid",
            ),
        ),
    ]
