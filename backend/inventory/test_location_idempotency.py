from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from core.models import AuditEvent, Location, Organization, Role, User
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from .models import Bin, Warehouse


class InventoryLocationCreateTests(TransactionTestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(
            name="Location Create Fleet", slug="location-create-fleet"
        )
        role = Role.objects.create(
            organization=self.organization, slug="parts_clerk", name="Parts clerk"
        )
        self.users = [
            User.objects.create_user(
                username=f"location-clerk-{index}", organization=self.organization
            )
            for index in range(2)
        ]
        for user in self.users:
            user.roles.add(role)
        self.location = Location.objects.create(
            organization=self.organization, name="Main Shop", code="MAIN"
        )

    @staticmethod
    def _client(user: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_warehouse_and_bin_retries_return_the_original_response(self) -> None:
        client = self._client(self.users[0])
        warehouse_key = str(uuid.uuid4())
        warehouse_payload = {
            "location_id": str(self.location.pk),
            "code": "MAIN",
            "name": "Main Warehouse",
        }
        first = client.post(
            "/api/v1/inventory/warehouses/",
            warehouse_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=warehouse_key,
        )
        replay = client.post(
            "/api/v1/inventory/warehouses/",
            warehouse_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=warehouse_key,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(Warehouse.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="warehouse.created").count(), 1)

        bin_key = str(uuid.uuid4())
        bin_payload = {
            "warehouse_id": first.json()["warehouse"]["id"],
            "code": "A-01",
            "name": "Primary",
        }
        first_bin = client.post(
            "/api/v1/inventory/bins/",
            bin_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=bin_key,
        )
        replayed_bin = client.post(
            "/api/v1/inventory/bins/",
            bin_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=bin_key,
        )

        self.assertEqual(first_bin.status_code, 201)
        self.assertEqual(replayed_bin.status_code, 201)
        self.assertEqual(replayed_bin.json(), first_bin.json())
        self.assertEqual(Bin.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="bin.created").count(), 1)

    def test_concurrent_duplicate_warehouse_returns_one_clean_conflict(self) -> None:
        self.assertEqual(connection.vendor, "postgresql")
        barrier = Barrier(2)
        real_create = Warehouse.objects.create

        def synchronized_create(**kwargs: object) -> Warehouse:
            barrier.wait(timeout=10)
            return real_create(**kwargs)

        def attempt(index: int) -> tuple[int, str]:
            close_old_connections()
            try:
                client = self._client(User.objects.get(pk=self.users[index].pk))
                response = client.post(
                    "/api/v1/inventory/warehouses/",
                    {
                        "location_id": str(self.location.pk),
                        "code": "RACE",
                        "name": f"Racing Warehouse {index}",
                    },
                    format="json",
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
                if response.status_code == 201:
                    return response.status_code, "created"
                return response.status_code, str(response.json()["error"]["code"])
            finally:
                close_old_connections()

        with (
            patch.object(Warehouse.objects, "create", new=synchronized_create),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(attempt, range(2)))

        self.assertCountEqual(results, [(201, "created"), (409, "duplicate_warehouse")])
        self.assertEqual(Warehouse.objects.filter(code="RACE").count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="warehouse.created").count(), 1)
