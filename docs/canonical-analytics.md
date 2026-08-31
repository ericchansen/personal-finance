# Canonical analytics

Wealthfolio remains an unmodified upstream renderer. Household history that the
upstream data model cannot represent truthfully is generated under the private
finance-data directory:

```powershell
python -m importers.analytics.cli build --data-dir <finance-data>
python -m importers.analytics.cli verify --data-dir <finance-data>
```

The generator first runs the canonical publication verifier, including its
schema, source hashes, data hashes, row counts, and CSV shapes. It then writes a
complete immutable version under `normalized/analytics/publications/<sha256>`
and atomically updates `normalized/analytics/current.json`. A reader resolves
the pointer before opening any output file. Interrupted builds leave the
previous pointer valid and never expose a partial publication. Old versions are
retained for recovery. File contents and each ordered directory rename are
flushed to durable storage before the current pointer advances. On first use,
each newly created hierarchy level is also persisted into its parent, from the
deepest entry back to the first existing directory. The generator never reads
the production Wealthfolio
database or writes financial data into this repository.

## Output contract

| File | Contract |
|---|---|
| `monthly-analytics.csv` | Monthly net worth, investable assets, liabilities, property equity, cash flow with explicit availability, and component values |
| `investment-performance.csv` | Modified Dietz periods only where source-backed endpoint valuations exist |
| `metadata-review.json` | Stable asset/account identifiers, reviewed classifications, citations, and unresolved fields |
| `reporting-portfolios.json` | Proposal-only reporting portfolios; never mutates Wealthfolio |
| `manifest.json` | Canonical source/output SHA-256 hashes, coverage counts, and methodology limitations |

`cash_flow_status` is `available` only when every active cash and credit-card
account has at least one canonical transaction as evidence for that month.
Otherwise `cash_flow` is blank and `data_quality` is `partial`; the generator
never turns missing transaction evidence into a zero. A zero is emitted only
for an evidenced month whose included non-transfer transactions net to zero.
Coverage uses calendar-month overlap with each account's open/closed interval,
so an account opened or closed mid-month still requires evidence for the active
part of that month.

Account values are used only in months with a canonical observation and are
never carried into another month. Property values are interpolated only between
observed values and are zero outside the ownership window. Fixed-rate
liabilities are amortized from fact-backed terms and become zero at payoff.
Those rules keep sold property and retired loans out of current net worth
without creating fake Wealthfolio activities.

Optional reviewed metadata belongs at
`<finance-data>/plans/analytics-metadata.json`:

```json
{
  "schemaVersion": 1,
  "entries": [
    {
      "id": "asset:SYN",
      "type": "asset",
      "reviewed": true,
      "values": {
        "assetClass": "Synthetic equity",
        "region": "Synthetic region",
        "sector": "Synthetic sector"
      },
      "citations": [
        {
          "source": "Synthetic issuer profile",
          "url": "https://example.com/synthetic-profile"
        }
      ]
    }
  ]
}
```

The generator rejects classifications without a review flag and at least one
source citation. Local `sourcePath` citations must stay under the private data
directory; their SHA-256 hashes are added to the review and manifest. Account
entries use `account:<canonical-account-id>` and may review `owner`,
`taxBucket`, `retirement`, and `investable`.

## Wealthfolio performance diagnosis

Wealthfolio 3.7.0 marks TWR unavailable when a daily valuation has unknown
external-flow provenance, then nulls the headline when any fatal reason exists
([upstream calculation](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/crates/core/src/portfolio/performance/performance_service.rs#L550-L730)).
XIRR follows the same unknown-flow guard
([upstream calculation](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/crates/core/src/portfolio/performance/performance_service.rs#L1218-L1243)).
The API can still return cumulative points, but the frontend admits a
time-weighted chart only when headline TWR is non-null
([upstream chart gate](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/apps/frontend/src/pages/performance/performance-chart-series.ts#L21-L46)).

Run the read-only production diagnostic to preserve the affected dates,
warnings, image revision, and response shape privately:

```powershell
python -m importers.analytics.cli diagnose-wealthfolio `
  --data-dir <finance-data> `
  --upstream-version 3.7.0 `
  --upstream-revision 5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6 `
  --image-digest <deployed-image-digest>
```

Diagnostics use the same immutable-publication pattern under
`normalized/analytics-diagnostics/wealthfolio-performance`. Each response is
stored by its SHA-256 digest and `current.json` is updated atomically; analytics
rebuilds neither replace nor delete diagnostic history.

Unknown transfer boundaries are data-fixable only after source-backed review:
link true internal pairs or classify genuine external transfers, then recalculate
valuations. The all-or-nothing headline/chart behavior is upstream.

## Addon boundary

This change intentionally stops at a portable data contract. The addon SDK
supports host analytics and user-driven file dialogs, while arbitrary prepared
local-file reads are not an addon permission
([SDK permissions](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/packages/addon-sdk/src/permissions.ts#L53-L211)).
A production addon therefore needs a deliberate private publication transport
and refresh/authentication design. Once that exists, it should render the files
above through `@wealthfolio/addon-sdk`; it must not reinterpret Wealthfolio's
forward-filled alternative-asset history.
