# Fleetline

Fleetline is a purpose-built fleet maintenance and parts platform. It keeps assets, meters, preventive maintenance, inspections, defects, work orders, stock, purchasing, receipts, attachments, and audit history in one PostgreSQL-backed modular monolith. The responsive React client supports offline field work; AutoPi is an optional adapter and is not required for maintenance operations.

The authoritative product specification is [deep-research-report.md](deep-research-report.md). Architectural decisions are under [docs/adr](docs/adr).

## Requirements

- Python 3.12
- uv (only when regenerating the Python lock files)
- Node.js 22 and npm
- PostgreSQL 16 and its client tools
- Tesseract OCR with English trained data for native scanned-manual processing
- GnuPG 2.4 or another maintained GnuPG release with AES-256 symmetric encryption
- Google Chrome for the locked Playwright E2E project (`channel: chrome`)
- Docker Compose only when using the container deployment

`requirements.txt` and `requirements-dev.txt` declare the reviewed Python inputs;
`requirements.lock` and `requirements-dev.lock` pin their complete dependency
graphs with artifact hashes. JavaScript versions are pinned in `package.json`,
and `package-lock.json` locks the complete npm graph. See
[docs/dependency-policy.md](docs/dependency-policy.md) for regeneration and
release controls.

## Local setup

```bash
make install
cp .env.example .env
```

Set a unique `DJANGO_SECRET_KEY` and matching PostgreSQL credentials in `.env`. Non-debug startup fails before migrations or service launch when the key is missing, weak, or still a documented placeholder. As a PostgreSQL administrator, create the role/database, then export the application configuration and initialize it:

```bash
createuser --pwprompt fleetline
createdb --owner=fleetline fleetline
set -a
. ./.env
set +a
mkdir -p backend/media
export MEDIA_ROOT="$PWD/backend/media"
make migrate
make seed
make run
```

The `MEDIA_ROOT` override above gives a native development process a writable
attachment directory. Use the durable service path from `.env` in deployment.
For scanned/manual-image OCR on a native worker, also install Tesseract plus
English trained data and ensure `tesseract --version` works for the service
account. The checked-in container image supplies this runtime automatically;
see [the document-library deployment notes](docs/truck-document-library.md).

Open `http://127.0.0.1:8088`. `make seed` is deterministic and intended for development and E2E environments, not an existing production database.

The seed creates the following role accounts in `Gator Fleet Services`; all use
`E2E_PASSWORD` or its default, `DemoPass123!`:

| Role | Username |
|---|---|
| Driver | `driver@example.com` |
| Technician | `technician@example.com` |
| Shop supervisor | `supervisor@example.com` |
| Parts clerk | `parts.clerk@example.com` |
| Purchasing manager | `purchasing.manager@example.com` |
| Purchasing approver | `purchasing.approver@example.com` |
| Fleet manager | `fleet.manager@example.com` |
| Management | `management@example.com` |
| System administrator | `system.admin@example.com` |
| Integration administrator | `integration.admin@example.com` |

The two administrator fixtures require a current TOTP code generated from the
development-only seed secret `JBSWY3DPEHPK3PXP`. To print the current code:

```bash
cd backend
../.venv/bin/python -c 'from core.security import totp; print(totp("JBSWY3DPEHPK3PXP"))'
```

`E2E_USERNAME` can add another fleet-manager login. The seeded AutoPi device uses
`E2E_DEVICE_TOKEN`, defaulting to `e2e-autopi-device-token`. Override these values
before `make seed` when a test needs different credentials. Never reuse any seed
credential, secret, token, or seeded organization in production.

Stable workflow fixtures include assets `TRK-012`, `TRK-007`, and `TRL-003`, work
orders `WO-DEMO-1001` and `WO-DEMO-0998`, overdue plan `5,000 Mile PM B`, part
`FIL-1001` with alias `WIX-51734`, and partially received PO `PO-DEMO-2401`.
That PO is submitted by `purchasing.manager@example.com` and independently
approved by `purchasing.approver@example.com`; both accounts have the purchasing
manager role.
Organization `other-fleet`, user `other.manager@example.com`, and asset
`OTHER-001` exist only for tenant-isolation tests.

Useful endpoints:

- Liveness: `http://127.0.0.1:8088/health/live`
- Readiness, including PostgreSQL, worker, and frontend: `http://127.0.0.1:8088/health/ready`
- OpenAPI document: `http://127.0.0.1:8088/api/schema/`
- Interactive API reference: `http://127.0.0.1:8088/api/docs/`

## Common commands

| Command | Purpose |
|---|---|
| `make build` | Build the production frontend and collect static files |
| `make lock` | Regenerate the complete hashed Python production and development locks |
| `make run` | Build, migrate, start the worker, and run Gunicorn on `127.0.0.1:8088` |
| `make migrate` | Apply all database migrations |
| `make seed` | Load deterministic demonstration data |
| `make test` | Run backend tests with the coverage gate |
| `make test-e2e` | Provision an isolated PostgreSQL database and run the real-browser suite |
| `make verify` | Run the complete clean verification pipeline |
| `make sbom` | Write Python and npm CycloneDX SBOMs under `artifacts/` |
| `backend/manage.py replay_telematics <file> --device <id> [--dry-run]` | Replay saved AutoPi messages through the real ingest path; samples live in `backend/integrations/samples/`. Sample files use `{{organizationId}}`/`{{deviceId}}` placeholders that are filled from `--device`; real recorded files are replayed untouched |

The E2E and verification commands return nonzero on failure. Failure artifacts are written under `artifacts/`; see [docs/e2e-coverage.md](docs/e2e-coverage.md) for the workflow matrix.

## Container deployment

```bash
cp .env.example .env
# Set strong production values in .env.
docker compose up --build -d
docker compose exec -e FLEETLINE_ALLOW_DEMO_SEED=1 app python manage.py seed_demo  # isolated demonstration environments only
curl --fail http://127.0.0.1:8080/health/ready
```

The Compose stack runs PostgreSQL, a one-shot migration container, the web application, the database-backed worker, and Caddy. It persists the database and attachments in separate named volumes. See [docs/deployment.md](docs/deployment.md) before exposing it outside a trusted host.
Production installations must use the documented `bootstrap_organization`
management command, not `seed_demo`, to create the first organization and
MFA-protected system administrator.

## Operations and development references

- [API use and contracts](docs/api.md)
- [GatorHub integration and PM cutover](docs/gatorhub-integration.md)
- [GatorHub personnel and assignment boundary](docs/gatorhub-personnel-integration.md)
- [Truck document library and grounded retrieval](docs/truck-document-library.md)
- [Parts physical-location catalog](docs/parts-location-catalog.md)
- [Deployment and upgrades](docs/deployment.md)
- [Backup and clean restore drills](docs/backup-restore.md)
- [Validation assumptions](docs/validation-assumptions.md)
- [Prior art: what open-source fleet, CMMS and inventory projects do](docs/prior-art.md)
- [E2E coverage matrix](docs/e2e-coverage.md)

Do not edit meter, stock, posted receipt, submitted inspection, or audit facts in the database. Use the corresponding correction, reversal, void-and-replace, or reopen workflow so the original record remains auditable.
