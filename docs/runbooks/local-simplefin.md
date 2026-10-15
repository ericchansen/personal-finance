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

Use the earlier import's transaction coverage, not its download date or balance observation date, to choose this boundary. An export can report a newer balance while containing transactions only through an earlier day.

For historical imports, verify the issuer's posting-date column separately from the purchase date or an export field name. Preserve the original transaction date as provenance when correcting the ledger date.

For timestamped bank transactions, set `transactionTimezone` to the institution's verified IANA financial timezone when needed. Omitting it retains the provider's UTC calendar day; the sync never guesses a bank timezone from the app or machine locale. The bank's after-midnight posting day can differ from the app's local day. The original UTC timestamp is retained while parsing and its UTC day remains in source metadata for comparisons.

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

Posted foreign-source activities after the boundary block that account until their ownership is resolved; the sync does not infer cross-source identity from descriptions. This catches conflicting ownership, not an incorrectly late cutoff or every missing transaction: historical coverage still needs evidence.

The mapped source must represent the same complete financial account as Wealthfolio. Mark a known additional-card view with `"sourceScope": "cardmember"`; the sync reports an error for that mapping instead of treating it as the primary billing account. The ordinary full-account value is `"account"`. Other accounts continue syncing; a blocked member view is not silently excluded or treated as zero debt.

Before requesting replacement exports, inspect the private original files, retained browser responses, and earlier account decisions. Missing processed output does not establish missing bank evidence; verify account scope, date columns, and pagination in the original source.

Historical duplicate cleanup must preserve native category assignments and splits, not just activity rows. Read `/spending/activities/{activityId}/assignments` for both copies and preserve supported assignments through the native API before deletion. An activity-row comparison alone does not cover that state; conflicting manual assignments require review.

The importer keeps the app's existing account names. Name accounts in Wealthfolio when creating them; do not copy aggregator decorations into the app.

## Routine behavior

- Fetch 45 days of overlapping data. Posted transactions use stable, account-scoped provider IDs. Pending transactions wait until posted; two identical purchases with distinct IDs remain two purchases.
- Replaying transactions is a no-op. Provider corrections update importer-owned rows, preserving unrelated metadata, categories, and edited notes. A conflicting manual amount/date edit is reported instead of overwritten. An older saved response cannot replace the last sync.
- Source dates display on the correct day in the app's configured timezone, including daylight-saving transitions.
- Balance observation timestamps use the app's timezone. Transaction financial days use the source's verified `transactionTimezone`, independently of the app timezone, or retain the provider UTC day when no source timezone is declared. Native activity timestamps display that financial day consistently while source metadata retains the UTC comparison day.
- Card balance comparisons retain the original UTC observation day so they use the same calendar basis as provider transaction days. Snapshot dates and card projection cutoffs are not interchangeable.
- Institution errors are reported without discarding healthy institutions. Missing accounts are not zeroed out, and old source balances retain their actual observation date.
- Missing source currency never defaults to USD. It can be established from explicit position currencies only when all positions have the same declared unit and their reported values exactly equal the account total. Otherwise the account reports unknown units and retains existing data. A destination account's configured currency does not prove source units.
- Cash accounts use native `HOLDINGS` snapshots for reported balances while retaining their real transactions for spending. This avoids inventing income or expenses to force a balance.
- Credit cards retain transaction tracking and import only real transactions. Balance mismatches are errors: reconcile missing charges, payments, credits, or historical data instead of creating balance-adjustment activities. Wealthfolio 3.10 does not support holdings tracking for credit cards. Legacy `simplefin-balance:` entries must be reviewed and removed after a backup; the sync never creates or resizes them, and reports their presence as an error.
- Transfer matching and linking use Wealthfolio's native tools. The importer does not guess counterparts or manufacture missing legs. Unmatched cash legs are reported because Wealthfolio includes them in income/spending until a counterpart is linked.
- Explicit `VENMO CASHOUT` bank credits use `TRANSFER_IN` so native matching can find them. Generic ACH credits are not presumed self-transfers, and bank-funded peer payments remain outflows. Link only when source destinations and payment purposes agree, not merely amounts and dates.
- Posted card repayments use native `TRANSFER_IN`, not refund `CREDIT`; punctuation in payment descriptions does not change that meaning. Pending payments are still excluded until the provider marks them posted.
- A linked transfer's two banks can report different dates and descriptions. For a date or description correction, the sync explicitly supplies both legs to the native bulk API so it does not mirror one bank's fields onto the other. Amount or type corrections unlink the pair for review instead of overwriting the counterpart's source facts.

