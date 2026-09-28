from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from .auth_throttle import login_attempt_gate
from .exceptions import DomainError
from .models import (
    ApiToken,
    Attachment,
    AuditEvent,
    Comment,
    Document,
    Location,
    Notification,
    Organization,
    Role,
    SyncConflict,
    User,
    WebhookDelivery,
    WebhookSubscription,
    WorkerHeartbeat,
)
from .permissions import (
    ROLE_PERMISSIONS,
    can_export_financials,
    can_manage_financials,
    can_view_financials,
    has_permission,
    navigation_for,
    permissions_for,
    redact_financial_fields,
    require_permission,
)
from .reporting import (
    REPORT_PERMISSIONS,
    build_operations_report,
    build_organization_export,
    encode_export,
)
from .security import new_totp_secret, totp_provisioning_uri, validate_outbound_url, verify_totp
from .services import audit, idempotent

PRIVILEGED_MFA_ROLES = {"system_admin", "integration_admin"}
ALLOWED_ATTACHMENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "text/plain",
    "text/csv",
}
ATTACHMENT_EXTENSIONS = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "application/pdf": {".pdf"},
    "text/plain": {".txt"},
    "text/csv": {".csv"},
}


def _require_interactive_session(request: Request) -> None:
    if isinstance(request.auth, ApiToken):
        raise DomainError(
            "Identity and credential changes require an interactive administrator session",
            code="interactive_session_required",
            status=403,
        )


EXECUTABLE_ATTACHMENT_SUFFIXES = {
    ".app",
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".hta",
    ".jar",
    ".js",
    ".jse",
    ".msi",
    ".ps1",
    ".scr",
    ".sh",
    ".vbe",
    ".vbs",
    ".wsf",
}
STAGED_ATTACHMENT_TYPES = {"defect", "inspection", "work_note", "task", "stock"}
COMMERCIAL_ATTACHMENT_TYPES = frozenset({"part", "vendor", "purchase_order", "receipt"})
WEBHOOK_EVENT_TYPE_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def _user_json(user: User, auth: object | None = None) -> dict[str, object]:
    roles = sorted(user.roles.values_list("slug", flat=True))
    permissions = permissions_for(user)
    navigation = navigation_for(user)
    if isinstance(auth, ApiToken):
        permissions.intersection_update(auth.scopes)
        navigation = []
    return {
        "id": str(user.pk),
        "username": user.username,
        "name": user.get_full_name() or user.username,
        "organization_id": str(user.organization_id) if user.organization_id else None,
        "roles": roles,
        "permissions": sorted(permissions),
        "navigation": navigation,
    }


def _mfa_provisioning(user: User) -> dict[str, str]:
    organization = user.organization
    if organization is None:  # pragma: no cover - callers require an organization
        raise DomainError("User has no organization", code="invalid_user_organization")
    return {
        "secret": user.mfa_secret,
        "otpauth_uri": totp_provisioning_uri(
            secret=user.mfa_secret,
            account_name=user.username,
            issuer=organization.name,
        ),
    }


def _redact_mfa_provisioning(response: Response) -> object:
    data = dict(response.data)
    if "mfa_provisioning" not in data:
        return data
    data.pop("mfa_provisioning")
    data["secret_recoverable"] = False
    data["message"] = (
        "MFA provisioning was shown once and cannot be recovered; reset MFA to provision again."
    )
    return data


def _token_issue_idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key")
    if not value:
        raise DomainError(
            "Idempotency-Key is required", code="idempotency_key_required", status=400
        )
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainError("Idempotency-Key must be a UUID", code="invalid_idempotency_key") from exc


def _issue_api_token(
    request: Request,
    *,
    target: User,
    name: str,
    scopes: list[str],
    expires_at: datetime,
) -> Response:
    organization = request.user.organization
    key = _token_issue_idempotency_key(request)

    def issue() -> Response:
        prefix = secrets.token_hex(6)
        raw_token = f"flt_{prefix}_{secrets.token_urlsafe(32)}"
        token = ApiToken.objects.create(
            organization=organization,
            user=target,
            name=name,
            prefix=prefix,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            scopes=scopes,
            expires_at=expires_at,
        )
        token_data = token.to_dict()
        audit(
            organization=organization,
            actor=request.user,
            action="api_token.issued",
            resource=token,
            new_state="active",
            context={
                "user_id": str(target.pk),
                "prefix": prefix,
                "scopes": scopes,
                "expires_at": token.expires_at.isoformat(),
            },
            correlation_id=key,
        )
        return Response(
            {"api_token": token_data, "token": raw_token, "secret_recoverable": True}, status=201
        )

    def redact_secret(response: Response) -> object:
        return {
            "api_token": response.data["api_token"],
            "secret_recoverable": False,
            "message": (
                "The token was shown once and cannot be recovered; revoke it and issue a new token."
            ),
        }

    return idempotent(request, issue, stored_response_transform=redact_secret)


def health(request: HttpRequest, ready: bool = False) -> JsonResponse:
    checks: dict[str, object] = {"application": "ok"}
    status = 200
    if ready:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - infrastructure path
            checks["database"] = "failed"
            checks["database_error"] = type(exc).__name__
            status = 503
        heartbeat = (
            WorkerHeartbeat.objects.filter(name="default").first() if status == 200 else None
        )
        worker_fresh = bool(
            heartbeat and heartbeat.seen_at >= timezone.now() - timedelta(minutes=2)
        )
        pm_error = (
            str(heartbeat.details.get("pm_recalculation_error", ""))
            if heartbeat and isinstance(heartbeat.details, dict)
            else ""
        )
        worker_ready = worker_fresh and not pm_error
        checks["worker"] = "ok" if worker_ready else ("failed" if worker_fresh else "missing")
        if pm_error:
            checks["worker_error"] = "preventive_maintenance_recalculation"
        if settings.REQUIRE_WORKER and not worker_ready:
            status = 503
        checks["frontend"] = (
            "ok" if (settings.BASE_DIR / "frontend_dist" / "index.html").exists() else "missing"
        )
        if checks["frontend"] == "missing":
            status = 503
    return JsonResponse(
        {"status": "ok" if status == 200 else "unavailable", "checks": checks}, status=status
    )


def serve_spa(request: HttpRequest) -> HttpResponseBase:
    path = settings.BASE_DIR / "frontend_dist" / "index.html"
    if not path.exists():
        return HttpResponse(
            "Frontend build is missing. Run npm run build.", status=503, content_type="text/plain"
        )
    return FileResponse(path.open("rb"), content_type="text/html")


def service_worker(request: HttpRequest, manifest: bool = False) -> HttpResponseBase:
    filename = "manifest.webmanifest" if manifest else "sw.js"
    path = settings.BASE_DIR / "frontend_dist" / filename
    if not path.exists():
        return HttpResponse("Build artifact missing", status=404, content_type="text/plain")
    content_type = "application/manifest+json" if manifest else "application/javascript"
    response = FileResponse(path.open("rb"), content_type=content_type)
    if not manifest:
        response["Service-Worker-Allowed"] = "/"
        response["Cache-Control"] = "no-cache"
    return response


