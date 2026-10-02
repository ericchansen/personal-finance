# Local SimpleFIN sync

The sync runs in the `sync` container of `compose.yml`, beside Wealthfolio. It fetches SimpleFIN once a day and updates the app through its local API. It needs no database or service beyond Wealthfolio itself.

Run the commands below from this repository after defining this PowerShell shorthand. Inside the container the data directory is `/finance`.

```powershell
function dc { docker compose --env-file "<your-finance-data>/compose.env" @args }
```

## Setup

1. Create the private settings file and start the stack:

   ```powershell
   pwsh deploy/init-env.ps1 -DataDir "<your-finance-data>"
   dc up -d --build
   ```

2. Connect SimpleFIN once with a setup token from the SimpleFIN Bridge. Claiming consumes the token and stores the access URL in `<data>\simplefin\access-url.txt`:

   ```powershell
   dc run --rm --no-deps sync python -m importers.simplefin.cli claim --token "<setup-token>"
   dc run --rm --no-deps sync python -m importers.simplefin.cli accounts
   ```

3. Create `<data>\simplefin\account-map.json` as described below, preview with a dry run, then run the first real sync by hand. The daily schedule starts only after that first sync, so a new mapping is always previewed before anything is written:

   ```powershell
   dc run --rm sync python -m importers.simplefin.local_sync --dry-run
   dc run --rm sync python -m importers.simplefin.local_sync
   ```

Create `<data>\simplefin\account-map.json` (version 1), keyed by SimpleFIN account ID. Every source account needs an entry; an unmapped account is reported as an error. Use `"action": "exclude"` for accounts that should not sync. Keep household-specific configuration out of this public repository.

For cash and credit-card entries, `wealthfolioAccountId` names the existing app account. Set `historyThrough` to the last calendar day already covered by earlier imports. SimpleFIN owns posted transactions after that date; do not keep another importer writing that same period.

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

The importer keeps the app's existing account names. Name accounts in Wealthfolio when creating them; do not copy aggregator decorations into the app.

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

Known average cost is retained and provider purchase prices are used when supplied. Missing cost basis remains unknown; account values are not a claim of complete tax basis or reliable investment-performance history. If SimpleFIN supplies only an account total and there are no known securities, it is labeled as a reported-value position, not presented as cash or a fabricated security purchase. Missing position data never replaces known securities with that aggregate: the account reports an error and retains its previous holdings. An explicitly empty position list with a zero balance can clear an empty account.

An existing loan maps through `wealthfolioAlternativeAssetId`. Source debt updates its native liability valuation using the actual observation date. A failed bank connection keeps its last reported value and produces a warning; reconnect it in SimpleFIN to obtain fresh data.

The command waits for actual native balances to agree with the source, including Wealthfolio's asynchronous recalculation after changing tracking mode. A mismatch is an error, not a successful plan. For an existing installation, remove obsolete generated `gap:` / `rebuild:assertion:` entries after identifying them as bookkeeping rather than bank transactions; do not let old balance adjustments masquerade as spending or import them a second time.

Preview without changing the app:

```powershell
dc run --rm sync python -m importers.simplefin.local_sync --dry-run
```

Replay a saved response without making another SimpleFIN request:

```powershell
dc run --rm sync python -m importers.simplefin.local_sync --snapshot "/finance/raw/simplefin/<date>/<saved-response>.json"
```

The private `simplefin\local-last-sync.json` records the result. `local-preview.json` is separate, so previews do not overwrite the last real run. An account-level failure produces a nonzero exit status. The module also runs on any Python 3.11+ with `FINANCE_DATA` and `WEALTHFOLIO_PASSWORD` set.

## Schedule and alerts

The `sync` container runs the sync at the first check after `SYNC_AT` (default `06:00` in `TZ`), once a day, after the first manual sync. It checks every five minutes, so a computer that slept through the scheduled time catches up when it wakes. A crash retries hourly, replaying the day's saved SimpleFIN response instead of requesting another, and retries stop requesting new data after half of SimpleFIN's 24 daily requests. A run that finishes with account errors waits for the next day. The sync signs in to Wealthfolio before requesting SimpleFIN data, so an unavailable app costs no requests. These limits are kept in the data directory, so they survive container restarts.

Docker marks `wealthfolio-sync` unhealthy when the last sync is more than 26 hours old or reported errors. Check it with `docker ps` or `dc logs sync`.

For an alert that also fires when the computer or Docker is off, create a check at [healthchecks.io](https://healthchecks.io) (or a self-hosted Healthchecks) with a one-day period, and set `SYNC_PING_URL` in `compose.env` to its ping URL. Each run sends only its exit status: `0` is success, anything else is a failure. A missing ping alerts after the grace period. Apply the change with `dc up -d`.

## Upgrade or move

Everything private lives in the data directory: `compose.env`, the SimpleFIN files, and Wealthfolio's database. To move machines, copy that directory and run `dc up -d --build` from a checkout of this repository. To update the code, pull and run the same command.
