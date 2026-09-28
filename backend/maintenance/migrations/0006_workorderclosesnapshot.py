import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _json_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _rows(model, *, fields, **filters):
    return [_json_safe(row) for row in model.objects.filter(**filters).values(*fields)]


def backfill_closed_work_orders(apps, schema_editor):
    WorkOrder = apps.get_model("maintenance", "WorkOrder")
    WorkOrderTask = apps.get_model("maintenance", "WorkOrderTask")
    LaborEntry = apps.get_model("maintenance", "LaborEntry")
    MaintenanceTrigger = apps.get_model("maintenance", "MaintenanceTrigger")
    WorkOrderCloseSnapshot = apps.get_model("maintenance", "WorkOrderCloseSnapshot")
    StockTransaction = apps.get_model("inventory", "StockTransaction")
    Reservation = apps.get_model("inventory", "Reservation")
    Attachment = apps.get_model("core", "Attachment")
    Comment = apps.get_model("core", "Comment")

    for work_order in WorkOrder.objects.filter(status="Closed").iterator():
        resource_id = str(work_order.pk)
        payload = {
            "schema_version": 1,
            "legacy_backfill": True,
            "work_order": _json_safe(
                {
                    field: getattr(work_order, field)
                    for field in (
                        "id",
                        "number",
                        "asset_id",
                        "request_id",
                        "maintenance_plan_id",
                        "service_package_snapshot",
                        "status",
                        "priority",
                        "summary",
                        "complaint",
                        "diagnosis",
                        "completion_summary",
                        "blocked_reason",
                        "reopen_reason",
                        "requires_qc",
                        "target_date",
                        "assigned_to_id",
                        "ready_at",
                        "started_at",
                        "completed_at",
                        "completed_by_id",
                        "closed_at",
                        "closed_by_id",
                        "completion_meter_id",
                        "version",
                    )
                }
            ),
            "tasks": _rows(
                WorkOrderTask,
                fields=(
                    "id",
                    "title",
                    "instructions",
                    "sequence",
                    "required",
                    "status",
                    "notes",
                    "measurement",
                    "completed_at",
                    "completed_by_id",
                ),
                work_order_id=work_order.pk,
            ),
            "labor_entries": _rows(
                LaborEntry,
                fields=(
                    "id",
                    "technician_id",
                    "started_at",
                    "ended_at",
                    "minutes",
                    "hourly_rate",
                    "cost",
                    "note",
                    "corrects_id",
                ),
                work_order_id=work_order.pk,
            ),
            "stock_transactions": _rows(
                StockTransaction,
                fields=(
                    "id",
                    "operation_id",
                    "transaction_type",
                    "part_id",
                    "bin_id",
                    "quantity",
                    "unit_cost",
                    "total_cost",
                    "reservation_id",
                    "original_transaction_id",
                    "reason",
                    "created_at",
                ),
                work_order_id=work_order.pk,
            ),
            "reservations": _rows(
                Reservation,
                fields=(
                    "id",
                    "operation_id",
                    "part_id",
                    "bin_id",
                    "requested_quantity",
                    "issued_quantity",
                    "released_quantity",
                    "status",
                    "reason",
                ),
                work_order_id=work_order.pk,
            ),
            "attachments": _rows(
                Attachment,
                fields=(
                    "id",
                    "document_key",
                    "category",
                    "title",
                    "version",
                    "supersedes_id",
                    "original_name",
                    "content_type",
                    "size",
                    "sha256",
                    "uploader_id",
                    "created_at",
                ),
                resource_type="WorkOrder",
                resource_id=resource_id,
            ),
            "comments": _rows(
                Comment,
                fields=("id", "author_id", "body", "created_at"),
                resource_type="WorkOrder",
                resource_id=resource_id,
            ),
            # The pre-reset trigger values no longer exist for legacy rows. Reclose preserves
            # the current projection instead of guessing and accidentally advancing PM twice.
            "plan_reset_baseline": None,
            "plan_reset_after": _rows(
                MaintenanceTrigger,
                fields=("id", "last_completed_at", "last_completed_value"),
                plan_id=work_order.maintenance_plan_id,
            )
            if work_order.maintenance_plan_id
            else [],
        }
        WorkOrderCloseSnapshot.objects.create(
            id=uuid.uuid5(work_order.pk, "close:1"),
            organization_id=work_order.organization_id,
            work_order_id=work_order.pk,
            sequence=1,
            closed_by_id=work_order.closed_by_id,
            closed_at=work_order.closed_at or work_order.updated_at,
            snapshot=payload,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_attachment_versions"),
        ("maintenance", "0005_maintenance_concurrency_constraints"),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkOrderCloseSnapshot",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("sequence", models.PositiveIntegerField()),
                ("closed_at", models.DateTimeField()),
                ("snapshot", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "closed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="work_order_close_snapshots",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="work_order_close_snapshots",
                        to="core.organization",
                    ),
                ),
                (
                    "work_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="close_snapshots",
                        to="maintenance.workorder",
                    ),
                ),
            ],
            options={"ordering": ["sequence"]},
        ),
        migrations.AddConstraint(
            model_name="workorderclosesnapshot",
            constraint=models.UniqueConstraint(
                fields=("organization", "work_order", "sequence"),
                name="uniq_work_order_close_snapshot_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="workorderclosesnapshot",
            constraint=models.CheckConstraint(
                condition=models.Q(("sequence__gte", 1)),
                name="work_order_close_snapshot_sequence_positive",
            ),
        ),
        migrations.RunPython(backfill_closed_work_orders, migrations.RunPython.noop),
        migrations.RunSQL(
            """
            CREATE TRIGGER work_order_close_snapshot_immutable
            BEFORE UPDATE OR DELETE ON maintenance_workorderclosesnapshot
            FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
            """,
            """
            DROP TRIGGER IF EXISTS work_order_close_snapshot_immutable
                ON maintenance_workorderclosesnapshot;
            """,
        ),
    ]