@ensure_csrf_cookie
@api_view(["GET"])
@permission_classes([AllowAny])
def csrf_cookie(request: Request) -> Response:
    return Response(
        {
            "csrf_token": get_token(request._request),
            "authenticated": bool(request.user.is_authenticated),
        }
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_protect
def login_view(request: Request) -> Response:
    username = str(request.data.get("username", "")).strip().lower()
    password = str(request.data.get("password", ""))
    account_id = User.objects.filter(username=username).values_list("pk", flat=True).first()
    user: User | None = None
    failure_status: int | None = None
    retry_after_seconds: int | None = None
    mfa_required = False
    with login_attempt_gate(request, str(account_id) if account_id else None) as gate:
        if gate.client_blocked:
            failure_status = 429
            retry_after_seconds = gate.retry_after_seconds
        elif gate.blocked:
            gate.record_blocked_account_attempt()
            failure_status = 429 if gate.client_blocked else 401
            retry_after_seconds = gate.retry_after_seconds
        else:
            authenticated = authenticate(request._request, username=username, password=password)
            if not isinstance(authenticated, User):
                gate.record_failure()
                failure_status = 429 if gate.client_blocked else 401
                retry_after_seconds = gate.retry_after_seconds
            elif authenticated.is_superuser or authenticated.role_slugs & PRIVILEGED_MFA_ROLES:
                otp = str(request.data.get("otp", ""))
                if not authenticated.mfa_secret:
                    gate.record_failure()
                    failure_status = 429 if gate.client_blocked else 401
                    retry_after_seconds = gate.retry_after_seconds
                elif not otp:
                    mfa_required = True
                elif not verify_totp(authenticated.mfa_secret, otp):
                    gate.record_failure()
                    failure_status = 429 if gate.client_blocked else 401
                    retry_after_seconds = gate.retry_after_seconds
                else:
                    gate.record_success()
                    user = authenticated
            else:
                gate.record_success()
                user = authenticated
    if failure_status is not None:
        raise DomainError(
            "Authentication failed",
            code="login_throttled" if failure_status == 429 else "invalid_credentials",
            status=failure_status,
            details=(
                {"retry_after_seconds": retry_after_seconds}
                if retry_after_seconds is not None
                else None
            ),
        )
    if mfa_required:
        raise DomainError("A one-time code is required", code="mfa_required", status=401)
    if user is None:  # pragma: no cover - all outcomes above are exhaustive
        raise DomainError("Authentication failed", code="invalid_credentials", status=401)
    login(request._request, user)
    return Response({"user": _user_json(user)})


@api_view(["POST"])
def logout_view(request: Request) -> Response:
    logout(request._request)
    return Response({"ok": True})


@api_view(["GET"])
def me_view(request: Request) -> Response:
    return Response({"user": _user_json(request.user, request.auth)})


@api_view(["GET", "POST"])
def users(request: Request) -> Response:
    can_administer = has_permission(request.user, "admin.users", request.auth)
    if request.method == "POST" and not can_administer:
        raise DomainError(
            "User administration is not allowed", code="permission_denied", status=403
        )
    if request.method == "GET" and not (
        can_administer or has_permission(request.user, "maintenance.manage", request.auth)
    ):
        raise DomainError(
            "User directory access is not allowed", code="permission_denied", status=403
        )
    if request.method == "POST":
        _require_interactive_session(request)
        username = str(request.data.get("username", "")).strip().lower()
        name = str(request.data.get("name", "")).strip()
        password = str(request.data.get("password", ""))
        requested_roles = request.data.get("role_slugs")
        if not username or not name or not password:
            raise DomainError("username, name, and password are required", code="required_fields")
        if not isinstance(requested_roles, list) or not requested_roles:
            raise DomainError("role_slugs must contain at least one role", code="invalid_roles")
        role_slugs = {str(slug).strip() for slug in requested_roles}
        if "" in role_slugs or not role_slugs.issubset(ROLE_PERMISSIONS):
            raise DomainError("role_slugs contains an unknown role", code="invalid_roles")
        assigned_roles = list(
            Role.objects.filter(
                organization=request.user.organization, slug__in=role_slugs
            ).order_by("slug")
        )
        if {role.slug for role in assigned_roles} != role_slugs:
            raise DomainError(
                "One or more roles are not configured for this organization",
                code="roles_not_configured",
            )

        location = None
        location_id = request.data.get("default_location")
        if location_id:
            try:
                location_uuid = uuid.UUID(str(location_id))
            except ValueError as exc:
                raise DomainError(
                    "default_location is invalid", code="invalid_default_location"
                ) from exc
            location = Location.objects.filter(
                pk=location_uuid, organization=request.user.organization, active=True
            ).first()
            if location is None:
                raise DomainError(
                    "default_location is not an active location in this organization",
                    code="invalid_default_location",
                )

        privileged = bool(role_slugs & PRIVILEGED_MFA_ROLES)
        if privileged and isinstance(request.auth, ApiToken):
            raise DomainError(
                "Privileged users must be created from an interactive administrator session",
                code="interactive_session_required",
                status=403,
            )

        def create() -> Response:
            user = User(
                username=username,
                first_name=name,
                organization=request.user.organization,
                default_location=location,
                mfa_secret=new_totp_secret() if privileged else "",
            )
            try:
                validate_password(password, user=user)
                user.set_password(password)
                user.full_clean()
            except ValidationError as exc:
                raise DomainError(
                    "User details are invalid", code="invalid_user", details=exc.messages
                ) from exc
            try:
                with transaction.atomic():
                    user.save()
                    user.roles.set(assigned_roles)
                    audit(
                        organization=request.user.organization,
                        actor=request.user,
                        action="user.created",
                        resource=user,
                        context={
                            "role_slugs": sorted(role_slugs),
                            "default_location": str(location.pk) if location else None,
                            "mfa_provisioned": privileged,
                        },
                        correlation_id=request.headers.get("Idempotency-Key", ""),
                    )
            except IntegrityError as exc:
                raise DomainError(
                    "Username is already in use", code="username_exists", status=409
                ) from exc
            data: dict[str, object] = {
                "user": {
                    "id": str(user.pk),
                    "name": user.get_full_name(),
                    "username": user.username,
                    "roles": sorted(role_slugs),
                    "default_location": str(location.pk) if location else None,
                    "active": True,
                }
            }
            if privileged:
                data.update(
                    {
                        "mfa_provisioning": _mfa_provisioning(user),
                        "secret_recoverable": True,
                    }
                )
            return Response(data, status=201)

        return idempotent(
            request,
            create,
            required=privileged,
            stored_response_transform=_redact_mfa_provisioning,
        )

    rows = User.objects.filter(
        organization=request.user.organization, is_active=True
    ).prefetch_related("roles")
    role = str(request.query_params.get("role", "")).strip()
    if role:
        rows = rows.filter(roles__slug=role)
    return Response(
        {
            "users": [
                {
                    "id": str(user.pk),
                    "name": user.get_full_name() or user.username,
                    "username": user.username,
                    "roles": sorted(role.slug for role in user.roles.all()),
                }
                | ({"mfa_configured": bool(user.mfa_secret)} if can_administer else {})
                for user in rows.order_by("username")
            ]
        }
    )


@api_view(["POST"])
@require_permission("admin.users")
def provision_user_mfa(request: Request, user_id: uuid.UUID) -> Response:
    organization = request.user.organization
    if organization is None:
        raise DomainError("User has no organization", code="organization_required", status=403)
    if isinstance(request.auth, ApiToken):
        raise DomainError(
            "MFA changes require an interactive administrator session",
            code="interactive_session_required",
            status=403,
        )

    reason = str(request.data.get("reason", "")).strip()
    if len(reason) > 500:
        raise DomainError("MFA reset reason is too long", code="invalid_reason")

    def provision() -> Response:
        target = get_object_or_404(
            User.objects.select_for_update().prefetch_related("roles"),
            pk=user_id,
            organization=organization,
            is_active=True,
        )
        if target.pk == request.user.pk:
            raise DomainError(
                "You cannot reset MFA for your own account",
                code="cannot_manage_own_mfa",
                status=403,
            )
        role_slugs = {role.slug for role in target.roles.all()}
        if not role_slugs & PRIVILEGED_MFA_ROLES:
            raise DomainError(
                "MFA provisioning is limited to privileged administrator accounts",
                code="mfa_role_required",
            )
        rotating = bool(target.mfa_secret)
        if rotating and not reason:
            raise DomainError(
                "A reason is required to reset existing MFA",
                code="reason_required",
            )
        target.mfa_secret = new_totp_secret()
        target.save(update_fields=["mfa_secret"])
        action = "user.mfa.rotated" if rotating else "user.mfa.enrolled"
        audit(
            organization=organization,
            actor=request.user,
            action=action,
            resource=target,
            previous_state="configured" if rotating else "not_configured",
            new_state="configured",
            context={
                "target_user_id": str(target.pk),
                "target_username": target.username,
                "role_slugs": sorted(role_slugs),
                "rotation": rotating,
                "reason": reason,
            },
            correlation_id=request.headers.get("Idempotency-Key", ""),
        )
        return Response(
            {
                "user": {
                    "id": str(target.pk),
                    "username": target.username,
                    "roles": sorted(role_slugs),
                    "mfa_configured": True,
                },
                "operation": "rotated" if rotating else "enrolled",
                "mfa_provisioning": _mfa_provisioning(target),
                "secret_recoverable": True,
            }
        )

    return idempotent(request, provision, stored_response_transform=_redact_mfa_provisioning)


@api_view(["GET", "POST"])
@require_permission("admin.users")
def api_tokens(request: Request) -> Response:
    organization = request.user.organization
    if organization is None:
        raise DomainError("User has no organization", code="organization_required", status=403)
    if request.method == "GET":
        rows = ApiToken.objects.filter(organization=organization).select_related("user")
        return Response({"api_tokens": [token.to_dict() for token in rows.order_by("-created_at")]})

    _require_interactive_session(request)
    name = str(request.data.get("name", "")).strip()
    if not name or len(name) > 120:
        raise DomainError(
            "name must contain between 1 and 120 characters", code="invalid_token_name"
        )
    try:
        target_id = uuid.UUID(str(request.data.get("user_id", "")))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainError("user_id must be a UUID", code="invalid_user_id") from exc
    target = get_object_or_404(
        User,
        pk=target_id,
        organization=organization,
        is_active=True,
    )

    requested_scopes = request.data.get("scopes")
    if (
        not isinstance(requested_scopes, list)
        or not requested_scopes
        or any(not isinstance(scope, str) or not scope.strip() for scope in requested_scopes)
    ):
        raise DomainError("scopes must be a non-empty string list", code="invalid_token_scopes")
    scopes = sorted({scope.strip() for scope in requested_scopes})
    if "*" in scopes:
        raise DomainError(
            "Wildcard API token scopes are not supported", code="invalid_token_scopes"
        )
    known_scopes = set().union(*ROLE_PERMISSIONS.values())
    if not set(scopes).issubset(known_scopes):
        raise DomainError(
            "scopes contains an unknown or invalid scope", code="invalid_token_scopes"
        )
    target_permissions = permissions_for(target)
    if "*" not in target_permissions and not set(scopes).issubset(target_permissions):
        raise DomainError(
            "scopes exceed the target user's permissions", code="token_scope_escalation", status=403
        )

    expires_value = request.data.get("expires_at")
    expires_at = parse_datetime(expires_value) if isinstance(expires_value, str) else None
    if expires_at is None or timezone.is_naive(expires_at):
        raise DomainError(
            "expires_at must be an ISO-8601 timestamp with a timezone",
            code="invalid_token_expiry",
        )
    if expires_at <= timezone.now():
        raise DomainError("expires_at must be in the future", code="invalid_token_expiry")
    return _issue_api_token(
        request,
        target=target,
        name=name,
        scopes=scopes,
        expires_at=expires_at,
    )


@api_view(["POST"])
@require_permission("admin.users")
def revoke_api_token(request: Request, token_id: uuid.UUID) -> Response:
    _require_interactive_session(request)
    organization = request.user.organization
    if organization is None:
        raise DomainError("User has no organization", code="organization_required", status=403)
    get_object_or_404(ApiToken, organization=organization, pk=token_id)
    reason = str(request.data.get("reason", "")).strip()[:500]

    def revoke() -> Response:
        token = get_object_or_404(
            ApiToken.objects.select_for_update().select_related("user"),
            organization=organization,
            pk=token_id,
        )
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at", "updated_at"])
            audit(
                organization=organization,
                actor=request.user,
                action="api_token.revoked",
                resource=token,
                previous_state="active",
                new_state="revoked",
                context={"user_id": str(token.user_id), "prefix": token.prefix, "reason": reason},
                correlation_id=str(request.headers.get("Idempotency-Key", "")),
            )
        return Response({"api_token": token.to_dict()})

    return idempotent(request, revoke)


@api_view(["GET"])
@require_permission("admin.users")
def roles(request: Request) -> Response:
    rows = Role.objects.filter(
        organization=request.user.organization, slug__in=ROLE_PERMISSIONS
    ).order_by("slug")
    return Response(
        {"roles": [{"id": str(role.pk), "slug": role.slug, "name": role.name} for role in rows]}
    )


@api_view(["GET", "POST"])
def locations(request: Request) -> Response:
    can_configure = has_permission(request.user, "admin.config", request.auth)
    if request.method == "POST" and not can_configure:
        raise DomainError(
            "Location administration is not allowed", code="permission_denied", status=403
        )
    if request.method == "GET" and not (
        can_configure or has_permission(request.user, "admin.users", request.auth)
    ):
        raise DomainError(
            "Location directory access is not allowed", code="permission_denied", status=403
        )
    if request.method == "POST":
        name = str(request.data.get("name", "")).strip()
        code = str(request.data.get("code", "")).strip().upper()
        if not name or not code:
            raise DomainError("name and code are required", code="required_fields")
        if Location.objects.filter(
            organization=request.user.organization, code__iexact=code
        ).exists():
            raise DomainError(
                "Location code is already in use", code="location_code_exists", status=409
            )
        location = Location(organization=request.user.organization, name=name, code=code)
        try:
            location.full_clean()
            with transaction.atomic():
                location.save()
                audit(
                    organization=request.user.organization,
                    actor=request.user,
                    action="location.created",
                    resource=location,
                    context={"code": location.code},
                )
        except ValidationError as exc:
            raise DomainError(
                "Location details are invalid",
                code="invalid_location",
                details=exc.messages,
            ) from exc
        except IntegrityError as exc:
            raise DomainError(
                "Location code is already in use", code="location_code_exists", status=409
            ) from exc
        return Response(
            {
                "location": {
                    "id": str(location.pk),
                    "name": location.name,
                    "code": location.code,
                    "active": location.active,
                }
            },
            status=201,
        )

    rows = Location.objects.filter(organization=request.user.organization).order_by("code")
    return Response(
        {
            "locations": [
                {
                    "id": str(location.pk),
                    "name": location.name,
                    "code": location.code,
                    "active": location.active,
                }
                for location in rows
            ]
        }
    )


@api_view(["GET"])
@require_permission("dashboard.view")
def bootstrap(request: Request) -> Response:
    from assets.models import Asset
    from inventory.models import Part, StockBalance
    from maintenance.models import InspectionTemplate, WorkOrder
    from maintenance.services import filter_work_orders_for_assignee

    from .offline_access import issue_offline_access_grant

    org = request.user.organization
    auth = request.auth
    role_slugs = request.user.role_slugs
    may_view_financials = can_view_financials(request.user, auth)

    assets = Asset.objects.none()
    if has_permission(request.user, "assets.view", auth):
        assets = Asset.objects.filter(organization=org, archived_at__isnull=True)
    elif has_permission(request.user, "assets.assigned", auth):
        assets = Asset.objects.filter(organization=org, archived_at__isnull=True)
        assets = assets.filter(assigned_driver=request.user)

    work = WorkOrder.objects.none()
    may_manage_work = has_permission(request.user, "maintenance.manage", auth)
    may_view_work = has_permission(request.user, "work_orders.view", auth)
    if may_manage_work or (
        may_view_work and role_slugs.intersection({"parts_clerk", "purchasing_manager"})
    ):
        work = WorkOrder.objects.filter(organization=org).exclude(
            status__in=["Closed", "Cancelled"]
        )
    elif may_view_work:
        work = WorkOrder.objects.filter(organization=org).exclude(
            status__in=["Closed", "Cancelled"]
        )
        work = filter_work_orders_for_assignee(work, request.user)

    may_view_inventory = has_permission(request.user, "inventory.view", auth)
    parts = Part.objects.none()
    stock = StockBalance.objects.none()
    if may_view_inventory:
        parts = Part.objects.filter(organization=org, active=True)
        stock = StockBalance.objects.filter(organization=org).select_related("part", "bin")

    templates = InspectionTemplate.objects.none()
    if has_permission(request.user, "inspections.create", auth) or has_permission(
        request.user, "maintenance.manage", auth
    ):
        templates = InspectionTemplate.objects.filter(organization=org, active=True)
    may_view_locations = any(
        has_permission(request.user, permission, auth)
        for permission in ("admin.config", "inventory.transact", "inventory.adjust")
    )
    conflict_rows = SyncConflict.objects.filter(
        organization=org, user=request.user, resolved_at__isnull=True
    )
    offline_expires_at = timezone.now() + timedelta(hours=settings.OFFLINE_CACHE_HOURS)
    offline_grant: dict[str, str] = {}
    if not isinstance(auth, ApiToken):
        issued_grant = issue_offline_access_grant(request.user)
        offline_expires_at = issued_grant.expires_at
        offline_grant["offline_grant"] = issued_grant.token
    return Response(
        {
            "user": _user_json(request.user, auth),
            "offline_expires_at": offline_expires_at.isoformat(),
            **offline_grant,
            "assets": [
                a.to_dict() for a in assets.select_related("asset_type", "home_location")[:100]
            ],
            "locations": (
                list(
                    Location.objects.filter(organization=org, active=True)
                    .order_by("name")
                    .values("id", "code", "name")
                )
                if may_view_locations
                else []
            ),
            "work_orders": [w.to_dict() for w in work.select_related("asset", "assigned_to")[:100]],
            "parts": [p.to_dict(include_financial=may_view_financials) for p in parts[:500]],
            "stock": [b.to_dict() for b in stock[:500]],
            "inspection_templates": [t.to_dict() for t in templates],
            "sync_conflicts": [
                {
                    "id": str(conflict.pk),
                    "operation_id": str(conflict.operation_id),
                    "operation_type": conflict.operation_type,
                    "message": conflict.message,
                    "client_payload": (
                        conflict.client_payload
                        if may_view_financials
                        else redact_financial_fields(conflict.client_payload)
                    ),
                    "server_payload": (
                        conflict.server_payload
                        if may_view_financials
                        else redact_financial_fields(conflict.server_payload)
                    ),
                    "created_at": conflict.created_at,
                    "resolved_at": conflict.resolved_at,
                    "resolution_options": ["review_server", "discard_local"],
                }
                for conflict in conflict_rows
            ],
        }
    )


@api_view(["GET"])
@require_permission("dashboard.view")
def global_search(request: Request) -> Response:
    from assets.models import Asset, Component
    from inventory.models import Part, PartCrossReference
    from maintenance.models import WorkOrder
    from maintenance.services import can_view_all_work_orders, filter_work_orders_for_assignee
    from purchasing.models import Vendor, VendorPart

    query = str(request.query_params.get("q", "")).strip()
    if len(query) < 2:
        return Response({"results": []})
    org = request.user.organization
    results: list[dict[str, object]] = []
    if has_permission(request.user, "assets.view", request.auth) or has_permission(
        request.user, "assets.assigned", request.auth
    ):
        assets = Asset.objects.filter(organization=org).filter(
            Q(unit_number__icontains=query) | Q(vin__icontains=query)
        )
        if not has_permission(request.user, "assets.view", request.auth):
            assets = assets.filter(assigned_driver=request.user)
        results += [
            {"type": "asset", "id": str(x.pk), "label": x.unit_number, "detail": x.vin}
            for x in assets[:10]
        ]
    if has_permission(request.user, "inventory.view", request.auth):
        part_ids = PartCrossReference.objects.filter(
            organization=org, value_normalized__icontains=query.upper()
        ).values("part_id")
        vendor_part_ids = VendorPart.objects.filter(
            organization=org, vendor_part_number__icontains=query
        ).values("part_id")
        parts = Part.objects.filter(organization=org).filter(
            Q(number__icontains=query)
            | Q(name__icontains=query)
            | Q(manufacturer_number__icontains=query)
            | Q(barcode__iexact=query)
            | Q(pk__in=part_ids)
            | Q(pk__in=vendor_part_ids)
        )[:10]
        results += [
            {"type": "part", "id": str(x.pk), "label": x.number, "detail": x.name} for x in parts
        ]
    if has_permission(request.user, "work_orders.view", request.auth) or has_permission(
        request.user, "maintenance.manage", request.auth
    ):
        work = WorkOrder.objects.filter(organization=org).filter(
            Q(number__icontains=query) | Q(summary__icontains=query)
        )
        if not can_view_all_work_orders(request.user, request.auth):
            work = filter_work_orders_for_assignee(work, request.user)
        work = work[:10]
        results += [
            {"type": "work_order", "id": str(x.pk), "label": x.number, "detail": x.summary}
            for x in work
        ]
    if has_permission(request.user, "vendors.view", request.auth) or has_permission(
        request.user, "vendors.manage", request.auth
    ):
        vendors = Vendor.objects.filter(organization=org, name__icontains=query)[:10]
        results += [
            {"type": "vendor", "id": str(x.pk), "label": x.name, "detail": x.code} for x in vendors
        ]
    if has_permission(request.user, "assets.view", request.auth):
        components = Component.objects.filter(organization=org, serial_number__icontains=query)[:10]
        results += [
            {
                "type": "component",
                "id": str(x.pk),
                "label": x.serial_number,
                "detail": x.get_kind_display(),
            }
            for x in components
        ]
    return Response({"results": results[:30]})


@api_view(["GET"])
def operations_report(request: Request) -> Response:
    granted = {
        permission
        for permission in REPORT_PERMISSIONS
        if has_permission(request.user, permission, request.auth)
    }
    if not granted:
        raise DomainError("Report access is not allowed", code="permission_denied", status=403)
    if can_view_financials(request.user, request.auth):
        granted.add("financial.view")
    return Response(build_operations_report(request.user.organization, granted))


def _validate_upload(
    upload: UploadedFile,
    *,
    max_bytes: int | None = None,
    allowed_types: set[str] | None = None,
) -> tuple[str, int]:
    original_name = Path(str(upload.name or "").replace("\\", "/")).name
    if (
        not original_name
        or len(original_name) > 255
        or any(character in original_name for character in ("\x00", "\r", "\n"))
    ):
        raise DomainError("Attachment name is not allowed", code="invalid_attachment_name")
    upload.name = original_name
    suffixes = {suffix.casefold() for suffix in Path(original_name).suffixes}
    if suffixes.intersection(EXECUTABLE_ATTACHMENT_SUFFIXES):
        raise DomainError("Executable attachments are not allowed", code="unsupported_attachment")
    declared_type = str(upload.content_type or "").partition(";")[0].strip().casefold()
    content_type = declared_type or mimetypes.guess_type(original_name)[0] or ""
    size = upload.size or 0
    allowed_types = allowed_types or ALLOWED_ATTACHMENT_TYPES
    if content_type not in allowed_types:
        raise DomainError("Unsupported attachment type", code="unsupported_attachment")
    if not suffixes.intersection(ATTACHMENT_EXTENSIONS[content_type]):
        raise DomainError(
            "Attachment extension does not match its declared type",
            code="attachment_type_mismatch",
        )
    if size <= 0 or size > (max_bytes or settings.ATTACHMENT_MAX_BYTES):
        raise DomainError("Attachment size is not allowed", code="invalid_attachment_size")
    content = upload.read() if content_type in {"text/plain", "text/csv"} else upload.read(16)
    upload.seek(0)
    signatures: dict[str, tuple[bytes, ...]] = {
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/jpeg": (b"\xff\xd8\xff",),
        "application/pdf": (b"%PDF-",),
    }
    if content_type == "image/webp" and not (
        content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    ):
        raise DomainError(
            "Attachment content does not match its declared type", code="attachment_type_mismatch"
        )
    expected = signatures.get(content_type)
    if expected and not content.startswith(expected):
        raise DomainError(
            "Attachment content does not match its declared type", code="attachment_type_mismatch"
        )
    if content_type in {"text/plain", "text/csv"}:
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DomainError(
                "Text attachments must be valid UTF-8", code="attachment_type_mismatch"
            ) from exc
        if b"\x00" in content:
            raise DomainError(
                "Text attachments cannot contain binary data", code="attachment_type_mismatch"
            )
    return content_type, size


def _attachment_json(
    attachment: Attachment,
    *,
    superseded_by_id: uuid.UUID | None = None,
    is_current: bool | None = None,
) -> dict[str, object]:
    return {
        "id": str(attachment.pk),
        "name": attachment.original_name,
        "title": attachment.title,
        "sensitivity": attachment.sensitivity,
        "category": attachment.category,
        "document_key": str(attachment.document_key),
        "version": attachment.version,
        "supersedes_id": (str(attachment.supersedes_id) if attachment.supersedes_id else None),
        "superseded_by_id": str(superseded_by_id) if superseded_by_id else None,
        "is_current": superseded_by_id is None if is_current is None else is_current,
        "content_type": attachment.content_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
        "created_at": attachment.created_at.isoformat(),
    }


def _attachment_resource_kind(resource_type: str) -> str:
    kind = resource_type.replace("-", "_").casefold()
    return {
        "maintenancerequest": "maintenance_request",
        "workorder": "work_order",
        "workordertask": "work_order_task",
        "purchaseorder": "purchase_order",
    }.get(kind, kind)


def _attachment_sensitivity(
    resource_type: str,
    value: object,
    *,
    require_explicit: bool = True,
) -> str:
    """Validate a classification before opaque bytes enter the attachment store."""

    sensitivity = str(value or "").strip().casefold()
    kind = _attachment_resource_kind(resource_type)
    if not sensitivity:
        if require_explicit and kind in COMMERCIAL_ATTACHMENT_TYPES:
            raise DomainError(
                "Commercial attachments require an operational or financial sensitivity",
                code="attachment_sensitivity_required",
            )
        return Attachment.Sensitivity.OPERATIONAL
    if sensitivity not in Attachment.Sensitivity.values:
        raise DomainError(
            "Attachment sensitivity must be operational or financial",
            code="invalid_attachment_sensitivity",
        )
    return sensitivity


def _attachment_is_visible(user: User, attachment: Attachment, auth: object | None) -> bool:
    """Fail closed for unknown classifications as well as explicitly financial bytes."""

    return attachment.sensitivity == Attachment.Sensitivity.OPERATIONAL or can_view_financials(
        user, auth
    )


def _require_attachment_sensitivity_manage(
    user: User, sensitivity: str, auth: object | None
) -> None:
    if sensitivity != Attachment.Sensitivity.OPERATIONAL and not can_manage_financials(user, auth):
        raise DomainError(
            "Permission required: financial.manage",
            code="financial_permission_denied",
            status=403,
        )


def _attachment_target_allowed(
    user: User, resource_type: str, resource_id: str, auth: object | None = None
) -> bool:
    """Authorize against the linked domain record without trusting attachment metadata."""

    kind = _attachment_resource_kind(resource_type)
    try:
        uuid.UUID(resource_id)
    except (AttributeError, TypeError, ValueError):
        return False
    organization = user.organization
    if kind == "asset":
        from assets.models import Asset

        asset_target = Asset.objects.filter(pk=resource_id, organization=organization).first()
        return bool(
            asset_target
            and (
                has_permission(user, "assets.view", auth)
                or asset_target.assigned_driver_id == user.pk
            )
        )
    if kind in {
        "defect",
        "inspection",
        "maintenance_request",
        "work_order",
        "work_order_task",
    }:
        from maintenance.models import (
            Defect,
            Inspection,
            MaintenanceRequest,
            WorkOrder,
            WorkOrderTask,
        )
        from maintenance.services import (
            can_view_all_work_orders,
            filter_work_orders_for_assignee,
            is_active_work_order_assignee,
        )

        if has_permission(user, "maintenance.manage", auth):
            return (
                {
                    "defect": Defect,
                    "inspection": Inspection,
                    "maintenance_request": MaintenanceRequest,
                    "work_order": WorkOrder,
                    "work_order_task": WorkOrderTask,
                }[kind]
                ._default_manager.filter(pk=resource_id, organization=organization)
                .exists()
            )
        if kind == "defect":
            defect_target = Defect.objects.filter(pk=resource_id, organization=organization).first()
            if not defect_target:
                return False
            if defect_target.reported_by_id == user.pk:
                return True
            if "technician" not in user.role_slugs or not has_permission(
                user, "maintenance.execute", auth
            ):
                return False
            return (
                filter_work_orders_for_assignee(
                    WorkOrder.objects.filter(
                        organization=organization,
                        request__defect=defect_target,
                    ),
                    user,
                )
                .exclude(status__in=["Closed", "Cancelled"])
                .exists()
            )
        if kind == "inspection":
            inspection_target = Inspection.objects.filter(
                pk=resource_id, organization=organization
            ).first()
            return bool(inspection_target and inspection_target.performed_by_id == user.pk)
        if kind == "work_order":
            work_order_target = WorkOrder.objects.filter(
                pk=resource_id, organization=organization
            ).first()
            if not work_order_target:
                return False
            return can_view_all_work_orders(user, auth) or bool(
                has_permission(user, "maintenance.execute", auth)
                and is_active_work_order_assignee(work_order_target, user)
            )
        if kind == "work_order_task":
            task_target = WorkOrderTask.objects.filter(
                pk=resource_id, organization=organization
            ).first()
            return bool(
                task_target
                and (
                    can_view_all_work_orders(user, auth)
                    or has_permission(user, "maintenance.execute", auth)
                    and is_active_work_order_assignee(task_target.work_order, user)
                )
            )
        request_target = MaintenanceRequest.objects.filter(
            pk=resource_id, organization=organization
        ).first()
        if not request_target:
            return False
        return can_view_all_work_orders(user, auth) or bool(
            has_permission(user, "maintenance.execute", auth)
            and filter_work_orders_for_assignee(
                WorkOrder.objects.filter(request=request_target, organization=organization), user
            ).exists()
        )
    if kind == "part":
        from inventory.models import Part

        return (
            has_permission(user, "inventory.view", auth)
            and Part.objects.filter(pk=resource_id, organization=organization).exists()
        )
    if kind in {"purchase_order", "receipt"}:
        from purchasing.models import PurchaseOrder, Receipt

        if not (
            has_permission(user, "purchasing.manage", auth)
            or has_permission(user, "purchasing.receive", auth)
        ):
            return False
        if kind == "purchase_order":
            return PurchaseOrder.objects.filter(pk=resource_id, organization=organization).exists()
        return Receipt.objects.filter(pk=resource_id, organization=organization).exists()
    return False


def _staged_attachment(resource_type: str, resource_id: str) -> bool:
    if resource_type not in STAGED_ATTACHMENT_TYPES:
        return False
    try:
        uuid.UUID(resource_id)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _enforce_staged_attachment_quota(request: Request, incoming_size: int) -> None:
    User.objects.select_for_update().get(pk=request.user.pk)
    cutoff = timezone.now() - timedelta(hours=settings.STAGED_ATTACHMENT_TTL_HOURS)
    usage = Attachment.objects.filter(
        organization=request.user.organization,
        uploader=request.user,
        resource_type__in=STAGED_ATTACHMENT_TYPES,
        created_at__gt=cutoff,
    ).aggregate(count=Count("pk"), total_bytes=Sum("size"))
    if int(usage["count"] or 0) >= settings.STAGED_ATTACHMENT_MAX_COUNT:
        raise DomainError(
            "Too many attachments are waiting to synchronize",
            code="staged_attachment_quota_exceeded",
            status=429,
        )
    if int(usage["total_bytes"] or 0) + incoming_size > settings.STAGED_ATTACHMENT_MAX_BYTES:
        raise DomainError(
            "Staged attachment storage limit exceeded",
            code="staged_attachment_quota_exceeded",
            status=429,
        )


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser])
@require_permission("attachments.create")
def attachments(request: Request) -> Response:
    org = request.user.organization
    if request.method == "GET":
        resource_type = request.query_params.get("resource_type", "")
        resource_id = request.query_params.get("resource_id", "")
        staged = _staged_attachment(resource_type, resource_id)
        if not staged and not _attachment_target_allowed(
            request.user, resource_type, resource_id, request.auth
        ):
            raise DomainError(
                "Attachment access is not allowed", code="permission_denied", status=403
            )
        target_rows = (
            Attachment.objects.filter(
                organization=org, resource_type=resource_type, resource_id=resource_id
            )
            .filter(technical_document__isnull=True)
            .order_by("document_key", "version", "created_at")
        )
        all_successors = dict(
            target_rows.exclude(supersedes_id__isnull=True).values_list("supersedes_id", "pk")
        )
        rows = target_rows
        if not can_view_financials(request.user, request.auth):
            rows = rows.filter(sensitivity=Attachment.Sensitivity.OPERATIONAL)
        if staged or "driver" in request.user.role_slugs:
            rows = rows.filter(uploader=request.user)
        attachments_for_target = list(rows)
        visible_ids = {item.pk for item in attachments_for_target}
        visible_successors = {
            item.supersedes_id: item.pk
            for item in attachments_for_target
            if item.supersedes_id is not None
        }
        return Response(
            {
                "attachments": [
                    _attachment_json(
                        item,
                        superseded_by_id=(
                            visible_successors.get(item.pk)
                            if all_successors.get(item.pk) in visible_ids
                            else None
                        ),
                        is_current=item.pk not in all_successors,
                    )
                    for item in attachments_for_target
                ]
            }
        )

    def store() -> Response:
        upload = request.FILES.get("file")
        if not upload:
            raise DomainError("A file is required", code="file_required")
        content_type, size = _validate_upload(upload)
        resource_type = str(request.data.get("resource_type", ""))[:80]
        resource_id = str(request.data.get("resource_id", ""))[:80]
        raw_sensitivity = request.data.get("sensitivity")
        raw_supersedes_id = str(request.data.get("supersedes_id", "")).strip()
        sensitivity = _attachment_sensitivity(
            resource_type,
            raw_sensitivity,
            require_explicit=not bool(raw_supersedes_id),
        )
        staged = _staged_attachment(resource_type, resource_id)
        if not staged and not _attachment_target_allowed(
            request.user, resource_type, resource_id, request.auth
        ):
            raise DomainError(
                "Attachment target is not allowed", code="permission_denied", status=403
            )
        if staged:
            _enforce_staged_attachment_quota(request, size)
        original_name = Path(upload.name).name[:255]
        raw_category = request.data.get("category")
        raw_title = request.data.get("title")
        category = str(raw_category).strip() if raw_category is not None else ""
        title = str(raw_title).strip() if raw_title is not None else original_name
        if len(category) > 80 or len(title) > 255:
            raise DomainError(
                "Attachment category or title is too long", code="invalid_attachment_metadata"
            )

        previous: Attachment | None = None
        if raw_supersedes_id:
            try:
                supersedes_id = uuid.UUID(raw_supersedes_id)
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    "supersedes_id must be a UUID", code="invalid_supersedes_id"
                ) from exc
            previous = (
                Attachment.objects.select_for_update()
                .filter(pk=supersedes_id, organization=org)
                .first()
            )
            if not previous:
                raise DomainError(
                    "The prior attachment was not found",
                    code="attachment_not_found",
                    status=404,
                )
            previous_is_staged = _staged_attachment(previous.resource_type, previous.resource_id)
            if (
                previous_is_staged or "driver" in request.user.role_slugs
            ) and previous.uploader_id != request.user.pk:
                raise DomainError(
                    "The prior attachment was not found",
                    code="attachment_not_found",
                    status=404,
                )
            if previous_is_staged:
                raise DomainError(
                    "A staged attachment cannot be versioned until synchronization completes",
                    code="attachment_staged",
                    status=409,
                )
            if (
                previous.resource_type.casefold() != resource_type.casefold()
                or previous.resource_id != resource_id
            ):
                raise DomainError(
                    "A new version must use the same attachment target",
                    code="attachment_target_mismatch",
                    status=409,
                )
            if Attachment.objects.filter(supersedes=previous).exists():
                raise DomainError(
                    "The prior attachment already has a newer version",
                    code="attachment_not_current",
                    status=409,
                )
            if Document.objects.filter(attachment=previous).exists():
                raise DomainError(
                    "Replace a technical manual through the document library",
                    code="document_replacement_required",
                    status=409,
                )
            resource_type = previous.resource_type
            resource_id = previous.resource_id
            if raw_sensitivity in (None, ""):
                sensitivity = previous.sensitivity
            elif sensitivity != previous.sensitivity:
                raise DomainError(
                    "A new version must keep the prior attachment sensitivity",
                    code="attachment_sensitivity_mismatch",
                    status=409,
                )
            if raw_category is None:
                category = previous.category
            if raw_title is None:
                title = previous.title

        _require_attachment_sensitivity_manage(request.user, sensitivity, request.auth)

        digest = hashlib.sha256()
        for chunk in upload.chunks():
            digest.update(chunk)
        upload.seek(0)
        attachment = Attachment(
            organization=org,
            uploader=request.user,
            resource_type=resource_type,
            resource_id=resource_id,
            sensitivity=sensitivity,
            document_key=previous.document_key if previous else uuid.uuid4(),
            category=category,
            title=title,
            version=previous.version + 1 if previous else 1,
            supersedes=previous,
            file=upload,
            original_name=original_name,
            content_type=content_type,
            size=size,
            sha256=digest.hexdigest(),
        )
        try:
            attachment.full_clean()
        except ValidationError as exc:
            raise DomainError(
                "Attachment version is invalid",
                code="invalid_attachment_version",
                details=getattr(exc, "message_dict", None) or exc.messages,
            ) from exc
        attachment.save()
        correlation_id = request.headers.get("Idempotency-Key", "")
        audit(
            organization=org,
            actor=request.user,
            action="attachment.created",
            resource=attachment,
            context={
                "sha256": attachment.sha256,
                "document_key": str(attachment.document_key),
                "version": attachment.version,
                "sensitivity": attachment.sensitivity,
                "supersedes_id": str(previous.pk) if previous else None,
            },
            correlation_id=correlation_id,
        )
        if previous:
            audit(
                organization=org,
                actor=request.user,
                action="attachment.superseded",
                resource=previous,
                previous_state=str(previous.version),
                new_state=str(attachment.version),
                context={
                    "document_key": str(attachment.document_key),
                    "superseded_by_id": str(attachment.pk),
                },
                correlation_id=correlation_id,
            )
        return Response(
            {"attachment": _attachment_json(attachment)},
            status=201,
        )

    return idempotent(request, store)


