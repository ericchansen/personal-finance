# Canonical normalized data

This package builds an app-independent system of record in the private finance
data directory. It never writes to raw inputs or Wealthfolio.

```powershell
python -m importers.normalized.cli plan --data-dir D:\documents\finance-data
python -m importers.normalized.cli build --data-dir D:\documents\finance-data
python -m importers.normalized.cli verify --data-dir D:\documents\finance-data
```

`plan` parses and validates every supported input without writing. `build`
writes `normalized/canonical/{accounts,transactions,positions,valuations}.csv`
with an atomic directory replacement, plus `manifest.json`. `verify` checks
source and output hashes, schemas, references, numeric/date invariants, and a
fresh byte-for-byte recomputation.

The CSV files are deterministic. The manifest intentionally is not: its build
timestamp records when the atomic snapshot was published. Source paths and
hashes preserve lineage. Cross-source lookalikes are retained and reported as
ambiguities unless a durable fact makes an explicit decision.

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

Canonical transaction schema version 2 adds `symbol`, signed `quantity`,
`price`, and `external_flow`. Position basis is emitted when the complete
history supports it; it is left blank after an external in-kind arrival whose
original basis is not known.
