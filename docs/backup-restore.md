# Backup and restore

The backup unit is the PostgreSQL database, attachment files, and deployment configuration together. `scripts/backup.sh` creates a versioned, internally checksummed archive, encrypts it symmetrically with GPG AES-256, and writes it with mode `0600`. `scripts/restore.sh` authenticates/decrypts and validates the archive, then restores only into an empty database and empty attachment target. Both commands fail closed unless `FLEETLINE_BACKUP_PASSPHRASE_FILE` names a nonempty, nonsymlink, current-user-owned file with no group or other permissions.

The encrypted archive contains secrets from `.env`. Restrict archive access, keep at least one logically or physically separate copy, and retain the passphrase in an approved secret manager with separate recovery escrow. Never place the passphrase in `.env`, the archive directory, source control, command arguments, or job logs. Losing it makes the backup unrecoverable. The research paper's provisional target is at most 24 hours of data exposure and same-business-day recovery; leadership must approve the actual RPO, RTO, retention, encryption, key custody/rotation, and offsite policy.

Create a strong passphrase file once on an access-controlled encrypted filesystem:

```bash
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(64))' > /secure-secrets/fleetline-backup-passphrase
export FLEETLINE_BACKUP_PASSPHRASE_FILE=/secure-secrets/fleetline-backup-passphrase
```

The scripts keep plaintext staging in a private mode-`0700` directory under `TMPDIR` and remove it on exit. Set `TMPDIR` to a sufficiently sized encrypted local filesystem; deletion alone cannot guarantee erasure from unencrypted or copy-on-write storage.

Restore is fail-closed and bounded before data reaches the target database or attachment directory. The defaults accept an encrypted/decrypted archive up to 100 GiB, at most 100 GiB of top-level bundle content, a 50 GiB database dump, a 50 GiB compressed media archive, one million media members, and 500 GiB of expanded media. Set these positive-integer byte/member variables higher only from measured fleet data and available staging/target capacity:

- `FLEETLINE_RESTORE_MAX_ARCHIVE_BYTES`
- `FLEETLINE_RESTORE_MAX_BUNDLE_BYTES`
- `FLEETLINE_RESTORE_MAX_DATABASE_BYTES`
- `FLEETLINE_RESTORE_MAX_MEDIA_ARCHIVE_BYTES`
- `FLEETLINE_RESTORE_MAX_MEDIA_MEMBERS`
- `FLEETLINE_RESTORE_MAX_MEDIA_BYTES`

The restore streams checksum calculation and archive extraction, rejects duplicate or escaping paths, and refuses links, devices, sparse files, and other special entries. A limit failure leaves the live database and media untouched; increase a limit only after authenticating the archive and confirming its recorded inventory and expected growth.

## Native backup

Export the same environment used by the running service. `pg_dump` must be from PostgreSQL 16 or a compatible newer client, and `MEDIA_ROOT` must identify the live attachment directory.

```bash
set -a
. ./.env
set +a
./scripts/backup.sh /secure-backups/fleetline-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.gpg
```

The script refuses to overwrite an existing archive or proceed without the configuration file, database dump, or attachment directory. Keep the printed archive path in the backup job log and copy the archive to the approved separate store.

If configuration is stored outside the repository, set
`FLEETLINE_CONFIG_FILE=/absolute/path/to/fleetline.env`; Compose mode uses that
file for interpolation and includes that exact file in the archive.

## Compose backup

The `db` and `app` services must be running. Compose mode reads PostgreSQL from the database container and attachments from the application volume.

```bash
FLEETLINE_RUNTIME=compose ./scripts/backup.sh /secure-backups/fleetline-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.gpg
```

Database records are dumped before attachment copying. Because normal attachments are append-oriented, this prevents a database snapshot from referring to a later upload that was not copied. Use a brief write-maintenance window when the organization requires a strict point-in-time image across both stores.

## Restore configuration

Configuration recovery is deliberately separate so a restore cannot silently replace live secrets or hostnames. The target must not already exist.

```bash
./scripts/restore.sh --config-only /secure-backups/fleetline-20260903T120000Z.tar.gz.gpg .env.restored
test -s .env.restored
```

Review hostnames, origins, filesystem paths, and secrets with an approved secure editor; do not print the file into shared logs. Move the reviewed file into the service's secret-management path and keep it mode `0600`.

## Native clean restore

1. Install the same Fleetline release and PostgreSQL major version used for the backup.
2. Create a new empty database and an empty or absent attachment directory.
3. Export the target connection variables and `MEDIA_ROOT`.
4. Run the restore as the Fleetline service account, or set the restored directory ownership to that account before startup.
5. Start the worker and web process, then verify readiness and login.

```bash
createdb --host=127.0.0.1 --port=5432 --username=fleetline fleetline_restore
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432
export POSTGRES_DB=fleetline_restore POSTGRES_USER=fleetline
export POSTGRES_PASSWORD='target-database-password'
export MEDIA_ROOT=/var/lib/fleetline-restore/media
./scripts/restore.sh /secure-backups/fleetline-20260903T120000Z.tar.gz.gpg
make build
make run
```

With `make run` supervised and still running, verify from another shell:

```bash
curl --fail http://127.0.0.1:8088/health/ready
```

Do not run migrations before restoring: the target database must be empty. Restore the backup with its matching application release first, then follow the normal upgrade procedure if moving to a newer release.

## Compose clean restore drill

Use an isolated Compose project name so the drill cannot address production volumes. Recover and review configuration before starting the isolated database.

```bash
./scripts/restore.sh --config-only /secure-backups/fleetline-20260903T120000Z.tar.gz.gpg .env.restored
cp .env.restored .env
export COMPOSE_PROJECT_NAME=fleetline_restore_drill
export ALLOWED_HOSTS=localhost,127.0.0.1
export CSRF_TRUSTED_ORIGINS=http://127.0.0.1:8080
export FLEETLINE_SITE_ADDRESS=localhost COOKIE_SECURE=0
export SECURE_SSL_REDIRECT=0 SECURE_HSTS_SECONDS=0 TRUST_PROXY_HEADERS=0
docker compose build
docker compose up -d db
docker compose up -d --no-deps app
FLEETLINE_RUNTIME=compose ./scripts/restore.sh /secure-backups/fleetline-20260903T120000Z.tar.gz.gpg
docker compose up -d worker proxy
curl --fail http://127.0.0.1:8080/health/ready
```

After recording evidence and confirming the project name, remove only the isolated drill resources:

```bash
docker compose -p fleetline_restore_drill down -v
```

Never use `down -v` against the production Compose project.

## Restore acceptance checks

A restore drill is complete only after all of these succeed:

- archive checksum and format validation;
- PostgreSQL schema presence and migration history;
- application and worker readiness;
- login with a designated recovery-test account;
- counts for organizations, users, assets, work orders, parts, stock transactions, POs, receipts, audit events, technical documents, document applicability records, and indexed document pages against backup evidence;
- attachment download and SHA-256 match for sampled files, including a technical manual and an appropriately authorized financial attachment;
- technical-document full-text search returns the expected source document and page citation after restore;
- ledger-derived inventory balances and current meter projections;
- organization-isolation check;
- application restart with records and files still present.

Record the archive identifier, backup time, restore start/end time, software version, PostgreSQL version, operator, target, checks performed, and any reconciliation required. A backup is not considered proven until a clean restore drill has passed.