@api_view(["GET"])
def attachment_download(request: Request, attachment_id: uuid.UUID) -> FileResponse:
    attachment = get_object_or_404(
        Attachment, pk=attachment_id, organization=request.user.organization
    )
    document = Document.objects.select_related("asset").filter(attachment=attachment).first()
    document_source_allowed = False
    if document:
        from .document_library import can_access_document_asset

        document_source_allowed = can_access_document_asset(request, document.asset)
    staged = _staged_attachment(attachment.resource_type, attachment.resource_id)
    allowed = (
        has_permission(request.user, "documents.view", request.auth)
        and document_source_allowed
        and (
            document.status in {Document.Status.INDEXED, Document.Status.OCR_UNAVAILABLE}
            or has_permission(request.user, "documents.manage", request.auth)
        )
        if document
        else (
            staged
            and attachment.uploader_id == request.user.pk
            and _attachment_is_visible(request.user, attachment, request.auth)
        )
        or (
            not staged
            and _attachment_is_visible(request.user, attachment, request.auth)
            and _attachment_target_allowed(
                request.user, attachment.resource_type, attachment.resource_id, request.auth
            )
        )
    )
    if not allowed:
        raise DomainError("Attachment access is not allowed", code="permission_denied", status=403)
    response = FileResponse(
        attachment.file.open("rb"),
        content_type=attachment.content_type,
        as_attachment=True,
        filename=attachment.original_name,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@api_view(["GET", "POST"])
def comments(request: Request) -> Response:
    if not has_permission(request.user, "comments.create", request.auth):
        raise DomainError("Comment access is not allowed", code="permission_denied", status=403)
    org = request.user.organization
    source = request.query_params if request.method == "GET" else request.data
    resource_type = str(source.get("resource_type", "")).strip()
    resource_id = str(source.get("resource_id", "")).strip()
    if not resource_type or not resource_id or len(resource_type) > 80 or len(resource_id) > 80:
        raise DomainError("A valid comment target is required", code="invalid_comment_target")
    if not _attachment_target_allowed(request.user, resource_type, resource_id, request.auth):
        raise DomainError("Comment target is not allowed", code="permission_denied", status=403)
    if request.method == "GET":
        rows = Comment.objects.filter(
            organization=org,
            resource_type=resource_type,
            resource_id=resource_id,
        ).select_related("author")
        return Response(
            {
                "comments": [
                    {
                        "id": str(x.pk),
                        "body": x.body,
                        "author": x.author.get_full_name() or x.author.username,
                        "created_at": x.created_at,
                    }
                    for x in rows
                ]
            }
        )
    body = str(request.data.get("body", "")).strip()
    if not body:
        raise DomainError("A comment is required", code="comment_required")
    if len(body) > 5000:
        raise DomainError("Comment is too long", code="comment_too_long")

    def create() -> Response:
        row = Comment.objects.create(
            organization=org,
            author=request.user,
            resource_type=resource_type,
            resource_id=resource_id,
            body=body,
        )
        audit(
            organization=org,
            actor=request.user,
            action="comment.created",
            resource=row,
            correlation_id=request.headers.get("Idempotency-Key", ""),
        )
        return Response({"comment": {"id": str(row.pk), "body": row.body}}, status=201)

    return idempotent(request, create)


@api_view(["GET", "POST"])
def notifications(request: Request) -> Response:
    rows = Notification.objects.filter(
        organization=request.user.organization, user=request.user
    ).order_by("-created_at")
    if request.method == "POST":
        notification = get_object_or_404(rows, pk=request.data.get("id"))
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])
    return Response(
        {
            "notifications": [
                {
                    "id": str(x.pk),
                    "title": x.title,
                    "body": x.body,
                    "resource_type": x.resource_type,
                    "resource_id": x.resource_id,
                    "read_at": x.read_at,
                }
                for x in rows[:100]
            ]
        }
    )


