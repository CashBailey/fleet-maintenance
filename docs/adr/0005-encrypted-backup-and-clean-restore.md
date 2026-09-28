# ADR 0005: Encrypted backup unit and clean restore

- Status: accepted
- Date: 2026-09-03

Back up PostgreSQL, filesystem attachments, and the exact deployment
configuration as one checksummed unit, then encrypt that unit with GPG symmetric
AES-256. Backup and restore require a current-user-owned passphrase file with no
group or other permissions; the passphrase remains outside configuration,
archives, source control, command arguments, and logs.

Restore validates authentication, the fixed bundle manifest, streaming
checksums, configured compressed/expanded byte and member ceilings, and every
attachment path and type before writing target data. It restores only into a
clean database and empty media target; configuration recovery is a separate,
non-overwriting operation. Plaintext staging uses a private directory under an
operator-selected encrypted `TMPDIR` and is removed on exit.

This satisfies the research requirement for encrypted database, attachment, and
configuration backups plus a documented restore drill without adding a backup
service to the modular monolith. Operations must keep an off-host copy and the
recovery secret under separate approved custody, set policy RPO/RTO and
retention, size the limits from measured data, and prove recoverability through
the repository's clean-restore verification phase.
