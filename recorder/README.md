# Browser traffic recorder

Records a real browser session over the Chrome DevTools Protocol so a manual
export can be studied and, where possible, replayed without a human.

```sh
node launch.mjs                                  # debuggable browser
node record.mjs --out D:/documents/finance-data/recordings
# drive the browser by hand, then Ctrl+C
```

## Why this exists

US banks have no consumer API, so getting data out means downloading files by
hand — every month, from every institution. The recorder watches one manual run
and captures exactly how the export was fetched: the URL, the parameters, the
headers, and the response itself.

That answers three questions a runbook cannot: what the export endpoint really
is, what a valid request to it looks like, and whether it could be called
directly next time.

## It captures everything

No request is dropped and no body is skipped by default — including images,
fonts and scripts.

Filtering while recording risks losing the one call that matters. An export
served from an unexpected CDN host, or returned with a surprising content type,
would vanish with no way to get it back. Requests are *classified* instead:

- `noise` — third-party telemetry and browser internals
- `exportCandidate` — mime type, URL shape or `Content-Disposition` suggests a data export

Those are hints for analysis, not filters. A superset can always be narrowed
later; a missed request cannot be un-missed.

`--skip-media` drops image/font/audio/video **bodies** for a lighter capture.
Their metadata is still recorded.

## It is passive

The recorder never clicks, types or navigates. The human drives; it observes.

That is deliberate. Banks run bot detection that automation trips, and driving
someone's authenticated banking session unattended is not something to do
casually. Passive observation also captures what a *real* session looks like,
which is the point.

## Why it is not blocked

`launch.mjs` starts Edge with only `--remote-debugging-port`. No automation
flags, so `navigator.webdriver` stays false and no "controlled by automated test
software" banner appears — both of which Akamai and PerimeterX check for.
Playwright and Puppeteer set those flags, which is why they get blocked where
this is not.

A dedicated `--user-data-dir` is required, not optional: Edge and Chrome 136+
silently ignore the debug port on the default profile as an anti-cookie-theft
measure. The profile persists between runs, so a bank's trusted-device cookie
survives and MFA is not re-challenged every time. It also keeps the capture
isolated from everyday browsing.

## Secrets

A recording of a banking session contains live session cookies, bearer tokens
and account identifiers.

Everything is redacted before it touches disk:

- credential-bearing headers (`Cookie`, `Authorization`, CSRF and API keys) are
  replaced with their length, which is enough to reason about a replay
- credential-shaped query parameters
- tokens and passwords inside JSON bodies, and bare JWTs anywhere
- card-shaped numbers, verified with a Luhn check so ordinary long identifiers
  stay readable

Recordings are written **outside the repository** and `*.har`, `recordings/` and
`captures/` are denied by `.gitignore`.

Redaction is covered by tests:

```sh
node --test recorder/
```

## Output

One JSON file per session:

```jsonc
{
  "requestCount": 412,
  "bodiesCaptured": 380,
  "exportCandidates": 6,
  "hosts": ["online.citi.com", "..."],
  "requests": [
    {
      "url": "...", "method": "GET", "status": 200,
      "resourceType": "Fetch", "mimeType": "text/csv",
      "requestHeaders": { "Cookie": "<redacted:1841 chars>" },
      "responseBody": "...",
      "noise": false, "exportCandidate": true
    }
  ]
}
```

## Known gaps

- Analysis is manual: the recording is written, but nothing yet turns an export
  candidate into a runbook or a replay script.
- Redacted headers cannot be replayed as-is by design; a replay has to re-use a
  live session rather than a recorded credential.
- WebSocket frames are not captured, only HTTP.
