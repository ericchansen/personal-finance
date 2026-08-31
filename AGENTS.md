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
- Hardcode credentials; use environment variables or the Wealthfolio addon `secrets` API

Test fixtures must be **synthetic**. Invent plausible-looking data; never trim a real export.

## Architecture

We **consume Wealthfolio unforked** as an upstream Docker image. It owns the ledger, net
worth, cash flow, budgets, charts, and the FIRE simulator. We do not reimplement any of that.

Where Wealthfolio needs extending, we write an **addon** against
`@wealthfolio/addon-sdk` — a separate module, never a patch to upstream.

```
deploy/wealthfolio/   Docker Compose for the Wealthfolio server
importers/            Extract -> Wealthfolio normalization
recorder/             CDP network recorder
docs/runbooks/        Per-institution extract instructions (PII-free)
```

## Data lives outside the repo

The data directory is configured by `WEALTHFOLIO_DATA` / `FINANCE_DATA` and defaults to a
path outside this checkout. Layout:

```
<data>/extracts/<institution>/   Raw downloads, as retrieved
<data>/recordings/               Redacted CDP captures
<data>/normalized/               Importer output
<data>/wealthfolio/              Wealthfolio SQLite volume
```

## Data-quality invariants

- **Trust cutoff.** Aggregator exports may carry *stale, forward-filled* balances that look
  real. Every account has a trust-cutoff date; balances after it must be discarded, not
  imported. This is enforced in code, not by memory.
- **Idempotent imports.** Re-importing an overlapping date range must never double-count.
  Dedup on a stable source ID where one exists.
- **Active vs. closed accounts.** Closed accounts must not distort net worth or cash flow.

## Conventions

- Python 3.11+, standard library preferred; keep dependencies minimal and justified
- Filenames follow the existing convention: `Institution - Document Type - YYYY-MM-DD.ext`
- Commits are conventional (`feat:`, `fix:`, `docs:`, `chore:`) and self-contained

## Before pushing

```sh
git config core.hooksPath .githooks   # once
python .githooks/scan_staged.py --all # scan the whole tree
```

Review `git diff --stat` for anything data-shaped that slipped through.
