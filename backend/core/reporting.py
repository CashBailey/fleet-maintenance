from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from assets.models import (
    Asset,
    AssetStatusEvent,
    AssetType,
    Component,
    ComponentInstallation,
    Meter,
    MeterReading,
)
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from integrations.models import (
    Device,
    DeviceAssetAssociation,
    NormalizedTelematicsEvent,
    TelematicsMessage,
)
from inventory.models import (
    Bin,
    InventoryCount,
    InventoryCountLine,
    Part,
    PartCrossReference,
    Reservation,
    StockBalance,
    StockTransaction,
    Warehouse,
)
from maintenance.models import (
    Defect,
    Inspection,
    InspectionFinding,
    InspectionResponse,
    InspectionTemplate,
    LaborEntry,
    MaintenanceAlert,
    MaintenancePlan,
    MaintenanceRequest,
    MaintenanceTrigger,
    ServicePackage,
    WorkOrder,
    WorkOrderCloseSnapshot,
    WorkOrderTask,
)
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequest,
    Receipt,
    ReceiptLine,
    Vendor,
    VendorPart,
)

from .models import (
    Attachment,
    AuditEvent,
    Comment,
    Document,
    DocumentApplicability,
    DocumentPage,
    Location,
    Notification,
    Organization,
    Role,
    SyncConflict,
    User,
)
from .permissions import ROLE_PERMISSIONS

REPORT_PERMISSIONS = frozenset(
    {
        "reports.shop",
        "reports.all",
        "reports.executive",
        "reports.inventory",
        "reports.purchasing",
        "reports.integration",
    }
)


def _source(
    *,
    record_type: str,
    record_id: object,
    label: str,
    link: str,
    contribution: int | Decimal,
    **details: object,
) -> dict[str, object]:
    return {
        "type": record_type,
        "id": str(record_id),
        "label": label,
        "link": link,
        "contribution": str(contribution) if isinstance(contribution, Decimal) else contribution,
        **details,
    }


def _add_metric(
    summary: dict[str, object],
    drilldown: dict[str, object],
    name: str,
    sources: list[dict[str, object]],
    *,
    decimal: bool = False,
) -> None:
    if decimal:
        value: object = str(
            sum((Decimal(str(row["contribution"])) for row in sources), Decimal("0"))
        )
    else:
        value = sum(int(str(row["contribution"])) for row in sources)
    summary[name] = value
    drilldown[name] = {
        "source_total": value,
        "source_count": len(sources),
        "sources": sources,
    }


