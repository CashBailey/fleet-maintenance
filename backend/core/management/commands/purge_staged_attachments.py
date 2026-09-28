from __future__ import annotations

from argparse import ArgumentParser
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from core.models import Attachment

STAGED_TYPES = {"defect", "inspection", "work_note", "task", "stock"}


class Command(BaseCommand):
    help = "Delete expired, unlinked offline attachment uploads and their stored bytes."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=settings.STAGED_ATTACHMENT_TTL_HOURS,
        )
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: Any) -> None:
        hours = options["older_than_hours"]
        limit = options["limit"]
        if hours < 1 or limit < 1:
            raise CommandError("older-than-hours and limit must both be positive")
        cutoff = timezone.now() - timedelta(hours=hours)
        candidates = list(
            Attachment.objects.filter(
                resource_type__in=STAGED_TYPES,
                created_at__lte=cutoff,
            ).order_by("created_at")[:limit]
        )
        if options["dry_run"]:
            self.stdout.write(f"Would purge {len(candidates)} expired staged attachments")
            return
        stored_files = [
            (attachment.file.storage, attachment.file.name) for attachment in candidates
        ]
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT set_config('fleetline.purge_staged_attachments', 'on', true)")
            Attachment.objects.filter(pk__in=[item.pk for item in candidates]).delete()
        failures = 0
        for storage, name in stored_files:
            try:
                storage.delete(name)
            except OSError:
                failures += 1
        if failures:
            raise CommandError(
                f"Purged {len(candidates)} rows but failed to remove {failures} stored files"
            )
        self.stdout.write(self.style.SUCCESS(f"Purged {len(candidates)} staged attachments"))
