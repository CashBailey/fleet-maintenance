from __future__ import annotations

import json
from argparse import ArgumentParser
from collections import Counter
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from integrations.models import Device
from integrations.services import IngestResult, ingest_autopi

ORG_PLACEHOLDER = "{{organizationId}}"
DEVICE_PLACEHOLDER = "{{deviceId}}"


def load_messages(path: Path) -> list[object]:
    """Read a JSON array or JSON Lines file. Blank and #-prefixed lines are skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"Cannot read {path}: {exc}") from exc
    if text.lstrip().startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, list):
            raise CommandError(f"{path} must be a JSON array or JSON Lines")
        return list(loaded)
    messages: list[object] = []
    for number, line in enumerate(text.splitlines(), start=1):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        try:
            messages.append(json.loads(candidate))
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} line {number} is not valid JSON: {exc}") from exc
    return messages


def substitute_identity(message: object, *, device: Device) -> object:
    """Replace only the exact placeholder strings; real ids pass through untouched."""
    if not isinstance(message, dict):
        return message
    result = dict(message)
    if result.get("organizationId") == ORG_PLACEHOLDER:
        result["organizationId"] = str(device.organization_id)
    if result.get("deviceId") == DEVICE_PLACEHOLDER:
        result["deviceId"] = device.external_id
    return result


def resolve_device(*, external_id: str, organization_slug: str | None) -> Device:
    devices = Device.objects.select_related("organization").filter(external_id=external_id)
    if organization_slug:
        devices = devices.filter(organization__slug=organization_slug)
    matches = list(devices.order_by("created_at")[:2])
    if not matches:
        raise CommandError(f"No device with external id {external_id!r}")
    if len(matches) > 1:
        raise CommandError(
            f"Device {external_id!r} exists in more than one organization; pass --organization"
        )
    return matches[0]


def outcome_label(result: IngestResult) -> str:
    if result.duplicate:
        return "duplicate"
    if result.error_code:
        return f"{result.message.status} ({result.error_code})"
    return "accepted"


def message_label(raw: object, result: IngestResult) -> str:
    if result.message.message_id:
        return result.message.message_id
    if isinstance(raw, dict) and raw.get("messageId"):
        return str(raw["messageId"])
    return "<no messageId>"


class DryRunRollback(Exception):
    """Raised inside the atomic block so a --dry-run replay is rolled back."""


class Command(BaseCommand):
    help = "Replay a file of saved AutoPi messages through the real ingest path."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("file", type=Path, help="JSON Lines or JSON array of messages")
        parser.add_argument(
            "--device", required=True, help="Device.external_id that sent the messages"
        )
        parser.add_argument(
            "--organization", default=None, help="Organization slug, if --device is ambiguous"
        )
        parser.add_argument("--dry-run", action="store_true", help="Replay, then roll back")

    def handle(self, *args: object, **options: Any) -> None:
        device = resolve_device(
            external_id=options["device"], organization_slug=options["organization"]
        )
        messages = load_messages(options["file"])
        self.stdout.write(f"Replaying {len(messages)} messages as device {device.external_id}")

        outcomes: Counter[str] = Counter()
        qualities: Counter[str] = Counter()

        def replay_all() -> None:
            for index, raw in enumerate(messages, start=1):
                result = ingest_autopi(
                    device=device, payload=substitute_identity(raw, device=device)
                )
                label = outcome_label(result)
                outcomes[label] += 1
                self.stdout.write(f"{index:>4}  {message_label(raw, result)} {label}")
                for event in result.events:
                    qualities[f"{event.kind}:{event.quality}"] += 1
                    reason = f" — {event.reason}" if event.reason else ""
                    self.stdout.write(
                        f"        {event.kind} {event.signal} -> {event.quality}{reason}"
                    )

        if options["dry_run"]:
            try:
                with transaction.atomic():
                    replay_all()
                    raise DryRunRollback()
            except DryRunRollback:
                pass
        else:
            replay_all()

        self.stdout.write("")
        self.stdout.write("Summary")
        for label, count in sorted(outcomes.items()):
            self.stdout.write(f"  {label}: {count}")
        for label, count in sorted(qualities.items()):
            self.stdout.write(f"  events {label}: {count}")
        if options["dry_run"]:
            self.stdout.write("DRY RUN — nothing written")