def build_operations_report(
    organization: Organization, granted_report_permissions: set[str]
) -> dict[str, object]:
    """Build traceable metrics without crossing the caller's report domain."""

    summary: dict[str, object] = {}
    drilldown: dict[str, object] = {}
    shop = bool(granted_report_permissions & {"reports.shop", "reports.all"})
    executive = "reports.executive" in granted_report_permissions
    inventory = bool(
        granted_report_permissions & {"reports.inventory", "reports.shop", "reports.all"}
    )
    purchasing = "reports.purchasing" in granted_report_permissions
    integration = "reports.integration" in granted_report_permissions
    financial = "financial.view" in granted_report_permissions

    if shop or executive:
        assets = list(Asset.objects.filter(organization=organization).order_by("unit_number"))
        _add_metric(
            summary,
            drilldown,
            "asset_count",
            [
                _source(
                    record_type="asset",
                    record_id=row.pk,
                    label=row.unit_number,
                    link=f"/assets/{row.pk}",
                    contribution=1,
                    status=row.status,
                )
                for row in assets
            ],
        )
        out_of_service = [row for row in assets if row.status == Asset.Status.OUT_OF_SERVICE]
        _add_metric(
            summary,
            drilldown,
            "out_of_service",
            [
                _source(
                    record_type="asset",
                    record_id=row.pk,
                    label=row.unit_number,
                    link=f"/assets/{row.pk}",
                    contribution=1,
                    status=row.status,
                )
                for row in out_of_service
            ],
        )
        downtime_events = AssetStatusEvent.objects.filter(
            organization=organization, new_status=Asset.Status.OUT_OF_SERVICE
        ).select_related("asset")
        _add_metric(
            summary,
            drilldown,
            "downtime_events",
            [
                _source(
                    record_type="asset_status_event",
                    record_id=row.pk,
                    label=f"{row.asset.unit_number} out of service",
                    link=f"/assets/{row.asset_id}",
                    contribution=1,
                    asset_id=str(row.asset_id),
                    occurred_at=row.occurred_at,
                )
                for row in downtime_events.order_by("occurred_at", "id")
            ],
        )

    work_order_sources: list[dict[str, object]] = []
    if shop:
        overdue_plans = MaintenancePlan.objects.filter(
            organization=organization, due_status="Overdue"
        ).select_related("asset", "service_package")
        _add_metric(
            summary,
            drilldown,
            "pm_overdue",
            [
                _source(
                    record_type="maintenance_plan",
                    record_id=row.pk,
                    label=f"{row.asset.unit_number}: {row.name}",
                    link=f"/schedule?plan_id={row.pk}",
                    contribution=1,
                    asset_id=str(row.asset_id),
                    service_package_id=str(row.service_package_id),
                )
                for row in overdue_plans.order_by("id")
            ],
        )
        open_work_orders = (
            WorkOrder.objects.filter(organization=organization)
            .exclude(status__in=["Closed", "Cancelled"])
            .select_related("asset")
            .order_by("number", "id")
        )
        work_order_sources = [
            _source(
                record_type="work_order",
                record_id=row.pk,
                label=row.number,
                link=f"/work-orders/{row.pk}",
                contribution=1,
                number=row.number,
                summary=row.summary,
                status=row.status,
                asset_id=str(row.asset_id),
                asset_unit_number=row.asset.unit_number,
                **{"asset__unit_number": row.asset.unit_number},
            )
            for row in open_work_orders
        ]
        _add_metric(summary, drilldown, "open_work_orders", work_order_sources)

        if financial:
            current_labor = (
                LaborEntry.objects.filter(organization=organization, correction__isnull=True)
                .select_related("work_order", "technician")
                .order_by("created_at", "id")
            )
            _add_metric(
                summary,
                drilldown,
                "labor_cost",
                [
                    _source(
                        record_type="labor_entry",
                        record_id=row.pk,
                        label=f"{row.work_order.number}: {row.minutes} minutes",
                        link=f"/work-orders/{row.work_order_id}",
                        contribution=row.cost,
                        work_order_id=str(row.work_order_id),
                        technician_id=str(row.technician_id),
                        corrects_id=str(row.corrects_id) if row.corrects_id else None,
                    )
                    for row in current_labor
                ],
                decimal=True,
            )

    if inventory:
        parts = Part.objects.filter(organization=organization).order_by("number", "id")
        _add_metric(
            summary,
            drilldown,
            "part_count",
            [
                _source(
                    record_type="part",
                    record_id=row.pk,
                    label=row.number,
                    link=f"/parts?part_id={row.pk}",
                    contribution=1,
                    name=row.name,
                )
                for row in parts
            ],
        )
        balances = StockBalance.objects.filter(organization=organization).select_related(
            "part", "bin", "bin__warehouse"
        )
        for metric, field in (
            ("quantity_on_hand", "quantity_on_hand"),
            ("quantity_reserved", "quantity_reserved"),
        ):
            _add_metric(
                summary,
                drilldown,
                metric,
                [
                    _source(
                        record_type="stock_balance",
                        record_id=row.pk,
                        label=f"{row.part.number} at {row.bin}",
                        link=f"/parts?part_id={row.part_id}",
                        contribution=getattr(row, field),
                        part_id=str(row.part_id),
                        bin_id=str(row.bin_id),
                    )
                    for row in balances.order_by("part__number", "bin__code", "id")
                ],
                decimal=True,
            )
        if financial:
            work_order_transactions = (
                StockTransaction.objects.filter(organization=organization, work_order__isnull=False)
                .select_related("work_order", "part", "bin")
                .order_by("created_at", "id")
            )
            stock_sources = []
            for transaction in work_order_transactions:
                details: dict[str, object] = {
                    "transaction_type": transaction.transaction_type,
                    "part_id": str(transaction.part_id),
                    "bin_id": str(transaction.bin_id),
                    "original_transaction_id": (
                        str(transaction.original_transaction_id)
                        if transaction.original_transaction_id
                        else None
                    ),
                }
                if shop:
                    details["work_order_id"] = str(transaction.work_order_id)
                    label = (
                        f"{getattr(transaction.work_order, 'number', '')}: "
                        f"{transaction.part.number} {transaction.transaction_type}"
                    )
                    link = f"/work-orders/{transaction.work_order_id}"
                else:
                    label = f"{transaction.part.number} {transaction.transaction_type}"
                    link = f"/parts?part_id={transaction.part_id}"
                stock_sources.append(
                    _source(
                        record_type="stock_transaction",
                        record_id=transaction.pk,
                        label=label,
                        link=link,
                        contribution=transaction.total_cost,
                        **details,
                    )
                )
            _add_metric(
                summary,
                drilldown,
                "part_cost",
                stock_sources,
                decimal=True,
            )

    if purchasing:
        open_orders = list(
            PurchaseOrder.objects.filter(organization=organization)
            .exclude(status__in=["Received", "Closed", "Cancelled"])
            .select_related("vendor")
            .order_by("number", "id")
        )
        _add_metric(
            summary,
            drilldown,
            "open_purchase_orders",
            [
                _source(
                    record_type="purchase_order",
                    record_id=row.pk,
                    label=row.number,
                    link=f"/purchase-orders?purchase_order_id={row.pk}",
                    contribution=1,
                    status=row.status,
                    vendor_id=str(row.vendor_id),
                )
                for row in open_orders
            ],
        )
        if financial:
            open_order_ids = [row.pk for row in open_orders]
            open_lines = PurchaseOrderLine.objects.filter(
                organization=organization, purchase_order_id__in=open_order_ids
            ).select_related("purchase_order", "part")
            open_value_sources = []
            for row in open_lines.order_by("purchase_order__number", "id"):
                remaining = row.quantity_remaining
                if remaining <= 0:
                    continue
                open_value_sources.append(
                    _source(
                        record_type="purchase_order_line",
                        record_id=row.pk,
                        label=f"{row.purchase_order.number}: {row.part.number}",
                        link=f"/purchase-orders?purchase_order_id={row.purchase_order_id}",
                        contribution=remaining * row.unit_cost,
                        purchase_order_id=str(row.purchase_order_id),
                        part_id=str(row.part_id),
                        quantity_remaining=remaining,
                        unit_cost=row.unit_cost,
                    )
                )
            _add_metric(
                summary,
                drilldown,
                "open_purchase_order_value",
                open_value_sources,
                decimal=True,
            )
        posted_receipts = Receipt.objects.filter(
            organization=organization, status=Receipt.Status.POSTED
        ).select_related("purchase_order")
        _add_metric(
            summary,
            drilldown,
            "posted_receipts",
            [
                _source(
                    record_type="receipt",
                    record_id=row.pk,
                    label=row.number,
                    link=f"/purchase-orders?purchase_order_id={row.purchase_order_id}",
                    contribution=1,
                    purchase_order_id=str(row.purchase_order_id),
                    reversal_of_id=str(row.reversal_of_id) if row.reversal_of_id else None,
                )
                for row in posted_receipts.order_by("received_at", "id")
            ],
        )

    if integration:
        devices = Device.objects.filter(
            organization=organization, status=Device.Status.ACTIVE
        ).order_by("serial_number", "id")
        _add_metric(
            summary,
            drilldown,
            "active_devices",
            [
                _source(
                    record_type="device",
                    record_id=row.pk,
                    label=row.name,
                    link=f"/integrations?device_id={row.pk}",
                    contribution=1,
                    serial_number=row.serial_number,
                )
                for row in devices
            ],
        )
        for metric, status in (
            ("accepted_messages", TelematicsMessage.Status.ACCEPTED),
            ("quarantined_messages", TelematicsMessage.Status.QUARANTINED),
        ):
            messages = TelematicsMessage.objects.filter(
                organization=organization, status=status
            ).select_related("device")
            _add_metric(
                summary,
                drilldown,
                metric,
                [
                    _source(
                        record_type="telematics_message",
                        record_id=row.pk,
                        label=row.message_id or str(row.pk),
                        link="/data-quality",
                        contribution=1,
                        device_id=str(row.device_id),
                        observed_at=row.observed_at,
                    )
                    for row in messages.order_by("received_at", "id")
                ],
            )

    return {
        "summary": summary,
        "drilldown": drilldown,
        # Kept for the existing report table while clients adopt metric-specific drill-downs.
        "source_records": work_order_sources,
        "report_permissions": sorted(granted_report_permissions),
    }


