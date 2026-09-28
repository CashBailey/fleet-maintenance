from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar, cast

from assets.models import Asset, AssetType, Meter, MeterReading
from assets.services import create_asset, create_meter, record_meter_reading, update_asset
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction
from integrations.models import Device
from integrations.services import associate_device_to_asset
from inventory.models import Bin, Part, PartCrossReference, Warehouse
from inventory.services import issue_stock, receive_stock, reserve_stock, return_stock
from maintenance.models import (
    Defect,
    InspectionTemplate,
    MaintenancePlan,
    MaintenanceTrigger,
    ServicePackage,
    WorkOrder,
    WorkOrderTask,
)
from maintenance.services import (
    calculate_plan_due,
    create_defect,
    create_labor_entry,
    create_request,
    create_work_order,
    transition_defect,
    transition_request,
    transition_work_order,
    update_work_order_task,
)
from purchasing.models import PurchaseOrder, PurchaseRequest, Receipt, Vendor
from purchasing.services import (
    create_purchase_order,
    create_purchase_request,
    create_vendor,
    post_receipt,
    transition_purchase_order,
)

from core.models import Location, Organization, Role, User
from core.services import audit

SEED_NAMESPACE = uuid.UUID("90ba3477-1b5c-47cd-ad33-dc32c9bd84c1")
SEED_LABOR_END = datetime(2026, 2, 8, 16, 0, tzinfo=UTC)
SEED_LABOR_START = SEED_LABOR_END - timedelta(minutes=35)
SEED_PURCHASE_NEEDED_BY = date(2026, 2, 28)
SEED_PURCHASE_EXPECTED_AT = date(2026, 2, 21)
MFA_SECRET = "JBSWY3DPEHPK3PXP"  # noqa: S105 - documented deterministic E2E fixture
ROLE_NAMES = {
    "driver": "Driver",
    "technician": "Technician",
    "supervisor": "Shop supervisor",
    "parts_clerk": "Parts clerk",
    "purchasing_manager": "Purchasing manager",
    "fleet_manager": "Fleet manager",
    "management": "Management",
    "system_admin": "System administrator",
    "integration_admin": "Integration administrator",
}

ModelT = TypeVar("ModelT", bound=models.Model)


def seed_id(name: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, name)


def upsert(
    model: type[ModelT],
    *,
    seed_name: str,
    lookup: dict[str, Any],
    values: dict[str, Any],
) -> tuple[ModelT, bool]:
    """Keep fixture identities stable without replacing pre-existing primary keys."""
    row, created = cast(
        tuple[ModelT, bool],
        model._default_manager.get_or_create(
            **lookup,
            defaults={"id": seed_id(seed_name), **values},
        ),
    )
    if not created:
        changed = []
        for field, value in values.items():
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed.append(field)
        if changed:
            if hasattr(row, "updated_at"):
                changed.append("updated_at")
            row.save(update_fields=changed)
    return row, created


