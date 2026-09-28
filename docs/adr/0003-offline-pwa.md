# ADR 0003: Local-first field operations

- Status: accepted
- Date: 2026-09-03

The PWA caches its shell and minimum assigned-work/bootstrap data. Supported field actions are first written to IndexedDB with a stable operation ID and attachment bytes, then synchronized through the normal API. Background Sync is optional; foreground reconnect drives correctness.

Additive notes and attachments merge. Versioned work-state changes conflict visibly. Permissions, purchasing approvals, retirement, central adjustments, and destructive reversals stay online-only.

