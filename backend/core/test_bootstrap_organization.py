from __future__ import annotations

import io
import json
import stat
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import AuditEvent, Location, Organization, Role, User
from core.permissions import ROLE_PERMISSIONS
from core.security import totp


class BootstrapOrganizationCommandTests(TestCase):
    password = "Production-Fleet-Access-2026!"  # noqa: S105 - deterministic test credential

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.password_file = self.root / "password"
        self.password_file.write_text(self.password + "\n", encoding="utf-8")
        self.password_file.chmod(0o600)
        self.provisioning_file = self.root / "mfa.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def options(self) -> dict[str, str]:
        return {
            "organization_name": "Production Fleet",
            "organization_slug": "production-fleet",
            "location_name": "Main Shop",
            "location_code": "MAIN",
            "admin_username": "admin@production.example",
            "admin_first_name": "Pat",
            "admin_last_name": "Administrator",
            "password_file": str(self.password_file),
            "provisioning_output": str(self.provisioning_file),
        }

    def test_creates_complete_tenant_and_mfa_admin_without_exposing_secret(self) -> None:
        stdout = io.StringIO()
        call_command("bootstrap_organization", stdout=stdout, **self.options())

        organization = Organization.objects.get(slug="production-fleet")
        location = Location.objects.get(organization=organization, code="MAIN")
        administrator = User.objects.get(username="admin@production.example")
        self.assertEqual(administrator.organization, organization)
        self.assertEqual(administrator.default_location, location)
        self.assertEqual(administrator.role_slugs, {"system_admin"})
        self.assertTrue(administrator.check_password(self.password))
        self.assertEqual(
            set(Role.objects.filter(organization=organization).values_list("slug", flat=True)),
            set(ROLE_PERMISSIONS),
        )

        provisioning = json.loads(self.provisioning_file.read_text(encoding="utf-8"))
        self.assertEqual(provisioning["mfa_secret"], administrator.mfa_secret)
        self.assertIn("otpauth://totp/", provisioning["otpauth_uri"])
        self.assertEqual(stat.S_IMODE(self.provisioning_file.stat().st_mode), 0o600)
        self.assertNotIn(administrator.mfa_secret, stdout.getvalue())
        self.assertEqual(
            AuditEvent.objects.get(action="organization.bootstrapped").context["administrator_id"],
            str(administrator.pk),
        )

        client = APIClient()
        response = client.post(
            "/api/v1/auth/login/",
            {
                "username": administrator.username,
                "password": self.password,
                "otp": totp(administrator.mfa_secret),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_refuses_existing_tenant_and_never_overwrites_provisioning_file(self) -> None:
        call_command("bootstrap_organization", **self.options())
        original = self.provisioning_file.read_bytes()
        with self.assertRaisesRegex(CommandError, "provisioning output already exists"):
            call_command("bootstrap_organization", **self.options())
        self.assertEqual(self.provisioning_file.read_bytes(), original)

    def test_refuses_password_file_with_group_access(self) -> None:
        self.password_file.chmod(0o640)
        with self.assertRaisesRegex(CommandError, "Password file must not be"):
            call_command("bootstrap_organization", **self.options())
        self.assertFalse(self.provisioning_file.exists())
        self.assertFalse(Organization.objects.exists())