_EXPORTED_MODELS: tuple[tuple[str, Any, frozenset[str]], ...] = (
    ("locations", Location, frozenset()),
    ("roles", Role, frozenset()),
    ("asset_types", AssetType, frozenset()),
    ("assets", Asset, frozenset()),
    ("asset_status_events", AssetStatusEvent, frozenset()),
    ("meters", Meter, frozenset()),
    ("meter_readings", MeterReading, frozenset()),
    ("components", Component, frozenset()),
    ("component_installations", ComponentInstallation, frozenset()),
    ("service_packages", ServicePackage, frozenset()),
    ("maintenance_plans", MaintenancePlan, frozenset()),
    ("maintenance_triggers", MaintenanceTrigger, frozenset()),
    ("inspection_templates", InspectionTemplate, frozenset()),
    ("inspections", Inspection, frozenset()),
    ("inspection_responses", InspectionResponse, frozenset()),
    ("inspection_findings", InspectionFinding, frozenset()),
    ("defects", Defect, frozenset()),
    ("maintenance_alerts", MaintenanceAlert, frozenset()),
    ("maintenance_requests", MaintenanceRequest, frozenset()),
    ("work_orders", WorkOrder, frozenset()),
    ("work_order_close_snapshots", WorkOrderCloseSnapshot, frozenset()),
    ("work_order_tasks", WorkOrderTask, frozenset()),
    ("labor_entries", LaborEntry, frozenset()),
    ("parts", Part, frozenset()),
    ("part_cross_references", PartCrossReference, frozenset()),
    ("warehouses", Warehouse, frozenset()),
    ("bins", Bin, frozenset()),
    ("stock_balances", StockBalance, frozenset()),
    ("stock_transactions", StockTransaction, frozenset()),
    ("reservations", Reservation, frozenset()),
    ("inventory_counts", InventoryCount, frozenset()),
    ("inventory_count_lines", InventoryCountLine, frozenset()),
    ("vendors", Vendor, frozenset()),
    ("vendor_parts", VendorPart, frozenset()),
    ("purchase_requests", PurchaseRequest, frozenset()),
    ("purchase_orders", PurchaseOrder, frozenset()),
    ("purchase_order_lines", PurchaseOrderLine, frozenset()),
    ("receipts", Receipt, frozenset()),
    ("receipt_lines", ReceiptLine, frozenset()),
    ("attachments", Attachment, frozenset()),
    ("documents", Document, frozenset()),
    ("document_applicability", DocumentApplicability, frozenset()),
    ("document_pages", DocumentPage, frozenset({"search_vector"})),
    ("comments", Comment, frozenset()),
    ("notifications", Notification, frozenset()),
    ("audit_events", AuditEvent, frozenset()),
    ("sync_conflicts", SyncConflict, frozenset()),
    ("devices", Device, frozenset({"token_hash"})),
    ("device_associations", DeviceAssetAssociation, frozenset()),
    ("telematics_messages", TelematicsMessage, frozenset()),
    ("normalized_telematics_events", NormalizedTelematicsEvent, frozenset()),
)

