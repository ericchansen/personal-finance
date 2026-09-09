# Bounded receipt-repair promotion

This is a **clone-first, cash-ledger-only release path**, not the whole-rebuild
cutover and not a deployed canonical writer. Implementing it grants no financial
authorization. The old pilot, schema-1 plans, and v4 identity evidence cannot be
promoted. Rebuild the plan with current schema-2/v5 evidence first.

Keep every request, diff, signature, backup, restore database, execution journal,
and authorization under the external private data directory. Never put them in
this public repository. Examples below use environment variables and generic
filenames; they contain no household data.

## What the contract proves

- It validates the existing repair policy without reinterpreting match decisions.
  The original receipt and recovery hash seals, downloaded recovery backup,
  complete pre/post ledger fingerprints, and actual conserved cash-ledger effect
  must agree. A `verified: true` statement is not evidence.
- It binds the exact plan file, embedded policy/evidence hash, original canonical
  manifest and source plan/receipt files, normalized evidence through the original
  canonical data files and hashed identity scope, explicitly declared raw artifacts
  and source-authority interval files, independently reviewed evidence files,
  final raw SQLite diff, release commit, production identity, and backup bytes.
- Both the original and candidate backups need real isolated restore proofs:
  copy, start, authenticate, verify the receipt-repair state, stop, and compare
  all restored table contents and DDL. Any supported startup environmental changes
  are retained as full before/after rows in the proof, then independently reviewed.
  The copies and proof databases remain available for inspection. A proof for a
  different image/instance/backup fails.
- All tables, columns, SQLite value types, blobs, implicit row identities, schema objects and persistent
  application pragmas participate. Settings, rules, goals, notes, splits, unknown
  extension tables, assets, quote history and manual holdings are not ignored.
- Production is stopped **before** the decisive complete prestate comparison.
  Changes to any table since preparation abort promotion and restart the
  untouched original. A physical-hash sandwich around the logical read plus a
  Windows mandatory file-sharing fence closes the final read-to-rename window.
  Sidecars or other readers/writers prevent renaming.
- Exact original/incoming/rejected slots and a signed, fsynced phase journal make
  interrupted operations distinguishable from success. Caught post-swap failures
  restore the original **only after proving that the current database contains
  no unreviewed app work**, retaining the rejected candidate. Otherwise the current
  database and original slot remain intact for explicit manual recovery.

Economic identity, coverage, categorization and compensation truth still need
independent source review. Hashes establish bindings, not those financial facts.

The pinned server does not guarantee a checkpointed SQLite shutdown. After its
owner is confirmed stopped, the controller checkpoints committed WAL with
SQLite's own `wal_checkpoint(TRUNCATE)` and proves identical complete logical
state before/after. It never deletes sidecars to make a guard pass. Busy writers,
hot rollback journals, changed logical state, or remaining sidecars fail closed.
The decisive stopped-prestate comparison and mandatory rename fence run after
this maintenance step. Rollback applies the same preservation rule.

The offline structural validator compares the cash account's REST-style
fingerprint and the complete activity count. Unrelated investment amounts may
be rounded in the REST representation; their exact SQLite rows remain protected
by the full database diff instead of reimplementing Wealthfolio's conversion.
Authenticated original/candidate restore proofs still require the complete
global REST ledger fingerprints from the plan.

## Supported scope and conservative blockers

The structural diff validator permits only:

1. The plan's exact activity deletions, with their source and rollback fingerprints.
   In the pinned model SQLite `notes` is API `comment`, including the imported
   merchant description. It must match the reviewed rollback `comment` exactly;
   nonempty descriptions are not themselves user notes. User-modified state,
   changed/unreviewed comment text, grouped-event state, unknown populated columns,
   and dependent split/event/unknown linked rows remain blockers.
2. The compensation row's planned amount/type and its `updated_at` field.
   Every other compensation field and every survivor field remains identical.
3. Exact snapshotted source assignments removed with the source, and the plan's
   assignment upserts, preserving existing assignment identity/creation metadata
   and all fields outside category, weight, provenance and update timestamp.
   Snapshot comparison performs the pinned RFC3339-to-naive-UTC API conversion
   without truncating any of the nine fractional-second digits. Raw SQLite
   timestamps remain exact in preservation diffs; API equivalence does not
   authorize rewriting unrelated rows.
