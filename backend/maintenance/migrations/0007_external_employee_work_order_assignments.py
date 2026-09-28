import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_legacy_work_order_assignments(apps, schema_editor):
    """Preserve the old lead field as the first immutable assignment fact."""

    WorkOrder = apps.get_model("maintenance", "WorkOrder")
    WorkOrderAssignment = apps.get_model("maintenance", "WorkOrderAssignment")
    for work_order in WorkOrder.objects.exclude(assigned_to_id__isnull=True).iterator():
        WorkOrderAssignment.objects.get_or_create(
            organization_id=work_order.organization_id,
            work_order_id=work_order.pk,
            sequence=1,
            defaults={
                "id": uuid.uuid5(work_order.pk, "legacy-lead-assignment"),
                "local_user_id": work_order.assigned_to_id,
                "role": "lead",
                "action": "assigned",
                "reason": "Migrated from legacy assigned_to",
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_allow_staged_attachment_expiry"),
        ("maintenance", "0006_workorderclosesnapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExternalEmployeeProjection",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("source_system", models.CharField(max_length=40)),
                ("external_employee_id", models.CharField(max_length=160)),
                ("external_user_id", models.CharField(blank=True, max_length=160)),
                ("display_name", models.CharField(max_length=200)),
                ("job_title", models.CharField(blank=True, max_length=160)),
                ("department", models.CharField(blank=True, max_length=160)),
                ("active", models.BooleanField(default=True)),
                ("source_version", models.CharField(max_length=160)),
                ("source_updated_at", models.DateTimeField()),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="core.organization"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="WorkOrderAssignment",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[("lead", "Lead"), ("technician", "Technician")],
                        default="technician",
                        max_length=16,
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[("assigned", "Assigned"), ("unassigned", "Unassigned")],
                        max_length=16,
                    ),
                ),
                ("sequence", models.PositiveIntegerField()),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="work_order_assignments_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "external_employee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="work_order_assignment_events",
                        to="maintenance.externalemployeeprojection",
                    ),
                ),
                (
                    "local_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="work_order_assignment_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="work_order_assignment_events",
                        to="core.organization",
                    ),
                ),
                (
                    "work_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assignment_events",
                        to="maintenance.workorder",
                    ),
                ),
            ],
            options={"ordering": ["sequence"]},
        ),
        migrations.AddConstraint(
            model_name="externalemployeeprojection",
            constraint=models.UniqueConstraint(
                fields=("organization", "source_system", "external_employee_id"),
                name="uniq_external_employee_identity_org",
            ),
        ),
        migrations.AddIndex(
            model_name="externalemployeeprojection",
            index=models.Index(
                fields=["organization", "source_system", "active"],
                name="external_employee_active_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="workorderassignment",
            constraint=models.UniqueConstraint(
                fields=("organization", "work_order", "sequence"),
                name="uniq_work_order_assignment_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="workorderassignment",
            constraint=models.CheckConstraint(
                condition=models.Q(sequence__gte=1),
                name="work_order_assignment_sequence_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="workorderassignment",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(local_user__isnull=False, external_employee__isnull=True)
                    | models.Q(local_user__isnull=True, external_employee__isnull=False)
                ),
                name="work_order_assignment_single_subject",
            ),
        ),
        migrations.AddIndex(
            model_name="workorderassignment",
            index=models.Index(
                fields=["organization", "work_order", "local_user", "sequence"],
                name="work_order_assignment_user_idx",
            ),
        ),
        migrations.RunPython(backfill_legacy_work_order_assignments, migrations.RunPython.noop),
        migrations.RunSQL(
            """
            CREATE TRIGGER work_order_assignment_immutable
            BEFORE UPDATE OR DELETE ON maintenance_workorderassignment
            FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
            """,
            """
            DROP TRIGGER IF EXISTS work_order_assignment_immutable
                ON maintenance_workorderassignment;
            """,
        ),
    ]
