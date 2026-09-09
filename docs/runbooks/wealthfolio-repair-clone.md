# Wealthfolio repair-clone safeguards

A full clean rebuild and whole-bundle cutover are not supported operations.
Use [bounded repair promotion](bounded-repair-promotion.md) for a reviewed
receipt repair and [incremental finance](incremental-finance.md) for scoped
ingestion. Do not reconstruct the ledger or app-owned state through a generic
projector.

## Isolated, authenticated clone

The retained [repair clone Compose definition](../../deploy/wealthfolio-rebuild/compose.yml)
uses a digest-pinned Wealthfolio image, loopback binding, and required
authentication. Keep its configuration and database outside the checkout.
The [target checks](../../importers/rebuild/projector.py) require:

- The exact `wealthfolio-clean-rebuild-v1` candidate marker.
- An explicit loopback port other than production port `8088`.
- Wealthfolio `3.7.0` and database path `/data/wealthfolio-rebuild.db`.
- An authenticated instance fingerprint matching the operator's expected
  fingerprint.

These identifiers remain stable for bounded-repair compatibility; they do not
authorize a full rebuild. The [receipt repair CLI](../../importers/rebuild/receipt_repair_cli.py)
checks the URL and marker before reading credentials or opening transport.
The bounded plan still requires its own scope, identity, and authorization
checks.

## Backups and recovery evidence

The shared [backup and runtime helpers](../../importers/rebuild/cutover.py)
support bounded promotion and scoped incremental writes:

- Backup creation is serialized within the process, waits out an existing
  same-second filename, and requires exactly one new inventory entry.
  Keep other backup writers stopped during the operation: the upstream
  [backup implementation](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/crates/storage-sqlite/src/db/mod.rs#L613-L715)
  uses one-second filenames.
- Downloads are restricted to the private data root, matched against the
  backup inventory, and verified for SQLite integrity, required tables,
  schema migration, size, and SHA-256.
- A restore uses a fresh writable copy, never the sealed evidence file.
  Its receipt binds the original bytes, schema, size, and destination path.
  The receipt describes the copy **before startup**; bounded promotion
  separately checks the restored database and authenticated API state.
- Docker control rechecks the configured Compose project, service, container,
  immutable container ID, and image ID before starting or stopping that
  specific container. It never stops an entire Compose project.
- Recovery state is flushed before publication. Preserve the original
  backup, signed plan, promotion journal, rejected candidate, and rollback
  evidence; use the [bounded recovery procedure](bounded-repair-promotion.md)
  instead of an ad hoc database replacement.

For continued receipt repair, follow
[repeated receipt repair](repeated-receipt-repair.md). Existing stage-copy
receipts retain their verification format. No full-bundle preparation,
synthetic rehearsal engine, or authority-cycle scheduler is required.
