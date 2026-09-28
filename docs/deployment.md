# Deployment and operations

Fleetline deploys as one application image used by the web process, migration job, and database-backed worker, plus PostgreSQL 16 and durable attachment storage. The checked-in Compose stack is the supported single-host baseline. Kubernetes, Redis, and a separate queue are intentionally not required.

## Production configuration

Copy `.env.example` to an owner-readable secret file and set at least:

| Variable | Production guidance |
|---|---|
| `DJANGO_SECRET_KEY` | Unique random value of at least 50 characters; rotate only with a planned session reset |
| `DJANGO_DEBUG` | `0`; never expose Django debug output to users |
| `POSTGRES_*` | Dedicated database, user, and strong password |
| `DB_CONN_MAX_AGE` | Database connection reuse in seconds; tune below infrastructure idle timeouts |
| `ALLOWED_HOSTS` | Comma-separated exact application hostnames; Compose uses the first entry for its internal readiness request, so keep that entry concrete rather than `*` or a leading-dot wildcard |
| `CSRF_TRUSTED_ORIGINS` | Exact HTTPS origins, including scheme |
| `COOKIE_SECURE` | `1` when users connect over HTTPS |
| `SESSION_COOKIE_AGE` | Maximum session lifetime in seconds; default is eight hours |
| `LOGIN_ATTEMPT_LIMIT` | Failed password/TOTP attempts allowed per account and client within the window; default `5` |
| `LOGIN_ATTEMPT_WINDOW_SECONDS` | Failure-count window; default `300` seconds |
| `LOGIN_LOCKOUT_SECONDS` | Temporary lockout after the configured failure limit; default `900` seconds |
| `SECURE_SSL_REDIRECT` | `1` after the public HTTPS endpoint is working |
| `SECURE_HSTS_SECONDS` | Start at `0`; raise gradually only after HTTPS validation, then use the approved long-lived value |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | Keep `0`; opt in only after every subdomain is HTTPS-only |
| `SECURE_HSTS_PRELOAD` | Keep `0`; opt in only after the domain meets browser preload requirements and removal risk is accepted |
| `TRUST_PROXY_HEADERS` | `1` only when requests can arrive solely through the trusted reverse proxy |
| `FLEETLINE_SITE_ADDRESS` | Public DNS hostname used by Caddy, without a URL scheme; normally the first `ALLOWED_HOSTS` entry |
| `MEDIA_ROOT` | Durable, backed-up attachment path |
| `TIME_ZONE` | Organization operating timezone |
| `OFFLINE_CACHE_HOURS` | Maximum bootstrap lifetime on a field device |
| `ATTACHMENT_MAX_BYTES` | Upload boundary; default is 10 MiB |
| `DOCUMENT_MAX_BYTES` | Separate technical-manual PDF ceiling; default is 100 MiB and never raises the ordinary attachment limit |
| `DOCUMENT_MAX_PAGES` | PDF page ceiling for one manual; default `500`, maximum `1000` |
| `DOCUMENT_COMMAND_TIMEOUT_SECONDS` | Maximum duration of one local OCR command; default `30`, maximum `120` |
| `DOCUMENT_PROCESS_TIMEOUT_SECONDS` | Total extraction deadline for one document; default `180`, maximum `900` |
| `DOCUMENT_MAX_PAGE_TEXT_BYTES` | Per-page retained text ceiling; default 512 KiB, maximum 2 MiB |
| `DOCUMENT_MAX_TOTAL_TEXT_BYTES` | Total retained text ceiling per manual; default 16 MiB, maximum 128 MiB |
| `DOCUMENT_MAX_OCR_IMAGE_BYTES` | Per-page rendered PNG ceiling; default 16 MiB, maximum 64 MiB |
| `DOCUMENT_MAX_OCR_TSV_BYTES` | Tesseract TSV output ceiling; default 2 MiB, maximum 8 MiB |
| `DOCUMENT_MAX_OCR_PIXELS` | Per-page PDFium rendering ceiling; default 20 million pixels, maximum 64 million |
| `TESSDATA_PREFIX` | English trained-data directory for the local Tesseract worker; Compose default is `/usr/share/tesseract-ocr/5/tessdata` |
| `STAGED_ATTACHMENT_MAX_COUNT` | Maximum recent unlinked offline uploads per user; default `25` |
| `STAGED_ATTACHMENT_MAX_BYTES` | Maximum recent unlinked offline-upload bytes per user; default 50 MiB |
| `STAGED_ATTACHMENT_TTL_HOURS` | Staging quota window and orphan-cleanup age; default `24` hours |
| `PO_APPROVAL_THRESHOLD` | Organization approval threshold; validate before use |
| `INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD` | Gross count-variance value requiring independent approval; safe default `0.00` |
| `METER_MAX_MILES_PER_HOUR` | Meter plausibility threshold; validate per fleet |
| `METER_MAX_ENGINE_HOURS_PER_HOUR` | Engine-hour plausibility threshold; default `1.25`, validate per fleet |
| `METER_FUTURE_TOLERANCE_SECONDS` | External meter clock-skew allowance; default `300` seconds |
| `WEBHOOK_ALLOW_HTTP` | Keep `0` outside isolated development |
| `WEBHOOK_ALLOW_PRIVATE_NETWORKS` | Keep `0` unless an approved internal receiver requires it |
| `REQUIRE_WORKER` | Keep `1`; readiness should fail when background processing is unavailable |
| `PM_RECALCULATION_INTERVAL_SECONDS` | Worker interval for date-based PM due-state recalculation; default `3600` |
| `FLEETLINE_RESTORE_MAX_*` | Fail-closed archive, dump, compressed/expanded media, and member-count ceilings; keep defaults until measured backup size and recovery capacity justify a reviewed change |

