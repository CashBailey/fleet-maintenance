# GOAL: Ship component tracking in Fleetline

Complete all 9 tasks in `docs/superpowers/plans/2026-09-05-component-tracking.md`.
Read it and its spec (`.../specs/2026-09-05-component-tracking-design.md`) in full before
writing code. Where plan and code disagree, THE CODE WINS — correct the plan.

## What you are building
Each major part (engine, transmission, reefer unit, APU, axle, aftertreatment) becomes a
`Component` keyed by serial. Each time one goes on or off a truck writes one
`ComponentInstallation` period row: install half written once, removal half filled once with
a required reason, meters frozen by value at both ends. A trigger rejects all other UPDATEs
and every DELETE; a partial unique index enforces one open installation per component.

## The 9 tasks
1. Models, migration, legacy backfill, write-once trigger
2. Meter snapshot helpers + `install_component` (with technician/manager rules)
3. `remove_component` + closing open installations on retirement
4. `WorkOrderTask.component`
5. Four API endpoints, asset history, export, schema test
6. Cross-app: serials in `global_search`; close snapshot freezes asset meters
7. Frontend: asset Components panel; legacy serial fields out of the forms
8. Frontend: work-order swap panel, `/components/:id` page, timeline
9. ADR 0011, docs, the Playwright spec

## Method
One task at a time, 1 -> 9. Never start a task until the previous one's tests, ruff and mypy
are green. Every step: write the failing test FIRST; run it and watch it fail for the reason
the plan states (a test passing on its first run is testing nothing, rewrite it); write
the minimum code to pass; re-run, then run the whole app suite for regressions. Tick each
`- [ ]` checkbox as you go.

## Environment
- Root: `/home/gatorhub/fleet_maint_track`. NOT a git repo, never run `git`.
- Always set `DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789`
- Tests: `cd backend && ../.venv/bin/python manage.py test [label] -v 2`
- PostgreSQL: scratch instance on port 54329, env at `scratchpad/env.sh`. If down, use
  `scripts/postgres-test-lib.sh` (`fleetline_provision_postgres` / `_start_postgres`).
- Lint: `.venv/bin/ruff check backend/`, `.venv/bin/mypy backend/` (secret must be set).
- Frontend: `npm run build && npx tsc --noEmit && npx eslint frontend/src/App.tsx`
- Full gate: `make verify`

## Non-negotiables
- ADR 0002: durable facts are never edited in place. `ComponentInstallation` is append-only
  apart from the one removal update the trigger permits.
- ADR 0004: nothing here may auto-create a work order.
- ADR 0007/0011: legacy `specs.equipment.*.serial_number` keys stay in the JSON, never
  rewritten. `Component` is the new source of truth.
- Tenant scoping returns 404, never 403.
- YAGNI: add no field, status or config knob the plan does not name.
- No placeholders: no `TODO`, no stub test bodies, no "same as Task N".

## Known traps (each has bitten once)
- NEVER claim Fleetline lacks something without grepping first. It usually exists under a
  different name.
- Do not invent field or enum names. Read the model, then write the test.
- `remove_component`'s `save(update_fields=[...])` is EXACTLY the set the trigger allows;
  adding one makes the trigger reject the save.
- The trigger's two `ROW()` lists must lead with `id`, matching
  `maintenance/migrations/0004_inspection_immutability_guards.py`.
- Run the backfill BEFORE creating the trigger — it closes retired rows.
- `ComponentInstallation.ordering` needs its tie-break: the backfill gives one asset
  several rows with identical `installed_at` on day one.
- `assets/test_retirement.py` must stay green. If it breaks, your retirement change sits at
  the wrong point in `change_asset_status`.

## Done when
All 9 tasks ticked, `make verify` green (tests, ruff, mypy, frontend build, eslint,
Playwright incl. `tests/e2e/component-tracking.spec.ts`), plan checklist passing.

If blocked, stop and report. Do not guess past a blocker.
