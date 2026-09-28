import uuid

import django.db.models.deletion
from django.db import migrations, models


ATTACHMENT_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION fleetline_guard_attachment_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Offline uploads are staged against their operation UUID. The synchronizer may
    -- finalize that association once; bytes, lineage, ownership, and metadata stay fixed.
    IF TG_OP = 'UPDATE'
       AND OLD.resource_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       AND NEW.resource_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       AND OLD.resource_id <> NEW.resource_id
       AND (
           (OLD.resource_type = 'defect' AND NEW.resource_type = 'Defect')
           OR (OLD.resource_type = 'inspection' AND NEW.resource_type = 'Inspection')
           OR (OLD.resource_type = 'work_note' AND NEW.resource_type = 'WorkOrder')
           OR (OLD.resource_type = 'task' AND NEW.resource_type = 'WorkOrderTask')
       )
       AND (
           to_jsonb(NEW) - ARRAY['resource_type', 'resource_id', 'updated_at']::text[]
       ) IS NOT DISTINCT FROM (
           to_jsonb(OLD) - ARRAY['resource_type', 'resource_id', 'updated_at']::text[]
       ) THEN
        NEW.updated_at := CURRENT_TIMESTAMP;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'core_attachment is append-only' USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER core_attachment_immutable
BEFORE UPDATE OR DELETE ON core_attachment
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_attachment_mutation();
"""

ATTACHMENT_GUARD_REVERSE_SQL = """
DROP TRIGGER IF EXISTS core_attachment_immutable ON core_attachment;
DROP FUNCTION IF EXISTS fleetline_guard_attachment_mutation();
"""


def initialize_attachment_documents(apps, schema_editor):
    Attachment = apps.get_model("core", "Attachment")
    for attachment in Attachment.objects.only("pk", "original_name").iterator(chunk_size=500):
        Attachment.objects.filter(pk=attachment.pk).update(
            document_key=uuid.uuid4(), title=attachment.original_name
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_api_token_expiry"),
    ]

    operations = [
        migrations.AddField(
            model_name="attachment",
            name="category",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="attachment",
            name="document_key",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="attachment",
            name="supersedes",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="superseded_by",
                to="core.attachment",
            ),
        ),
        migrations.AddField(
            model_name="attachment",
            name="title",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="attachment",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(initialize_attachment_documents, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="attachment",
            name="document_key",
            field=models.UUIDField(default=uuid.uuid4, editable=False),
        ),
        migrations.AddConstraint(
            model_name="attachment",
            constraint=models.UniqueConstraint(
                fields=("organization", "document_key", "version"),
                name="uniq_attachment_document_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="attachment",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)), name="attachment_version_positive"
            ),
        ),
        migrations.RunSQL(ATTACHMENT_GUARD_SQL, ATTACHMENT_GUARD_REVERSE_SQL),
    ]