Generate a secret without writing it to shell history:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
```

Fleetline refuses to initialize non-debug settings unless `DJANGO_SECRET_KEY` is
explicitly set, is at least 50 characters with at least five unique characters,
and is not a known development or documentation placeholder. The native
`scripts/run-prod.sh` entrypoint checks this before building assets or applying
migrations; container startup applies the same settings validation even if a
debug flag is accidentally enabled.

Restrict `.env` to the service account:

```bash
chmod 600 .env
```

The checked-in Caddy configuration binds plaintext port 8080 to host loopback for local verification only and serves `FLEETLINE_SITE_ADDRESS` with Caddy-managed HTTPS. For public deployment, point that hostname at the host, allow ports 80/443, make it the first `ALLOWED_HOSTS` entry, set `CSRF_TRUSTED_ORIGINS=https://<hostname>`, and use `COOKIE_SECURE=1`, `SECURE_SSL_REDIRECT=1`, and `TRUST_PROXY_HEADERS=1`. Begin with all HSTS settings disabled. Increase `SECURE_HSTS_SECONDS` only after HTTPS, redirects, and health checks have been validated. That value never opts in subdomains or browser preload: enable `SECURE_HSTS_INCLUDE_SUBDOMAINS` and `SECURE_HSTS_PRELOAD` separately only after reviewing each broader, hard-to-reverse scope. Do not expose PostgreSQL publicly.

For a local HTTP-only Compose run, retain the `.env.example` overrides: `FLEETLINE_SITE_ADDRESS=localhost`, `COOKIE_SECURE=0`, `SECURE_SSL_REDIRECT=0`, all HSTS settings `0`, and `TRUST_PROXY_HEADERS=0`. These values are not suitable for public exposure. Compose restarts the database, application, worker, and proxy unless an operator stops them. PostgreSQL, attachments, and Caddy certificate/configuration state persist in named volumes across container replacement; include all of those volumes in host backup and recovery procedures.

## Compose deployment

```bash
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8080/health/live
curl --fail http://127.0.0.1:8080/health/ready
```

Readiness is successful only when the application can query PostgreSQL, the worker heartbeat is current, and the production frontend exists. The `migrate` service must complete successfully before `app` and `worker` are considered ready.

Never run `seed_demo` against production. Bootstrap the first production
organization as described below, require MFA for privileged roles, and give
routine users only the roles they need.

