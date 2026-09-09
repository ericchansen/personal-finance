# Incremental cash/card delivery

This worker handles one explicitly mapped, active cash or credit-card account
at a time. It consumes existing source-only collection receipts; **it never
requests SimpleFIN data, installs a schedule, renames an account, deletes an
activity, creates reconciliation gaps, or enables a writer**.
This release is **create-only**: exact read-only adoption remains supported, but
existing activity amount/date corrections are held before financial API calls.

Implementation: [worker](../../finance_store/incremental.py),
[input contract](../../finance_store/incremental_inputs.py),
[activity contract](../../finance_store/incremental_projection.py),
[bootstrap contract](../../finance_store/incremental_bootstrap.py),
[source-anchor transitions](../../finance_store/incremental_anchor.py),
[CLI](../../finance_store/incremental_cli.py),
[migration 0019](../../deploy/postgres/migrations/0019_incremental_cash_projection.sql),
[forward extension 0020](../../deploy/postgres/migrations/0020_incremental_source_anchors.sql).

## Runtime prerequisites

An operator must establish these independently before running an account:

1. Apply migrations 0019 and 0020 using the existing reviewed migration/backup
   procedure. The source-anchor extension is forward-only; it does not change
   the previously delivered 0019 or installed identity history.
2. Provide a least-privilege PostgreSQL ingest login through
   `FINANCE_INCREMENTAL_DSN`, and the existing authenticated app password through
   `WEALTHFOLIO_PASSWORD`. Neither belongs in a scope document or command line.
3. Maintain the old overlapping writer task **disabled**. An operator must
   install the distinct **schema-2 incremental ownership marker** below, then
   configure `WEALTHFOLIO_WRITER_MODE=incremental-cash-projector-v1`, the exact
   `WEALTHFOLIO_WRITER_ENVIRONMENT_ID`, and the existing global mutation
   interlock. The worker does not set environment variables or activate markers.
   The schema-1 whole-rebuild marker remains unchanged and fixed to production;
   it cannot authorize incremental staging.
4. Preserve an immutable canonical baseline and its exact dependencies. Supply
   a baseline root with the normal `normalized/canonical/` layout, identity
   declarations, facts, and source artifacts referenced by the manifest.
   `baselineRoot: "."` is supported only while that publication remains pinned
   and unchanged. A copied baseline must preserve its relative source paths.
5. Review the account scope below against actual facts, mapping entries, app
   instance metadata, and current account settings. The target must be loopback.
6. Ensure the runtime has IANA `zoneinfo` data. Missing timezone data is a hold,
   not a fallback to UTC or a fixed offset.
7. Treat existing-activity corrections as a separate maintenance prerequisite,
   **not a supported operation of this release**. An independent empty-instance
   Wealthfolio 3.7 probe found `/activities/bulk` partial updates clear an omitted
   comment and set `isUserModified` to true. A separate native probe also found
   `PUT /activities` clears an omitted comment and ignores explicit
   `isUserModified: false`. It is **not** a fallback update route. GET metadata
   is an object, whereas that PUT route requires a JSON string (an object
   receives HTTP 422); serializing it does not fix the lossless-update problem.
   These native proofs remain external, not public test fixtures. The
   [synthetic contract regression](../../tests/test_incremental.py) models that
   observed bulk behavior and verifies no activity-write route is called for
   held corrections; it is not independent native-app proof. This worker emits
   `upstream-update-contract-unsupported` and never sends updates. A version
   string, scope setting, or marker cannot enable them. Any future capability
   needs independently verified, version/instance-bound lossless and conditional
   write semantics, or a separately controlled maintenance path outside this
   worker. Merely adding back an old comment is not safe concurrent preservation.

The existing writer marker and PostgreSQL gate are cooperative controls, not
protection against an administrator running arbitrary old code or direct SQL.
There is also no lock shared with manual UI edits. Changes observed before a
write hold the plan; unexpected changes observed afterward cause an uncertain
result without rollback. Client-side pre/post checks are not server-side
compare-and-swap or a shared transaction with PostgreSQL. Even a create can race
an unrelated UI cash edit; postcondition failure is uncertain, not authority to
undo that edit. No existing activity is updated, and `isUserModified` is not
used to attribute a change to a person rather than automation.

## Scoped incremental ownership marker

Use a separate external rehearsal data root for staging. Within **that root**,
the only accepted marker path remains
`wealthfolio-rebuild/writer-ownership.json`. An explicit marker environment path
cannot redirect a worker to another root or another filename.