## Categories and transfers

Categorization is Wealthfolio's job; this repository adds no categorization code. After each sync, Wealthfolio applies its own rules to new, uncategorized activity in the accounts selected in Spending settings. Rules never overwrite a category you chose by hand, and they live in Wealthfolio's database, so its backups include them.

- **Accounts.** Select every cash and card account that should count as household spending in Spending settings. Unselected accounts are never auto-categorized.
- **Frequent merchants.** Add a rule for each frequent merchant, matching a distinctive fragment of the description. A handful of merchants usually covers a large share of transactions; categorize the rest by hand.
- **Long tail.** Wealthfolio's built-in assistant can draft a rule from a hint such as "coffee shops are Food / Coffee"; it saves nothing until you approve the draft. The assistant sends transaction text to its AI provider, so use only a local [Ollama](https://ollama.com) provider. Wealthfolio runs in Docker, so set the provider URL to `http://host.docker.internal:11434` on Docker Desktop, not `localhost`.

`unmatchedTransferCount` in `local-last-sync.json` counts imported cash transfer legs still unlinked, which Wealthfolio counts as income or spending. Review candidate matches and link genuine pairs in Wealthfolio; if the money really left your accounts, classify the activity appropriately in the app.

## Investments and loans

The original SimpleFIN parser discarded `holdings`. The local sync imports the provider's actual position quantities and market values using Wealthfolio's native holdings snapshots. Investment accounts use `HOLDINGS` tracking; their existing transaction history is retained, but the importer does not invent trades from investment cash movements.

Each provider position has its own stable, manually priced asset. The account-qualified symbol is intentional: providers round share counts and can report different implied prices for the same ticker in two accounts. Reusing one global quote changes the other account's value. Independent position prices preserve both the reported quantities and account totals.

Native automatic Stock/Equity classifications are defaults, not verified issuer facts. Use Wealthfolio's native taxonomy assignments for source-backed corrections; manual assignments persist on the same scoped asset during replay. A money-market mutual fund remains a security with Money Market allocation exposure, not a bank deposit or an invented cash balance. Classification does not establish acquisition cost, guarantee a share price, or imply deposit insurance.

Existing/local search results describe stored metadata, not independent reference evidence. Match external identifiers exactly, retain a broad instrument type when its legal subtype is unverified, and leave unsupported underlying exposure unclassified rather than inventing asset-mix percentages. A depositary-receipt classification does not authorize rescaling provider share counts, prices, or basis.

Real syncs also request Wealthfolio's native incremental market refresh. Native market-priced instruments use their configured providers; manual-priced source positions are excluded by Wealthfolio and retain their reported prices. A read-only check does not request market refreshes.

Provider total cost basis is divided by the reported quantity for Wealthfolio's native average-cost input; a provided purchase price is used only when total basis is unavailable. Some providers put a per-share average cost in the total-basis field. Set `"sourceCostBasis": "perUnit"` on that account's map entry only after the values match the issuer's own transaction history. Existing average cost is retained only for the exact same source-scoped asset with unchanged quantity; matching names or tickers do not prove cost basis for different or additional shares. Missing cost basis remains unverified; account values are not a claim of complete tax basis or reliable investment-performance history. Missing position data reports an error and retains previous holdings; a nonzero account total never becomes a fake security or presumed cash.

When the provider reports no cost, an account entry can record issuer evidence in `verifiedCosts`, keyed by the source symbol (or description when there is no symbol). `{"unitCost": "1.00"}` suits a fund whose issuer history shows every purchase at one fixed price, such as a stable-NAV money-market fund; it survives dividend reinvestment. `{"quantity": "...", "costBasis": "..."}` records an issuer's dated total for an exact share count and applies only while the source still reports that quantity. Note the issuer document and date alongside it.

Normal syncs, previews, and read-only checks warn when a position has neither reported, recorded, nor retained acquisition cost, and when a recorded total no longer matches the source quantity. Wealthfolio represents an omitted snapshot cost as zero; that is not proof of a zero-cost acquisition, and its cost-based gains are not verified. Quantities and market values can still reconcile independently. A native calculation status of `complete` does not certify the provenance of its cost inputs.

