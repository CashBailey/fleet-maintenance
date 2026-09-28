from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from assets.models import Asset, AssetType
from core.exceptions import DomainError
from core.models import AuditEvent, Organization, Role, User
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from .models import (
    Defect,
    MaintenancePlan,
    MaintenanceRequest,
    ServicePackage,
    WorkOrder,
    WorkOrderTask,
)
from .services import create_work_order, transition_request, transition_work_order


class MaintenanceConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(
            name="Concurrency Fleet", slug="concurrency-fleet"
        )
        role = Role.objects.create(
            organization=self.organization,
            slug="supervisor",
            name="Shop supervisor",
        )
        self.supervisor = User.objects.create_user(
            username="concurrency-supervisor",
            organization=self.organization,
        )
        self.supervisor.roles.add(role)
        asset_type = AssetType.objects.create(
            organization=self.organization,
            name="Truck",
        )
        self.asset = Asset.objects.create(
            organization=self.organization,
            asset_type=asset_type,
            unit_number="RACE-01",
        )
        self.package = ServicePackage.objects.create(
            organization=self.organization,
            name="Annual service",
            tasks=[],
            created_by=self.supervisor,
        )
        self.plan = MaintenancePlan.objects.create(
            organization=self.organization,
            asset=self.asset,
            service_package=self.package,
            name="Annual service",
        )
        self.defect = Defect.objects.create(
            organization=self.organization,
            asset=self.asset,
            reported_by=self.supervisor,
            category="brakes",
            description="Brake warning lamp",
            status="Acknowledged",
        )
        self.api_client = APIClient()
        self.api_client.force_authenticate(self.supervisor)

    def _planned_work_attempt(self, barrier: Barrier) -> str:
        close_old_connections()
        try:
            organization = Organization.objects.get(pk=self.organization.pk)
            actor = User.objects.get(pk=self.supervisor.pk)
            asset = Asset.objects.get(pk=self.asset.pk)
            plan = MaintenancePlan.objects.get(pk=self.plan.pk)
            with transaction.atomic():
                barrier.wait(timeout=10)
                create_work_order(
                    organization=organization,
                    actor=actor,
                    asset=asset,
                    plan=plan,
                    summary="Annual service",
                )
            return "created"
        except DomainError as exc:
            return exc.code
        finally:
            close_old_connections()

    def test_concurrent_planned_work_creates_one_nonterminal_order(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency behavior requires PostgreSQL")

        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self._planned_work_attempt(barrier), range(2)))

        self.assertCountEqual(results, ["created", "duplicate_planned_work"])
        self.assertEqual(
            WorkOrder.objects.filter(maintenance_plan=self.plan)
            .exclude(status__in=["Closed", "Cancelled"])
            .count(),
            1,
        )

    def _defect_request_attempt(self, barrier: Barrier) -> str:
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(User.objects.get(pk=self.supervisor.pk))
            barrier.wait(timeout=10)
            response = client.post(
                f"/api/v1/maintenance/defects/{self.defect.pk}/request/",
                {"summary": "Inspect brake warning"},
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )
            if response.status_code == 201:
                return "created"
            return str(response.json()["error"]["code"])
        finally:
            close_old_connections()

    def test_concurrent_defect_conversion_creates_one_active_request(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency behavior requires PostgreSQL")

        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self._defect_request_attempt(barrier), range(2)))

        self.assertCountEqual(results, ["created", "duplicate_request"])
        self.assertEqual(
            MaintenanceRequest.objects.filter(defect=self.defect)
            .exclude(status__in=["Rejected", "Closed"])
            .count(),
            1,
        )
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "Acknowledged")

    def test_database_rejects_duplicate_active_plan_and_defect_records(self) -> None:
        WorkOrder.objects.create(
            organization=self.organization,
            number="WO-CONSTRAINT-1",
            asset=self.asset,
            maintenance_plan=self.plan,
            created_by=self.supervisor,
            summary="First planned order",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkOrder.objects.create(
                organization=self.organization,
                number="WO-CONSTRAINT-2",
                asset=self.asset,
                maintenance_plan=self.plan,
                created_by=self.supervisor,
                summary="Duplicate planned order",
            )

        MaintenanceRequest.objects.create(
            organization=self.organization,
            asset=self.asset,
            defect=self.defect,
            submitted_by=self.supervisor,
            summary="First defect request",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            MaintenanceRequest.objects.create(
                organization=self.organization,
                asset=self.asset,
                defect=self.defect,
                submitted_by=self.supervisor,
                summary="Duplicate defect request",
            )

    def test_terminal_request_cannot_reactivate_over_replacement(self) -> None:
        original = MaintenanceRequest.objects.create(
            organization=self.organization,
            asset=self.asset,
            defect=self.defect,
            submitted_by=self.supervisor,
            status="Closed",
            summary="Original defect request",
        )
        MaintenanceRequest.objects.create(
            organization=self.organization,
            asset=self.asset,
            defect=self.defect,
            submitted_by=self.supervisor,
            summary="Replacement defect request",
        )

        with self.assertRaises(DomainError) as caught:
            transition_request(
                request=original,
                actor=self.supervisor,
                new_status="Triaged",
                reason="Reconsidering closure",
            )

        self.assertEqual(caught.exception.code, "duplicate_request")
        self.assertEqual(caught.exception.status, 409)
        original.refresh_from_db()
        self.assertEqual(original.status, "Closed")

    def test_closed_planned_work_cannot_reopen_over_replacement(self) -> None:
        original = WorkOrder.objects.create(
            organization=self.organization,
            number="WO-REOPEN-1",
            asset=self.asset,
            maintenance_plan=self.plan,
            created_by=self.supervisor,
            status="Closed",
            summary="Original planned work",
        )
        WorkOrder.objects.create(
            organization=self.organization,
            number="WO-REOPEN-2",
            asset=self.asset,
            maintenance_plan=self.plan,
            created_by=self.supervisor,
            summary="Replacement planned work",
        )

        with self.assertRaises(DomainError) as caught:
            transition_work_order(
                work_order=original,
                actor=self.supervisor,
                new_status="Reopened",
                reason="Additional repair required",
            )

        self.assertEqual(caught.exception.code, "duplicate_planned_work")
        self.assertEqual(caught.exception.status, 409)
        original.refresh_from_db()
        self.assertEqual(original.status, "Closed")

    def test_work_order_patch_requires_idempotency_and_current_base_version(self) -> None:
        work_order = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=self.asset,
            summary="Original summary",
        )
        path = f"/api/v1/maintenance/work-orders/{work_order.pk}/"
        operation_id = str(uuid.uuid4())
        payload = {"summary": "Updated summary", "base_version": 1}

        first = self.api_client.patch(
            path,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=operation_id,
        )
        replay = self.api_client.patch(
            path,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=operation_id,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(first.json()["work_order"]["version"], 2)
        work_order.refresh_from_db()
        self.assertEqual(work_order.summary, "Updated summary")
        self.assertEqual(work_order.version, 2)
        self.assertEqual(
            AuditEvent.objects.filter(
                resource_type="WorkOrder",
                resource_id=str(work_order.pk),
                action="work_order.updated",
            ).count(),
            1,
        )

        stale = self.api_client.patch(
            path,
            {"summary": "Lost update", "base_version": 1},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "sync_conflict")
        self.assertEqual(stale.json()["error"]["details"]["current_version"], 2)
        work_order.refresh_from_db()
        self.assertEqual(work_order.summary, "Updated summary")

        missing_key = self.api_client.patch(
            path,
            {"summary": "No key", "base_version": 2},
            format="json",
        )
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_key_required")

        missing_version = self.api_client.patch(
            path,
            {"summary": "No version"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(missing_version.status_code, 400)
        self.assertEqual(missing_version.json()["error"]["code"], "base_version_required")

    def test_concurrent_work_order_patch_allows_one_version_winner(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency behavior requires PostgreSQL")
        work_order = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=self.asset,
            summary="Concurrent update order",
        )
        path = f"/api/v1/maintenance/work-orders/{work_order.pk}/"
        barrier = Barrier(2)

        def attempt(index: int) -> tuple[int, str]:
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=self.supervisor.pk))
                barrier.wait(timeout=10)
                response = client.patch(
                    path,
                    {"summary": f"Winner {index}", "base_version": 1},
                    format="json",
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
                if response.status_code == 200:
                    return response.status_code, "updated"
                return response.status_code, str(response.json()["error"]["code"])
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))

        self.assertCountEqual(results, [(200, "updated"), (409, "sync_conflict")])
        work_order.refresh_from_db()
        self.assertEqual(work_order.version, 2)
        self.assertIn(work_order.summary, {"Winner 0", "Winner 1"})
        self.assertEqual(
            AuditEvent.objects.filter(
                resource_type="WorkOrder",
                resource_id=str(work_order.pk),
                action="work_order.updated",
            ).count(),
            1,
        )

    def test_task_add_is_idempotent_and_rechecks_terminal_state(self) -> None:
        work_order = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=self.asset,
            summary="Task mutation order",
        )
        WorkOrderTask.objects.create(
            organization=self.organization,
            work_order=work_order,
            title="Existing task",
            sequence=7,
        )
        path = f"/api/v1/maintenance/work-orders/{work_order.pk}/tasks/"

        missing_key = self.api_client.post(path, {"title": "No key"}, format="json")
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_key_required")

        operation_id = str(uuid.uuid4())
        first = self.api_client.post(
            path,
            {"title": "Inspect belt"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=operation_id,
        )
        replay = self.api_client.post(
            path,
            {"title": "Inspect belt"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=operation_id,
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(first.json()["task"]["sequence"], 8)
        self.assertEqual(WorkOrderTask.objects.filter(work_order=work_order).count(), 2)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="work_order_task.created",
                resource_id=first.json()["task"]["id"],
            ).count(),
            1,
        )

        duplicate_sequence = self.api_client.post(
            path,
            {"title": "Duplicate position", "sequence": 7},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(duplicate_sequence.status_code, 409)
        self.assertEqual(
            duplicate_sequence.json()["error"]["code"],
            "duplicate_task_sequence",
        )

        WorkOrder.objects.filter(pk=work_order.pk).update(status="Closed")
        terminal = self.api_client.post(
            path,
            {"title": "Too late"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(terminal.status_code, 409)
        self.assertEqual(terminal.json()["error"]["code"], "work_order_not_editable")
        self.assertEqual(WorkOrderTask.objects.filter(work_order=work_order).count(), 2)

    def test_concurrent_task_add_assigns_distinct_sequences(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency behavior requires PostgreSQL")
        work_order = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=self.asset,
            summary="Concurrent task order",
        )
        WorkOrderTask.objects.create(
            organization=self.organization,
            work_order=work_order,
            title="Existing task",
            sequence=7,
        )
        path = f"/api/v1/maintenance/work-orders/{work_order.pk}/tasks/"
        barrier = Barrier(2)

        def attempt(index: int) -> tuple[int, int]:
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=self.supervisor.pk))
                barrier.wait(timeout=10)
                response = client.post(
                    path,
                    {"title": f"Concurrent task {index}"},
                    format="json",
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
                return response.status_code, response.json()["task"]["sequence"]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))

        self.assertCountEqual(results, [(201, 8), (201, 9)])
        self.assertEqual(
            list(
                WorkOrderTask.objects.filter(work_order=work_order)
                .order_by("sequence")
                .values_list("sequence", flat=True)
            ),
            [7, 8, 9],
        )