```json
{
  "schemaVersion": 2,
  "mode": "incremental-projector-only",
  "writerModeToken": "incremental-cash-projector-v1",
  "environmentId": "<exact scope writerEnvironmentId>",
  "origin": "http://127.0.0.1:18091",
  "instanceId": "<fingerprint of the authenticated target app>",
  "release": {
    "commit": "<current repository commit>",
    "codeHash": "<current runtime-source and migration hash>"
  },
  "scopes": [
    {
      "scopeId": "<computed scope id>",
      "configurationHash": "<exact validated scope configuration hash>"
    }
  ],
  "activatedAt": "<explicit timezone-aware activation timestamp>",
  "markerHash": "<writer_marker_hash of this document>"
}
```

This template contains no rebuild bundle, preparation, or execution IDs.
`current_incremental_release()` in `mutation_guard` reads the current Git
revision and hashes runtime Python sources plus migrations, including dirty and
untracked runtime files. `writer_marker_hash()` computes the marker's content
binding. Neither helper writes or activates a marker. A code change requires a
new operator-reviewed release binding; an environment variable cannot claim
that old code is the current release.
The worker also pins its loaded release and each plan's release. Editing runtime
files does not authorize a still-running old worker, and a pending plan from a
different release is held for review rather than silently delivered by new code.

Before delivery the worker reads the actual authenticated app instance and
checks it against both the scope and marker. The HTTP side effect runs inside a
bounded request context carrying the allowed scope/configuration, instance,
environment, root, and origin. A legacy client without that context is denied
even if it sets the new mode string. Context cleanup also occurs on exceptions.
Schema 2 supports bare loopback staging origins and explicitly reviewed
production origins; schema 1 retains its original fixed-origin rules.

## External scope document

Store the document outside the checkout. This is a template, not an executable
approval: substitute actual IDs, independently computed hashes, and app metadata.

```json
{
  "schemaVersion": 1,
  "kind": "incremental-cash-scope",
  "dataRoot": "<absolute external data root>",
  "canonicalAccountId": "<account fact id>",
  "sourceAccountId": "<raw SimpleFIN account id>",
  "sourceConnectionId": "<scope returned by the existing source admission parser>",
  "rawConnectionId": "<exact raw organization scope or v2 conn_id>",
  "wealthfolioAccountId": "<actual existing app account id>",
  "origin": "http://127.0.0.1:8088",
  "instanceId": "<existing instance_fingerprint result>",
  "writerEnvironmentId": "<activated writer marker environment id>",
  "postgres": {
    "database": "<authoritative database name>",
    "instanceId": "<shadow_authority_metadata instance_id>",
    "environment": "<shadow_authority_metadata environment_marker>"
  },
  "timezone": "America/Chicago",
  "maxSnapshotAgeSeconds": 129600,
  "maxBalanceAgeSeconds": 172800,
  "accountMap": {
    "path": "simplefin/account-map.json",
    "sha256": "<exact file hash>"
  },
  "accountFact": {
    "path": "facts/accounts.json",
    "sha256": "<exact file hash>"
  },
  "baselineRoot": "<preserved baseline directory relative to dataRoot>",
  "baselineManifestSha256": "<exact canonical manifest hash>"
}
```

Do not guess connection IDs from institution names. Reuse the existing
`partition_snapshot` / `organization_scope_id` contracts and the full raw
`organization_scope` value; a shortened admission hash alone is not the raw
connection binding. Account routing must
match the map's explicit `action: "import"`, `assertionAccountId`, and
`wealthfolioAccountId`. The selected fact must be an active, non-excluded
`AccountFact` of kind `CASH` or `CREDIT_CARD`; the canonical baseline and
authenticated app must agree on the account identity, canonical display name,
type, currency, transaction tracking, and active state. Account-owned investments
are unsupported.
Freshness thresholds are mandatory reviewed policy, not defaults. The illustrative
36-hour collection / 48-hour balance limits above suit a daily cadence better
than a two-hour limit; choose values appropriate to the actual collector and
bank balance timestamps (the implementation bounds each limit to at most 48 hours).

The live fact-file inventory must equal the baseline's fact evidence inventory.
This prevents a newly added exclusion/decision file from being silently ignored.
The entire scope configuration is pinned on first registration. Baseline,
fact, routing, timezone, or app-version changes require an independently reviewed
configuration transition; this first package deliberately has no
`--force`, configuration-reset, or automatic rebaseline operation.
The database name, instance ID, and environment marker are checked before
planning or delivering; pointing the worker at an unrelated writable database
does not establish authority.

