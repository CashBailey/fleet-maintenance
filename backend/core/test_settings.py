from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

import yaml
from django.test import SimpleTestCase

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
SETTINGS_PROBE = (
    "from fleetops import settings; print(settings.SECRET_KEY == 'development-only-change-me')"
)
ERROR_FRAGMENT = "DJANGO_SECRET_KEY must be explicitly set to a random value"
STRONG_TEST_VALUE = "test-only-strong-explicit-secret-with-more-than-fifty-characters-0123456789"


class ProductionSecretSettingsTests(SimpleTestCase):
    def probe(
        self, *, debug: bool, secret: str | None, require_strong: bool = False
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["DJANGO_DEBUG"] = "1" if debug else "0"
        environment["FLEETLINE_REQUIRE_STRONG_SECRET"] = "1" if require_strong else "0"
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        if secret is None:
            environment.pop("DJANGO_SECRET_KEY", None)
        else:
            environment["DJANGO_SECRET_KEY"] = secret
        return subprocess.run(  # noqa: S603 - executable and probe are fixed test inputs
            [sys.executable, "-c", SETTINGS_PROBE],
            cwd=BACKEND_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_non_debug_settings_reject_missing_secret(self) -> None:
        result = self.probe(debug=False, secret=None)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(ERROR_FRAGMENT, result.stderr)

    def test_non_debug_settings_reject_weak_or_placeholder_secrets(self) -> None:
        weak_values = (
            "short",
            "a" * 60,
            "django-insecure-this-value-is-long-but-still-not-for-production-123456",
            "development-only-this-value-is-long-but-still-not-for-production-123",
            "replace-with-this-value-is-long-but-still-not-for-production-1234567",
        )
        for value in weak_values:
            with self.subTest(value=value):
                result = self.probe(debug=False, secret=value)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(ERROR_FRAGMENT, result.stderr)

    def test_non_debug_settings_accept_strong_explicit_secret(self) -> None:
        result = self.probe(
            debug=False,
            secret=STRONG_TEST_VALUE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_debug_settings_retain_local_fallback(self) -> None:
        result = self.probe(debug=True, secret=None)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")

    def test_production_entrypoint_rejects_debug_fallback(self) -> None:
        result = self.probe(debug=True, secret=None, require_strong=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(ERROR_FRAGMENT, result.stderr)


class HstsSettingsTests(SimpleTestCase):
    def probe(
        self,
        *,
        seconds: int | None = None,
        include_subdomains: bool | None = None,
        preload: bool | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "DJANGO_DEBUG": "0",
                "DJANGO_SECRET_KEY": STRONG_TEST_VALUE,
            }
        )
        for name, value in {
            "SECURE_HSTS_SECONDS": seconds,
            "SECURE_HSTS_INCLUDE_SUBDOMAINS": include_subdomains,
            "SECURE_HSTS_PRELOAD": preload,
        }.items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = str(int(value))
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        return subprocess.run(  # noqa: S603 - executable and probe are fixed test inputs
            [
                sys.executable,
                "-c",
                "from fleetops import settings; "
                "print(settings.SECURE_HSTS_SECONDS, "
                "settings.SECURE_HSTS_INCLUDE_SUBDOMAINS, settings.SECURE_HSTS_PRELOAD)",
            ],
            cwd=BACKEND_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_hsts_defaults_are_disabled(self) -> None:
        result = self.probe()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0 False False")

    def test_hsts_scope_requires_separate_opt_in(self) -> None:
        result = self.probe(seconds=3600, include_subdomains=False, preload=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "3600 False False")

    def test_hsts_scope_can_be_enabled_independently(self) -> None:
        result = self.probe(seconds=3600, include_subdomains=True, preload=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "3600 True False")

        result = self.probe(seconds=3600, include_subdomains=False, preload=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "3600 False True")


class ComposeDeploymentTests(SimpleTestCase):
    compose: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.compose = yaml.safe_load((PROJECT_DIR / "compose.yaml").read_text(encoding="utf-8"))

    def test_long_running_services_restart_and_state_is_durable(self) -> None:
        services = self.compose["services"]
        for name in ("db", "app", "worker", "proxy"):
            with self.subTest(service=name):
                self.assertEqual(services[name]["restart"], "unless-stopped")

        self.assertIn("postgres-data:/var/lib/postgresql/data", services["db"]["volumes"])
        self.assertIn("attachment-data:/data/media", services["app"]["volumes"])
        self.assertIn("caddy-data:/data", services["proxy"]["volumes"])
        self.assertIn("caddy-config:/config", services["proxy"]["volumes"])
        self.assertTrue(
            {"postgres-data", "attachment-data", "caddy-data", "caddy-config"}
            <= self.compose["volumes"].keys()
        )

        environment = services["app"]["environment"]
        self.assertEqual(environment["SECURE_HSTS_SECONDS"], "${SECURE_HSTS_SECONDS:-0}")
        self.assertEqual(
            environment["SECURE_HSTS_INCLUDE_SUBDOMAINS"],
            "${SECURE_HSTS_INCLUDE_SUBDOMAINS:-0}",
        )
        self.assertEqual(environment["SECURE_HSTS_PRELOAD"], "${SECURE_HSTS_PRELOAD:-0}")

    def test_healthcheck_uses_documented_allowed_host(self) -> None:
        command = self.compose["services"]["app"]["healthcheck"]["test"][-1]

        self.assertIn("os.environ['ALLOWED_HOSTS'].split(',')[0].strip()", command)
        self.assertNotIn("os.environ['FLEETLINE_SITE_ADDRESS']", command)
