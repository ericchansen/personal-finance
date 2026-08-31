# Fidelity — positions and transaction export

Derived from a recorded browser session on 2026-08-26. No credentials or
account identifiers appear here; substitute your own at runtime.

**Automation tier: 2** — real JSON endpoints with free-form date parameters,
callable directly with headers lifted from a live session. The best-behaved
institution encountered so far.

## What you have to download

Fidelity splits one household across three systems, and **no single export
covers all of it**:

| Export | Covers | Where |
|---|---|---|
| Portfolio positions | Brokerage + employer plan holdings | Positions tab → Download |
| Account history | Brokerage + employer plan transactions | Activity & Orders → Download |
| Stock plan history | ESPP contributions | Stock plans → ESPP details → Full Transaction History |

The ESPP is the trap: it appears in the account sidebar total but is **absent
from the positions export**, because stock plans live in a separate system. A
reconciliation that trusts the positions file alone will silently understate the
household by the whole ESPP balance.

## The endpoints

### Transactions

```http
POST https://digital.fidelity.com/ftgw/digital/activityapi/api/v1/transactions/history
Content-Type: application/json
```

```jsonc
{
  "filter": {
    "accounts": [
      { "acctNum": "...", "acctName": "...", "acctType": "Brokerage" },
      { "acctNum": "...", "acctName": "...", "acctType": "WPS" }
    ],
    "searchCriteriaDetail": {
      "txnFromDate": 1756180800,   // unix seconds
      "txnToDate":   1787716800,
      "includeBasketNames": false,
      "includeCoreFundSettlementTransactions": false
    }
  }
}
```

Returns **JSON**, not CSV. The downloaded `Accounts_History.csv` is generated in
the browser from this response, so replaying the endpoint means writing the CSV
step yourself — or, better, consuming the JSON directly.

`acctType` is `Brokerage` for retail accounts and `WPS` (Workplace Solutions)
for employer plans.

### Positions

```http
POST https://digital.fidelity.com/ftgw/digital/portfolio/api/GetPositions
POST https://digital.fidelity.com/ftgw/digital/positions/poswebex/api/positions
POST https://digital.fidelity.com/ftgw/digital/positions/poswebex/api/spspositions
```

`GetPositions` covers brokerage and workplace plans. `spspositions` is the
stock-plan-specific variant.

### Required headers

| Header | Notes |
|---|---|
| `Cookie` | Session cookies. Expire with the session. |
| `Content-Type` | `application/json` |
| `Referer` | `https://digital.fidelity.com/ftgw/digital/portfolio/activity` |
| `appId` / `appName` | App identifiers the gateway checks (e.g. `activity-orders-ui`). |

`baggage` carries a session id for tracing and can be omitted.

### The date range is free-form

`txnFromDate` and `txnToDate` are **arbitrary unix timestamps**, not an
enumerated period. Three recorded downloads used three different windows,
which is what proves it:

```
1756180800 → 1787716800
1724644800 → 1756180800
1693108800 → 1724644800
```

The UI caps a download at one rolling year; the endpoint does not appear to.
This turns "download five times, once per year" into a single call.

### One call returns every account

The `accounts` array is sent whole and the response covers all of them at once.
Unlike Ally, there is no per-account loop — and unlike Ally, **every row carries
its own account number and account name**, so several files downloaded as
`Accounts_History.csv`, `Accounts_History (1).csv` … can still be told apart
after the fact.

## Manual procedure

1. Sign in at `digital.fidelity.com`
2. **Positions** tab → Download → `Portfolio_Positions_<date>.csv`
3. **Activity & Orders** → Download → repeat per year for as much history as wanted
4. **Stock plans** → ESPP details → Full Transaction History → Download

Steps 2 and 4 are the ones that matter for a balance. Step 3 is history.

## File shapes

### Positions

```csv
Account number,Account name,Symbol,Description,Quantity,Last price,...,Current value,...
```

Self-naming (`Portfolio_Positions_Aug-26-2026.csv`) and self-identifying. Both
tables end with several paragraphs of legal disclaimer that a naive CSV reader
will happily parse as data.

### Account history

```csv
Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,...,Amount ($),Settlement Date
```

Preceded by blank lines, followed by the same disclaimer block.

### ESPP history

```csv
Transaction Date,Transaction Type,Plan Name,Offering period,Quantity,Net Proceeds
```

Mixes two record kinds. `Payroll contribution` rows carry money; `Contribution
change` rows carry a **percentage** in the same columns and must be skipped.

## Things that will produce wrong numbers

**The money market fund is cash.** Uninvested cash is swept into `SPAXX`
(flagged `SPAXX**`) and reported as a position. Buying it as a security invents
a holding the account does not have.

**Employer plan assets have no ticker.** A 401k may hold a collective
investment trust identified by a 9-character CUSIP rather than a
symbol. No quote provider can price it, so it must be created with a manual
quote — otherwise the holding is permanently flagged as needing a price update.

Manual pricing needs a quote for every day the position is held, including the
day the account was funded, or Wealthfolio reports incomplete valuation
coverage. Quotes are imported through `POST /market-data/quotes/import`, and
that endpoint's `symbol` field wants the **internal asset UUID**, not the
CUSIP; passing the CUSIP fails with a foreign-key violation. The asset's UUID
is on its holding, under `instrument.id`.

**Fund the account the day before the buy.** Activities sharing a date have no
guaranteed order, so depositing cash and buying positions on the same day can
be evaluated buy-first. An existing account has enough prior cash to absorb
that; a newly created one starts at zero and goes straight negative.

**Fund from `quantity * price`, not the exported value.** The export rounds
each position's current value to the cent, but a buy costs the full-precision
product. In a synthetic example, `3.333 * 10.01` is `33.36333` against an
exported `33.36`; that fractional-cent shortfall still produces a negative
balance and is reported as a data error.

**The ESPP balance is cash, not stock.** It is money withheld from pay that buys
shares at a discount when the offering period closes. Recording it as a position
invents shares that do not exist yet and books the discount as a gain months
early. It is still created as a SECURITIES account rather than a CASH one:
Wealthfolio's CASH type means "feeds spending reports", and this money never
passes through a tracked checking account, so reporting it there invents income.

## Automation sketch

1. Sign in once in a recorder-launched browser so the trusted-device cookie
   persists in that profile
2. Lift `Cookie`, `appId` and `appName` from any authenticated XHR
3. `GetPositions` for current holdings
4. `transactions/history` once, with the full date range, for all accounts
5. Fetch the stock plan separately — it is not in either of the above

Steps 3–5 need no UI interaction, but the session cookie is short-lived, so this
stays a human-in-the-loop refresh rather than an unattended job.

Fidelity's terms prohibit automated access. This is documented to make a manual
export faster and reproducible, not to run unattended against their servers.
