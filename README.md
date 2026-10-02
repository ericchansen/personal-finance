# personal-finance

Self-hosted personal finance: [Wealthfolio](https://github.com/wealthfolio/wealthfolio) runs
locally in Docker, and one small daily job syncs [SimpleFIN](https://www.simplefin.org/) into it.

> **This repository contains no financial data and no PII.** It is code, configuration, and
> documentation only. All real data lives outside the repo in a separate data directory that
> is never committed. See [Security](#security).

## How it works

Wealthfolio, consumed **unforked** as an upstream image, owns the ledger, net worth, cash
flow, spending categories, budgets, and charts. This repository only feeds it.

`compose.yml` runs two containers: pinned Wealthfolio, and a `sync` container that runs
`importers/simplefin/local_sync.py` once a day. The sync fetches 45 days of SimpleFIN data and
updates the app through its local API: posted transactions, cash and card balances, investment
positions, and liability values. Replays are no-ops, and it never invents income or spending to
force a balance. Wealthfolio's own rules categorize new activity. Every secret and all data live
in one private directory, so the stack is reproducible from a checkout plus that directory. See
the [runbook](docs/runbooks/local-simplefin.md).

## Quick start

```powershell
# 1. Create <your-finance-data>/compose.env (needs: pip install argon2-cffi), then start
pwsh deploy/init-env.ps1 -DataDir "<your-finance-data>"
function dc { docker compose --env-file "<your-finance-data>/compose.env" @args }
dc up -d --build

# 2. Connect SimpleFIN once
dc run --rm --no-deps sync python -m importers.simplefin.cli claim --token "<setup-token>"

# 3. Map accounts in <your-finance-data>/simplefin/account-map.json (see the runbook), preview,
#    then run the first sync by hand; the daily schedule starts after it
dc run --rm sync python -m importers.simplefin.local_sync --dry-run
dc run --rm sync python -m importers.simplefin.local_sync
```

The sync then runs daily at 06:00. Open <http://localhost:8088> (or the configured `WF_PORT`).
For alerts when a sync fails or does not run, set `SYNC_PING_URL` (see the runbook).

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
- **Secrets live in `<data>/compose.env`**, never in a checkout, so deleting or recloning the
  repository cannot lose them.
- **Wealthfolio stays on loopback** with authentication enabled. The sync container shares
  Wealthfolio's network namespace and refuses any non-loopback URL. The optional alert ping
  carries only an exit status.

## Layout

```
compose.yml, Dockerfile  Wealthfolio plus the sync container
deploy/init-env.ps1      Creates the private compose.env
importers/simplefin/     SimpleFIN client, setup CLI, local sync, and daily scheduler
docs/runbooks/           Sync runbook
.githooks/               Pre-commit data guard
```

## History

An earlier evidence pipeline (PostgreSQL shadow authority, canonical identity, release
packaging, clone-first repair, categorization, and per-institution backfill importers) was
removed once the direct sync replaced it. It is preserved at the git tag
`archive/evidence-pipeline`.

## License

MIT — see [LICENSE](LICENSE). Wealthfolio itself is AGPL-3.0 and is consumed as an
unmodified upstream container image.