## Commands

These commands do not collect source data:

```powershell
python -m finance_store.incremental_cli plan --data-dir <external-root> --scope incremental/scopes/cash.json
python -m finance_store.incremental_cli apply --data-dir <external-root> --scope incremental/scopes/cash.json --run-hash <exact-plan-hash>
python -m finance_store.incremental_cli run --data-dir <external-root> --scope incremental/scopes/cash.json
python -m finance_store.incremental_cli status --data-dir <external-root> --scope incremental/scopes/cash.json
```

`plan` writes reviewed input bindings, accepted revisions, qualifications, and
pending operations, but makes no app mutation. `apply` consumes one exact stored
plan. `run` first resumes an unfinished plan, otherwise plans and delivers an
eligible batch. Repeated `--scope` arguments process independent scopes
sequentially; one held scope does not abort subsequent scopes. `apply` accepts
only one scope and one run hash.

CLI output contains state/reason codes, counts, and hashes rather than activity
rows. Full plans, backup evidence, attempts, and receipts remain under the
external `incremental/` directory and in PostgreSQL.

Do not interpret `operationCount` as a write count on a held plan.
`proposedOperationCount` counts candidate operations;
`journaledOperationCount` counts durable outbox records, including completed ones;
`appliedOperationCount` counts acknowledged effects for that run. A held balance
batch can contain proposals while both journaled and applied counts are zero.
`status` reads actual journal counts and verifies the hash-bound external plan,
so it can also explain plans from earlier releases without rewriting them.
It requires migration 0021's read-only operation-history view.

`plannedBalanceMatchesSource` compares the plan with its bound source balance,
not an independently current balance. Check source freshness separately.
`newHistoricalReviewCount` counts unrepresented, newly surfaced source IDs held
relative to the prior checkpoint; `frontierReviewCount` identifies those on its
observed posting watermark day. These are review counts, not duplicate findings
or permission to add rows. An amount that equals a reconciliation difference
never approves that record. Only the explicit post-origin arrival proof below
can qualify a later-dated source occurrence; all other history remains held.
There is no balance-fitting subset or synthetic adjustment.

Bootstrap history accepts the existing schema-1 application/schema-3 applied
receipt pair as well as schema-3/schema-5. Original bytes, plan fingerprints,
receipt effects, and completed-repair bindings are verified without upgrading
or re-executing historical plans. The legacy pair may omit the later
`metadataFinalizations` operation.

## Source evidence and financial versions

The worker verifies `automation/source-collection/latest-success.json`, its
content-addressed run receipt, the exact snapshot bytes, and the matching request
sidecar. It checks the selected connection using the existing source admission
logic. An error assigned to another connection does not discard a healthy
account; unscopable actionable errors and a failed latest collection cannot prove
health. Recognized request advisories remain in the source and anchor proofs
without blocking a healthy account. Unknown messages remain actionable.
The inclusive request window must agree with the receipt and source-only
collector bounds.

Account currency must be explicitly present in raw data. Transaction currency
must agree with it, including when validating the original baseline artifact.
Baseline SF currency proof identifies the exact raw account and transaction ID
and checks its amount, source day, lifecycle, and currency; account currency
alone does not validate a conflicting transaction currency.
SimpleFIN's `pending` flag is optional under the existing parser: a nonzero
posted timestamp without that flag is posted, while zero posted time denotes
pending. If a pending record needs an effective time, an actual `transacted_at`
is required instead of accepting the parser's observation-clock fallback.
Malformed flags and contradictory explicit status are held.
The actual account balance timestamp is separately checked for freshness, not
replaced with collection time. A missing source account or stale source balance
is a hold, never a zero.

The baseline uses `verified_resolution(require_resolved=False)` after publication
verification. The shared verifier still checks every policy/hash/count and
replays the exact published source-artifact hashes. The row index uses the
returned verified observations themselves, not an unbound reconstruction.
Policy document/hash, generation, source artifacts, and membership
history remain intact. Hash-valid unresolved baseline groups remain unresolved;
they are not certified and do not globally prevent another event/account from
being qualified. For the selected account:

- A fresh snapshot with unchanged scoped provider ID, business date, amount,
  currency, description, and status records only a **sighting**.
