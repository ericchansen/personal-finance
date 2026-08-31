# Monarch Money → Wealthfolio importer

Loads a Monarch Money CSV export into a self-hosted Wealthfolio instance.

```sh
python cli.py plan  --data-dir D:/documents/finance-data   # read-only
python cli.py apply --data-dir D:/documents/finance-data
```

Both are safe to re-run — see [Idempotency](#idempotency).

## Expected layout

The importer reads from and writes to the **external data directory**, never this
repository:

```
<data>/legacy/monarch/Transactions_*.csv      from Monarch's export
<data>/legacy/monarch/Balances_*.csv
<data>/legacy/monarch/account-overrides.json  optional, see below
<data>/normalized/monarch-plan.json           written by `plan`
<data>/normalized/monarch-account-map.json    written by `apply`
```

The newest file matching each glob is used.

## The trust cutoff

When an aggregator loses its connection to an institution it keeps emitting the
last known balance indefinitely. Those rows look like real history and will
quietly corrupt net worth.

`detect_trust_cutoff` finds the trailing run of identical balances and reports
the last date the balance actually *changed*. Everything after is discarded.

It is deliberately conservative: forward-filling repeats the last real value, so
the boundary is genuinely ambiguous. We drop one real reading rather than admit
a fabricated one.

Review the reported cutoff and discarded-row count privately before importing;
the public repository never records results from a personal export.

## Account overrides

Account type is inferred from the name. Employer-named retirement plans carry no
clue about their type. Override them explicitly:

```json
{ "employer name as it appears (...1234)": "RETIREMENT" }
```

Keys are matched case-insensitively against the full account name. Because those
names are personal data, this file lives in the data directory.

## Idempotency

Every activity carries `idempotencyKey = "monarch:<Monarch row id>"`. Wealthfolio
rejects duplicates server-side, so re-running `apply` adds nothing:

```
first run : <count> created,     0 already present
second run:     0 created, <count> already present
```

A duplicate anywhere in a batch fails the whole request, so `import_batch` retries
that batch item by item and counts duplicates as skipped.

## Wealthfolio API notes

Behaviours worth knowing, all found the hard way against v3.7.0.

### Dates must be ISO-8601 datetimes, not plain dates

Spending endpoints reject `2025-11-01` with a `500 premature end of input`, which
looks like a server bug but is a parse failure. Send what the UI sends:

```json
{ "startDate": "2025-11-01T05:00:00.000Z", "endDate": "2025-12-01T04:59:59.999Z" }
```

### Base currency and timezone must be set before anything works

A fresh instance created entirely through the API never runs onboarding, leaving
`baseCurrency` and `timezone` as empty strings. Set them via `PUT /settings` and
restart the container, since they are cached in memory at startup.

### Accounts must be enrolled in spending tracking

`GET /spending/settings` starts with an empty `accountIds`. Until cash and credit
accounts are added there, every spending report is empty. Only `CASH` and
`CREDIT_CARD` accounts are eligible.

### Credit cards reject several activity types

Verified against a live server:

| Type | CASH | CREDIT_CARD |
|---|---|---|
| `WITHDRAWAL`, `CREDIT`, `TRANSFER_IN`, `INTEREST`, `FEE` | accepted | accepted |
| `DEPOSIT` | accepted | **rejected** |
| `TRANSFER_OUT` | accepted | **rejected** |
| `TAX` | accepted | **rejected** |

`resolve_activity_type` substitutes accordingly. This matters more than it looks:
rejections are returned in the response body rather than raised, so ignoring them
silently drops rows.

### Only four account types exist

`SECURITIES`, `CASH`, `CREDIT_CARD`, `CRYPTOCURRENCY`. Vehicles and loans have no
account equivalent — `wealthfolio_account_type` returns `None` for them so they
are routed to the alternative-assets and liability surfaces instead of being
created as wrong-typed accounts.

### Activity search paging is 0-based

`POST /activities/search` numbers pages from **0**. Starting at 1 silently skips
the first page while still returning a plausible row count. Use
`iter_activities`, which pages correctly.

### Search returns `date`, updates expect `activityDate`

An activity read back from search cannot be passed straight to `PUT /activities`.

### External flow is set through `metadata`, not a flag

On create, `isExternal` works. On update it is ignored; pass the raw JSON
string instead:

```json
{ "metadata": "{\"flow\": {\"is_external\": true}}" }
```

### Accounts need a tracking mode

A new account defaults to `NOT_SET` and the app reports it as needing setup.
Holdings derived from imported history should be `TRANSACTIONS`.

## Known gaps

- **Retirement and brokerage balances are held as cash**, not holdings. Net
  worth is correct, but there is no per-position detail, so those accounts
  cannot yet drive performance or allocation views.
- **The historical net-worth curve is approximate.** Only one balance point per
  account is used to derive the opening. Earlier history is reconstructed from
  transaction flows and steps up when an account's opening lands.
- **Cash accounts may dip negative mid-history**, reported by the app as an INFO
  issue. A single opening balance cannot reproduce every intermediate balance
  when money arrived from outside the tracked set.
- Data after the export cutoff must come from newer source extracts.

## Commands

Run in order on a fresh instance:

| Command | Effect |
|---|---|
| `plan` | Read-only. Parses the export, classifies accounts, writes a plan file. |
| `apply` | Creates accounts, reconciles drifted ones, loads activities. |
| `link` | Links the two legs of each internal transfer. |
| `external` | Marks unpaired transfers as crossing the portfolio boundary. |
| `balances` | Reconciles each account to its last trusted balance. |

All are idempotent; `link`, `external` and `balances` accept `--dry-run`.

Authentication resolves in this order: `WEALTHFOLIO_PASSWORD`, then the
generated password file if it still exists, then an interactive prompt.

Review the app's Data Health page before and after each command and keep the
result only in the private data directory.

### Why `link` matters

An aggregator records both legs of an internal transfer independently. Left
unlinked, one leg counts as spending and the other as income. For example, a
fully synthetic report can show:

| Synthetic year | Before `link` | After `link` |
|---|---:|---:|
| Income | $12,000 | $10,000 |
| Outflow | $9,000 | $7,000 |
| Net | $3,000 | $3,000 |

### Why `external` matters

Some transfers never pair, and widening the match window can create false
matches rather than resolve genuine external flows.

Those transfers are genuinely unpairable: paying a person over Venmo, or moving
money to an institution absent from the export. `external` declares them as
crossing the tracked-account boundary, which is what the app asks for and is a
more honest statement than leaving them merely unmatched.

### Why `balances` matters

Transactions give an account the *net change* over the imported window, not its
opening balance. Reconciliation restores the source-backed starting value
without publishing any personal totals.

The correction is dated the day before each account's first transaction,
because an opening balance posted as a deposit counts as income in cash-flow
reports and would otherwise distort the period it lands in.

### Account type drift is not cosmetic

Only `CASH` and `CREDIT_CARD` accounts feed spending reports. An employer 401(k)
mistyped as `CASH` turns its opening balance into reported income for that
year. `apply` reconciles the type, group, active flag and tracking mode of
accounts that already exist rather than skipping them.

## Tests

```sh
python -m pytest tests/ -q
```

All fixtures are synthetic. Never copy rows from a real export into this repo.
