# Component Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track each serviceable major component (engine, transmission, reefer unit, APU, axle, aftertreatment) as its own record, with a write-once installation period for every time it goes on or comes off a truck.

**Architecture:** Two new models in `backend/assets/`. `Component` is identity only (kind, serial, make, model). `ComponentInstallation` is a period row: the install half is written once, the removal half is filled exactly once, and a PostgreSQL trigger rejects every other UPDATE and all DELETEs. A partial unique index enforces one open installation per component, so a serial cannot be on two trucks at once even under concurrent requests. Meter readings are frozen by value at install and removal. Technicians act from the work-order screen; managers may also act from the asset page.

**Tech Stack:** Django 5.2 + DRF, PostgreSQL 16, React 19 + TypeScript (single `frontend/src/App.tsx`), Playwright for E2E.

**Spec:** `docs/superpowers/specs/2026-09-05-component-tracking-design.md` — read it alongside this plan. Prior-art reasoning behind several decisions is in `docs/prior-art.md`.

## Global Constraints

- **The project is not under git.** There are no commit steps anywhere in this plan. Do not run `git` commands.
- **TDD is mandatory.** Write the test, run it, watch it fail for the right reason, then implement. A test that passes the first time you run it proves nothing — go back and check you are testing what you think.
- **ADR 0002:** durable facts are never edited in place. `ComponentInstallation` is append-only apart from the single removal update the trigger permits.
- **ADR 0004:** nothing in this delivery may auto-create a work order.
- **ADR 0007:** legacy `specs.equipment.*.serial_number` keys are left in the JSON, never rewritten. `Component` becomes the source of truth; the UI stops reading and writing the legacy keys. `specs.equipment.engine.type` stays authoritative — the document library reads it.
- **Tenant scoping returns 404, never 403.**
- Backend tests: `make test` (runs `manage.py test` from `backend/`). A live PostgreSQL is required; `scripts/postgres-test-lib.sh` provides `fleetline_provision_postgres`, `fleetline_start_postgres`, `fleetline_stop_postgres` against the bundled `.tools/postgresql-16.15`.
- Lint gates: `ruff` (rules E/F/I/B/S/DJ, 100 columns) and `mypy` (strict on every file except `tests.py`). Both run under `make verify`. `mypy` loads Django settings, so `DJANGO_SECRET_KEY` must be set in the environment when you run it.
- New model fields use `str(...)` for UUIDs in every `to_dict()` and audit context — the codebase convention.
- Error codes are snake_case strings on `DomainError(message, code=..., status=..., details=...)`.

---

## Task 1: Models, migration, backfill and the write-once guard

**Files:**
- Modify: `backend/assets/models.py` (append after `MeterReading`, which ends at line 349)
- Create: `backend/assets/migrations/0004_components.py`
- Create: `backend/assets/test_components_migration.py`
- Create: `backend/assets/test_components_guard.py`

**Interfaces:**
- Consumes: `OrganizationOwnedModel` (`core/models.py:78`), `Asset`, `MeterReading`
- Produces: `Component` (with `Component.Kind` TextChoices), `ComponentInstallation`. Both are imported by every later task. `Component.to_dict()` and `ComponentInstallation.to_dict()` return the shapes given in the spec.

- [x] **Step 1: Write the failing migration test**

Create `backend/assets/test_components_migration.py`:

```python
from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from django.test import TransactionTestCase


class ComponentBackfillTests(TransactionTestCase):
    """The 0004 backfill turns legacy specs serials into Component rows."""

    migrate_from = ("assets", "0003_asset_external_identity")
    migrate_to = ("assets", "0004_components")

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def setUp(self):
        self.apps_before = self._migrate([self.migrate_from])
        Organization = self.apps_before.get_model("core", "Organization")
        AssetType = self.apps_before.get_model("assets", "AssetType")
        Asset = self.apps_before.get_model("assets", "Asset")
        self.org = Organization.objects.create(name="Yard", slug="yard")
        kind = AssetType.objects.create(organization=self.org, name="Truck", code="TRK")
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
                }
            },
        )

    def test_backfill_creates_components_and_skips_placeholders(self):
        apps = self._migrate([self.migrate_to])
        Component = apps.get_model("assets", "Component")
        Installation = apps.get_model("assets", "ComponentInstallation")

        serials = sorted(Component.objects.values_list("kind", "serial_number"))
        self.assertEqual(
            serials, [("engine", "CUM-4567"), ("transmission", "ALLISON-3000-778")]
        )
        self.assertFalse(Component.objects.filter(serial_number="N/A").exists())

        engine = Component.objects.get(kind="engine")
        self.assertEqual(engine.manufacturer, "Cummins")
        self.assertEqual(engine.model, "X15")

        self.assertEqual(Installation.objects.count(), 2)
        row = Installation.objects.get(component=engine)
        self.assertEqual(row.source, "legacy_specs_backfill")
        self.assertIsNone(row.installed_by_id)
        self.assertEqual(row.installed_meters, [])
        self.assertIsNone(row.removed_at)
```

