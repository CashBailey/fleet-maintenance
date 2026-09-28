from django.db import migrations

FORWARD_SQL = """
CREATE OR REPLACE FUNCTION fleetline_guard_inspection_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status NOT IN ('Submitted', 'Voided') THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE'
       AND OLD.status = 'Submitted'
       AND NEW.status = 'Voided'
       AND BTRIM(COALESCE(NEW.void_reason, '')) <> ''
       AND NEW.voided_at IS NOT NULL
       AND NEW.voided_by_id IS NOT NULL
       AND ROW(
           NEW.id,
           NEW.organization_id,
           NEW.asset_id,
           NEW.template_id,
           NEW.template_snapshot,
           NEW.performed_by_id,
           NEW.started_at,
           NEW.submitted_at,
           NEW.acknowledgment,
           NEW.replaces_id,
           NEW.created_at
       ) IS NOT DISTINCT FROM ROW(
           OLD.id,
           OLD.organization_id,
           OLD.asset_id,
           OLD.template_id,
           OLD.template_snapshot,
           OLD.performed_by_id,
           OLD.started_at,
           OLD.submitted_at,
           OLD.acknowledgment,
           OLD.replaces_id,
           OLD.created_at
       )
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'submitted inspection records are immutable; void and replace the record'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION fleetline_guard_inspection_response_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    inspection_ids uuid[];
    inspection_status text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        inspection_ids := ARRAY[NEW.inspection_id];
    ELSIF TG_OP = 'DELETE' THEN
        inspection_ids := ARRAY[OLD.inspection_id];
    ELSE
        inspection_ids := ARRAY[OLD.inspection_id, NEW.inspection_id];
    END IF;

    FOR inspection_status IN
        SELECT status
        FROM maintenance_inspection
        WHERE id = ANY(inspection_ids)
        ORDER BY id
        FOR SHARE
    LOOP
        IF inspection_status IN ('Submitted', 'Voided') THEN
            RAISE EXCEPTION 'responses on submitted inspections are immutable'
                USING ERRCODE = '55000';
        END IF;
    END LOOP;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER maintenance_inspection_immutable
BEFORE UPDATE OR DELETE ON maintenance_inspection
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_inspection_immutability();

CREATE TRIGGER maintenance_inspection_response_immutable
BEFORE INSERT OR UPDATE OR DELETE ON maintenance_inspectionresponse
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_inspection_response_immutability();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS maintenance_inspection_response_immutable
    ON maintenance_inspectionresponse;
DROP TRIGGER IF EXISTS maintenance_inspection_immutable ON maintenance_inspection;
DROP FUNCTION IF EXISTS fleetline_guard_inspection_response_immutability();
DROP FUNCTION IF EXISTS fleetline_guard_inspection_immutability();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("maintenance", "0003_laborentry_correction_guards"),
    ]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