## First production organization

The bootstrap command atomically creates one organization, its initial location,
all standard roles, and one MFA-protected system administrator. It refuses an
existing organization slug, username, or provisioning-output path. Prepare the
password in an owner-only file without putting it in shell history:

```bash
install -d -m 700 /secure-provisioning
umask 077
python3 -c 'import getpass, pathlib; pathlib.Path("/secure-provisioning/admin-password").write_text(getpass.getpass("Initial administrator password: ") + "\n", encoding="utf-8")'
chmod 600 /secure-provisioning/admin-password
```

For a native deployment with its application environment loaded:

```bash
cd backend
../.venv/bin/python manage.py bootstrap_organization \
  --organization-name "Example Fleet" \
  --organization-slug example-fleet \
  --location-name "Main Shop" \
  --location-code MAIN \
  --admin-username fleet-admin@example.com \
  --admin-first-name Fleet \
  --admin-last-name Administrator \
  --password-file /secure-provisioning/admin-password \
  --provisioning-output /secure-provisioning/fleet-admin-mfa.json
```

For Compose, copy the input into the already-running application container, run
the same command there, and copy the one-time output back to protected storage:

```bash
docker compose cp /secure-provisioning/admin-password app:/tmp/fleetline-admin-password
app_uid=$(docker compose exec -T app id -u | tr -d '\r')
app_gid=$(docker compose exec -T app id -g | tr -d '\r')
docker compose exec --user 0 app chown "$app_uid:$app_gid" /tmp/fleetline-admin-password
docker compose exec --user 0 app chmod 600 /tmp/fleetline-admin-password
docker compose exec app python manage.py bootstrap_organization \
  --organization-name "Example Fleet" \
  --organization-slug example-fleet \
  --location-name "Main Shop" \
  --location-code MAIN \
  --admin-username fleet-admin@example.com \
  --admin-first-name Fleet \
  --admin-last-name Administrator \
  --password-file /tmp/fleetline-admin-password \
  --provisioning-output /tmp/fleetline-admin-mfa.json
docker compose cp app:/tmp/fleetline-admin-mfa.json /secure-provisioning/fleet-admin-mfa.json
chmod 600 /secure-provisioning/fleet-admin-mfa.json
docker compose exec app rm -f /tmp/fleetline-admin-password /tmp/fleetline-admin-mfa.json
```

Transfer `fleet-admin-mfa.json` to the named administrator through an approved
secure channel. They must enroll its `otpauth_uri`, verify a login with a current
one-time code, and then delete every copy of the password and provisioning files.
The application never needs either file again; the MFA secret is stored with the
administrator record.

## Native process deployment

Install locked dependencies with `make install`, provision PostgreSQL 16 and a durable `MEDIA_ROOT`, and export the secret configuration to the service environment. Then run migrations and static collection once per release:

```bash
make build
make migrate
```

Run these as separate supervised processes from `backend/`:

```bash
../.venv/bin/gunicorn fleetops.wsgi:application --bind 127.0.0.1:8088 --workers 2 --access-logfile - --error-logfile -
../.venv/bin/python manage.py runworker
```

Place an HTTPS reverse proxy in front of Gunicorn. Give the service account read access to code and configuration, write access only to `MEDIA_ROOT` and required runtime directories, and database privileges only on the Fleetline database.

## Technical-document OCR runtime

The supported container image installs `tesseract-ocr` plus English trained data.
The worker first uses `pypdf` for selectable text, then renders image-only pages
with bundled PDFium and invokes the local Tesseract binary. Therefore a Compose
deployment supports scanned PDFs without Poppler, Ghostscript, or an external
LLM/OCR service.

For a native installation, install a maintained Tesseract engine and English
trained data from the operating-system package source before starting the
worker. Ensure `tesseract --version` works as the Fleetline service account. If
the trained data is outside Tesseract’s standard path, set `TESSDATA_PREFIX` in
the worker service environment. `pypdfium2` and Pillow are installed from the
locked Python dependencies; do not replace the renderer with a GPL/AGPL utility
without the dependency-policy review.

