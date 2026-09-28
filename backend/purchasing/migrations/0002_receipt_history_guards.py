from django.db import migrations


FORWARD_SQL = """
CREATE OR REPLACE FUNCTION fleetline_guard_receipt_line_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_status varchar(12);
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT status
          INTO parent_status
          FROM purchasing_receipt
         WHERE id = NEW.receipt_id
         FOR UPDATE;
        IF parent_status IS DISTINCT FROM 'Draft' THEN
            RAISE EXCEPTION 'Lines may only be added to a draft receipt'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    SELECT status
      INTO parent_status
      FROM purchasing_receipt
     WHERE id = OLD.receipt_id
     FOR UPDATE;
    IF parent_status IS DISTINCT FROM 'Draft' THEN
        RAISE EXCEPTION 'Posted or reversed receipt lines are append-only'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.receipt_id IS DISTINCT FROM OLD.receipt_id THEN
            RAISE EXCEPTION 'A receipt line cannot be moved to another receipt'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER purchasing_receipt_line_history_guard
BEFORE INSERT OR UPDATE OR DELETE ON purchasing_receiptline
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_receipt_line_history();

CREATE OR REPLACE FUNCTION fleetline_guard_receipt_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    valid_reversal boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IS DISTINCT FROM 'Draft' THEN
            RAISE EXCEPTION 'Posted or reversed receipts cannot be deleted'
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'Draft' THEN
        IF NEW.status NOT IN ('Draft', 'Posted') THEN
            RAISE EXCEPTION 'A draft receipt may only be posted'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.status = 'Posted' AND (
            NOT EXISTS (
                SELECT 1 FROM purchasing_receiptline WHERE receipt_id = OLD.id
            )
            OR EXISTS (
                SELECT 1
                  FROM purchasing_receiptline line
                  LEFT JOIN purchasing_purchaseorderline order_line
                    ON order_line.id = line.purchase_order_line_id
                  LEFT JOIN inventory_bin stock_bin ON stock_bin.id = line.bin_id
                  LEFT JOIN inventory_stocktransaction stock_tx
                    ON stock_tx.id = line.stock_transaction_id
                  LEFT JOIN purchasing_receiptline original_line
                    ON original_line.id = line.reversal_of_id
                 WHERE line.receipt_id = OLD.id
                   AND (
                       line.organization_id IS DISTINCT FROM NEW.organization_id
                       OR order_line.purchase_order_id IS DISTINCT FROM NEW.purchase_order_id
                       OR order_line.organization_id IS DISTINCT FROM NEW.organization_id
                       OR order_line.part_id IS DISTINCT FROM line.part_id
                       OR stock_bin.organization_id IS DISTINCT FROM NEW.organization_id
                       OR stock_tx.id IS NULL
                       OR stock_tx.organization_id IS DISTINCT FROM NEW.organization_id
                       OR stock_tx.part_id IS DISTINCT FROM line.part_id
                       OR stock_tx.bin_id IS DISTINCT FROM line.bin_id
                       OR stock_tx.quantity IS DISTINCT FROM line.quantity
                       OR stock_tx.unit_cost IS DISTINCT FROM line.unit_cost
                       OR (
                           NEW.reversal_of_id IS NULL
                           AND (
                               line.reversal_of_id IS NOT NULL
                               OR line.quantity <= 0
                               OR stock_tx.transaction_type IS DISTINCT FROM 'RECEIPT'
                               OR stock_tx.original_transaction_id IS NOT NULL
                               OR stock_tx.reference_type IS DISTINCT FROM 'receipt_line'
                               OR stock_tx.reference_id IS DISTINCT FROM line.id::text
                           )
                       )
                       OR (
                           NEW.reversal_of_id IS NOT NULL
                           AND (
                               original_line.id IS NULL
                               OR original_line.receipt_id IS DISTINCT FROM NEW.reversal_of_id
                               OR line.part_id IS DISTINCT FROM original_line.part_id
                               OR line.bin_id IS DISTINCT FROM original_line.bin_id
                               OR line.purchase_order_line_id IS DISTINCT
                                  FROM original_line.purchase_order_line_id
                               OR line.unit_cost IS DISTINCT FROM original_line.unit_cost
                               OR line.quantity IS DISTINCT FROM -original_line.quantity
                               OR stock_tx.transaction_type IS DISTINCT FROM 'REVERSAL'
                               OR stock_tx.original_transaction_id IS DISTINCT
                                  FROM original_line.stock_transaction_id
                           )
                       )
                   )
            )
        ) THEN
            RAISE EXCEPTION 'A posted receipt requires complete, matching stock transactions'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'Posted' THEN
        SELECT EXISTS (
            SELECT 1
              FROM purchasing_receipt reversal
             WHERE reversal.reversal_of_id = OLD.id
               AND reversal.status = 'Posted'
               AND reversal.organization_id = OLD.organization_id
               AND reversal.purchase_order_id = OLD.purchase_order_id
               AND NOT EXISTS (
                   SELECT 1
                     FROM purchasing_receiptline original_line
                    WHERE original_line.receipt_id = OLD.id
                      AND NOT EXISTS (
                          SELECT 1
                            FROM purchasing_receiptline reversal_line
                           WHERE reversal_line.receipt_id = reversal.id
                             AND reversal_line.reversal_of_id = original_line.id
                      )
               )
               AND NOT EXISTS (
                   SELECT 1
                     FROM purchasing_receiptline reversal_line
                    WHERE reversal_line.receipt_id = reversal.id
                      AND NOT EXISTS (
                          SELECT 1
                            FROM purchasing_receiptline original_line
                           WHERE original_line.receipt_id = OLD.id
                             AND original_line.id = reversal_line.reversal_of_id
                      )
               )
        ) INTO valid_reversal;
        IF NEW.status IS DISTINCT FROM 'Reversed'
           OR OLD.reversal_of_id IS NOT NULL
           OR NOT valid_reversal
           OR NEW.reversed_by_id IS NULL
           OR NEW.reversed_at IS NULL
           OR NEW.id IS DISTINCT FROM OLD.id
           OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
           OR NEW.purchase_order_id IS DISTINCT FROM OLD.purchase_order_id
           OR NEW.number IS DISTINCT FROM OLD.number
           OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
           OR NEW.received_by_id IS DISTINCT FROM OLD.received_by_id
           OR NEW.received_at IS DISTINCT FROM OLD.received_at
           OR NEW.packing_slip IS DISTINCT FROM OLD.packing_slip
           OR NEW.reason IS DISTINCT FROM OLD.reason
           OR NEW.reversal_of_id IS DISTINCT FROM OLD.reversal_of_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'Posted receipts are immutable and may only be reversed'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'Reversed receipts are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER purchasing_receipt_lifecycle_guard
BEFORE UPDATE OR DELETE ON purchasing_receipt
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_receipt_lifecycle();
"""


REVERSE_SQL = """
DROP TRIGGER IF EXISTS purchasing_receipt_lifecycle_guard ON purchasing_receipt;
DROP FUNCTION IF EXISTS fleetline_guard_receipt_lifecycle();
DROP TRIGGER IF EXISTS purchasing_receipt_line_history_guard ON purchasing_receiptline;
DROP FUNCTION IF EXISTS fleetline_guard_receipt_line_history();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_platform_guards"),
        ("purchasing", "0001_initial"),
    ]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
