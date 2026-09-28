from django.db import migrations


FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION fleetline_guard_attachment_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('fleetline.purge_staged_attachments', true) = 'on'
           AND OLD.resource_type IN ('defect', 'inspection', 'work_note', 'task', 'stock') THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'core_attachment is append-only' USING ERRCODE = '55000';
    END IF;
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
"""


REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION fleetline_guard_attachment_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
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
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0005_login_attempt_throttle")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