Only an explicitly reported cash position (description `Cash` and no security symbol) becomes a native cash balance. Securities and reported cash must reconcile to the account total; an unexplained positive or negative remainder is not presumed cash. Cash positions must declare compatible units just like securities.

If the quantity of a source-scoped asset changes without provider basis, purchase price, or an
applicable recorded cost, the new holdings are published without cost (and the warning above
applies) rather than scaling the old basis onto new shares. Earlier snapshots keep the basis
that was valid on their dates. Pending, draft, and void activities do not
count as posted cash movements; a missing native card calculation is unavailable, not an
observed zero balance.

An aggregate account valuation does not establish cash, coin quantities, purchases, or acquisition cost. Wealthfolio's native `Other` value-only asset can retain a dated valuation without creating an account, activity, or security. Omit unknown purchase details and visibly label unverified legacy values. A retained reporting currency is not independent proof of source units: do not extend unlabelled balances into other dates or currencies without compatible evidence. This manual-source representation is not a fallback for an active SimpleFIN investment feed with missing positions; that feed must still report an error.

An existing loan maps through `wealthfolioAlternativeAssetId`. Source debt updates its native liability valuation using the actual observation date. A failed bank connection keeps its last reported value and produces a warning; reconnect it in SimpleFIN to obtain fresh data.

The command waits for actual native balances to agree with the source, including Wealthfolio's asynchronous recalculation after changing tracking mode. A mismatch is an error, not a successful plan. For an existing installation, remove obsolete generated `gap:` / `rebuild:assertion:` entries after identifying them as bookkeeping rather than bank transactions; do not let old balance adjustments masquerade as spending or import them a second time.

For observation tracking, the source must persist as a readable, non-`CALCULATED` snapshot.
A matching total, including zero, is not sufficient. Wealthfolio 3.10 can skip an
identical snapshot without promoting its calculated source; the sync reports that
failure and restores the previous tracking mode instead of silently hiding its balance.
Newer manual or CSV-imported observations are retained rather than overwritten.

Cash accounts explicitly set to native `TRANSACTIONS` tracking keep that setting.
The sync imports actual transactions and verifies their signed cash and native value
against the source; a discrepancy never creates funding or forces an observation-mode
switch. `HOLDINGS` cash accounts use reported balance observations. Choose the native
mode from the available history, not simply because two ending totals happen to match.

Classify contribution-only subaccounts from issuer evidence, not the institution's name.
When the issuer defines a balance as payroll contributions excluding purchased shares,
use a separate cash contribution account; keep the actual share-holding brokerage as
securities. This does not make contributions spendable cash or establish acquisition cost.
Do not relabel an unitemized investment total as cash just to avoid a positions error.
A cash-mapped source reporting securities or foreign-currency positions is rejected
before importing transactions.

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

Wealthfolio is pinned to 3.10.0. To upgrade it, change the version and multi-platform digest in `compose.yml` and run the integration tests. They start that image with synthetic data, replay the sync, verify native Spending, transfer matching/linking, all four cost-basis methods, return of capital, and loan setup/valuation:

```powershell
pip install pytest argon2-cffi
$env:WEALTHFOLIO_IT = "1"; python -m pytest
```

CI runs the same test on every pull request. Deploy only after it passes:

1. Pull the replacement image with `dc pull wealthfolio`. If `compose.env` sets `WF_IMAGE`, remove the override or set it to the tested version and digest.
2. Stop both writers with `dc stop sync wealthfolio`.
3. Back up the entire `<data>\wealthfolio` directory and `compose.env` to a timestamped location outside the repository. Keep the existing image available for rollback. Copying only `wealthfolio.db` is insufficient: 3.9 adds separate profile databases, and the master key in `compose.env` is also required.
4. Start the stack with `dc up -d --build`, then check `dc ps` and sign in to confirm the existing accounts are present.

The 3.9 upgrade keeps the existing `WF_DB_PATH` and `/data` mount; existing data appears in a Personal profile. Database migrations run automatically on startup. Encryption remains optional; do not enable `WF_DB_REQUIRE_ENCRYPTION` without first performing the documented offline conversion. Update all devices to 3.10 before pairing device sync.

The 3.10 engine recalculates history on first launch. Derived valuations and cost basis may
change; do not change real transactions merely to reproduce old derived numbers. New
cost-basis methods apply to transaction-tracked investment accounts, not holdings snapshots.
Loan schedules require actual terms; never infer them from a reported liability balance.
Connect cloud backup is optional and is not enabled by this deployment.

