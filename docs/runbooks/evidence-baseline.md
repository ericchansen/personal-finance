# Evidence baseline

Create a baseline before forensic matching, migration, or remediation. The command
is read-only: after authentication it permits HTTP `GET` requests and the
documented read-only `POST /activities/search` query only. It cannot call activity,
account, settings, category, budget, goal, holdings, or backup mutations.

All output is private and must stay under an explicitly supplied external
`--data-dir`. The command rejects output inside this repository. It inventories
source files by relative path, size, kind, and SHA-256 without copying their
contents. Credential files are classified and omitted without reading their
contents or metadata. Live SQLite databases, journals, shared-memory files, and
sockets are also omitted because they cannot be sealed while their owning process
is running; their transient appearance or disappearance does not invalidate
sealed evidence, while explicitly stored database backups remain hashed evidence.
Filesystem failures name only a data-root-relative entry. API responses are
recursively sanitized before persistence: credential-bearing fields are replaced
with `<redacted>`, while host-local database and log paths are stored only as
SHA-256 digests. The environment fingerprint still binds the actual database path
in memory without publishing it.

## Create a baseline

1. Confirm the external finance-data tree is complete and read-only source
   downloads are in their intended private locations.
2. Set `WEALTHFOLIO_PASSWORD` in the process environment, or place the password in
   `<finance-data>/wealthfolio/ADMIN-PASSWORD.txt`. Never pass a password on the
   command line.
3. Against staging, use the authenticated staging URL:

   ```powershell
   python -m importers.audit.cli create `
     --data-dir <finance-data> `
     --base-url http://127.0.0.1:<staging-port>
   ```

4. Against production, use the authenticated production URL:

   ```powershell
   python -m importers.audit.cli create `
     --data-dir <finance-data> `
     --base-url http://127.0.0.1:8088
   ```

The command prints only the private output path and aggregate counts. It writes
`audit/baselines/current.json`, which points to an immutable publication under
`audit/baselines/publications/<sha256>/`. Each publication contains a manifest
and separate JSON snapshots for accounts, activities and source metadata,
transfers, taxonomies, assignments, splits, spending state, goals and plans,
assets and profiles, quote history, exchange rates, allocation configuration,
holdings snapshots, settings, health, and backup inventory metadata.
Unsupported API domains remain visible as typed capability gaps.

## Verify a baseline

Run verification before any downstream comparison or remediation:

```powershell
python -m importers.audit.cli verify --data-dir <finance-data>
```

Verification resolves the current pointer, validates the manifest and domain
schemas, re-hashes every included evidence file and domain snapshot, checks record
counts, and detects missing or newly added evidence. Any discrepancy fails closed.
Old immutable publications remain available for comparison and recovery.

## Database backups are separate

Baseline creation lists backup metadata through Wealthfolio's authenticated read
API, but it does **not** create or download a database backup. Before a later
mutation workflow, the operator must separately create, download, and verify the
required backup according to that workflow's change controls. A successful
baseline is evidence, not a substitute for a restorable database backup.
