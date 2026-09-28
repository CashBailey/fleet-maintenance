"""Turn normalized J1939 diagnostic events into human-reviewed maintenance alerts.

A DTC is evidence for review, never an automatic diagnosis (ADR 0004). Listed
codes raise an alert on the existing Alerts queue; unlisted codes stay recorded
as NormalizedTelematicsEvent rows without alerting. Nothing here creates work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from maintenance.models import MaintenanceAlert
from maintenance.services import record_alert_occurrence

from .models import NormalizedTelematicsEvent

RULE_VERSION = "dtc-rules-2026-09-04"

# Alerts in these states have been handled by a person; the cooldown keeps the
# same code from immediately re-raising a fresh alert after closure.
CLOSED_STATUSES = ("Suppressed", "Converted", "Resolved", "Dismissed")


@dataclass(frozen=True)
class DiagnosticRule:
    spn: int
    fmi: int
    title: str
    severity: str  # "critical" | "warning" — the vocabulary the Alerts UI already renders


# ponytail: provisional starter list from SAE J1939-73 (FMI 0 = above normal,
# FMI 1 = below normal). Which codes matter for this fleet, and at what urgency,
# must come from real fault history and maintenance-manager review — see
# docs/validation-assumptions.md. Add a DB-backed override table only once
# there is data to put in it.
RULES: dict[tuple[int, int], DiagnosticRule] = {
    (rule.spn, rule.fmi): rule
    for rule in (
        DiagnosticRule(110, 0, "Engine coolant temperature above normal", "critical"),
        DiagnosticRule(111, 1, "Engine coolant level below normal", "critical"),
        DiagnosticRule(100, 1, "Engine oil pressure below normal", "critical"),
        DiagnosticRule(175, 0, "Engine oil temperature above normal", "warning"),
        DiagnosticRule(168, 0, "Battery voltage above normal", "warning"),
        DiagnosticRule(168, 1, "Battery voltage below normal", "warning"),
        DiagnosticRule(94, 1, "Fuel delivery pressure below normal", "warning"),
        DiagnosticRule(1761, 1, "DEF tank level below normal", "warning"),
        DiagnosticRule(190, 0, "Engine speed above normal", "warning"),
    )
}


def evaluate_diagnostic(event: NormalizedTelematicsEvent) -> MaintenanceAlert | None:
    """Raise or update an alert for a listed SPN/FMI pair. Returns None when nothing fires."""
    payload = event.normalized_payload
    try:
        spn, fmi = int(payload.get("spn")), int(payload.get("fmi"))
    except (TypeError, ValueError):
        return None
    rule = RULES.get((spn, fmi))
    if rule is None or event.asset is None:
        return None

    dedupe_key = f"dtc:{event.asset_id}:{spn}:{fmi}"
    cooldown = timedelta(hours=settings.DTC_ALERT_COOLDOWN_HOURS)
    recently_closed = MaintenanceAlert.objects.filter(
        organization=event.organization,
        dedupe_key=dedupe_key,
        status__in=CLOSED_STATUSES,
        updated_at__gt=timezone.now() - cooldown,
    ).exists()
    if recently_closed:
        return None

    ecu = payload.get("ecu") or "unknown ECU"
    alert, _created = record_alert_occurrence(
        organization=event.organization,
        asset=event.asset,
        source_id=str(event.pk),
        dedupe_key=dedupe_key,
        title=rule.title,
        observed_at=event.observed_at,
        severity=rule.severity,
        description=(
            f"J1939 SPN {spn} FMI {fmi} reported by device {event.device.external_id} "
            f"({ecu}). Review before creating work; a fault code is evidence, not a diagnosis."
        ),
        rule_version=RULE_VERSION,
    )
    return alert