Use Spending Insights for the native money-flow chart and its category drill-down. Its
accuracy depends on real transactions, reviewed transfer links, account selection, and
categorization; updating the app does not repair incomplete historical imports.
Use the native Backups tab and supported restore workflow for ordinary application backups.
The stopped-volume copy above is specifically the pre-migration rollback, not a replacement
backup system.

The upstream final-cash migration can rewrite legacy activity amounts and flag ambiguous rows for review. Rewritten amounts retain their original values in `final_cash_migration.legacy_amount` metadata. Review flagged activities in Wealthfolio; a raw activity fingerprint need not remain identical across this migration.

If startup or the sync fails, stop both containers before restoring the complete pre-upgrade Wealthfolio directory and its matching secrets, then start the previous pinned image. Never point an older image at a migrated database.

## Read-only financial audit

A healthy scheduler is not proof of complete historical data. Reuse the importer to check
the last saved SimpleFIN response without fetching again or changing the app:

```powershell
dc run --rm --no-deps sync python -m importers.simplefin.local_sync --check
```

`python -m importers.simplefin.audit` is only an alias for this check, not a second audit
engine. Use `--snapshot` to choose another saved response. Results go to the existing
private `simplefin\local-preview.json`, leaving the last real sync untouched.

The check uses the same source validation, transaction planning, native balance reads,
holdings-quantity/price checks, and unmatched-transfer warnings as the sync, without
recalculation or snapshot repair. Needed activity or liability-valuation changes and balance
mismatches are errors, not adjustments.
Newer native observations are retained and reported as warnings rather than overwritten.

Inactive is not archived: Wealthfolio deliberately retains non-archived accounts in net worth and can retain their historical Spending. Normal syncs, previews, and read-only checks warn when an inactive account still has a nonzero native value, including card debt or credit. That is a source-scope warning, not proof of closure, zero balance, duplication, or permission to hide history. Card sync requires native transaction tracking and rejects a different mode before activity writes. Card comparisons read the mode-correct, non-future snapshot; a dormant observation cannot stand in for the active ledger calculation.

A flat balance history does not establish a dead connection or account closure. In particular, the first changed value of a final flat run is still a reported change; discarding it can retain the balance before a payment updated. Do not infer a replacement opening or invent another payment from that difference. Credit cards cannot use native holdings tracking in the pinned image; do not bypass that boundary or change account type to hide a reconciliation gap.

In Wealthfolio 3.10, activity `amount` is final cash. Fee and tax fields describe attribution
inside that amount; they are not extra deductions. The adapter preserves those fields and
never subtracts them a second time when comparing a bank transaction or a card balance.
Comparisons also respect account type: native `INTEREST` is income in a cash account
but an outgoing charge on a credit card.

Wallet balances do not establish purchases, dollar funding, or acquisition cost.
Actual on-chain movements can be native asset transfers with unknown basis; do not
turn a quoted holding value into a `BUY`/`DEPOSIT` pair. Retain base-unit precision
and source fee units rather than inventing fiat fees. Flag unknown acquisition basis
for native review; a displayed zero book cost does not establish a zero tax basis.
An observed coin quantity without a known movement belongs in dated native holdings,
not a fabricated `TRANSFER_IN`. Keep an existing market-priced asset's pricing mode
and quote history; a new market quote does not refresh the observed quantity's date.
Daemon synchronization and wallet scanning are separate. A displayed quantity from an
unfinished wallet scan is not a certified complete or current balance. Retain it only
as an explicitly unverified observation; never infer zero, a transfer, or missing units.
Reopening a wallet or changing its cache-file timestamp alone does not prove that
its blockchain data refreshed.

Cash-wallet balance observations belong in native holdings history, not inferred
opening-income deposits. Keep real payment history without fabricating missing bank
funding. Native transfer suggestions still require source-purpose review: a bank
cashout is not a payment from a friend merely because its amount and date match.

Spending, categories, transfer classification, and money-flow calculations belong to Wealthfolio.
Use its Health Center, Spending, transfer tools, and native reports for those workflows.
There is no custom report store, duplicate categorizer, currency converter, or shadow ledger.
Exit `0` means the checked inputs produced no errors, `2` means discrepancies, and `1` means
execution failed. Inspect warnings and observation dates; source agreement is not proof of
complete lifetime transaction history.
