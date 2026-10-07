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

## Categories and transfers

Categorization is Wealthfolio's job; this repository adds no categorization code. After each sync, Wealthfolio applies its own rules to new, uncategorized activity in the accounts selected in Spending settings. Rules never overwrite a category you chose by hand, and they live in Wealthfolio's database, so its backups include them.

- **Accounts.** Select every cash and card account that should count as household spending in Spending settings. Unselected accounts are never auto-categorized.
- **Frequent merchants.** Add a rule for each frequent merchant, matching a distinctive fragment of the description. A handful of merchants usually covers a large share of transactions; categorize the rest by hand.
- **Long tail.** Wealthfolio's built-in assistant can draft a rule from a hint such as "coffee shops are Food / Coffee"; it saves nothing until you approve the draft. The assistant sends transaction text to its AI provider, so use only a local [Ollama](https://ollama.com) provider. Wealthfolio runs in Docker, so set the provider URL to `http://host.docker.internal:11434` on Docker Desktop, not `localhost`.

Transfers between synced accounts are linked automatically when both legs are unambiguous. `unmatchedTransferCount` in `local-last-sync.json` counts the transfer legs still unlinked, which Wealthfolio counts as income or spending. Link each pair in the app; if the money really left your accounts, change the activity's type instead.

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

Wealthfolio is pinned to 3.9.1. To upgrade it, change the version and multi-platform digest in `compose.yml` and run the integration test, which starts that image with synthetic data and runs the sync twice:

```powershell
pip install pytest argon2-cffi
$env:WEALTHFOLIO_IT = "1"; python -m pytest
```

CI runs the same test on every pull request. Deploy only after it passes:

1. Pull the replacement image with `dc pull wealthfolio`. If `compose.env` sets `WF_IMAGE`, remove the override or set it to the tested version and digest.
2. Stop both writers with `dc stop sync wealthfolio`.
3. Back up the entire `<data>\wealthfolio` directory and `compose.env` to a timestamped location outside the repository. Keep the existing image available for rollback. Copying only `wealthfolio.db` is insufficient: 3.9 adds separate profile databases, and the master key in `compose.env` is also required.
4. Start the stack with `dc up -d --build`, then check `dc ps` and sign in to confirm the existing accounts are present.

The 3.9 upgrade keeps the existing `WF_DB_PATH` and `/data` mount; existing data appears in a Personal profile. Database migrations run automatically on startup. Encryption remains optional; do not enable `WF_DB_REQUIRE_ENCRYPTION` without first performing the documented offline conversion. Update all devices to 3.9.1 before pairing device sync.

The upstream final-cash migration can rewrite legacy activity amounts and flag ambiguous rows for review. Rewritten amounts retain their original values in `final_cash_migration.legacy_amount` metadata. Review flagged activities in Wealthfolio; a raw activity fingerprint need not remain identical across this migration.

If startup or the sync fails, stop both containers before restoring the complete pre-upgrade Wealthfolio directory and its matching secrets, then start the previous pinned image. Never point an older image at a migrated database.
