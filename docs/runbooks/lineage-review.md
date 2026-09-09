# Private lineage review

Use this workflow after a verified forensic duplicate audit to turn candidate
evidence into durable human decisions. It is read-only with respect to
Wealthfolio: there is no ledger transport and no command that applies, deletes,
or updates activities.

All queue packets, reviewer identities, rationales, and evidence bindings are
private. `--data-dir` must point outside this public repository.

## Build the review queue

```powershell
python -m importers.lineage_review.cli build --data-dir <finance-data>
python -m importers.lineage_review.cli status --data-dir <finance-data>
```

The builder verifies the current evidence baseline and forensic publication
before reading either. It then creates immutable, content-addressed output:

```text
<finance-data>/audit/lineage-review/
  current.json
  publications/<manifest-sha256>/
    manifest.json
    queue.json
    summary.json
    summary.md
    packets/<priority>-<batch>-<hash>.json
    packets/<priority>-<batch>-<hash>.md
```

Packets contain private activity, account, date, amount, description,
dependent-state, and evidence details needed for review. The console and
summary files contain only fixed status codes, counts, and SHA-256 values.
Never copy a packet or decision file into this repository.

Queue order is deterministic:

1. Extract and SimpleFIN one-to-one duplicate candidates
2. Other one-to-one duplicate candidates
3. Remaining one-to-many duplicate candidates
4. Remaining many-to-many duplicate candidates

Transfer-only graph components are relationship evidence, not duplicate
remediation work, and are not placed in this queue. Legacy or mixed groups may
still carry a reviewed `linked-transfer` decision.
6. Ambiguous receipt or reconciliation bindings

Within a priority, higher dependent-state risk is reviewed first, followed by
stronger evidence and the stable group ID. Exact provider source identity can
be recommended as evidence, but it is never accepted as a decision by the
review workflow.

## Record decisions

Copy decision templates from a packet into one private import document that
conforms to
`importers/lineage_review/decision-schema.json`. Supported rulings are:

- `same-economic-event`
- `distinct-events`
- `linked-transfer`
- `account-handoff-reissue`
- `defer-insufficient-evidence`
- `source-error-mirror`
- `receipt-binding-resolution`

Every ruling binds the reviewer, decision timestamp, rationale, environment,
baseline and forensic publication IDs, complete candidate graph and group
hashes, member and dependent-state fingerprints, and reviewed evidence
hashes. Suppression-shaped rulings select a survivor. Transfer rulings list
every linked member. Receipt rulings select one candidate receipt and bind any
required reconciliation fingerprints.

Additional private evidence can be listed in `evidenceFiles` with its relative
path, size, kind, SHA-256, and `coversCandidateGroupIds`. Rollback readiness
accepts only a sealed backup manifest, rollback plan, rollback receipt, or an
embedded rollback payload bound to the exact candidate group.
Generated canonical, staging, backup, review-publication, and lock-state paths
cannot serve as decision evidence; evidence must remain independent of the
publication it authorizes.
Legacy baseline manifests may retain a canonical predecessor hash for
historical binding, but that hash is non-citable and cannot satisfy decision or
remediation-readiness evidence.

Import and verify:

```powershell
python -m importers.lineage_review.cli import `
  --data-dir <finance-data> `
  --input <finance-data>\audit\.lineage-state\decision-imports\lineage-decisions.json
python -m importers.lineage_review.cli verify --data-dir <finance-data>
```

The import document must be inside
`audit/.lineage-state/decision-imports/`. This operator inbox is excluded
from baseline inventory so drafting a decision cannot invalidate the queue it
binds. It is not an evidence source, and generated state under this tree cannot
satisfy decision or rollback evidence.

Imports are serialized, append-only, hash-chained, and immutable. Re-importing
an identical ruling is idempotent. A ruling may later add evidence,
reconciliation attribution, or rollback proof without changing its reviewed
classification. A different ruling for an already claimed group, a second
claim over the same member and authority domain, a history rollback or fork,
or a changed external evidence file fails closed.

Queue replacement uses the same lock. Once any decision history exists, a
different queue publication (including a different batch size) is refused
rather than stranding or silently rebinding reviewed rulings. Build a fresh
forensic and queue lineage only through an explicit future migration design.
The shared lock uses an OS-held advisory lock under
`audit/.lineage-state`; a crashed process releases it automatically. A durable
pending-publication journal completes or abandons an interrupted pointer swap
under that lock before later work begins.

Validation also rejects missing members, changed cardinality, reason or source
families, stale publications or graph hashes, a survivor outside its group,
omitted transfer members, dependent-state drift, a foreign environment, and
placeholder reviewer metadata.

## Canonical publication

Canonical schema version 5 adds:

- `transaction-observations.json`, preserving every normalized source
  observation and replay;
- `transaction-lineage.json`, publishing stable canonical transaction
  identities and explicit preservation, suppression, defer, or link
  projections.

Unreviewed cross-source lookalikes remain separate. A reviewed same-event,
source-mirror, or account-handoff ruling selects one canonical survivor while
the suppressed source observations remain in the observation publication.
Reviewed transfers retain both legs and receive an explicit stable link.
Existing private fact decisions remain supported; overlapping transfer or
handoff authorities are rejected.

This is canonical publication only. It does not mutate Wealthfolio.

## Remediation readiness

`status` reports aggregate ready, restore-eligible, surgical-eligible, and
rebuild-eligible group counts. A group is ready only when its reviewed ruling,
proved source lineage, survivor when applicable, required reconciliation
attribution, complete linked and dependent graph, and rollback evidence are
all present.

`defer-insufficient-evidence` is durable but never remediation-ready. Missing
source lineage or rollback proof remains an evidence gap; the workflow does
not infer either from fuzzy descriptions, prefixes, dates, or equal amounts.
