#!/usr/bin/env bash
set -euo pipefail

umask 077
repo_root=$(cd "$(dirname "$0")/.." && pwd)
runtime=${FLEETLINE_RUNTIME:-native}
config_file=${FLEETLINE_CONFIG_FILE:-$repo_root/.env}
passphrase_file=${FLEETLINE_BACKUP_PASSPHRASE_FILE:-}
archive=${1:-"$repo_root/backups/fleetline-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.gpg"}

die() {
  printf 'backup: %s\n' "$*" >&2
  exit 1
}

[[ "$runtime" == native || "$runtime" == compose ]] || die "FLEETLINE_RUNTIME must be native or compose"
[[ -f "$config_file" ]] || die "configuration file not found: $config_file"
[[ -n "$passphrase_file" ]] || die "FLEETLINE_BACKUP_PASSPHRASE_FILE is required"
[[ -f "$passphrase_file" && ! -L "$passphrase_file" && -s "$passphrase_file" ]] || \
  die "passphrase file must be a non-empty regular file, not a symlink"
[[ "$(stat -c '%u' -- "$passphrase_file")" == "$(id -u)" ]] || \
  die "passphrase file must be owned by the current user"
passphrase_mode=$(stat -c '%a' -- "$passphrase_file")
(( (8#$passphrase_mode & 077) == 0 )) || die "passphrase file must not grant group or other access"
command -v gpg >/dev/null 2>&1 || die "gpg is required"
[[ ! -e "$archive" ]] || die "refusing to overwrite: $archive"

archive_parent=$(dirname "$archive")
mkdir -p "$archive_parent"
archive_dir=$(cd "$archive_parent" && pwd)
archive="$archive_dir/$(basename "$archive")"
stage=$(mktemp -d "${TMPDIR:-/tmp}/fleetline-backup.XXXXXX")
chmod 700 "$stage"
partial=$(mktemp "$archive_dir/.fleetline-backup.XXXXXX")
cleanup() {
  rm -rf -- "$stage"
  rm -f -- "$partial"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

cp -- "$config_file" "$stage/config.env"

if [[ "$runtime" == compose ]]; then
  command -v docker >/dev/null 2>&1 || die "docker is required for compose mode"
  postgres_db=${POSTGRES_DB:-fleetline}
  postgres_user=${POSTGRES_USER:-fleetline}
  (
    cd "$repo_root"
    docker compose --env-file "$config_file" exec -T db pg_dump \
      --format=custom --no-owner --no-privileges \
      --username="$postgres_user" "$postgres_db"
  ) >"$stage/database.dump"
  postgres_version=$(cd "$repo_root" && docker compose --env-file "$config_file" exec -T db pg_dump --version | tr -d '\r')
  mkdir "$stage/media"
  (
    cd "$repo_root"
    docker compose --env-file "$config_file" cp app:/data/media/. "$stage/media" >/dev/null
  )
else
  command -v pg_dump >/dev/null 2>&1 || die "pg_dump is required for native mode"
  postgres_host=${POSTGRES_HOST:-127.0.0.1}
  postgres_port=${POSTGRES_PORT:-5432}
  postgres_db=${POSTGRES_DB:-fleetline}
  postgres_user=${POSTGRES_USER:-fleetline}
  export PGPASSWORD=${POSTGRES_PASSWORD:-}
  pg_dump --format=custom --no-owner --no-privileges \
    --host="$postgres_host" --port="$postgres_port" \
    --username="$postgres_user" --dbname="$postgres_db" \
    --file="$stage/database.dump"
  postgres_version=$(pg_dump --version)
  media_root=${MEDIA_ROOT:-$repo_root/backend/media}
  [[ -d "$media_root" ]] || die "attachment directory not found: $media_root"
  mkdir "$stage/media"
  cp -a -- "$media_root/." "$stage/media/"
fi

tar -czf "$stage/media.tar.gz" -C "$stage/media" .
rm -rf -- "${stage:?}/media"
application_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$repo_root/package.json")
cat >"$stage/metadata.txt" <<EOF
format_version=1
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
runtime=$runtime
database=$postgres_db
application_version=$application_version
postgres_version=$postgres_version
encryption=GPG-AES256
EOF
(
  cd "$stage"
  sha256sum database.dump media.tar.gz config.env metadata.txt >checksums.sha256
  tar -czf - database.dump media.tar.gz config.env metadata.txt checksums.sha256 |
    gpg --no-options --batch --yes --no-tty --pinentry-mode loopback --no-symkey-cache \
      --passphrase-file "$passphrase_file" --symmetric --cipher-algo AES256 --output "$partial"
)
chmod 600 "$partial"
mv -- "$partial" "$archive"
printf '%s\n' "$archive"
