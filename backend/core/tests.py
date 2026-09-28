from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIClient

from .exceptions import DomainError
from .management.commands.runworker import Command, _open_webhook
from .models import (
    ApiToken,
    Attachment,
    AuditEvent,
    IdempotencyRecord,
    Location,
    LoginAttemptThrottle,
    Notification,
    Organization,
    OutboxEvent,
    Role,
    User,
    WebhookDelivery,
    WebhookSubscription,
    WorkerHeartbeat,
)
from .permissions import has_permission, navigation_for, permissions_for
from .security import ResolvedOutboundURL, totp, validate_outbound_url
from .services import audit, idempotent

TEST_PASSWORD = "correct horse battery staple"  # noqa: S105
NEW_USER_PASSWORD = "Fleet!Workshop-2026-Access"  # noqa: S105
MFA_SECRET = "JBSWY3DPEHPK3PXP"  # noqa: S105
TEST_WEBHOOK_SECRET = "test-signing-secret"  # noqa: S105
OTHER_WEBHOOK_SECRET = "other-signing-secret"  # noqa: S105


class CoreApiTests(TestCase):
    org: Organization
    other_org: Organization
    location: Location
    other_location: Location
    roles: dict[str, Role]
    other_driver_role: Role
    other_technician_role: Role
    driver: User
    other_driver: User
    technician: User
    inactive_technician: User
    supervisor: User
    admin: User
    outsider: User
    other_technician: User
    client: APIClient

    @override_settings(REQUIRE_WORKER=True)
    def test_readiness_rejects_worker_with_pm_recalculation_error(self) -> None:
        WorkerHeartbeat.objects.create(
            name="default",
            seen_at=timezone.now(),
            details={"pm_recalculation_error": "DatabaseError: unavailable"},
        )

        response = self.client.get(reverse("health-ready"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["worker"], "failed")
        self.assertEqual(
            response.json()["checks"]["worker_error"],
            "preventive_maintenance_recalculation",
        )

    @classmethod
    def setUpTestData(cls) -> None:
        cls.org = Organization.objects.create(name="North Fleet", slug="north")
        cls.other_org = Organization.objects.create(name="South Fleet", slug="south")
        cls.location = Location.objects.create(organization=cls.org, name="Main Shop", code="MAIN")
        cls.other_location = Location.objects.create(
            organization=cls.other_org, name="South Shop", code="MAIN"
        )
        cls.roles = {
            slug: Role.objects.create(
                organization=cls.org, slug=slug, name=slug.replace("_", " ").title()
            )
            for slug in ("driver", "technician", "supervisor", "system_admin")
        }
        cls.other_driver_role = Role.objects.create(
            organization=cls.other_org, slug="driver", name="Driver"
        )
        cls.other_technician_role = Role.objects.create(
            organization=cls.other_org, slug="technician", name="Technician"
        )
        cls.driver = cls._user("driver", cls.org, cls.location, cls.roles["driver"])
        cls.other_driver = cls._user("other-driver", cls.org, cls.location, cls.roles["driver"])
        cls.technician = cls._user("technician", cls.org, cls.location, cls.roles["technician"])
        cls.inactive_technician = cls._user(
            "inactive-technician", cls.org, cls.location, cls.roles["technician"]
        )
        cls.inactive_technician.is_active = False
        cls.inactive_technician.save(update_fields=["is_active"])
        cls.supervisor = cls._user("supervisor", cls.org, cls.location, cls.roles["supervisor"])
        cls.admin = cls._user("admin", cls.org, cls.location, cls.roles["system_admin"])
        cls.outsider = cls._user(
            "outsider", cls.other_org, cls.other_location, cls.other_driver_role
        )
        cls.other_technician = cls._user(
            "other-technician",
            cls.other_org,
            cls.other_location,
            cls.other_technician_role,
        )

    @classmethod
    def _user(
        cls, username: str, organization: Organization, location: Location, role: Role
    ) -> User:
        user = User.objects.create_user(
            username=username,
            password=TEST_PASSWORD,
            organization=organization,
            default_location=location,
        )
        user.roles.add(role)
        return user

    def setUp(self) -> None:
        self.client = APIClient()
        self.media_dir = tempfile.mkdtemp(prefix="fleetline-core-test-")
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_dir, True)

    def test_login_logout_and_csrf(self) -> None:
        csrf_client = APIClient(enforce_csrf_checks=True)
        login_url = reverse("login")

        self.assertEqual(
            csrf_client.post(
                login_url,
                {"username": self.driver.username, "password": TEST_PASSWORD},
                format="json",
            ).status_code,
            403,
        )
        csrf_response = csrf_client.get(reverse("csrf"))
        self.assertFalse(csrf_response.json()["authenticated"])
        token = csrf_response.json()["csrf_token"]
        response = csrf_client.post(
            login_url,
            {"username": " DRIVER ", "password": TEST_PASSWORD},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["roles"], ["driver"])
        self.assertTrue(csrf_client.get(reverse("csrf")).json()["authenticated"])
        self.assertEqual(csrf_client.get(reverse("me")).status_code, 200)

        session_csrf = csrf_client.cookies["csrftoken"].value
        response = csrf_client.post(
            reverse("logout"), {}, format="json", HTTP_X_CSRFTOKEN=session_csrf
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(csrf_client.get(reverse("me")).status_code, 403)

    def test_invalid_disabled_and_revoked_session_authentication(self) -> None:
        login_url = reverse("login")
        response = self.client.post(
            login_url, {"username": self.driver.username, "password": "wrong"}, format="json"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

        response = self.client.post(
            login_url,
            {"username": self.driver.username, "password": TEST_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        User.objects.filter(pk=self.driver.pk).update(is_active=False)
        self.assertEqual(self.client.get(reverse("me")).status_code, 403)
        self.assertEqual(
            APIClient()
            .post(
                login_url,
                {"username": self.driver.username, "password": TEST_PASSWORD},
                format="json",
            )
            .status_code,
            401,
        )

    def test_privileged_login_requires_configured_valid_mfa(self) -> None:
        login_url = reverse("login")
        credentials = {"username": self.admin.username, "password": TEST_PASSWORD}
        response = self.client.post(login_url, credentials, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

        User.objects.filter(pk=self.admin.pk).update(mfa_secret=MFA_SECRET)
        self.admin.refresh_from_db()
        response = self.client.post(login_url, credentials, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "mfa_required")
        response = self.client.post(login_url, credentials | {"otp": "abcdef"}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

        instant = 1_800_000_000
        with patch("core.security.time.time", return_value=instant):
            response = self.client.post(
                login_url, credentials | {"otp": totp(MFA_SECRET, instant)}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["username"], self.admin.username)

        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        password_only = APIClient()
        self.assertEqual(
            password_only.post(
                "/admin/login/",
                {"username": self.admin.username, "password": TEST_PASSWORD},
            ).status_code,
            404,
        )
        self.assertEqual(password_only.get(reverse("users")).status_code, 403)

    def test_django_superuser_flag_does_not_grant_product_authority_and_requires_mfa(self) -> None:
        legacy = User.objects.create_superuser(
            username="legacy-superuser",
            password=TEST_PASSWORD,
            organization=self.org,
            default_location=self.location,
        )
        self.assertEqual(permissions_for(legacy), set())
        self.assertFalse(has_permission(legacy, "admin.users"))

        client = APIClient()
        credentials = {"username": legacy.username, "password": TEST_PASSWORD}
        response = client.post(reverse("login"), credentials, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

        legacy.mfa_secret = MFA_SECRET
        legacy.save(update_fields=["mfa_secret"])
        response = client.post(reverse("login"), credentials, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "mfa_required")
        instant = 1_800_000_000
        with patch("core.security.time.time", return_value=instant):
            response = client.post(
                reverse("login"),
                credentials | {"otp": totp(MFA_SECRET, instant)},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["permissions"], [])
        self.assertEqual(client.get(reverse("api-tokens")).status_code, 403)

    @override_settings(
        LOGIN_ATTEMPT_LIMIT=3,
        LOGIN_ATTEMPT_WINDOW_SECONDS=60,
        LOGIN_LOCKOUT_SECONDS=120,
    )
    def test_login_throttle_is_generic_bounded_and_recovers_after_expiry(self) -> None:
        login_url = reverse("login")
        started_at = timezone.now()
        known = APIClient()
        unknown = APIClient()

        def attempt(client: APIClient, username: str, remote_addr: str):
            with patch("core.auth_throttle.timezone.now", return_value=started_at):
                return client.post(
                    login_url,
                    {"username": username, "password": "incorrect-password"},
                    format="json",
                    REMOTE_ADDR=remote_addr,
                )

        known_first = attempt(known, self.driver.username, "192.0.2.10")
        unknown_first = attempt(unknown, "does-not-exist", "192.0.2.20")
        self.assertEqual(known_first.status_code, 401)
        self.assertEqual(
            (known_first.status_code, known_first.json()),
            (unknown_first.status_code, unknown_first.json()),
        )
        for _ in range(2):
            known_last = attempt(known, self.driver.username, "192.0.2.10")
            unknown_last = attempt(unknown, "does-not-exist", "192.0.2.20")
        self.assertEqual(known_last.status_code, 429)
        self.assertEqual(
            (known_last.status_code, known_last.json()),
            (unknown_last.status_code, unknown_last.json()),
        )
        self.assertEqual(known_last.json()["error"]["code"], "login_throttled")
        self.assertEqual(known_last.json()["error"]["details"]["retry_after_seconds"], 120)
        self.assertEqual(known_last["Retry-After"], "120")

        # Arbitrary nonexistent names consume only the bounded client row, not durable account rows.
        spray = APIClient()
        for number in range(10):
            attempt(spray, f"invented-{number}", "192.0.2.30")
        self.assertEqual(
            LoginAttemptThrottle.objects.filter(
                dimension=LoginAttemptThrottle.Dimension.CLIENT
            ).count(),
            3,
        )
        self.assertEqual(
            LoginAttemptThrottle.objects.filter(
                dimension=LoginAttemptThrottle.Dimension.ACCOUNT
            ).count(),
            1,
        )
        serialized = json.dumps(list(LoginAttemptThrottle.objects.values("dimension", "key_hash")))
        self.assertNotIn(self.driver.username, serialized)
        self.assertNotIn("does-not-exist", serialized)

        with patch(
            "core.auth_throttle.timezone.now",
            return_value=started_at + timedelta(seconds=121),
        ):
            recovered = known.post(
                login_url,
                {"username": self.driver.username, "password": TEST_PASSWORD},
                format="json",
                REMOTE_ADDR="192.0.2.10",
            )
        self.assertEqual(recovered.status_code, 200)
        account = LoginAttemptThrottle.objects.get(dimension=LoginAttemptThrottle.Dimension.ACCOUNT)
        self.assertEqual(account.failure_count, 0)
        self.assertIsNone(account.locked_until)

    @override_settings(
        LOGIN_ATTEMPT_LIMIT=2,
        LOGIN_ATTEMPT_WINDOW_SECONDS=60,
        LOGIN_LOCKOUT_SECONDS=120,
    )
    def test_login_throttle_bounds_totp_guesses(self) -> None:
        User.objects.filter(pk=self.admin.pk).update(mfa_secret=MFA_SECRET)
        login_url = reverse("login")
        credentials = {"username": self.admin.username, "password": TEST_PASSWORD}
        started_at = timezone.now()
        client = APIClient()
        instant = 1_800_000_000
        valid_codes = {totp(MFA_SECRET, instant + drift * 30) for drift in (-1, 0, 1)}
        wrong_codes = [f"{value:06d}" for value in range(10) if f"{value:06d}" not in valid_codes]
        with (
            patch("core.auth_throttle.timezone.now", return_value=started_at),
            patch("core.security.time.time", return_value=instant),
        ):
            challenge = client.post(login_url, credentials, format="json", REMOTE_ADDR="192.0.2.40")
            first = client.post(
                login_url,
                credentials | {"otp": wrong_codes[0]},
                format="json",
                REMOTE_ADDR="192.0.2.40",
            )
            second = client.post(
                login_url,
                credentials | {"otp": wrong_codes[1]},
                format="json",
                REMOTE_ADDR="192.0.2.40",
            )
        self.assertEqual(challenge.json()["error"]["code"], "mfa_required")
        self.assertEqual(first.json()["error"]["code"], "invalid_credentials")
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "login_throttled")

    def test_role_permissions_navigation_and_scoped_token(self) -> None:
        self.supervisor.roles.add(self.roles["driver"])
        granted = permissions_for(self.supervisor)
        self.assertTrue({"maintenance.manage", "defects.create"}.issubset(granted))
        self.assertTrue(has_permission(self.supervisor, "maintenance.manage"))
        self.assertFalse(has_permission(self.driver, "audit.view"))
        self.assertEqual(
            [item["label"] for item in navigation_for(self.driver)],
            ["Home", "Inspect", "Report Problem", "My Reports"],
        )

        token = ApiToken.objects.create(
            organization=self.org,
            user=self.supervisor,
            name="narrow",
            prefix="test",
            token_hash="0" * 64,
            scopes=["dashboard.view"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.assertTrue(has_permission(self.supervisor, "dashboard.view", token))
        self.assertFalse(has_permission(self.supervisor, "maintenance.manage", token))
        token.scopes = ["*"]
        self.assertFalse(has_permission(self.supervisor, "maintenance.manage", token))

        self.client.force_authenticate(self.driver)
        self.assertEqual(self.client.get(reverse("audit-events")).status_code, 403)

    def test_api_token_issue_list_use_and_revoke_lifecycle(self) -> None:
        expires_at = timezone.now() + timedelta(days=7)
        issue_key = "00000000-0000-0000-0000-000000000601"
        payload = {
            "user_id": str(self.supervisor.pk),
            "name": "Reporting integration",
            "scopes": ["reports.shop"],
            "expires_at": expires_at.isoformat(),
        }
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("api-tokens"), payload, format="json", HTTP_IDEMPOTENCY_KEY=issue_key
        )
        self.assertEqual(response.status_code, 201)
        raw_token = response.json()["token"]
        token_id = response.json()["api_token"]["id"]
        token = ApiToken.objects.get(pk=token_id)
        self.assertTrue(raw_token.startswith(f"flt_{token.prefix}_"))
        self.assertEqual(token.token_hash, hashlib.sha256(raw_token.encode()).hexdigest())
        self.assertNotEqual(token.token_hash, raw_token)
        self.assertEqual(token.organization, self.org)
        self.assertEqual(token.user, self.supervisor)
        self.assertEqual(token.scopes, ["reports.shop"])
        self.assertGreater(token.expires_at, timezone.now())
        issued_event = AuditEvent.objects.get(action="api_token.issued", resource_id=token_id)
        self.assertEqual(issued_event.actor, self.admin)
        self.assertEqual(issued_event.correlation_id, issue_key)
        self.assertNotIn(raw_token, json.dumps(issued_event.context))

        replay = self.client.post(
            reverse("api-tokens"), payload, format="json", HTTP_IDEMPOTENCY_KEY=issue_key
        )
        self.assertEqual(replay.status_code, 201)
        self.assertNotIn("token", replay.json())
        self.assertFalse(replay.json()["secret_recoverable"])
        self.assertIn("cannot be recovered", replay.json()["message"])
        issuance_record = IdempotencyRecord.objects.get(
            organization=self.org,
            user=self.admin,
            route=reverse("api-tokens"),
            key=issue_key,
        )
        self.assertNotIn(raw_token, json.dumps(issuance_record.response_body))
        self.assertEqual(ApiToken.objects.filter(pk=token_id).count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(action="api_token.issued", resource_id=token_id).count(), 1
        )
        conflict = self.client.post(
            reverse("api-tokens"),
            payload | {"name": "Different request"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=issue_key,
        )
        self.assertEqual(conflict.status_code, 409)

        other_token = ApiToken.objects.create(
            organization=self.other_org,
            user=self.outsider,
            name="Other tenant",
            prefix="other",
            token_hash="f" * 64,
            scopes=["dashboard.view"],
            expires_at=timezone.now() + timedelta(days=1),
        )
        listed = self.client.get(reverse("api-tokens"))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row["id"] for row in listed.json()["api_tokens"]], [token_id])
        self.assertNotIn(str(other_token.pk), listed.content.decode())
        self.assertNotIn("token_hash", listed.json()["api_tokens"][0])

        bearer = APIClient()
        bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
        self.assertEqual(bearer.get(reverse("operations-report")).status_code, 200)
        token.refresh_from_db()
        self.assertIsNotNone(token.last_used_at)

        revoke_key = "00000000-0000-0000-0000-000000000602"
        revoke_url = reverse("api-token-revoke", args=[token.pk])
        revoked = self.client.post(
            revoke_url,
            {"reason": "Integration retired"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=revoke_key,
        )
        self.assertEqual(revoked.status_code, 200)
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        self.assertEqual(bearer.get(reverse("operations-report")).status_code, 403)
        self.assertEqual(
            AuditEvent.objects.filter(action="api_token.revoked", resource_id=token_id).count(), 1
        )
        replay_revoke = self.client.post(
            revoke_url,
            {"reason": "Integration retired"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=revoke_key,
        )
        self.assertEqual(replay_revoke.status_code, 200)
        self.assertEqual(
            AuditEvent.objects.filter(action="api_token.revoked", resource_id=token_id).count(), 1
        )

    def test_api_token_management_rejects_non_admin_cross_tenant_and_bad_expiry(self) -> None:
        payload = {
            "user_id": str(self.supervisor.pk),
            "name": "Bounded token",
            "scopes": ["reports.shop"],
            "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        }
        self.client.force_authenticate(self.driver)
        self.assertEqual(self.client.get(reverse("api-tokens")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("api-tokens"),
                payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000603",
            ).status_code,
            403,
        )

        self.client.force_authenticate(self.admin)
        cross_tenant = self.client.post(
            reverse("api-tokens"),
            payload | {"user_id": str(self.outsider.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000604",
        )
        self.assertEqual(cross_tenant.status_code, 404)
        expired = self.client.post(
            reverse("api-tokens"),
            payload | {"expires_at": (timezone.now() - timedelta(minutes=1)).isoformat()},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000605",
        )
        self.assertEqual(expired.status_code, 400)
        excessive_scope = self.client.post(
            reverse("api-tokens"),
            payload | {"user_id": str(self.driver.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000606",
        )
        self.assertEqual(excessive_scope.status_code, 403)
        wildcard_scope = self.client.post(
            reverse("api-tokens"),
            payload | {"user_id": str(self.admin.pk), "scopes": ["*"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000608",
        )
        self.assertEqual(wildcard_scope.status_code, 400)

    def test_admin_token_cannot_change_identities_or_credentials(self) -> None:
        admin_token = ApiToken.objects.create(
            organization=self.org,
            user=self.admin,
            name="Admin automation",
            prefix="adminident",
            token_hash="8" * 64,
            scopes=["admin.users"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        managed_token = ApiToken.objects.create(
            organization=self.org,
            user=self.supervisor,
            name="Managed token",
            prefix="managed",
            token_hash="7" * 64,
            scopes=["reports.shop"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.client.force_authenticate(self.admin, admin_token)

        requests = [
            self.client.post(
                reverse("users"),
                {
                    "username": "automation-created-user",
                    "name": "Automation Created",
                    "password": NEW_USER_PASSWORD,
                    "role_slugs": ["technician"],
                },
                format="json",
            ),
            self.client.post(
                reverse("api-tokens"),
                {
                    "user_id": str(self.supervisor.pk),
                    "name": "Escalated token",
                    "scopes": ["reports.shop"],
                    "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000609",
            ),
            self.client.post(
                reverse("api-token-revoke", args=[managed_token.pk]), {}, format="json"
            ),
            self.client.post(
                reverse("revoke-user-offline-access", args=[self.supervisor.pk]),
                {},
                format="json",
            ),
            self.client.post(reverse("disable-user", args=[self.supervisor.pk]), {}, format="json"),
        ]

        for response in requests:
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "interactive_session_required")
        self.assertFalse(User.objects.filter(username="automation-created-user").exists())
        managed_token.refresh_from_db()
        self.assertIsNone(managed_token.revoked_at)
        self.supervisor.refresh_from_db()
        self.assertTrue(self.supervisor.is_active)
        self.assertIsNone(self.supervisor.offline_access_revoked_at)

    def test_api_token_authentication_rejects_expired_and_cross_tenant_bindings(self) -> None:
        expired_raw = "expired-api-token"  # noqa: S105
        expired = ApiToken.objects.create(
            organization=self.org,
            user=self.supervisor,
            name="Expiring token",
            prefix="expired",
            token_hash=hashlib.sha256(expired_raw.encode()).hexdigest(),
            scopes=["dashboard.view"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        expired_client = APIClient()
        expired_client.credentials(HTTP_AUTHORIZATION=f"Bearer {expired_raw}")
        with patch(
            "core.authentication.timezone.now",
            return_value=expired.expires_at + timedelta(seconds=1),
        ):
            self.assertEqual(expired_client.get(reverse("search"), {"q": "truck"}).status_code, 403)

        cross_tenant_raw = "cross-tenant-api-token"  # noqa: S105
        ApiToken.objects.create(
            organization=self.other_org,
            user=self.supervisor,
            name="Invalid tenant binding",
            prefix="tenant",
            token_hash=hashlib.sha256(cross_tenant_raw.encode()).hexdigest(),
            scopes=["dashboard.view"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        cross_tenant_client = APIClient()
        cross_tenant_client.credentials(HTTP_AUTHORIZATION=f"Bearer {cross_tenant_raw}")
        self.assertEqual(
            cross_tenant_client.get(reverse("search"), {"q": "truck"}).status_code,
            403,
        )

    def test_narrow_bearer_scope_is_enforced_in_downstream_core_and_offline_checks(self) -> None:
        from assets.models import Asset, AssetType

        self.supervisor.roles.add(self.roles["driver"])
        asset_type = AssetType.objects.create(organization=self.org, name="Scope truck")
        asset = Asset.objects.create(
            organization=self.org,
            asset_type=asset_type,
            home_location=self.location,
            unit_number="SCOPE-100",
        )
        attachment = Attachment.objects.create(
            organization=self.org,
            uploader=self.supervisor,
            resource_type="asset",
            resource_id=str(asset.pk),
            file=SimpleUploadedFile("scope.txt", b"sensitive", content_type="text/plain"),
            original_name="scope.txt",
            content_type="text/plain",
            size=9,
            sha256=hashlib.sha256(b"sensitive").hexdigest(),
        )
        raw_token = "narrow-dashboard-token"  # noqa: S105
        token = ApiToken.objects.create(
            organization=self.org,
            user=self.supervisor,
            name="Dashboard only",
            prefix="narrow",
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            scopes=["dashboard.view"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        bearer = APIClient()
        bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")

        search = bearer.get(reverse("search"), {"q": "SCOPE"})
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["results"], [])
        self.assertEqual(bearer.get(reverse("operations-report")).status_code, 403)
        self.assertEqual(bearer.get(reverse("comments")).status_code, 403)
        self.assertEqual(
            bearer.get(reverse("attachment-download", args=[attachment.pk])).status_code, 403
        )
        offline = bearer.post(
            reverse("offline-sync"),
            {
                "operations": [
                    {
                        "operation_id": "00000000-0000-0000-0000-000000000607",
                        "type": "defect.create",
                        "payload": {"asset_id": str(asset.pk), "description": "scope test"},
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(offline.status_code, 403)
        self.assertEqual(offline.json()["error"]["code"], "invalid_offline_grant")
        token.refresh_from_db()
        self.assertIsNotNone(token.last_used_at)

    def test_cross_organization_user_mutation_is_denied(self) -> None:
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("disable-user", args=[self.outsider.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, 404)
        self.outsider.refresh_from_db()
        self.assertTrue(self.outsider.is_active)

    def test_disabling_user_permanently_revokes_their_api_tokens(self) -> None:
        token = ApiToken.objects.create(
            organization=self.org,
            user=self.technician,
            name="Field integration",
            prefix="field",
            token_hash="e" * 64,
            scopes=["maintenance.execute"],
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(reverse("disable-user", args=[self.technician.pk]), {})
        self.assertEqual(response.status_code, 200)
        self.technician.refresh_from_db()
        token.refresh_from_db()
        self.assertFalse(self.technician.is_active)
        self.assertIsNotNone(self.technician.offline_access_revoked_at)
        self.assertIsNotNone(token.revoked_at)
        event = AuditEvent.objects.get(action="user.disabled", resource_id=str(self.technician.pk))
        self.assertEqual(event.context["api_tokens_revoked"], 1)

    def test_audit_api_exposes_provenance_without_cross_tenant_events(self) -> None:
        visible = audit(
            organization=self.org,
            actor=self.supervisor,
            action="asset.status.changed",
            resource=self.location,
            context={"reason": "Safety repair"},
            source="offline-pwa",
            correlation_id="request-123",
        )
        hidden = audit(
            organization=self.other_org,
            actor=self.outsider,
            action="asset.status.changed",
            resource=self.other_location,
            source="integration",
            correlation_id="request-other-org",
        )
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("audit-events"))
        self.assertEqual(response.status_code, 200)
        events = response.json()["events"]
        self.assertEqual([event["id"] for event in events], [str(visible.pk)])
        self.assertEqual(events[0]["source"], "offline-pwa")
        self.assertEqual(events[0]["correlation_id"], "request-123")
        self.assertNotIn(str(hidden.pk), {event["id"] for event in events})

    def test_notifications_include_resource_and_keep_reads_user_and_tenant_scoped(self) -> None:
        visible = Notification.objects.create(
            organization=self.org,
            user=self.driver,
            title="New defect",
            body="Brake defect needs review",
            resource_type="Defect",
            resource_id="00000000-0000-0000-0000-000000000201",
        )
        another_user = Notification.objects.create(
            organization=self.org,
            user=self.other_driver,
            title="Other driver's defect",
            body="Not visible",
            resource_type="Defect",
            resource_id="00000000-0000-0000-0000-000000000202",
        )
        another_tenant = Notification.objects.create(
            organization=self.other_org,
            user=self.outsider,
            title="Other fleet defect",
            body="Not visible",
            resource_type="Defect",
            resource_id="00000000-0000-0000-0000-000000000203",
        )
        self.client.force_authenticate(self.driver)
        response = self.client.get(reverse("notifications"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["notifications"],
            [
                {
                    "id": str(visible.pk),
                    "title": "New defect",
                    "body": "Brake defect needs review",
                    "resource_type": "Defect",
                    "resource_id": "00000000-0000-0000-0000-000000000201",
                    "read_at": None,
                }
            ],
        )

        self.assertEqual(
            self.client.post(
                reverse("notifications"), {"id": str(visible.pk)}, format="json"
            ).status_code,
            200,
        )
        visible.refresh_from_db()
        self.assertIsNotNone(visible.read_at)
        self.assertEqual(
            self.client.post(
                reverse("notifications"), {"id": str(another_user.pk)}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("notifications"), {"id": str(another_tenant.pk)}, format="json"
            ).status_code,
            404,
        )

    def test_user_directory_is_authorized_filtered_and_tenant_scoped(self) -> None:
        self.client.force_authenticate(self.supervisor)
        response = self.client.get(reverse("users"), {"role": "technician"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "users": [
                    {
                        "id": str(self.technician.pk),
                        "name": self.technician.username,
                        "username": self.technician.username,
                        "roles": ["technician"],
                    }
                ]
            },
        )

        active_ids = {row["id"] for row in self.client.get(reverse("users")).json()["users"]}
        self.assertIn(str(self.technician.pk), active_ids)
        self.assertNotIn(str(self.inactive_technician.pk), active_ids)
        self.assertNotIn(str(self.other_technician.pk), active_ids)

        self.client.force_authenticate(self.driver)
        self.assertEqual(self.client.get(reverse("users"), {"role": "technician"}).status_code, 403)

    def test_admin_creates_scoped_user_with_validated_roles_and_location(self) -> None:
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("users"),
            {
                "username": " New.Tech ",
                "name": "Taylor Technician",
                "password": NEW_USER_PASSWORD,
                "default_location": str(self.location.pk),
                "role_slugs": ["technician"],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json()["user"],
            {
                "id": response.json()["user"]["id"],
                "name": "Taylor Technician",
                "username": "new.tech",
                "roles": ["technician"],
                "default_location": str(self.location.pk),
                "active": True,
            },
        )
        user = User.objects.get(pk=response.json()["user"]["id"])
        self.assertEqual(user.organization, self.org)
        self.assertTrue(user.check_password(NEW_USER_PASSWORD))
        self.assertEqual(user.role_slugs, {"technician"})
        event = AuditEvent.objects.get(action="user.created", resource_id=str(user.pk))
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.context["role_slugs"], ["technician"])

    def test_privileged_user_creation_provisions_mfa_once_and_supports_login(self) -> None:
        self.client.force_authenticate(self.admin)
        key = "00000000-0000-0000-0000-000000000701"
        payload = {
            "username": "new.integration.admin",
            "name": "New Integration Admin",
            "password": NEW_USER_PASSWORD,
            "default_location": str(self.location.pk),
            "role_slugs": ["system_admin"],
        }
        first = self.client.post(reverse("users"), payload, format="json", HTTP_IDEMPOTENCY_KEY=key)
        replay = self.client.post(
            reverse("users"), payload, format="json", HTTP_IDEMPOTENCY_KEY=key
        )

        self.assertEqual(first.status_code, 201)
        secret = first.json()["mfa_provisioning"]["secret"]
        self.assertTrue(first.json()["secret_recoverable"])
        self.assertIn("otpauth://totp/", first.json()["mfa_provisioning"]["otpauth_uri"])
        self.assertEqual(replay.status_code, 201)
        self.assertFalse(replay.json()["secret_recoverable"])
        self.assertNotIn("mfa_provisioning", replay.json())
        created = User.objects.get(username="new.integration.admin")
        self.assertEqual(created.mfa_secret, secret)
        self.assertNotIn(secret, json.dumps(IdempotencyRecord.objects.get(key=key).response_body))
        event = AuditEvent.objects.get(action="user.created", resource_id=str(created.pk))
        self.assertNotIn(secret, json.dumps(event.context))

        login_client = APIClient()
        instant = 1_800_000_000
        with patch("core.security.time.time", return_value=instant):
            response = login_client.post(
                reverse("login"),
                {
                    "username": created.username,
                    "password": NEW_USER_PASSWORD,
                    "otp": totp(secret, instant),
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200)

    def test_mfa_rotation_is_interactive_scoped_reasoned_and_shown_once(self) -> None:
        target = self._user("mfa-target", self.org, self.location, self.roles["system_admin"])
        target.mfa_secret = MFA_SECRET
        target.save(update_fields=["mfa_secret"])
        url = reverse("provision-user-mfa", args=[target.pk])
        self.client.force_authenticate(self.admin)
        missing_reason = self.client.post(
            url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000702",
        )
        self.assertEqual(missing_reason.status_code, 400)
        self.assertEqual(missing_reason.json()["error"]["code"], "reason_required")

        key = "00000000-0000-0000-0000-000000000703"
        rotated = self.client.post(
            url,
            {"reason": "Authenticator replaced"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        replay = self.client.post(
            url,
            {"reason": "Authenticator replaced"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(rotated.status_code, 200)
        secret = rotated.json()["mfa_provisioning"]["secret"]
        self.assertNotEqual(secret, MFA_SECRET)
        self.assertNotIn("mfa_provisioning", replay.json())
        self.assertFalse(replay.json()["secret_recoverable"])
        self.assertNotIn(secret, json.dumps(IdempotencyRecord.objects.get(key=key).response_body))
        event = AuditEvent.objects.get(action="user.mfa.rotated", resource_id=str(target.pk))
        self.assertEqual(event.context["reason"], "Authenticator replaced")
        self.assertNotIn(secret, json.dumps(event.context))

        scoped_token = ApiToken.objects.create(
            organization=self.org,
            user=self.admin,
            name="Admin automation",
            prefix="admin-mfa",
            token_hash="9" * 64,
            scopes=["admin.users"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.client.force_authenticate(self.admin, scoped_token)
        denied = self.client.post(
            url,
            {"reason": "Automation must not rotate human MFA"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000704",
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "interactive_session_required")

        self.client.force_authenticate(self.admin)
        self.assertEqual(
            self.client.post(
                reverse("provision-user-mfa", args=[self.admin.pk]),
                {"reason": "Self reset"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000705",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("provision-user-mfa", args=[self.outsider.pk]),
                {"reason": "Cross tenant"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000706",
            ).status_code,
            404,
        )

    def test_user_creation_rejects_weak_or_cross_tenant_input_and_non_admins(self) -> None:
        self.client.force_authenticate(self.admin)
        valid = {
            "username": "candidate",
            "name": "Casey Candidate",
            "password": NEW_USER_PASSWORD,
            "default_location": str(self.location.pk),
            "role_slugs": ["technician"],
        }
        response = self.client.post(
            reverse("users"),
            valid | {"default_location": str(self.other_location.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_default_location")
        response = self.client.post(
            reverse("users"), valid | {"role_slugs": ["owner"]}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_roles")
        response = self.client.post(
            reverse("users"), valid | {"password": "password"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_user")
        self.assertFalse(User.objects.filter(username="candidate").exists())

        self.client.force_authenticate(self.supervisor)
        self.assertEqual(self.client.post(reverse("users"), valid, format="json").status_code, 403)

    def test_role_and_location_admin_are_scoped_validated_and_audited(self) -> None:
        Role.objects.create(organization=self.org, slug="contractor", name="Contractor")
        self.client.force_authenticate(self.admin)

        role_response = self.client.get(reverse("roles"))
        self.assertEqual(role_response.status_code, 200)
        returned_roles = role_response.json()["roles"]
        self.assertEqual(
            {row["slug"] for row in returned_roles},
            {"driver", "technician", "supervisor", "system_admin"},
        )
        self.assertTrue(
            all(Role.objects.get(pk=row["id"]).organization == self.org for row in returned_roles)
        )

        location_response = self.client.get(reverse("locations"))
        self.assertEqual(location_response.status_code, 200)
        self.assertEqual(
            {row["id"] for row in location_response.json()["locations"]},
            {str(self.location.pk)},
        )
        response = self.client.post(
            reverse("locations"), {"name": "North Yard", "code": " yard "}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["location"]["code"], "YARD")
        location = Location.objects.get(pk=response.json()["location"]["id"])
        self.assertEqual(location.organization, self.org)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="location.created", resource_id=str(location.pk), actor=self.admin
            ).exists()
        )
        duplicate = self.client.post(
            reverse("locations"), {"name": "Duplicate", "code": "yard"}, format="json"
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "location_code_exists")

        self.client.force_authenticate(self.driver)
        self.assertEqual(self.client.get(reverse("roles")).status_code, 403)
        self.assertEqual(self.client.get(reverse("locations")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("locations"), {"name": "Forbidden", "code": "NO"}, format="json"
            ).status_code,
            403,
        )

    def test_attachment_validation_visibility_and_download_scope(self) -> None:
        self.client.force_authenticate(self.driver)
        valid_bytes = b"\x89PNG\r\n\x1a\nproof"
        response = self.client.post(
            reverse("attachments"),
            {
                "resource_type": "defect",
                "resource_id": "00000000-0000-0000-0000-000000000101",
                "file": SimpleUploadedFile("proof.png", valid_bytes, content_type="image/png"),
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000102",
        )
        self.assertEqual(response.status_code, 201)
        attachment = Attachment.objects.get(pk=response.json()["attachment"]["id"])
        self.assertEqual(attachment.uploader, self.driver)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="attachment.created", resource_id=str(attachment.pk)
            ).count(),
            1,
        )

        response = self.client.post(
            reverse("attachments"),
            {"file": SimpleUploadedFile("spoof.png", b"not a png", content_type="image/png")},
            format="multipart",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000103",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "attachment_type_mismatch")
        response = self.client.post(
            reverse("attachments"),
            {
                "file": SimpleUploadedFile(
                    "run.exe", b"MZpayload", content_type="application/x-msdownload"
                )
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000104",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_attachment")

        download_url = reverse("attachment-download", args=[attachment.pk])
        response = self.client.get(download_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), valid_bytes)  # type: ignore[attr-defined]
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

        self.client.force_authenticate(self.other_driver)
        self.assertEqual(self.client.get(download_url).status_code, 403)
        self.assertEqual(
            self.client.get(
                reverse("attachments"),
                {
                    "resource_type": "defect",
                    "resource_id": "00000000-0000-0000-0000-000000000101",
                },
            ).json()["attachments"],
            [],
        )
        self.client.force_authenticate(self.outsider)
        self.assertEqual(self.client.get(download_url).status_code, 404)


@override_settings(
    LOGIN_ATTEMPT_LIMIT=5,
    LOGIN_ATTEMPT_WINDOW_SECONDS=60,
    LOGIN_LOCKOUT_SECONDS=120,
)
class LoginThrottleConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_first_attempt_is_serialized_without_duplicate_rows(self) -> None:
        barrier = threading.Barrier(3)
        result_lock = threading.Lock()
        statuses: list[int] = []
        errors: list[BaseException] = []

        def attempt() -> None:
            close_old_connections()
            try:
                client = APIClient()
                barrier.wait(timeout=5)
                response = client.post(
                    reverse("login"),
                    {"username": "concurrent-unknown", "password": "incorrect-password"},
                    format="json",
                    REMOTE_ADDR="192.0.2.50",
                )
                with result_lock:
                    statuses.append(response.status_code)
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertEqual(statuses, [401, 401])
        self.assertEqual(
            LoginAttemptThrottle.objects.filter(
                dimension=LoginAttemptThrottle.Dimension.CLIENT
            ).count(),
            1,
        )
        self.assertFalse(
            LoginAttemptThrottle.objects.filter(
                dimension=LoginAttemptThrottle.Dimension.ACCOUNT
            ).exists()
        )


class CoreServiceTests(TestCase):
    org: Organization
    location: Location
    role: Role
    user: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.org = Organization.objects.create(name="Service Fleet", slug="service")
        cls.location = Location.objects.create(organization=cls.org, name="Shop", code="SHOP")
        cls.role = Role.objects.create(organization=cls.org, slug="driver", name="Driver")
        cls.user = User.objects.create_user(
            username="service-driver",
            password=TEST_PASSWORD,
            organization=cls.org,
            default_location=cls.location,
        )
        cls.user.roles.add(cls.role)

    def _request(
        self,
        data: dict[str, object],
        key: str | None = "00000000-0000-0000-0000-000000000001",
    ) -> SimpleNamespace:
        headers = {"Idempotency-Key": key} if key is not None else {}
        return SimpleNamespace(
            headers=headers,
            data=data,
            method="POST",
            path="/api/v1/test-transaction/",
            user=self.user,
        )

    def test_idempotency_replays_response_once_and_rejects_key_reuse(self) -> None:
        calls: list[int] = []

        def handler() -> Response:
            calls.append(1)
            return Response({"transaction_id": "TX-1"}, status=201)

        first = idempotent(self._request({"quantity": 2}), handler)
        replay = idempotent(self._request({"quantity": 2}), handler)
        self.assertEqual((first.status_code, first.data), (201, {"transaction_id": "TX-1"}))
        self.assertEqual((replay.status_code, replay.data), (201, {"transaction_id": "TX-1"}))
        self.assertEqual(len(calls), 1)

        with self.assertRaisesMessage(DomainError, "already used for different input") as conflict:
            idempotent(self._request({"quantity": 3}), handler)
        self.assertEqual(conflict.exception.status, 409)
        with self.assertRaisesMessage(DomainError, "Idempotency-Key is required"):
            idempotent(self._request({"quantity": 2}, key=None), handler)

    def test_idempotency_can_store_a_redacted_replay_without_changing_first_response(self) -> None:
        raw_secret = "one-time-secret"  # noqa: S105

        def handler() -> Response:
            return Response({"resource": {"id": "R-1"}, "token": raw_secret}, status=201)

        def redact(response: Response) -> object:
            return {
                "resource": response.data["resource"],
                "secret_recoverable": False,
                "message": "The credential cannot be recovered; rotate it.",
            }

        request = self._request({"name": "credential"})
        first = idempotent(request, handler, stored_response_transform=redact)
        replay = idempotent(request, handler, stored_response_transform=redact)
        self.assertEqual(first.data["token"], raw_secret)
        self.assertNotIn("token", replay.data)
        self.assertFalse(replay.data["secret_recoverable"])
        record = IdempotencyRecord.objects.get(
            organization=self.org,
            user=self.user,
            route=request.path,
            key=request.headers["Idempotency-Key"],
        )
        self.assertNotIn(raw_secret, json.dumps(record.response_body))

    def test_audit_events_are_append_only_and_organization_scoped(self) -> None:
        created = audit(
            organization=self.org,
            actor=self.user,
            action="location.created",
            resource=self.location,
            previous_state="",
            new_state="Active",
            context={"source": "test"},
            correlation_id="corr-1",
        )
        self.assertEqual(created.resource_type, "Location")
        self.assertEqual(created.resource_id, str(self.location.pk))

        created.action = "tampered"
        with self.assertRaisesMessage(ValidationError, "append-only"):
            created.save()
        with self.assertRaisesMessage(ValidationError, "cannot be deleted"):
            created.delete()
        created.refresh_from_db()
        self.assertEqual(created.action, "location.created")

        correction = audit(
            organization=self.org,
            actor=self.user,
            action="location.corrected",
            resource=self.location,
            previous_state="Active",
            new_state="Active",
            context={"reason": "name corrected"},
        )
        self.assertNotEqual(created.pk, correction.pk)
        self.assertEqual(AuditEvent.objects.filter(organization=self.org).count(), 2)


class WebhookApiTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.org = Organization.objects.create(name="Webhook Fleet", slug="webhook-fleet")
        cls.other_org = Organization.objects.create(name="Other Fleet", slug="other-webhook-fleet")
        cls.admin_role = Role.objects.create(
            organization=cls.org, slug="system_admin", name="System administrator"
        )
        cls.driver_role = Role.objects.create(organization=cls.org, slug="driver", name="Driver")
        cls.other_admin_role = Role.objects.create(
            organization=cls.other_org,
            slug="system_admin",
            name="System administrator",
        )
        cls.admin = User.objects.create_user(
            username="webhook-admin", password=TEST_PASSWORD, organization=cls.org
        )
        cls.admin.roles.add(cls.admin_role)
        cls.driver = User.objects.create_user(
            username="webhook-driver", password=TEST_PASSWORD, organization=cls.org
        )
        cls.driver.roles.add(cls.driver_role)
        cls.other_admin = User.objects.create_user(
            username="other-webhook-admin",
            password=TEST_PASSWORD,
            organization=cls.other_org,
        )
        cls.other_admin.roles.add(cls.other_admin_role)
        cls.subscription = WebhookSubscription.objects.create(
            organization=cls.org,
            name="Maintenance receiver",
            url="https://example.com/hooks/fleetline",
            signing_secret=TEST_WEBHOOK_SECRET,
            event_types=["defect.created"],
        )
        cls.event = OutboxEvent.objects.create(
            organization=cls.org,
            event_type="defect.created",
            resource_type="Defect",
            resource_id="defect-1",
            payload={"summary": "Air leak"},
            available_at=timezone.now(),
        )
        cls.delivery = WebhookDelivery.objects.create(
            organization=cls.org,
            subscription=cls.subscription,
            outbox_event=cls.event,
            attempts=8,
            status="dead",
            response_status=503,
            next_attempt_at=timezone.now(),
            last_error="HTTP 503",
        )
        other_subscription = WebhookSubscription.objects.create(
            organization=cls.other_org,
            name="Other receiver",
            url="https://example.net/hooks/fleetline",
            signing_secret=OTHER_WEBHOOK_SECRET,
        )
        other_event = OutboxEvent.objects.create(
            organization=cls.other_org,
            event_type="asset.created",
            resource_type="Asset",
            resource_id="asset-other",
            available_at=timezone.now(),
        )
        cls.other_delivery = WebhookDelivery.objects.create(
            organization=cls.other_org,
            subscription=other_subscription,
            outbox_event=other_event,
            status="dead",
            next_attempt_at=timezone.now(),
            last_error="other tenant error",
        )

    def setUp(self) -> None:
        self.client = APIClient()

    def test_delivery_list_is_tenant_scoped_filtered_and_authorized(self) -> None:
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("webhook-deliveries"), {"status": "dead"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dead_letter_count"], 1)
        self.assertEqual(
            [row["id"] for row in response.json()["deliveries"]],
            [str(self.delivery.pk)],
        )
        row = response.json()["deliveries"][0]
        self.assertEqual(row["last_error"], "HTTP 503")
        self.assertEqual(row["event"]["resource"], {"type": "Defect", "id": "defect-1"})
        self.assertNotIn("signing_secret", row["subscription"])
        self.assertNotIn(str(self.other_delivery.pk), response.content.decode())

        invalid = self.client.get(reverse("webhook-deliveries"), {"status": "unknown"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "invalid_delivery_status")

        self.client.force_authenticate(self.driver)
        self.assertEqual(self.client.get(reverse("webhook-deliveries")).status_code, 403)

    def test_retry_is_idempotent_tenant_scoped_authorized_and_audited(self) -> None:
        retry_url = reverse("webhook-delivery-retry", args=[self.delivery.pk])
        key = "00000000-0000-0000-0000-000000000501"
        self.client.force_authenticate(self.driver)
        self.assertEqual(
            self.client.post(retry_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=key).status_code,
            403,
        )

        self.client.force_authenticate(self.other_admin)
        self.assertEqual(
            self.client.post(retry_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=key).status_code,
            404,
        )

        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(retry_url, {}, format="json").status_code, 400)
        response = self.client.post(retry_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(response.status_code, 200)
        self.delivery.refresh_from_db()
        self.assertEqual((self.delivery.status, self.delivery.attempts), ("retry", 0))
        self.assertEqual(self.delivery.last_error, "HTTP 503")
        event = AuditEvent.objects.get(
            action="webhook.delivery_retried", resource_id=str(self.delivery.pk)
        )
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.previous_state, "dead")
        self.assertEqual(event.context["previous_attempts"], 8)
        self.assertEqual(event.context["previous_last_error"], "HTTP 503")

        replay = self.client.post(retry_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="webhook.delivery_retried", resource_id=str(self.delivery.pk)
            ).count(),
            1,
        )

    @override_settings(WEBHOOK_MAX_SUBSCRIPTIONS=2, WEBHOOK_MAX_EVENT_TYPES=2)
    def test_webhook_create_is_bounded_idempotent_and_secret_is_shown_once(self) -> None:
        self.client.force_authenticate(self.admin)
        key = "00000000-0000-0000-0000-000000000502"
        payload = {
            "name": "Asset receiver",
            "url": "https://receiver.example.test/fleetline",
            "event_types": ["asset.updated", "asset.updated"],
        }
        with patch("core.views.validate_outbound_url", return_value=payload["url"]):
            first = self.client.post(
                reverse("webhooks"), payload, format="json", HTTP_IDEMPOTENCY_KEY=key
            )
            replay = self.client.post(
                reverse("webhooks"), payload, format="json", HTTP_IDEMPOTENCY_KEY=key
            )

        self.assertEqual(first.status_code, 201)
        secret = first.json()["webhook"]["signing_secret"]
        self.assertTrue(first.json()["secret_recoverable"])
        self.assertEqual(replay.status_code, 201)
        self.assertNotIn("signing_secret", replay.json()["webhook"])
        self.assertFalse(replay.json()["secret_recoverable"])
        self.assertEqual(replay.json()["webhook"]["event_types"], ["asset.updated"])
        self.assertEqual(WebhookSubscription.objects.filter(organization=self.org).count(), 2)
        record = IdempotencyRecord.objects.get(key=key)
        self.assertNotIn(secret, json.dumps(record.response_body))
        self.assertEqual(
            AuditEvent.objects.filter(action="webhook.created", organization=self.org).count(),
            1,
        )

    @override_settings(WEBHOOK_MAX_SUBSCRIPTIONS=1, WEBHOOK_MAX_EVENT_TYPES=1)
    def test_webhook_subscription_and_event_type_limits_fail_closed(self) -> None:
        self.client.force_authenticate(self.admin)
        too_many_types = self.client.post(
            reverse("webhooks"),
            {
                "name": "Too many types",
                "url": "https://receiver.example.test/fleetline",
                "event_types": ["asset.created", "asset.updated"],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000503",
        )
        self.assertEqual(too_many_types.status_code, 400)
        self.assertEqual(too_many_types.json()["error"]["code"], "too_many_webhook_event_types")

        with patch("core.views.validate_outbound_url", return_value="validated"):
            at_limit = self.client.post(
                reverse("webhooks"),
                {
                    "name": "At limit",
                    "url": "https://receiver.example.test/fleetline",
                    "event_types": ["asset.updated"],
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000504",
            )
        self.assertEqual(at_limit.status_code, 409)
        self.assertEqual(at_limit.json()["error"]["code"], "webhook_subscription_limit")

    def test_webhook_secret_rotation_and_deactivation_are_tenant_scoped_and_idempotent(
        self,
    ) -> None:
        rotate_url = reverse("webhook-rotate-secret", args=[self.subscription.pk])
        rotate_key = "00000000-0000-0000-0000-000000000505"
        self.client.force_authenticate(self.other_admin)
        self.assertEqual(
            self.client.post(
                rotate_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=rotate_key
            ).status_code,
            404,
        )

        self.client.force_authenticate(self.admin)
        rotated = self.client.post(rotate_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=rotate_key)
        self.assertEqual(rotated.status_code, 200)
        secret = rotated.json()["webhook"]["signing_secret"]
        self.assertNotEqual(secret, TEST_WEBHOOK_SECRET)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.signing_secret, secret)
        replay = self.client.post(rotate_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=rotate_key)
        self.assertNotIn("signing_secret", replay.json()["webhook"])
        self.assertFalse(replay.json()["secret_recoverable"])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.signing_secret, secret)
        self.assertNotIn(
            secret,
            json.dumps(IdempotencyRecord.objects.get(key=rotate_key).response_body),
        )

        pending = WebhookDelivery.objects.create(
            organization=self.org,
            subscription=self.subscription,
            outbox_event=OutboxEvent.objects.create(
                organization=self.org,
                event_type="asset.updated",
                resource_type="Asset",
                resource_id="asset-pending",
                available_at=timezone.now(),
            ),
            next_attempt_at=timezone.now(),
        )
        status_url = reverse("webhook-status", args=[self.subscription.pk])
        status_key = "00000000-0000-0000-0000-000000000506"
        disabled = self.client.post(
            status_url,
            {"status": "inactive"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=status_key,
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["webhook"]["active"])
        pending.refresh_from_db()
        self.assertEqual(pending.status, "dead")
        self.assertEqual(pending.last_error, "Subscription deactivated")
        self.client.post(
            status_url,
            {"status": "inactive"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=status_key,
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action="webhook.status_changed", resource_id=str(self.subscription.pk)
            ).count(),
            1,
        )


class WebhookWorkerTests(TransactionTestCase):
    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Worker Fleet", slug="worker-fleet")
        self.subscription = WebhookSubscription.objects.create(
            organization=self.org,
            name="Receiver",
            url="https://example.com/fleetline",
            signing_secret=TEST_WEBHOOK_SECRET,
        )
        self.event = OutboxEvent.objects.create(
            organization=self.org,
            event_type="work_order.completed",
            resource_type="WorkOrder",
            resource_id="WO-44",
            payload={"summary": "Brake repair completed"},
            available_at=timezone.now(),
        )
        self.delivery = WebhookDelivery.objects.create(
            organization=self.org,
            subscription=self.subscription,
            outbox_event=self.event,
            next_attempt_at=timezone.now(),
        )
        self.target = ResolvedOutboundURL(
            url=self.subscription.url,
            scheme="https",
            hostname="example.com",
            port=443,
            request_target="/fleetline",
            addresses=("93.184.216.34",),
        )

    @override_settings(WEBHOOK_DELIVERY_TIMEOUT_SECONDS=3.5)
    def test_delivery_payload_signature_and_success_state(self) -> None:
        captured: dict[str, object] = {}

        class Response:
            status = 204

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def open_request(
            request: object, *, target: ResolvedOutboundURL, timeout: float
        ) -> Response:
            captured["request"] = request
            captured["target"] = target
            captured["timeout"] = timeout
            return Response()

        with (
            patch(
                "core.management.commands.runworker.resolve_outbound_url",
                return_value=self.target,
            ),
            patch("core.management.commands.runworker._open_webhook", side_effect=open_request),
        ):
            self.assertTrue(Command.deliver_one())

        request = captured["request"]
        body = request.data  # type: ignore[attr-defined]
        payload = json.loads(body)
        self.assertEqual(
            payload,
            {
                "id": str(self.event.pk),
                "schema_version": "1.0",
                "type": "work_order.completed",
                "organization_id": str(self.org.pk),
                "occurred_at": self.event.created_at.isoformat(),
                "resource": {"type": "WorkOrder", "id": "WO-44"},
                "data": {"summary": "Brake repair completed"},
            },
        )
        expected = hmac.new(TEST_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers = {key.lower(): value for key, value in request.header_items()}  # type: ignore[attr-defined]
        self.assertEqual(headers["x-fleetline-signature"], f"sha256={expected}")
        self.assertEqual(captured["target"], self.target)
        self.assertEqual(captured["timeout"], 3.5)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "delivered")
        self.assertEqual(self.delivery.response_status, 204)
        self.assertIsNotNone(self.delivery.delivered_at)

    def test_delivery_redacts_financial_values_but_keeps_operational_context(self) -> None:
        """Generic webhook subscriptions never receive monetary event fields."""

        self.event.payload = {
            "part_number": "FILTER-44",
            "quantity": "2.000",
            "bin_code": "MAIN/A-01",
            "unit_cost": "12.5000",
            "total_cost": "25.0000",
            "nested": {"hourly_rate": "75.00", "note": "Installed"},
        }
        self.event.save(update_fields=["payload"])
        captured: dict[str, object] = {}

        class Response:
            status = 204

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def open_request(
            request: object, *, target: ResolvedOutboundURL, timeout: float
        ) -> Response:
            captured["request"] = request
            return Response()

        with (
            patch(
                "core.management.commands.runworker.resolve_outbound_url",
                return_value=self.target,
            ),
            patch("core.management.commands.runworker._open_webhook", side_effect=open_request),
        ):
            self.assertTrue(Command.deliver_one())

        request = captured["request"]
        payload = json.loads(request.data)  # type: ignore[attr-defined]
        self.assertEqual(
            payload["data"],
            {
                "part_number": "FILTER-44",
                "quantity": "2.000",
                "bin_code": "MAIN/A-01",
                "nested": {"note": "Installed"},
            },
        )

    def test_transport_connects_to_vetted_ip_without_another_dns_lookup(self) -> None:
        captured: dict[str, object] = {}

        class Response:
            status = 204

            def close(self) -> None:
                captured["response_closed"] = True

        class Connection:
            def __init__(self, hostname: str, address: str, port: int, *, timeout: float) -> None:
                captured.update(
                    hostname=hostname,
                    address=address,
                    port=port,
                    timeout=timeout,
                )

            def request(
                self, method: str, path: str, *, body: object, headers: dict[str, str]
            ) -> None:
                captured.update(method=method, path=path, body=body, headers=headers)

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                captured["connection_closed"] = True

        request = urllib.request.Request(  # noqa: S310 - the pinned transport is under test
            self.subscription.url,
            data=b"{}",
            method="POST",
            headers={"X-Fleetline-Signature": "sha256=test"},
        )
        with (
            patch("core.management.commands.runworker.resolve_outbound_url") as resolver,
            patch("core.management.commands.runworker._PinnedHTTPSConnection", Connection),
        ):
            with _open_webhook(request, target=self.target, timeout=10) as response:
                self.assertEqual(response.status, 204)

        resolver.assert_not_called()
        self.assertEqual(captured["hostname"], "example.com")
        self.assertEqual(captured["address"], "93.184.216.34")
        self.assertEqual(captured["path"], "/fleetline")
        self.assertTrue(captured["response_closed"])
        self.assertTrue(captured["connection_closed"])

    @override_settings(WEBHOOK_ALLOW_PRIVATE_NETWORKS=False)
    def test_dns_rebinding_to_private_address_is_rejected_at_delivery(self) -> None:
        public_resolution = [(0, 0, 0, "", ("93.184.216.34", 443))]
        private_resolution = [(0, 0, 0, "", ("127.0.0.1", 443))]
        with (
            patch(
                "core.security.socket.getaddrinfo",
                side_effect=[public_resolution, private_resolution],
            ) as resolver,
            patch("core.management.commands.runworker._PinnedHTTPSConnection") as connection,
        ):
            validate_outbound_url(self.subscription.url)
            self.assertTrue(Command.deliver_one())

        self.assertEqual(resolver.call_count, 2)
        connection.assert_not_called()
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "dead")
        self.assertIn("non-public address", self.delivery.last_error)

    @override_settings(WEBHOOK_ALLOW_HTTP=True, WEBHOOK_ALLOW_PRIVATE_NETWORKS=True)
    def test_redirect_is_rejected_without_fetching_redirect_target(self) -> None:
        paths: list[str] = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                paths.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/hook":
                    self.send_response(302)
                    self.send_header(
                        "Location", f"http://127.0.0.1:{self.server.server_port}/redirected"
                    )
                else:
                    self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.subscription.url = f"http://127.0.0.1:{server.server_port}/hook"
            self.subscription.save(update_fields=["url", "updated_at"])
            self.assertTrue(Command.deliver_one())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.delivery.refresh_from_db()
        self.assertEqual(paths, ["/hook"])
        self.assertEqual(self.delivery.status, "retry")
        self.assertEqual(self.delivery.response_status, 302)
        self.assertIn("Webhook redirects are not allowed", self.delivery.last_error)

    def test_retry_uses_bounded_jitter_and_eighth_failure_goes_dead(self) -> None:
        WebhookDelivery.objects.filter(pk=self.delivery.pk).update(attempts=6)
        before = timezone.now()
        with (
            patch(
                "core.management.commands.runworker.resolve_outbound_url",
                return_value=self.target,
            ),
            patch(
                "core.management.commands.runworker._open_webhook",
                side_effect=urllib.error.URLError("receiver unavailable"),
            ),
            patch("core.management.commands.runworker.secrets.randbelow", return_value=13),
        ):
            self.assertTrue(Command.deliver_one())
        after = timezone.now()
        self.delivery.refresh_from_db()
        self.assertEqual((self.delivery.status, self.delivery.attempts), ("retry", 7))
        self.assertGreaterEqual(self.delivery.next_attempt_at, before + timedelta(seconds=77))
        self.assertLessEqual(self.delivery.next_attempt_at, after + timedelta(seconds=77))
        self.assertIn("receiver unavailable", self.delivery.last_error)

        WebhookDelivery.objects.filter(pk=self.delivery.pk).update(next_attempt_at=timezone.now())
        with (
            patch(
                "core.management.commands.runworker.resolve_outbound_url",
                return_value=self.target,
            ),
            patch(
                "core.management.commands.runworker._open_webhook",
                side_effect=urllib.error.URLError("still unavailable"),
            ),
        ):
            self.assertTrue(Command.deliver_one())
        self.delivery.refresh_from_db()
        self.assertEqual((self.delivery.status, self.delivery.attempts), ("dead", 8))
        self.assertIn("still unavailable", self.delivery.last_error)

    def test_malformed_response_is_retried_and_next_delivery_continues(self) -> None:
        first_due = timezone.now() - timedelta(seconds=2)
        WebhookDelivery.objects.filter(pk=self.delivery.pk).update(next_attempt_at=first_due)
        next_event = OutboxEvent.objects.create(
            organization=self.org,
            event_type="asset.updated",
            resource_type="Asset",
            resource_id="asset-45",
            payload={"summary": "Next delivery"},
            available_at=timezone.now(),
        )
        next_delivery = WebhookDelivery.objects.create(
            organization=self.org,
            subscription=self.subscription,
            outbox_event=next_event,
            next_attempt_at=first_due + timedelta(seconds=1),
        )

        class Response:
            status = 204

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with (
            patch(
                "core.management.commands.runworker.resolve_outbound_url",
                return_value=self.target,
            ),
            patch(
                "core.management.commands.runworker._open_webhook",
                side_effect=[http.client.BadStatusLine("malformed status"), Response()],
            ),
        ):
            self.assertTrue(Command.deliver_one())
            self.assertTrue(Command.deliver_one())

        self.delivery.refresh_from_db()
        next_delivery.refresh_from_db()
        self.assertEqual((self.delivery.status, self.delivery.attempts), ("retry", 1))
        self.assertIn("BadStatusLine: malformed status", self.delivery.last_error)
        self.assertEqual(next_delivery.status, "delivered")

    def test_concurrent_workers_claim_a_delivery_once(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        counter_lock = threading.Lock()
        calls = 0
        results: list[bool] = []
        errors: list[BaseException] = []

        class Response:
            status = 204

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def open_request(
            request: object, *, target: ResolvedOutboundURL, timeout: float
        ) -> Response:
            nonlocal calls
            with counter_lock:
                calls += 1
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release receiver")
            return Response()

        def run_worker() -> None:
            close_old_connections()
            try:
                results.append(Command.deliver_one())
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                connections.close_all()

        with (
            patch(
                "core.management.commands.runworker.resolve_outbound_url",
                return_value=self.target,
            ),
            patch("core.management.commands.runworker._open_webhook", side_effect=open_request),
        ):
            first = threading.Thread(target=run_worker)
            second = threading.Thread(target=run_worker)
            first.start()
            self.assertTrue(entered.wait(timeout=5))
            second.start()
            second.join(timeout=5)
            try:
                self.assertFalse(second.is_alive())
            finally:
                release.set()
                first.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(calls, 1)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "delivered")
