#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_root"

mkdir -p artifacts/verify .e2e
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
artifact_dir="$repo_root/artifacts/verify/$run_id"
mkdir -p "$artifact_dir"
exec > >(tee "$artifact_dir/verify.log") 2>&1

echo "Installing locked dependencies..."
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements-dev.lock
npm ci

echo "Running static checks and production build..."
.venv/bin/ruff check backend
.venv/bin/ruff format --check backend
DJANGO_SECRET_KEY=verify-static-only-secret-key-with-at-least-fifty-characters-00000 \
  .venv/bin/mypy backend
npm run lint
npm run typecheck
npm run build

# postgres-test-lib.sh is resolved from the runtime repository root.
# shellcheck disable=SC1091
source "$repo_root/scripts/postgres-test-lib.sh"
python_bin="$repo_root/.venv/bin/python"
postgres_dir=$(mktemp -d "$repo_root/.e2e/verify.XXXXXX")
free_port() {
  "$python_bin" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}
postgres_port=$(free_port)
restore_app_pid=
restore_worker_pid=
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$restore_app_pid" ]]; then
    kill -TERM "$restore_app_pid" 2>/dev/null || true
    wait "$restore_app_pid" 2>/dev/null || true
  fi
  if [[ -n "$restore_worker_pid" ]]; then
    kill -TERM "$restore_worker_pid" 2>/dev/null || true
    wait "$restore_worker_pid" 2>/dev/null || true
  fi
  [[ -f "$postgres_dir/postgres.log" ]] && cp "$postgres_dir/postgres.log" "$artifact_dir/postgres.log"
  fleetline_stop_postgres "$postgres_dir" || true
  case "$postgres_dir" in
    "$repo_root"/.e2e/verify.*) rm -rf -- "$postgres_dir" ;;
    *) echo "Refusing to remove unexpected verification directory: $postgres_dir" >&2 ;;
  esac
  echo "Verification artifacts: $artifact_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "Starting clean PostgreSQL and validating migrations..."
fleetline_start_postgres "$postgres_dir" "$postgres_port" fleetline_verify
export DJANGO_SECRET_KEY="verify-only-secret-key-with-at-least-fifty-characters-000000000"
export DJANGO_DEBUG=0
export ALLOWED_HOSTS="127.0.0.1,localhost"
export CSRF_TRUSTED_ORIGINS="http://127.0.0.1"
export COOKIE_SECURE=0
export SESSION_COOKIE_AGE=28800
export LOGIN_ATTEMPT_LIMIT=5
export LOGIN_ATTEMPT_WINDOW_SECONDS=300
export LOGIN_LOCKOUT_SECONDS=900
export SECURE_SSL_REDIRECT=0
export SECURE_HSTS_SECONDS=0
export SECURE_HSTS_INCLUDE_SUBDOMAINS=0
export SECURE_HSTS_PRELOAD=0
export TRUST_PROXY_HEADERS=0
export DB_CONN_MAX_AGE=0
export FLEETLINE_SITE_ADDRESS=localhost
export TIME_ZONE=America/Chicago
export OFFLINE_CACHE_HOURS=24
export ATTACHMENT_MAX_BYTES=10485760
export STAGED_ATTACHMENT_MAX_COUNT=25
export STAGED_ATTACHMENT_MAX_BYTES=52428800
export STAGED_ATTACHMENT_TTL_HOURS=24
export PO_APPROVAL_THRESHOLD=1000.00
export METER_MAX_MILES_PER_HOUR=100.0
export METER_MAX_ENGINE_HOURS_PER_HOUR=1.25
export METER_FUTURE_TOLERANCE_SECONDS=300
export PM_RECALCULATION_INTERVAL_SECONDS=3600
export WEBHOOK_ALLOW_HTTP=0
export WEBHOOK_ALLOW_PRIVATE_NETWORKS=0
export REQUIRE_WORKER=1
export FLEETLINE_RESTORE_MAX_ARCHIVE_BYTES=107374182400
export FLEETLINE_RESTORE_MAX_BUNDLE_BYTES=107374182400
export FLEETLINE_RESTORE_MAX_DATABASE_BYTES=53687091200
export FLEETLINE_RESTORE_MAX_MEDIA_ARCHIVE_BYTES=53687091200
export FLEETLINE_RESTORE_MAX_MEDIA_MEMBERS=1000000
export FLEETLINE_RESTORE_MAX_MEDIA_BYTES=536870912000
export MEDIA_ROOT="$postgres_dir/media"
export E2E_USERNAME=${E2E_USERNAME:-fleet.manager@example.com}
export E2E_PASSWORD=${E2E_PASSWORD:-DemoPass123!}
export FLEETLINE_ALLOW_DEMO_SEED=1
export INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD=0.00
mkdir -p "$MEDIA_ROOT"