@api_view(["GET", "POST"])
@require_permission("webhooks.manage")
def webhooks(request: Request) -> Response:
    org = request.user.organization
    if request.method == "GET":
        rows = WebhookSubscription.objects.filter(organization=org)
        return Response(
            {
                "webhooks": [
                    {
                        "id": str(x.pk),
                        "name": x.name,
                        "url": x.url,
                        "event_types": x.event_types,
                        "active": x.active,
                    }
                    for x in rows
                ]
            }
        )

    def create() -> Response:
        name = str(request.data.get("name", "")).strip()
        url = str(request.data.get("url", "")).strip()
        raw_event_types = request.data.get("event_types")
        if not name or len(name) > 120:
            raise DomainError("Webhook name must be between 1 and 120 characters")
        if len(url) > 500:
            raise DomainError("Webhook URL is too long", code="invalid_webhook_url")
        if not isinstance(raw_event_types, list) or not raw_event_types:
            raise DomainError(
                "event_types must be a non-empty list",
                code="invalid_webhook_event_types",
            )
        if len(raw_event_types) > settings.WEBHOOK_MAX_EVENT_TYPES:
            raise DomainError(
                "event_types exceeds the configured item limit",
                code="too_many_webhook_event_types",
            )
        event_types: list[str] = []
        for value in raw_event_types:
            event_type = str(value).strip()
            if len(event_type) > 100 or not WEBHOOK_EVENT_TYPE_PATTERN.fullmatch(event_type):
                raise DomainError(
                    "event_types contains an invalid event type",
                    code="invalid_webhook_event_types",
                )
            if event_type not in event_types:
                event_types.append(event_type)
        try:
            validate_outbound_url(url)
        except ValueError as exc:
            raise DomainError(str(exc), code="invalid_webhook_url") from exc
        signing_secret = secrets.token_urlsafe(32)
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=org.pk)
            if (
                WebhookSubscription.objects.filter(organization=org).count()
                >= settings.WEBHOOK_MAX_SUBSCRIPTIONS
            ):
                raise DomainError(
                    "Webhook subscription limit reached",
                    code="webhook_subscription_limit",
                    status=409,
                )
            row = WebhookSubscription.objects.create(
                organization=org,
                name=name,
                url=url,
                event_types=event_types,
                signing_secret=signing_secret,
            )
            audit(
                organization=org,
                actor=request.user,
                action="webhook.created",
                resource=row,
                context={"event_types": event_types},
                correlation_id=request.headers.get("Idempotency-Key", ""),
            )
        return Response(
            {
                "webhook": {
                    "id": str(row.pk),
                    "name": row.name,
                    "url": row.url,
                    "event_types": row.event_types,
                    "active": row.active,
                    "signing_secret": signing_secret,
                },
                "secret_recoverable": True,
            },
            status=201,
        )

    return idempotent(request, create, stored_response_transform=_redact_webhook_secret)


