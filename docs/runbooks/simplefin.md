# SimpleFIN — safe scheduled transaction plans

SimpleFIN is read-only, but its access URL is a credential for every connected
institution. It must remain outside this public repository. The pipeline fetches 45 inclusive calendar days by default (the Bridge's live
recommendation), permits at most 90, saves the response unchanged, and creates
a review plan. The scheduled pipeline **never applies the plan to Wealthfolio**.
Application is a separate, explicitly authorized promotion workflow.

Spending categorization is also separate. It reads the private canonical
history and the original Monarch statement field locally, writes a
privacy-preserving plan, and never sends descriptions to an external service.

## Private files

The default data root is `D:\documents\finance-data`:

```text
simplefin\
  access-url.txt             # secret; created by the existing claim command
  account-map.json           # private durable account decisions
  manual-decisions.json      # source-hashed rulings for every held row
raw\simplefin\YYYY-MM-DD\
  request-NN                 # one of 24 quota slots, including failed attempts
  simplefin-HHMMSS-ffffff.json
normalized\simplefin\
  plan-YYYY-MM-DD-HHMMSS-ffffff.json
  assertions-YYYY-MM-DD-HHMMSS-ffffff.json
```

Raw snapshots are exclusively created and marked read-only. Never move any of
these files into the repository: they contain account names, balances, and
transactions.

## Account mapping and exclusions

Create `simplefin\account-map.json` privately. Keys are stable SimpleFIN account
IDs and values point to existing stable Wealthfolio IDs:

```json
{
  "version": 1,
  "accounts": {
    "synthetic-source-account-id": {
      "action": "import",
      "wealthfolioAccountId": "synthetic-stable-ledger-id",
      "assertionAccountId": "synthetic-durable-fact-id"
    },
    "synthetic-corporate-card-id": {
      "action": "exclude",
      "decision": "employer-corporate-card"
    },
    "synthetic-loan-id": {
      "action": "monitor",
      "wealthfolioAlternativeAssetId": "synthetic-alternative-liability-id",
      "assertionAccountId": "synthetic-durable-loan-id"
    },
    "synthetic-untracked-account-id": {
      "action": "observe",
      "assertionAccountId": "synthetic-durable-account-id"
    }
  }
}
```

Use the second form for an employer-settled corporate card. This durable source-ID
decision is intentional; do not use a name substring, which could accidentally
exclude a personal account. An unmapped source account, unknown target, or
unrecognized exclusion blocks readiness rather than guessing.
`assertionAccountId` is the durable account ID used by balance facts; omit it
only when that ID is the same as the Wealthfolio ID.

Use `monitor` for institution accounts represented as alternative liabilities
in Wealthfolio, such as a mortgage or auto loan. Their balances are compared
and asserted, but their transactions are never planned as account activities:
the alternative-asset API has a different data model and pretending otherwise
would create a second liability.

Use `observe` when an account should remain visible in assertions but does not
yet have an application target -- for example, a new zero-balance card that is
not worth creating in Wealthfolio. Its transactions are retained in the raw
snapshot but never planned for import.

## Pull and plan

Set `WEALTHFOLIO_PASSWORD` for an unattended run, or place it in the existing
private Wealthfolio password file. Then run:

```powershell
python importers\simplefin\cli.py pull-plan `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:8088
```

The command requests pending transactions so they are visible, but marks them
skipped. It reads Wealthfolio accounts and activities without mutation.

## Durable manual rulings

Every held transaction must have a private decision before production planning.
The decision identifies the exact source account and transaction, preserves the
detected reason, records a rationale, and hashes every supporting private file.
Supported rulings are:

- `hold`: evidence is ambiguous or proves the row should remain unapplied;
- `hold-non-household`: investment valuation timing must not become household
  income or spending;
- `pair-existing`: one exact, unlinked existing counterpart is source-proven.

The loader fails closed if evidence changes, a reason changes, a counterpart is
not unique, or a decision no longer matches the reviewed snapshot. Keep this
file under the private data root; never copy it into the repository.
The staging receipt and production plan bind both the exact decision-file hash
and the complete set of nested evidence hashes. Every nested file is rehashed
again immediately before a production mutation.

Bulk activity updates do not reliably persist metadata. When an existing
activity needs metadata repaired, create a private remediation file under the
data root:

```json
{
  "schemaVersion": 1,
  "activities": [{
    "activityId": "<existing-activity-id>",
    "originalFingerprint": "<sealed-semantic-sha256>",
    "desiredMetadata": {"flow": {"is_external": true}}
  }]
}
```

Pass it as `--metadata-remediations <private-file>` to both `plan` and
`promote-plan`. Each repair becomes an explicit sealed operation. Application
guards the current fingerprint, writes the complete activity through
`PUT /activities`, rereads and verifies every semantic field, and handles a
lost response by classifying the reread state. Rollback uses the same endpoint
to restore the sealed original metadata. A remediation-only operation accepts
only its sealed original or metadata-dropped fingerprints as forward states;
any third state fails before the PUT. If a finalized activity is subsequently
linked, the plan separately seals its post-link group identity and internal-flow
metadata. Postflight verifies both the exact two-member group and the normalized
post-link semantic fingerprint.

## Deterministic spending categorization

Build the private August review report against production in read-only mode:

```powershell
python -m importers.simplefin.categorize_cli plan `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:8088 `
  --start-date 2026-08-01 `
  --end-date 2026-08-31
```

