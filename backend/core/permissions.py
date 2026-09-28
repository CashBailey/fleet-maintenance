from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from rest_framework.exceptions import PermissionDenied

from .models import ApiToken, User

_TOKEN_AUTH_ATTRIBUTE = "_fleetline_api_token_auth"  # noqa: S105

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "driver": {
        "dashboard.view",
        "assets.assigned",
        "inspections.create",
        "inspections.view_own",
        "defects.create",
        "defects.view_own",
        "attachments.create",
    },
    "technician": {
        "dashboard.view",
        "assets.view",
        "inspections.create",
        "defects.create",
        "defects.view_own",
        "maintenance.execute",
        "work_orders.view",
        "inventory.view",
        "inventory.issue",
        "inventory.return",
        "attachments.create",
        "comments.create",
        "documents.view",
    },
    "supervisor": {
        "dashboard.view",
        "assets.view",
        "assets.status",
        "maintenance.manage",
        "maintenance.execute",
        "pm.manage",
        "inventory.view",
        "inventory.issue",
        "reports.shop",
        "audit.view",
        "attachments.create",
        "comments.create",
        "documents.view",
        "documents.manage",
    },
    "parts_clerk": {
        "dashboard.view",
        "assets.view",
        "work_orders.view",
        "inventory.view",
        "inventory.transact",
        "inventory.issue",
        "inventory.return",
        "inventory.count",
        "purchasing.request",
        "purchasing.receive",
        "vendors.view",
        "reports.inventory",
        "attachments.create",
    },
    "purchasing_manager": {
        "dashboard.view",
        "assets.view",
        "work_orders.view",
        "inventory.view",
        "inventory.adjust",
        "purchasing.request",
        "purchasing.manage",
        "purchasing.approve",
        "purchasing.receive",
        "vendors.manage",
        "reports.purchasing",
        "financial.view",
        "financial.manage",
        "attachments.create",
        "documents.view",
    },
    "fleet_manager": {
        "dashboard.view",
        "assets.view",
        "assets.manage",
        "assets.status",
        "maintenance.manage",
        "maintenance.execute",
        "pm.manage",
        "inventory.view",
        "purchasing.policy",
        "reports.all",
        "financial.view",
        "financial.manage",
        "financial.export",
        "audit.view",
        "import.manage",
        "export.all",
        "attachments.create",
        "comments.create",
        "documents.view",
        "documents.manage",
    },
    "management": {"dashboard.view", "reports.executive", "assets.view", "financial.view"},
    "system_admin": {
        "dashboard.view",
        "admin.users",
        "admin.config",
        "audit.view",
        "export.all",
        "system.health",
        "attachments.create",
        "webhooks.manage",
        "documents.view",
        "documents.manage",
    },
    "integration_admin": {
        "dashboard.view",
        "assets.view",
        "assets.sync",
        "integrations.manage",
        "integrations.ingest",
        "reports.integration",
        "audit.view",
        "webhooks.manage",
        "personnel.sync",
    },
}

ROLE_NAVIGATION: dict[str, list[tuple[str, str]]] = {
    "driver": [
        ("Home", "/"),
        ("Inspect", "/inspections"),
        ("Report Problem", "/report-problem"),
        ("My Reports", "/defects"),
    ],
    "technician": [
        ("My Work", "/my-work"),
        ("Assets", "/assets"),
        ("Parts", "/parts"),
        ("Inspections", "/inspections"),
    ],
    "supervisor": [
        ("Today", "/"),
        ("Work", "/work-orders"),
        ("Assets", "/assets"),
        ("Schedule", "/schedule"),
        ("Parts", "/parts"),
        ("Alerts", "/alerts"),
        ("Reports", "/reports"),
    ],
    "parts_clerk": [
        ("Parts", "/parts"),
        ("Inventory", "/inventory"),
        ("Purchase Orders", "/purchase-orders"),
        ("Vendors", "/vendors"),
    ],
    "purchasing_manager": [
        ("Inventory", "/inventory"),
        ("Purchase Orders", "/purchase-orders"),
        ("Vendors", "/vendors"),
        ("Reports", "/reports"),
    ],
    "fleet_manager": [
        ("Today", "/"),
        ("Work", "/work-orders"),
        ("Assets", "/assets"),
        ("Schedule", "/schedule"),
        ("Parts", "/parts"),
        ("Reports", "/reports"),
    ],
    "management": [("Overview", "/"), ("Reports", "/reports")],
    "system_admin": [("System", "/"), ("Administration", "/administration"), ("Audit", "/audit")],
    "integration_admin": [
        ("Integration health", "/"),
        ("Devices", "/integrations"),
        ("Data quality", "/data-quality"),
        ("Audit", "/audit"),
    ],
}