- Its original normalized observation, source artifact, and observation time
  remain unchanged. Closed authority intervals are not edited or re-certified
  from daily snapshot timestamps.
- Changed versions append evidence and run the existing resolver with the
  preserved policy. Pending-to-posted continuity uses the same scoped claim.
  Regressions and out-of-order versions are recorded as held evidence rather
  than rewinding a posted financial interpretation.
- Absent posted records are retained. Missing records never authorize deletion,
  historical recreation, a synthetic zero, or an unobserved correction.
- A changed version that no longer fits a closed interval may become a local
  identity/correction hold. The worker does not alter matching policy or invent
  human overrides to keep it projectable.

## Qualification is not the old accepted-current view

`accepted_identity_current` remains identity history, not financial
certification. This package adds scoped qualification records referencing:

- the exact accepted event/revision and generation-local event;
- the immutable canonical manifest and policy hash;
- the source-only receipt, snapshot, request/admission proof, and source IDs;
- explicit raw SimpleFIN account currency or original OFX/QFX `CURDEF`;
- current authenticated account/ledger prestate, actual source balance, and
  the entire eligible batch's predicted effect.

Plaintext description, source day, amount, currency, lifecycle status, and
qualification reasons are materialized from those source events. Unknown
historical currency stays held. A legacy Monarch-only row is not made
source-currency-proven merely because an adapter defaulted it to USD.
This is not another ledger or resolver, and it never silently chooses
`canonical_transactions` as an alternative source of truth.

## Source-state anchor when an existing assertion already represents cash

**Use this contract when an existing `rebuild:assertion:<canonical-id>:<date>`
already brings the app's cash ledger to an immutable source snapshot balance.**
Missing transaction detail is not missing cash. Neither the date of the last
detailed import nor the source request start date authorizes creating all
missing history. Date-window bootstrap is rejected for accounts with an existing
balance assertion.

`incremental_anchor.build_contract` binds:

- the exact raw source snapshot and optional request sidecar;
- exact account/connection/currency identity and hashes of the raw account and
  normalized transaction states;
- the source account balance and its actual balance-observation timestamp;
- collection time encoded by the preserved snapshot path;
- the explicit assertion activity ID, scoped assertion key, and full fingerprint;
- authenticated app inventory, exact prior application receipts, and verified
  repair-alias export described below;
- an explicit bound on the number of transitions.

The initial **entire app cash ledger** must equal the source snapshot's account
balance. The worker does not search snapshots to find a matching sum, modify an
assertion, or add a gap.

Example read-only preparation call (the parent/operator stores and reviews the
returned document):

```python
from finance_store import incremental_anchor

contract = incremental_anchor.build_contract(
    scope,
    source_anchor_spec={
        "snapshot": {"path": "<original dated snapshot path>", "sha256": "<hash>"},
        "request": {"path": "<matching sidecar path>", "sha256": "<hash>"}
    },
    inventory=inventory_file,
    applications=exact_application_pairs,
    repair_history=verified_repair_export,
    assertion_activity_id=actual_assertion_id,
    maximum_transitions=100,
)
```

`request` may be omitted if no sidecar exists; absence never creates an inferred
coverage window. Preserve the snapshot's original dated directory/filename so
its collection time is not replaced by its balance timestamp.

Bind the reviewed document using the scope's **`bootstrapSourceAnchor`**, instead
of `bootstrap`:

```json
{
  "bootstrapSourceAnchor": {
    "path": "incremental/bootstrap/source-anchor.json",
    "sha256": "<exact reviewed file hash>"
  }
}
```

This changes the scope configuration hash. Apply 0020 and review/reissue the
schema-2 scope/configuration marker binding before use. As with other frozen
scope changes, install it before first registration or use an independently
reviewed transition/fresh rehearsal database; there is no reset of production
history or force-accept option.

The versioned transition rules do not select events by their sums:

1. Every newly observed POSTED ID whose source date is **later than the prior
   observed POSTED watermark** is a new cash movement.
2. Every known pending ID that becomes POSTED is a full posted movement,
   even if its source date is older than that watermark. Pending `posted=0`
   records retain a **null source date**; no epoch-zero or collection-date
   posting date is invented.
3. Every known POSTED ID's amount change contributes its exact amount delta.
4. A newly exposed ID at/before observed progress can be an **explicit
   post-origin late arrival** only under the proof below.
5. Other newly exposed IDs at/before observed progress remain **held historical
   detail/backfill**, not new cash and not alleged duplicates.

