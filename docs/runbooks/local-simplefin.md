# Local SimpleFIN sync

Wealthfolio owns the ledger, reports, categories, and transfer matching. The `sync` container
only feeds it from SimpleFIN through Wealthfolio's local API.

Commands below use this PowerShell shorthand. Inside the container the data directory is
`/finance`.

```powershell
function dc { docker compose --env-file "<your-finance-data>/compose.env" @args }
```

## What runs

`compose.yml` runs pinned Wealthfolio and `sync`, which runs `importers.simplefin.schedule`.

- **Schedule.** After the first manual sync, it syncs once a day at the first five-minute poll
  after `SYNC_AT` (default `06:00` in `TZ`), so a sleeping host catches up when it wakes. A crash
  retries hourly by replaying the day's saved response, and retries stop requesting new data
  after half of SimpleFIN's 24 daily requests. A run that ends with account errors waits for the
  next day. State lives in the data directory, so restarts keep these limits.
- **Each run** signs in to Wealthfolio, fetches 45 days from SimpleFIN, and saves the raw
  response under `raw/simplefin/<date>/`. Posted transactions after each account's
  `historyThrough` are imported with stable provider IDs, so replays are no-ops and provider
  corrections update the same activity. Cash accounts record the reported balance as a native
  holdings snapshot, unless set to transaction tracking, in which case their ledger must
  reconcile. Investment accounts record reported positions; credit cards stay
  transaction-tracked; loans update their liability valuation. The run then waits for
  Wealthfolio's values to match the source. A mismatch is an
  error, never an adjustment: the sync does not invent income, spending, trades, or cash.
- **Health.** Docker marks `wealthfolio-sync` unhealthy when the last sync is more than 26 hours
  old or reported errors. For an alert that also fires when the host is off, set
  `SYNC_PING_URL` in `compose.env` to a [healthchecks.io](https://healthchecks.io) check with a
  one-day period; each run pings only its exit status.

Results go to `simplefin/local-last-sync.json`; previews and checks go to
`simplefin/local-preview.json`. Exit status `0` is clean, `2` means account errors, and `1`
means the run failed.

## Deploy

```powershell
pwsh deploy/init-env.ps1 -DataDir "<your-finance-data>"   # once: creates compose.env
dc up -d --build

# Connect SimpleFIN once; claiming consumes the setup token
dc run --rm --no-deps sync python -m importers.simplefin.cli claim --token "<setup-token>"
dc run --rm --no-deps sync python -m importers.simplefin.cli accounts

# Map accounts (below), preview, then run the first sync by hand; the schedule starts after it
dc run --rm sync python -m importers.simplefin.local_sync --dry-run
dc run --rm sync python -m importers.simplefin.local_sync
```

To update the code or move machines, copy the data directory if needed and run
`dc up -d --build` from a checkout.

**Upgrading Wealthfolio.** Change the version and multi-platform digest in `compose.yml` and
run the integration tests (`pip install pytest argon2-cffi`, then
`$env:WEALTHFOLIO_IT = "1"; python -m pytest`); CI runs them too. Then `dc pull wealthfolio`,
`dc stop sync wealthfolio`, back up the whole `<data>/wealthfolio` directory and `compose.env`
outside the repository, and `dc up -d --build`. To roll back, restore that whole backup and
the previous image; never point an older image at a migrated database.

## Check

```powershell
dc run --rm --no-deps sync python -m importers.simplefin.local_sync --check
```

The check replays the last saved response against the app without fetching or writing.
`--snapshot <saved-response>` picks another saved response. Any activity or valuation change
the source still needs, and any balance mismatch, is an error. Review the warnings too:

- **Unmatched transfers.** `unmatchedTransferCount` counts imported cash transfer legs without
  a counterpart, which Wealthfolio counts as income or spending. Link genuine pairs with
  Wealthfolio's transfer tools, or reclassify the activity.
- **Unknown cost.** A position without reported, recorded, or retained cost shows zero cost in
  Wealthfolio. That is not a verified tax basis; record it under `verifiedCosts`.
- **Inactive accounts with value.** Inactive accounts still count in net worth until archived.
- **Old balances and connection errors.** Reconnect the institution in SimpleFIN.

Categorization is Wealthfolio's job: select the household accounts in Spending settings and
add rules for frequent merchants. Rules never overwrite a category chosen by hand.

## Add or map an account

`<data>/simplefin/account-map.json` maps every SimpleFIN account ID (from `cli accounts`); an
unmapped account is an error. Create and name the account in Wealthfolio first: its type
(`CASH`, `CREDIT_CARD`, or `SECURITIES`) decides how it syncs, and the sync never renames it.

```json
{
  "version": 1,
  "accounts": {
    "synthetic-source-account": {
      "wealthfolioAccountId": "synthetic-app-account",
      "historyThrough": "2026-01-31"
    },
    "synthetic-ignored-account": {"action": "exclude"}
  }
}
```

| Key | Meaning |
| --- | --- |
| `action: "exclude"` / `localSync: false` | Skip the account. |
| `wealthfolioAccountId` | The Wealthfolio account to update. |
| `wealthfolioAlternativeAssetId` | Instead, a native liability to revalue (loans). |
| `historyThrough` | Cash and cards: the last day covered by earlier imports. Use their transaction coverage, not the download date. Only later transactions are imported; another source's posted activity after this day blocks the account until resolved. |
| `existingActivities` | SimpleFIN transaction ID to existing activity ID, for transactions an earlier import already brought in after `historyThrough`. |
| `transactionTimezone` | The institution's IANA timezone, when its posting days differ from UTC. |
| `sourceScope` | Anything but the default `"account"` marks a partial view of an account, which is reported as an error instead of synced. |
| `purchaseAccount` | For a balance that only empties when a plan buys shares (an ESPP contribution account): the SimpleFIN ID of the account that receives the purchase. A reported `0.00` is recorded only when that account shows a purchase of the previous balance (within 0.05) posted at most 7 days before it; otherwise the earlier balance is kept with a warning, because providers intermittently report such plans as empty. |
| `sourceCostBasis: "perUnit"` | The provider's cost-basis field is per share, not total. Set it only after the issuer's history confirms. |
| `verifiedCosts` | Issuer-documented cost when the provider reports none, keyed by symbol (or description). `{"unitCost": "1.00"}` for a fixed-price fund; `{"quantity": "...", "costBasis": "..."}` applies only while the source reports that exact quantity. |
| `rollForward` | Carries a known average cost through quantity changes using the provider's transactions: `{"transactions": {"<exact description>": {"<symbol>": "<fraction of dollars>"}}, "settlementDays": n}`. Map a description to `{}` when it moves no shares. A step is accepted only when the attributed dollars per share fall within the observed prices; otherwise cost becomes unknown with a warning until re-anchored by `verifiedCosts`. |

Investment positions are priced per account from the provider's reported values and must sum
to the account balance, with any reported `Cash` position becoming the cash balance. A newer
manual observation in Wealthfolio is kept rather than overwritten.