EXPORTED_MODEL_KEYS = frozenset(name for name, _model, _excluded in _EXPORTED_MODELS)


def _model_rows(model: Any, organization: Organization, excluded: frozenset[str]) -> list[dict]:
    fields = [
        field.attname
        for field in model._meta.concrete_fields
        if field.name not in excluded and field.attname not in excluded
    ]
    return list(
        model._default_manager.filter(organization=organization)
        .order_by(model._meta.pk.attname)
        .values(*fields)
    )


def build_organization_export(organization: Organization) -> dict[str, object]:
    users: list[dict[str, object]] = [
        dict(row)
        for row in (
            User.objects.filter(organization=organization)
            .order_by("id")
            .values(
                "id",
                "username",
                "first_name",
                "last_name",
                "email",
                "is_active",
                "is_staff",
                "is_superuser",
                "last_login",
                "date_joined",
                "default_location_id",
                "offline_access_revoked_at",
            )
        )
    ]
    role_ids = {
        str(user.pk): [
            str(value) for value in user.roles.order_by("id").values_list("id", flat=True)
        ]
        for user in User.objects.filter(organization=organization).prefetch_related("roles")
    }
    for row in users:
        row["role_ids"] = role_ids[str(row["id"])]
        row["organization_id"] = organization.pk

    payload: dict[str, object] = {
        "schema_version": "1.0",
        "exported_at": timezone.now().isoformat(),
        "scope": {
            "organization_id": str(organization.pk),
            "permissions": ["export.all", "financial.export"],
        },
        "organization": {
            "id": str(organization.pk),
            "name": organization.name,
            "slug": organization.slug,
            "settings": organization.settings,
            "created_at": organization.created_at,
        },
        "users": users,
    }
    exported_rows: dict[str, list[dict]] = {
        name: _model_rows(model, organization, excluded)
        for name, model, excluded in _EXPORTED_MODELS
    }
    for role in exported_rows["roles"]:
        role["permissions"] = sorted(ROLE_PERMISSIONS.get(str(role["slug"]), set()))
    payload.update(exported_rows)
    return payload


def encode_export(payload: dict[str, object]) -> str:
    return json.dumps(payload, cls=DjangoJSONEncoder, indent=2, sort_keys=True)