The watermark comes only from observed POSTED source states. It is **not**
request coverage, a balance-date cutoff, or proof of historical completeness.
With no previously observed POSTED state, no date watermark is invented; known
pending transitions can still settle.

The entire fixed financial transition set must be explainable by eligible
operations and reconcile to the fresh source balance. A blocked transition
holds the account cash batch even when some subset would happen to balance.
Historical backfill holds remain visible separately and do not authorize
financial writes.

### Explicit post-origin arrivals

The source-state anchor retains its original `observed-posted-progress-v1`
origin binding. New runs declare `observed-posted-progress-v2` separately; they
do not rewrite old anchors, plans, receipts, or accepted checkpoints.
The installed release/code hash remains an explicit writer-activation binding.

An unseen source ID on or behind the current posting-date watermark may be a
newly reported occurrence rather than pre-anchor history. The
`explicit-post-anchor-arrival-v1` proof requires all of the following:

- The raw record has explicit, nonzero posting and transaction timestamps.
  Both dates are strictly after the day the **original source snapshot was
  observed**; transaction time is no later than posting time, and posting time
  is no later than the current observation. This does not use the balance
  timestamp as a complete posting cutoff.
- A previous successfully reconciled collector receipt exists, passes its
  original content/input-set hashes, and binds the previous checkpoint's
  snapshot and protocol.
- The occurrence lies inside both the prior and current explicit request
  windows. The new request neither widens backward nor retreats its end.
  Request bounds establish this relationship, not transaction completeness.
- The parsed source state matches the exact raw transaction bytes. No amount
  or balance residual participates in selecting a late arrival.
- Existing alias/binding, linked-source, removed-projection, and correction
  guards still pass. An existing app row with the same signed amount, currency,
  exact normalized description, and UTC or local business day holds for
  identity review; it is never silently merged or recreated.

All occurrences satisfying these structural criteria join the fixed transition
set, including zero-valued posted events. The **entire** set must reconcile to
the fresh source balance. A mismatch holds every proposed financial operation,
even if a subset would fit. Earlier/pre-origin records, missing timestamps,
unproved request relationships, and known removed aliases remain protected.
This rule does not enable existing-row updates or any historical repair whose
required evidence is unavailable.

Raw linkage/security restrictions are checked on every current sighting,
including unchanged economic versions, and in the frozen baseline. Newly
observed restrictions create an unadmitted source version even when cash
fields are identical. Those holds remain durable if a later response omits
the fields; omission is not evidence that a link or security interpretation
was cleared. An older admitted version is retained as history, not reused as
permission to create the restricted occurrence.
Dateless pending restrictions are preserved in the sealed run's
`heldSourceChanges` journal rather than inventing a dated financial observation.
Later qualification consults that history even when the source ID is absent
from the latest snapshot. A missing transaction or omitted restriction field
cannot release the hold.

When upgrading an older deployment, inspect its preserved source evidence for
restrictions that the older code did not record. This code does not claim to
recover never-recorded historical decisions or to validate a previously
published financial state merely by starting a new release.

### Explicit full versus delta cash representation

An already detailed, exactly linked source activity requiring correction is
held by the upstream-update-contract guard, retaining its verified
delivered/anchor basis. If a known posted source record has **no
detailed app row** because its principal is already represented by the cash
anchor, only its genuine amount change may be represented.

That creates an explicitly named **source-delta** activity, keyed by stable
accepted identity and anchor hash. Its amount is the source amount minus the
immutable source base—not a reconciliation residual. PostgreSQL retains the
full source amount, source base, checkpoint reference, and represented app cash
amount separately. A later same-ID change requiring an update of this existing
delta representation is held, including a return to the source base. The
worker does not append a second adjustment to bypass that update hold.
Pending settlements and new tail events use normal full-amount activities.

The assertion is never used as an event's application binding and never appears
as an operation target. Known prior projections, removed aliases,
missing bindings, multiple aliases, user-modified rows, and unsupported linked
or cross-source interpretations retain their normal protections; a delta is
not a way around them.

### Verified checkpoint progress, not accepted intent

Migration 0020 stores immutable source-state checkpoints and anchor-backed
adoptions/representations. A checkpoint advances only after a verified no-op
cash reconciliation or after **all** required operations have been observed and
acknowledged. Held plans do not advance it.

