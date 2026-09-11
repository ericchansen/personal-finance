# Local SimpleFIN sync

Run Wealthfolio, keep the SimpleFIN access URL and Wealthfolio password in the existing private data directory, and run:

```powershell
python -m importers.simplefin.local_sync --data-dir "<your-finance-data>"
```

This is a direct local importer. It does not require PostgreSQL, canonical publications, approval files, backups, or a release installation. The existing guarded commands are unchanged; this command explicitly uses the local API client.

## Setup

Use Python 3.11 or newer. On Windows, install the IANA timezone database with `python -m pip install tzdata` (also included in `requirements.txt`). The direct sync otherwise uses the Python standard library.

Use `<data>\simplefin\account-map.json`, the same version-1 map used by the collector. Each source account must have an explicit disposition. Keep household-specific configuration out of this public repository.

For cash and credit-card entries, `wealthfolioAccountId` names the existing app account. Set `historyThrough` to the last calendar day owned by the old historical importer. SimpleFIN owns posted transactions after that date; do not keep another importer writing that same period.

```json
{
  "version": 1,
  "accounts": {
    "synthetic-source-account": {
      "action": "import",
      "wealthfolioAccountId": "synthetic-app-account",
      "historyThrough": "2026-01-31"
    }
  }
}
```

For an empty account, choose a date before the first transaction you want. SimpleFIN only supplies its recent history window, not complete lifetime history. Keep existing historical imports.

If a previous importer already delivered transactions after the boundary, add `existingActivities` to that account entry: an object mapping each exact SimpleFIN transaction ID to its existing Wealthfolio activity ID. This is a one-time mapping, not fuzzy merchant matching. Legacy `simplefin:<app-account-id>:<transaction-id>` keys are recognized automatically.

The importer keeps the app's existing account names. Set names from the private account facts when first creating accounts; do not copy aggregator decorations into the app.

## Routine behavior

- Fetch 45 days of overlapping data. Posted transactions use stable, account-scoped provider IDs. Pending transactions wait until posted; two identical purchases with distinct IDs remain two purchases.
- Replaying transactions is a no-op. Provider corrections update importer-owned rows, preserving unrelated metadata, categories, and edited notes. A conflicting manual amount/date edit is reported instead of overwritten. An older saved response cannot replace the last sync.
- Source dates display on the correct day in the app's configured timezone, including daylight-saving transitions.
- Institution errors are reported without discarding healthy institutions. Missing accounts are not zeroed out, and old source balances retain their actual observation date.
- Cash accounts use native `HOLDINGS` snapshots for reported balances while retaining their real transactions for spending. This avoids inventing income or expenses to force a balance.
- Credit cards retain transaction tracking. A reusable positive `TRANSFER_IN` reconciliation is neutral in native spending. A required negative adjustment is an error: import the missing charges or correct the historical data rather than manufacture an expense. Wealthfolio 3.7 cannot represent a neutral negative card adjustment through its API.
- Unambiguous equal/opposite transfers are linked without changing their posting dates. Explicit account destinations must agree; cross-day links require routing evidence. Unmatched cash legs are reported because Wealthfolio includes them in income/spending until a counterpart is linked. The importer does not invent counterpart transactions.

## Investments and loans

The original SimpleFIN parser discarded `holdings`. The local sync imports the provider's actual position quantities and market values using Wealthfolio's native holdings snapshots. Investment accounts use `HOLDINGS` tracking; their existing transaction history is retained, but the importer does not invent trades from investment cash movements.

Each provider position has its own stable, manually priced asset. The account-qualified symbol is intentional: providers round share counts and can report different implied prices for the same ticker in two accounts. Reusing one global quote changes the other account's value. Independent position prices preserve both the reported quantities and account totals.

Known average cost is retained and provider purchase prices are used when supplied. Missing cost basis remains unknown; account values are not a claim of complete tax basis or reliable investment-performance history. If SimpleFIN supplies only an account total, it is labeled as a reported-value position, not presented as cash or a fabricated security purchase.

An existing loan maps through `wealthfolioAlternativeAssetId`. Source debt updates its native liability valuation using the actual observation date. A failed bank connection keeps its last reported value and produces a warning; reconnect it in SimpleFIN to obtain fresh data.

The command waits for actual native balances to agree with the source, including Wealthfolio's asynchronous recalculation after changing tracking mode. A mismatch is an error, not a successful plan. For an existing installation, remove obsolete generated `gap:` / `rebuild:assertion:` entries after identifying them as bookkeeping rather than bank transactions; do not let old balance adjustments masquerade as spending or import them a second time.

Preview without changing the app:

```powershell
python -m importers.simplefin.local_sync --data-dir "<your-finance-data>" --dry-run
```

Replay a saved response without making another SimpleFIN request:

```powershell
python -m importers.simplefin.local_sync --data-dir "<your-finance-data>" --snapshot "<saved-response.json>"
```

The private `simplefin\local-last-sync.json` records the result. `local-preview.json` is separate, so previews do not overwrite the last real run. An account-level failure produces a nonzero exit status.

## Run daily on Windows

Disable the old SimpleFIN collector and incremental writer tasks first. Keep only one daily writer:

```powershell
.\importers\simplefin\install-local-task.ps1 -DataDir "<your-finance-data>"
```

The task runs directly from this checkout, ignores overlapping starts, and runs a missed invocation when the computer becomes available. Keep the checkout at that path. No service, container, or additional database is needed for the importer.
