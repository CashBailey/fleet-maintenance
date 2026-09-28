from django.db import migrations

FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION fleetline_guard_document_attachment_sensitivity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM core_attachment
        WHERE id = NEW.attachment_id AND sensitivity = 'operational'
    ) THEN
        RAISE EXCEPTION 'technical documents require an operational attachment'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER core_document_attachment_sensitivity_guard
BEFORE INSERT OR UPDATE OF attachment_id ON core_document
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_document_attachment_sensitivity();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS core_document_attachment_sensitivity_guard ON core_document;
DROP FUNCTION IF EXISTS fleetline_guard_document_attachment_sensitivity();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0010_attachment_sensitivity")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