Partial APPLIED operations are overlaid on the prior source state for
per-ID amount continuity, but they do not advance its date watermark. Therefore
an unsent tail item cannot become "historical" merely because a later-dated
item committed first. Missing posted IDs are retained. Wider-window historical
IDs can be recorded as newly observed state after reconciliation without
creating their details; later real same-ID amount changes can then be compared.

Each subsequent account prestate must match the original financial inventory
plus journal-proven operations, including exact cash. Ordinary notes remain
legitimate inputs; financial edits outside that journal, removal of a bound
activity, or alteration of the fingerprinted assertion produce a hold, not a
rollback/rebase. Checkpoint source state, journal cursor, and private sealed plan
bindings are verified before use.

Read models:
- `finance_read.incremental_source_anchor_status`: original anchor hashes,
  anchor balance/assertion, last reconciled source checkpoint, and observed
  POSTED watermark.
- `finance_read.incremental_cash_representations`: full source amount versus
  actual represented cash, immutable base, stable event ID, and anchor reference.
- Existing scope status still shows the latest source/balance freshness, which
  can be newer than the last reconciled checkpoint.

## Reviewed date-window bootstrap without a cash assertion

A posted observation in the frozen source baseline may never have been
projected while the old writer was disabled. Without additional evidence it
remains a hold. The optional `bootstrap` scope binding below permits **only a
bounded, explicitly reviewed set of unrepresented baseline SF versions**—not
all missing activities and not a balance-fitting subset.
This older contract is for scopes without a current-balance assertion. It must
not be used to backfill detail that an account-level source anchor already
financially represents.

The contract requires:

1. Exact immutable copies of prior schema-3 source application plans and their
   schema-5 **applied** receipts, or historical schema-1/schema-3 pairs.
   Plan seals, plan/receipt hashes, account
   assertions, operation counts, and pre/post fingerprints must agree. Obsolete
   absolute paths *inside* those historical documents are treated as data, not
   followed or rewritten.
2. A portable signed export of the complete, ordered completed repair history
   for the account, pinned to its exact head execution hash. The export includes
   original application-plan/receipt hashes, removed activity IDs/source aliases,
   and the last repaired account-ledger fingerprint.
3. A frozen, authenticated inventory of the **actual target app** in the
   rehearsal/production scope. Its financial identity fingerprint must match the
   verified repair poststate. Its complete DTO state—including unknown fields—is
   checked against the live app before bootstrap creation.
4. An explicit source-business-date window of at most 90 days and a maximum
   candidate count of at most 1,000.
5. The complete deterministically derived candidate-version list and its hash.
   It binds baseline observation ID, raw source identity, financial-version hash,
   date, amount, currency, description hash, and raw currency evidence. Removing
   candidates to make a balance happen to fit invalidates the contract.

Previously projected source IDs and completed-repair removals are **negative
evidence**. Their missing activities cannot be recreated, including when an old
alias reappears outside the current baseline. Missing existing bindings and
multiple existing aliases remain holds. An opaque existing activity with a
colliding economic tuple is also a hold, never fuzzy adoption.

These are read-only preparation helpers; they return documents but do not write
files, activate a marker, or invoke a source collector:

```python
from finance_store.incremental_bootstrap import (
    export_repair_history, capture_inventory, build_contract,
)
```

- The parent/operator runs `export_repair_history(origin_root, executions,
  evidence_key=..., operator_key=...)` against the actual completed evidence.
  Each `executions` entry contains a hash-bound `execution` file specification
  and an optional relative `evidenceRoot`. The helper delegates to the existing
  `repair_lineage.completed_execution` verifier: a claimed `completed` flag is
  insufficient. Supply the entire origin-to-head sequence. This verification
  reads historical backups and receipts, not the running app.
- Copy the resulting signed export, unmodified historical plans, and receipts
  into the separate staging data root. The worker verifies the portable export's
  HMAC using the named environment variable; it does not follow production-root
  paths or re-run production verification from the rehearsal.
- Use `capture_inventory(scope, authenticated_client)` to read the target
  instance. Persist the returned document immutably outside the checkout.
- Call `build_contract(scope, applications=[...], repair_history={...},
  inventory={...}, source_date_window={"from": "...", "through": "..."},
  maximum_candidates=...)`. All file specifications are `{"path": "<relative
  private path>", "sha256": "<exact bytes hash>"}`. Application entries are
  `{"plan": <file>, "receipt": <file>}`. `repair_history` contains `file`,
  `headExecutionHash`, and optional `verificationKeyEnv` (defaulting to the
  existing promotion evidence-key environment variable). That environment value
  is the hex-encoded verification key, never a value written into the contract.

