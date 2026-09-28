from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ComponentBackfillTests(TransactionTestCase):
    """The 0004 backfill turns legacy specs serials into Component rows."""

    migrate_from = ("assets", "0003_asset_external_identity")
    migrate_to = ("assets", "0004_components")

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)
        executor.loader.build_graph()
        return executor.loader.project_state(target).apps

    def setUp(self):
        apps = self._migrate([self.migrate_from])
        Organization = apps.get_model("core", "Organization")
        AssetType = apps.get_model("assets", "AssetType")
        Asset = apps.get_model("assets", "Asset")
        self.org = Organization.objects.create(name="Yard", slug="yard")
        kind = AssetType.objects.create(organization=self.org, name="Truck")
        self.truck = Asset.objects.create(
            organization=self.org,
            asset_type=kind,
            unit_number="TRK-012",
            specs={
                "equipment": {
                    "engine": {
                        "serial_number": " cum-4567 ",
                        "manufacturer": "Cummins",
                        "model": "X15",
                    },
                    "transmission": {"serial_number": "ALLISON-3000-778"},
                    "axle": {"serial_number": "N/A"},
                    "emissions": {"serial_number": "?"},
                }
            },
        )

    def tearDown(self):
        self._migrate([self.migrate_to])

    def test_backfill_closes_installations_on_a_retired_asset(self):
        apps = self._migrate([self.migrate_from])
        Organization = apps.get_model("core", "Organization")
        AssetType = apps.get_model("assets", "AssetType")
        Asset = apps.get_model("assets", "Asset")
        AssetStatusEvent = apps.get_model("assets", "AssetStatusEvent")
        from django.utils import timezone

        org = Organization.objects.get(slug="yard")
        kind = AssetType.objects.get(organization=org, name="Truck")
        retired_at = timezone.now()
        scrapped = Asset.objects.create(
            organization=org,
            asset_type=kind,
            unit_number="TRK-099",
            status="Retired",
            archived_at=retired_at,
            specs={"equipment": {"engine": {"serial_number": "DET-60-991"}}},
        )
        AssetStatusEvent.objects.create(
            organization=org,
            asset=scrapped,
            previous_status="Available",
            new_status="Retired",
            reason="Sold at auction",
            context={"final_meter_readings": [{"meter_id": "m1", "value": "500000.000"}]},
        )

        apps = self._migrate([self.migrate_to])
        Installation = apps.get_model("assets", "ComponentInstallation")
        row = Installation.objects.get(asset_id=scrapped.pk)
        self.assertEqual(row.removed_at, retired_at)
        self.assertEqual(row.removal_reason, "Asset retired")
        self.assertEqual(row.removed_meters, [{"meter_id": "m1", "value": "500000.000"}])

    def test_backfill_creates_components_and_skips_placeholders(self):
        apps = self._migrate([self.migrate_to])
        Component = apps.get_model("assets", "Component")
        Installation = apps.get_model("assets", "ComponentInstallation")

        serials = sorted(Component.objects.values_list("kind", "serial_number"))
        self.assertEqual(serials, [("engine", "CUM-4567"), ("transmission", "ALLISON-3000-778")])

        engine = Component.objects.get(kind="engine")
        self.assertEqual(engine.manufacturer, "Cummins")
        self.assertEqual(engine.model, "X15")

        self.assertEqual(Installation.objects.count(), 2)
        row = Installation.objects.get(component=engine)
        self.assertEqual(row.source, "legacy_specs_backfill")
        self.assertIsNone(row.installed_by_id)
        self.assertEqual(row.installed_meters, [])
        self.assertIsNone(row.removed_at)
        self.assertEqual(row.asset_id, self.truck.pk)