def _redact_webhook_secret(response: Response) -> object:
    webhook = dict(response.data["webhook"])
    webhook.pop("signing_secret", None)
    return {
        "webhook": webhook,
        "secret_recoverable": False,
        "message": (
            "The signing secret was shown once and cannot be recovered; "
            "rotate it to obtain a new secret."
        ),
    }


@api_view(["POST"])
@require_permission("webhooks.manage")
def rotate_webhook_secret(request: Request, webhook_id: uuid.UUID) -> Response:
    organization = request.user.organization

    def rotate() -> Response:
        signing_secret = secrets.token_urlsafe(32)
        with transaction.atomic():
            webhook = get_object_or_404(
                WebhookSubscription.objects.select_for_update(),
                pk=webhook_id,
                organization=organization,
            )
            webhook.signing_secret = signing_secret
            webhook.save(update_fields=["signing_secret", "updated_at"])
            audit(
                organization=organization,
                actor=request.user,
                action="webhook.secret_rotated",
                resource=webhook,
                correlation_id=request.headers.get("Idempotency-Key", ""),
            )
        return Response(
            {
                "webhook": {
                    "id": str(webhook.pk),
                    "name": webhook.name,
                    "url": webhook.url,
                    "event_types": webhook.event_types,
                    "active": webhook.active,
                    "signing_secret": signing_secret,
                },
                "secret_recoverable": True,
            }
        )

    return idempotent(request, rotate, stored_response_transform=_redact_webhook_secret)