The report contains activity and evidence hashes, category totals, current
Wealthfolio totals, high-confidence candidates, and every manual ambiguity.
Only activities inside the requested date window are read for assignments,
classified, counted, or included in a rehearsal. The private data root is
validated against the module's actual repository root, independent of the
shell's current directory, before any key or report is created.
Merchant descriptions are represented by keyed SHA-256 hashes. The stable
private key is created at `simplefin\category-hash-key.txt` and is itself bound
into the plan evidence; this prevents practical merchant-name dictionary
attacks if a report is accidentally exposed. Canonical categories
are authoritative; Monarch's original statement text is used only as another
exact normalized merchant spelling. A merchant with conflicting historical
categories is never applied automatically.

Optional reviewed exceptions live only at
`simplefin\category-decisions.json`. The version 1 document supports
`categoryAliases`, `merchantOverrides`, and `activityOverrides`. Merchant
overrides use a `merchantHash`, may include a `canonicalAccountId`, and require
a rationale. Activity overrides use the exact `sourceAccountId` and `sourceId`.
Both override forms name an existing `taxonomyId` and `categoryId`. This file,
like the generated report, must never enter the public repository.

Transfers are not consumption. Paired transfers, unpaired transfers, and
external reconciliation activities are listed separately and receive no
category operation. An `external_transfer` subtype or canonical
`metadata.flow.is_external=true` marker takes precedence over the activity
type, so a withdrawal carrying either marker cannot become spending. In
particular, a truthful unpaired balance-gap transfer remains visible instead
of being hidden behind a spending category.

Wealthfolio supports category assignments at
`PUT /api/v1/spending/activities/{activityId}/assignments`. This repository
intentionally exposes that endpoint only through a staging rehearsal:

```powershell
python -m importers.simplefin.categorize_cli rehearse `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:<staging-port> `
  --category-plan <private-category-plan> `
  --rebuild-map <private-staging-account-map> `
  --staging-data-dir <private-staging-data-root> `
  --plan-fingerprint <exact-plan-fingerprint>
```

The rehearsal rejects port 8088, validates the plan and every evidence hash,
checks portable source/account identity, creates a database backup, verifies
every assignment, rolls back assignments on failure, and is idempotent on a
second run. There is deliberately no production category-apply command.
Reviewed decisions should first be recorded in the private decision file and,
where they correct history, in canonical category decision facts; a later
reviewed promotion can project those durable decisions to Wealthfolio.

## Guarded production promotion

First create a fresh isolated staging instance from a consistent read-only
snapshot, build a decision-sealed plan, and apply it twice. The first run must
produce `status=applied`; the second must produce `status=already-applied`.

Only then build a production plan. This command reads production but does not
back it up or mutate it:

```powershell
python -m importers.simplefin.apply_cli promote-plan `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:8088 `
  --reviewed-plan <private-reviewed-plan> `
  --manual-decisions <private-manual-decisions> `
  --metadata-remediations <private-metadata-remediations> `
  --staging-plan <private-staging-plan> `
  --staging-receipt <private-applied-receipt>
```

Promotion planning requires the exact staging plan and receipt, matching
portable intent (including canonical create, update, and linked-transfer
account-scoped source identity, subtype, and metadata semantics), current source
hashes, current production
identity, an exact pre-ledger fingerprint, measured spending availability,
ruled manual rows, and no health issue that the plan cannot safely preserve or
repair. It writes a private production-ready report with the plan SHA and
expected cash-flow and category impact.

Production application is deliberately a different command:

```powershell
python -m importers.simplefin.apply_cli promote-apply `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:8088 `
  --application-plan <private-production-plan> `
  --allow-production `
  --expected-plan-sha <exact-sha256>
