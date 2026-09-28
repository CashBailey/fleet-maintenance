# ADR 0002: Append facts and idempotent mutations

- Status: accepted
- Date: 2026-09-03

Meter readings, posted stock transactions, posted receipt lines, submitted inspections, raw telematics messages, work-order close snapshots, attachment document versions, and audit events are append-oriented facts. Corrections reference the original and append a superseding or compensating fact. Current balances, current document versions, and meter values are projections.

Transactional mutation endpoints require a client operation ID or `Idempotency-Key`. The server records principal, route, request fingerprint, status, and response. PostgreSQL row locks serialize final-unit stock operations.

A stock count that exceeds its captured value threshold remains ledger-neutral until a different authorized user approves it. Approval locks the affected balances and appends count-adjustment transactions; direct balance editing remains prohibited.