@api_view(["POST"])
@require_permission("webhooks.manage")
def set_webhook_status(request: Request, webhook_id: uuid.UUID) -> Response:
    organization = request.user.organization
    status = str(request.data.get("status", "")).strip().lower()
    if status not in {"active", "inactive"}:
        raise DomainError(
            "status must be active or inactive",
            code="invalid_webhook_status",
        )

    def update_status() -> Response:
        with transaction.atomic():
            webhook = get_object_or_404(
                WebhookSubscription.objects.select_for_update(),
                pk=webhook_id,
                organization=organization,
            )
            previous = "active" if webhook.active else "inactive"
            webhook.active = status == "active"
            webhook.save(update_fields=["active", "updated_at"])
            cancelled = 0
            if not webhook.active:
                cancelled = WebhookDelivery.objects.filter(
                    subscription=webhook,
                    status__in=["pending", "retry"],
                ).update(
                    status="dead",
                    delivered_at=None,
                    last_error="Subscription deactivated",
                    updated_at=timezone.now(),
                )
            if previous != status:
                audit(
                    organization=organization,
                    actor=request.user,
                    action="webhook.status_changed",
                    resource=webhook,
                    previous_state=previous,
                    new_state=status,
                    context={"cancelled_deliveries": cancelled},
                    correlation_id=request.headers.get("Idempotency-Key", ""),
                )
        return Response(
            {
                "webhook": {
                    "id": str(webhook.pk),
                    "name": webhook.name,
                    "url": webhook.url,
                    "event_types": webhook.event_types,
                    "active": webhook.active,
                }
            }
        )

    return idempotent(request, update_status)