4. Explicitly reviewed calculated, empty-position cash snapshots **only for the
   repaired account**, plus added/updated daily account valuations for that
   same-currency cash account. Daily valuations cannot be deleted or acquire an
   investment market value. Full before/after rows are in the signed diff; neither
   manual nor broker/imported snapshots qualify. Position and lot tables remain exact.
5. New matching `sync_outbox` events, not changes to existing history. The pinned
   entity names are `activity` and `activity_taxonomy_assignment`. Payload is
   plaintext JSON: `{"id": "<deleted-id>"}` for deletion, or the exact snake-case
   SQLite row for create/update. One new pending event per affected entity is
   supported. Every event needs its exact `sync_entity_metadata` counterpart:
   matching last event ID, client timestamp and operation; existing `last_seq`
   remains unchanged and a new metadata row starts at zero. Unmatched events,
   changed prior outbox rows, or unrelated metadata changes fail.
   The observed native snake-case create/update path is supported explicitly.
   Camel-case payloads and another writer's UPDATE-on-insert convention are not
   silently substituted for it.
6. Provider-only market refresh: `quote_sync_state.last_synced_at`/`updated_at`,
   and an automatic quote's ingestion `created_at`. Price changes are restricted
   to `close`, `adjclose`, and `volume` on the latest automatic day for that asset,
   no more than seven days before the plan's generation date. Manual quotes,
   annotated automatic prices, historical prices, row additions/deletions,
   source/day/currency changes and unrecognized providers are prohibited.
   Historical automatic ingestion timestamps may change, never historical prices.

Every allowed field change, including technical timestamps and derived values,
is bound by the reviewed diff hash. There is no general “automatic data” exemption.
The candidate backup and its genuinely restored database form **two exact
reviewed reference states**, not per-row combinations of arbitrary alternatives.
After production restart, financial values and preserved tables must match one
of these entire states. Only specifically listed row/field timestamps may differ;
they must be monotonic and fall within the execution window (two seconds of
clock/serialization tolerance). The runtime timestamp policy is derived from
observed repair/clone-prestate/restore diffs and bound by the independent review.
Actual changed timestamps and the matched reference hash are written into the
signed execution receipt.

An unobserved latest price is **not** automatically authorized. It also blocks
automatic rollback: without a write fence its provenance cannot safely be assumed.
Unrelated accounts' DAILY additions, changed assets, sync engine state,
manual holdings, unknown columns and user-owned work remain blockers. A reviewer
cannot whitelist an arbitrary table using a JSON flag. The final stopped
production prestate check still compares **everything exactly**; these
environmental rules never excuse production edits since the original backup.

Other limitations: no virtual-table/changed keyless-table support, performance
account receipts, volume mounts, remote Docker hosts, non-Windows production
execution, alternative origins, or automatic journal-slot reuse. The initial
production origin is exactly `http://127.0.0.1:8088`; isolated restores must use
another explicit loopback port. Both require configured password authentication.
No global recalculation or upstream app patch is used.

## Inputs and signatures

Use a clean, committed release checkout. Preparation and execution both compare
`git rev-parse HEAD` and reject a dirty tree. Do not prepare an uncommitted working
tree for real execution.

The Python module exposes `seal(body, key)` for an external reviewed signing
process. Keys are at least 32 random bytes, represented as hex in:

- `WEALTHFOLIO_PROMOTION_EVIDENCE_KEY`: signs restore proofs, independent reviews,
  preparations and execution journals.
- `WEALTHFOLIO_PROMOTION_OPERATOR_KEY`: a separate operator authorization key.

HMAC is a **shared-secret local attestation**, not a public-key signature or
third-party proof. Restrict the keys and private files with OS permissions. The
CLI does not generate an approval or automatically authorize its own preparation.
Do not commit keys, include them in command-line arguments, or log their values.
`WEALTHFOLIO_PASSWORD` supplies the actual target password for authenticated reads.

### Target document

An independently selected target has these fields:

```text
containerId          full immutable Docker container ID
containerName        exact name, not a discovery pattern
composeProject       exact com.docker.compose.project label
composeService       exact com.docker.compose.service label
imageId              immutable Docker image ID, not a mutable tag
origin               exact loopback origin
database             host database path under the private root
containerDatabase    authenticated /app/info dbPath
containerPort        container-side port key, e.g. 8080/tcp
instanceId           existing safety.instance_fingerprint(client, origin)
```