`build_contract` derives the whole candidate set independently of the current
source balance. Review that returned document before installing it. It is a
bounded proposal, not proof that every candidate is currently eligible.
Current resolver conflicts, source admission, currency/lifecycle checks,
current source presence, and the unchanged whole-batch balance check still apply.

After review, bind the stored contract in the external scope:

```json
{
  "bootstrap": {
    "path": "incremental/bootstrap/reviewed-contract.json",
    "sha256": "<exact reviewed contract file hash>"
  }
}
```

The scope ID does not change when adding this binding, but the configuration
hash **does**. Install/review the corresponding schema-2 marker binding before
apply. Configure bootstrap before the scope's first registry registration.
An already registered different configuration is not silently replaced; use a
fresh disposable rehearsal database or an independently reviewed configuration
transition, never delete production history or use a force switch.

Only unchanged reviewed versions that are actually present in the current
admitted snapshot can bootstrap. A changed amount/date/status/version, an
unreviewed source ID, or a changed inventory is held. Journal-proven APPLIED
changes are replayed over the frozen inventory, allowing interrupted catch-up
and safe replay without treating the worker's own creations as drift. Once all
bootstrap candidates are represented, ordinary later edits and daily
incremental activity do not require the old inventory to remain frozen.
Existing activity bindings still prevent later user-deleted rows from being
recreated under the old grant.

The fresh source balance is never copied into a reconciliation transaction.
The **entire eligible batch**, including catch-up and ordinary eligible new
events, must reconcile from the actual app ledger. A discrepancy remains an
account-batch hold, even with a valid bootstrap contract.

## Exact projection and cash checks

Legacy adoption requires exact scoped source lineage and matching economic
fields. Existing SimpleFIN/extract/Monarch keys remain attached to their actual
app activity IDs. An OFX survivor already covering an SF alias keeps its key;
replayed SF sightings do not recreate that alias. Missing previously bound or
baseline-posted activities are holds unless the narrowly bound bootstrap
contract above proves a reviewed unrepresented source version. Known projected
or removed activities are never covered by that exception.
The worker also recognizes `canonical:` keys from the **verified baseline**
and exact active `application_projection_bindings` linked by the accepted-ID
registry. A canonical-looking prefix or equal description is not proof. Recorded
target hashes are matched to actual scoped activity IDs; absence or multiple
candidates never triggers re-creation.

Multiple existing activities for one event remain projection conflicts. Linked
transfers, splits, core correction/reversal references, and cross-source
economic corrections are not automatically rewritten.

New supported activities use `finance:accepted:<accepted-event-id>` as their
deterministic idempotency key. Date-only events are written at local noon in the
authenticated app's configured IANA timezone, converted to UTC. Existing adopted
timestamps remain untouched, including when a new source revision proves a
different source business date.

All existing amount/date/type corrections receive the explicit
`upstream-update-contract-unsupported` hold. Original ordinary types, timestamps,
comments/notes, assignments, flags, metadata, and unknown fields remain untouched:
there is no update call. No assignment, rule, goal, or account rename endpoint
is written. New activities choose ordinary `DEPOSIT`/`WITHDRAWAL` or card
`CREDIT`/`WITHDRAWAL`, never a transfer type. Exact economics may still be
adopted read-only regardless of `isUserModified`; that flag does not prove
human or automated authorship.

The correction predecessor is the source event/revision recorded at a verified
**adoption or APPLIED acknowledgement**, not the latest accepted intention.
Planning a held or unsent correction cannot advance that delivered basis.
Bootstrap adoption of the existing baseline is preserved even if the proposed
batch is held. Legacy verified projection bindings can reference their original
immutable generation event without inventing a historical accepted revision
number. Held/replanned amount and date-only corrections therefore still compare
against the actual delivered amount and timestamp.
Held qualifications expose that delivered source basis separately from the
newly accepted source intent and identify the `create-only-v1` write contract.
A safe independent create can proceed only if the whole eligible batch still
reconciles. In source-anchor mode, any required transition needing an update
holds the entire fixed financial set; opposite deltas cannot justify sending
only the create half.

