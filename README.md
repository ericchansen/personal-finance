# personal-finance

Self-hosted personal finance tooling: a [Wealthfolio](https://github.com/wealthfolio/wealthfolio)
deployment, importers for financial data extracts, and a browser-based extract recorder.

> **This repository contains no financial data and no PII.** It is code, configuration, and
> documentation only. All real data lives outside the repo in a separate data directory that
> is never committed. See [Security](#security).

## Why this exists

Consumer aggregator APIs (Plaid and friends) are not practically available to individuals,
so financial data has to be collected as **manual extracts** — CSV, XLSX, OFX/QFX, and PDF
statements downloaded by hand from each institution.

This repo makes that workflow survivable:

| Component | What it does |
|---|---|
| `deploy/` | Runs Wealthfolio (net worth, cash flow, budgets, FIRE simulator) in Docker |
| `importers/` | Normalizes extracts into Wealthfolio's import schema |
| `importers/analytics/` | Builds private canonical analytics and metadata review plans |
| `recorder/` | Watches a real browser session over CDP and records how an extract was fetched |
| `docs/runbooks/` | Per-institution, PII-free instructions for repeating an extract |

## What this repo is *not*

It does not implement a ledger, a net-worth engine, a cash-flow engine, or charts.
[Wealthfolio](https://github.com/wealthfolio/wealthfolio) already does all of that, and we
consume it **unforked**, extending only through its addon SDK where genuinely necessary.

## Quick start

```sh
cp deploy/wealthfolio/.env.example deploy/wealthfolio/.env
# edit .env and set WEALTHFOLIO_DATA to a path outside this repo
docker compose -f deploy/wealthfolio/compose.yml up -d
```

Then open <http://localhost:1420>.

## Security

Financial data is sensitive and this repository is public, so the boundary is strict:

- **No data files, ever.** `.gitignore` denies `*.csv`, `*.ofx`, `*.qfx`, `*.xlsx`, `*.pdf`,
  recordings, and database files by extension.
- **A pre-commit hook** (`.githooks/pre-commit`) blocks data-shaped files and scans staged
  content for secrets and account-number patterns. Enable it with:
  ```sh
  git config core.hooksPath .githooks
  ```
- **Recordings contain live credentials.** The recorder redacts cookies, `Authorization`
  headers, and account numbers *before* anything is written to disk.
- **Runbooks are written generically** — no account numbers, balances, or personal URLs.
- **No cloud model, ever.** The optional categorization agent talks only to an
  Ollama server on loopback, with proxies explicitly disabled and redirects
  refused. Merchant descriptions are never written to disk, cached, or logged.

## Layout

```
deploy/wealthfolio/   Docker Compose for the Wealthfolio server
importers/monarch/    Monarch Money CSV export -> Wealthfolio
importers/analytics/  Canonical private analytics -> portable CSV/JSON contract
importers/categorize/ Source-agnostic categorization of live Wealthfolio cash activity
                      (plus an optional loopback-only Ollama suggestion agent)
recorder/             CDP network recorder (browser extract capture)
docs/                 Architecture notes and per-institution runbooks
```

See [Canonical analytics](docs/canonical-analytics.md) for truthful sold-asset,
liability, performance, metadata-review, and reporting-portfolio outputs.
See [Spending categorization](docs/runbooks/categorization.md) for the
source-agnostic plan, staging rehearsal, and production promotion workflow,
including the private read-only index of Wealthfolio's own categorized history.
See [Local model categorization](docs/runbooks/ollama-categorization.md) for the
optional loopback-only Ollama agent that suggests categories for genuinely novel
merchants. Merchant text goes to `127.0.0.1` and nowhere else; every artifact it
writes is merchant-redacted.

## License

MIT — see [LICENSE](LICENSE). Wealthfolio itself is AGPL-3.0 and is consumed as an
unmodified upstream container image.