(
  cd backend
  "$python_bin" manage.py makemigrations --check --dry-run
  "$python_bin" manage.py migrate --noinput
  "$python_bin" manage.py seed_demo
  "$python_bin" manage.py check
  DJANGO_DEBUG=0 \
    ALLOWED_HOSTS=fleetline.example.com \
    CSRF_TRUSTED_ORIGINS=https://fleetline.example.com \
    COOKIE_SECURE=1 \
    SECURE_SSL_REDIRECT=1 \
    SECURE_HSTS_SECONDS=31536000 \
    SECURE_HSTS_INCLUDE_SUBDOMAINS=1 \
    SECURE_HSTS_PRELOAD=1 \
    TRUST_PROXY_HEADERS=1 \
    "$python_bin" manage.py check --deploy --tag security --fail-level WARNING
  "$python_bin" manage.py spectacular --validate --fail-on-warn --file "$artifact_dir/openapi.yaml"
  "$python_bin" -m coverage erase
  "$python_bin" -m coverage run manage.py test --noinput
  "$python_bin" -m coverage xml -o "$artifact_dir/coverage.xml"
  "$python_bin" -m coverage report
)

snapshot_domain_state() {
  local output=$1
  (
    cd backend
    FLEETLINE_SNAPSHOT_FILE="$output" "$python_bin" manage.py shell <<'PY'
import json
import os
from pathlib import Path

from assets.models import Asset, Meter, MeterReading
from core.models import (
    Attachment,
    AuditEvent,
    Document,
    DocumentApplicability,
    DocumentPage,
    Organization,
    User,
)
from inventory.models import StockBalance, StockTransaction
from maintenance.models import WorkOrder, WorkOrderCloseSnapshot
from purchasing.models import PurchaseOrder, Receipt


def rows(queryset, *fields):
    return list(queryset.order_by("id").values(*fields))


users = User.objects.select_related("organization").prefetch_related("roles").order_by("id")
state = {
    "organizations": rows(Organization.objects.all(), "id", "slug", "name", "settings"),
    "users": [
        {
            "id": user.id,
            "organization_id": user.organization_id,
            "username": user.username,
            "is_active": user.is_active,
            "offline_access_revoked_at": user.offline_access_revoked_at,
            "roles": list(user.roles.order_by("slug").values_list("slug", flat=True)),
        }
        for user in users
    ],
    "assets": rows(
        Asset.objects.all(),
        "id", "organization_id", "unit_number", "status", "assigned_driver_id", "archived_at",
    ),
    "meters": [
        {
            "id": meter.id,
            "organization_id": meter.organization_id,
            "asset_id": meter.asset_id,
            "name": meter.name,
            "kind": meter.kind,
            "unit": meter.unit,
            "current_reading_id": meter.current_reading.id if meter.current_reading else None,
            "current_value": meter.current_value,
        }
        for meter in Meter.objects.select_related("asset").order_by("id")
    ],
    "meter_readings": rows(
        MeterReading.objects.all(),
        "id", "organization_id", "meter_id", "value", "observed_at", "received_at", "source",
        "quality", "external_id", "corrects_id", "reason",
    ),
    "work_orders": rows(
        WorkOrder.objects.all(),
        "id", "organization_id", "number", "asset_id", "status", "request_id",
        "maintenance_plan_id", "service_package_id", "service_package_snapshot",
        "completion_meter_id", "version",
    ),
    "work_order_close_snapshots": rows(
        WorkOrderCloseSnapshot.objects.all(),
        "id", "organization_id", "work_order_id", "sequence", "closed_by_id",
        "closed_at", "snapshot", "created_at",
    ),
    "stock_balances": rows(
        StockBalance.objects.all(),
        "id", "organization_id", "part_id", "bin_id", "quantity_on_hand", "quantity_reserved",
    ),
    "stock_transactions": rows(
        StockTransaction.objects.all(),
        "id", "organization_id", "part_id", "bin_id", "transaction_type", "quantity",
        "unit_cost", "total_cost", "work_order_id", "reservation_id", "original_transaction_id",
        "reference_type", "reference_id", "operation_id", "actor_id", "created_at",
    ),
    "purchase_orders": rows(
        PurchaseOrder.objects.all(),
        "id", "organization_id", "number", "status", "created_by_id", "submitted_by_id",
        "approved_by_id", "approval_required",
    ),
    "receipts": rows(
        Receipt.objects.all(),
        "id", "organization_id", "purchase_order_id", "number", "status", "operation_id",
        "reversal_of_id",
    ),
    "audit_events": rows(
        AuditEvent.objects.all(),
        "id", "organization_id", "actor_id", "action", "resource_type", "resource_id",
        "previous_state", "new_state", "context", "correlation_id", "source", "occurred_at",
    ),
    "attachments": rows(
        Attachment.objects.all(),
        "id", "organization_id", "uploader_id", "resource_type", "resource_id", "file",
        "sensitivity", "document_key", "category", "title", "version", "supersedes_id",
        "original_name", "content_type", "size", "sha256",
    ),
    "documents": rows(
        Document.objects.all(),
        "id", "organization_id", "attachment_id", "asset_id", "title", "category",
        "manufacturer", "model", "engine_type", "revision", "source", "license", "status",
        "processing_detail", "security_review_reference", "security_review_note",
        "security_reviewed_by_id", "security_reviewed_at", "processed_at", "supersedes_id",
        "created_at", "updated_at",
    ),
    "document_applicability": rows(
        DocumentApplicability.objects.all(),
        "id", "organization_id", "document_id", "asset_id", "asset_type_id", "make", "model",
        "engine_type", "created_at", "updated_at",
    ),
    "document_pages": rows(
        DocumentPage.objects.all(),
        "id", "organization_id", "document_id", "page_number", "text", "extraction_method",
        "confidence", "provenance", "search_vector", "created_at",
    ),
}
Path(os.environ["FLEETLINE_SNAPSHOT_FILE"]).write_text(
    json.dumps(state, default=str, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  )
}

snapshot_media() {
  local root=$1
  local output=$2
  "$python_bin" - "$root" "$output" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
manifest = {}
for path in sorted(item for item in root.rglob("*") if item.is_file()):
    manifest[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
Path(sys.argv[2]).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

echo "Testing repository backup and clean restore..."
pg_bin=$(fleetline_pg_bin)
export PATH="$pg_bin:$PATH"
backup_archive="$artifact_dir/fleetline-verify.tar.gz.gpg"
backup_config="$postgres_dir/source.env"
backup_passphrase="$postgres_dir/backup-passphrase"
wrong_passphrase="$postgres_dir/wrong-passphrase"
restore_config="$postgres_dir/restored.env"
restore_media="$postgres_dir/restored-media"
proof_metadata="$artifact_dir/backup-attachment.json"
source_state="$artifact_dir/backup-source-state.json"
restored_state="$artifact_dir/backup-restored-state.json"
source_media_manifest="$artifact_dir/backup-source-media.json"
restored_media_manifest="$artifact_dir/backup-restored-media.json"

(
  cd backend
  export FLEETLINE_BACKUP_FIXTURE="$proof_metadata"
  "$python_bin" manage.py shell <<'PY'
import hashlib
import json
import os
from pathlib import Path

from assets.models import Asset
from core.models import Attachment, Document, DocumentApplicability, DocumentPage, Organization, User
from core.services import audit
from django.core.files.base import ContentFile
from django.utils import timezone
from inventory.models import Part

payload = b"Fleetline backup and restore verification attachment.\n"
organization = Organization.objects.get(slug="gator-fleet")
uploader = User.objects.get(username="fleet.manager@example.com")
asset = Asset.objects.get(organization=organization, unit_number="TRK-012")
attachment = Attachment(
    organization=organization,
    uploader=uploader,
    resource_type="asset",
    resource_id=str(asset.id),
    original_name="backup-restore-proof.txt",
    content_type="text/plain",
    size=len(payload),
    sha256=hashlib.sha256(payload).hexdigest(),
)
attachment.file.save(attachment.original_name, ContentFile(payload), save=False)
attachment.save()
audit(
    organization=organization,
    actor=uploader,
    action="attachment.created",
    resource=attachment,
    context={"sha256": attachment.sha256, "verification_fixture": True},
)

part = Part.objects.get(organization=organization, number="FIL-1001")
financial_payload = b"Fleetline backup financial attachment verification.\n"
financial_attachment = Attachment(
    organization=organization,
    uploader=uploader,
    resource_type="part",
    resource_id=str(part.id),
    sensitivity=Attachment.Sensitivity.FINANCIAL,
    category="supplier_quote",
    title="Backup financial attachment proof",
    original_name="backup-financial-proof.txt",
    content_type="text/plain",
    size=len(financial_payload),
    sha256=hashlib.sha256(financial_payload).hexdigest(),
)
financial_attachment.file.save(
    financial_attachment.original_name, ContentFile(financial_payload), save=False
)
financial_attachment.save()

manual_payload = b"%PDF-1.4\n% Fleetline technical-manual backup fixture\n%%EOF\n"
manual_attachment = Attachment(
    organization=organization,
    uploader=uploader,
    resource_type="asset",
    resource_id=str(asset.id),
    sensitivity=Attachment.Sensitivity.OPERATIONAL,
    category="torque_specification",
    title="Backup torque specification manual",
    original_name="backup-torque-manual.pdf",
    content_type="application/pdf",
    size=len(manual_payload),
    sha256=hashlib.sha256(manual_payload).hexdigest(),
)
manual_attachment.file.save(manual_attachment.original_name, ContentFile(manual_payload), save=False)
manual_attachment.save()
document = Document(
    organization=organization,
    attachment=manual_attachment,
    asset=asset,
    title="Backup torque specification manual",
    category="torque_specification",
    manufacturer=asset.make,
    model=asset.model,
    revision="backup-verify-v1",
    source="Fleetline clean-restore verification fixture",
    license="Internal test fixture",
)
document.full_clean()
document.save()
DocumentApplicability.objects.create(
    organization=organization,
    document=document,
    asset_type=asset.asset_type,
    make=asset.make,
    model=asset.model,
)
document.status = Document.Status.QUEUED
document.processing_detail = "Approved for verification extraction."
document.security_review_reference = "backup-verify-scan-001"
document.security_review_note = "Approved verification fixture."
document.security_reviewed_by = uploader
document.security_reviewed_at = timezone.now()
document.save(
    update_fields=[
        "status", "processing_detail", "security_review_reference", "security_review_note",
        "security_reviewed_by", "security_reviewed_at", "updated_at",
    ]
)
document.status = Document.Status.PROCESSING
document.processing_detail = "Verification extraction started."
document.save(update_fields=["status", "processing_detail", "updated_at"])
DocumentPage.objects.create(
    organization=organization,
    document=document,
    page_number=1,
    text="Backup verification torque specification: tighten the fastener to 125 lb-ft.",
    extraction_method=DocumentPage.ExtractionMethod.EMBEDDED,
    provenance={"tool": "backup-restore-verification", "source": "embedded-text"},
)
document.status = Document.Status.INDEXED
document.processing_detail = "Indexed by verification fixture."
document.processed_at = timezone.now()
document.save(update_fields=["status", "processing_detail", "processed_at", "updated_at"])
Path(os.environ["FLEETLINE_BACKUP_FIXTURE"]).write_text(
    json.dumps(
        {
            "id": str(attachment.id),
            "file": attachment.file.name,
            "sha256": attachment.sha256,
            "financial_attachment_id": str(financial_attachment.id),
            "financial_attachment_sha256": financial_attachment.sha256,
            "financial_part_id": str(part.id),
            "document_id": str(document.id),
            "document_sha256": manual_attachment.sha256,
            "document_asset_id": str(asset.id),
        },
        indent=2,
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
PY
)

VERIFY_CONFIG_FILE="$backup_config" VERIFY_PASSPHRASE_FILE="$backup_passphrase" \
  VERIFY_WRONG_PASSPHRASE_FILE="$wrong_passphrase" "$python_bin" <<'PY'
import os
import secrets
from pathlib import Path

names = (
    "DJANGO_SECRET_KEY", "DJANGO_DEBUG", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS",
    "COOKIE_SECURE", "SESSION_COOKIE_AGE", "LOGIN_ATTEMPT_LIMIT",
    "LOGIN_ATTEMPT_WINDOW_SECONDS", "LOGIN_LOCKOUT_SECONDS", "SECURE_SSL_REDIRECT",
    "SECURE_HSTS_SECONDS", "SECURE_HSTS_INCLUDE_SUBDOMAINS", "SECURE_HSTS_PRELOAD",
    "TRUST_PROXY_HEADERS", "FLEETLINE_SITE_ADDRESS", "POSTGRES_HOST", "POSTGRES_PORT",
    "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "DB_CONN_MAX_AGE", "MEDIA_ROOT",
    "TIME_ZONE", "OFFLINE_CACHE_HOURS", "ATTACHMENT_MAX_BYTES",
    "STAGED_ATTACHMENT_MAX_COUNT", "STAGED_ATTACHMENT_MAX_BYTES",
    "STAGED_ATTACHMENT_TTL_HOURS", "PO_APPROVAL_THRESHOLD",
    "INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD", "METER_MAX_MILES_PER_HOUR",
    "METER_MAX_ENGINE_HOURS_PER_HOUR", "METER_FUTURE_TOLERANCE_SECONDS",
    "PM_RECALCULATION_INTERVAL_SECONDS",
    "WEBHOOK_ALLOW_HTTP", "WEBHOOK_ALLOW_PRIVATE_NETWORKS", "REQUIRE_WORKER",
    "FLEETLINE_RESTORE_MAX_ARCHIVE_BYTES", "FLEETLINE_RESTORE_MAX_BUNDLE_BYTES",
    "FLEETLINE_RESTORE_MAX_DATABASE_BYTES", "FLEETLINE_RESTORE_MAX_MEDIA_ARCHIVE_BYTES",
    "FLEETLINE_RESTORE_MAX_MEDIA_MEMBERS", "FLEETLINE_RESTORE_MAX_MEDIA_BYTES",
)
target = Path(os.environ["VERIFY_CONFIG_FILE"])
target.write_text("".join(f"{name}={os.environ.get(name, '')}\n" for name in names), encoding="utf-8")
target.chmod(0o600)
for variable in ("VERIFY_PASSPHRASE_FILE", "VERIFY_WRONG_PASSPHRASE_FILE"):
    secret = Path(os.environ[variable])
    secret.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    secret.chmod(0o600)
PY

snapshot_domain_state "$source_state"
snapshot_media "$MEDIA_ROOT" "$source_media_manifest"
export FLEETLINE_BACKUP_PASSPHRASE_FILE="$backup_passphrase"
if env -u FLEETLINE_BACKUP_PASSPHRASE_FILE FLEETLINE_RUNTIME=native \
  FLEETLINE_CONFIG_FILE="$backup_config" \
  "$repo_root/scripts/backup.sh" "$postgres_dir/unencrypted-backup-must-not-exist.gpg" \
  >"$artifact_dir/backup-missing-secret-rejection.log" 2>&1; then
  echo "Backup proceeded without a passphrase file" >&2
  exit 1
fi
rg -q "FLEETLINE_BACKUP_PASSPHRASE_FILE is required" \
  "$artifact_dir/backup-missing-secret-rejection.log"
[[ ! -e "$postgres_dir/unencrypted-backup-must-not-exist.gpg" ]]
FLEETLINE_RUNTIME=native FLEETLINE_CONFIG_FILE="$backup_config" \
  "$repo_root/scripts/backup.sh" "$backup_archive"
[[ "$(stat -c '%a' "$backup_archive")" == 600 ]] || {
  echo "Backup archive permissions are not 0600" >&2
  exit 1
}

encrypted_tampered="$postgres_dir/encrypted-tampered.tar.gz.gpg"
decrypted_bundle="$postgres_dir/decrypted-bundle.tar.gz"
checksum_plain="$postgres_dir/checksum-tampered.tar.gz"
unsafe_plain="$postgres_dir/unsafe-media.tar.gz"
checksum_encrypted="$postgres_dir/checksum-tampered.tar.gz.gpg"
unsafe_encrypted="$postgres_dir/unsafe-media.tar.gz.gpg"
restore_limit_prefix="$postgres_dir/restore-limit"

"$python_bin" - "$backup_archive" "$encrypted_tampered" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_bytes()
if len(source) < 32:
    raise SystemExit("encrypted backup is unexpectedly short")
tampered = bytearray(source)
tampered[len(tampered) // 2] ^= 1
Path(sys.argv[2]).write_bytes(tampered)
PY

if FLEETLINE_BACKUP_PASSPHRASE_FILE="$wrong_passphrase" \
  "$repo_root/scripts/restore.sh" --config-only "$backup_archive" \
  "$postgres_dir/wrong-secret.env" >"$artifact_dir/backup-wrong-secret-rejection.log" 2>&1; then
  echo "Restore accepted the wrong backup passphrase" >&2
  exit 1
fi
rg -q "backup decryption failed" "$artifact_dir/backup-wrong-secret-rejection.log"

if "$repo_root/scripts/restore.sh" --config-only "$encrypted_tampered" \
  "$postgres_dir/encrypted-tampered.env" \
  >"$artifact_dir/backup-encrypted-tamper-rejection.log" 2>&1; then
  echo "Restore accepted a tampered encrypted archive" >&2
  exit 1
fi
rg -q "backup decryption failed" "$artifact_dir/backup-encrypted-tamper-rejection.log"

gpg --no-options --batch --yes --no-tty --pinentry-mode loopback --no-symkey-cache \
  --passphrase-file "$backup_passphrase" --decrypt --output "$decrypted_bundle" "$backup_archive"
"$python_bin" - "$decrypted_bundle" "$checksum_plain" "$unsafe_plain" <<'PY'
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile

source, checksum_target, unsafe_target = map(Path, sys.argv[1:])
members = ("database.dump", "media.tar.gz", "config.env", "metadata.txt", "checksums.sha256")


def unpack(target):
    with tarfile.open(source, "r:gz") as bundle:
        bundle.extractall(target, filter="data")


def pack(root, target):
    with tarfile.open(target, "w:gz") as bundle:
        for name in members:
            bundle.add(root / name, arcname=name)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    unpack(root)
    (root / "config.env").write_bytes((root / "config.env").read_bytes() + b"# tampered\n")
    pack(root, checksum_target)

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    unpack(root)
    payload = b"must not escape\n"
    with tarfile.open(root / "media.tar.gz", "w:gz") as media:
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(payload)
        media.addfile(info, io.BytesIO(payload))
    checked = ("database.dump", "media.tar.gz", "config.env", "metadata.txt")
    checksums = "".join(
        f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}\n" for name in checked
    )
    (root / "checksums.sha256").write_text(checksums, encoding="ascii")
    pack(root, unsafe_target)
PY
rm -f -- "$decrypted_bundle"

"$python_bin" - "$restore_limit_prefix" <<'PY'
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile

prefix = Path(sys.argv[1])
kinds = ("limit", "symlink", "hardlink", "device", "special", "sparse")


def add_bytes(bundle, name, payload):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    bundle.addfile(info, io.BytesIO(payload))


def write_media(path, kind):
    with tarfile.open(path, "w:gz") as media:
        if kind == "limit":
            add_bytes(media, "one.txt", b"aa")
            add_bytes(media, "two.txt", b"bb")
            return
        info = tarfile.TarInfo(f"{kind}-entry")
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "target"
        elif kind == "hardlink":
            info.type = tarfile.LNKTYPE
            info.linkname = "target"
        elif kind == "device":
            info.type = tarfile.CHRTYPE
            info.devmajor = 1
            info.devminor = 3
        elif kind == "special":
            info.type = tarfile.FIFOTYPE
        elif kind == "sparse":
            info.type = tarfile.GNUTYPE_SPARSE
        media.addfile(info)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


for kind in kinds:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "database.dump").write_bytes(b"db")
        (root / "config.env").write_text("TEST_CONFIG=1\n", encoding="utf-8")
        (root / "metadata.txt").write_text("format_version=1\n", encoding="utf-8")
        write_media(root / "media.tar.gz", kind)
        checked = ("database.dump", "media.tar.gz", "config.env", "metadata.txt")
        (root / "checksums.sha256").write_text(
            "".join(f"{sha256(root / name)}  {name}\n" for name in checked),
            encoding="ascii",
        )
        with tarfile.open(f"{prefix}-{kind}.tar.gz", "w:gz") as outer:
            for name in (*checked, "checksums.sha256"):
                outer.add(root / name, arcname=name)
PY

for fixture in limit symlink hardlink device special sparse; do
  gpg --no-options --batch --yes --no-tty --pinentry-mode loopback --no-symkey-cache \
    --passphrase-file "$backup_passphrase" --symmetric --cipher-algo AES256 \
    --output "$restore_limit_prefix-$fixture.tar.gz.gpg" \
    "$restore_limit_prefix-$fixture.tar.gz"
  rm -f -- "$restore_limit_prefix-$fixture.tar.gz"
done

gpg --no-options --batch --yes --no-tty --pinentry-mode loopback --no-symkey-cache \
  --passphrase-file "$backup_passphrase" --symmetric --cipher-algo AES256 \
  --output "$checksum_encrypted" "$checksum_plain"
gpg --no-options --batch --yes --no-tty --pinentry-mode loopback --no-symkey-cache \
  --passphrase-file "$backup_passphrase" --symmetric --cipher-algo AES256 \
  --output "$unsafe_encrypted" "$unsafe_plain"
rm -f -- "$checksum_plain" "$unsafe_plain"

if FLEETLINE_RESTORE_MAX_ARCHIVE_BYTES=1 "$repo_root/scripts/restore.sh" --config-only \
  "$restore_limit_prefix-limit.tar.gz.gpg" "$postgres_dir/archive-over-limit.env" \
  >"$artifact_dir/backup-archive-limit-rejection.log" 2>&1; then
  echo "Restore accepted an encrypted archive over its configured limit" >&2
  exit 1
fi
rg -q "encrypted archive exceeds configured size limit" \
  "$artifact_dir/backup-archive-limit-rejection.log"

if FLEETLINE_RESTORE_MAX_BUNDLE_BYTES=1 "$repo_root/scripts/restore.sh" --config-only \
  "$restore_limit_prefix-limit.tar.gz.gpg" "$postgres_dir/bundle-over-limit.env" \
  >"$artifact_dir/backup-bundle-limit-rejection.log" 2>&1; then
  echo "Restore accepted an outer bundle over its configured limit" >&2
  exit 1
fi
rg -q "outer bundle exceeds configured size limit" \
  "$artifact_dir/backup-bundle-limit-rejection.log"

if FLEETLINE_RESTORE_MAX_DATABASE_BYTES=1 "$repo_root/scripts/restore.sh" --config-only \
  "$restore_limit_prefix-limit.tar.gz.gpg" "$postgres_dir/database-over-limit.env" \
  >"$artifact_dir/backup-database-limit-rejection.log" 2>&1; then
  echo "Restore accepted a database dump over its configured limit" >&2
  exit 1
fi
rg -q "database.dump exceeds configured size limit" \
  "$artifact_dir/backup-database-limit-rejection.log"

if FLEETLINE_RESTORE_MAX_MEDIA_ARCHIVE_BYTES=1 \
  "$repo_root/scripts/restore.sh" --config-only \
  "$restore_limit_prefix-limit.tar.gz.gpg" "$postgres_dir/media-archive-over-limit.env" \
  >"$artifact_dir/backup-media-archive-limit-rejection.log" 2>&1; then
  echo "Restore accepted a compressed media archive over its configured limit" >&2
  exit 1
fi
rg -q "media.tar.gz exceeds configured size limit" \
  "$artifact_dir/backup-media-archive-limit-rejection.log"

if FLEETLINE_RUNTIME=native FLEETLINE_RESTORE_MAX_MEDIA_MEMBERS=1 \
  "$repo_root/scripts/restore.sh" "$restore_limit_prefix-limit.tar.gz.gpg" \
  >"$artifact_dir/backup-media-member-limit-rejection.log" 2>&1; then
  echo "Restore accepted too many attachment members" >&2
  exit 1
fi
rg -q "attachment archive exceeds configured member limit" \
  "$artifact_dir/backup-media-member-limit-rejection.log"

if FLEETLINE_RUNTIME=native FLEETLINE_RESTORE_MAX_MEDIA_BYTES=1 \
  "$repo_root/scripts/restore.sh" "$restore_limit_prefix-limit.tar.gz.gpg" \
  >"$artifact_dir/backup-media-size-limit-rejection.log" 2>&1; then
  echo "Restore accepted expanded attachment data over its configured limit" >&2
  exit 1
fi
rg -q "attachment archive exceeds configured expanded size limit" \
  "$artifact_dir/backup-media-size-limit-rejection.log"

for fixture in symlink hardlink device special sparse; do
  if FLEETLINE_RUNTIME=native "$repo_root/scripts/restore.sh" \
    "$restore_limit_prefix-$fixture.tar.gz.gpg" \
    >"$artifact_dir/backup-$fixture-rejection.log" 2>&1; then
    echo "Restore accepted unsupported attachment entry: $fixture" >&2
    exit 1
  fi
done
rg -q "unsupported link or device" "$artifact_dir/backup-symlink-rejection.log"
rg -q "unsupported link or device" "$artifact_dir/backup-hardlink-rejection.log"
rg -q "unsupported link or device" "$artifact_dir/backup-device-rejection.log"
rg -q "unsupported special entry" "$artifact_dir/backup-special-rejection.log"
rg -q "sparse entry" "$artifact_dir/backup-sparse-rejection.log"

if "$repo_root/scripts/restore.sh" --config-only \
  "$checksum_encrypted" "$postgres_dir/tampered.env" \
  >"$artifact_dir/backup-checksum-rejection.log" 2>&1; then
  echo "Restore accepted a checksum-tampered archive" >&2
  exit 1
fi
rg -q "checksum failed for config.env" "$artifact_dir/backup-checksum-rejection.log"

if FLEETLINE_RUNTIME=native "$repo_root/scripts/restore.sh" \
  "$unsafe_encrypted" \
  >"$artifact_dir/backup-path-rejection.log" 2>&1; then
  echo "Restore accepted an unsafe attachment path" >&2
  exit 1
fi
rg -q "unsafe attachment path" "$artifact_dir/backup-path-rejection.log"

"$repo_root/scripts/restore.sh" --config-only "$backup_archive" "$restore_config"
cmp --silent "$backup_config" "$restore_config"
[[ "$(stat -c '%a' "$restore_config")" == 600 ]] || {
  echo "Restored configuration permissions are not 0600" >&2
  exit 1
}

"$pg_bin/createdb" -h 127.0.0.1 -p "$postgres_port" fleetline_restore
export POSTGRES_DB=fleetline_restore
export MEDIA_ROOT="$restore_media"
FLEETLINE_RUNTIME=native "$repo_root/scripts/restore.sh" "$backup_archive"
snapshot_domain_state "$restored_state"
snapshot_media "$MEDIA_ROOT" "$restored_media_manifest"
cmp --silent "$source_state" "$restored_state" || {
  diff -u "$source_state" "$restored_state" || true
  echo "Restored domain state does not match the seeded source" >&2
  exit 1
}
cmp --silent "$source_media_manifest" "$restored_media_manifest" || {
  diff -u "$source_media_manifest" "$restored_media_manifest" || true
  echo "Restored attachment bytes do not match the source" >&2
  exit 1
}

if FLEETLINE_RUNTIME=native "$repo_root/scripts/restore.sh" "$backup_archive" \
  >"$artifact_dir/backup-nonempty-rejection.log" 2>&1; then
  echo "Restore accepted a non-empty target database" >&2
  exit 1
fi
rg -q "target database is not empty" "$artifact_dir/backup-nonempty-rejection.log"

echo "Starting the restored production application and worker..."
restore_app_port=$(free_port)
export CSRF_TRUSTED_ORIGINS="http://127.0.0.1:$restore_app_port"
export REQUIRE_WORKER=1
(
  cd backend
  exec "$python_bin" manage.py runworker --poll 0.1
) >"$artifact_dir/restored-worker.log" 2>&1 &
restore_worker_pid=$!
(
  cd backend
  exec "$repo_root/.venv/bin/gunicorn" fleetops.wsgi:application \
    --bind "127.0.0.1:$restore_app_port" --workers 2 --timeout 30 \
    --access-logfile - --error-logfile -
) >"$artifact_dir/restored-application.log" 2>&1 &
restore_app_pid=$!

restore_base_url="http://127.0.0.1:$restore_app_port"
restored_ready=0
for _ in $(seq 1 240); do
  if ! kill -0 "$restore_app_pid" 2>/dev/null || ! kill -0 "$restore_worker_pid" 2>/dev/null; then
    break
  fi
  if curl --silent --show-error --fail "$restore_base_url/health/ready" \
    --output "$artifact_dir/restored-health.json"; then
    if "$python_bin" - "$artifact_dir/restored-health.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
checks = payload.get("checks", {})
raise SystemExit(not (
    payload.get("status") == "ok"
    and checks.get("database") == checks.get("frontend") == checks.get("worker") == "ok"
))
PY
    then
      restored_ready=1
      break
    fi
  fi
  sleep 0.25
done
[[ "$restored_ready" == 1 ]] || {
  echo "Restored application did not become ready" >&2
  tail -n 100 "$artifact_dir/restored-application.log" >&2 || true
  tail -n 100 "$artifact_dir/restored-worker.log" >&2 || true
  exit 1
}

cookie_jar="$postgres_dir/restored-cookies.txt"
csrf_token=$(
  curl --silent --show-error --fail --cookie-jar "$cookie_jar" \
    "$restore_base_url/api/v1/auth/csrf/" |
    "$python_bin" -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])'
)
login_payload=$(
  "$python_bin" -c 'import json,os; print(json.dumps({"username": os.environ["E2E_USERNAME"], "password": os.environ["E2E_PASSWORD"]}))'
)
curl --silent --show-error --fail --cookie "$cookie_jar" --cookie-jar "$cookie_jar" \
  --header "Content-Type: application/json" --header "X-CSRFToken: $csrf_token" \
  --data "$login_payload" "$restore_base_url/api/v1/auth/login/" \
  --output "$artifact_dir/restored-login.json"
"$python_bin" - "$artifact_dir/restored-login.json" "$E2E_USERNAME" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
user = payload.get("user", {})
raise SystemExit(not (user.get("username") == sys.argv[2] and user.get("organization_id")))
PY

attachment_id=$(
  "$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["id"])' \
    "$proof_metadata"
)
curl --silent --show-error --fail --cookie "$cookie_jar" \
  "$restore_base_url/api/v1/attachments/$attachment_id/download/" \
  --output "$postgres_dir/restored-attachment.bin"
"$python_bin" - "$proof_metadata" "$postgres_dir/restored-attachment.bin" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

metadata = json.load(open(sys.argv[1], encoding="utf-8"))
actual = hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest()
raise SystemExit(actual != metadata["sha256"])
PY

document_id=$(
  "$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["document_id"])' \
    "$proof_metadata"
)
document_asset_id=$(
  "$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["document_asset_id"])' \
    "$proof_metadata"
)
curl --silent --show-error --fail --cookie "$cookie_jar" \
  "$restore_base_url/api/v1/documents/search/?q=torque&asset_id=$document_asset_id" \
  --output "$artifact_dir/restored-document-search.json"
"$python_bin" - "$proof_metadata" "$artifact_dir/restored-document-search.json" <<'PY'
import json
import sys

metadata = json.load(open(sys.argv[1], encoding="utf-8"))
result = json.load(open(sys.argv[2], encoding="utf-8"))
expected = metadata["document_id"]
raise SystemExit(not any(
    row.get("document_id") == expected and row.get("page_number") == 1
    for row in result.get("results", [])
))
PY
curl --silent --show-error --fail --cookie "$cookie_jar" \
  "$restore_base_url/api/v1/documents/$document_id/download/" \
  --output "$postgres_dir/restored-document.pdf"
"$python_bin" - "$proof_metadata" "$postgres_dir/restored-document.pdf" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

metadata = json.load(open(sys.argv[1], encoding="utf-8"))
actual = hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest()
raise SystemExit(actual != metadata["document_sha256"])
PY

financial_attachment_id=$(
  "$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["financial_attachment_id"])' \
    "$proof_metadata"
)
financial_part_id=$(
  "$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["financial_part_id"])' \
    "$proof_metadata"
)
curl --silent --show-error --fail --cookie "$cookie_jar" \
  "$restore_base_url/api/v1/attachments/?resource_type=part&resource_id=$financial_part_id" \
  --output "$artifact_dir/restored-financial-attachment.json"
"$python_bin" - "$proof_metadata" "$artifact_dir/restored-financial-attachment.json" <<'PY'
import json
import sys

metadata = json.load(open(sys.argv[1], encoding="utf-8"))
result = json.load(open(sys.argv[2], encoding="utf-8"))
expected = metadata["financial_attachment_id"]
raise SystemExit(not any(
    row.get("id") == expected and row.get("sensitivity") == "financial"
    for row in result.get("attachments", [])
))
PY
curl --silent --show-error --fail --cookie "$cookie_jar" \
  "$restore_base_url/api/v1/attachments/$financial_attachment_id/download/" \
  --output "$postgres_dir/restored-financial-attachment.bin"
"$python_bin" - "$proof_metadata" "$postgres_dir/restored-financial-attachment.bin" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

metadata = json.load(open(sys.argv[1], encoding="utf-8"))
actual = hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest()
raise SystemExit(actual != metadata["financial_attachment_sha256"])
PY

kill -TERM "$restore_app_pid" "$restore_worker_pid"
wait "$restore_app_pid" || true
wait "$restore_worker_pid" || true
restore_app_pid=
restore_worker_pid=
echo "Backup archive, rejection controls, domain state, documents, attachments, readiness, and login passed."

echo "Auditing dependencies and producing SBOMs..."
.venv/bin/pip-audit -r requirements.lock
npm audit --audit-level=high
mkdir -p artifacts
.venv/bin/cyclonedx-py requirements requirements.lock --output-format JSON --output-file artifacts/sbom-python.cdx.json
npm run sbom

fleetline_stop_postgres "$postgres_dir"

echo "Running complete production-stack browser E2E suite..."
PLAYWRIGHT_ARTIFACT_DIR="$artifact_dir/e2e" "$repo_root/scripts/test-e2e.sh"
echo "All verification gates passed."
