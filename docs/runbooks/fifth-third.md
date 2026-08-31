# Fifth Third — transaction export

Derived from a recorded browser session on 2026-08-26. No credentials or
account identifiers appear here; substitute your own at runtime.

**Automation tier: 2** — a clean three-step REST flow returning CSV, callable
directly with headers lifted from a live session.

## ⚠️ Download QFX, not CSV

The export options endpoint advertises three formats:

```json
"downloadOptions": ["CSV", "QFX", "QIF"]
```

**QFX is OFX-family and carries `FITID`, a stable per-transaction id. CSV does
not.** Without an id, imports must dedupe on a synthesized key of date, amount
and description, so two identical same-day charges collapse into one. That
under-counts rather than double-counts, which is the safer failure, but QFX
avoids it entirely.

The session that produced this runbook took CSV, which is why the loaded data
uses synthesized ids. Take QFX next time.

## The endpoints

All live under `https://secure.53.com/api/cw-ibs-proxy-bff/services`.

### 1. Ask what is available

```http
GET /account/transaction/export/options?accountId={accountId}
```

```jsonc
{
  "lastExportRange": { "fromDate": "...", "toDate": "..." },
  "earliestTransactionDate": "2026-02-14",
  "downloadOptions": ["CSV", "QFX", "QIF"],
  "account": { }
}
```

`earliestTransactionDate` is the useful field: it states how far back this
account can go, so a caller can request the full available history in one pass
instead of guessing.

### 2. Request the export

```http
POST /account/transaction/export
Content-Type: application/json
```

```jsonc
{
  "accountId": "<uuid>",
  "dateRange": { "fromDate": "2026-02-14", "toDate": "2026-08-25" },
  "exportOption": "CSV"
}
```

Returns an `exportId`.

### 3. Download it

```http
GET /account/transaction/export?exportId={exportId}
```

Responds `text/csv;charset=utf-8` with
`Content-Disposition: inline;filename=EXPORT.CSV`.

### Required headers

| Header | Notes |
|---|---|
| `Authorization` | ****** token, ~1.7 KB. Session-lived. |
| `x-api-key` | Very short; session-derived. |
| `X-CSRF-Token` | 36 chars. |
| `x-xsrf-token` | 36 chars, sent alongside the above. |
| `Referer` | `https://secure.53.com/olb/account/details/transactionExport` |
| `X-Application` | `OnlineBanking` |
| `X-Client-Version` | e.g. `5.17.1` |
| `X-Device-UID` | Per-device UUID; stable for a browser profile. |

Four independent credentials, all session-scoped. This is the most heavily
guarded of the institutions documented here.

### Is the date range free-form?

**Probably, but unproven.** The range is expressed as explicit ISO-8601
`fromDate`/`toDate` fields rather than a preset enum — which is the shape a
free-form range takes. But only **one** export was recorded, and a single
request cannot prove the server accepts arbitrary values: proof requires two
exports whose ranges differ, as was obtained for Ally and Fidelity.

Treat this as a strong hint, not an established fact. Recording a second export
over a different window would settle it.

Note also that the requested window and the returned data are not the same
thing. One export requested 2026-02-14 to 2026-08-25 and returned transactions
spanning 2026-02-27 to 2026-08-03 — the endpoint returns what posted inside the
range, and does not pad it.

## One account per call

`accountId` is a UUID and each export covers exactly one account. Unlike
Fidelity, there is no way to pull the household in a single request; a caller
must list accounts and loop.

## Other endpoints worth knowing

| Endpoint | Returns |
|---|---|
| `GET /account/list` | Every account with its `accountId`, display name and masked number |
| `GET /account/detail?id={accountId}` | Current balance, interest YTD, description |
| `GET /account/transaction/statementCycles?accountId={id}` | Statement periods as `{index, startDate, endDate}` |
| `GET /account/transaction/history?accountId={id}` | **Transactions as JSON** — `amount`, `postDate`, `description`, `id`, `creditDebitType`, `status` |

`transaction/history` is the interesting one. It returns a per-transaction `id`
and an explicit `creditDebitType`, so it sidesteps both CSV weaknesses — no
synthesized ids, and no inferring direction from a sign. If this pipeline is
ever rewritten to call the API directly, prefer it over any file download.

## Manual procedure

1. Sign in at `53.com`
2. Open the account → Transactions → Export
3. Pick a format (**choose QFX**) and a date range
4. Repeat per account

## CSV shape

```csv
Date,Description,"Check Number",Amount
```

- `Date` is `MM/DD/YYYY`
- `Amount` is a single signed column: negative is money out
- `Check Number` is usually empty
- **Rows are grouped by statement period, not sorted by date.** Nothing
  downstream may assume the file is ordered.
- Every download is named `EXPORT.CSV`, and **nothing inside the file
  identifies the account** — the same trap as Ally. Rename immediately.

The header carries both `Amount` and `Description`, which is also true of
Ally's, so a format sniffer must check for `Check Number` first or it will
misidentify the file.

## Automation sketch

1. Sign in once in a recorder-launched browser so the trusted-device cookie
   persists in that profile
2. Lift `Authorization`, `x-api-key` and both CSRF headers from any
   authenticated XHR
3. `GET /account/list` to collect account UUIDs
4. Per account: `GET .../export/options` for `earliestTransactionDate`, then
   POST the export for the full range, then GET the result — or skip the file
   entirely and read `transaction/history`

Five to six calls per account, no UI interaction after login. The credentials
are short-lived, so this stays a human-in-the-loop refresh rather than an
unattended job.

Fifth Third's terms prohibit automated access. This is documented to make a
manual export faster and reproducible, not to run unattended against their
servers.