The adapter requires a local Windows named-pipe Docker endpoint, then checks
Docker labels/image, a writable directory bind mount mapping
that exact host file to that container path, password-hash configuration and the
exact loopback port binding. Other running containers with writable binds
covering the database cause rejection. A file mount is not safely renameable and
is unsupported.

### Independent review

After inspecting the full private diff, sign a body with kind
`wealthfolio-bounded-repair-promotion-review` and:

```text
planHash, evidenceHash, diffHash, releaseRevision, targetHash
clonePrestateDiffHash, restoreDiffHashes, runtimePolicyHash, canonicalEvidenceHash
independentEvidence: [{path, sha256}, ...]
legacyWriterTasks: [{taskPath, taskName}, ...]
legacyWriterState: {tasks: [...], definitionHash: ...}
```

`evidenceHash` is `plan_fingerprint(plan["evidence"])`; `targetHash` hashes the
whole target document. `independentEvidence` must include the exact original
canonical manifest, source application plan and source application receipt
files bound by the repair plan, declared raw artifacts needed by the selected
scope rows and source-authority intervals, plus independent sources/review
rationale. Operation `sourceHashes` mix namespaces: some are actual file-byte
hashes and others are normalized canonical-row hashes. **Do not manufacture
files for normalized hashes.** The latter must match rows in the original
manifest-bound `identityScope`; the original observation/lineage records must
also bind the selected decision and survivor. Every canonical `dataFiles` entry
is checked byte-for-byte. Hash-bound correspondence inputs belong in the raw
evidence inventory too.
Review the source occurrence and compensation evidence before signing.

`clonePrestateDiffHash` binds supported startup differences between the original
production backup and the clone's actual pre-apply recovery backup. No activity,
assignment, setting, sync, or other user-owned difference is permitted there.
`restoreDiffHashes` maps `original`/`candidate` to each real restore proof's full
environmental diff. `runtimePolicyHash` binds the generated per-row timestamp
policy; it is not an operator-provided blanket exclusion.
`canonicalEvidenceHash` binds the manifest/data files, scope hash, selected
normalized rows, their declared source artifacts, and operation lineage links.

The task inventory must identify the actual legacy app writers, **not** the
source-only collector or arbitrary disabled tasks. `GuardedDockerRuntime.writer_state`
reads their Windows Task Scheduler definitions and requires both disabled state
and `Settings.Enabled == false`. Its returned definition hash is part of the
review and is checked again at preparation, execution and after startup.
This is verification only: the path never changes any scheduled task.
The operator must separately inventory/quiesce other app/external writers.

## Workflow

### 1. Fresh clone and exact diff

Use the existing authenticated backup flow to obtain a fresh production backup.
Restore a fresh staging clone and apply the schema-2 receipt-repair recipe with
its unchanged staging interlocks. Retain its applied receipt, recovery record and
downloaded recovery backup. Obtain the repaired clone's candidate backup.

```powershell
python -m importers.rebuild.bounded_promotion_cli diff `
  --data-dir $env:FINANCE_DATA --plan plan.json `
  --original original.db --candidate candidate.db --output bounded-diff.json
```

This is offline and never touches runtime state. The private output contains the
full diff and its hash, plus a supported/blocker indication. An unsupported diff
must not be approved.

### 2. Real isolated restore proofs

Provide two independently identified, **already stopped**, disposable restore
containers with empty database slots and the exact production image. Their
origins and host database slots must not be production. No production container
is stopped by these commands:

```powershell
python -m importers.rebuild.bounded_promotion_cli restore-proof `
  --data-dir $env:FINANCE_DATA --plan plan.json --backup original.db `
  --target original-restore-target.json --expected ready --output original-proof.json
python -m importers.rebuild.bounded_promotion_cli restore-proof `
  --data-dir $env:FINANCE_DATA --plan plan.json --backup candidate.db `
  --target candidate-restore-target.json --expected applied --output candidate-proof.json
```

The commands really copy/start/authenticate/stop those isolated containers.
They retain each restored database and its exact environmental diff, rejecting
unsupported drift. They cannot target port 8088. Keep both proof databases stopped
and unchanged. Financial values observed in the candidate restore must be reviewed
explicitly before they can be an alternative production post-state.

### 3. Build the review, prepare and inspect without stopping production

The private request is an object with `target` and these fields:

```text
plan, receipt, recovery, review
originalBackup, candidateBackup
originalRestoreProof, candidateRestoreProof
appliedCloneInstanceId, releaseRevision
canonicalManifest (optional)
```

Paths are private-root-relative or absolute under that root. `review` names the
independently signed review described above.
`canonicalManifest` defaults to `normalized/canonical/manifest.json`; if the
publication has advanced, point it at the archived **original** manifest with
its unchanged sibling data files, never at a rebuilt approximation.

```powershell
python -m importers.rebuild.bounded_promotion_cli review-inputs `
  --data-dir $env:FINANCE_DATA --request request.json --output review-inputs.json
```

This offline command needs the plan, receipt/recovery, both backups and both
restore proofs, but **not an existing review**. It returns complete repair,
clone-prestate and restore diffs, the precise timestamp policy, all review-binding
hashes, `requiredEvidenceSha256` for actual files, and separate
`normalizedRowHashes`/`canonicalEvidence` for normalized evidence. It does not inspect the live database,
Docker, or scheduled tasks, and never signs a review or authorization.

After independently reviewing this material, supplying the complete source-file
inventory and checking the actual disabled legacy writers, sign the review and run:

```powershell
python -m importers.rebuild.bounded_promotion_cli prepare `
  --data-dir $env:FINANCE_DATA --request request.json --output preparation.json
python -m importers.rebuild.bounded_promotion_cli inspect `
  --data-dir $env:FINANCE_DATA --preparation preparation.json
```

`prepare` authenticates/read-checks the live target and task/container state, but
does not stop it, create a live backup, mutate its API, or install a writer marker.
`inspect` is offline: it verifies signed content and bound files/restored states,
not current runtime readiness.

### 4. Explicit release authorization and execution

After reviewing the final preparation, the operator signs a separate
`wealthfolio-bounded-repair-promotion-authorization` body containing:

```text
preparationId     preparation.documentHash
planHash         preparation.planHash
releaseRevision  preparation.releaseRevision
targetHash       preparation.targetHash
diffHash         preparation.diffHash
approval         AUTHORIZE_EXACT_BOUNDED_DATABASE_PROMOTION
writeBoundary    NO_APP_OR_EXTERNAL_WRITES_UNTIL_TERMINAL_RECEIPT
operator         nonempty operator identity
issuedAt         timezone-aware ISO timestamp
expiresAt        timezone-aware ISO timestamp; at most 30 minutes after issuedAt
```

Keep users and other external writers out of the maintenance window until a
terminal receipt. **There is no enforceable HTTP maintenance fence in this
implementation.** Disabled scheduled tasks and the signed acknowledgment do not
block interactive or other external clients. The operator quiescence dependency
is real; violating it can prevent completion and require manual reconciliation.

The implementation does not rely on that acknowledgment to discard data safely:
pre-stop edits are checked against the full original, and rollback independently
checks the full current database against the reviewed post-state references.
Unexpected notes, categories, settings, transactions, unknown-table changes or
unreviewed market values block rollback. No current database is replaced in this
case. Verification receipts are point-in-time observations, not a claim that
later writes cannot occur.

```powershell
python -m importers.rebuild.bounded_promotion_cli execute `
  --data-dir $env:FINANCE_DATA --preparation preparation.json `
  --preparation-id $env:REVIEWED_PREPARATION_ID --authorization authorization.json
```

The command rechecks all evidence (including after staging the candidate),
authorization, clean release, disabled tasks,
target and backups before stop. It stages a byte-exact candidate, records durable
intent, stops the allowlisted container, rechecks the **entire stopped prestate**,
fences files, preserves the original and installs the candidate. It restarts,
authenticates, waits for the scoped read-only performance result to settle,
checks dependent state and the complete database against the reviewed references,
and records completion only after all checks pass. No writer ownership marker or
PostgreSQL publication is activated.

### Programmatic interfaces

The parent runtime owner may call these functions directly; `root` is the external
private data directory, and `runtime` is `GuardedDockerRuntime(target, root, password)`.

```python
restore_proof(root=root, backup=backup_path, plan=plan, runtime=restore_runtime,
              evidence_key=evidence_key, expected="ready")  # or "applied"
build_review_inputs(root=root, request=request, evidence_key=evidence_key)
prepare(root=root, request=request, runtime=production_runtime, evidence_key=evidence_key)
inspect_preparation(preparation, root=root, evidence_key=evidence_key)
execute(root=root, document=preparation, authorization=authorization,
        supplied_preparation_id=preparation_id, runtime=production_runtime,
        evidence_key=evidence_key, operator_key=operator_key)