```

Do not run it during planning. It refuses any other host or port and requires
the exact plan SHA. Before mutation it revalidates all sealed evidence and the
live pre-ledger state, then creates a production backup. Activity counts,
reconciliation state, transfer links, balances, spending, net worth, health,
and the semantic post-ledger fingerprint are verified. Fingerprints include
normalized subtypes and complete canonical metadata so external cash-flow
semantics cannot drift unnoticed. Ambiguous transport or
any failed postcondition triggers a complete, fingerprint-verified rollback.
Rollback rereads transfer link state after an ambiguous unlink response, safely
resumes restoration, and verifies the full sealed pre-ledger fingerprint.
Health verification waits for repeated stable refreshes. Production preflight
rejects every non-INFO code except `transfer_incomplete`, and accepts that code
only when every affected activity is explicitly named by a metadata
finalization or transfer link in the sealed plan. Postflight compares the
multiset of stable issue identities and affected activity IDs, so one resolved
issue cannot hide a newly introduced issue with the same code. Every targeted
transfer error must disappear.

Net-worth conservation is source-safe: every plan-touched account and
alternative holding must retain its value, while valuation-cache movement in
untouched market-priced accounts is reported separately. The receipt records
global value before and after, untouched cache movement, and the unexplained
residual. A residual over one cent remains a hard failure; the workflow never
compares a stale global performance cache directly with a recalculated cache.

### Balance assertions in Spending

An unlinked balance-assertion `TRANSFER_OUT` is an unavoidable Spending
artifact in Wealthfolio 3.7.0. The source row is needed to preserve the
bank-stated balance, and its canonical metadata correctly marks it external.
Wealthfolio classifies an unlinked cash `TRANSFER_OUT` as an expense without
consulting category or external-flow metadata. A category changes only the
breakdown, not the total. An activity type override to an ignored type would
also remove the cash effect, while a linked transfer requires a real
counterpart and would fabricate a destination balance when none is supported
by source evidence. Record the exact amount and identity in the private audit
evidence; do not categorize, override, or pair it merely to suppress Spending.

The relevant upstream classification is
[`classify_activity_for_aggregation`](https://github.com/wealthfolio/wealthfolio/blob/5c49592ab96b881307fb4b0de00bc0f7d2ceb5d6/crates/spending/src/activity_classification.rs):
linked cross-boundary transfers become Saving, linked internal transfers are
neutral, and unlinked cash transfer-outs fall through to Expense.

Application plans containing deletions are rejected before backup or mutation
in both staging and production. Reconciliation activities are updated in place
when a nonzero adjustment preserves identity; an exact replacement that would
require deletion remains held for manual review until database-restore rollback
is implemented and tested.

Dedupe order is:

1. stable source transaction ID within the mapped account;
2. conservative overlap on mapped account, date, exact decimal amount, and a
   description normalized only for case, punctuation, and whitespace.

A unique overlap is skipped. Multiple possible overlaps are marked for review;
the importer never chooses one silently. This catches direct QFX/OFX overlap
even when FITID and SimpleFIN IDs differ. Re-running against an already planned
source ID is exactly idempotent.

SimpleFIN transaction IDs are account-scoped. Transfer decisions, pairing,
ambiguity checks, and promotion intent therefore bind both the source account
ID and transaction ID; a transaction ID reused by another account cannot
inherit a transfer classification or link. A durable pair member, reviewed
counterpart, or resolved existing activity may be claimed only once; competing
claims share one global member registry and fail planning before any operation
is emitted.

The assertions file uses the facts verifier's canonical `balances` snapshot
shape, while also recording activity-derived ledger balance and drift. Run it
through `facts\cli.py verify-assertions` when durable assertions exist for that
date. Drift is a signal only: `balanceAction`/`action` is always
`report-only`, and no correcting transaction is generated. Institution errors
also persist in the snapshot, plan, and assertions and block readiness. Empty
accounts remain explicit zero-transaction account records.

The Bridge limit is enforced locally at 24 attempts per UTC date. Failed
requests count because they may consume provider quota. Daily scheduling stays
well below that ceiling. SimpleFIN's short rolling window maintains current coverage;
it cannot backfill older history.

## Windows Task Scheduler

Preview the installation only:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File importers\simplefin\install-task.ps1 -WhatIf
```

Do **not** install it until the user confirms the schedule, execution identity,
data directory, and password availability. After confirmation, remove
`-WhatIf`; optional parameters include `-At '06:00'`, `-TaskName`, and
`-Python`. The task runs daily, ignores overlapping instances, and invokes only
`pull-plan`. It has no apply or Wealthfolio mutation path.

Inspect Task Scheduler history and the newest private plan after the first
confirmed run. Exit code `2` means the snapshot was preserved but the plan has
blockers requiring attention.
