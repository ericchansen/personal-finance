# PostgreSQL finance shadow authority

PostgreSQL stores
immutable source observations, provenance, review decisions, derived canonical
state, and application journals. The legacy shadow loader never fetches
SimpleFIN, mutates source artifacts, or writes Wealthfolio. The separate
[incremental worker](../../docs/runbooks/incremental-finance.md) can use the same
store for explicitly activated account scopes; its own release/instance/scope
ownership gate controls app writes. Wealthfolio remains the user-facing ledger.
Database setup and migrations do not authorize application writes or repairs.

Diagnostic generations do not certify the
household ledger or establish that an automatic application writer is deployed.
Report collection, normalized evidence, identity selection, and application
projection as separate states.

The original `shadow_authority_metadata` flags describe the shadow loader's
boundary. Use the incremental scope, qualification, and operation-history views
for an activated worker's actual state; do not interpret those legacy flags as
an inventory of every app writer.

## Isolation and secrets

The production project, disposable integration project, and restore-rehearsal
project have distinct fixed project, container, network, and volume names. The
production database defaults to loopback port `55432`; the integration database
uses loopback port `55433` and `tmpfs`. Never reuse those project names for
another stack.

Copy `.env.example` to the ignored `.env`. Create the referenced password,
backup, and optional TLS files under the external data root. Passwords enter
containers only as Compose secrets; no password or DSN belongs in this repository
or on a command line.

```powershell
docker compose -f deploy/postgres/compose.yml up -d shadow-db
docker compose -f deploy/postgres/compose.yml --profile tools run --rm migrate
docker compose -f deploy/postgres/compose.yml --profile tools run --rm provision-logins
```

The bootstrap login is reserved for migrations and role provisioning. Routine
ingest must use `finance_shadow_loader`, a non-superuser member of
`finance_shadow_ingest`. Put its DSN in the external file
`postgres-shadow/secrets/ingest-dsn.txt`. The read-only
`finance_shadow_agent` login can select `finance_read` views only and defaults
to read-only transactions. Dumps use the separate
`finance_shadow_backup_login`, which can read only `finance` and
`finance_read`; it cannot read global catalogs such as `pg_authid` and has no
write or administrative privileges. Dumps explicitly include only those two
schemas.

For private-network access, supply certificates and layer the TLS override:

```powershell
docker compose -f deploy/postgres/compose.yml `
  -f deploy/postgres/compose.tls.yml up -d shadow-db