- [x] **Step 2: Run it and watch it fail**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_components_migration -v 2
```

Expected: FAIL — `NodeNotFoundError` or `KeyError: ('assets', '0004_components')`, because the migration does not exist. This is the right failure.

- [x] **Step 3: Add the two models**

Append to `backend/assets/models.py`, after `MeterReading`:

```python
class Component(OrganizationOwnedModel):
    """A serviceable major part tracked by serial number, independent of any asset."""

    class Kind(models.TextChoices):
        ENGINE = "engine", "Engine"
        TRANSMISSION = "transmission", "Transmission"
        REEFER_UNIT = "reefer_unit", "Reefer unit"
        APU = "apu", "APU"
        AXLE = "axle", "Axle"
        AFTERTREATMENT = "aftertreatment", "Aftertreatment"
        OTHER = "other", "Other"

    kind = models.CharField(max_length=30, choices=Kind.choices)
    serial_number = models.CharField(max_length=100)
    manufacturer = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)

    class Meta(OrganizationOwnedModel.Meta):
        ordering = ["kind", "serial_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "kind", "serial_number"],
                name="uniq_component_kind_serial_org",
            ),
            models.CheckConstraint(
                condition=~models.Q(serial_number=""),
                name="component_serial_not_empty",
            ),
        ]
        indexes = [models.Index(fields=["organization", "kind"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.serial_number}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.serial_number = self.serial_number.strip().upper()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise ValidationError("Components are retired, not deleted")

    def to_dict(self) -> dict[str, Any]:
        open_row = next(
            (row for row in self.installations.all() if row.removed_at is None), None
        )
        return {
            "id": str(self.pk),
            "kind": self.kind,
            "kind_label": self.get_kind_display(),
            "serial_number": self.serial_number,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "installed_on": (
                {
                    "installation_id": str(open_row.pk),
                    "asset_id": str(open_row.asset_id),
                    "unit_number": open_row.asset.unit_number,
                    "installed_at": open_row.installed_at.isoformat(),
                }
                if open_row is not None
                else None
            ),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class ComponentInstallation(OrganizationOwnedModel):
    """One period a component spent on one asset. Write-once; removal fills once."""

    component = models.ForeignKey(
        Component, on_delete=models.PROTECT, related_name="installations"
    )
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="component_installations"
    )
    installed_at = models.DateTimeField()
    installed_by = models.ForeignKey(
        "core.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_installs",
    )
    installed_work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_installs",
    )
    installed_meters = models.JSONField(default=list)
    source = models.CharField(max_length=40, default="web")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        "core.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_removals",
    )
    removed_work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_removals",
    )
    removed_meters = models.JSONField(default=list)
    removal_reason = models.TextField(max_length=1000, blank=True)

    class Meta(OrganizationOwnedModel.Meta):
        ordering = ["-installed_at", "-created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(removed_at__isnull=True)
                | models.Q(removed_at__gt=models.F("installed_at")),
                name="component_installation_positive_period",
            ),
            models.UniqueConstraint(
                fields=["component"],
                condition=models.Q(removed_at__isnull=True),
                name="uniq_open_component_installation",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(removed_at__isnull=True, removal_reason="")
                    | (models.Q(removed_at__isnull=False) & ~models.Q(removal_reason=""))
                ),
                name="component_installation_removal_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "asset", "installed_at"]),
            models.Index(fields=["organization", "component", "installed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.component} on {self.asset.unit_number}"

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise ValidationError("Component installations are append-only")
```

The `ordering` tie-break is load-bearing: the backfill gives every equipment section of one asset an identical `installed_at`, so bare `["-installed_at"]` would be nondeterministic on day one.

- [x] **Step 4: Add `clean()` and `to_dict()` to `ComponentInstallation`**

Still in `backend/assets/models.py`, inside `ComponentInstallation`:

```python
    def clean(self) -> None:
        super().clean()
        now = timezone.now()
        if self.installed_at and self.installed_at > now:
            raise ValidationError({"installed_at": "Installed at cannot be in the future"})
        if self.removed_at and self.removed_at > now:
            raise ValidationError({"removed_at": "Removed at cannot be in the future"})
        for name in ("component", "asset", "installed_work_order", "removed_work_order"):
            related = getattr(self, name, None)
            if related is not None and related.organization_id != self.organization_id:
                raise ValidationError({name: "Must belong to the same organization"})
        for name in ("installed_work_order", "removed_work_order"):
            work_order = getattr(self, name, None)
            if work_order is not None and work_order.asset_id != self.asset_id:
                raise ValidationError({name: "Work order must be on the same asset"})
        if self.component_id and self.installed_at:
            # Period-overlap check, copied from DeviceAssetAssociation.clean: a
            # backdated install may not fall inside another period of the same
            # component.
            overlapping = ComponentInstallation.objects.filter(
                component_id=self.component_id
            ).exclude(pk=self.pk)
            end = self.removed_at or datetime.max.replace(tzinfo=dt_timezone.utc)
            for other in overlapping:
                other_end = other.removed_at or datetime.max.replace(tzinfo=dt_timezone.utc)
                if self.installed_at < other_end and other.installed_at < end:
                    raise ValidationError(
                        {"installed_at": "Overlaps an existing installation period"}
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "component_id": str(self.component_id),
            "component": {
                "kind": self.component.kind,
                "kind_label": self.component.get_kind_display(),
                "serial_number": self.component.serial_number,
                "manufacturer": self.component.manufacturer,
                "model": self.component.model,
            },
            "asset_id": str(self.asset_id),
            "asset": {"unit_number": self.asset.unit_number},
            "installed_at": self.installed_at.isoformat(),
            "installed_by_id": str(self.installed_by_id) if self.installed_by_id else None,
            "installed_by": self.installed_by.get_full_name() if self.installed_by else "",
            "installed_work_order_id": (
                str(self.installed_work_order_id) if self.installed_work_order_id else None
            ),
            "installed_work_order_number": (
                self.installed_work_order.number if self.installed_work_order_id else ""
            ),
            "installed_meters": self.installed_meters,
            "source": self.source,
            "removed_at": self.removed_at.isoformat() if self.removed_at else None,
            "removed_by_id": str(self.removed_by_id) if self.removed_by_id else None,
            "removed_by": self.removed_by.get_full_name() if self.removed_by else "",
            "removed_work_order_id": (
                str(self.removed_work_order_id) if self.removed_work_order_id else None
            ),
            "removed_work_order_number": (
                self.removed_work_order.number if self.removed_work_order_id else ""
            ),
            "removed_meters": self.removed_meters,
            "removal_reason": self.removal_reason,
        }
```

Add to the imports at the top of `backend/assets/models.py` whatever is not already there: `from datetime import datetime, timezone as dt_timezone` and `from django.utils import timezone`. Check first — `django.utils.timezone` is very likely already imported.

- [x] **Step 5: Generate the migration skeleton, then write the backfill into it**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py makemigrations assets --name components
```

Then edit `backend/assets/migrations/0004_components.py`. Keep the generated `CreateModel`/`AddConstraint`/`AddIndex` operations. Add this backfill function above `class Migration`, and append `migrations.RunPython(backfill_specs_serials, migrations.RunPython.noop)` after the generated operations:

```python
import uuid

SECTION_KINDS = {
    "engine": "engine",
    "transmission": "transmission",
    "axle": "axle",
    "emissions": "aftertreatment",
    "auxiliary": "other",
}

# ponytail: legacy equipment serials are free text and contain junk. This list is
# what makes the (org, kind, serial) unique key safe — without it two sections
# holding "N/A" become one Component representing two physical parts.
PLACEHOLDERS = {
    "N/A", "NA", "NONE", "N\\A", "-", "--", "0", "00", "000",
    "UNKNOWN", "TBD", "SEE PLATE", "ON PLATE", "?",
}

LEGACY_NAMESPACE = uuid.UUID("6b2a1f4c-0d3e-4a1b-9c8d-7e5f4a3b2c1d")


def backfill_specs_serials(apps, schema_editor):
    Asset = apps.get_model("assets", "Asset")
    Component = apps.get_model("assets", "Component")
    Installation = apps.get_model("assets", "ComponentInstallation")
    AssetStatusEvent = apps.get_model("assets", "AssetStatusEvent")

    for asset in Asset.objects.all().iterator():
        equipment = (asset.specs or {}).get("equipment") or {}
        if not isinstance(equipment, dict):
            continue
        for section, kind in SECTION_KINDS.items():
            values = equipment.get(section)
            if not isinstance(values, dict):
                continue
            raw = values.get("serial_number")
            if not isinstance(raw, str):
                continue
            serial = raw.strip().upper()
            if not serial or serial in PLACEHOLDERS or len(serial) < 4:
                if raw.strip():
                    print(
                        f"  skip {asset.unit_number} {section}: "
                        f"{raw.strip()!r} is not a serial"
                    )
                continue
            component, _created = Component.objects.get_or_create(
                organization_id=asset.organization_id,
                kind=kind,
                serial_number=serial,
                defaults={
                    "manufacturer": str(values.get("manufacturer") or "")[:100],
                    "model": str(values.get("model") or "")[:100],
                },
            )
            if Installation.objects.filter(
                component=component, removed_at__isnull=True
            ).exists():
                other = Installation.objects.filter(
                    component=component, removed_at__isnull=True
                ).first()
                print(
                    f"  skip {asset.unit_number} {section}: serial {serial} is "
                    f"already open on {other.asset.unit_number}"
                )
                continue
            row = Installation(
                id=uuid.uuid5(LEGACY_NAMESPACE, f"{asset.pk}:legacy-component:{section}"),
                organization_id=asset.organization_id,
                component=component,
                asset=asset,
                installed_at=asset.created_at,
                installed_by=None,
                installed_meters=[],
                source="legacy_specs_backfill",
            )
            if asset.archived_at:
                event = (
                    AssetStatusEvent.objects.filter(asset=asset, status="Retired")
                    .order_by("-sequence")
                    .first()
                )
                snapshots = []
                if event is not None:
                    snapshots = (event.context or {}).get("final_meter_readings") or []
                row.removed_at = asset.archived_at
                row.removed_meters = snapshots
                row.removal_reason = (
                    "Asset retired" if snapshots else "Asset retired (legacy backfill)"
                )
            row.save()
```

The `uuid5` namespace constant makes backfilled installation ids deterministic, so re-running the migration on a restored database produces the same ids.

- [x] **Step 6: Run the migration test to verify it passes**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_components_migration -v 2
```

Expected: PASS. If `Component.objects.get_or_create` complains about `save()` not existing on the historical model, remember migration models have no custom `save()` — that is why the backfill normalizes `serial` itself rather than relying on the model.

- [x] **Step 7: Write the failing write-once guard test**

Create `backend/assets/test_components_guard.py`:

```python
from django.db import connection, transaction
from django.db.utils import InternalError
from django.test import TransactionTestCase
from django.utils import timezone

from assets.models import Asset, AssetType, Component, ComponentInstallation
from core.models import Organization


class WriteOnceGuardTests(TransactionTestCase):
    """The PostgreSQL trigger is the final boundary, not the service layer."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("write-once guard is enforced by a PostgreSQL trigger")
        self.org = Organization.objects.create(name="Yard", slug="yard")
        kind = AssetType.objects.create(organization=self.org, name="Truck", code="TRK")
        self.asset = Asset.objects.create(
            organization=self.org, asset_type=kind, unit_number="TRK-012"
        )
        self.component = Component.objects.create(
            organization=self.org, kind=Component.Kind.ENGINE, serial_number="CUM-4567"
        )
        self.row = ComponentInstallation.objects.create(
            organization=self.org,
            component=self.component,
            asset=self.asset,
            installed_at=timezone.now(),
        )

    def test_updating_an_install_field_raises(self):
        with self.assertRaises(InternalError), transaction.atomic():
            ComponentInstallation.objects.filter(pk=self.row.pk).update(
                installed_at=timezone.now()
            )

    def test_deleting_raises(self):
        with self.assertRaises(InternalError), transaction.atomic():
            ComponentInstallation.objects.filter(pk=self.row.pk).delete()

    def test_closing_once_is_allowed_then_frozen(self):
        ComponentInstallation.objects.filter(pk=self.row.pk).update(
            removed_at=timezone.now(), removal_reason="Bench test only"
        )
        with self.assertRaises(InternalError), transaction.atomic():
            ComponentInstallation.objects.filter(pk=self.row.pk).update(
                removal_reason="Changed my mind"
            )
```

- [x] **Step 8: Run it and watch it fail**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_components_guard -v 2
```

Expected: FAIL on all three — no trigger exists yet, so the updates and the delete succeed and `assertRaises` finds nothing raised.

- [x] **Step 9: Add the trigger to the migration**

Append to the `operations` list in `backend/assets/migrations/0004_components.py`, **after** the `RunPython` backfill (the backfill closes retired rows, which the trigger would otherwise reject):

```python
        migrations.RunSQL(
            sql="""
CREATE OR REPLACE FUNCTION assets_component_installation_write_once()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'assets_componentinstallation is append-only'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.removed_at IS NULL
       AND NEW.removed_at IS NOT NULL
       AND ROW(
           NEW.id,
           NEW.organization_id,
           NEW.component_id,
           NEW.asset_id,
           NEW.installed_at,
           NEW.installed_by_id,
           NEW.installed_work_order_id,
           NEW.installed_meters,
           NEW.source,
           NEW.created_at
       ) IS NOT DISTINCT FROM ROW(
           OLD.id,
           OLD.organization_id,
           OLD.component_id,
           OLD.asset_id,
           OLD.installed_at,
           OLD.installed_by_id,
           OLD.installed_work_order_id,
           OLD.installed_meters,
           OLD.source,
           OLD.created_at
       ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'assets_componentinstallation is write-once'
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS component_installation_write_once
    ON assets_componentinstallation;
CREATE TRIGGER component_installation_write_once
    BEFORE UPDATE OR DELETE ON assets_componentinstallation
    FOR EACH ROW EXECUTE FUNCTION assets_component_installation_write_once();
""",
            reverse_sql="""
DROP TRIGGER IF EXISTS component_installation_write_once
    ON assets_componentinstallation;
DROP FUNCTION IF EXISTS assets_component_installation_write_once();
""",
        ),
```

The primary key leads both `ROW()` lists, matching `maintenance/migrations/0004_inspection_immutability_guards.py:21`. A column absent from the comparison is silently freely updatable.

- [x] **Step 10: Run both test files and verify they pass**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_components_guard assets.test_components_migration -v 2
```

Expected: PASS, 4 tests. Output must be pristine apart from the backfill's `skip …` print lines.

- [x] **Step 11: Register both models in the admin**

In `backend/assets/admin.py`, following the conventions already in that file:

```python
@admin.register(Component)
class ComponentAdmin(admin.ModelAdmin):
    list_display = ("serial_number", "kind", "manufacturer", "model", "organization")
    list_filter = ("kind",)
    search_fields = ("serial_number", "manufacturer", "model")


@admin.register(ComponentInstallation)
class ComponentInstallationAdmin(admin.ModelAdmin):
    list_display = ("component", "asset", "installed_at", "removed_at", "source")
    list_filter = ("source",)
    readonly_fields = [field.name for field in ComponentInstallation._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
```

`ComponentInstallation` is a fact table, so admin is read-only — the same convention the other fact tables in this file use.

- [x] **Step 12: Run lint and type checks**

```bash
cd /home/gatorhub/fleet_maint_track && .venv/bin/ruff check backend/assets/ && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  .venv/bin/mypy backend/assets/models.py backend/assets/admin.py
```

Expected: both clean. `test_*.py` files are exempt from mypy but not from ruff.

---

## Task 2: Meter snapshot helpers and `install_component`

**Files:**
- Modify: `backend/assets/services.py` (extract the snapshot literal at ~L660-672, then append the new services)
- Create: `backend/assets/test_components.py`

**Interfaces:**
- Consumes: `Component`, `ComponentInstallation` (Task 1); `DomainError`, `audit()`, `emit()` from `core.services`; `is_active_work_order_assignee` from `maintenance.services`; `has_permission` from `core.permissions`
- Produces:
  - `_meter_snapshot(meter: Meter, reading: MeterReading) -> dict[str, Any]`
  - `_meter_snapshots_as_of(asset: Asset, at: datetime) -> list[dict[str, Any]]`
  - `install_component(*, asset, actor, component=None, kind="", serial_number="", manufacturer="", model="", installed_at=None, work_order=None, source="web") -> ComponentInstallation`

- [x] **Step 1: Write the failing test for the snapshot helper and a plain install**

Create `backend/assets/test_components.py`. This file grows across Tasks 2-4; start with the shared fixture and the first two tests:

```python
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from assets.models import (
    Asset,
    AssetStatusEvent,
    AssetType,
    Component,
    Meter,
    MeterReading,
)
from assets.services import install_component
from core.models import AuditEvent, Organization, Role, User
from django.core.exceptions import ValidationError

from core.services import DomainError


class ComponentServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Yard", slug="yard")
        self.truck_type = AssetType.objects.create(
            organization=self.org, name="Truck", code="TRK"
        )
        self.asset = Asset.objects.create(
            organization=self.org, asset_type=self.truck_type, unit_number="TRK-012"
        )
        self.manager_role = Role.objects.create(
            organization=self.org, name="Fleet manager", permissions=["assets.manage"]
        )
        self.manager = User.objects.create_user(
            username="manager", password="x", organization=self.org, role=self.manager_role
        )
        self.odometer = Meter.objects.create(
            organization=self.org,
            asset=self.asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        self.now = timezone.now()
        self.reading = MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("120500"),
            observed_at=self.now - timedelta(hours=1),
            source="manual",
            quality=MeterReading.Quality.ACCEPTED,
        )

    def test_install_creates_component_and_snapshots_meters(self):
        installation = install_component(
            asset=self.asset,
            actor=self.manager,
            kind="engine",
            serial_number="  cum-4567 ",
            manufacturer="Cummins",
            model="X15",
        )
        component = installation.component
        self.assertEqual(component.serial_number, "CUM-4567")
        self.assertEqual(component.kind, "engine")
        self.assertIsNone(installation.removed_at)
        self.assertEqual(installation.installed_by_id, self.manager.pk)
        self.assertEqual(len(installation.installed_meters), 1)
        snapshot = installation.installed_meters[0]
        self.assertEqual(snapshot["reading_id"], str(self.reading.pk))
        self.assertEqual(snapshot["value"], "120500.00")
        self.assertEqual(snapshot["kind"], Meter.Kind.ODOMETER)
        actions = set(
            AuditEvent.objects.filter(organization=self.org).values_list("action", flat=True)
        )
        self.assertIn("component.created", actions)
        self.assertIn("component.installed", actions)

    def test_install_snapshots_the_reading_as_of_installed_at_not_the_latest(self):
        MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("121000"),
            observed_at=self.now,
            source="manual",
            quality=MeterReading.Quality.ACCEPTED,
        )
        installation = install_component(
            asset=self.asset,
            actor=self.manager,
            kind="engine",
            serial_number="CUM-4567",
            installed_at=self.now - timedelta(minutes=30),
        )
        self.assertEqual(installation.installed_meters[0]["reading_id"], str(self.reading.pk))
```

Check the exact `value` string the retirement snapshot produces before asserting `"120500.00"` — read `assets/services.py` around line 660 and match its formatting exactly. If retirement uses `str(current.value)`, the expected string is whatever `Decimal` renders for that column's `decimal_places`.

- [x] **Step 2: Run it and watch it fail**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_components -v 2
```

Expected: FAIL with `ImportError: cannot import name 'install_component' from 'assets.services'`.

- [x] **Step 3: Extract the meter snapshot helper**

In `backend/assets/services.py`, the retirement branch of `change_asset_status` builds a dict literal at roughly lines 660-672. Extract it verbatim into a module-level function and call it from retirement so behaviour is unchanged:

```python
def _meter_snapshot(meter: Meter, reading: MeterReading) -> dict[str, Any]:
    """Freeze a reading by value. Later corrections must not rewrite a snapshot."""
    return {
        "meter_id": str(meter.pk),
        "meter_name": meter.name,
        "kind": meter.kind,
        "unit": meter.unit,
        "reading_id": str(reading.pk),
        "value": str(reading.value),
        "observed_at": reading.observed_at.isoformat(),
        "source": reading.source,
        "quality": reading.quality,
    }
```

Then replace the literal inside the retirement loop with `retirement_meter_snapshots.append(_meter_snapshot(meter, current))`.

- [x] **Step 4: Run the existing retirement tests to prove the extraction changed nothing**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_retirement -v 2
```

Expected: PASS, unchanged. If anything fails, the extraction is not verbatim — fix it before continuing.

- [x] **Step 5: Add the best-effort snapshot helper**

Below `_meter_snapshot` in `backend/assets/services.py`:

```python
def _meter_snapshots_as_of(asset: Asset, at: datetime) -> list[dict[str, Any]]:
    """Latest accepted, uncorrected reading per active cumulative meter, at or before `at`.

    Best-effort by design: a meter with no qualifying reading is omitted, never
    invented, and never blocks a technician. Retirement stays strict and keeps
    its own required-readings check.
    """
    snapshots: list[dict[str, Any]] = []
    meters = Meter.objects.filter(
        organization=asset.organization,
        asset=asset,
        active=True,
        kind__in=(Meter.Kind.ODOMETER, Meter.Kind.ENGINE_HOURS),
    ).order_by("kind", "name")
    for meter in meters:
        reading = (
            meter.readings.filter(
                quality=MeterReading.Quality.ACCEPTED,
                correction__isnull=True,
                observed_at__lte=at,
            )
            .order_by("-observed_at", "-received_at")
            .first()
        )
        if reading is not None:
            snapshots.append(_meter_snapshot(meter, reading))
    return snapshots
```

Verify the field names against `backend/assets/models.py` before running: `Meter.active`, the `readings` related name, `MeterReading.correction` (the self-FK marking a superseded reading) and `received_at` must all exist with those exact names. Fix the query to match the model, not this snippet, if they differ.

- [x] **Step 6: Add `install_component`**

Append to `backend/assets/services.py`:

```python
def install_component(
    *,
    asset: Asset,
    actor: User,
    component: Component | None = None,
    kind: str = "",
    serial_number: str = "",
    manufacturer: str = "",
    model: str = "",
    installed_at: datetime | None = None,
    work_order: Any = None,
    source: str = "web",
) -> ComponentInstallation:
    """Put a component on an asset. One open installation per component, enforced by the DB."""
    now = timezone.now()
    installed_at = installed_at or now
    if timezone.is_naive(installed_at):
        raise DomainError("installed_at must be timezone aware", code="invalid_installed_at")
    if installed_at > now:
        raise DomainError("installed_at cannot be in the future", code="invalid_installed_at")

    with transaction.atomic():
        # Lock order Asset -> Component -> installations, matching
        # associate_device_to_asset, so this cannot deadlock against retirement.
        locked_asset = Asset.objects.select_for_update().get(pk=asset.pk)
        if locked_asset.status == Asset.Status.RETIRED or locked_asset.archived_at:
            raise DomainError(
                "Retired assets cannot take components", code="asset_retired", status=409
            )
        component = _resolve_component(
            organization=locked_asset.organization,
            actor=actor,
            component=component,
            kind=kind,
            serial_number=serial_number,
            manufacturer=manufacturer,
            model=model,
        )
        work_order = _validate_component_work_order(
            asset=locked_asset, actor=actor, work_order=work_order
        )
        open_row = (
            ComponentInstallation.objects.select_for_update()
            .filter(component=component, removed_at__isnull=True)
            .select_related("asset")
            .first()
        )
        if open_row is not None:
            raise _installed_elsewhere(open_row)
        installation = ComponentInstallation(
            organization=locked_asset.organization,
            component=component,
            asset=locked_asset,
            installed_at=installed_at,
            installed_by=actor,
            installed_work_order=work_order,
            installed_meters=_meter_snapshots_as_of(locked_asset, installed_at),
            source=source,
        )
        installation.full_clean(exclude=["removal_reason"])
        try:
            installation.save()
        except IntegrityError as exc:
            if "uniq_open_component_installation" not in str(exc):
                raise
            # Race path: another request opened an installation between the
            # pre-check and the insert. Re-read so the 409 is the same shape.
            raced = (
                ComponentInstallation.objects.filter(
                    component=component, removed_at__isnull=True
                )
                .select_related("asset")
                .first()
            )
            if raced is None:
                raise
            raise _installed_elsewhere(raced) from exc

        audit(
            organization=locked_asset.organization,
            actor=actor,
            action="component.installed",
            resource=installation,
            new_state=locked_asset.unit_number,
            context={
                "component_id": str(component.pk),
                "asset_id": str(locked_asset.pk),
                "work_order_id": str(work_order.pk) if work_order else None,
                "installed_meters": installation.installed_meters,
            },
        )
        emit(
            organization=locked_asset.organization,
            event_type="component.installed",
            resource=installation,
            payload={
                "component_id": str(component.pk),
                "asset_id": str(locked_asset.pk),
                "installation_id": str(installation.pk),
            },
        )
    return installation


def _installed_elsewhere(open_row: ComponentInstallation) -> DomainError:
    return DomainError(
        "That component is already installed on another asset",
        code="component_installed_elsewhere",
        status=409,
        details={
            "asset_id": str(open_row.asset_id),
            "unit_number": open_row.asset.unit_number,
            "installation_id": str(open_row.pk),
        },
    )


def _resolve_component(
    *,
    organization: Organization,
    actor: User,
    component: Component | None,
    kind: str,
    serial_number: str,
    manufacturer: str,
    model: str,
) -> Component:
    if component is not None:
        if component.organization_id != organization.pk:
            raise DomainError("Unknown component", code="invalid_component")
        return component
    if not kind or not serial_number.strip():
        raise DomainError(
            "kind and serial_number are required", code="component_identity_required"
        )
    if kind not in Component.Kind.values:
        raise DomainError("Unknown component kind", code="invalid_component_kind")
    resolved, created = Component.objects.get_or_create(
        organization=organization,
        kind=kind,
        serial_number=serial_number.strip().upper(),
        defaults={"manufacturer": manufacturer[:100], "model": model[:100]},
    )
    if created:
        audit(
            organization=organization,
            actor=actor,
            action="component.created",
            resource=resolved,
            new_state=resolved.serial_number,
            context={"kind": resolved.kind},
        )
    return resolved
```

Add `IntegrityError` to the `django.db` imports at the top of the file if it is not already there.

- [x] **Step 7: Add the work-order and authorization helper**

Still in `backend/assets/services.py`. This is the rule from spec decision 5 — a technician must work through an open work order they are assigned to; a manager may act freely:

```python
def _validate_component_work_order(*, asset: Asset, actor: User, work_order: Any) -> Any:
    """Technicians act only through an assigned open work order; managers act freely."""
    from maintenance.models import WorkOrder
    from maintenance.services import is_active_work_order_assignee

    is_manager = has_permission(actor, "assets.manage") or has_permission(
        actor, "maintenance.manage"
    )
    if work_order is not None:
        if work_order.organization_id != asset.organization_id:
            raise DomainError("Unknown work order", code="invalid_work_order")
        if work_order.asset_id != asset.pk:
            raise DomainError(
                "Work order is not on this asset", code="invalid_work_order"
            )
        if work_order.status in (
            WorkOrder.Status.COMPLETED,
            WorkOrder.Status.CLOSED,
            WorkOrder.Status.CANCELLED,
        ):
            raise DomainError(
                "Work order is not open", code="work_order_not_open", status=409
            )
    if is_manager:
        return work_order
    if not has_permission(actor, "maintenance.execute"):
        raise DomainError(
            "Not allowed to change components", code="permission_denied", status=403
        )
    if work_order is None:
        raise DomainError(
            "A work order is required", code="work_order_required", status=403
        )
    if not is_active_work_order_assignee(work_order, actor):
        raise DomainError(
            "Not assigned to this work order", code="permission_denied", status=403
        )
    return work_order
```

Check `WorkOrder.Status` member names against `backend/maintenance/models.py` before running — use whatever that enum actually calls the closed states. Check `has_permission`'s signature too: in views it is called as `has_permission(user, name, auth)`, so confirm the two-argument form is valid for a service-layer call and add the third argument if it is required.

- [x] **Step 8: Run the tests and verify they pass**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_components -v 2
```

Expected: PASS, 2 tests.

- [x] **Step 9: Add the remaining install tests**

Append to `ComponentServiceTests` in `backend/assets/test_components.py`:

```python
    def test_install_of_a_component_open_elsewhere_is_rejected(self):
        other = Asset.objects.create(
            organization=self.org, asset_type=self.truck_type, unit_number="TRK-007"
        )
        install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=other, actor=self.manager, kind="engine", serial_number="CUM-4567"
            )
        error = caught.exception
        self.assertEqual(error.code, "component_installed_elsewhere")
        self.assertEqual(error.status, 409)
        self.assertEqual(error.details["unit_number"], "TRK-012")
        self.assertEqual(Component.objects.get(serial_number="CUM-4567").installations.count(), 1)

    def test_future_installed_at_is_rejected(self):
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.manager,
                kind="engine",
                serial_number="CUM-4567",
                installed_at=self.now + timedelta(days=1),
            )
        self.assertEqual(caught.exception.code, "invalid_installed_at")

    def test_backdated_install_inside_a_closed_period_is_rejected(self):
        """The period-overlap check copied from DeviceAssetAssociation.clean."""
        from assets.services import remove_component

        first = install_component(
            asset=self.asset,
            actor=self.manager,
            kind="engine",
            serial_number="CUM-4567",
            installed_at=self.now - timedelta(days=10),
        )
        remove_component(
            component=first.component,
            actor=self.manager,
            reason="Bench test only",
            removed_at=self.now - timedelta(days=5),
        )
        with self.assertRaises(ValidationError):
            install_component(
                asset=self.asset,
                actor=self.manager,
                component=first.component,
                installed_at=self.now - timedelta(days=7),
            )

    def test_identity_is_required_when_no_component_is_given(self):
        with self.assertRaises(DomainError) as caught:
            install_component(asset=self.asset, actor=self.manager, kind="engine")
        self.assertEqual(caught.exception.code, "component_identity_required")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset, actor=self.manager, kind="turbo", serial_number="T-1"
            )
        self.assertEqual(caught.exception.code, "invalid_component_kind")

    def test_install_onto_a_retired_asset_is_rejected(self):
        self.asset.status = Asset.Status.RETIRED
        self.asset.archived_at = timezone.now()
        self.asset.save(update_fields=["status", "archived_at"])
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
            )
        self.assertEqual(caught.exception.code, "asset_retired")

    def test_meter_correction_after_install_leaves_the_snapshot_alone(self):
        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        original = installation.installed_meters[0]
        MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("119000"),
            observed_at=self.reading.observed_at,
            source="manual",
            quality=MeterReading.Quality.ACCEPTED,
            correction=self.reading,
        )
        installation.refresh_from_db()
        self.assertEqual(installation.installed_meters[0], original)
        self.assertEqual(installation.installed_meters[0]["reading_id"], str(self.reading.pk))
```

Check the correction field name — the plan assumes `MeterReading.correction` points at the reading being corrected. If the model names it `corrects`, use that.

- [x] **Step 10: Run, verify each new test fails for the right reason, then confirm all pass**

Run the file. Any test that passes immediately is testing something already true — read it again and make sure it asserts what you meant. Then:

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets -v 2
```

Expected: PASS, whole app green including the pre-existing suites.

- [x] **Step 11: Lint and type check**

```bash
.venv/bin/ruff check backend/assets/ && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  .venv/bin/mypy backend/assets/services.py
```

---

## Task 3: `remove_component` and retirement closure

**Files:**
- Modify: `backend/assets/services.py` (append `remove_component`; extend the RETIRED branch of `change_asset_status`)
- Modify: `backend/assets/test_components.py` (append tests)

**Interfaces:**
- Consumes: everything from Task 2
- Produces: `remove_component(*, component, actor, reason, removed_at=None, work_order=None) -> ComponentInstallation`. The RETIRED branch of `change_asset_status` closes open installations and adds `removed_component_ids: list[str]` to the retirement event context.

- [x] **Step 1: Write the failing removal tests**

Append to `ComponentServiceTests` in `backend/assets/test_components.py`:

```python
    def test_remove_requires_a_reason(self):
        from assets.services import remove_component

        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        with self.assertRaises(DomainError) as caught:
            remove_component(
                component=installation.component, actor=self.manager, reason="   "
            )
        self.assertEqual(caught.exception.code, "reason_required")

    def test_remove_closes_the_period_and_snapshots_meters(self):
        from assets.services import remove_component

        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        closed = remove_component(
            component=installation.component,
            actor=self.manager,
            reason="Bench test only",
        )
        self.assertEqual(closed.pk, installation.pk)
        self.assertIsNotNone(closed.removed_at)
        self.assertEqual(closed.removal_reason, "Bench test only")
        self.assertEqual(closed.removed_by_id, self.manager.pk)
        self.assertEqual(len(closed.removed_meters), 1)

    def test_removing_twice_is_rejected(self):
        from assets.services import remove_component

        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        remove_component(
            component=installation.component, actor=self.manager, reason="Bench test only"
        )
        with self.assertRaises(DomainError) as caught:
            remove_component(
                component=installation.component, actor=self.manager, reason="Again"
            )
        self.assertEqual(caught.exception.code, "component_not_installed")

    def test_reinstall_reuses_the_component_and_adds_a_second_period(self):
        from assets.services import remove_component

        first = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        remove_component(
            component=first.component, actor=self.manager, reason="Bench test only"
        )
        second = install_component(
            asset=self.asset, actor=self.manager, component=first.component
        )
        self.assertEqual(Component.objects.filter(serial_number="CUM-4567").count(), 1)
        self.assertEqual(first.component.installations.count(), 2)
        self.assertNotEqual(first.pk, second.pk)
```

- [x] **Step 2: Run and watch them fail**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_components -v 2
```

Expected: FAIL with `ImportError: cannot import name 'remove_component'`.

- [x] **Step 3: Implement `remove_component`**

Append to `backend/assets/services.py`:

```python
def remove_component(
    *,
    component: Component,
    actor: User,
    reason: str,
    removed_at: datetime | None = None,
    work_order: Any = None,
) -> ComponentInstallation:
    """Close the open installation period. This is the one UPDATE the trigger permits."""
    cleaned = reason.strip()
    if not cleaned:
        raise DomainError("A removal reason is required", code="reason_required")
    now = timezone.now()
    removed_at = removed_at or now
    if timezone.is_naive(removed_at):
        raise DomainError("removed_at must be timezone aware", code="invalid_removed_at")
    if removed_at > now:
        raise DomainError("removed_at cannot be in the future", code="invalid_removed_at")

    with transaction.atomic():
        installation = (
            ComponentInstallation.objects.select_for_update()
            .filter(component=component, removed_at__isnull=True)
            .select_related("asset")
            .first()
        )
        if installation is None:
            raise DomainError(
                "That component is not installed",
                code="component_not_installed",
                status=409,
            )
        if removed_at <= installation.installed_at:
            raise DomainError(
                "removed_at must be after installed_at", code="invalid_removed_at"
            )
        asset = Asset.objects.select_for_update().get(pk=installation.asset_id)
        work_order = _validate_component_work_order(
            asset=asset, actor=actor, work_order=work_order
        )
        installation.removed_at = removed_at
        installation.removed_by = actor
        installation.removed_work_order = work_order
        installation.removed_meters = _meter_snapshots_as_of(asset, removed_at)
        installation.removal_reason = cleaned
        installation.save(
            update_fields=[
                "removed_at",
                "removed_by",
                "removed_work_order",
                "removed_meters",
                "removal_reason",
                "updated_at",
            ]
        )
        audit(
            organization=asset.organization,
            actor=actor,
            action="component.removed",
            resource=installation,
            previous_state=asset.unit_number,
            context={
                "component_id": str(component.pk),
                "asset_id": str(asset.pk),
                "work_order_id": str(work_order.pk) if work_order else None,
                "reason": cleaned,
                "removed_meters": installation.removed_meters,
            },
        )
        emit(
            organization=asset.organization,
            event_type="component.removed",
            resource=installation,
            payload={
                "component_id": str(component.pk),
                "asset_id": str(asset.pk),
                "installation_id": str(installation.pk),
            },
        )
    return installation
```

The `update_fields` list is exactly the set the trigger permits, plus `updated_at`. Adding any other field to that list will make the trigger reject the save.

- [x] **Step 4: Run and verify they pass**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets.test_components -v 2
```

Expected: PASS.

- [x] **Step 5: Write the failing retirement-closure test**

Append to `backend/assets/test_components.py`:

```python
    def test_retiring_an_asset_closes_its_open_installations(self):
        from assets.services import change_asset_status

        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        change_asset_status(
            asset=self.asset,
            actor=self.manager,
            new_status=Asset.Status.RETIRED,
            disposition="Sold at auction",
            final_meter_readings=[
                {"meter_id": str(self.odometer.pk), "reading_id": str(self.reading.pk)}
            ],
        )
        installation.refresh_from_db()
        self.assertIsNotNone(installation.removed_at)
        self.assertEqual(installation.removal_reason, "Asset retired")
        self.assertTrue(installation.removed_meters)
        event = AssetStatusEvent.objects.filter(
            asset=self.asset, status=Asset.Status.RETIRED
        ).latest("sequence")
        self.assertEqual(
            event.context["removed_component_ids"], [str(installation.component_id)]
        )
```

Read `change_asset_status`'s signature in `backend/assets/services.py` and match the retirement call exactly — the `final_meter_readings` shape above is a guess and the real one is whatever `assets/test_retirement.py` already passes. Copy it from there.

- [x] **Step 6: Run and watch it fail**

Expected: FAIL — `installation.removed_at` is still `None`.

- [x] **Step 7: Close open installations in the RETIRED branch**

In `backend/assets/services.py`, inside `change_asset_status`, after `retirement_meter_snapshots` is fully built and after `locked.save(...)`, and before the `context` dict is assembled:

```python
        removed_component_ids: list[str] = []
        if new_status == Asset.Status.RETIRED:
            open_rows = (
                ComponentInstallation.objects.select_for_update()
                .filter(asset=locked, removed_at__isnull=True)
                .select_related("component")
            )
            for row in open_rows:
                row.removed_at = now
                row.removed_by = actor
                row.removed_meters = retirement_meter_snapshots
                row.removal_reason = "Asset retired"
                row.save(
                    update_fields=[
                        "removed_at",
                        "removed_by",
                        "removed_meters",
                        "removal_reason",
                        "updated_at",
                    ]
                )
                removed_component_ids.append(str(row.component_id))
                audit(
                    organization=locked.organization,
                    actor=actor,
                    action="component.removed",
                    resource=row,
                    previous_state=locked.unit_number,
                    context={
                        "component_id": str(row.component_id),
                        "asset_id": str(locked.pk),
                        "reason": "Asset retired",
                        "removed_meters": row.removed_meters,
                    },
                )
```

Then, where the RETIRED branch already does `context.update({...})`, add `"removed_component_ids": removed_component_ids` to that dict.

`source` is deliberately left unchanged on these rows — it records how the installation was *created*, not how it ended.

- [x] **Step 8: Run the retirement tests and the component tests together**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test assets -v 2
```

Expected: PASS. `assets/test_retirement.py` must still be green — if it broke, the closure loop is running at the wrong point in the function.

- [x] **Step 9: Write and run the authorization tests**

Append a second test class to `backend/assets/test_components.py` covering spec decision 5. Build a technician user with a `maintenance.execute` role and a driver with `assets.assigned`, and assert:

```python
class ComponentAuthorizationTests(TestCase):
    """Technicians act through an assigned open work order; managers act freely."""

    # setUp: reuse the fixture shape from ComponentServiceTests, plus a
    # technician (role permissions ["maintenance.execute"]), an open WorkOrder on
    # self.asset, and a WorkOrderAssignment adding the technician.

    def test_technician_without_a_work_order_is_refused(self):
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset, actor=self.technician, kind="engine", serial_number="CUM-4567"
            )
        self.assertEqual(caught.exception.code, "work_order_required")
        self.assertEqual(caught.exception.status, 403)

    def test_technician_with_an_assigned_open_work_order_may_install(self):
        installation = install_component(
            asset=self.asset,
            actor=self.technician,
            kind="engine",
            serial_number="CUM-4567",
            work_order=self.work_order,
        )
        self.assertEqual(installation.installed_work_order_id, self.work_order.pk)

    def test_technician_not_assigned_is_refused(self):
        # remove the assignment, or use a second technician with no assignment
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.other_technician,
                kind="engine",
                serial_number="CUM-4567",
                work_order=self.work_order,
            )
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_a_closed_work_order_is_refused(self):
        self.work_order.status = WorkOrder.Status.CLOSED
        self.work_order.save(update_fields=["status"])
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.technician,
                kind="engine",
                serial_number="CUM-4567",
                work_order=self.work_order,
            )
        self.assertEqual(caught.exception.code, "work_order_not_open")

    def test_a_work_order_on_another_asset_is_refused(self):
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.technician,
                kind="engine",
                serial_number="CUM-4567",
                work_order=self.other_asset_work_order,
            )
        self.assertEqual(caught.exception.code, "invalid_work_order")

    def test_manager_may_install_without_a_work_order(self):
        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        self.assertIsNone(installation.installed_work_order_id)
```

Fill in the `setUp` from the real model signatures — read `maintenance/models.py` for `WorkOrder` and `WorkOrderAssignment` required fields, and copy the creation pattern from an existing maintenance test rather than inventing it.

- [x] **Step 10: Lint and type check**

```bash
.venv/bin/ruff check backend/assets/ && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  .venv/bin/mypy backend/assets/services.py
```

---

## Task 4: `WorkOrderTask.component`

**Files:**
- Modify: `backend/maintenance/models.py` (`WorkOrderTask`), `backend/maintenance/services.py`, `backend/maintenance/views.py`
- Create: `backend/maintenance/migrations/0009_workordertask_component.py`
- Create: `backend/maintenance/test_component_tasks.py`

**Interfaces:**
- Consumes: `Component` (Task 1)
- Produces: `WorkOrderTask.component` (nullable FK, `related_name="work_order_tasks"`); `create_work_order_task(..., component=None)` and `update_work_order_task(..., component=UNSET)`; helper `_component_on_asset(component, asset)`. Task 5's `component_detail` reads `WorkOrderTask.objects.filter(component=...)`.

- [x] **Step 1: Write the failing tests**

Create `backend/maintenance/test_component_tasks.py`:

```python
class ComponentTaskTests(TestCase):
    """A work-order task may point at a component, making service history a query."""

    def test_task_accepts_a_component_installed_on_the_asset(self):
        response = self.client.post(
            f"/api/v1/work-orders/{self.work_order.pk}/tasks/",
            {"title": "Replace filter", "component_id": str(self.component.pk)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["component_id"], str(self.component.pk))
        self.assertEqual(response.json()["component"]["serial_number"], "CUM-4567")

    def test_task_rejects_a_component_open_on_another_asset(self):
        response = self.client.post(
            f"/api/v1/work-orders/{self.work_order.pk}/tasks/",
            {"title": "Replace filter", "component_id": str(self.other_component.pk)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "component_not_on_asset")

    def test_task_accepts_an_uninstalled_component(self):
        """The transmission about to go in is not on the asset yet."""
        response = self.client.post(
            f"/api/v1/work-orders/{self.work_order.pk}/tasks/",
            {"title": "Fit transmission", "component_id": str(self.bench_component.pk)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)

    def test_patch_null_clears_the_component_and_bumps_version(self):
        task = self.work_order.tasks.create(
            organization=self.org, title="Replace filter", component=self.component
        )
        before = self.work_order.version
        response = self.client.patch(
            f"/api/v1/work-orders/{self.work_order.pk}/tasks/{task.pk}/",
            {"component_id": None},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["component_id"])
        task.refresh_from_db()
        self.assertIsNone(task.component_id)
        self.work_order.refresh_from_db()
        self.assertGreater(self.work_order.version, before)

    def test_component_survives_into_the_close_snapshot(self):
        """Additive under snapshot schema_version 1 — no snapshot code changes."""
        self.work_order.tasks.create(
            organization=self.org, title="Replace filter", component=self.component
        )
        self._complete_and_close(self.work_order)
        snapshot = WorkOrderCloseSnapshot.objects.get(work_order=self.work_order)
        task_rows = snapshot.snapshot["tasks"]
        self.assertEqual(task_rows[0]["component_id"], str(self.component.pk))
        self.assertEqual(
            task_rows[0]["component"]["serial_number"], "CUM-4567"
        )
```

Build the fixture by copying an existing work-order test's `setUp` — do not invent
`WorkOrder` field names. `_complete_and_close` is a helper you write in this test class
that walks the work order through whatever transitions `transition_work_order` requires;
copy that sequence from the existing close-snapshot test in `backend/maintenance/tests.py`
rather than guessing the status ladder. Check the task-create signature too — the plan
assumes `work_order.tasks.create(organization=..., title=..., component=...)`; if
`WorkOrderTask` requires more fields, add them.

- [x] **Step 2: Run and watch them fail**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test maintenance.test_component_tasks -v 2
```

Expected: FAIL — the API ignores `component_id`, so the 201 response has no `component_id` key.

- [x] **Step 3: Add the field and migration**

In `backend/maintenance/models.py`, on `WorkOrderTask`:

```python
    component = models.ForeignKey(
        "assets.Component",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_order_tasks",
    )
```

Extend `WorkOrderTask.to_dict()` with:

```python
            "component_id": str(self.component_id) if self.component_id else None,
            "component": (
                {
                    "kind": self.component.kind,
                    "kind_label": self.component.get_kind_display(),
                    "serial_number": self.component.serial_number,
                }
                if self.component_id
                else None
            ),
```

Because `WorkOrderCloseSnapshot` serializes `to_dict()`, the component flows into the snapshot automatically — additive under `schema_version` 1, no snapshot change needed.

Generate the migration:

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py makemigrations maintenance --name workordertask_component
```

Confirm the generated file depends on `assets.0004_components`; add the dependency by hand if `makemigrations` did not infer it.

- [x] **Step 4: Add the service validation**

In `backend/maintenance/services.py`:

```python
def _component_on_asset(component: Any, asset: Any) -> Any:
    """A task may tag a component installed here, or one not installed anywhere."""
    from assets.models import ComponentInstallation

    if component is None:
        return None
    if component.organization_id != asset.organization_id:
        raise DomainError("Unknown component", code="invalid_reference")
    open_row = ComponentInstallation.objects.filter(
        component=component, removed_at__isnull=True
    ).select_related("asset").first()
    if open_row is not None and open_row.asset_id != asset.pk:
        raise DomainError(
            "That component is on another asset",
            code="component_not_on_asset",
            status=409,
            details={"asset_id": str(open_row.asset_id),
                     "unit_number": open_row.asset.unit_number},
        )
    return component
```

Thread `component=None` through `create_work_order_task` and `component=UNSET` through `update_work_order_task` (the codebase's existing sentinel for "not supplied", so an explicit `null` clears the field and an absent key leaves it alone). Both call `_component_on_asset` before saving. Add `component_id` to the accepted-field allow-lists in `backend/maintenance/views.py` for both the POST and PATCH task routes.

- [x] **Step 5: Run and verify they pass**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test maintenance -v 1
```

Expected: PASS, whole maintenance app green.

- [x] **Step 6: Lint and type check**

```bash
.venv/bin/ruff check backend/maintenance/ && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  .venv/bin/mypy backend/maintenance/
```

---

## Task 5: API endpoints

**Files:**
- Modify: `backend/assets/views.py`, `backend/assets/urls.py`
- Modify: `backend/core/reporting.py` (`_EXPORTED_MODELS`)
- Modify: `backend/core/test_schema.py`, `backend/core/test_reporting_export.py`
- Create: `backend/assets/test_components_api.py`

**Interfaces:**
- Consumes: `install_component`, `remove_component` (Tasks 2-3), `_asset_queryset` (`assets/views.py:151`), `idempotent()`
- Produces: four routes named `asset-components`, `component-detail`, `remove-component`, plus `component_installations` on the existing asset history response.

| Method & path | Permission | Response |
|---|---|---|
| `GET /api/v1/assets/{asset_id}/components/` | `_asset_queryset` (assets.view or assigned driver) | `{"installations": [...]}`, newest first |
| `POST /api/v1/assets/{asset_id}/components/` | `assets.manage` \| `maintenance.manage` \| `maintenance.execute` | 201 `{"installation", "component"}` |
| `GET /api/v1/assets/components/{component_id}/` | `assets.view` | `{"component", "installations", "tasks", "work_orders"}` |
| `POST /api/v1/assets/components/{component_id}/remove/` | same trio as POST install | 200 `{"installation"}` |

- [x] **Step 1: Write the failing API tests**

Create `backend/assets/test_components_api.py` with a `TestCase` that logs in as the manager and covers, at minimum:

```python
    def test_list_returns_all_periods_newest_first(self): ...
    def test_post_creates_and_returns_201(self): ...
    def test_post_rejects_unknown_fields(self):
        response = self.client.post(
            f"/api/v1/assets/{self.asset.pk}/components/",
            {"kind": "engine", "serial_number": "CUM-4567", "colour": "red"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "unsupported_fields")
    def test_replaying_the_same_idempotency_key_returns_the_same_installation(self): ...
    def test_remove_returns_200_and_the_closed_installation(self): ...
    def test_detail_includes_installations_and_service_history(self): ...
    def test_another_organization_gets_404_on_detail_list_and_remove(self): ...
    def test_driver_may_read_but_not_write(self): ...
```

Imports this file needs: `install_component` and `remove_component` from
`assets.services`, and the models it asserts against. Copy the client-login and
idempotency-header patterns from an existing API test file — `backend/assets/tests.py` already exercises `Idempotency-Key`.

- [x] **Step 2: Run and watch them fail**

Expected: 404 on every route.

- [x] **Step 3: Add the four views**

In `backend/assets/views.py`, following the existing view conventions in that file (`@api_view`, `DomainError` propagation, `idempotent(request, handler)`):

```python
@api_view(["GET", "POST"])
def asset_components(request: Request, asset_id: str) -> Response:
    asset = get_object_or_404(_asset_queryset(request), pk=asset_id)
    if request.method == "GET":
        rows = (
            ComponentInstallation.objects.filter(organization=asset.organization, asset=asset)
            .select_related("component", "asset", "installed_by", "removed_by",
                            "installed_work_order", "removed_work_order")
        )
        return Response({"installations": [row.to_dict() for row in rows]})

    allowed = {
        "component_id", "kind", "serial_number", "manufacturer", "model",
        "installed_at", "work_order_id",
    }
    unsupported = set(request.data) - allowed
    if unsupported:
        raise DomainError(
            "Unsupported fields", code="unsupported_fields",
            details={"fields": sorted(unsupported)},
        )

    def handler() -> Response:
        component = None
        if request.data.get("component_id"):
            component = get_object_or_404(
                Component.objects.filter(organization=asset.organization),
                pk=request.data["component_id"],
            )
        work_order = None
        if request.data.get("work_order_id"):
            work_order = get_object_or_404(
                WorkOrder.objects.filter(organization=asset.organization),
                pk=request.data["work_order_id"],
            )
        installation = install_component(
            asset=asset,
            actor=request.user,
            component=component,
            kind=str(request.data.get("kind") or ""),
            serial_number=str(request.data.get("serial_number") or ""),
            manufacturer=str(request.data.get("manufacturer") or ""),
            model=str(request.data.get("model") or ""),
            installed_at=_datetime(request.data["installed_at"])
            if request.data.get("installed_at")
            else None,
            work_order=work_order,
        )
        return Response(
            {
                "installation": installation.to_dict(),
                "component": installation.component.to_dict(),
            },
            status=201,
        )

    return idempotent(request, handler)
```

Write `component_detail` and `remove_component_view` in the same style. The service is
`remove_component`; the view is `remove_component_view` so the two names do not collide in
this module. `component_detail` needs `WorkOrder` and `WorkOrderTask` imported from
`maintenance.models` — import them inside the function if `assets/views.py` avoids
module-level cross-app imports (check what the file already does). `component_detail` builds the service history as tagged tasks ∪ install/remove work orders:

```python
@api_view(["GET"])
def component_detail(request: Request, component_id: str) -> Response:
    if not has_permission(request.user, "assets.view", request.auth):
        raise DomainError("Not allowed", code="permission_denied", status=403)
    component = get_object_or_404(
        Component.objects.filter(organization=request.user.organization), pk=component_id
    )
    installations = list(
        component.installations.select_related(
            "asset", "installed_by", "removed_by", "installed_work_order", "removed_work_order"
        )
    )
    tasks = (
        WorkOrderTask.objects.filter(component=component)
        .select_related("work_order", "work_order__asset")
        .order_by("-work_order__created_at")
    )
    work_order_ids = {
        row.installed_work_order_id for row in installations if row.installed_work_order_id
    } | {row.removed_work_order_id for row in installations if row.removed_work_order_id}
    return Response(
        {
            "component": component.to_dict(),
            "installations": [row.to_dict() for row in installations],
            "tasks": [
                {
                    "task_id": str(task.pk),
                    "work_order_id": str(task.work_order_id),
                    "work_order_number": task.work_order.number,
                    "title": task.title,
                    "status": task.status,
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                    "asset_id": str(task.work_order.asset_id),
                    "unit_number": task.work_order.asset.unit_number,
                }
                for task in tasks
            ],
            "work_orders": [
                {"id": str(wo.pk), "number": wo.number, "summary": wo.summary}
                for wo in WorkOrder.objects.filter(pk__in=work_order_ids).order_by("-created_at")
            ],
        }
    )
```

`WorkOrderTask.component` is delivered by Task 4, which runs first, so the `tasks`
block above is complete as written. If you are executing out of order, do Task 4
before this step rather than shipping a stub.

- [x] **Step 4: Wire the URLs**

In `backend/assets/urls.py`, matching the existing path style. The literal `components/` segment must come **before** any `<uuid:asset_id>` catch-all that could shadow it:

```python
    path("assets/<uuid:asset_id>/components/", views.asset_components, name="asset-components"),
    path("assets/components/<uuid:component_id>/", views.component_detail, name="component-detail"),
    path(
        "assets/components/<uuid:component_id>/remove/",
        views.remove_component_view,
        name="remove-component",
    ),
```

- [x] **Step 5: Extend the asset history endpoint**

In the existing `asset_history` view, add `component_installations` to the response and two timeline entry types, `component_installed` and `component_removed`, each with `links.component_id`, optional `links.work_order_id`, and `context.installed_meters` / `context.removed_meters`. Follow the exact shape the other timeline entries in that view already use.

- [x] **Step 5b: Test the history endpoint**

Add to `backend/assets/test_components_api.py`:

```python
    def test_asset_history_includes_installations_and_both_timeline_types(self):
        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        remove_component(
            component=installation.component, actor=self.manager, reason="Bench test only"
        )
        body = self.client.get(f"/api/v1/assets/{self.asset.pk}/history/").json()
        self.assertEqual(len(body["component_installations"]), 1)
        kinds = {entry["type"] for entry in body["timeline"]}
        self.assertIn("component_installed", kinds)
        self.assertIn("component_removed", kinds)
        entry = next(e for e in body["timeline"] if e["type"] == "component_installed")
        self.assertEqual(entry["links"]["component_id"], str(installation.component_id))
        self.assertTrue(entry["context"]["installed_meters"])
```

Match the timeline entry key names (`type`, `links`, `context`) to whatever the existing
entries in that view already use — read the view before asserting.

- [x] **Step 6: Add both models to the export**

In `backend/core/reporting.py`, add to `_EXPORTED_MODELS` after `meter_readings`:

```python
    ("components", Component, frozenset()),
    ("component_installations", ComponentInstallation, frozenset()),
```

Add a test to `backend/core/test_reporting_export.py` asserting both families appear and that the export `schema_version` is still `"1.0"` — additive record families do not bump it.

- [x] **Step 7: Register the new paths in the schema test**

Add the three new paths to the expected-paths list in `backend/core/test_schema.py`, so the auto-added `Idempotency-Key` parameter and the 400/401/403/404/409 responses are asserted the same way every other route is.

- [x] **Step 8: Run the full backend suite**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test -v 1
```

Expected: PASS, whole project green.

- [x] **Step 9: Lint and type check**

```bash
.venv/bin/ruff check backend/ && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  .venv/bin/mypy backend/
```

---

## Task 6: Cross-app additions

Two changes outside `assets/` that this delivery makes cheap and that get
expensive later. Both were verified against the code before being planned.

**Files:**
- Modify: `backend/core/views.py` (`global_search`, lines 903-966)
- Modify: `backend/maintenance/services.py` (`_close_snapshot_payload`, lines 1684-1772)
- Modify: `frontend/src/App.tsx` (`pathFor` at line 1565 and the search field label on 1566)
- Modify: `backend/core/tests.py` (or wherever `global_search` is currently tested), `backend/maintenance/tests.py`

**Interfaces:**
- Consumes: `Component` (Task 1), `_meter_snapshots_as_of` (Task 2)
- Produces: `global_search` emits `{"type": "component", ...}` rows; `WorkOrderCloseSnapshot.snapshot` gains an `asset_meters` key.

- [x] **Step 1: Write the failing search test**

```python
    def test_search_finds_a_component_by_serial(self):
        response = self.client.get("/api/v1/search/", {"q": "CUM-4567"})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        match = next(row for row in rows if row["type"] == "component")
        self.assertEqual(match["label"], "CUM-4567")
        self.assertEqual(match["detail"], "Engine")
```

- [x] **Step 2: Run and watch it fail**

Expected: FAIL with `StopIteration` — no component row is returned.

- [x] **Step 3: Add the component branch to `global_search`**

In `backend/core/views.py`, alongside the existing asset/part/work-order/vendor branches, gated on `assets.view` exactly as the asset branch is:

```python
    if has_permission(request.user, "assets.view", request.auth):
        components = Component.objects.filter(
            organization=org, serial_number__icontains=query
        )[:10]
        results += [
            {
                "type": "component",
                "id": str(x.pk),
                "label": x.serial_number,
                "detail": x.get_kind_display(),
            }
            for x in components
        ]
```

Match the result limit the neighbouring branches use rather than hardcoding 10.

- [x] **Step 4: Add the frontend route and fix the label**

In `frontend/src/App.tsx`, extend the `pathFor` ternary chain on line 1565 with `row.type === "component" ? \`/components/${row.id}\` :` and change the hardcoded `Field` label on the next line from `"Search assets, work orders, parts, and vendors"` to `"Search assets, work orders, parts, vendors, and components"`.

- [x] **Step 5: Write the failing close-snapshot test**

```python
    def test_close_snapshot_freezes_the_asset_meters(self):
        # close a work order with no completion_meter supplied
        snapshot = WorkOrderCloseSnapshot.objects.get(work_order=self.work_order)
        self.assertTrue(snapshot.snapshot["asset_meters"])
        self.assertEqual(
            snapshot.snapshot["asset_meters"][0]["reading_id"], str(self.reading.pk)
        )
```

- [x] **Step 6: Run and watch it fail**

Expected: FAIL with `KeyError: 'asset_meters'`.

- [x] **Step 7: Add the key**

In `backend/maintenance/services.py`, inside `_close_snapshot_payload`, add to the returned `payload` dict:

```python
        "asset_meters": _meter_snapshots_as_of(work_order.asset, closed_at),
```

Import `_meter_snapshots_as_of` from `assets.services` locally inside the function, matching how this file already imports across apps. `closed_at` must be the value the caller is already using for the close; read the function's parameters and use that, do not call `timezone.now()` again.

This is additive under the existing `"schema_version": 1`. Today a non-PM repair closed without `completion_meter` leaves a permanent record with no usage figure at all, while `ComponentInstallation` freezes meters by value on the very same swap.

- [x] **Step 8: Run the full suite, lint and type check**

```bash
cd /home/gatorhub/fleet_maint_track/backend && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  ../.venv/bin/python manage.py test -v 1
.venv/bin/ruff check backend/ && \
  DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 \
  .venv/bin/mypy backend/
```

---

## Task 7: Frontend — asset detail Components panel

**Files:**
- Modify: `frontend/src/App.tsx` (asset detail view, "Engine information" `dl`, "Edit equipment details" form, asset create form)
- Modify: `backend/assets/tests.py` (~L86-143 assert `serial_number` inside specs)

**Interfaces:**
- Consumes: `GET/POST /api/v1/assets/{id}/components/`, `POST /api/v1/assets/components/{id}/remove/` (Task 5)
- Produces: the `Components` panel; `saveEquipment` no longer sends `serial_number`.

- [x] **Step 1: Add the Components panel to asset detail**

Second panel in the split. A collapsed `<details>` with the count in the summary (`"2 installed components"`); never render an empty table. Columns: Component (kind label + make/model), Component serial (a `Link` to `/components/{id}`), Installed, Meter at install (first snapshot as `value unit`, `—` when the list is empty), and a Remove button gated on `!isRetired && (can("assets.manage") || can("maintenance.manage"))` — technicians act from the work-order screen instead.

Remove uses `window.prompt("Reason for removal")` then POSTs, the same pattern `availability()` already uses in this file.

Panel action is a `details.action-details` "Install component" popover containing: SelectField Kind, Field "Component serial number" (required), Make, Model, then a nested `<details><summary>Advanced</summary>` holding "Installed at" (datetime-local) and "Work order number".

Below the open table, a `<details><summary>Past components (n)</summary>` table with Component, Serial, Installed, Removed, Meter at removal, Reason.

- [x] **Step 2: Point the Engine information serial row at the component**

The "Engine information" `dl` keeps Manufacturer / Model / Type from `specs.equipment.engine`. The Serial row now renders the installed engine component's serial as a `Link`, or `—` when there is none.

- [x] **Step 3: Strip legacy serials from the equipment form**

"Edit equipment details" drops the `*_serial_number` inputs, and `saveEquipment` strips `serial_number` from every spread section so re-saving never re-sends the legacy keys. The keys stay in the stored JSON — they are historical, not authoritative (ADR 0007 as amended by ADR 0011).

- [x] **Step 4: Remove the "Engine serial number" field from the asset create form**

The fleet manager installs the engine from the Components panel instead — one popover, no two-POST path that could half-fail.

- [x] **Step 5: Update the backend tests that assert the legacy behaviour**

`backend/assets/tests.py` around lines 86-143 asserts `serial_number` inside `specs`. Change those assertions to match the new behaviour in this same delivery.

- [x] **Step 6: Build and typecheck the frontend**

```bash
cd /home/gatorhub/fleet_maint_track && npm run build && npx tsc --noEmit && npx eslint frontend/src/App.tsx
```

Expected: clean.

---

## Task 8: Frontend — work-order panel, component page, timeline

**Files:**
- Modify: `frontend/src/App.tsx` (work-order detail, new `ComponentDetail` route, timeline links, status tone regex)

**Interfaces:**
- Consumes: `GET /api/v1/assets/components/{id}/` (Task 5), `component_id` on task create/update (Task 4)
- Produces: route `/components/:id`

- [x] **Step 1: Add the "Components on {unit}" panel to work-order detail**

Placed after the Tasks / Work details split. Install and Remove controls gated on
`(can("maintenance.execute") || can("maintenance.manage")) && !/completed|closed|cancelled/i.test(status)`.
Every POST from this panel includes `work_order_id`.

The whole point is the swap flow: Remove (tap) → reason → OK, then Install (tap) → Kind → serial → Install. No ids typed, no meter picked.

- [x] **Step 2: Show the component on task rows and in the task form**

Task rows append `Component: Transmission · ALLISON-3000-778` when set. The "Add work-order task" form gains an optional SelectField "Component" listing the components currently installed on this asset.

- [x] **Step 3: Add the `/components/:id` route**

New `ComponentDetail` component, registered **before** the `*` catch-all route. Title `${kind_label} ${serial_number}`, subtitle make/model, Status chip Installed / Not installed. Two panels: "Installation history" (Asset link, Installed, Meter at install, Removed, Meter at removal, Reason, Work order link) and "Service history" (tagged tasks ∪ install/remove work orders). No nav entry — it is reached from the asset and work-order panels and from search.

- [x] **Step 4: Wire the timeline**

`links.component_id → /components/{id}`, and add `installed → success` to the status tone regex.

- [x] **Step 5: Build and typecheck**

```bash
cd /home/gatorhub/fleet_maint_track && npm run build && npx tsc --noEmit && npx eslint frontend/src/App.tsx
```

---

## Task 9: ADR 0011, documentation and the E2E spec

**Files:**
- Create: `docs/adr/0011-serviceable-component-tracking.md`
- Create: `tests/e2e/component-tracking.spec.ts`
- Modify: `docs/api.md`, `docs/validation-assumptions.md`, `docs/e2e-coverage.md`, `scripts/test-e2e.sh`, `tests/e2e/preventive-maintenance.spec.ts`

- [x] **Step 1: Write ADR 0011**

Header `- Status: accepted`, `- Date: 2026-09-05`. Quote ADR 0007's deferral sentence verbatim and name requirement AST-05 plus the warranty dependency as the trigger that has now been met. Decision text: the plain-English decisions from the spec, plus explicitly:

(a) this is a third shape between `DeviceAssetAssociation` (fully mutable close) and `AssetStatusEvent` (pure facts) — a period row with a DB write-once guard — chosen so the database can enforce one open installation and warranty gets a stable period id;
(b) `assets` may reference `maintenance.WorkOrder` by string FK (`inventory` already crosses into `maintenance`);
(c) transfer is remove-then-install in one transaction — there is no fourth verb;
(d) serial uniqueness is `(organization, kind, serial_number)`; widening the key is a lossless follow-up but **narrowing it to `(organization, serial_number)` is not** — that needs a row merge, and the backfill's placeholder guard is what makes the current key safe;
(e) `Component` is the source of truth for serials; the legacy JSON keys are historical.

Also record that any future column on `ComponentInstallation` requires re-creating the write-once trigger, and that this is why the columns listed under "Columns deliberately not added" in the spec were refused.

- [x] **Step 2: Update the docs**

- `docs/api.md`: new endpoint rows in the assets table; task `component_id`; the export bullet gains components and installations; one sentence stating that additive record families do not bump the export schema version.
- `docs/validation-assumptions.md`: new row "Component identity" — the kind list and `(org, kind, serial)` uniqueness are provisional; evidence needed is roster component serials and manufacturer collisions; note that meter evidence is best-effort at install/remove but strict at retirement.
- `docs/e2e-coverage.md`: a row for `component-tracking.spec.ts`.
- `scripts/test-e2e.sh`: add the spec to `required_specs`.

- [x] **Step 3: Write the E2E spec**

`tests/e2e/component-tracking.spec.ts`. A supervisor creates a work order on seeded TRK-012 via the API and assigns the technician. In a second browser context the technician opens the work order and, from the "Components on TRK-012" panel:

1. installs a transmission — assert 201, `installed_meters.length >= 1`, and `installed_work_order_id` set
2. removes it with reason "Bench test only"
3. replays the captured install request with the same `Idempotency-Key` and gets the same installation id back
4. reinstalls the same serial — one component, two installations

Then the supervisor reloads the asset page, sees the open row and "Past components (1)", opens the component page, and sees two installation rows carrying the work-order number and the reason, with the tagged task under service history. Assert the audit events for the installation carry the technician as actor and `correlation_id == Idempotency-Key`. End with `diagnostics.assertClean()`.

Update `tests/e2e/preventive-maintenance.spec.ts` for the removed engine-serial field on the asset create form.

- [x] **Step 4: Run the full verification**

```bash
cd /home/gatorhub/fleet_maint_track && make verify
```

Expected: green — backend tests, ruff, mypy, frontend build, eslint, and the Playwright suite including the new spec.

---

## Self-review checklist for the executor

Before calling this plan done:

- [x] Every one of the spec's 21 backend tests has a home in some task
- [x] `make verify` is green
- [x] The write-once trigger rejects an update to `installed_at`, rejects a second removal update, and rejects a DELETE — proven by `assets/test_components_guard.py`, not by inspection
- [x] The backfill printed a skip line for every placeholder serial in the seed data
- [x] `assets/test_retirement.py` still passes unchanged in intent
- [x] No `git` command was run
- [x] Every symbol a task calls is defined by an earlier task or already exists in the
      codebase — no task invents a helper another task was supposed to provide
