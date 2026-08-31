# Ledger Live (local Bitcoin cache)

Ledger Live keeps a read-only JSON cache at:

```text
C:\Users\<username>\AppData\Roaming\Ledger Live\app.json
```

The importer reads that file directly and never modifies it. It makes no
network requests. Cached operations are preferred over a block explorer because
they preserve local-first operation and do not disclose wallet identifiers to a
third party.

## Security boundary

- Never copy `app.json` into this repository.
- Never print, log, or commit an xpub. An xpub cannot spend funds, but it reveals
  the account's addresses and transaction history.
- Never read or handle a recovery phrase, seed, or private key. This workflow
  does not need them.
- Normalized records omit xpubs, addresses, seed identifiers, and raw Ledger
  account IDs. The latter may itself contain an xpub.
- Exports are accepted only below
  `D:\documents\finance-data\facts` or `D:\documents\finance-data\raw`.

## Commands

Run these from the repository root:

```powershell
# Safe local summary; does not display wallet identifiers
python -m importers.crypto.cli summary

# Offline data-quality check
python -m importers.crypto.cli assert-current

# Normalize the balance, account metadata, and cached history
python -m importers.crypto.cli export
```

The export defaults to
`D:\documents\finance-data\raw\ledger-live-normalized.json`. Use
`export --output <path>` only for another path below the two approved private
directories. Export first asserts that successful cached inflows minus outflows
equal the current balance in `app.json`.

All integer amounts ending in `_sat` are satoshis:

```text
1 BTC = 100,000,000 sat
1 sat = 0.00000001 BTC
```

This eight-order-of-magnitude conversion is exact and uses decimal arithmetic,
not binary floating point. Operations are deduplicated by Bitcoin transaction
ID (`txid`).

## Optional automation

`importers.crypto.explorer.BlockExplorerAdapter` defines an integration boundary
for a future explicitly configured explorer. No adapter is implemented or
called by these commands. Adding one is a privacy decision: deriving or querying
addresses can let the provider associate the wallet's history with the client.
Keep local cached operations as the default, never log adapter inputs, and do
not enable a provider implicitly.

## Troubleshooting

- **No accounts:** an empty Ledger Live account list is valid and produces an
  empty export.
- **Malformed data:** close and reopen Ledger Live so it can rewrite its cache,
  then retry. Do not hand-edit `app.json`.
- **Reconciliation failure:** cached history may be incomplete or the cache may
  be mid-refresh. Open Ledger Live, allow it to synchronize, close it cleanly,
  and rerun `assert-current`. The importer refuses to export a mismatched
  snapshot rather than silently inventing history.