class Command(BaseCommand):
    help = "Load deterministic demonstration and E2E data (never for production)."

    def handle(self, *args: object, **options: object) -> None:
        if not settings.DEBUG and os.environ.get("FLEETLINE_ALLOW_DEMO_SEED") != "1":
            raise CommandError(
                "Refusing to load demonstration data with DEBUG disabled; "
                "set FLEETLINE_ALLOW_DEMO_SEED=1 only for an isolated test environment."
            )
        password = os.environ.get("E2E_PASSWORD") or "DemoPass123!"
        manager_username = (
            (os.environ.get("E2E_USERNAME") or "fleet.manager@example.com").strip().lower()
        )
        device_token = os.environ.get("E2E_DEVICE_TOKEN") or "e2e-autopi-device-token"
        if not manager_username or len(manager_username) > cast(
            int, User._meta.get_field("username").max_length
        ):
            raise CommandError("E2E_USERNAME must be a valid-length username")
        if len(device_token) > 256:
            raise CommandError("E2E_DEVICE_TOKEN must be at most 256 characters")

        with transaction.atomic():
            organization, users, locations = self._identity(password, manager_username)
            assets, meters = self._assets(organization, users, locations)
            packages, plan = self._preventive_maintenance(
                organization, users["fleet_manager"], assets["pm"], meters["pm_odometer"]
            )
            self._inspection_template(organization, users["supervisor"])
            active_work, closed_work = self._maintenance_history(
                organization,
                users,
                assets,
                packages["current"],
                meters["assigned_odometer"].current_reading,
            )
            parts, bins = self._inventory(organization, users, locations["main"], active_work)
            self._purchasing(organization, users, parts["filter"], bins["a01"])
            self._device(
                organization,
                users["integration_admin"],
                assets["assigned"],
                device_token,
            )
            self._tenant_isolation(password)

        self.stdout.write(
            self.style.SUCCESS(
                "Demo data ready: 2 organizations, 11 users across 9 operational roles, "
                f"PM plan {plan.due_status}, active {active_work.number}, "
                f"closed {closed_work.number}."
            )
        )

    def _identity(
        self, password: str, manager_username: str
    ) -> tuple[Organization, dict[str, User], dict[str, Location]]:
        organization, _ = upsert(
            Organization,
            seed_name="organization:demo",
            lookup={"slug": "gator-fleet"},
            values={
                "name": "Gator Fleet Services",
                "settings": {
                    "timezone": "America/Chicago",
                    "allow_negative_stock": False,
                    "telematics": {
                        "future_timestamp_tolerance_seconds": 300,
                        "history_retention_days": 3650,
                        "max_odometer": "10000000",
                        "max_odometer_per_hour": "120",
                    },
                },
            },
        )
        main, _ = upsert(
            Location,
            seed_name="location:demo:main",
            lookup={"organization": organization, "code": "MAIN"},
            values={"name": "Main Shop", "active": True},
        )
        north, _ = upsert(
            Location,
            seed_name="location:demo:north",
            lookup={"organization": organization, "code": "NORTH"},
            values={"name": "North Yard", "active": True},
        )
        roles = {
            slug: upsert(
                Role,
                seed_name=f"role:demo:{slug}",
                lookup={"organization": organization, "slug": slug},
                values={"name": name},
            )[0]
            for slug, name in ROLE_NAMES.items()
        }
        account_specs = {
            "driver": ("driver@example.com", "Drew", "Driver"),
            "technician": ("technician@example.com", "Taylor", "Technician"),
            "supervisor": ("supervisor@example.com", "Sam", "Supervisor"),
            "parts_clerk": ("parts.clerk@example.com", "Pat", "Parts"),
            "purchasing_manager": (
                "purchasing.manager@example.com",
                "Morgan",
                "Purchasing",
            ),
            "fleet_manager": ("fleet.manager@example.com", "Casey", "Fleet"),
            "management": ("management@example.com", "Alex", "Management"),
            "system_admin": ("system.admin@example.com", "Sidney", "Admin"),
            "integration_admin": (
                "integration.admin@example.com",
                "Indigo",
                "Integration",
            ),
        }
        users = {
            slug: self._user(
                organization=organization,
                location=main,
                role=roles[slug],
                username=username,
                first_name=first_name,
                last_name=last_name,
                password=password,
                privileged=slug in {"system_admin", "integration_admin"},
            )
            for slug, (username, first_name, last_name) in account_specs.items()
        }
        users["purchasing_approver"] = self._user(
            organization=organization,
            location=main,
            role=roles["purchasing_manager"],
            username="purchasing.approver@example.com",
            first_name="Avery",
            last_name="Approver",
            password=password,
        )
        users["technician_two"] = self._user(
            organization=organization,
            location=main,
            role=roles["technician"],
            username="technician.two@example.com",
            first_name="Robin",
            last_name="Technician",
            password=password,
        )
        if manager_username != "fleet.manager@example.com":
            reserved_usernames = {spec[0] for spec in account_specs.values()} | {
                "purchasing.approver@example.com",
                "technician.two@example.com",
            }
            if manager_username in reserved_usernames:
                raise CommandError(
                    "A custom E2E_USERNAME cannot replace another required role account"
                )
            users["e2e_manager"] = self._user(
                organization=organization,
                location=main,
                role=roles["fleet_manager"],
                username=manager_username,
                first_name="E2E",
                last_name="Fleet Manager",
                password=password,
            )
        return organization, users, {"main": main, "north": north}

    def _user(
        self,
        *,
        organization: Organization,
        location: Location,
        role: Role,
        username: str,
        first_name: str,
        last_name: str,
        password: str,
        privileged: bool = False,
    ) -> User:
        user, _ = User.objects.get_or_create(
            username=username,
            defaults={"id": seed_id(f"user:{username}")},
        )
        user.organization = organization
        user.default_location = location
        user.email = username if "@" in username else ""
        user.first_name = first_name
        user.last_name = last_name
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        user.mfa_secret = MFA_SECRET if privileged else ""
        if not user.check_password(password):
            user.set_password(password)
        user.save()
        user.roles.set([role])
        return user

    def _assets(
        self,
        organization: Organization,
        users: dict[str, User],
        locations: dict[str, Location],
    ) -> tuple[dict[str, Asset], dict[str, Meter]]:
        truck_type, _ = upsert(
            AssetType,
            seed_name="asset-type:demo:truck",
            lookup={"organization": organization, "name": "Truck"},
            values={"category": "vehicle"},
        )
        trailer_type, _ = upsert(
            AssetType,
            seed_name="asset-type:demo:trailer",
            lookup={"organization": organization, "name": "Trailer"},
            values={"category": "trailer"},
        )
        assigned = self._asset(
            organization=organization,
            actor=users["fleet_manager"],
            asset_type=truck_type,
            unit_number="TRK-012",
            home_location=locations["main"],
            assigned_driver=users["driver"],
            year=2022,
            make="Freightliner",
            model="Cascadia",
            vin="1FUJHHDR7NLAA0012",
            ownership="Owned",
            specs={"fuel": "diesel", "gvwr_lb": 52000},
        )
        pm_asset = self._asset(
            organization=organization,
            actor=users["fleet_manager"],
            asset_type=truck_type,
            unit_number="TRK-007",
            home_location=locations["north"],
            year=2020,
            make="Peterbilt",
            model="579",
            vin="1NPXLP0X8LD700007",
            ownership="Owned",
            specs={"fuel": "diesel", "gvwr_lb": 80000},
        )
        trailer = self._asset(
            organization=organization,
            actor=users["fleet_manager"],
            asset_type=trailer_type,
            unit_number="TRL-003",
            home_location=locations["main"],
            year=2019,
            make="Great Dane",
            model="Everest",
            serial_number="TRL-DEMO-0003",
            ownership="Owned",
        )
        assigned_odometer = self._meter(
            organization, assigned, users["fleet_manager"], "Odometer", "odometer", "mi"
        )
        assigned_hours = self._meter(
            organization,
            assigned,
            users["fleet_manager"],
            "Engine hours",
            "engine_hours",
            "h",
        )
        pm_odometer = self._meter(
            organization, pm_asset, users["fleet_manager"], "Odometer", "odometer", "mi"
        )
        readings = [
            (assigned_odometer, "78120", datetime(2026, 1, 2, 14, tzinfo=UTC), "trk12-001"),
            (assigned_odometer, "78650", datetime(2026, 2, 2, 14, tzinfo=UTC), "trk12-002"),
            (assigned_hours, "4321.5", datetime(2026, 2, 2, 14, tzinfo=UTC), "trk12-h-001"),
            (pm_odometer, "105200", datetime(2026, 2, 10, 15, tzinfo=UTC), "trk07-001"),
        ]
        for meter, value, observed_at, external_id in readings:
            record_meter_reading(
                meter=meter,
                value=value,
                observed_at=observed_at,
                source="seed",
                actor=users["fleet_manager"],
                external_id=external_id,
                provenance={"fixture": "demo-v1"},
                record_id=seed_id(f"meter-reading:{external_id}"),
            )
        return (
            {"assigned": assigned, "pm": pm_asset, "trailer": trailer},
            {
                "assigned_odometer": assigned_odometer,
                "assigned_hours": assigned_hours,
                "pm_odometer": pm_odometer,
            },
        )

    def _asset(
        self,
        *,
        organization: Organization,
        actor: User,
        asset_type: AssetType,
        unit_number: str,
        home_location: Location,
        assigned_driver: User | None = None,
        **fields: Any,
    ) -> Asset:
        asset = Asset.objects.filter(organization=organization, unit_number=unit_number).first()
        desired = {
            "asset_type": asset_type,
            "home_location": home_location,
            "assigned_driver": assigned_driver,
            **fields,
        }
        if asset is None:
            return create_asset(
                id=seed_id(f"asset:{organization.slug}:{unit_number}"),
                organization=organization,
                actor=actor,
                unit_number=unit_number,
                **desired,
            )
        changed = {key: value for key, value in desired.items() if getattr(asset, key) != value}
        return update_asset(asset=asset, actor=actor, fields=changed) if changed else asset

    @staticmethod
    def _meter(
        organization: Organization,
        asset: Asset,
        actor: User,
        name: str,
        kind: str,
        unit: str,
    ) -> Meter:
        meter = Meter.objects.filter(organization=organization, asset=asset, name=name).first()
        return meter or create_meter(
            organization=organization,
            asset=asset,
            actor=actor,
            name=name,
            kind=kind,
            unit=unit,
            record_id=seed_id(f"meter:{organization.slug}:{asset.unit_number}:{name}"),
        )

    def _preventive_maintenance(
        self,
        organization: Organization,
        actor: User,
        asset: Asset,
        meter: Meter,
    ) -> tuple[dict[str, ServicePackage], MaintenancePlan]:
        v1, created_v1 = upsert(
            ServicePackage,
            seed_name="service-package:pm-b:v1",
            lookup={"organization": organization, "name": "PM B Service", "version": 1},
            values={
                "description": "Initial preventive-maintenance checklist.",
                "tasks": [
                    {
                        "title": "Change engine oil",
                        "instructions": "Record oil grade.",
                        "required": True,
                        "sequence": 1,
                    },
                    {
                        "title": "Replace oil filter",
                        "instructions": "Inspect removed filter.",
                        "required": True,
                        "sequence": 2,
                    },
                ],
                "expected_parts": [{"part_number": "FIL-1001", "quantity": "1"}],
                "expected_labor_minutes": 90,
                "active": False,
                "supersedes": None,
                "created_by": actor,
            },
        )
        v2, created_v2 = upsert(
            ServicePackage,
            seed_name="service-package:pm-b:v2",
            lookup={"organization": organization, "name": "PM B Service", "version": 2},
            values={
                "description": "PM B checklist with brake and fluid inspections.",
                "tasks": [
                    {
                        "title": "Change engine oil",
                        "instructions": "Record oil grade.",
                        "required": True,
                        "sequence": 1,
                    },
                    {
                        "title": "Replace oil filter",
                        "instructions": "Inspect removed filter.",
                        "required": True,
                        "sequence": 2,
                    },
                    {
                        "title": "Inspect brakes and fluids",
                        "instructions": "Record measurements and exceptions.",
                        "required": True,
                        "sequence": 3,
                    },
                ],
                "expected_parts": [{"part_number": "FIL-1001", "quantity": "1"}],
                "expected_labor_minutes": 120,
                "active": True,
                "supersedes": v1,
                "created_by": actor,
            },
        )
        for package, created in ((v1, created_v1), (v2, created_v2)):
            if created:
                audit(
                    organization=organization,
                    actor=actor,
                    action="service_package.version_created",
                    resource=package,
                    context={"version": package.version},
                )
        plan, created = MaintenancePlan.objects.get_or_create(
            organization=organization,
            asset=asset,
            name="5,000 Mile PM B",
            defaults={
                "id": seed_id("maintenance-plan:trk-007:pm-b"),
                "service_package": v2,
            },
        )
        if created:
            MaintenanceTrigger.objects.create(
                id=seed_id("maintenance-trigger:trk-007:pm-b:mileage"),
                organization=organization,
                plan=plan,
                kind="mileage",
                interval=Decimal("5000"),
                grace=Decimal("100"),
                due_soon_threshold=Decimal("500"),
                meter=meter,
                last_completed_value=Decimal("100000"),
                reset_rule="completion",
            )
            audit(
                organization=organization,
                actor=actor,
                action="maintenance_plan.created",
                resource=plan,
                context={"trigger_kinds": ["mileage"]},
            )
        calculate_plan_due(plan)
        return {"original": v1, "current": v2}, plan

    def _inspection_template(self, organization: Organization, actor: User) -> None:
        template, created = upsert(
            InspectionTemplate,
            seed_name="inspection-template:pre-trip:v1",
            lookup={
                "organization": organization,
                "name": "Daily Pre-Trip",
                "version": 1,
            },
            values={
                "description": "Driver pre-trip inspection for commercial vehicles.",
                "questions": [
                    {
                        "id": "brakes",
                        "label": "Brakes operate normally",
                        "type": "pass_fail",
                        "required": True,
                        "safety_critical": True,
                    },
                    {
                        "id": "tires",
                        "label": "Tires and wheels are serviceable",
                        "type": "pass_fail",
                        "required": True,
                        "safety_critical": True,
                    },
                    {
                        "id": "lights",
                        "label": "Lights and signals operate",
                        "type": "pass_fail",
                        "required": True,
                        "safety_critical": False,
                    },
                    {
                        "id": "leaks",
                        "label": "No visible fluid leaks",
                        "type": "pass_fail",
                        "required": True,
                        "safety_critical": False,
                    },
                ],
                "active": True,
                "retention_months": 14,
                "supersedes": None,
                "created_by": actor,
            },
        )
        if created:
            audit(
                organization=organization,
                actor=actor,
                action="inspection_template.version_created",
                resource=template,
                context={"version": 1},
            )

    def _maintenance_history(
        self,
        organization: Organization,
        users: dict[str, User],
        assets: dict[str, Asset],
        package: ServicePackage,
        completion_meter: MeterReading | None,
    ) -> tuple[WorkOrder, WorkOrder]:
        active = WorkOrder.objects.filter(organization=organization, number="WO-DEMO-1001").first()
        if active is None:
            defect = create_defect(
                record_id=seed_id("defect:demo:steering-vibration"),
                organization=organization,
                actor=users["driver"],
                asset=assets["assigned"],
                category="steering",
                description="Steering wheel vibration above 55 mph",
                severity="high",
            )
            transition_defect(defect=defect, actor=users["supervisor"], new_status="Acknowledged")
            request = create_request(
                record_id=seed_id("maintenance-request:demo:steering-vibration"),
                organization=organization,
                actor=users["supervisor"],
                asset=assets["assigned"],
                defect=defect,
                summary="Diagnose steering vibration",
                description="Inspect front suspension, tires, and wheel balance.",
                priority="high",
            )
            transition_request(request=request, actor=users["supervisor"], new_status="Triaged")
            transition_request(request=request, actor=users["fleet_manager"], new_status="Approved")
            active = create_work_order(
                record_id=seed_id("work-order:demo:1001"),
                organization=organization,
                actor=users["supervisor"],
                asset=assets["assigned"],
                request=request,
                package=package,
                assigned_to=users["technician"],
                summary="Diagnose steering vibration",
                complaint="Vibration reported during highway operation.",
                priority="high",
            )
            WorkOrder.objects.filter(pk=active.pk).update(number="WO-DEMO-1001")
            active.refresh_from_db()
            transition_work_order(work_order=active, actor=users["supervisor"], new_status="Ready")
            active.refresh_from_db()

        closed = WorkOrder.objects.filter(organization=organization, number="WO-DEMO-0998").first()
        if closed is None:
            defect = create_defect(
                record_id=seed_id("defect:demo:right-low-beam"),
                organization=organization,
                actor=users["driver"],
                asset=assets["assigned"],
                category="lighting",
                description="Right low-beam headlamp is inoperative",
                severity="medium",
            )
            transition_defect(defect=defect, actor=users["supervisor"], new_status="Acknowledged")
            request = create_request(
                record_id=seed_id("maintenance-request:demo:right-low-beam"),
                organization=organization,
                actor=users["supervisor"],
                asset=assets["assigned"],
                defect=defect,
                summary="Replace right low-beam headlamp",
                priority="normal",
            )
            transition_request(request=request, actor=users["supervisor"], new_status="Triaged")
            transition_request(request=request, actor=users["fleet_manager"], new_status="Approved")
            closed = create_work_order(
                record_id=seed_id("work-order:demo:0998"),
                organization=organization,
                actor=users["supervisor"],
                asset=assets["assigned"],
                request=request,
                assigned_to=users["technician"],
                summary="Replace right low-beam headlamp",
            )
            WorkOrder.objects.filter(pk=closed.pk).update(number="WO-DEMO-0998")
            closed.refresh_from_db()
            task = WorkOrderTask.objects.create(
                id=seed_id("work-order-task:demo:0998:lamp"),
                organization=organization,
                work_order=closed,
                title="Replace and verify right low-beam lamp",
                instructions="Confirm beam aim after installation.",
                sequence=1,
            )
            transition_work_order(work_order=closed, actor=users["supervisor"], new_status="Ready")
            closed.refresh_from_db()
            transition_work_order(
                work_order=closed, actor=users["technician"], new_status="InProgress"
            )
            update_work_order_task(
                task=task,
                actor=users["technician"],
                status="Completed",
                notes="Lamp replaced; beam pattern and operation verified.",
            )
            create_labor_entry(
                record_id=seed_id("labor-entry:demo:0998:lamp"),
                work_order=closed,
                actor=users["technician"],
                technician=users["technician"],
                minutes=35,
                hourly_rate=Decimal("42.50"),
                note="Diagnosis, replacement, and functional check.",
                started_at=SEED_LABOR_START,
                ended_at=SEED_LABOR_END,
            )
            closed.refresh_from_db()
            transition_work_order(
                work_order=closed,
                actor=users["technician"],
                new_status="Completed",
                completion_summary="Replaced failed lamp and confirmed correct operation.",
                completion_meter=completion_meter,
            )
            closed.refresh_from_db()
            transition_work_order(work_order=closed, actor=users["supervisor"], new_status="Closed")
            defect.refresh_from_db()
            transition_defect(defect=defect, actor=users["technician"], new_status="Corrected")
            defect.refresh_from_db()
            transition_defect(defect=defect, actor=users["supervisor"], new_status="Verified")
            defect.refresh_from_db()
            transition_defect(defect=defect, actor=users["supervisor"], new_status="Closed")
            request.refresh_from_db()
            transition_request(request=request, actor=users["supervisor"], new_status="Closed")
            closed.refresh_from_db()

        if not Defect.objects.filter(
            organization=organization,
            description="Hydraulic fluid seepage at left rear lift gate",
        ).exists():
            create_defect(
                record_id=seed_id("defect:demo:hydraulic-seepage"),
                organization=organization,
                actor=users["driver"],
                asset=assets["assigned"],
                category="fluid_leak",
                description="Hydraulic fluid seepage at left rear lift gate",
                severity="medium",
            )
        return active, closed

    def _inventory(
        self,
        organization: Organization,
        users: dict[str, User],
        location: Location,
        active_work: WorkOrder,
    ) -> tuple[dict[str, Part], dict[str, Bin]]:
        warehouse, _ = upsert(
            Warehouse,
            seed_name="warehouse:demo:main",
            lookup={"organization": organization, "code": "MAIN"},
            values={"location": location, "name": "Main Parts Room", "active": True},
        )
        a01, _ = upsert(
            Bin,
            seed_name="bin:demo:main:a01",
            lookup={"warehouse": warehouse, "code": "A-01"},
            values={"organization": organization, "name": "Filters", "active": True},
        )
        b02, _ = upsert(
            Bin,
            seed_name="bin:demo:main:b02",
            lookup={"warehouse": warehouse, "code": "B-02"},
            values={"organization": organization, "name": "Brake components", "active": True},
        )
        parts: dict[str, Part] = {}
        for key, number, name, manufacturer, manufacturer_number, cost in (
            ("filter", "FIL-1001", "Heavy-duty oil filter", "Fleetguard", "LF14000NN", "28.7500"),
            ("brake", "BRK-PAD-22", "Air disc brake pad kit", "Meritor", "MDP3128", "164.5000"),
            ("oil", "OIL-15W40", "15W-40 diesel engine oil", "Chevron", "DELO-400-XLE", "6.2500"),
        ):
            parts[key], _ = upsert(
                Part,
                seed_name=f"part:demo:{number}",
                lookup={"organization": organization, "number": number},
                values={
                    "name": name,
                    "description": f"Demo stock item: {name}.",
                    "manufacturer": manufacturer,
                    "manufacturer_number": manufacturer_number,
                    "unit_of_measure": "quart" if key == "oil" else "each",
                    "barcode": f"DEMO-{number}",
                    "default_unit_cost": Decimal(cost),
                    "active": True,
                },
            )
        upsert(
            PartCrossReference,
            seed_name="part-xref:demo:wix-51734",
            lookup={"organization": organization, "value_normalized": "WIX-51734"},
            values={"part": parts["filter"], "kind": "ALTERNATE", "value": "WIX-51734"},
        )
        opening = (
            (parts["filter"], a01, "24", "28.7500"),
            (parts["brake"], b02, "8", "164.5000"),
            (parts["oil"], a01, "120", "6.2500"),
        )
        for part, stock_bin, quantity, cost in opening:
            receive_stock(
                organization=organization,
                actor=users["parts_clerk"],
                part=part,
                bin=stock_bin,
                quantity=quantity,
                unit_cost=cost,
                operation_id=seed_id(f"stock-opening:{part.number}"),
                reference_type="opening_balance",
                reference_id="DEMO-OPENING",
                reason="Auditable demonstration opening balance",
            )
        reservation = reserve_stock(
            organization=organization,
            actor=users["parts_clerk"],
            part=parts["filter"],
            bin=a01,
            work_order=active_work,
            quantity="2",
            operation_id=seed_id("reservation:demo:wo-1001:filter"),
            reason="Parts staged for diagnosis and repair",
        )
        issued = issue_stock(
            organization=organization,
            actor=users["parts_clerk"],
            part=parts["filter"],
            bin=a01,
            work_order=active_work,
            reservation=reservation,
            quantity="1",
            operation_id=seed_id("issue:demo:wo-1001:filter"),
            reason="Issued to technician",
        )
        return_stock(
            organization=organization,
            actor=users["parts_clerk"],
            original=issued,
            quantity="0.250",
            operation_id=seed_id("return:demo:wo-1001:filter"),
            reason="Unused packaged quantity returned",
        )
        return parts, {"a01": a01, "b02": b02}

    def _purchasing(
        self,
        organization: Organization,
        users: dict[str, User],
        part: Part,
        stock_bin: Bin,
    ) -> None:
        vendor = Vendor.objects.filter(organization=organization, code="NAPA-HD").first()
        if vendor is None:
            vendor = create_vendor(
                record_id=seed_id("vendor:demo:napa-hd"),
                organization=organization,
                actor=users["purchasing_manager"],
                code="NAPA-HD",
                name="NAPA Heavy Duty",
                contact_name="Jordan Lee",
                email="orders@example.invalid",
                phone="555-0108",
                payment_terms="Net 30",
                address="100 Supply Way\nSpringfield, IL 62701",
            )
        if not PurchaseRequest.objects.filter(
            organization=organization, reason="Replenish shop safety stock"
        ).exists():
            create_purchase_request(
                record_id=seed_id("purchase-request:demo:safety-stock"),
                organization=organization,
                actor=users["parts_clerk"],
                part=part,
                quantity=Decimal("12"),
                reason="Replenish shop safety stock",
                needed_by=SEED_PURCHASE_NEEDED_BY,
            )
        purchase_order = PurchaseOrder.objects.filter(
            organization=organization, number="PO-DEMO-2401"
        ).first()
        if purchase_order is None:
            purchase_order = create_purchase_order(
                record_id=seed_id("purchase-order:demo:2401"),
                organization=organization,
                actor=users["purchasing_manager"],
                vendor=vendor,
                number="PO-DEMO-2401",
                expected_at=SEED_PURCHASE_EXPECTED_AT,
                notes="Demonstrates approval, partial receiving, and backorder tracking.",
                lines=[
                    {
                        "part": part,
                        "description": part.name,
                        "vendor_part_number": "NAPA-1748XD",
                        "quantity_ordered": "40",
                        "unit_cost": "28.75",
                    }
                ],
            )
            transition_purchase_order(
                organization=organization,
                actor=users["purchasing_manager"],
                purchase_order=purchase_order,
                target=PurchaseOrder.Status.SUBMITTED,
            )
            purchase_order.refresh_from_db()
            transition_purchase_order(
                organization=organization,
                actor=users["purchasing_approver"],
                purchase_order=purchase_order,
                target=PurchaseOrder.Status.APPROVED,
            )
            purchase_order.refresh_from_db()
            transition_purchase_order(
                organization=organization,
                actor=users["purchasing_manager"],
                purchase_order=purchase_order,
                target=PurchaseOrder.Status.SENT,
            )
            purchase_order.refresh_from_db()
        receipt_operation = seed_id("receipt:demo:po-2401:partial")
        if not Receipt.objects.filter(
            organization=organization, operation_id=receipt_operation
        ).exists():
            order_line = purchase_order.lines.get()
            post_receipt(
                record_id=seed_id("receipt:demo:po-2401:partial"),
                organization=organization,
                actor=users["parts_clerk"],
                purchase_order=purchase_order,
                operation_id=receipt_operation,
                packing_slip="PS-DEMO-0001",
                lines=[{"purchase_order_line": order_line, "bin": stock_bin, "quantity": "4"}],
            )

    def _device(
        self,
        organization: Organization,
        actor: User,
        asset: Asset,
        raw_token: str,
    ) -> None:
        device, created = upsert(
            Device,
            seed_name="device:demo:autopi-012",
            lookup={
                "organization": organization,
                "provider": "autopi",
                "serial_number": "AUTOPI-DEMO-012",
            },
            values={
                "name": "AutoPi TRK-012",
                "vendor": "AutoPi",
                "model": "TMU CM4",
                "external_id": "autopi-demo-012",
                "status": Device.Status.ACTIVE,
                "token_prefix": raw_token[:12],
                "token_hash": hashlib.sha256(raw_token.encode()).hexdigest(),
            },
        )
        if created:
            audit(
                organization=organization,
                actor=actor,
                action="device.registered",
                resource=device,
                context={"provider": "autopi", "serial_number": device.serial_number},
            )
        associate_device_to_asset(
            device=device,
            asset=asset,
            actor=actor,
            effective_from=datetime(2025, 1, 1, tzinfo=UTC),
        )

    def _tenant_isolation(self, password: str) -> None:
        organization, _ = upsert(
            Organization,
            seed_name="organization:isolation",
            lookup={"slug": "other-fleet"},
            values={"name": "Other Fleet Company", "settings": {"timezone": "America/Chicago"}},
        )
        location, _ = upsert(
            Location,
            seed_name="location:isolation:main",
            lookup={"organization": organization, "code": "MAIN"},
            values={"name": "Other Fleet Yard", "active": True},
        )
        role, _ = upsert(
            Role,
            seed_name="role:isolation:fleet-manager",
            lookup={"organization": organization, "slug": "fleet_manager"},
            values={"name": "Fleet manager"},
        )
        manager = self._user(
            organization=organization,
            location=location,
            role=role,
            username="other.manager@example.com",
            first_name="Riley",
            last_name="Other Fleet",
            password=password,
        )
        asset_type, _ = upsert(
            AssetType,
            seed_name="asset-type:isolation:truck",
            lookup={"organization": organization, "name": "Truck"},
            values={"category": "vehicle"},
        )
        self._asset(
            organization=organization,
            actor=manager,
            asset_type=asset_type,
            unit_number="OTHER-001",
            home_location=location,
            year=2021,
            make="International",
            model="LT",
            vin="3HSDZAPR7MN000001",
            ownership="Leased",
        )
        upsert(
            Part,
            seed_name="part:isolation:private",
            lookup={"organization": organization, "number": "PRIVATE-001"},
            values={
                "name": "Other organization private part",
                "description": "Tenant-isolation search and export fixture.",
                "manufacturer": "Private Supply",
                "manufacturer_number": "PRIVATE-ONLY",
                "unit_of_measure": "each",
                "barcode": "OTHER-PRIVATE-001",
                "default_unit_cost": Decimal("10.0000"),
                "active": True,
            },
        )
