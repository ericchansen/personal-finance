# harvest-ally.mjs — Ally Bank transaction harvester

Pull every Ally account's transaction CSV in one command. No manual
renaming. No copy-pasting tokens.

## Quick start

```sh
# 1. Launch the recorder browser (once per machine)
node recorder/launch.mjs

# 2. Sign in at secure.ally.com in that browser window.
#    Your session cookie persists between runs.

# 3. Harvest all accounts (last 24 months by default)
node recorder/harvest-ally.mjs --out D:\documents\finance-data\extracts\ally

# 4. For a custom date range
node recorder/harvest-ally.mjs \
  --out D:\documents\finance-data\extracts\ally \
  --from 2023-01-01 \
  --to   2025-08-26
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--out <dir>` | required | Directory to write CSVs into |
| `--from YYYY-MM-DD` | 24 months ago | Requested start of the date range |
| `--to YYYY-MM-DD` | today | Requested end of the date range |
| `--port 9222` | 9222 | CDP debug port of the recorder browser |
| `--dry-run` | off | Discover accounts, print plan, write nothing |

## How credential capture works

Ally's transaction endpoint requires two non-cookie request headers:

- **`authorization`** — a short opaque session token (~35 chars, not a JWT).
  Expires when the browser session ends.
- **`cif`** — a numeric customer identifier, stable across sessions.

The harvester **never asks you to paste anything**. Instead it:

1. Connects to the recorder browser via CDP (Chrome DevTools Protocol).
2. Finds your open `secure.ally.com` tab.
3. Enables the CDP Network domain to intercept outgoing request headers.
4. Triggers a lightweight read-only API call *from inside the page's own
   JavaScript context* (`Runtime.evaluate`) — so the browser attaches all
   session cookies and the Ally SPA attaches `authorization` / `cif`
   automatically.
5. Captures those headers from the CDP event stream.
6. Uses them to call the accounts list and then the per-account CSV endpoint.

Credentials live **in memory only**. They are never written to disk, never
printed to stdout, and never logged.

## Output filenames

Every Ally download is named `transactions.csv` by the server, and nothing
inside the file identifies the account. The harvester names files itself
using account metadata:

```
Transactions - Ally Checking (1234) - 2024-01-05 to 2025-08-26.csv
Transactions - Ally Online Savings (5678) - 2024-01-05 to 2025-08-26.csv
```

The date range in the filename is derived from the **actual CSV contents**,
not from the requested range — so if an account has no activity before a
certain date the filename reflects reality.

## Session lifecycle

The recorder browser uses a persistent profile at
`%USERPROFILE%\.copilot\browser-profiles\finance-recorder`. Ally's
trusted-device cookie persists there between runs, so MFA is not re-
challenged each time.

The `authorization` token is short-lived (it expires with the browser
session). When it expires you will see:

```
Ally session expired — sign in again at secure.ally.com and re-run.
```

Re-open the browser, sign in, and re-run the harvester. This is intentional
— automated unattended re-authentication would require storing credentials,
which the project explicitly forbids.

## Limitations

- **One browser session at a time.** The harvester reads credentials from a
  live browser tab; if the recorder browser is not running, or no Ally tab
  is open, it exits with a clear message.

- **Token lifetime.** Ally's `authorization` token expires with the browser
  session. If you close the browser and re-open it you must sign in again.

- **No transaction IDs.** Ally's CSV format (`Date, Time, Amount, Type,
  Description`) carries no stable transaction identifier. Downstream imports
  must deduplicate on a synthesised key. Prefer OFX format if Ally ever
  offers it for automated access.

- **Ally's terms of service.** Ally prohibits automated access. This tool
  is a productivity aid for a single human account holder, not an unattended
  scraper. The user must initiate every run manually.

- **Account discovery.** The harvester calls
  `/acs/v2/customers/{cif}/accounts`. If Ally changes this endpoint's
  response shape the account-mapping logic may need updating.

## Running tests

```sh
node --test recorder/
```

Tests cover the pure functions (filename construction, CSV date extraction).
Network and CDP interactions require a live session and are not unit-tested.
