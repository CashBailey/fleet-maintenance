from __future__ import annotations

from core.models import Organization
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from .models import Asset, AssetType, Component, ComponentInstallation


class ComponentInstallationGuardTests(TransactionTestCase):
    """The PostgreSQL trigger is the final boundary, not the service layer."""

    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Component write-once guard requires PostgreSQL")
        self.organization = Organization.objects.create(name="Guard Fleet", slug="guard-fleet")
        asset_type = AssetType.objects.create(
            organization=self.organization, name="Truck", category="vehicle"
        )
        self.asset = Asset.objects.create(
            organization=self.organization, asset_type=asset_type, unit_number="GUARD-1"
        )
        self.component = Component.objects.create(
            organization=self.organization,
            kind=Component.Kind.ENGINE,
            serial_number="CUM-4567",
        )
        self.installation = ComponentInstallation.objects.create(
            organization=self.organization,
            component=self.component,
            asset=self.asset,
            installed_at=timezone.now(),
        )

    def test_queryset_updates_cannot_rewrite_the_install_half(self) -> None:
        original = self.installation.installed_at
        with self.assertRaises(DatabaseError), transaction.atomic():
            ComponentInstallation.objects.filter(pk=self.installation.pk).update(
                installed_at=timezone.now()
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            ComponentInstallation.objects.filter(pk=self.installation.pk).update(source="tampered")
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.installed_at, original)
        self.assertEqual(self.installation.source, "web")

    def test_deletes_are_rejected(self) -> None:
        with self.assertRaises(DatabaseError), transaction.atomic():
            ComponentInstallation.objects.filter(pk=self.installation.pk).delete()
        self.assertTrue(ComponentInstallation.objects.filter(pk=self.installation.pk).exists())

    def test_the_removal_half_fills_exactly_once(self) -> None:
        ComponentInstallation.objects.filter(pk=self.installation.pk).update(
            removed_at=timezone.now(), removal_reason="Bench test only"
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            ComponentInstallation.objects.filter(pk=self.installation.pk).update(
                removal_reason="Changed my mind"
            )
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.removal_reason, "Bench test only")

    def test_closing_may_not_smuggle_a_change_to_the_install_half(self) -> None:
        with self.assertRaises(DatabaseError), transaction.atomic():
            ComponentInstallation.objects.filter(pk=self.installation.pk).update(
                removed_at=timezone.now(),
                removal_reason="Bench test only",
                asset_id=self.asset.pk,
                source="tampered",
            )
        self.installation.refresh_from_db()
        self.assertIsNone(self.installation.removed_at)
