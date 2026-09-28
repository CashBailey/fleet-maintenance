from django.db import migrations, models


def _local_display_name(user):
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return (full_name or user.username)[:200]


def backfill_assignment_identity_snapshots(apps, schema_editor):
    """Freeze the identity visible on pre-existing assignment evidence.

    Assignments created before this migration did not retain profile snapshots.
    The upgrade takes one best-available snapshot from the then-current local
    account or external projection. For an existing unassignment, prefer the
    preceding active assignment snapshot in the same work-order history.
    """

    WorkOrderAssignment = apps.get_model("maintenance", "WorkOrderAssignment")
    active_snapshots = {}
    events = (
        WorkOrderAssignment.objects.select_related("local_user", "external_employee")
        .order_by("work_order_id", "sequence")
        .iterator()
    )
    for event in events:
        if event.local_user_id:
            subject_key = (event.work_order_id, "user", event.local_user_id)
            current_snapshot = {
                "subject_display_name": _local_display_name(event.local_user),
                "subject_source_system": "",
                "subject_external_employee_id": "",
                "subject_source_version": "",
            }
        else:
            subject_key = (event.work_order_id, "external_employee", event.external_employee_id)
            current_snapshot = {
                "subject_display_name": event.external_employee.display_name,
                "subject_source_system": event.external_employee.source_system,
                "subject_external_employee_id": event.external_employee.external_employee_id,
                "subject_source_version": event.external_employee.source_version,
            }

        if event.action == "unassigned":
            snapshot = active_snapshots.pop(subject_key, current_snapshot)
        else:
            snapshot = current_snapshot
            active_snapshots[subject_key] = snapshot
        WorkOrderAssignment.objects.filter(pk=event.pk).update(**snapshot)


CREATE_IMMUTABLE_TRIGGER = """
CREATE TRIGGER work_order_assignment_immutable
BEFORE UPDATE OR DELETE ON maintenance_workorderassignment
FOR EACH ROW EXECUTE FUNCTION fleetline_reject_fact_mutation();
"""

DROP_IMMUTABLE_TRIGGER = """
DROP TRIGGER IF EXISTS work_order_assignment_immutable
    ON maintenance_workorderassignment;
"""


class Migration(migrations.Migration):
    dependencies = [("maintenance", "0007_external_employee_work_order_assignments")]

    operations = [
        migrations.AddField(
            model_name="workorderassignment",
            name="subject_display_name",
            field=models.CharField(default="", max_length=200),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="workorderassignment",
            name="subject_external_employee_id",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="workorderassignment",
            name="subject_source_system",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="workorderassignment",
            name="subject_source_version",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.RunSQL(DROP_IMMUTABLE_TRIGGER, CREATE_IMMUTABLE_TRIGGER),
        migrations.RunPython(backfill_assignment_identity_snapshots, migrations.RunPython.noop),
        migrations.RunSQL(CREATE_IMMUTABLE_TRIGGER, DROP_IMMUTABLE_TRIGGER),
    ]
