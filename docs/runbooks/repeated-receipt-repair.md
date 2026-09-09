# Continuing completed bounded repairs

This extends the [bounded promotion contract](bounded-repair-promotion.md).
It does **not** grant production authorization, add a matcher, or relax the v5
exact-description/same-source-day evidence gates. All artifacts remain outside
this public repository. Historical schema-1 verification and existing schema-2
single-batch records keep their original hashes and interpretation.
Full historical input closure is the default. The narrowly enumerated
[historical matching replay qualification](#qualified-historical-matching-replay-gaps)
below can disclose a lost ancestor-only overlap input without weakening proof
of applied financial effects.

## Contract

New continuation plans remain schema 2 and add an optional, versioned
`productionLineage` extension (`schemaVersion: 1`). It binds an immutable signed
chain file, chain/head hashes, production target, original source application
plan and receipt, account, reconciliation identity, expected compensation
fingerprint, receipt-proven cash value, and already deleted IDs/aliases.

The chain starts with the **original applied source application receipt**.
Every subsequent member must have:

- A complete, signed, actual production execution ending in `completed`, with
  the ordered stop/swap/start phases and no failure or rollback history.
- The exact original authorization, preparation, schema-2 repair plan, applied
  clone receipt, recovery record, backup files and signed restore proofs.
- A reconstructable production database post-state and authenticated applied
  endpoint result. A successful clone receipt alone is insufficient.
- An explicitly completed archival receipt, retaining the original execution
  JSON bytes and original rollback database.
- The exact preceding chain/head, same source application/account/reconciliation,
  disjoint newly deleted IDs/aliases, and a compensation adjustment equal to
  **only that step's newly deleted signed effects**.

Duplicate, sibling, forked, missing or reordered steps are rejected. The old
token-based proposal cannot enter this chain: neither schema-1 plans nor a
stage-only applied receipt qualify as a completed production execution.

The original application receipt anchors the compensation row. The first
completed production repair's cash-value receipt, checked against its actual
original/candidate SQLite snapshots, anchors the current account value; an older
application's asserted total is not silently treated as today's balance.
Every continuation keeps that proved value. The planner never invents a
balancing adjustment to make a changed account fit this anchor.

## 1. Explicitly archive a completed release

Archival has a **new, separate operator authorization**, not reuse of the old
promotion or rollback authorization. Sign this body with the existing operator
key in the separately controlled signing process:

```text
kind              wealthfolio-bounded-repair-promotion-archive-authorization
approval          AUTHORIZE_COMPLETED_PROMOTION_ARCHIVE
executionHash     completed execution.documentHash (not its file-byte SHA)
targetHash        plan_fingerprint(the exact production target document)
archiveDirectory  exact private-root-relative destination directory
operator          nonempty operator identity
issuedAt          timezone-aware ISO timestamp
expiresAt         at most 30 minutes after issuedAt
```

The existing evidence/operator verification keys are read from
`WEALTHFOLIO_PROMOTION_EVIDENCE_KEY` / `WEALTHFOLIO_PROMOTION_OPERATOR_KEY`.
No command generates a secret or an authorization.

```powershell
python -m importers.rebuild.bounded_promotion_cli archive `
  --data-dir $env:FINANCE_DATA --target production-target.json `
  --execution-hash $env:REVIEWED_EXECUTION_HASH `
  --destination $env:REVIEWED_ARCHIVE_DIRECTORY `
  --authorization archive-authorization.json
```

**Archival is not rollback and does not access the live database.** The schema-2
archiver holds the promotion execution lock, checks Docker target/image/path
bindings, verifies the already completed signed execution and retained original
slot, and freezes the exact historical input bytes before retiring side slots.
It never opens or hashes the live SQLite file, checkpoints it, stops/restarts the
service, calls the app API, or invokes recalculation. No app password is needed.
Ordinary later user edits and transactions do not invalidate historical archival
and are never reversed.

The destination must be new, private and on the same filesystem as the retained
slots. Choose a location outside collection input directories. Hard-link support is required for
the read-only compatibility view. Its contents
include:

```text
execution.json          byte-exact copy of the original execution journal
original-journal.json   original journal, moved without rewriting its contents
original-slot.db        original rollback database, moved and hash-verified
blobs/<sha256>          immutable, content-addressed bound input bytes
evidence/               read-only hard-link view for primary historical files
receipt.json            signed completed archival receipt
intent.json             retained signed archival intent
```

References inside old documents are not rewritten. The signed archival receipt
records `originalDataRoot`, the complete `relocations` list and its hash: original
locator spellings, normalized relative paths, exact byte hashes, immutable blob
locations, and primary mirror locations.

The evidence closure includes preparation/receipt/recovery/backup files,
independent review inputs, **every original canonical-manifest source file**, and
**the original source application's `evidence` bindings**. This includes mutable
inputs such as `extracts/mapping.json` and `identity/source-authority.json`, even
when they were not repeated in the independent-review list.
For schema-1 and schema-3 application plans, it also traverses
`decisionEvidence.nestedEvidence`, schema-1 manual decision documents'
`decisions[].evidence`, and the inputs of each explicitly referenced
`evidence.stagingApplicationPlan`, recursively. Every referenced application
plan's schema and seal are checked. Recursion is keyed by `(path, hash)`;
arbitrary hashes inside raw evidence documents are not treated as file locators.

The address is **(original path, expected hash)**, not the current working path.
An older application and a newer canonical publication can legitimately bind
different versions of the same mapping file; both byte versions are retained.
The primary mirror serves existing historical readers, while the relocation
manifest selects any older version unambiguously. Already archived bytes can
satisfy old references during a later archival cycle; current mappings are never
substituted for a missing old hash, and no files are invented for normalized-row
hashes.

Three deterministic sibling files coordinate future cycles:

```text
<live database>.bounded-history.json  signed, append-only archive registry
<live database>.bounded-history-origin.json  immutable first-archive guard
<live database>.bounded-archive.json  pending intent; blocks all new promotions
```

The history head is published only after the completed archival receipt is
durable; the pending intent is removed last. The fixed OS lock remains.
The next cycle cannot silently ignore the registry, pick a sibling, or reuse
slots while archival is pending.
The first-archive guard prevents a missing/truncated registry from silently
turning an already-repaired target into a new first-release history.

### Crash-atomic metadata publication

Every new metadata file—including intent, archival receipt, first history head,
origin guard, plans and recovery records—is written to a uniquely owned staging
file, flushed and file-fsynced, verified, and then linked atomically into its
final name **without overwrite**. The final JSON path is never an in-progress
write. Existing matching bytes are verified and reused; different/unverifiable
bytes are refused. Later history/journal updates retain their existing
fsynced-staging/atomic-replacement path under the execution lock.

A process interrupted during a byte write leaves only an unpublished staging
file. A successful retry publishes complete metadata and cleans abandoned
staging files for that destination. A crash after linking leaves either a
complete final file or no final file, so an intact pending intent can resume
even after the old side slots were retired. Staging output reservations use
separate marker files, not empty JSON documents.

### Failure and interruption

Nonterminal, rolled-back, failed, manual-recovery or uncertain executions cannot
be archived. Unexplained incoming/rejected slots also block archival: they are
left untouched, not discarded to clear the workspace.

Live data does **not** have to remain at the old promotion post-state forever.
No quiescence of interactive app writers is needed for archival itself: only
promotion/rollback executions are excluded by the shared execution lock. Target
or historical slot/hash mismatches still refuse archival. Keep the original
bound input bytes available until freezing completes; if a mutable input changes
before it is frozen and no matching prior blob exists, archival refuses rather
than recording a newer version as the old one.

Retry `archive` with the same execution/destination and a current authorization
after an operational interruption. Each blob is copied and hash-verified
atomically; completed blobs are reused, never overwritten. A durable closed
receipt resumes only head publication; already moved originals are checked in
their archive slots. Later live edits do not block resumption. Changed immutable
evidence does. Never delete a pending intent to force slot reuse.

Previously completed schema-1 archival receipts retain their original verifier
and hashes. An unfinished schema-1 intent requires explicit operator recovery;
the new archiver will not resume its older live-control algorithm.

## 2. Build and save the verified lineage

After archival completes, this command is offline and does not need the app
password or perform Docker operations:

```powershell
python -m importers.rebuild.bounded_promotion_cli lineage `
  --data-dir $env:FINANCE_DATA --target production-target.json `
  --output lineage-next.json
```

It verifies the entire registered archive prefix, original source plan/receipt,
all completed execution/authorization/backup hashes, compensation progression,
and deletion uniqueness. It emits the signed, deterministic current chain.
Old chains remain readable evidence but cannot authorize a new mutation once
the registered head has advanced.

## 3. Plan and rehearse only the next proven batch

Refresh the normal baseline/forensic/canonical evidence through its existing
workflow. Use the same original source application plan SHA and exact selected
`sourceActivityIds`; parsing improvements must be proved in the source-side
pipeline, not inferred from the chain.

```powershell
python -m importers.rebuild.receipt_repair_cli plan `
  --data-dir $env:FINANCE_DATA --selection selection-next.json `
  --production-lineage lineage-next.json --output plan-next.json
```

For continuation plans:

- `selectedSourceRows` still counts the original source cohort in the selected
  date range.
- `previouslyRepairedSourceRows` counts members already removed by verified
  production history in that range.
- `liveSourceRowsBefore` excludes those previous removals.
- `sourceSuppressedRows` counts only the new proposed deletions.
- `preservedSourceRows` / `remainingLiveSourceRows` count remaining **live**
  source rows, not previously deleted aliases.

The previous IDs/provider aliases must remain absent. Every remaining source row
must be live and unambiguous. The current compensation must match the previous
completed step; an edited amount or metadata cannot be adopted as a new baseline.
Repeated planning does not apply or count any deletion again.

Run the unchanged staging-only `apply`/`verify` commands with new receipt,
recovery and backup filenames, then the normal `restore-proof`, `review-inputs`,
independent review, `prepare` and separately authorized `execute` workflow.
The extended plan/chain file is bound into preparation. The archive head and
all prior evidence are checked again before the next production stop.

Unknown user-owned state captured in the **fresh next baseline** is preserved;
there is no requirement that unrelated accounts or notes have stayed frozen
since the previous release. The strict current-to-prepared production check
still applies immediately before each swap.

## Programmatic interfaces

`importers.rebuild.repair_lineage` provides:

```python
archive_completed(root=root, runtime=runtime, destination=archive_directory,
                  authorization=archive_authorization,
                  supplied_execution_hash=execution_hash,
                  evidence_key=evidence_key, operator_key=operator_key)
build_lineage(root=root, target=target,
              evidence_key=evidence_key, operator_key=operator_key)
load_lineage(chain_path, root=root,
             evidence_key=evidence_key, operator_key=operator_key)
resolve_archived_reference(archive_receipt, {"path": original_path, "sha256": expected_hash},
                           root=root, evidence_key=evidence_key)
```

`receipt_repair.build_plan` accepts `production_lineage`, `lineage_evidence_key`
and `lineage_operator_key`. `apply_plan` accepts the two optional lineage keys;
CLI commands use the environment variables above. Existing calls with no
continuation extension remain unchanged. Promotion `prepare` and
`build_review_inputs` additionally accept `operator_key` for historical
authorization verification.

## Initial limits

- One homogeneous chain per production target: same source application receipt,
  account, reconciliation and exact container/image/database identity.
- Same-day exact-source v5 matches only. No phone, prefix or date-window matching.
- The account must retain its original receipt-proven cash value. Newly posted
  financial activity changing that value needs a separately justified anchor,
  not an automatic continuation adjustment.
- Archival requires each bound version available either at its working path or
  in a previously verified content-addressed archive. Missing old bytes cannot
  be reconstructed from a hash. Only the explicit ancestor-matching qualification
  below can record an eligible absence; material bindings remain mandatory.
- Failed/manual-recovery states never become lineage members. No cross-database
  atomicity or deployed-writer ownership is implied.

## Qualified historical matching replay gaps

Strict archival remains the default. A lost input to an **older staging
application's matcher** is not the same as missing proof of what production
actually created, deleted or compensated. An explicit qualification may record
that the old matcher cannot be replayed while retaining the independently
verified applied-effects chain. This does not validate that old matcher's
decisions and does not authorize any new identity inference.

The narrow supported exception requires **all** of:

- A referenced ancestor reached through `evidence.stagingApplicationPlan`,
  with a valid schema-3 plan seal and `mode: "staging-apply-plan"`.
- Its evidence key is exactly `canonical`, `canonicalTransactions`, or
  `canonicalOverlap`.
- The normalized input path is exactly
  `normalized/canonical/transactions.csv`, with its exact old byte SHA.
- An operator-signed enumeration naming the missing input, ancestor plan
  path/hash and exact role. Available files cannot deliberately be skipped.

No exception is allowed for the original directly applied source plan's own
bindings, current canonical data, raw bank/source evidence, manual rulings,
snapshots, receipts, backups, account maps, compensation data, or other roles.
If the same path/hash is also needed in a mandatory role, archival still fails.
Unknown roles and schemas fail closed.

### Exact acknowledgment body

Sign this JSON body with the existing operator key via `seal(body, operator_key)`.
Replace the descriptive digest placeholders with real lowercase SHA-256 values.
Paths may be absolute under the original private root or private-root-relative.

```json
{
  "kind": "wealthfolio-bounded-repair-promotion-historical-replay-gap",
  "schemaVersion": 1,
  "approval": "ACKNOWLEDGE_ENUMERATED_ANCESTOR_MATCHING_REPLAY_GAPS",
  "executionHash": "EXACT_COMPLETED_EXECUTION_DOCUMENT_HASH",
  "targetHash": "EXACT_TARGET_DOCUMENT_HASH",
  "verificationScope": "historical-matching-replay-only",
  "operator": "OPERATOR_IDENTITY",
  "reason": "Why the original auxiliary bytes are unavailable and matching replay is not claimed",
  "issuedAt": "2026-01-20T12:00:00+00:00",
  "missingBindings": [
    {
      "path": "normalized/canonical/transactions.csv",
      "sha256": "EXACT_UNAVAILABLE_OLD_CSV_BYTE_HASH",
      "ancestorPlan": {
        "path": "PATH_TO_THE_REFERENCED_STAGING_APPLICATION_PLAN",
        "sha256": "EXACT_ANCESTOR_PLAN_FILE_BYTE_HASH"
      },
      "bindingRole": "schema3-staging-application-plan.evidence.canonical"
    }
  ]
}
```

Use the ancestor's actual supported evidence-key spelling in `bindingRole`.
This acknowledgment is separate from the ordinary, current archive authorization:

```powershell
python -m importers.rebuild.bounded_promotion_cli archive `
  --data-dir $env:FINANCE_DATA --target production-target.json `
  --execution-hash $env:REVIEWED_EXECUTION_HASH `
  --destination $env:REVIEWED_ARCHIVE_DIRECTORY `
  --authorization archive-authorization.json `
  --historical-replay-gap historical-replay-gap.json
```

The programmatic equivalent is
`archive_completed(..., historical_replay_gap=sealed_acknowledgment)`.
No command generates an acknowledgment automatically. A later completed batch
needs its own explicit acknowledgment if it still depends on the same lost
ancestor input; the first exception is not a global skip permission.

### Visible qualification, never “full closure verified”

A qualified archive uses **archive schema 3**, retains the signed acknowledgment
and normalized `missingBindings`, and explicitly records:

```text
historicalMatchingReplayAvailable = false
historicalInputClosureVerified = false
```

Missing inputs have **no fabricated blob or relocation entry**. Every other
binding is still frozen and verified normally, including the actual completed
execution's before/after SQLite states and source-application compensation
receipt.

Chains containing a qualified archive use **lineage schema 2**. They carry the
false availability flag, per-execution missing-binding list, acknowledgment
hashes and `historicalReplayQualificationHash`. The next repair remains schema 2
but its `productionLineage` extension is **version 2** and carries that exact
qualification. Unqualified old hashes are not reinterpreted.

For the next independent review, copy fields with
`bounded_promotion.review_bindings(review_inputs)` rather than copying only the
legacy `REVIEW_BINDINGS` tuple. Qualified review inputs/preparations must visibly
include `historicalMatchingReplayAvailable`, `missingHistoricalBindings` and
`historicalReplayQualificationHash`; omitting or changing them rejects promotion.
The full missing list stays in private artifacts; CLI output reports the flag
and count without printing private paths.

Synthetic coverage: `tests/test_repair_lineage.py`, `tests/test_receipt_repair.py`
and `tests/test_bounded_promotion.py`. These tests do not prove that a particular
private history is ready for archival or another production release.
