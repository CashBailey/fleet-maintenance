from __future__ import annotations

import base64
import json
import os
import secrets
from argparse import ArgumentParser
from pathlib import Path
from urllib.parse import quote

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from core.models import Location, Organization, Role, User
from core.permissions import ROLE_PERMISSIONS
from core.services import audit

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


class Command(BaseCommand):
    help = "Create a production organization and its first MFA-protected system administrator."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--organization-name", required=True)
        parser.add_argument("--organization-slug", required=True)
        parser.add_argument("--location-name", default="Main Shop")
        parser.add_argument("--location-code", default="MAIN")
        parser.add_argument("--admin-username", required=True)
        parser.add_argument("--admin-first-name", required=True)
        parser.add_argument("--admin-last-name", required=True)
        parser.add_argument(
            "--password-file",
            required=True,
            help="Path to a mode-0600 file whose first line is the administrator password.",
        )
        parser.add_argument(
            "--provisioning-output",
            required=True,
            help="New file to receive the one-time MFA provisioning data (created mode 0600).",
        )

    def handle(self, *args: object, **options: object) -> None:
        organization_name = str(options["organization_name"]).strip()
        organization_slug = str(options["organization_slug"]).strip().lower()
        location_name = str(options["location_name"]).strip()
        location_code = str(options["location_code"]).strip().upper()
        username = str(options["admin_username"]).strip().lower()
        first_name = str(options["admin_first_name"]).strip()
        last_name = str(options["admin_last_name"]).strip()
        if not all(
            (
                organization_name,
                organization_slug,
                location_name,
                location_code,
                username,
                first_name,
                last_name,
            )
        ):
            raise CommandError("Organization, location, and administrator values cannot be blank")

        password_path = Path(str(options["password_file"])).expanduser()
        output_path = Path(str(options["provisioning_output"])).expanduser()
        if not password_path.is_file():
            raise CommandError("Password file does not exist or is not a regular file")
        if password_path.stat().st_mode & 0o077:
            raise CommandError("Password file must not be readable or writable by group or others")
        try:
            password = password_path.read_text(encoding="utf-8").splitlines()[0]
        except (IndexError, OSError, UnicodeError) as exc:
            raise CommandError("Password file could not be read") from exc
        if output_path.exists():
            raise CommandError("MFA provisioning output already exists; refusing to overwrite it")
        if not output_path.parent.is_dir():
            raise CommandError("MFA provisioning output directory does not exist")
        if Organization.objects.filter(slug=organization_slug).exists():
            raise CommandError("An organization with this slug already exists")
        if User.objects.filter(username=username).exists():
            raise CommandError("A user with this username already exists")

        secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
        pending_user = User(
            username=username,
            email=username if "@" in username else "",
            first_name=first_name,
            last_name=last_name,
        )
        try:
            validate_password(password, user=pending_user)
        except ValidationError as exc:
            raise CommandError(
                "Administrator password does not satisfy the password policy"
            ) from exc

        issuer = organization_name
        provisioning = {
            "organization_slug": organization_slug,
            "username": username,
            "mfa_secret": secret,
            "otpauth_uri": (
                f"otpauth://totp/{quote(issuer, safe='')}:{quote(username, safe='')}"
                f"?secret={secret}&issuer={quote(issuer, safe='')}"
                "&algorithm=SHA1&digits=6&period=30"
            ),
        }
        try:
            descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(provisioning, handle, indent=2, sort_keys=True)
                handle.write("\n")
            output_path.chmod(0o600)
        except OSError as exc:
            raise CommandError("Could not create the MFA provisioning output") from exc

        try:
            with transaction.atomic():
                organization = Organization(name=organization_name, slug=organization_slug)
                organization.full_clean()
                organization.save()
                location = Location(
                    organization=organization,
                    name=location_name,
                    code=location_code,
                )
                location.full_clean()
                location.save()
                roles = {
                    slug: Role.objects.create(
                        organization=organization,
                        slug=slug,
                        name=ROLE_NAMES.get(slug, slug.replace("_", " ").title()),
                    )
                    for slug in ROLE_PERMISSIONS
                }
                administrator = User(
                    username=username,
                    email=username if "@" in username else "",
                    first_name=first_name,
                    last_name=last_name,
                    organization=organization,
                    default_location=location,
                    mfa_secret=secret,
                    is_active=True,
                    is_staff=False,
                    is_superuser=False,
                )
                administrator.set_password(password)
                administrator.full_clean()
                administrator.save()
                administrator.roles.set([roles["system_admin"]])
                audit(
                    organization=organization,
                    actor=administrator,
                    action="organization.bootstrapped",
                    resource=organization,
                    new_state="active",
                    context={
                        "administrator_id": str(administrator.pk),
                        "location_id": str(location.pk),
                        "role_slugs": sorted(roles),
                    },
                )
        except (IntegrityError, ValidationError, OSError) as exc:
            output_path.unlink(missing_ok=True)
            raise CommandError("Organization bootstrap failed; no records were created") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Created organization {organization_slug} and administrator {username}. "
                f"Transfer {output_path} securely, enroll MFA, then delete the file."
            )
        )