```

The overlay copies key material into a PostgreSQL-owned runtime volume with
mode `0600` and installs an HBA that rejects every non-TLS TCP connection.
TLS 1.2 is the minimum. Client DSNs must use `sslmode=verify-full` and a trusted
root certificate. Keep the base deployment on loopback unless the host firewall
and private network are reviewed.

## Migrations

Migrations are immutable, LF-normalized, ordered, and forward-only. The
migration runner closes a durable writer gate, then holds the same
database-scoped advisory lock used by every repository mutation across checksum
validation and every pending migration. Changed history or a mismatched
environment marker fails closed. Add a new numbered migration; never edit an
applied file.

Before migrating a non-empty database, create and verify a fresh backup, then
pass its basename through `POSTGRES_MIGRATION_BACKUP`. The gate remains closed
if backup verification or migration fails. Never roll back by editing or
reversing evidence migrations. Restore the last verified dump into a new
instance, validate it, and switch clients only after review. See the
[disaster-recovery runbook](../../docs/runbooks/postgresql-shadow-disaster-recovery.md).

A database still below migration `0007` has neither the backup role nor state
manifest function. Stop every legacy writer, set
`FINANCE_SHADOW_LEGACY_QUIESCED=confirmed`, run the `backup-legacy` tool with
the bootstrap secret through both `compose.yml` and
`compose.legacy-upgrade.yml`, and pass that verified basename to `migrate`
using the same override. The override replaces the new named volume with the
explicit legacy candidate data directory; never point it at another stack.
After migration and verification, restore into the new named-volume instance.
Clear the quiescence marker immediately afterward. This compatibility path is
only for crossing the writer-gate migration; normal backups must use the
dedicated backup login.

Database dumps exclude cluster roles. The restore runner first provisions only
the NOLOGIN role prerequisites, then migrates, then provisions login secrets and
table grants. A successful migration releases only its owned writer gate. A
failure leaves that gate closed for explicit recovery and returns a failure;
neither a missing role nor a failed cleanup is reported as a working migration.

## Sealed shadow ingest

The loader verifies the external data root, all migration checksums, database
instance marker, least-privilege role, immutable triggers, source hashes,
canonical publication, baseline publication, forensic publication, parser
hashes, and source capability state before planning. Credential files are never
treated as evidence.

```powershell
python -m finance_store.cli --data-dir <external-data-root> plan
```

The command writes a content-addressed private plan under
`postgres-shadow/plans/` and prints only counts and hashes. A blocked
institution or baseline capability makes `ready` false. Apply re-reads every
source and requires the exact sealed plan, database state, data-root hash, and
two exact environment interlocks:

```powershell
$env:FINANCE_SHADOW_ENVIRONMENT = 'shadow-authority-v1'
$env:FINANCE_SHADOW_MUTATIONS_ENABLED = 'apply-reviewed-plan'
try {
  python -m finance_store.cli --data-dir <external-data-root> apply `
    --plan postgres-shadow/plans/<plan-hash>.json `
    --plan-hash <plan-hash> `
    --backup-basename <required-when-database-is-non-empty>
} finally {
  Remove-Item Env:FINANCE_SHADOW_MUTATIONS_ENABLED
  Remove-Item Env:FINANCE_SHADOW_ENVIRONMENT
}
```

Apply holds a global advisory lock and commits all source batches, lineage
metadata, the sealed plan, and the success event in one transaction. It never
opens a Wealthfolio client. Reapplying to a non-empty database requires a dump
whose digest and observation count bind the starting state.

## Source and reconciliation policy

Adapters reuse the existing parsers and verifiers for:

- SimpleFIN v1/v2 immutable snapshots;
- mapped OFX, QFX, and supported CSV extracts;
- Monarch legacy transactions and balance history;
- private facts, mappings, and decisions;
- the canonical normalized publication;
- verified Wealthfolio baseline and forensic duplicate publications.

Every file has a blob hash, parser hash, source version, immutable record
observations, and a hashed external locator. SimpleFIN v2 connection IDs and v1
organization scope are part of account identity. The collector now seals the
actual request window and snapshot hash in an immutable sidecar; older snapshots
without one remain usable but report an explicit missing-window gap and cannot
claim vanished-row coverage. Source identity is account-scoped;
pending-to-posted and changed versions append evidence; out-of-order sightings
cannot rewind canonical state; a posted row missing from a later overlap window
is retained and opens an issue.

The current identity policy resolves exact account-scoped provider identity and
source-authority matches with proven coverage, occurrence capacity, and exact
normalized descriptions. Generic prefixes and merchant contact/store tokens are
review evidence, not transaction identity. Explicitly bound paired bank exports
can restore a source's truncated description before matching; their raw evidence
and stable source IDs are preserved. Category and `T00`/`T12` writer signatures
are provenance only.

Opposite-signed activity on different accounts is classified separately as
transfer evidence and is never merged as a duplicate. Every policy generation,
claim, edge, canonical event, automatic decision, and human override is
append-only. See
[Canonical transaction identity](../../docs/canonical-transaction-identity.md)
for the complete invariants and confidence equation.

Migration 0018 adds explicit, qualified stable event addresses and revision
history. It neither backfills old diagnostic generations nor activates accepted
financial authority. Identity persistence participates in the same global
backup/migration writer gate as ordinary ingest. See
[accepted event identity](../../docs/accepted-event-identity.md) for continuity,
omission retention, unresolved transitions, and source/currency qualifications.

## Backup, retention, and restore

Backups are custom-format dumps with sidecar SHA-256 and JSON manifests in the
external backup directory. A backup holds the global writer lock and binds the
dump to the database instance plus deterministic per-table row digests.
Verification checks all three files, compares the live state binding, and runs
`pg_restore --list`. Retention counts only complete verified sets and reports
insufficient or stale coverage; it never deletes backups.

```powershell
docker compose -f deploy/postgres/compose.yml --profile ops run --rm backup
docker compose -f deploy/postgres/compose.yml --profile ops run --rm `
  -e BACKUP_BASENAME=<basename> verify-backup
docker compose -f deploy/postgres/compose.yml --profile ops run --rm verify-retention
docker compose -f deploy/postgres/compose.rehearsal.yml up `
  --abort-on-container-exit --exit-code-from restore-rehearsal restore-rehearsal
docker compose -f deploy/postgres/compose.rehearsal.yml down
```

The rehearsal project restores only into a separate `tmpfs` database, rebuilds
roles and ACLs independently of the restored migration ledger, compares the
full state binding, and connects as the loader, read-only agent, and backup
login.

## Drift, health, and export

```powershell
python -m finance_store.cli --data-dir <external-data-root> drift
python -m finance_store.cli --data-dir <external-data-root> schema
python -m finance_store.cli --data-dir <external-data-root> status
python -m finance_store.cli --data-dir <external-data-root> health --format prometheus
python -m finance_store.cli --data-dir <external-data-root> export `
  postgres-shadow/exports/state.json
python -m finance_store.identity_shadow --data-dir <external-data-root> run
python -m finance_store.identity_shadow --data-dir <external-data-root> verify
```

Drift compares immutable inputs, PostgreSQL evidence/canonical state, the
verified Wealthfolio baseline, and forensic lineage groups. Detailed reports
stay under `postgres-shadow/reports/`; stdout contains counts and hashes only.
The deterministic JSON export is a disposable, self-hashing replica.
Parquet publication is intentionally absent until an independently verified
publisher can prove that PostgreSQL remains authoritative.
The identity shadow report is content-addressed under
`postgres-shadow/reports/canonical-identity/` and contains only aggregate counts,
hashes, feature vectors, and cardinality proofs. It does not mutate Wealthfolio.
Run it *after* the schema-v5 canonical publication: the report replays the scope
that publication sealed, which is the only input carrying source provenance. With
no current publication it falls back to the Wealthfolio projection rows as an
explicit pre-publication diagnostic and reports the
`canonical-publication-scope-required` blocker, which stops an authority apply.

## Scheduling

`scheduled-run` holds a non-overlap file lock and always seals a plan before
apply. `run-shadow.ps1` adds a verified pre-apply backup. Choose a minute that is
not shared with institution collectors or backups; spread household jobs across
the hour rather than using `:00`.

This shadow schedule is not a Wealthfolio writer. Routine app updates use the
separate [scoped incremental worker](../../docs/runbooks/incremental-finance.md)
and its [immutable release launcher](../../docs/runbooks/incremental-release.md).
Do not enable overlapping legacy app writers. Historical repair uses the
separately authorized [bounded repair workflow](../../docs/runbooks/bounded-repair-promotion.md),
not an automatic whole-estate rebuild.

Preview setup only; do not omit `-WhatIf` without explicit authorization:

```powershell
deploy/postgres/install-shadow-task.ps1 -Minute 17 -WhatIf
deploy/postgres/install-shadow-task.ps1 -Minute 17 -Apply -WhatIf
```

No scheduled task is installed by repository setup or migration.

## Disposable verification

Use an external synthetic password file:

```powershell
$env:POSTGRES_TEST_PASSWORD_FILE = '<external-synthetic-secret-file>'
docker compose -f deploy/postgres/compose.integration.yml up `
  --abort-on-container-exit --exit-code-from roundtrip-test roundtrip-test
docker compose -f deploy/postgres/compose.integration.yml down --volumes
```

The integration project applies migrations twice, checks immutable controls and
roles/views, and performs a dump/restore round trip without touching production.
