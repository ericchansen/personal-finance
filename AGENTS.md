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
<data>/ollama-agent/             Local-model suggestions, reviews, decision cache
<data>/wealthfolio/              Wealthfolio SQLite volume
```

## Data-quality invariants

- **Trust cutoff.** Aggregator exports may carry *stale, forward-filled* balances that look
  real. Every account has a trust-cutoff date; balances after it must be discarded, not
  imported. This is enforced in code, not by memory.
- **Idempotent imports.** Re-importing an overlapping date range must never double-count.
  Dedup on a stable source ID where one exists.
- **Active vs. closed accounts.** Closed accounts must not distort net worth or cash flow.

## Account names and durable decisions

- The private account fact's `displayName` is the canonical user-facing name. Source and
  aggregator names are aliases for matching only and must not overwrite it during refresh.
- Use a concise ownership qualifier plus institution or product and account type. Append a
  real last-four only when it is known and needed to distinguish otherwise identical
  accounts. Never display placeholder masks, raw source IDs, or aggregator decorations.
- Make household-specific naming, ownership, exclusion, and source-mapping decisions in the
  external facts, decisions, and mapping files first. Future agents must inspect those
  private decisions before planning an import or changing Wealthfolio.
- Rebuild the private canonical publication after a fact change, then reconcile Wealthfolio
  from the reviewed canonical plan. Do not make an ad hoc app-only rename that will drift
  from the system of record.

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
