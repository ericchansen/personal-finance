# Canonical normalized data

This package builds an app-independent system of record in the private finance
data directory. It never writes to raw inputs or Wealthfolio.

```powershell
python -m importers.normalized.cli plan --data-dir D:\documents\finance-data
python -m importers.normalized.cli build --data-dir D:\documents\finance-data
python -m importers.normalized.cli verify --data-dir D:\documents\finance-data
```

`plan` parses and validates every supported input without writing. `build`
writes `normalized/canonical/{accounts,transactions,positions,valuations}.csv`,
`transaction-observations.json`, and `transaction-lineage.json` with an atomic
directory replacement, plus `manifest.json`. `verify` checks source and output
hashes, schemas, references, numeric/date invariants, lineage bindings, and a
fresh byte-for-byte recomputation.

The data files are deterministic. The manifest intentionally is not: its build
timestamp records when the atomic snapshot was published. Source paths and
hashes preserve lineage. Cross-source lookalikes are retained and reported as
ambiguities unless a verified private lineage decision selects a canonical
survivor or link. Stable same-source replays remain idempotent, while every
source observation is retained in `transaction-observations.json`.
Byte-equivalent stable-ID replays from overlapping source files share one
canonical identity while retaining each file-specific observation.

Account identity is isolated in `IdentityMap`. Canonical IDs currently preserve
account IDs established by facts (including existing app UUIDs) so that this
mapping can be replaced later without changing source adapters.

The Vanguard adapter treats the archived per-account activity XLSX workbooks as
the authoritative transaction history. It requires cited, HIGH-confidence
resolutions from `plans/vanguard-in-kind-prices.json` for every otherwise
ambiguous in-kind row and blocks if shares or cash do not exactly reconcile to
the latest combined export. Internal recharacterizations retain a
`transfer_group`; external in-kind arrivals are zero-cash transactions marked
`external_flow=true`.

The old combined Vanguard CSV remains authoritative only for the latest
positions and account valuation. Its overlapping transaction table is never
imported, so it cannot double money or shares. Closed/unmapped workbooks require
an explicit `excludedAccounts` entry in the private Vanguard mapping with both
a stable decision identifier and reason. The manifest hashes every workbook,
mapping, resolution artifact, and combined assertion source and reports mapped,
excluded, and canonical Vanguard row counts.

Canonical schema version 5 adds immutable transaction-observation and
transaction-lineage publications. It retains schema version 4's
`tracking_mode` so balance-only holdings accounts are distinct from
transaction-ledger accounts, plus the version 3 transaction fields and version
2 investment fields
(`symbol`, signed `quantity`, `price`, and `external_flow`) and structural
cash-flow semantics:

- `transaction_kind` distinguishes consumption, income, refunds, transfers,
  card and loan payments, saving, reconciliation, investment activity, and
  exclusions without relying on category names.
- `category_id` is a stable normalized identity separate from the source-facing
  `category` label.
- `payee_normalized` preserves a deterministic comparison value while
  `description` retains the source text.
- `assignment_source`, `assignment_rule_id`, and `assignment_confidence`
  preserve categorization provenance.
- `split_group` identifies exact monetary split lines; transfers and
  reconciliation rows cannot also be category splits.

Existing version 4 publications remain verifiable as sealed forensic inputs,
but a canonical `verify` recomputation requires rebuilding them into version 5.
Canonical output is downstream of evidence baselines and lineage decisions; it
is deliberately excluded from subsequent baseline inventories so publishing a
reviewed canonical projection cannot invalidate the evidence that authorized
it.
Existing version 3 publications are intentionally not upgraded in place because
two incompatible version 3 layouts existed. After all consumers support version
4, rebuild from the private source facts and extracts; the builder verifies the
new publication in a staging directory before atomically replacing the prior
canonical directory. Until then, version 4 consumers reject the old publication
instead of guessing which layout it uses.

## Reviewed transfer candidates

The canonical plan and manifest contain a private `transferReview` summary.
Candidates are proposals only: both legs must fall within the ownership dates
of non-excluded canonical accounts, use different accounts, have exactly
opposite `Decimal` amounts and the same currency, and occur within five calendar
days. Existing transfer groups, reconciliation gaps, external flows, security
activity, and zero amounts are excluded.
Multiple exact matches are marked `ambiguous`; no candidate, ambiguous or
otherwise, is confirmed automatically.

After reviewing a proposal, add a private decision fact using the exact
`candidateId` and `evidenceHash` emitted by the plan:

```json
{
  "type": "decision",
  "id": "reviewed-transfer-example",
  "decisionType": "transfer-confirmed",
  "candidateId": "transfer-candidate-00000000000000000000",
  "evidenceHash": "0000000000000000000000000000000000000000000000000000000000000000",
  "resolution": "CONFIRMED after reviewing both synthetic legs.",
  "evidence": "Synthetic example evidence.",
  "decidedOn": "2026-01-15",
  "affects": ["acct-example-one", "acct-example-two"],
  "source": "private review",
  "sourcePath": null,
  "notes": "Synthetic example only."
}
```

Use `transfer-rejected` for a rejected proposal. A matching rejection suppresses
the same candidate on every rebuild. If a leg's date, amount, currency,
description, category, source, or flow status changes, its evidence hash changes
and the proposal returns with `status: evidence-changed`. A confirmation creates
a stable transfer group derived from the decision ID and remains idempotent.
Stale decisions remain visible in the summary rather than being silently used.

## Exact category splits

A reviewed `category-split` decision replaces one ordinary transaction with
exact monetary child rows. It identifies one `sourceId` and at least two
categories. Amounts are parsed and summed as `Decimal`; percentages are never
stored. All amounts must be nonzero and have the parent's direction. Either
provide every exact child amount or mark exactly one child as the deterministic
residual:

```json
{
  "type": "decision",
  "id": "reviewed-split-example",
  "decisionType": "category-split",
  "sourceId": "synthetic:transaction",
  "resolution": "Reviewed exact synthetic allocation.",
  "evidence": "Synthetic example evidence.",
  "decidedOn": "2026-01-15",
  "affects": ["acct-example-one"],
  "splits": [
    {"amount": "-3.33", "category": "Example One", "categoryId": "example.one"},
    {"residual": true, "category": "Example Two", "categoryId": "example.two"}
  ],
  "source": "private review",
  "sourcePath": null,
  "notes": "Synthetic example only."
}
```

Child source IDs and the `split_group` are stable across rebuilds. The manifest
records the parent amount and exact child total so publication verification can
reconcile the group. Internal transfers, card payments, reconciliation,
investment/external-flow rows, and excluded rows fail closed if a split decision
targets them.

Position basis is emitted when the complete history supports it; it is left
blank after an external in-kind arrival whose original basis is not known.
