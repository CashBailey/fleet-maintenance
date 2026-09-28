#!/usr/bin/env bash
set -euo pipefail

umask 077
repo_root=$(cd "$(dirname "$0")/.." && pwd)

die() {
  printf 'restore: %s\n' "$*" >&2
  exit 1
}

[[ $# -ge 1 ]] || die "usage: $0 ARCHIVE | $0 --config-only ARCHIVE TARGET"
config_only=0
if [[ "$1" == --config-only ]]; then
  [[ $# -eq 3 ]] || die "usage: $0 --config-only ARCHIVE TARGET"
  config_only=1
  archive=$2
  config_target=$3
else
  [[ $# -eq 1 ]] || die "usage: $0 ARCHIVE"
  archive=$1
fi
[[ -f "$archive" ]] || die "backup not found: $archive"
passphrase_file=${FLEETLINE_BACKUP_PASSPHRASE_FILE:-}
max_archive_bytes=${FLEETLINE_RESTORE_MAX_ARCHIVE_BYTES:-107374182400}
max_bundle_bytes=${FLEETLINE_RESTORE_MAX_BUNDLE_BYTES:-107374182400}
max_database_bytes=${FLEETLINE_RESTORE_MAX_DATABASE_BYTES:-53687091200}
max_media_archive_bytes=${FLEETLINE_RESTORE_MAX_MEDIA_ARCHIVE_BYTES:-53687091200}
max_media_members=${FLEETLINE_RESTORE_MAX_MEDIA_MEMBERS:-1000000}
max_media_bytes=${FLEETLINE_RESTORE_MAX_MEDIA_BYTES:-536870912000}

require_limit() {
  local name=$1 value=$2
  [[ "$value" =~ ^[1-9][0-9]*$ && ${#value} -le 18 ]] || \
    die "$name must be a positive integer no longer than 18 digits"
}

require_limit FLEETLINE_RESTORE_MAX_ARCHIVE_BYTES "$max_archive_bytes"
require_limit FLEETLINE_RESTORE_MAX_BUNDLE_BYTES "$max_bundle_bytes"
require_limit FLEETLINE_RESTORE_MAX_DATABASE_BYTES "$max_database_bytes"
require_limit FLEETLINE_RESTORE_MAX_MEDIA_ARCHIVE_BYTES "$max_media_archive_bytes"
require_limit FLEETLINE_RESTORE_MAX_MEDIA_MEMBERS "$max_media_members"
require_limit FLEETLINE_RESTORE_MAX_MEDIA_BYTES "$max_media_bytes"
archive_size=$(stat -c '%s' -- "$archive")
(( archive_size <= max_archive_bytes )) || die "encrypted archive exceeds configured size limit"
[[ -n "$passphrase_file" ]] || die "FLEETLINE_BACKUP_PASSPHRASE_FILE is required"
[[ -f "$passphrase_file" && ! -L "$passphrase_file" && -s "$passphrase_file" ]] || \
  die "passphrase file must be a non-empty regular file, not a symlink"
[[ "$(stat -c '%u' -- "$passphrase_file")" == "$(id -u)" ]] || \
  die "passphrase file must be owned by the current user"
passphrase_mode=$(stat -c '%a' -- "$passphrase_file")
(( (8#$passphrase_mode & 077) == 0 )) || die "passphrase file must not grant group or other access"
command -v gpg >/dev/null 2>&1 || die "gpg is required"

stage=$(mktemp -d "${TMPDIR:-/tmp}/fleetline-restore.XXXXXX")
chmod 700 "$stage"
cleanup() {
  rm -rf -- "$stage"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

decrypted="$stage/archive.tar.gz"
if ! gpg --no-options --batch --yes --no-tty --pinentry-mode loopback --no-symkey-cache \
  --passphrase-file "$passphrase_file" --decrypt "$archive" | \
  head -c "$((max_archive_bytes + 1))" >"$decrypted"; then
  if [[ -f "$decrypted" ]] && (( $(stat -c '%s' -- "$decrypted") > max_archive_bytes )); then
    rm -f -- "$decrypted"
    die "decrypted archive exceeds configured size limit"
  fi
  rm -f -- "$decrypted"
  die "backup decryption failed"
fi
(( $(stat -c '%s' -- "$decrypted") <= max_archive_bytes )) || \
  die "decrypted archive exceeds configured size limit"

python3 - "$decrypted" "$stage" \
  "$max_bundle_bytes" "$max_database_bytes" "$max_media_archive_bytes" <<'PY'
import hashlib
import pathlib
import re
import shutil
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
stage = pathlib.Path(sys.argv[2])
max_bundle_bytes = int(sys.argv[3])
max_database_bytes = int(sys.argv[4])
max_media_archive_bytes = int(sys.argv[5])
expected = {"database.dump", "media.tar.gz", "config.env", "metadata.txt", "checksums.sha256"}
limits = {
    "database.dump": max_database_bytes,
    "media.tar.gz": max_media_archive_bytes,
    "config.env": 1024 * 1024,
    "metadata.txt": 64 * 1024,
    "checksums.sha256": 64 * 1024,
}
seen = set()
bundle_bytes = 0
with tarfile.open(archive, "r|gz") as bundle:
    for member in bundle:
        name = member.name.removeprefix("./")
        if name not in expected or name in seen:
            raise SystemExit("restore: invalid backup contents")
        if member.issparse() or not member.isfile():
            raise SystemExit("restore: outer bundle contains an unsupported special entry")
        if member.size < 0 or member.size > limits[name]:
            raise SystemExit(f"restore: {name} exceeds configured size limit")
        bundle_bytes += member.size
        if bundle_bytes > max_bundle_bytes:
            raise SystemExit("restore: outer bundle exceeds configured size limit")
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit("restore: invalid backup contents")
        with source, (stage / name).open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        seen.add(name)
if seen != expected:
    raise SystemExit("restore: invalid backup contents")

metadata = (stage / "metadata.txt").read_text(encoding="utf-8")
if not re.search(r"(?m)^format_version=1$", metadata):
    raise SystemExit("restore: unsupported backup format")

checksums = {}
for line in (stage / "checksums.sha256").read_text(encoding="ascii").splitlines():
    digest, marker, name = line.partition("  ")
    if not marker or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("restore: malformed checksum manifest")
    checksums[name] = digest
checked = {"database.dump", "media.tar.gz", "config.env", "metadata.txt"}
if len(checksums) != len(checked) or set(checksums) != checked:
    raise SystemExit("restore: invalid checksum manifest")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


for name, expected_digest in checksums.items():
    actual = sha256_file(stage / name)
    if actual != expected_digest:
        raise SystemExit(f"restore: checksum failed for {name}")
PY
rm -f -- "$decrypted"

if [[ "$config_only" -eq 1 ]]; then
  [[ ! -e "$config_target" ]] || die "refusing to overwrite: $config_target"
  mkdir -p "$(dirname "$config_target")"
  cp -- "$stage/config.env" "$config_target"
  chmod 600 "$config_target"
  printf '%s\n' "$config_target"
  exit 0
fi

media_stage="$stage/media"
mkdir "$media_stage"
python3 - "$stage/media.tar.gz" "$media_stage" \
  "$max_media_members" "$max_media_bytes" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2]).resolve()
max_members = int(sys.argv[3])
max_bytes = int(sys.argv[4])
member_count = 0
expanded_bytes = 0
seen = set()
with tarfile.open(archive, "r|gz") as bundle:
    for member in bundle:
        member_count += 1
        if member_count > max_members:
            raise SystemExit("restore: attachment archive exceeds configured member limit")
        path = (target / member.name).resolve()
        if target not in path.parents and path != target:
            raise SystemExit("restore: unsafe attachment path")
        relative = path.relative_to(target).as_posix() or "."
        if relative in seen:
            raise SystemExit("restore: attachment archive contains a duplicate path")
        seen.add(relative)
        if member.issparse():
            raise SystemExit("restore: attachment archive contains a sparse entry")
        if member.issym() or member.islnk():
            raise SystemExit("restore: attachment archive contains an unsupported link or device")
        if member.ischr() or member.isblk():
            raise SystemExit("restore: attachment archive contains an unsupported link or device")
        if not member.isfile() and not member.isdir():
            raise SystemExit("restore: attachment archive contains an unsupported special entry")
        if member.isfile():
            if member.size < 0:
                raise SystemExit("restore: attachment archive contains an invalid size")
            expanded_bytes += member.size
            if expanded_bytes > max_bytes:
                raise SystemExit("restore: attachment archive exceeds configured expanded size limit")
        bundle.extract(member, target, filter="data")
PY

runtime=${FLEETLINE_RUNTIME:-native}
[[ "$runtime" == native || "$runtime" == compose ]] || die "FLEETLINE_RUNTIME must be native or compose"
postgres_db=${POSTGRES_DB:-fleetline}
postgres_user=${POSTGRES_USER:-fleetline}
empty_query="SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema'))"

if [[ "$runtime" == compose ]]; then
  command -v docker >/dev/null 2>&1 || die "docker is required for compose mode"
  db_empty=$(
    cd "$repo_root"
    docker compose exec -T db psql -XAt --username="$postgres_user" --dbname="$postgres_db" --command="$empty_query"
  )
  [[ "$db_empty" == t ]] || die "target database is not empty; restore into a clean database"
  media_entry=$(
    cd "$repo_root"
    docker compose exec -T app sh -c 'find /data/media -mindepth 1 -print -quit' 2>/dev/null || true
  )
  [[ -z "$media_entry" ]] || die "target attachment volume is not empty"
  (
    cd "$repo_root"
    app_uid=$(docker compose exec -T app id -u | tr -d '\r')
    app_gid=$(docker compose exec -T app id -g | tr -d '\r')
    docker compose exec -T db pg_restore \
      --exit-on-error --single-transaction --no-owner --no-privileges \
      --username="$postgres_user" --dbname="$postgres_db"
    docker compose cp "$media_stage/." app:/data/media/ >/dev/null
    docker compose exec -T --user 0 app chown -R "$app_uid:$app_gid" /data/media
  ) <"$stage/database.dump"
  verify=$(
    cd "$repo_root"
    docker compose exec -T db psql -XAt --username="$postgres_user" --dbname="$postgres_db" \
      --command="SELECT to_regclass('public.django_migrations') IS NOT NULL"
  )
else
  command -v psql >/dev/null 2>&1 || die "psql is required for native mode"
  command -v pg_restore >/dev/null 2>&1 || die "pg_restore is required for native mode"
  postgres_host=${POSTGRES_HOST:-127.0.0.1}
  postgres_port=${POSTGRES_PORT:-5432}
  export PGPASSWORD=${POSTGRES_PASSWORD:-}
  psql_args=(--host="$postgres_host" --port="$postgres_port" --username="$postgres_user" --dbname="$postgres_db" -XAt -v ON_ERROR_STOP=1)
  db_empty=$(psql "${psql_args[@]}" --command="$empty_query")
  [[ "$db_empty" == t ]] || die "target database is not empty; restore into a clean database"
  media_root=${MEDIA_ROOT:-$repo_root/backend/media}
  if [[ -d "$media_root" ]]; then
    media_entry=$(find "$media_root" -mindepth 1 -print -quit)
    [[ -z "$media_entry" ]] || die "target attachment directory is not empty: $media_root"
  fi
  pg_restore --exit-on-error --single-transaction --no-owner --no-privileges \
    --host="$postgres_host" --port="$postgres_port" \
    --username="$postgres_user" --dbname="$postgres_db" "$stage/database.dump"
  mkdir -p "$media_root"
  cp -a -- "$media_stage/." "$media_root/"
  verify=$(psql "${psql_args[@]}" --command="SELECT to_regclass('public.django_migrations') IS NOT NULL")
fi

[[ "$verify" == t ]] || die "restore completed but schema verification failed"
printf 'restore complete: database=%s runtime=%s\n' "$postgres_db" "$runtime"