recover(root=root, runtime=production_runtime, evidence_key=evidence_key,
        operator_key=operator_key, expected_preparation_id=preparation_id)
```

`REVIEW_BINDINGS` lists the fields to copy from the offline review inputs into
the reviewed body. Add its `kind`, `independentEvidence`, `legacyWriterTasks` and
`legacyWriterState`, then use `seal(review_body, evidence_key)` in the separately
controlled signing process. These interfaces do not invent or grant financial
authorization; `execute` still requires the exact separately signed authorization.

Successful execution includes `databaseVerification`: `referenceStateHash`,
`actualStateHash`, `checkedAt`, the full `timestampDiff` and its hash. Verified rollback uses
the same contract under `rollbackVerification`, unless it simply restarted an
untouched original after detecting legitimate pre-stop drift.

## Interruption, rollback and retained slots

Slots are deterministic siblings of the authorized live database:

```text
<database>.bounded-incoming
<database>.bounded-original
<database>.bounded-rejected
<database>.bounded-journal.json
<database>.bounded-lock
```

Do not remove them to retry. The OS lock releases on process exit, but its file
and the signed phase journal persist. A later `execute` refuses existing state.
For a new batch after a genuinely completed release, use the separately authorized
[archival and verified continuation workflow](repeated-receipt-repair.md), never
manual slot deletion. Failed/manual-recovery states are not eligible.
The journal contains original authorization and preparation, phase history, the
stopped original byte hash, and final verification. Terminal states are
`completed`, `rolled-back`, and `aborted`; `recovery-required` and
`manual-recovery-required` are never success.

```powershell
python -m importers.rebuild.bounded_promotion_cli recover `
  --data-dir $env:FINANCE_DATA --target production-target.json `
  --preparation-id $env:REVIEWED_PREPARATION_ID
```

Recovery validates journal signatures, exact target and original authorization.
It can use an expired original authorization **only for a proven-safe rollback**,
never forward promotion or deletion of subsequent user work. Before-stop
interruption is aborted. After the candidate has served, recovery reads the
complete current state again: the old journal is not evidence that no one has
used the app since it was interrupted. The same check applies when an interrupted
rollback already restarted the original.

Protection is checked before a new stop, after the stop, and against the mandatory
file-fence byte hash immediately before any current-to-rejected rename. New work
observed at any boundary records `manual-recovery-required`, with the boundary
and observed state hash. The current database, its sidecars, the original slot
and any existing rejected slot are not discarded. A before-stop refusal leaves
the service as found; an after-stop/file-fence refusal leaves it stopped.
`recover` refuses this latched state on later calls, even with the old valid
signature. An operator must preserve/inspect the current data and make a newly
reviewed reconciliation decision; do not clear the journal to force a retry.

When current state is still exactly an approved reference plus the explicitly
allowed timestamps, automatic restoration may proceed. Other operational failures
such as changed original backup bytes or failed restart/authentication remain
`recovery-required`. All evidence is retained. Completed executions likewise need
a new separately reviewed operational decision, not automatic rollback.

The two filesystem renames are recoverable steps while the app is stopped; they
are not a multi-file transaction. There is **no cross-database atomicity claim**,
and no PostgreSQL accepted-generation or canonical-writer activation occurs.

## Validation and upstream contract references

Synthetic tests use disposable SQLite databases and mock Docker/task adapters:

```powershell
python -m pytest tests\test_bounded_promotion.py -q
python -m ruff check importers\rebuild\bounded_promotion.py `
  importers\rebuild\bounded_promotion_cli.py tests\test_bounded_promotion.py
```

These do not establish readiness of any actual household pilot or deployment.
Before runtime use, review the supported schema against the pinned image:

- [Upstream SQLite schema](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/crates/storage-sqlite/src/schema.rs).
- [Calculated versus manual snapshot sources](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/crates/core/src/portfolio/snapshot/snapshot_model.rs).
- [Quote sources and manual-price protection](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/crates/core/src/quotes/types.rs).
- [Outbox serialization and metadata sequence preservation](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/crates/storage-sqlite/src/sync/app_sync/repository.rs).
- [SQLite backup semantics](https://www.sqlite.org/backup.html).
- [Windows file sharing and delete-sharing semantics](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew).
