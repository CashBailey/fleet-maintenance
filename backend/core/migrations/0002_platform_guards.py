from django.db import migrations


FORWARD_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION fleetline_reject_fact_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER core_audit_immutable
BEFORE UPDATE OR DELETE ON core_auditevent
FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
CREATE TRIGGER asset_status_immutable
BEFORE UPDATE OR DELETE ON assets_assetstatusevent
FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
CREATE TRIGGER meter_reading_immutable
BEFORE UPDATE OR DELETE ON assets_meterreading
FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
CREATE TRIGGER stock_transaction_immutable
BEFORE UPDATE OR DELETE ON inventory_stocktransaction
FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
CREATE TRIGGER telemetry_message_immutable
BEFORE UPDATE OR DELETE ON integrations_telematicsmessage
FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
CREATE TRIGGER normalized_telemetry_immutable
BEFORE UPDATE OR DELETE ON integrations_normalizedtelematicsevent
FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();

CREATE INDEX asset_unit_trgm ON assets_asset USING gin (unit_number gin_trgm_ops);
CREATE INDEX asset_vin_trgm ON assets_asset USING gin (vin gin_trgm_ops);
CREATE INDEX part_number_trgm ON inventory_part USING gin (number gin_trgm_ops);
CREATE INDEX part_name_trgm ON inventory_part USING gin (name gin_trgm_ops);
CREATE INDEX vendor_name_trgm ON purchasing_vendor USING gin (name gin_trgm_ops);
CREATE INDEX work_order_number_trgm ON maintenance_workorder USING gin (number gin_trgm_ops);
CREATE INDEX work_order_summary_trgm ON maintenance_workorder USING gin (summary gin_trgm_ops);
"""

REVERSE_SQL = """
DROP INDEX IF EXISTS work_order_summary_trgm;
DROP INDEX IF EXISTS work_order_number_trgm;
DROP INDEX IF EXISTS vendor_name_trgm;
DROP INDEX IF EXISTS part_name_trgm;
DROP INDEX IF EXISTS part_number_trgm;
DROP INDEX IF EXISTS asset_vin_trgm;
DROP INDEX IF EXISTS asset_unit_trgm;
DROP TRIGGER IF EXISTS normalized_telemetry_immutable ON integrations_normalizedtelematicsevent;
DROP TRIGGER IF EXISTS telemetry_message_immutable ON integrations_telematicsmessage;
DROP TRIGGER IF EXISTS stock_transaction_immutable ON inventory_stocktransaction;
DROP TRIGGER IF EXISTS meter_reading_immutable ON assets_meterreading;
DROP TRIGGER IF EXISTS asset_status_immutable ON assets_assetstatusevent;
DROP TRIGGER IF EXISTS core_audit_immutable ON core_auditevent;
DROP FUNCTION IF EXISTS fleetline_reject_fact_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        ("assets", "0002_initial"),
        ("maintenance", "0001_initial"),
        ("inventory", "0001_initial"),
        ("purchasing", "0001_initial"),
        ("integrations", "0001_initial"),
    ]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
