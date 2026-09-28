from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from decimal import Decimal

from assets.models import Asset, AssetType, Meter, MeterReading
from core.exceptions import DomainError
from core.models import ApiToken, AuditEvent, Location, Organization, Role, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.test import TestCase
from django.utils import timezone
from inventory.models import Bin, Part, Reservation, StockBalance, Warehouse
from inventory.services import issue_stock, receive_stock, reserve_stock
from rest_framework.test import APIClient

from .models import (
    Defect,
    MaintenancePlan,
    MaintenanceRequest,
    MaintenanceTrigger,
    ServicePackage,
    WorkOrder,
    WorkOrderCloseSnapshot,
    WorkOrderTask,
)
from .services import (
    create_maintenance_plan,
    create_work_order,
    transition_request,
    transition_work_order,
    update_work_order_task,
)


class MaintenanceHardeningTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Hardening Fleet", slug="hardening")
        self.location = Location.objects.create(
            organization=self.organization, name="Main shop", code="MAIN"
        )
        self.asset_type = AssetType.objects.create(organization=self.organization, name="Truck")
        self.supervisor = self._user("hardening-supervisor", "supervisor")
        self.manager = self._user("hardening-manager", "fleet_manager")
        self.technician = self._user("hardening-technician", "technician")
        self.other_technician = self._user("hardening-other-tech", "technician")
        self.asset = self._asset("HARD-01")
        self.package = ServicePackage.objects.create(
            organization=self.organization,
            created_by=self.supervisor,
            name="Routine service",
            tasks=[{"title": "Inspect", "required": True, "sequence": 1}],
        )

    def _user(self, username: str, role_slug: str) -> User:
        role, _ = Role.objects.get_or_create(
            organization=self.organization,
            slug=role_slug,
            defaults={"name": role_slug.replace("_", " ").title()},
        )
        user = User.objects.create_user(username=username, organization=self.organization)
        user.roles.add(role)
        return user

    def _asset(self, unit_number: str) -> Asset:
        return Asset.objects.create(
            organization=self.organization,
            asset_type=self.asset_type,
            home_location=self.location,
            unit_number=unit_number,
        )

    def _client(self, user: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _complete(self, work_order: WorkOrder, *, summary: str = "Work complete") -> None:
        transition_work_order(work_order=work_order, actor=self.supervisor, new_status="Ready")
        transition_work_order(work_order=work_order, actor=self.technician, new_status="InProgress")
        for task in work_order.tasks.all():
            update_work_order_task(task=task, actor=self.technician, status="Completed")
        transition_work_order(
            work_order=work_order,
            actor=self.technician,
            new_status="Completed",
            completion_summary=summary,
        )

    def test_retired_and_archived_assets_reject_new_plans_and_work_orders(self) -> None:
        retired = self._asset("HARD-RET")
        retired.status = Asset.Status.RETIRED
        retired.save(update_fields=["status", "updated_at"])
        archived = self._asset("HARD-ARC")
        archived.archived_at = timezone.now()
        archived.save(update_fields=["archived_at", "updated_at"])
        client = self._client(self.manager)

        for index, asset in enumerate((retired, archived), start=1):
            with self.subTest(asset=asset.unit_number):
                plan_response = client.post(
                    "/api/v1/maintenance/plans/",
                    {
                        "asset_id": str(asset.pk),
                        "service_package_id": str(self.package.pk),
                        "name": f"Blocked plan {index}",
                        "triggers": [{"kind": "date", "interval": "30"}],
                    },
                    format="json",
                )
                self.assertEqual(plan_response.status_code, 409)
                self.assertEqual(plan_response.json()["error"]["code"], "asset_retired")

                work_response = client.post(
                    "/api/v1/maintenance/work-orders/",
                    {"asset_id": str(asset.pk), "summary": "Blocked work"},
                    format="json",
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
                self.assertEqual(work_response.status_code, 409)
                self.assertEqual(work_response.json()["error"]["code"], "asset_retired")

        self.assertFalse(MaintenancePlan.objects.filter(asset__in=[retired, archived]).exists())
        self.assertFalse(WorkOrder.objects.filter(asset__in=[retired, archived]).exists())

    def test_safety_request_defer_and_reject_require_fleet_manager_and_reason(self) -> None:
        for index, target in enumerate(("Deferred", "Rejected"), start=1):
            defect = Defect.objects.create(
                organization=self.organization,
                asset=self.asset,
                reported_by=self.supervisor,
                category="brakes",
                description=f"Safety defect {index}",
                severity="safety",
                safety_related=True,
                status="Acknowledged",
            )
            request = MaintenanceRequest.objects.create(
                organization=self.organization,
                asset=self.asset,
                defect=defect,
                submitted_by=self.supervisor,
                summary=f"Safety request {index}",
                priority="normal",
            )
            transition_request(request=request, actor=self.supervisor, new_status="Triaged")
            path = f"/api/v1/maintenance/requests/{request.pk}/transition/"

            missing_reason = self._client(self.manager).post(
                path,
                {"status": target},
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )
            self.assertEqual(missing_reason.status_code, 400)
            self.assertEqual(missing_reason.json()["error"]["code"], "reason_required")

            denied = self._client(self.supervisor).post(
                path,
                {"status": target, "reason": "Supervisor override attempt"},
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["error"]["code"], "safety_override_required")
            request.refresh_from_db()
            self.assertEqual(request.status, "Triaged")

            approved = self._client(self.manager).post(
                path,
                {"status": target, "reason": "Parts unavailable; manager accepted risk"},
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )
            self.assertEqual(approved.status_code, 200)
            request.refresh_from_db()
            self.assertEqual(request.status, target)
            event = AuditEvent.objects.get(
                resource_type="MaintenanceRequest",
                resource_id=str(request.pk),
                action="maintenance_request.transitioned",
                new_state=target,
            )
            self.assertEqual(event.actor_id, self.manager.pk)
            self.assertTrue(event.context["safety_override"])
            self.assertEqual(event.context["reason"], request.decision_reason)

        token_defect = Defect.objects.create(
            organization=self.organization,
            asset=self.asset,
            reported_by=self.manager,
            category="steering",
            description="Safety request for scoped-token check",
            severity="safety",
            safety_related=True,
            status="Acknowledged",
        )
        token_request = MaintenanceRequest.objects.create(
            organization=self.organization,
            asset=self.asset,
            defect=token_defect,
            submitted_by=self.manager,
            summary="Scoped safety request",
            priority="safety",
        )
        transition_request(request=token_request, actor=self.manager, new_status="Triaged")
        raw_token = b"hardening-maintenance-only-token"
        ApiToken.objects.create(
            organization=self.organization,
            user=self.manager,
            name="Maintenance-only",
            prefix="hardening",
            token_hash=hashlib.sha256(raw_token).hexdigest(),
            scopes=["maintenance.manage"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        token_client = APIClient()
        token_client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token.decode()}")
        denied_token = token_client.post(
            f"/api/v1/maintenance/requests/{token_request.pk}/transition/",
            {"status": "Deferred", "reason": "Scoped token must not override safety"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(denied_token.status_code, 403)
        self.assertEqual(denied_token.json()["error"]["code"], "safety_override_required")

    def test_my_work_is_priority_due_and_stably_ordered_and_hides_unassigned_task(self) -> None:
        today = timezone.localdate()
        created = timezone.now() - timedelta(days=5)

        def assigned(
            summary: str, priority: str, target_offset: int | None, created_offset: int
        ) -> WorkOrder:
            row = create_work_order(
                organization=self.organization,
                actor=self.supervisor,
                asset=self.asset,
                assigned_to=self.technician,
                summary=summary,
                priority=priority,
                target_date=today + timedelta(days=target_offset)
                if target_offset is not None
                else None,
            )
            WorkOrder.objects.filter(pk=row.pk).update(
                created_at=created + timedelta(minutes=created_offset)
            )
            return row

        safety = assigned("Safety", "safety", None, 5)
        high_older = assigned("High older", "high", 2, 1)
        high_newer = assigned("High newer", "high", 2, 2)
        normal = assigned("Normal", "normal", -1, 0)
        low = assigned("Low", "low", -2, 0)
        unassigned = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=self.asset,
            assigned_to=self.other_technician,
            summary="Not mine",
            priority="safety",
        )
        hidden_task = WorkOrderTask.objects.create(
            organization=self.organization, work_order=unassigned, title="Private task"
        )
        client = self._client(self.technician)

        response = client.get("/api/v1/maintenance/work-orders/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in response.json()["work_orders"]],
            [str(safety.pk), str(high_older.pk), str(high_newer.pk), str(normal.pk), str(low.pk)],
        )
        hidden = client.get(
            f"/api/v1/maintenance/work-orders/{unassigned.pk}/tasks/{hidden_task.pk}/"
        )
        self.assertEqual(hidden.status_code, 404)

    def test_overlapping_roles_use_the_union_without_technician_or_driver_narrowing(self) -> None:
        for slug in ("parts_clerk", "driver"):
            role, _ = Role.objects.get_or_create(
                organization=self.organization,
                slug=slug,
                defaults={"name": slug.replace("_", " ").title()},
            )
            self.technician.roles.add(role)
        other_asset = self._asset("HARD-UNION")
        other_work = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=other_asset,
            assigned_to=self.other_technician,
            summary="Union-visible work",
        )
        task = WorkOrderTask.objects.create(
            organization=self.organization,
            work_order=other_work,
            title="Union-visible task",
        )
        client = self._client(self.technician)

        work = client.get("/api/v1/maintenance/work-orders/")
        self.assertEqual(work.status_code, 200)
        self.assertIn(str(other_work.pk), {row["id"] for row in work.json()["work_orders"]})
        self.assertEqual(
            client.get(
                f"/api/v1/maintenance/work-orders/{other_work.pk}/tasks/{task.pk}/"
            ).status_code,
            200,
        )
        search = client.get("/api/v1/search/", {"q": "Union-visible"})
        self.assertIn(str(other_work.pk), {row["id"] for row in search.json()["results"]})

        defect = client.post(
            "/api/v1/maintenance/defects/",
            {
                "asset_id": str(other_asset.pk),
                "category": "other",
                "description": "Overlapping roles retain broader asset access",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(defect.status_code, 201)
        attachment = client.post(
            "/api/v1/attachments/",
            {
                "resource_type": "work_order",
                "resource_id": str(other_work.pk),
                "file": SimpleUploadedFile(
                    "union.txt", b"overlapping role evidence", content_type="text/plain"
                ),
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(attachment.status_code, 201)

    def test_close_releases_remaining_reservation_and_captures_projection(self) -> None:
        warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="HARD",
            name="Hardening stock",
        )
        bin_record = Bin.objects.create(
            organization=self.organization, warehouse=warehouse, code="A-01"
        )
        part = Part.objects.create(
            organization=self.organization,
            number="HARD-PART",
            name="Hardening part",
            default_unit_cost=Decimal("5"),
        )
        receive_stock(
            organization=self.organization,
            actor=self.supervisor,
            part=part,
            bin=bin_record,
            quantity="10",
            unit_cost="5",
            operation_id=uuid.uuid4(),
        )
        work_order = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=self.asset,
            assigned_to=self.technician,
            summary="Use reserved part",
        )
        reservation = reserve_stock(
            organization=self.organization,
            actor=self.supervisor,
            part=part,
            bin=bin_record,
            work_order=work_order,
            quantity="5",
            operation_id=uuid.uuid4(),
        )
        issue_stock(
            organization=self.organization,
            actor=self.technician,
            part=part,
            bin=bin_record,
            work_order=work_order,
            reservation=reservation,
            quantity="2",
            operation_id=uuid.uuid4(),
        )
        self._complete(work_order)

        transition_work_order(work_order=work_order, actor=self.supervisor, new_status="Closed")

        reservation.refresh_from_db()
        balance = StockBalance.objects.get(part=part, bin=bin_record)
        self.assertEqual(reservation.status, Reservation.Status.RELEASED)
        self.assertEqual(reservation.issued_quantity, Decimal("2"))
        self.assertEqual(reservation.released_quantity, Decimal("3"))
        self.assertEqual(balance.quantity_on_hand, Decimal("8"))
        self.assertEqual(balance.quantity_reserved, Decimal("0"))
        self.assertEqual(balance.available_quantity, Decimal("8"))
        snapshot = work_order.close_snapshots.get()
        self.assertEqual(
            snapshot.snapshot["released_reservations"],
            [{"reservation_id": str(reservation.pk), "quantity": "3.000"}],
        )
        self.assertEqual(snapshot.snapshot["reservations"][0]["remaining_quantity"], "0.000")
        self.assertTrue(
            AuditEvent.objects.filter(
                resource_type="Reservation",
                resource_id=str(reservation.pk),
                action="stock.reservation_released",
            ).exists()
        )

    def test_reclose_preserves_snapshots_and_does_not_advance_scheduled_pm_twice(self) -> None:
        meter = Meter.objects.create(
            organization=self.organization,
            asset=self.asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        reading = MeterReading.objects.create(
            organization=self.organization,
            meter=meter,
            value=Decimal("1500"),
            observed_at=timezone.now(),
            source="manual",
            created_by=self.manager,
        )
        plan = create_maintenance_plan(
            organization=self.organization,
            actor=self.manager,
            asset=self.asset,
            package=self.package,
            name="Scheduled reset",
            triggers=[
                {
                    "kind": "mileage",
                    "meter_id": str(meter.pk),
                    "interval": "500",
                    "last_completed_value": "1000",
                    "reset_rule": "scheduled",
                }
            ],
        )
        work_order = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=self.asset,
            plan=plan,
            assigned_to=self.technician,
            summary="Original close contents",
        )
        transition_work_order(work_order=work_order, actor=self.supervisor, new_status="Ready")
        transition_work_order(work_order=work_order, actor=self.technician, new_status="InProgress")
        update_work_order_task(
            task=work_order.tasks.get(), actor=self.technician, status="Completed"
        )
        transition_work_order(
            work_order=work_order,
            actor=self.technician,
            new_status="Completed",
            completion_summary="Original completion",
            completion_meter=reading,
        )
        transition_work_order(work_order=work_order, actor=self.supervisor, new_status="Closed")
        trigger = MaintenanceTrigger.objects.get(plan=plan)
        self.assertEqual(trigger.last_completed_value, Decimal("1500"))

        transition_work_order(
            work_order=work_order,
            actor=self.supervisor,
            new_status="Reopened",
            reason="Amend diagnosis",
        )
        work_order.refresh_from_db()
        amended = self._client(self.supervisor).patch(
            f"/api/v1/maintenance/work-orders/{work_order.pk}/",
            {
                "summary": "Amended close contents",
                "diagnosis": "Corrected diagnosis",
                "base_version": work_order.version,
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(amended.status_code, 200)
        transition_work_order(work_order=work_order, actor=self.technician, new_status="InProgress")
        transition_work_order(
            work_order=work_order,
            actor=self.technician,
            new_status="Completed",
            completion_summary="Amended completion",
            completion_meter=reading,
        )
        transition_work_order(work_order=work_order, actor=self.supervisor, new_status="Closed")

        trigger.refresh_from_db()
        snapshots = list(work_order.close_snapshots.order_by("sequence"))
        self.assertEqual(trigger.last_completed_value, Decimal("1500"))
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0].pk, uuid.uuid5(work_order.pk, "close:1"))
        self.assertEqual(snapshots[1].pk, uuid.uuid5(work_order.pk, "close:2"))
        self.assertEqual(snapshots[0].snapshot["work_order"]["summary"], "Original close contents")
        self.assertEqual(snapshots[1].snapshot["work_order"]["summary"], "Amended close contents")
        self.assertEqual(snapshots[0].snapshot["work_order"]["diagnosis"], "")
        self.assertEqual(snapshots[1].snapshot["work_order"]["diagnosis"], "Corrected diagnosis")
        self.assertTrue(snapshots[0].snapshot["plan_reset_applied"])
        self.assertTrue(snapshots[1].snapshot["plan_reset_applied"])
        self.assertEqual(
            snapshots[0].snapshot["plan_reset_baseline"],
            snapshots[1].snapshot["plan_reset_baseline"],
        )
        detail = self._client(self.supervisor).get(
            f"/api/v1/maintenance/work-orders/{work_order.pk}/"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            [item["sequence"] for item in detail.json()["work_order"]["close_snapshots"]],
            [1, 2],
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action="work_order.close_snapshot_created",
                context__work_order_id=str(work_order.pk),
            ).count(),
            2,
        )

        with self.assertRaises(DatabaseError), transaction.atomic():
            WorkOrderCloseSnapshot.objects.filter(pk=snapshots[0].pk).update(
                snapshot={"tampered": True}
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            WorkOrderCloseSnapshot.objects.filter(pk=snapshots[0].pk).delete()

    def test_service_guard_returns_stable_domain_error(self) -> None:
        self.asset.status = Asset.Status.RETIRED
        self.asset.save(update_fields=["status", "updated_at"])
        with self.assertRaises(DomainError) as caught:
            create_work_order(
                organization=self.organization,
                actor=self.manager,
                asset=self.asset,
                summary="No new work",
            )
        self.assertEqual(caught.exception.code, "asset_retired")
        self.assertEqual(caught.exception.status, 409)