def _webhook_delivery_json(delivery: WebhookDelivery) -> dict[str, object]:
    event = delivery.outbox_event
    return {
        "id": str(delivery.pk),
        "status": delivery.status,
        "attempts": delivery.attempts,
        "response_status": delivery.response_status,
        "next_attempt_at": delivery.next_attempt_at,
        "delivered_at": delivery.delivered_at,
        "last_error": delivery.last_error,
        "created_at": delivery.created_at,
        "updated_at": delivery.updated_at,
        "subscription": {
            "id": str(delivery.subscription_id),
            "name": delivery.subscription.name,
            "url": delivery.subscription.url,
        },
        "event": {
            "id": str(event.pk),
            "type": event.event_type,
            "occurred_at": event.created_at,
            "resource": {"type": event.resource_type, "id": event.resource_id},
        },
    }


@api_view(["GET"])
@require_permission("webhooks.manage")
def webhook_deliveries(request: Request) -> Response:
    status_filter = request.query_params.get("status", "")
    allowed_statuses = {"pending", "retry", "delivered", "dead"}
    if status_filter and status_filter not in allowed_statuses:
        raise DomainError("Invalid delivery status", code="invalid_delivery_status")
    rows = WebhookDelivery.objects.filter(organization=request.user.organization)
    dead_letter_count = rows.filter(status="dead").count()
    if status_filter:
        rows = rows.filter(status=status_filter)
    rows = rows.select_related("subscription", "outbox_event").order_by("-updated_at")[:200]
    return Response(
        {
            "deliveries": [_webhook_delivery_json(row) for row in rows],
            "dead_letter_count": dead_letter_count,
        }
    )


@api_view(["POST"])
@require_permission("webhooks.manage")
def retry_webhook_delivery(request: Request, delivery_id: uuid.UUID) -> Response:
    organization = request.user.organization

    def perform() -> Response:
        with transaction.atomic():
            delivery = get_object_or_404(
                WebhookDelivery.objects.select_for_update().select_related(
                    "subscription", "outbox_event"
                ),
                organization=organization,
                pk=delivery_id,
            )
            if delivery.status not in {"dead", "retry"}:
                raise DomainError(
                    "Only failed webhook deliveries can be retried",
                    code="webhook_delivery_not_retryable",
                    status=409,
                )
            previous_status = delivery.status
            previous_attempts = delivery.attempts
            previous_response_status = delivery.response_status
            previous_last_error = delivery.last_error
            delivery.status = "retry"
            delivery.attempts = 0
            delivery.next_attempt_at = timezone.now()
            delivery.save(update_fields=["status", "attempts", "next_attempt_at", "updated_at"])
            audit(
                organization=organization,
                actor=request.user,
                action="webhook.delivery_retried",
                resource=delivery,
                previous_state=previous_status,
                new_state="retry",
                context={
                    "previous_attempts": previous_attempts,
                    "previous_response_status": previous_response_status,
                    "previous_last_error": previous_last_error,
                },
            )
        return Response({"delivery": _webhook_delivery_json(delivery)})

    return idempotent(request, perform)


@api_view(["GET"])
@require_permission("audit.view")
def audit_events(request: Request) -> Response:
    rows = AuditEvent.objects.filter(organization=request.user.organization)
    resource_type = request.query_params.get("resource_type")
    resource_id = request.query_params.get("resource_id")
    if resource_type:
        rows = rows.filter(resource_type=resource_type)
    if resource_id:
        rows = rows.filter(resource_id=resource_id)
    include_financial = can_view_financials(request.user, request.auth)

    def audit_json(event: AuditEvent) -> dict[str, object]:
        labor_event = event.action.startswith("labor.")
        return {
            "id": str(event.pk),
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "previous_state": (
                event.previous_state if include_financial or not labor_event else ""
            ),
            "new_state": event.new_state if include_financial or not labor_event else "",
            "actor": event.actor.username if event.actor else None,
            "context": (
                event.context if include_financial else redact_financial_fields(event.context)
            ),
            "source": event.source,
            "correlation_id": event.correlation_id,
            "occurred_at": event.occurred_at,
        }

    return Response({"events": [audit_json(event) for event in rows[:500]]})


