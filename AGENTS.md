# AGENTS.md

Guidance for AI agents and humans working in this repository.

## The one rule that matters

**This repository is PUBLIC and must never contain financial data or PII.**

Before writing any file, ask whether it could contain an account number, a balance, a
merchant name, a transaction, a statement, a credential, or a personal identifier. If yes,
it belongs in the external data directory, not here.

Never:

- Commit `.csv`, `.ofx`, `.qfx`, `.xlsx`, `.pdf`, `.har`, or database files
- Paste real transaction rows, balances, or account numbers into code, tests, docs, or
  commit messages
- Write real account numbers into runbooks — describe navigation generically
- Hardcode credentials; use environment variables or files in the data directory

Test fixtures must be **synthetic**. Invent plausible-looking data; never trim a real export.

## Architecture

We **consume Wealthfolio unforked** as an upstream Docker image. It owns the ledger, net
worth, cash flow, spending categories, budgets, charts, and the FIRE simulator. We do not
reimplement any of that. Prefer Wealthfolio's own features (for example its spending rules)
over new code here.

```
compose.yml, Dockerfile  Wealthfolio plus the sync container
deploy/init-env.ps1      Creates the private compose.env
importers/simplefin/     SimpleFIN client, setup CLI, local sync, and daily scheduler
docs/runbooks/           Sync runbook (PII-free)
```

The `sync` container runs `python -m importers.simplefin.schedule`, which calls
`importers.simplefin.local_sync`; keep those module paths stable. Code removed from earlier
designs is preserved at the git tag `archive/evidence-pipeline`; restore pieces from there only
when a concrete need appears.

## Data lives outside the repo

The data directory is `FINANCE_DATA` (mounted at `/finance` in the sync container) and lives
outside this checkout. Layout:

```
<data>/compose.env                     Secrets and settings for compose.yml (secret)
<data>/simplefin/access-url.txt        SimpleFIN credential (secret)
<data>/simplefin/account-map.json      Private source-account -> Wealthfolio mapping
<data>/simplefin/local-last-sync.json  Result of the last real sync
<data>/simplefin/local-preview.json    Result of the last --dry-run
<data>/raw/simplefin/<date>/           Immutable SimpleFIN responses, replayable with --snapshot
<data>/wealthfolio/                    Wealthfolio SQLite volume and its backups
```

## Data-quality invariants

- **History boundary.** Each cash or card mapping has `historyThrough`, the last day owned by
  earlier imports. The sync only creates activity after it, so sources never overlap.
- **Idempotent imports.** Activities carry stable, account-scoped provider IDs. Replaying an
  overlapping window must never double-count.
- **No invented money.** Never fabricate income, spending, trades, or securities to force a
  balance. Report the mismatch instead.
- **Active vs. closed accounts.** Inactive or excluded accounts are skipped and must not
  distort net worth or cash flow.

## Account names and private decisions

- The account name in Wealthfolio is the user-facing name; the sync never renames accounts.
  Use a concise ownership qualifier plus institution or product and account type. Append a
  real last-four only when needed to distinguish otherwise identical accounts. Never display
  placeholder masks, raw source IDs, or aggregator decorations.
- Household-specific mapping, exclusion, and history-boundary decisions live in the private
  `account-map.json`. Inspect it before changing how an account is imported.

## Conventions

- Python 3.11+, standard library preferred; keep dependencies minimal and justified
- Commits are conventional (`feat:`, `fix:`, `docs:`, `chore:`) and self-contained

## Before pushing

```sh
git config core.hooksPath .githooks   # once
python .githooks/scan_staged.py --all # scan the whole tree
WEALTHFOLIO_IT=1 python -m pytest     # unit tests plus the pinned Wealthfolio image (needs Docker)
```

CI runs the same checks on every pull request. Review `git diff --stat` for anything
data-shaped that slipped through.