Treat scanned PDFs as untrusted input: the worker has a read-only source-media
mount, a bounded writable `/tmp`, and container memory/CPU limits. The existing
outbox worker also delivers approved outbound webhooks, so apply any egress
policy with an explicit allowed webhook delivery route rather than assuming
Compose makes OCR network-isolated. The extractor itself makes no network calls.
The application limits page count, pixels, rendered image bytes, OCR child CPU
time, stdout/stderr file sizes, extracted text, and total document time. A
missing local OCR executable produces `ocr_unavailable` with a visible reason;
it is not a successful scan index. Fleetline records a manager’s
`security_review_reference` before extraction but does not itself perform
malware scanning, so integrate the organization-approved scanner into that
review step.

## Staged attachment cleanup

Offline clients upload attachment bytes before the owning operation synchronizes.
Fleetline caps recent unlinked staging per user, but cleanup is an operator job,
not part of the request or worker process. Run one scheduled instance at least
daily, first validating its scope in a new installation:

```bash
cd backend
../.venv/bin/python manage.py purge_staged_attachments --dry-run
../.venv/bin/python manage.py purge_staged_attachments --older-than-hours 24 --limit 1000
```

For Compose, use:

```bash
docker compose exec app python manage.py purge_staged_attachments --dry-run
docker compose exec app python manage.py purge_staged_attachments --older-than-hours 24 --limit 1000
```

Repeat bounded production runs until the command reports no remaining expired
rows. It deletes only expired, unlinked staging rows and their bytes; linked
document versions remain immutable. Alert on file-removal errors and reconcile
the named storage path before rerunning.

## Upgrade procedure

1. Read the release notes and retain the currently running image/source and configuration.
2. Run `make verify` on the exact candidate release.
3. Create and verify a database, attachment, and configuration backup as documented in [backup-restore.md](backup-restore.md).
4. Restore that backup in an isolated environment and run the candidate migrations there.
5. Schedule the production change, build the new image, and apply migrations once.
6. Start the web and worker processes, then require successful liveness and readiness checks.
7. Exercise login, asset lookup, work-order lookup, and attachment download before ending the change window.

For Compose:

```bash
export FLEETLINE_BACKUP_PASSPHRASE_FILE=/run/secrets/fleetline-backup-passphrase
FLEETLINE_RUNTIME=compose ./scripts/backup.sh /secure-backups/fleetline-pre-upgrade.tar.gz.gpg
docker compose build --pull
docker compose run --rm migrate
docker compose up -d app worker proxy
curl --fail http://127.0.0.1:8080/health/ready
```

Application rollback uses the retained prior image/source. Database rollback is a restore into a clean PostgreSQL database; do not reverse a partially applied schema by hand. Any records accepted after the backup need an explicit reconciliation plan before restoring older data.

## Monitoring and logs

Monitor:

- `/health/live` for process liveness;
- `/health/ready` for database, worker, and frontend readiness;
- web and worker stderr/stdout;
- PostgreSQL health, storage, connections, and backup age;
- attachment-volume capacity;
- outbox retry/dead-delivery counts;
- device last-seen, ingest rejection, suspect-meter, and duplicate rates.

Compose logs are available with:

```bash
docker compose logs --since=30m app worker db migrate proxy
```

Alert on repeated readiness failure, a missing worker heartbeat, backup failure or age beyond policy, storage exhaustion, and sustained ingest/webhook errors. Keep application and worker clocks synchronized.

## Security maintenance

- Patch the operating system, PostgreSQL, Python, Node build environment, and dependencies on a defined cadence.
- Run `make sbom`, `.venv/bin/pip-audit -r requirements.lock`, and `npm audit` for each release candidate.
- Rotate leaked database, Django, device, and webhook secrets; revoke affected users or tokens immediately.
- Keep attachment and backup storage private. The application allowlists upload types and size, but deployments exposed to untrusted uploaders should add malware scanning before release.
- Test cross-organization authorization and privileged negative cases on every release.
- Perform a clean restore drill on the operational schedule and record duration and evidence.