def permissions_for(user: User) -> set[str]:
    permissions: set[str] = set()
    for role in user.roles.values_list("slug", flat=True):
        permissions.update(ROLE_PERMISSIONS.get(role, set()))
    return permissions


def has_permission(user: User, permission: str, auth: object | None = None) -> bool:
    granted = permissions_for(user)
    allowed = "*" in granted or permission in granted
    effective_auth = auth if auth is not None else getattr(user, _TOKEN_AUTH_ATTRIBUTE, None)
    if allowed and isinstance(effective_auth, ApiToken):
        scopes = effective_auth.scopes
        return isinstance(scopes, list) and permission in scopes
    return allowed


def can_view_financials(user: User, auth: object | None = None) -> bool:
    """Return whether this request may receive monetary values."""

    return has_permission(user, "financial.view", auth)


def can_manage_financials(user: User, auth: object | None = None) -> bool:
    """Return whether this request may set or approve monetary values."""

    return has_permission(user, "financial.manage", auth)


def can_export_financials(user: User, auth: object | None = None) -> bool:
    """Return whether this request may receive the complete financial export."""

    return has_permission(user, "financial.export", auth)


_FINANCIAL_FIELDS = frozenset(
    {
        "approval_threshold",
        "contribution",
        "cost",
        "current_labor_cost",
        "default_unit_cost",
        "hourly_rate",
        "labor_cost",
        "line_total",
        "open_purchase_order_value",
        "part_cost",
        "payment_terms",
        "previous_unit_cost",
        "price",
        "amount",
        "discount",
        "estimated_cost",
        "expected_cost",
        "inventory_value",
        "line_amount",
        "total",
        "total_cost",
        "total_variance_value",
        "total_value",
        "terms_snapshot",
        "unit_cost",
        "unit_cost_snapshot",
        "unit_price",
        "vendor_cost",
        "markup",
        "rate",
        "rate_per_hour",
    }
)


def redact_financial_fields(value: object) -> object:
    """Copy nested API data without money fields for non-financial viewers."""

    if isinstance(value, dict):
        return {
            key: redact_financial_fields(item)
            for key, item in value.items()
            if key not in _FINANCIAL_FIELDS
        }
    if isinstance(value, list):
        return [redact_financial_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_financial_fields(item) for item in value)
    return value


def bind_token_auth(user: User, token: ApiToken) -> None:
    """Keep bearer-token scope available in downstream domain-service checks."""

    setattr(user, _TOKEN_AUTH_ATTRIBUTE, token)


def require_permission(permission: str) -> Callable[..., Any]:
    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(request: Any, *args: Any, **kwargs: Any) -> Any:
            if not request.user.is_authenticated or not has_permission(
                request.user, permission, request.auth
            ):
                raise PermissionDenied(f"Permission required: {permission}")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def navigation_for(user: User) -> list[dict[str, str]]:
    navigation: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for role in sorted(user.roles.values_list("slug", flat=True)):
        for label, href in ROLE_NAVIGATION.get(role, []):
            if (label, href) not in seen:
                navigation.append({"label": label, "href": href})
                seen.add((label, href))
    return navigation
