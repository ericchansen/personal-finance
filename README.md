# personal-finance

Self-hosted personal finance: [Wealthfolio](https://github.com/wealthfolio/wealthfolio) runs
locally in Docker, and one small daily job syncs [SimpleFIN](https://www.simplefin.org/) into it.

> **This repository contains no financial data and no PII.** It is code, configuration, and
> documentation only. All real data lives outside the repo in a separate data directory that
> is never committed. See [Security](#security).

## How it works

Wealthfolio, consumed **unforked** as an upstream image, owns the ledger, net worth, cash
flow, spending categories, budgets, and charts. This repository only feeds it:

- `importers/simplefin/local_sync.py` fetches 45 days of SimpleFIN data and updates the running
  app through its local API: posted transactions, cash and card balances, investment positions,
  and liability values. Replays are no-ops, and it never invents income or spending to force a
  balance. See the [runbook](docs/runbooks/local-simplefin.md).

## Quick start

```powershell
# 1. Run Wealthfolio. init-env.ps1 writes deploy/wealthfolio/.env and a generated
#    login password beside the database (needs: pip install argon2-cffi).
pwsh deploy/wealthfolio/init-env.ps1 -DataDir "<your-finance-data>/wealthfolio"
docker compose -f deploy/wealthfolio/compose.yml up -d

# 2. Connect SimpleFIN once
python -m importers.simplefin.cli --data-dir "<your-finance-data>" claim --token "<setup-token>"

# 3. Map accounts in <your-finance-data>\simplefin\account-map.json (see the runbook), then sync
python -m importers.simplefin.local_sync --data-dir "<your-finance-data>" --dry-run
python -m importers.simplefin.local_sync --data-dir "<your-finance-data>"

# 4. Schedule it daily
.\importers\simplefin\install-local-task.ps1 -DataDir "<your-finance-data>"
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
- **The SimpleFIN access URL is a credential** for every connected institution. It lives only
  in the data directory and is revocable from the SimpleFIN Bridge dashboard.
- **Wealthfolio stays on loopback** with authentication enabled, and the sync refuses to talk
  to a non-loopback URL.

## Layout

```
deploy/wealthfolio/   Docker Compose for the Wealthfolio server
importers/simplefin/  SimpleFIN client, setup CLI, daily local sync, and task installer
docs/runbooks/        Sync runbook
.githooks/            Pre-commit data guard
```

## History

An earlier evidence pipeline (PostgreSQL shadow authority, canonical identity, release
packaging, clone-first repair, categorization, and per-institution backfill importers) was
removed once the direct sync replaced it. It is preserved at the git tag
`archive/evidence-pipeline`.

## License

MIT — see [LICENSE](LICENSE). Wealthfolio itself is AGPL-3.0 and is consumed as an
unmodified upstream container image.
