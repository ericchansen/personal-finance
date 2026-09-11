# personal-finance

Self-hosted personal finance tooling: a [Wealthfolio](https://github.com/wealthfolio/wealthfolio)
deployment, importers for financial data extracts, and a browser-based extract recorder.

> **This repository contains no financial data and no PII.** It is code, configuration, and
> documentation only. All real data lives outside the repo in a separate data directory that
> is never committed. See [Security](#security).

## Supported workflows

### Everyday local SimpleFIN sync

For a local experiment, use the [direct SimpleFIN sync](docs/runbooks/local-simplefin.md). One Python command fetches SimpleFIN and updates the running Wealthfolio app. It uses the existing private account map and Wealthfolio's own source IDs; no PostgreSQL, release installation, approval artifacts, or clone/rebuild workflow is required.

```powershell
python -m importers.simplefin.local_sync --data-dir "<your-finance-data>"
```

### Older evidence and repair workflows

These tools remain available for historical imports and specialized repairs. They are not prerequisites for the local sync.

- **Collect evidence:** scheduled SimpleFIN snapshots and institution exports stay immutable
  outside the repository. Source collection never writes Wealthfolio.
- **Resolve identity:** source-scoped occurrences, revisions, provenance, exclusions, and
  durable decisions determine which financial events can be accepted.
- **Deliver scoped updates:** the [incremental worker](docs/runbooks/incremental-finance.md)
  adopts exact existing records and journals source-backed creates. Unproved history,
  unsupported corrections, and reconciliation differences remain explicit holds.
- **Repair and recover:** [bounded repair](docs/runbooks/bounded-repair-promotion.md) requires
  exact evidence, clone rehearsal, preservation checks, and separate production authorization.
- **Categorize:** the existing [categorization workflow](docs/runbooks/categorization.md)
  preserves manual assignments and separates proposals, rehearsal, and application.

Deployment is explicit. [Stable releases](docs/runbooks/incremental-release.md) bind code,
accounts, source evidence, app instances, and writer ownership; checking out the repository
does not enable mutations. A successful update or reconciled balance is not a claim of
complete historical coverage or household-wide categorization.

## Why this exists

Scheduled aggregator observations provide ongoing updates, while **institution extracts** —
CSV, XLSX, OFX/QFX, and statements — provide source evidence and historical coverage.
Overlapping sources require explicit identity and reconciliation rather than blind imports.

This repo makes that workflow survivable:

| Component | What it does |
|---|---|
| `deploy/` | Runs Wealthfolio, PostgreSQL, isolated recovery environments, and pinned release launchers |
| `importers/` | Normalizes extracts into Wealthfolio's import schema |
| `importers/audit/` | Publishes verified evidence baselines and read-only duplicate audits |
| `importers/lineage_review/` | Builds private review queues and verifies durable lineage decisions |
| `importers/analytics/` | Builds private canonical analytics and metadata review plans |
| `recorder/` | Watches a real browser session over CDP and records how an extract was fetched |
| `finance_store/identity.py` | Resolves immutable observations into stable canonical events |
| `finance_store/incremental*.py` | Qualifies scoped source updates and records retry-safe application work |
| `docs/runbooks/` | Per-institution, PII-free instructions for repeating an extract |

## What this repo is *not*

It does not implement a ledger, a net-worth engine, a cash-flow engine, or charts.
[Wealthfolio](https://github.com/wealthfolio/wealthfolio) already does all of that, and we
consume it **unforked**, extending only through its addon SDK where genuinely necessary.

## Quick start

```sh
cp deploy/wealthfolio/.env.example deploy/wealthfolio/.env
# Set WF_DATA_DIR outside the repository and configure the required secrets.
docker compose -f deploy/wealthfolio/compose.yml up -d
```

Then open <http://localhost:8088> (or the configured `WF_PORT`).

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
See [Evidence baselines](docs/runbooks/evidence-baseline.md) for an immutable
inventory of private source evidence and authenticated Wealthfolio read state.
See [Forensic duplicate audit](docs/runbooks/forensic-duplicate-audit.md) for
private, receipt-backed duplicate and lineage review without ledger mutation.
See [Private lineage review](docs/runbooks/lineage-review.md) for deterministic
review packets, fail-closed decision imports, and remediation-readiness counts.
See [Spending categorization](docs/runbooks/categorization.md) for the
source-agnostic plan, staging rehearsal, and production promotion workflow,
including the private read-only index of Wealthfolio's own categorized history.
See [Wealthfolio mutation containment](docs/runbooks/mutation-containment.md)
for the default-deny global interlock and the complete guarded-command inventory.
See [Bounded repair promotion](docs/runbooks/bounded-repair-promotion.md) for
clone-first, evidence-bound repairs with database preservation and recovery.
See [Repair-clone safeguards](docs/runbooks/wealthfolio-repair-clone.md) for
authenticated target isolation, verified backup copies, and runtime controls.
See [Local model categorization](docs/runbooks/ollama-categorization.md) for the
optional loopback-only Ollama agent that suggests categories for genuinely novel
merchants. Merchant text goes to `127.0.0.1` and nowhere else; every artifact it
writes is merchant-redacted.
See [PostgreSQL shadow authority](deploy/postgres/README.md) for the isolated,
sealed evidence/reconciliation deployment. It does not replace Wealthfolio.
See [Supported local release](docs/runbooks/supported-release.md) for the
content-addressed execution target used by source-only scheduled collection.
See [Canonical transaction identity](docs/canonical-transaction-identity.md) for
the graph model, confidence proof, lifecycle rules, and private shadow report.

See [Accepted event identity](docs/accepted-event-identity.md) for stable addresses
and revision history, and [read-only incremental queries](docs/runbooks/incremental-agent-queries.md)
for source freshness, qualification, and applied-operation provenance.

## License

MIT — see [LICENSE](LICENSE). Wealthfolio itself is AGPL-3.0 and is consumed as an
unmodified upstream container image.
