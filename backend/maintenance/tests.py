from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from assets.models import Asset, AssetType, Meter, MeterReading
from core.exceptions import DomainError
from core.management.commands.runworker import Command as WorkerCommand
from core.models import (
    Attachment,
    AuditEvent,
    Location,
    Notification,
    Organization,
    OutboxEvent,
    Role,
    User,
    WorkerHeartbeat,
)
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import (
    Defect,
    InspectionFinding,
    InspectionTemplate,
    MaintenancePlan,
    MaintenanceTrigger,
    ServicePackage,
)
from .services import (
    calculate_plan_due,
    create_defect,
    create_inspection,
    create_maintenance_plan,
    create_request,
    create_service_package,
    create_work_order,
    recalculate_date_plans,
    sync_field_operation,
    transition_defect,
    transition_request,
    transition_work_order,
    update_work_order_task,
    void_inspection_record,
)


class MaintenanceDomainTests(TestCase):
    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Test Fleet", slug="test-fleet")
        self.location = Location.objects.create(organization=self.org, name="Main", code="MAIN")
        self.asset_type = AssetType.objects.create(organization=self.org, name="Truck")
        self.driver = self.user("driver", "driver")
        self.tech = self.user("tech", "technician")
        self.supervisor = self.user("supervisor", "supervisor")
        self.manager = self.user("manager", "fleet_manager")
        self.asset = Asset.objects.create(
            organization=self.org,
            asset_type=self.asset_type,
            home_location=self.location,
            assigned_driver=self.driver,
            unit_number="T-01",
        )
        self.odometer = Meter.objects.create(
            organization=self.org, asset=self.asset, name="Odometer", kind="odometer", unit="mi"
        )

    def user(self, username: str, role_slug: str) -> User:
        user = User.objects.create_user(username=username, organization=self.org)
        role, _ = Role.objects.get_or_create(
            organization=self.org,
            slug=role_slug,
            defaults={"name": role_slug.replace("_", " ").title()},
        )
        user.roles.add(role)
        return user

    def package(self, name: str = "PM A") -> ServicePackage:
        return create_service_package(
            organization=self.org,
            actor=self.supervisor,
            name=name,
            tasks=[{"title": "Change oil", "required": True}],
        )

    def test_whichever_trigger_is_most_urgent_and_grace_is_respected(self) -> None:
        now = timezone.now()
        MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("1600"),
            observed_at=now,
            source="manual",
            created_by=self.manager,
        )
        plan = MaintenancePlan.objects.create(
            organization=self.org,
            asset=self.asset,
            service_package=self.package(),
            name="Oil service",
        )
        MaintenanceTrigger.objects.create(
            organization=self.org,
            plan=plan,
            kind="mileage",
            meter=self.odometer,
            interval=500,
            grace=100,
            due_soon_threshold=50,
            last_completed_value=1000,
        )
        MaintenanceTrigger.objects.create(
            organization=self.org,
            plan=plan,
            kind="date",
            interval=30,
            grace=5,
            due_soon_threshold=7,
            last_completed_at=now - timedelta(days=28),
        )

        calculate_plan_due(plan, as_of=now)

        self.assertEqual(plan.due_status, "Overdue")
        self.assertEqual({reason["status"] for reason in plan.due_reasons}, {"Overdue", "DueSoon"})

    def test_new_plan_rejects_superseded_service_package(self) -> None:
        superseded = self.package("Versioned PM")
        current = self.package("Versioned PM")
        superseded.refresh_from_db()
        self.assertFalse(superseded.active)
        self.assertTrue(current.active)

        with self.assertRaises(DomainError) as caught:
            create_maintenance_plan(
                organization=self.org,
                actor=self.supervisor,
                asset=self.asset,
                package=superseded,
                name="Obsolete plan",
                triggers=[{"kind": "date", "interval": "30"}],
            )

        self.assertEqual(caught.exception.code, "inactive_service_package")
        self.assertFalse(MaintenancePlan.objects.filter(name="Obsolete plan").exists())

    def test_meter_plan_without_completion_baseline_is_due_without_a_reading(self) -> None:
        plan = create_maintenance_plan(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            package=self.package(),
            name="Unknown-baseline mileage PM",
            triggers=[
                {
                    "kind": "mileage",
                    "meter_id": str(self.odometer.pk),
                    "interval": "1000",
                }
            ],
        )

        trigger = plan.triggers.get()
        self.assertIsNone(trigger.last_completed_value)
        self.assertEqual(plan.due_status, "Due")
        self.assertEqual(
            plan.due_reasons[0]["message"],
            "Last completion baseline is unknown; initial service is due",
        )
        self.assertNotIn("current_value", plan.due_reasons[0])

    def test_meter_plan_without_completion_baseline_includes_current_reading_context(self) -> None:
        reading = MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("400"),
            observed_at=timezone.now(),
            source="manual",
            created_by=self.manager,
        )

        plan = create_maintenance_plan(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            package=self.package(),
            name="Unknown-baseline mileage PM",
            triggers=[
                {
                    "kind": "mileage",
                    "meter_id": str(self.odometer.pk),
                    "interval": "1000",
                }
            ],
        )

        self.assertEqual(plan.due_status, "Due")
        self.assertEqual(plan.due_reasons[0]["current_value"], "400.000")
        self.assertEqual(plan.due_reasons[0]["reading_id"], str(reading.pk))

    def test_date_plan_without_completion_date_is_due_without_inventing_a_baseline(self) -> None:
        plan = create_maintenance_plan(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            package=self.package("Unknown date baseline"),
            name="Unknown-baseline date PM",
            triggers=[{"kind": "date", "interval": "30"}],
        )

        trigger = plan.triggers.get()
        self.assertIsNone(trigger.last_completed_at)
        self.assertEqual(plan.due_status, "Due")
        self.assertEqual(
            plan.due_reasons[0]["message"],
            "Last completion date is unknown; initial service is due",
        )
        initial_event = OutboxEvent.objects.get(
            event_type="maintenance.plan_projection_changed",
            resource_id=str(plan.pk),
        )
        self.assertEqual(initial_event.payload["due_status"], "Due")
        self.assertIsNone(initial_event.payload["work_order_id"])
        self.assertEqual(
            initial_event.payload["next_due"][0]["message"],
            "Last completion date is unknown; initial service is due",
        )

        completed_at = timezone.now()
        known = create_maintenance_plan(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            package=self.package("Known date baseline"),
            name="Known-baseline date PM",
            triggers=[{"kind": "date", "interval": "30", "last_completed_at": completed_at}],
        )
        self.assertEqual(known.triggers.get().last_completed_at, completed_at)
        self.assertEqual(known.due_status, "Current")
        self.assertEqual(
            OutboxEvent.objects.get(
                event_type="maintenance.plan_projection_changed",
                resource_id=str(known.pk),
            ).payload["due_status"],
            "Current",
        )

    def test_plan_api_preserves_explicit_zero_completion_baseline(self) -> None:
        MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("400"),
            observed_at=timezone.now(),
            source="manual",
            created_by=self.manager,
        )
        package = self.package()
        client = APIClient()
        client.force_authenticate(self.supervisor)

        response = client.post(
            "/api/v1/maintenance/plans/",
            {
                "asset_id": str(self.asset.pk),
                "service_package_id": str(package.pk),
                "name": "Explicit-zero mileage PM",
                "triggers": [
                    {
                        "kind": "mileage",
                        "meter_id": str(self.odometer.pk),
                        "interval": "1000",
                        "last_completed_value": "0",
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        plan = MaintenancePlan.objects.get(name="Explicit-zero mileage PM")
        self.assertEqual(plan.triggers.get().last_completed_value, Decimal("0"))
        self.assertEqual(response.json()["plan"]["triggers"][0]["last_completed_value"], "0.000")
        self.assertEqual(plan.due_status, "Current")

    def test_periodic_date_recalculation_emits_and_notifies_once_per_cycle(self) -> None:
        now = timezone.now()
        plan = create_maintenance_plan(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            package=self.package(),
            name="Periodic date PM",
            triggers=[
                {
                    "kind": "date",
                    "interval": "30",
                    "last_completed_at": now,
                }
            ],
        )
        self.assertEqual(plan.due_status, "Current")

        future = now + timedelta(days=31)
        self.assertEqual(recalculate_date_plans(as_of=future), 1)
        self.assertEqual(recalculate_date_plans(as_of=future), 1)

        plan.refresh_from_db()
        events = OutboxEvent.objects.filter(
            organization=self.org,
            event_type="maintenance.due",
            resource_type="MaintenancePlan",
            resource_id=str(plan.pk),
        )
        self.assertEqual(plan.due_status, "Overdue")
        self.assertEqual(events.count(), 1)
        projections = OutboxEvent.objects.filter(
            organization=self.org,
            event_type="maintenance.plan_projection_changed",
            resource_id=str(plan.pk),
        )
        self.assertEqual(projections.count(), 2)

        worker = WorkerCommand()
        self.assertTrue(worker.process_one())
        self.assertTrue(worker.process_one())
        self.assertTrue(worker.process_one())
        self.assertFalse(worker.process_one())
        self.assertEqual(
            Notification.objects.filter(
                organization=self.org,
                resource_type="MaintenancePlan",
                resource_id=str(plan.pk),
            ).count(),
            2,
        )
        self.assertEqual(recalculate_date_plans(as_of=future), 1)
        self.assertEqual(events.count(), 1)
        self.assertEqual(projections.count(), 2)

        trigger = plan.triggers.get()
        trigger.last_completed_at = future
        trigger.save(update_fields=["last_completed_at", "updated_at"])
        self.assertEqual(recalculate_date_plans(as_of=future), 1)
        self.assertEqual(recalculate_date_plans(as_of=future + timedelta(days=31)), 1)
        self.assertEqual(recalculate_date_plans(as_of=future + timedelta(days=31)), 1)
        self.assertEqual(events.count(), 2)
        while worker.process_one():
            pass
        self.assertEqual(
            Notification.objects.filter(
                organization=self.org,
                resource_type="MaintenancePlan",
                resource_id=str(plan.pk),
            ).count(),
            4,
        )

    def test_manual_recalculation_uses_transition_events_and_ignores_retired_assets(self) -> None:
        now = timezone.now()
        plan = create_maintenance_plan(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            package=self.package("Manual recalculation"),
            name="Manual recalculation",
            triggers=[{"kind": "date", "interval": "30", "last_completed_at": now}],
        )
        trigger = plan.triggers.get()
        trigger.last_completed_at = now - timedelta(days=31)
        trigger.save(update_fields=["last_completed_at", "updated_at"])
        client = APIClient()
        client.force_authenticate(self.supervisor)

        response = client.post(
            "/api/v1/maintenance/plans/recalculate/",
            {"plan_id": str(plan.pk)},
            format="json",
        )
        replay = client.post(
            "/api/v1/maintenance/plans/recalculate/",
            {"plan_id": str(plan.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type="maintenance.plan_projection_changed", resource_id=str(plan.pk)
            ).count(),
            2,
        )
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type="maintenance.due", resource_id=str(plan.pk)
            ).count(),
            1,
        )

        self.asset.status = Asset.Status.RETIRED
        self.asset.archived_at = now
        self.asset.save(update_fields=["status", "archived_at", "updated_at"])
        plan.due_status = "Current"
        plan.save(update_fields=["due_status", "updated_at"])
        self.assertEqual(recalculate_date_plans(as_of=now + timedelta(days=60)), 0)
        plan.refresh_from_db()
        self.assertEqual(plan.due_status, "Current")

    def test_worker_cycle_runs_periodic_date_recalculation(self) -> None:
        worker = WorkerCommand()
        with (
            patch("maintenance.services.recalculate_date_plans") as recalculate,
            patch.object(worker, "process_one", return_value=False),
            patch.object(worker, "deliver_one", return_value=False),
        ):
            worker.handle(once=True, poll=0)

        recalculate.assert_called_once_with()

    def test_worker_surfaces_pm_sweep_failure_and_retries_quickly(self) -> None:
        worker = WorkerCommand()
        with (
            patch(
                "maintenance.services.recalculate_date_plans",
                side_effect=[RuntimeError("database unavailable"), None],
            ) as recalculate,
            patch.object(worker, "process_one", return_value=False),
            patch.object(worker, "deliver_one", return_value=False),
            patch.object(worker.stderr, "write") as stderr,
            patch("core.management.commands.runworker.time.monotonic", side_effect=[0, 61]),
            patch(
                "core.management.commands.runworker.time.sleep",
                side_effect=[None, RuntimeError("stop worker")],
            ),
            self.assertRaisesMessage(RuntimeError, "stop worker"),
        ):
            worker.handle(once=False, poll=0)

        self.assertEqual(recalculate.call_count, 2)
        stderr.assert_called_once_with(
            "Preventive-maintenance recalculation failed: RuntimeError: database unavailable"
        )
        heartbeat = WorkerHeartbeat.objects.get(name="default")
        self.assertEqual(heartbeat.details["pm_recalculation_error"], "")

    def test_work_order_keeps_package_version_snapshot(self) -> None:
        original = self.package()
        plan = create_maintenance_plan(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            package=original,
            name="Versioned PM",
            triggers=[{"kind": "date", "interval": "30", "last_completed_at": timezone.now()}],
        )
        work = create_work_order(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            plan=plan,
            assigned_to=self.tech,
            summary="PM A",
        )
        transition_work_order(work_order=work, actor=self.supervisor, new_status="Ready")
        transition_work_order(work_order=work, actor=self.tech, new_status="InProgress")
        update_work_order_task(task=work.tasks.get(), actor=self.tech, status="Completed")
        transition_work_order(
            work_order=work,
            actor=self.tech,
            new_status="Completed",
            completion_summary="Version one completed",
        )
        transition_work_order(work_order=work, actor=self.supervisor, new_status="Closed")
        replacement = create_service_package(
            organization=self.org,
            actor=self.supervisor,
            name="PM A",
            tasks=[{"title": "Changed future task", "required": True}],
        )

        work.refresh_from_db()
        plan.refresh_from_db()
        next_work = create_work_order(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            plan=plan,
            assigned_to=self.tech,
            summary="PM A next cycle",
        )
        self.assertEqual(replacement.version, 2)
        self.assertEqual(plan.service_package_id, replacement.pk)
        self.assertEqual(work.service_package_id, original.pk)
        self.assertEqual(work.service_package_snapshot["version"], 1)
        self.assertEqual(work.tasks.get().title, "Change oil")
        self.assertEqual(next_work.service_package_id, replacement.pk)
        self.assertEqual(next_work.service_package_snapshot["version"], 2)
        self.assertEqual(next_work.tasks.get().title, "Changed future task")

    def test_pm_close_uses_recorded_completion_meter_for_next_due(self) -> None:
        reading = MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("1500"),
            observed_at=timezone.now(),
            source="manual",
            created_by=self.manager,
        )
        plan = MaintenancePlan.objects.create(
            organization=self.org,
            asset=self.asset,
            service_package=self.package(),
            name="Mileage PM",
        )
        trigger = MaintenanceTrigger.objects.create(
            organization=self.org,
            plan=plan,
            kind="mileage",
            meter=self.odometer,
            interval=Decimal("500"),
            last_completed_value=Decimal("1000"),
        )
        work = create_work_order(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            plan=plan,
            assigned_to=self.tech,
            summary="Mileage PM",
        )
        transition_work_order(work_order=work, actor=self.supervisor, new_status="Ready")
        transition_work_order(work_order=work, actor=self.tech, new_status="InProgress")
        update_work_order_task(task=work.tasks.get(), actor=self.tech, status="Completed")
        transition_work_order(
            work_order=work,
            actor=self.tech,
            new_status="Completed",
            completion_summary="PM completed",
            completion_meter=reading,
        )
        transition_work_order(work_order=work, actor=self.supervisor, new_status="Closed")

        trigger.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(trigger.last_completed_value, Decimal("1500"))
        self.assertEqual(plan.due_reasons[0]["due_value"], "2000.000")
        self.assertEqual(plan.due_status, "Current")

    def test_pm_close_emits_transactional_idempotent_post_reset_projection(self) -> None:
        self.asset.source_system = "gatorhub"
        self.asset.external_id = str(uuid.uuid4())
        self.asset.full_clean()
        self.asset.save(update_fields=["source_system", "external_id", "updated_at"])
        reading = MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("1500"),
            observed_at=timezone.now(),
            source="manual",
            created_by=self.manager,
        )
        plan = create_maintenance_plan(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            package=self.package("Projection PM"),
            name="Projection PM",
            triggers=[
                {
                    "kind": "mileage",
                    "meter_id": str(self.odometer.pk),
                    "interval": "500",
                    "grace": "100",
                    "last_completed_value": "1000",
                }
            ],
        )
        work = create_work_order(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            plan=plan,
            assigned_to=self.tech,
            summary="Projection PM",
        )
        transition_work_order(work_order=work, actor=self.supervisor, new_status="Ready")
        transition_work_order(work_order=work, actor=self.tech, new_status="InProgress")
        update_work_order_task(task=work.tasks.get(), actor=self.tech, status="Completed")
        transition_work_order(
            work_order=work,
            actor=self.tech,
            new_status="Completed",
            completion_summary="Projection PM completed",
            completion_meter=reading,
        )
        events = OutboxEvent.objects.filter(
            event_type="maintenance.plan_projection_changed",
            resource_id=str(plan.pk),
            payload__work_order_id=str(work.pk),
        )
        self.assertEqual(events.count(), 0)

        with (
            patch("maintenance.services.emit", side_effect=RuntimeError("outbox unavailable")),
            self.assertRaisesMessage(RuntimeError, "outbox unavailable"),
        ):
            transition_work_order(work_order=work, actor=self.supervisor, new_status="Closed")
        work.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(work.status, "Completed")
        self.assertEqual(plan.triggers.get().last_completed_value, Decimal("1000"))
        self.assertFalse(work.close_snapshots.exists())
        self.assertEqual(events.count(), 0)

        client = APIClient()
        client.force_authenticate(self.supervisor)
        operation_id = str(uuid.uuid4())
        url = f"/api/v1/maintenance/work-orders/{work.pk}/transition/"
        first = client.post(
            url,
            {"status": "Closed"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=operation_id,
        )
        replay = client.post(
            url,
            {"status": "Closed"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=operation_id,
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(events.count(), 1)

        event = events.get()
        plan.refresh_from_db()
        trigger = plan.triggers.get()
        self.assertIsNotNone(plan.last_calculated_at)
        assert plan.last_calculated_at is not None
        self.assertEqual(event.resource_type, "MaintenancePlan")
        self.assertEqual(event.resource_id, str(plan.pk))
        self.assertEqual(
            event.payload,
            {
                "schema_version": "1.0",
                "asset_id": str(self.asset.pk),
                "source_system": "gatorhub",
                "external_id": self.asset.external_id,
                "work_order_id": str(work.pk),
                "maintenance_plan_id": str(plan.pk),
                "due_status": "Current",
                "next_due": [
                    {
                        "trigger_id": str(trigger.pk),
                        "kind": "mileage",
                        "status": "Current",
                        "due_value": "2000.000",
                        "grace_until": "2100.000",
                        "unit": "mi",
                    }
                ],
                "calculated_at": plan.last_calculated_at.isoformat(),
            },
        )

    def test_defect_request_and_work_order_lifecycle_is_audited(self) -> None:
        defect = create_defect(
            organization=self.org,
            actor=self.driver,
            asset=self.asset,
            category="brakes",
            description="Brake pedal is soft",
            severity="safety",
        )
        transition_defect(defect=defect, actor=self.supervisor, new_status="Acknowledged")
        request = create_request(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            defect=defect,
            summary="Inspect brake system",
            priority="safety",
        )
        transition_request(request=request, actor=self.supervisor, new_status="Triaged")
        transition_request(request=request, actor=self.supervisor, new_status="Approved")
        work = create_work_order(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            request=request,
            package=self.package("Brake inspection"),
            assigned_to=self.tech,
            summary="Repair brakes",
            priority="safety",
            requires_qc=True,
        )
        transition_work_order(work_order=work, actor=self.supervisor, new_status="Ready")
        transition_work_order(work_order=work, actor=self.tech, new_status="InProgress")
        with self.assertRaisesMessage(DomainError, "Required tasks"):
            transition_work_order(
                work_order=work, actor=self.tech, new_status="QC", completion_summary="Repaired"
            )
        work.refresh_from_db()
        update_work_order_task(task=work.tasks.get(), actor=self.tech, status="Completed")
        transition_work_order(work_order=work, actor=self.tech, new_status="QC")
        transition_work_order(
            work_order=work,
            actor=self.supervisor,
            new_status="Completed",
            completion_summary="Verified repair",
        )
        transition_work_order(work_order=work, actor=self.supervisor, new_status="Closed")

        request.refresh_from_db()
        work.refresh_from_db()
        self.assertEqual(request.status, "Converted")
        self.assertEqual(work.status, "Closed")
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org,
                resource_type="WorkOrder",
                resource_id=str(work.pk),
                new_state="Closed",
            ).exists()
        )

    def test_invalid_state_transition_is_rejected_without_mutation(self) -> None:
        work = create_work_order(
            organization=self.org, actor=self.supervisor, asset=self.asset, summary="Check leak"
        )
        with self.assertRaisesMessage(DomainError, "cannot transition"):
            transition_work_order(work_order=work, actor=self.supervisor, new_status="Closed")
        work.refresh_from_db()
        self.assertEqual(work.status, "Draft")

    def test_work_order_detail_includes_signed_parts_ledger_and_cost(self) -> None:
        from inventory.models import Bin, Part, Warehouse
        from inventory.services import issue_stock, receive_stock, return_stock

        work = create_work_order(
            organization=self.org,
            actor=self.supervisor,
            asset=self.asset,
            summary="Replace filter",
        )
        warehouse = Warehouse.objects.create(
            organization=self.org,
            location=self.location,
            code="MAIN",
            name="Main warehouse",
        )
        bin_record = Bin.objects.create(organization=self.org, warehouse=warehouse, code="A-01")
        part = Part.objects.create(
            organization=self.org,
            number="FILTER-1",
            name="Oil filter",
            default_unit_cost=Decimal("10"),
        )
        receive_stock(
            organization=self.org,
            actor=self.supervisor,
            part=part,
            bin=bin_record,
            quantity="3",
            unit_cost="10",
            operation_id=uuid.uuid4(),
        )
        issue = issue_stock(
            organization=self.org,
            actor=self.supervisor,
            part=part,
            bin=bin_record,
            work_order=work,
            quantity="2",
            operation_id=uuid.uuid4(),
        )
        return_stock(
            organization=self.org,
            actor=self.supervisor,
            original=issue,
            quantity="1",
            operation_id=uuid.uuid4(),
            reason="Unused",
        )
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.get(f"/api/v1/maintenance/work-orders/{work.pk}/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["work_order"]
        self.assertEqual(payload["part_cost"], "10.0000")
        self.assertEqual(
            {entry["type"] for entry in payload["stock_transactions"]}, {"ISSUE", "RETURN"}
        )

    def test_submitted_inspection_is_voided_and_replaced_not_edited(self) -> None:
        template = InspectionTemplate.objects.create(
            organization=self.org,
            created_by=self.supervisor,
            name="Pre-trip",
            questions=[
                {"id": "brakes", "label": "Brakes", "required": True, "safety_critical": True},
                {
                    "id": "pressure",
                    "label": "Oil pressure",
                    "type": "measurement",
                    "required": False,
                },
            ],
        )
        original = create_inspection(
            organization=self.org,
            actor=self.driver,
            asset=self.asset,
            template=template,
            responses=[
                {"question_id": "brakes", "result": "fail", "notes": "Air leak"},
                {
                    "question_id": "pressure",
                    "result": "value",
                    "answer": 10,
                    "abnormal": True,
                    "severity": "high",
                    "finding_description": "Oil pressure below operating range",
                },
            ],
            acknowledgment="Driver attests",
        )
        brake_response = original.responses.get(question_id="brakes")
        brake_finding = original.findings.get(response=brake_response)
        brake_defect = brake_finding.defect
        self.assertNotEqual(brake_response.pk, brake_finding.pk)
        self.assertNotEqual(brake_finding.pk, brake_defect.pk)
        self.assertEqual(brake_finding.inspection_id, original.pk)
        self.assertEqual(brake_finding.asset_id, self.asset.pk)
        self.assertEqual(brake_finding.reported_by_id, self.driver.pk)
        self.assertTrue(brake_finding.safety_related)
        self.assertEqual(brake_defect.inspection_finding_id, brake_finding.pk)
        self.assertEqual(brake_defect.inspection_response_id, brake_response.pk)
        self.assertEqual(original.findings.count(), 2)
        self.assertEqual(original.findings.get(response__question_id="pressure").severity, "high")
        detail = original.to_dict()
        self.assertEqual(len(detail["findings"]), 2)
        self.assertEqual(
            next(item for item in detail["findings"] if item["question_id"] == "brakes")[
                "defect_id"
            ],
            str(brake_defect.pk),
        )
        original.acknowledgment = "silently changed"
        with self.assertRaisesMessage(ValidationError, "immutable"):
            original.save()
        brake_finding.description = "silently changed"
        with self.assertRaisesMessage(ValidationError, "evidence is immutable"):
            brake_finding.save()
        original.refresh_from_db()
        void_inspection_record(
            inspection=original, actor=self.supervisor, reason="Wrong asset selected"
        )
        other_asset = Asset.objects.create(
            organization=self.org,
            asset_type=self.asset_type,
            home_location=self.location,
            unit_number="T-02",
        )
        client = APIClient()
        client.force_authenticate(self.supervisor)
        replacement_payload = {
            "template_id": str(template.pk),
            "replaces_id": str(original.pk),
            "responses": [{"question_id": "brakes", "result": "pass"}],
        }
        mismatch = client.post(
            "/api/v1/maintenance/inspections/",
            {**replacement_payload, "asset_id": str(other_asset.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json()["error"]["code"], "invalid_inspection_replacement")
        replacement_response = client.post(
            "/api/v1/maintenance/inspections/",
            {**replacement_payload, "asset_id": str(self.asset.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(replacement_response.status_code, 201)
        replacement = original.replacement

        original.refresh_from_db()
        self.assertEqual(original.status, "Voided")
        self.assertEqual(replacement.replaces_id, original.pk)
        self.assertEqual(replacement.performed_by_id, self.supervisor.pk)
        self.assertEqual(InspectionFinding.objects.filter(inspection=original).count(), 2)
        self.assertEqual(Defect.objects.filter(inspection_finding__inspection=original).count(), 2)

        other_org = Organization.objects.create(name="Inspection Outsider", slug="inspection-other")
        outsider = User.objects.create_user(username="inspection-outsider", organization=other_org)
        role = Role.objects.create(organization=other_org, slug="supervisor", name="Supervisor")
        outsider.roles.add(role)
        client = APIClient()
        client.force_authenticate(outsider)
        response = client.get(f"/api/v1/maintenance/inspections/{original.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_offline_operation_replay_is_idempotent_and_key_reuse_conflicts(self) -> None:
        operation_id = str(uuid.uuid4())
        attachment = Attachment.objects.create(
            organization=self.org,
            uploader=self.driver,
            resource_type="defect",
            resource_id=operation_id,
            file="staged/test.txt",
            original_name="headlamp.txt",
            content_type="text/plain",
            size=4,
            sha256="0" * 64,
        )
        operation: dict[str, Any] = {
            "operation_id": operation_id,
            "type": "defect.create",
            "attachment_ids": [str(attachment.pk)],
            "payload": {
                "asset_id": str(self.asset.pk),
                "category": "lights",
                "description": "Left headlamp out",
            },
        }
        first = sync_field_operation(self.driver, operation)
        second = sync_field_operation(self.driver, operation)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(Defect.objects.filter(organization=self.org, category="lights").count(), 1)
        attachment.refresh_from_db()
        self.assertEqual(
            (attachment.resource_type, attachment.resource_id), ("Defect", first["id"])
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org,
                resource_type="Attachment",
                resource_id=str(attachment.pk),
                action="attachment.linked",
            ).exists()
        )
        changed = {**operation, "payload": {**operation["payload"], "description": "Different"}}
        with self.assertRaisesMessage(DomainError, "different input"):
            sync_field_operation(self.driver, changed)

    def test_other_organization_cannot_read_work_order(self) -> None:
        work = create_work_order(
            organization=self.org, actor=self.supervisor, asset=self.asset, summary="Private work"
        )
        other_org = Organization.objects.create(name="Other", slug="other")
        outsider = User.objects.create_user(username="outsider", organization=other_org)
        role = Role.objects.create(organization=other_org, slug="supervisor", name="Supervisor")
        outsider.roles.add(role)
        client = APIClient()
        client.force_authenticate(outsider)

        response = client.get(f"/api/v1/maintenance/work-orders/{work.pk}/")

        self.assertEqual(response.status_code, 404)