The pinned activity GET DTO uses `date`, `comment`, and explicit
`status` (`POSTED`, `PENDING`, `DRAFT`, `VOID`), not `isDraft`. Only `POSTED`
affects the cash calculation. Writes use `activityDate` and explicit `POSTED`
for creates. Account archival is checked using explicit `isArchived`; missing
or unsupported lifecycle/archive values are not defaulted. Unknown activity
**and account** fields remain in read/prestate preservation. These lifecycle
values are also defined by the upstream
[ActivityStatus model](https://github.com/wealthfolio/wealthfolio/blob/main/crates/core/src/activities/activities_model.rs).

Cash reconciliation starts with the **actual scoped app cash ledger**, including
any already-existing reviewed compensation. Pending/draft effects are excluded.
It predicts the result of every eligible operation together and compares it
exactly with the fresh raw source balance. A mismatch holds the account batch.
No subset search, sign guessing, gap adjustment, or global recalculation is used.
Normal activity API calls emit the app's own scoped domain events.

## Transactions, attempts, and recovery

Accepted revisions, source versions/sightings, qualifications, and pending
operations commit in one PostgreSQL transaction. The external API side effect
happens afterward. A session-level global writer lock spans those separate
commits and API calls, and the shared migration gate is checked in the existing
lock order. There is no expiring lease that another worker can steal while an
old HTTP request is still in flight.

Before financial changes the worker downloads and verifies a unique app backup,
then rechecks exact account prestate. Each operation has a committed `prepared`
attempt before the API call and a separate observed `applied` acknowledgement.
If the process dies after app commit, the next worker reads the actual app
state and can acknowledge the proven result without sending it again—even if
the collector has since advanced.

A prepared/uncertain attempt whose exact poststate is not observable is **not
resent**, including when the target is absent. It remains uncertain for operator
investigation. Fresh source evidence is required before any *new* HTTP send.
Unattempted work from an advanced/expired source receipt is held for replanning.
No automated rollback writes over intervening user work.
Previously queued, unacknowledged update-shaped intents are rejected before
backup or financial API dispatch. Already prepared/uncertain update intents
stay uncertain for separate investigation, never retried or silently
acknowledged by this release. Historical journal shapes remain readable.

## Read models and limitations

- `finance_read.incremental_scope_status`: latest account-run status/reason and
  the last known admitted source/balance timestamps and value.
- `finance_read.incremental_cash_events`: scoped materialized source fields,
  qualification proof/reason, financial observation time, last sighting time,
  current collection/balance evidence, actual app ID, projection status, and
  the delivered source revision/event separately from accepted source intent.
- `finance_read.incremental_source_history`: immutable financial versions,
  held raw changes, original observation time, and separate last sighting time.
- `incremental_outbox`, `incremental_attempts`, and `incremental_run_events`:
  append-only delivery and recovery evidence.
- `incremental_projection_observations`: source-revision-linked, append-only
  verified adoption/APPLIED evidence used as the next correction's predecessor.
- Source-anchor mode also exposes `historicalBackfillCount` and its checkpoint
  hash separately from cash delivery status. A reconciled/no-op cash run does
  not claim those historical details have been projected.

The current-events view retains the last interpreted scope when a newer source
read fails; read it together with scope status. A successful source read is not
an app delivery success. An `applied` receipt certifies only that run's eligible
operations and observed cash postcondition; `heldCount` and local qualification
rows remain visible. It does not declare all history fixed.

## Verification

The [focused suite](../../tests/test_incremental.py) uses the real canonical
producer/verifier, resolver, all PostgreSQL migrations and provisioning SQL, and
the actual authenticated `WealthfolioClient` against a synthetic REST service.
It uses uniquely named disposable PostgreSQL resources and modeled external
synthetic files, with cleanup. It accepts no private database DSN.

```powershell
$env:FINANCE_INCREMENTAL_POSTGRES_TEST = '1'
python -m pytest tests\test_incremental.py -q
python -m ruff check finance_store\incremental*.py tests\test_incremental.py
```

The synthetic service uses the real DTO field shape and genuine schema-2
non-production ownership checks, without bypassing `require_writer`. It models
the observed **lossy** 3.7 update contract, including omitted-comment clearing
and the automation-induced `isUserModified` change. Worker regressions prove
those updates are held before sending, even for otherwise valid historical
outbox intents. This does not prove behavior of a deployed app version.
Before activation, independently exercise the pinned account's creates and
read-only adoption, backup, timezone, native role grants, source health,
exact bootstrap evidence, and whole-batch cash reconciliation. Existing-row
corrections remain a separate unsupported maintenance requirement.
This implementation and its tests do not activate any runtime writer or task.
