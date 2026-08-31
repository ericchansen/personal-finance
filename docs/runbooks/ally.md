# Ally Bank — transaction export

Derived from a recorded browser session on 2026-08-26. No credentials or
account identifiers appear here; substitute your own at runtime.

**Automation tier: 2** — a real REST endpoint returning CSV, callable directly
with headers lifted from a live session.

## The endpoint

```http
GET https://secure.ally.com/acs/v1/bank-accounts/transactions/{accountToken}/csv
      ?fromDate=YYYY-MM-DD
      &toDate=YYYY-MM-DD
      &status=Posted
```

Response is `text/csv` with `Content-Disposition: attachment; filename=transactions.csv`.

### Required headers

| Header | Notes |
|---|---|
| `authorization` | Short opaque session token (~35 chars), not a JWT. Expires with the session. |
| `cif` | Numeric customer identifier, stable across sessions. |
| `Referer` | `https://secure.ally.com/` — the call is rejected without it. |

`traceparent`, `tracestate`, `x-dtc` and `x-dtpc` are Dynatrace RUM telemetry
and can be omitted.

### The date range is a parameter, not a preset

`fromDate` and `toDate` are free-form. The UI only offers fixed ranges, but the
endpoint accepts any span, so a full history can be pulled in one request
instead of stitching together whatever the dropdown allows.

This is the single most useful finding: it turns a repetitive chunked download
into one call per account.

## Getting the account token

`{accountToken}` is Ally's `accountNumberPvtEncrypt` — an opaque per-account
string, not the account number. Two ways to obtain it:

```http
GET  /acs/v2/customers/{customerId}/accounts
POST /acs/v1/bank-accounts/transactions/search
     {"accountNumberPvtEncrypt": "<token>", "recordsToPull": 20}
```

The `search` endpoint returns JSON rather than CSV, with
`accountId`, `accountNumber`, `accountType` and `transactionHistory` per account
— richer than the CSV, and worth preferring if the parser is ever rewritten.

## Manual procedure

1. Sign in at `secure.ally.com`
2. Open an account → Transactions → Download
3. Choose CSV; pick the widest range offered
4. Repeat per account

## ⚠️ Every download is named `transactions.csv`

Ally sets the same filename for every account, so a browser saves them as
`transactions.csv`, `transactions (1).csv`, `transactions (2).csv`. **Nothing in
the file identifies the account** — no account number, no name, no header
metadata.

Consequences:

- Record the download order at the time. Afterwards it is unrecoverable except
  by matching balances or row counts.
- Rename immediately, before doing anything else.
- Unlike Citi's OFX, the CSV carries no account ID to recover it from.

## CSV shape

```csv
Date, Time, Amount, Type, Description
```

Note the leading spaces in every header after the first; strip them when
parsing. There is **no transaction ID**, so imports must dedupe on a synthesized
key (date + amount + description) rather than a stable identifier. Prefer OFX
where Ally offers it.

Export size varies with account age and activity. Confirm the returned date
range and row count before treating a download as complete.

## Automation sketch

1. Sign in once in a recorder-launched browser, so a trusted-device cookie
   persists in that profile
2. Capture `authorization` and `cif` from any authenticated XHR
3. List accounts to collect tokens
4. Call the CSV endpoint once per account with the full date range
5. Re-authenticate when the token expires — it is short-lived, so this stays a
   human-in-the-loop refresh rather than an unattended job

Ally's terms prohibit automated access. This is documented to make a manual
export faster and reproducible, not to run unattended against their servers.