@api_view(["POST"])
def offline_sync(request: Request) -> Response:
    from inventory.services import sync_inventory_operation
    from maintenance.services import sync_field_operation

    from .offline_access import validate_offline_access_grant

    validate_offline_access_grant(
        request.headers.get("X-Offline-Grant"),
        user=request.user,
        auth=request.auth,
    )
    may_view_financials = can_view_financials(request.user, request.auth)

    if not hasattr(request.data, "get"):
        raise DomainError("Request body must be an object", code="invalid_operations")
    operations = request.data.get("operations", [])
    if not isinstance(operations, list) or len(operations) > 100:
        raise DomainError(
            "operations must be a list of at most 100 items", code="invalid_operations"
        )
    results = []
    for index, candidate in enumerate(operations):
        operation: dict[str, object] = {}
        operation_type = ""
        try:
            if not isinstance(candidate, dict):
                raise DomainError("Offline operation must be an object", code="invalid_operation")
            operation = candidate
            try:
                uuid.UUID(str(operation.get("operation_id", "")))
            except (AttributeError, TypeError, ValueError) as exc:
                raise DomainError(
                    "A valid operation_id is required", code="operation_id_required"
                ) from exc
            raw_operation_type = operation.get("type")
            if not isinstance(raw_operation_type, str) or not raw_operation_type:
                raise DomainError(
                    "Offline operation type is required", code="invalid_operation_type"
                )
            operation_type = raw_operation_type
            if not isinstance(operation.get("payload"), dict):
                raise DomainError("Operation payload must be an object", code="invalid_payload")
            if operation_type in {
                "defect.create",
                "inspection.submit",
                "work_note.create",
                "task.complete",
            }:
                result = sync_field_operation(request.user, operation, auth=request.auth)
            elif operation_type in {"stock.issue", "stock.return"}:
                result = sync_inventory_operation(request.user, operation, auth=request.auth)
            else:
                raise DomainError(
                    "Unsupported offline operation", code="unsupported_offline_operation"
                )
            results.append(
                {
                    "operation_id": operation.get("operation_id"),
                    "status": "synced",
                    "result": result,
                }
            )
        except DomainError as exc:
            if exc.status == 409:
                conflict, _ = SyncConflict.objects.get_or_create(
                    organization=request.user.organization,
                    user=request.user,
                    operation_id=operation.get("operation_id"),
                    defaults={
                        "operation_type": operation_type,
                        "message": exc.message,
                        "client_payload": json.loads(json.dumps(operation, default=str)),
                        "server_payload": json.loads(json.dumps(exc.details or {}, default=str)),
                    },
                )
                results.append(
                    {
                        "operation_id": operation.get("operation_id"),
                        "status": "conflict",
                        "message": conflict.message,
                        "client": (
                            conflict.client_payload
                            if may_view_financials
                            else redact_financial_fields(conflict.client_payload)
                        ),
                        "server": (
                            conflict.server_payload
                            if may_view_financials
                            else redact_financial_fields(conflict.server_payload)
                        ),
                        "resolution_options": ["review_server", "discard_local"],
                    }
                )
            else:
                results.append(
                    {
                        "operation_id": (
                            candidate.get("operation_id") if isinstance(candidate, dict) else None
                        ),
                        "index": index,
                        "status": "rejected",
                        "message": exc.message,
                        "code": exc.code,
                    }
                )
    return Response({"results": results})


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
@require_permission("import.manage")
def import_assets(request: Request) -> Response:
    from assets.models import Asset, AssetType

    from core.models import Location

    def perform() -> Response:
        upload = request.FILES.get("file")
        if not upload:
            raise DomainError("A CSV file is required", code="file_required")
        if (upload.size or 0) <= 0 or (upload.size or 0) > settings.ATTACHMENT_MAX_BYTES:
            raise DomainError("CSV file size is not allowed", code="invalid_csv_size")
        try:
            text = upload.read().decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(text), strict=True))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise DomainError("CSV could not be read", code="invalid_csv") from exc
        if len(rows) > 5000:
            raise DomainError("CSV contains more than 5,000 rows", code="import_too_large")
        created: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        org = request.user.organization
        for number, row in enumerate(rows, start=2):
            try:
                with transaction.atomic():
                    unit = (row.get("unit_number") or "").strip()
                    type_name = (row.get("asset_type") or "Truck").strip()
                    location_code = (row.get("location_code") or "MAIN").strip()
                    if not unit:
                        raise ValueError("unit_number is required")
                    asset_type, _ = AssetType.objects.get_or_create(
                        organization=org,
                        name=type_name,
                        defaults={"category": "vehicle"},
                    )
                    location = Location.objects.get(organization=org, code=location_code)
                    asset = Asset(
                        organization=org,
                        asset_type=asset_type,
                        home_location=location,
                        unit_number=unit,
                        vin=(row.get("vin") or "").strip(),
                    )
                    asset.full_clean()
                    asset.save()
                    audit(
                        organization=org,
                        actor=request.user,
                        action="asset.imported",
                        resource=asset,
                        context={"csv_row": number},
                        correlation_id=request.headers.get("Idempotency-Key", ""),
                    )
                created.append({"row": number, "id": str(asset.pk), "unit_number": unit})
            except (IntegrityError, Location.DoesNotExist, ValidationError, ValueError) as exc:
                rejected.append({"row": number, "error": str(exc)[:300]})
        return Response(
            {
                "created": created,
                "rejected": rejected,
                "created_count": len(created),
                "rejected_count": len(rejected),
            }
        )

    return idempotent(request, perform)


@api_view(["GET"])
@require_permission("export.all")
def export_data(request: Request) -> HttpResponse:
    if not can_export_financials(request.user, request.auth):
        raise DomainError(
            "Permission required: financial.export",
            code="financial_permission_denied",
            status=403,
        )
    org = request.user.organization
    response = HttpResponse(
        encode_export(build_organization_export(org)), content_type="application/json"
    )
    response["Content-Disposition"] = f'attachment; filename="fleetline-{org.slug}-export.json"'
    return response


@api_view(["POST"])
@require_permission("admin.users")
def revoke_user_offline_access(request: Request, user_id: uuid.UUID) -> Response:
    _require_interactive_session(request)
    reason = str(request.data.get("reason", "")).strip()
    if len(reason) > 500:
        raise DomainError("Revocation reason is too long", code="invalid_reason")

    def revoke() -> Response:
        with transaction.atomic():
            user = get_object_or_404(
                User.objects.select_for_update(),
                pk=user_id,
                organization=request.user.organization,
            )
            organization = user.organization
            if organization is None:
                raise DomainError("User has no organization", code="invalid_user_organization")
            previous_revocation = user.offline_access_revoked_at
            revoked_at = timezone.now()
            user.offline_access_revoked_at = revoked_at
            user.save(update_fields=["offline_access_revoked_at"])
            audit(
                organization=organization,
                actor=request.user,
                action="user.offline_access_revoked",
                resource=user,
                previous_state=(previous_revocation.isoformat() if previous_revocation else ""),
                new_state=revoked_at.isoformat(),
                context={"reason": reason},
                correlation_id=request.headers.get("Idempotency-Key", ""),
            )
        return Response(
            {
                "user": {
                    "id": str(user.pk),
                    "active": user.is_active,
                    "offline_access_revoked_at": revoked_at.isoformat(),
                }
            }
        )

    return idempotent(request, revoke)


@api_view(["POST"])
@require_permission("admin.users")
def disable_user(request: Request, user_id: uuid.UUID) -> Response:
    _require_interactive_session(request)
    with transaction.atomic():
        user = get_object_or_404(
            User.objects.select_for_update(),
            pk=user_id,
            organization=request.user.organization,
        )
        if user.pk == request.user.pk:
            raise DomainError("You cannot disable your own account", code="cannot_disable_self")
        organization = user.organization
        if organization is None:
            raise DomainError("User has no organization", code="invalid_user_organization")
        now = timezone.now()
        user.is_active = False
        user.offline_access_revoked_at = now
        user.save(update_fields=["is_active", "offline_access_revoked_at"])
        revoked_tokens = ApiToken.objects.filter(
            organization=organization,
            user=user,
            revoked_at__isnull=True,
        ).update(revoked_at=now)
        audit(
            organization=organization,
            actor=request.user,
            action="user.disabled",
            resource=user,
            context={"api_tokens_revoked": revoked_tokens},
        )
    return Response({"user": {"id": str(user.pk), "active": False}})
