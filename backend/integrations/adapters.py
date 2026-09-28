from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.utils.dateparse import parse_datetime


class AdapterError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_message"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MeterValue:
    signal: str
    meter_kind: str
    value: Decimal
    unit: str
    original_value: Decimal
    original_unit: str


@dataclass(frozen=True)
class TelematicsEnvelope:
    schema_version: str
    message_id: str
    organization_id: str
    device_id: str
    observed_at: datetime
    sent_at: datetime | None
    sequence: int | None
    source: str
    message_type: str
    meters: tuple[MeterValue, ...]
    diagnostics: tuple[dict[str, Any], ...]
    canonical_hash: str
    legacy_canonical_hash: str


def _timestamp(value: object, field: str, *, required: bool = True) -> datetime | None:
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str):
        raise AdapterError(f"{field} must be an ISO 8601 timestamp", code="invalid_timestamp")
    parsed = parse_datetime(value)
    if parsed is None or parsed.utcoffset() is None:
        raise AdapterError(f"{field} must include a timezone", code="invalid_timestamp")
    return parsed


def _decimal(value: object, signal: str) -> Decimal:
    if isinstance(value, bool):
        raise AdapterError(f"{signal} value must be numeric", code="invalid_value")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AdapterError(f"{signal} value must be numeric", code="invalid_value") from exc
    if not parsed.is_finite() or parsed < 0:
        raise AdapterError(f"{signal} value must be finite and non-negative", code="invalid_value")
    return parsed


class AutoPiAdapter:
    source = "autopi"
    schema_version = "1.0"

    def parse(self, payload: object) -> TelematicsEnvelope:
        if not isinstance(payload, dict):
            raise AdapterError("Message body must be a JSON object")
        version = str(payload.get("schemaVersion", ""))
        if version != self.schema_version:
            raise AdapterError("Unsupported schemaVersion", code="unsupported_schema_version")
        source = str(payload.get("source", ""))
        if source != self.source:
            raise AdapterError("source must be autopi", code="invalid_source")
        message_type = str(payload.get("type", ""))
        if message_type not in {"telemetry", "diagnostics", "status"}:
            raise AdapterError("Unsupported message type", code="unsupported_message_type")
        organization_id = str(payload.get("organizationId", "")).strip()
        device_id = str(payload.get("deviceId", "")).strip()
        if not organization_id or not device_id:
            raise AdapterError("organizationId and deviceId are required", code="missing_identity")
        message_id = str(payload.get("messageId", "")).strip()
        if len(message_id) > 160:
            raise AdapterError("messageId is too long", code="invalid_message_id")
        observed_at = _timestamp(payload.get("observedAt"), "observedAt")
        assert observed_at is not None
        sent_at = _timestamp(payload.get("sentAt"), "sentAt", required=False)
        if sent_at and sent_at < observed_at:
            raise AdapterError("sentAt cannot be before observedAt", code="invalid_timestamp")
        sequence_raw = payload.get("sequence")
        if sequence_raw is None:
            sequence = None
        elif (
            isinstance(sequence_raw, bool) or not isinstance(sequence_raw, int) or sequence_raw < 0
        ):
            raise AdapterError("sequence must be a non-negative integer", code="invalid_sequence")
        else:
            sequence = sequence_raw

        meters: list[MeterValue] = []
        values = payload.get("values", {})
        if not isinstance(values, dict):
            raise AdapterError("values must be an object", code="invalid_values")
        definitions = {
            "odometer": ("odometer", {"mi": Decimal("1"), "km": Decimal("0.621371192")}, "mi"),
            "engineHours": (
                "engine_hours",
                {"h": Decimal("1"), "hr": Decimal("1"), "hours": Decimal("1")},
                "h",
            ),
        }
        for signal, (meter_kind, conversions, canonical_unit) in definitions.items():
            if signal not in values:
                continue
            reading = values[signal]
            if not isinstance(reading, dict):
                raise AdapterError(f"{signal} must contain value and unit", code="invalid_value")
            original_unit = str(reading.get("unit", "")).strip()
            if original_unit not in conversions:
                raise AdapterError(f"Unsupported unit for {signal}", code="unsupported_unit")
            original_value = _decimal(reading.get("value"), signal)
            meters.append(
                MeterValue(
                    signal=signal,
                    meter_kind=meter_kind,
                    value=original_value * conversions[original_unit],
                    unit=canonical_unit,
                    original_value=original_value,
                    original_unit=original_unit,
                )
            )

        diagnostics_raw = payload.get("diagnostics", [])
        if diagnostics_raw is None:
            diagnostics_raw = []
        if not isinstance(diagnostics_raw, list) or not all(
            isinstance(item, dict) for item in diagnostics_raw
        ):
            raise AdapterError("diagnostics must be a list of objects", code="invalid_diagnostics")
        if len(diagnostics_raw) > settings.TELEMATICS_MAX_DIAGNOSTICS:
            raise AdapterError(
                "diagnostics exceeds the configured item limit",
                code="too_many_diagnostics",
            )

        legacy_canonical = {
            "device": device_id,
            "observedAt": observed_at.isoformat(),
            "sentAt": sent_at.isoformat() if sent_at else None,
            "sequence": sequence,
            "source": source,
            "type": message_type,
            "values": values,
            "diagnostics": diagnostics_raw,
        }
        legacy_digest = hashlib.sha256(
            json.dumps(
                legacy_canonical, sort_keys=True, separators=(",", ":"), default=str
            ).encode()
        ).hexdigest()
        canonical = {
            "organization": organization_id,
            "device": device_id,
            "observedAt": observed_at.isoformat(),
            "sentAt": sent_at.isoformat() if sent_at else None,
            "sequence": sequence,
            "source": source,
            "type": message_type,
            "meters": [
                {
                    "signal": meter.signal,
                    "meterKind": meter.meter_kind,
                    "value": format(meter.value.normalize(), "f"),
                    "unit": meter.unit,
                }
                for meter in meters
            ],
            "diagnostics": diagnostics_raw,
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return TelematicsEnvelope(
            schema_version=version,
            message_id=message_id,
            organization_id=organization_id,
            device_id=device_id,
            observed_at=observed_at,
            sent_at=sent_at,
            sequence=sequence,
            source=source,
            message_type=message_type,
            meters=tuple(meters),
            diagnostics=tuple(diagnostics_raw),
            canonical_hash=digest,
            legacy_canonical_hash=legacy_digest,
        )
