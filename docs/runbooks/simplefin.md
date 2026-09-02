# SimpleFIN — safe scheduled transaction plans

SimpleFIN is read-only, but its access URL is a credential for every connected
institution. It must remain outside this public repository. The pipeline fetches 45 inclusive calendar days by default (the Bridge's live
recommendation), permits at most 90, saves the response unchanged, and creates
a review plan. The scheduled pipeline **never applies the plan to Wealthfolio**.
Application is a separate, explicitly authorized promotion workflow.

Spending categorization is also separate. It reads the private canonical
history and the original Monarch statement field locally, writes a
privacy-preserving plan, and never sends descriptions to an external service.

## Wealthfolio Spending setup checklist

Complete this checklist in an isolated staging copy before planning any bulk
categorization. Record household-specific choices only under the private data
root, never in this runbook.

- [ ] Keep the deployed `WF_IMAGE` at an explicit version and digest. Run the
  read-only capability diagnostic with that exact reference:
  ```powershell
  python -m importers.analytics.cli diagnose-wealthfolio-capabilities `
    --data-dir <finance-data> `
    --base-url http://127.0.0.1:8088 `
    --image-reference <exact-WF_IMAGE-value>
  ```
- [ ] In Wealthfolio Spending settings, enable only cash and credit accounts
  whose transactions should participate in household spending. Reconcile this
  selection with the private SimpleFIN account map.
- [ ] Review the Spending, Income, and Savings taxonomies. Keep user-facing
  categories to no more than two levels, use stable meanings, and resolve
  duplicates before adding rules.
- [ ] Create budget groups, assign each budgeted category once, and review
  rollover settings for every group. Confirm the displayed period before
  saving targets or copying a prior period.
- [ ] Create a fresh database backup, restore a separate staging instance, and
  rehearse the exact categorization plan twice. The second rehearsal must be
  idempotent before any separately authorized production workflow.
- [ ] Review the private capability publication under
  `normalized\analytics-diagnostics\wealthfolio-capabilities`. An unsupported
  category, rule, or budget interface blocks that integration; it never
  authorizes direct writes to Wealthfolio's private database tables.

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
    "synthetic-summary-id": {
      "action": "exclude",
      "decision": "aggregator-account-summary",
      "duplicateOfSourceAccountId": "synthetic-source-account-id"
    },
    "synthetic-dormant-id": {
      "action": "exclude",
      "decision": "dormant-zero-balance-account"
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

Use `aggregator-account-summary` only when a provider emits an umbrella account
that duplicates an imported detail account. `duplicateOfSourceAccountId` must
identify that imported source account, and every pull blocks if their currency,
balance date, balance, or transaction semantics diverge.

Use `dormant-zero-balance-account` for an intentionally untracked account only
while it has both a zero balance and no transactions in the fetched window. Any
balance or activity blocks the plan so an exclusion cannot silently hide a
reactivated account.

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

This section covers the **SimpleFIN-scoped** categorization flow. To categorize
every spending-enabled cash activity — Monarch, mapped CSV/OFX/QFX extracts and
SimpleFIN together — see
[`categorization.md`](categorization.md), which reuses the same sealed plan,
staging rehearsal and production promotion machinery described below. The
commands here are unchanged and keep working exactly as documented.

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
like the generated report, must never enter the public repository. Version 1
documents continue to load and produce exactly the same plans as before; there
is no migration requirement.

### Schema v2 staged rule engine

The private decisions file also accepts `"schemaVersion": 2`: a declarative,
staged rule engine with stable rule IDs, replacing `merchantOverrides` and
`activityOverrides` with explicit `rules`. `categoryAliases` is unchanged and
shared by both schema versions. A v2 document has this shape:

```json
{
  "schemaVersion": 2,
  "categoryAliases": {},
  "rules": [
    {
      "id": "stable-rule-id",
      "stage": "categorize",
      "priority": 10,
      "enabled": true,
      "reviewed": true,
      "stopProcessing": true,
      "rationale": "Human-readable reason this rule exists.",
      "when": {"field": "payeeHash", "op": "exact", "value": "<sha256-hex>"},
      "actions": [
        {"type": "setCategory", "taxonomyId": "spending_categories", "categoryId": "groceries"}
      ]
    }
  ]
}
```

Every transaction is evaluated through four non-recursive stages, in this
fixed order, with no stage re-entering an earlier one:

1. **normalize** — `setPayee` may correct the normalized payee text used by
   later text conditions. The raw source description is always kept separate;
   `merchantHash` is always derived from the raw description, never from a
   normalize-stage rewrite.
2. **classify** — `setTransactionKind` annotates a rule-local transaction
   kind. Setting it to `excluded` removes the transaction from automatic
   categorization (it becomes a manual `rule-excluded` item) without touching
   the structural cash-flow totals, which are computed independently.
3. **categorize** — `setCategory` assigns a `taxonomyId`/`categoryId`,
   exactly like a v1 override. Only a rule with `"reviewed": true` (required,
   no default) can auto-apply; a matching but unreviewed rule instead produces
   a distinguishable manual item (`reason: "unreviewed-rule-match"`, with the
   matched `ruleId`) rather than being silently applied or silently ignored.
4. **decorate** — `addTag`/`setEvent` propose tags or an event. Wealthfolio
   does not currently expose a supported write endpoint for these, so every
   decorate action is always recorded in the trace as
   `{"applied": false, "reason": "unsupported-destination"}`. Decorate always
   runs, even for an excluded transaction, since tag/event annotations are
   orthogonal to categorization.

Within a stage, rules are evaluated in a deterministic order (ascending
`priority`, then `id`); a rule with `"stopProcessing": true` halts further
rules in that stage once it matches, so rule order and stop behavior never
depend on file or dict iteration order.

Conditions support `exact`/`contains`/`startsWith`/`regex` text matching on
`payee`; an exact `payeeHash` (the same keyed SHA-256 used by v1 merchant
overrides, enabling privacy-preserving matching without plaintext); `account`
(`is`/`in` a canonical account ID); `activityIdentity` (exact
`sourceAccountId`+`sourceId`, for migrating v1 activity overrides);
`cashBucket` and `transactionKind` (structural/rule-local classification);
`amount` (inclusive range) and `direction` (`debit`/`credit`); `date` (range);
and `pendingState` (`pending`/`posted`). Conditions combine with `{"all": [...]}`
or `{"any": [...]}` groups, nested at most one level deep. All of this parsing
is strict and fail-closed: an invalid regex, an unknown field/op/action,
duplicate rule IDs, over-deep nesting, a rule action naming an unknown
category, and a rule whose `direction` condition contradicts its category's
taxonomy (e.g. `debit` paired with an income category) are all rejected before
any transaction is evaluated.

Every automatically categorized transaction's plan entry carries a per-rule
`ruleTrace` (matched rule IDs, their `reviewed` state, and decorate results)
so a review can show *which rule* matched — the private plan JSON and its
Markdown companion report an `evidenceKind` of `rule:<id>` (or
`unreviewed-rule:<id>`) and a `ruleId` column, but never merchant text. As
with v1, a rule/decisions-file edit changes the evidence hash bound into the
plan, which invalidates any previously sealed plan.

A non-mutating migration tool proposes a v2 document from an existing v1
file, purely offline (no Wealthfolio session required):

```powershell
python -m importers.simplefin.categorize_cli migrate-decisions `
  --data-dir D:\documents\finance-data
```

This reads the v1 `category-decisions.json`, converts each merchant/activity
override into an equivalent `reviewed`, `stopProcessing` categorize-stage
rule, carries `categoryAliases` through unchanged, self-validates the result,
and writes it to a sibling `category-decisions.v2-proposal.json` — the source
file is never modified. Pass `--decisions`/`--output` to use different paths,
or `--force` to overwrite an existing proposal. Because the migration is
offline, it cannot check that a proposed `categoryId` still exists in the live
catalog; that check still happens, fail-closed, the first time an actual plan
is built from the adopted rules.

A fully synthetic reference document, showing all four stages and the
condition/action vocabulary above, lives at
`importers/simplefin/category-decisions.example.json` in this repository (it
contains only placeholder `example*` text and zeroed hashes and is safe to
commit). Copy it to `simplefin\category-decisions.json`, delete the
`_comment` key, and replace every value with your own reviewed rules.

Transfers are not consumption. Paired transfers, unpaired transfers, and
external reconciliation activities are listed separately and receive no
category operation. An `external_transfer` subtype or canonical
`metadata.flow.is_external=true` marker takes precedence over the activity
type, so a withdrawal carrying either marker cannot become spending. In
particular, a truthful unpaired balance-gap transfer remains visible instead
of being hidden behind a spending category.

### Transfer review memory and exact splits

The canonical manifest supplies the category plan with private, merchant-free
`transferReview` and `splitReview` summaries. The Markdown companion shows:

- exact transfer candidates still awaiting review, including only stable
  candidate/evidence IDs, currency, date distance, and ambiguity;
- confirmed and rejected decision counts, with matching rejections shown as
  suppressed rather than proposed again;
- exact split group/decision IDs, child counts, and parent amounts.

Candidate discovery never links activities. It considers only different,
non-excluded owned accounts, exact opposite `Decimal` amounts, identical
currencies, and dates no more than five days apart. Fuzzy descriptions are
evidence hashes only and can never confirm a pair. Ambiguous same-amount matches
remain proposals until an explicit private `transfer-confirmed` fact identifies
the exact candidate and evidence hash. A `transfer-rejected` fact suppresses
that exact evidence; changing underlying evidence causes the candidate to
reappear as `evidence-changed`.

Exact category splits are also private decision facts. Canonical child amounts
must reconcile exactly to their parent; one child may be a deterministic
residual. Stable child and group IDs make rebuilds idempotent. Transfer,
external-flow, investment, and reconciliation rows cannot be category-split.
See `importers/normalized/README.md` for the synthetic decision shapes. The
private review never includes merchant or payee text.

Wealthfolio supports category assignments at
`PUT /api/v1/spending/activities/{activityId}/assignments`. This repository
uses only that supported REST endpoint for category writes. Rehearse first:

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
checks portable source/account identity, creates a database backup, rereads
every assignment and the Spending report, verifies category totals, income,
spending, and the uncategorized count, rolls back assignments on failure, and
is idempotent on a second run. Preserve the first `status=applied` receipt; an
`already-applied` receipt does not authorize production.

### Explicit category production promotion

Do not run this command while planning or reviewing. First confirm that the
private plan contains no unresolved transfer candidates, stale transfer
decisions, unpaired transfers, or unresolved reconciliation rows. Then copy
the exact plan fingerprint printed by `plan`, calculate the production
environment fingerprint through the same read-only identity check used during
planning, and supply both explicitly:

```powershell
python -m importers.simplefin.categorize_cli promote `
  --data-dir <finance-data> `
  --base-url http://127.0.0.1:8088 `
  --category-plan <exact-private-category-plan> `
  --rehearsal-receipt <exact-private-applied-rehearsal-receipt> `
  --plan-fingerprint <exact-plan-fingerprint> `
  --environment-fingerprint <exact-production-environment-fingerprint> `
  --allow-production
```

Promotion fails closed unless the receipt seals the exact plan SHA and
fingerprint, the operator and live environment fingerprints match the plan,
all evidence hashes and the complete pre-category state are unchanged, and
the live Spending summary still matches its sealed pre-state. Immediately
before writes it requests a backup and proves that the returned filename is
new, listed by Wealthfolio, non-empty, and timestamped.

After writing, promotion rereads every touched assignment and the supported
Spending report/search endpoints. Exact category amounts and counts, income,
spending, net, activity count, and uncategorized count must equal the sealed
post-state. Any write, reread, report, or immutable-receipt failure restores
every touched assignment to its before value and verifies that restoration.
The private receipt under `normalized\simplefin` seals the plan and rehearsal
hashes, production identity, backup metadata, and before/after assignments and
report values. It is created exclusively and marked read-only. Repeating the
same command verifies and returns that receipt without another backup or
write. Never move plans or receipts into this public repository, and never
open or modify Wealthfolio SQLite files.

### The Spending capability-gated adapter

`importers/simplefin/spending_adapter.py` is the single entry point for every
call this repository makes against Wealthfolio's Spending REST surface
(`/api/v1/spending/*`). `categorize_cli.py`, `categorization.py`, and
`apply_cli.py` no longer issue ad hoc `client.get`/`.put`/`.post`/`.delete`
calls against Spending paths directly; they go through `SpendingAdapter`,
which:

- makes exactly one HTTP call per logical read or write (no separate
  "probe" round-trip);
- classifies every `WealthfolioError` it observes as `unsupported` (HTTP 404
  or 405 — the endpoint does not exist on this Wealthfolio build) or `error`
  (any other HTTP failure), and classifies a structurally wrong response body
  as `incompatible`;
- caches the classification per capability for the lifetime of the adapter,
  so a capability that is unsupported is reported once, not re-derived on
  every call;
- raises `SpendingCapabilityBlocked` — never a raw `WealthfolioError` — for
  any unsupported, incompatible, or broken capability, carrying a
  `CapabilityStatus` (capability name, status, endpoint, human explanation);
- never falls back to a local database of any kind. A blocked capability is
  surfaced as an explicit artifact for a human to act on, not silently routed
  around.

`SpendingAdapter.capabilities()` returns a diagnostic snapshot of every
read-safe capability this repository depends on, without ever attempting a
write.

#### Verified supported endpoints (Wealthfolio 3.7.0)

Read from the pinned upstream source at tag `v3.7.0`
(`apps/server/src/api/taxonomies.rs`, `apps/server/src/api/spending.rs`, the
matching `crates/core/src/taxonomies` and `crates/spending` models, and the
official web client in `apps/frontend/src/adapters/web/core.ts`). Every path
below is one this adapter is allowed to call; nothing is guessed.

| Capability                | Method | Path                                                    |
| ------------------------- | ------ | ------------------------------------------------------- |
| `taxonomyList`            | GET    | `/taxonomies`                                           |
| `taxonomyRead`            | GET    | `/taxonomies/{taxonomyId}`                              |
| `taxonomyCategoryCreate`  | POST   | `/taxonomies/categories`                                |
| `taxonomyCategoryUpdate`  | PUT    | `/taxonomies/categories`                                |
| `taxonomyCategoryMove`    | POST   | `/taxonomies/categories/move`                           |
| `taxonomyCategoryDelete`  | DELETE | `/taxonomies/{taxonomyId}/categories/{categoryId}`      |
| `spendingSettingsRead`    | GET    | `/spending/settings`                                    |
| `spendingReportRead`      | POST   | `/spending/report`                                      |
| `spendingSearchRead`      | POST   | `/spending/cash-activities/search`                      |
| `activityAssignmentRead`  | GET    | `/spending/activities/{activityId}/assignments`         |
| `activityAssignmentWrite` | PUT    | `/spending/activities/{activityId}/assignments`         |
| `activityAssignmentDelete`| DELETE | `/spending/activities/{activityId}/assignments/{taxonomyId}` |
| `categorizationRuleRead`  | GET    | `/spending/rules`                                       |
| `categorizationRuleWrite` | POST   | `/spending/rules`                                       |
| `categorizationRuleUpdate`| PUT    | `/spending/rules/{ruleId}`                              |
| `categorizationRuleDelete`| DELETE | `/spending/rules/{ruleId}`                              |
| `categorizationRuleRerun` | POST   | `/spending/rules/rerun`                                 |
| `budgetRead`              | GET    | `/spending/budget`                                      |
| `budgetTargetWrite`       | POST   | `/spending/budget/targets`                              |
| `budgetTargetDelete`      | DELETE | `/spending/budget/targets/{targetId}`                   |
| `budgetRolloverWrite`     | POST   | `/spending/budget/rollovers`                            |
| `budgetRolloverDelete`    | DELETE | `/spending/budget/rollovers/{settingId}`                |
| `budgetGroupCreate`       | POST   | `/spending/budget/groups`                               |
| `budgetGroupUpdate`       | PUT    | `/spending/budget/groups/{groupId}`                     |
| `budgetGroupDelete`       | DELETE | `/spending/budget/groups/{groupId}`                     |
| `budgetGroupAssign`       | POST   | `/spending/budget/group-assignments`                    |
| `budgetTargetCopy`        | POST   | `/spending/budget/copy`                                 |
| `databaseBackup`          | POST   | `/utilities/database/backup`                            |
| `databaseBackupRead`      | GET    | `/utilities/database/backups`                           |

Every budget mutation accepts an optional `?periodKey=` (`default` or
`YYYY-MM`, defaulting to the current month in the instance timezone) and
returns the whole refreshed `BudgetSnapshot`, which the adapter validates
structurally before returning it.

An earlier revision of this runbook and of `spending_adapter.py` recorded
`rule_write` and `budget_write` as permanent API gaps. That was **wrong**:
Wealthfolio 3.7.0 exposes full CRUD for categorization rules, budget targets,
budget rollovers, and budget groups, as well as category create/update/move/
delete. The table above replaces that claim, and a contract test now asserts
those capabilities are never re-declared as gaps.

#### Genuine gaps in 3.7.0

These four are recorded as `unsupported` with endpoint `(no route)` and no
network call, because the pinned build genuinely has no route for them:

| Capability               | Gap                                                                                                                                    |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `taxonomyMergeImport`    | `POST /taxonomies/import` always creates a new, non-system taxonomy; nothing merges a taxonomy document into the existing system one.    |
| `taxonomyCategoryPatch`  | No partial category update: `PUT /taxonomies/categories` requires a complete `Category`, so a rename is a read-modify-write.             |
| `taxonomyCategoryReadOne`| No single-category read; a category is only reachable via its whole taxonomy.                                                            |
| `budgetPeriodList`       | No endpoint enumerates configured budget periods; the caller must already know the `periodKey` it wants.                                 |

#### Configuration writes are locked by default

`SpendingAdapter(client)` cannot create, rename, move, or delete a category,
cannot write a categorization rule, and cannot write any budget object. Those
methods raise `SpendingCapabilityBlocked` with status `locked` and make **zero**
network calls. They are only reachable from
`SpendingAdapter(client, allow_configuration_writes=True)`, which no read-only
or planning command ever constructs. Activity category assignment — the only
write the promotion pipeline performs — is unaffected and stays governed by the
existing rehearsal/promotion fingerprint gates.

### Compact taxonomy planning (read-only)

`importers/simplefin/taxonomy_plan.py` holds a generic, PII-free recommended
hierarchy and diffs it against the live taxonomy. Wealthfolio itself enforces
no depth limit, so the **at most two visible levels** rule is enforced here.

```powershell
python -m importers.simplefin.categorize_cli taxonomy-plan `
  --data-dir $env:FINANCE_DATA `
  --base-url http://127.0.0.1:8088 `
  --taxonomy spending_categories
```

The command only calls `GET /taxonomies/{id}` and writes a private plan under
`<data>/normalized/simplefin/`. It emits `create`, `rename`, `reparent`,
`keep`, and `review` operations, ordered so a parent is created before the
child that moves under it. It **never** emits a delete: an unrecognised live
category is surfaced as `review`, because deleting one would silently detach
historical activity assignments. A live category nested three or more levels
deep is flagged for flattening. Applying a plan is a separate, deliberate
operator action; the CLI ships plan-only.

### Budget proposals from live Wealthfolio (read-only)

`importers/simplefin/live_budget.py` proposes a monthly budget target per
category from the **live** instance. The canonical export carries this
repository's provisional categories, not the ones curated inside Wealthfolio,
so a canonical-sourced proposal covered nothing a real budget group contains.
The live instance is the source of truth for "which category is this spending
in", so the proposal reads it.

```powershell
python -m importers.simplefin.categorize_cli budget-propose `
  --data-dir $env:FINANCE_DATA --base-url http://127.0.0.1:8088 `
  --months 12 --min-months 6
```

- The window is the caller's `--months` **complete** trailing calendar months.
  The month containing `--as-of` (default today) is always excluded, including
  on its last day, because a partial month drags every median down.
- One `POST /spending/report` per month, plus `GET /taxonomies/{id}` and
  `GET /spending/budget`. Nothing else is called and nothing is written.
- Only categories a human already assigned to a native Wealthfolio budget group
  are considered. Anything else is listed under `excluded` with a reason.
  Categories and budget groups are never created to make a proposal fit.
- A category with fewer than `--min-months` months of observed spending is
  reported under `insufficientHistory` rather than given a fabricated target.
  A category curated into a group that never spent in the window appears there
  with `monthsObserved: 0`.
- The target is the median of the months that had spending, rounded up to the
  next `roundingStep`. Only `category` targets are ever proposed.
- Every live value the proposal depends on is hashed into an `evidence` block:
  a per-month report hash, a taxonomy hash, and a budget-curation hash over the
  groups and group assignments. Existing budget *targets* are deliberately not
  sealed there, since they are what promotion writes; they are checked by the
  conflict rules instead.
- The whole document is covered by a `proposalFingerprint`.
- Amounts stay in the private proposal file under `FINANCE_DATA`. Terminal
  output is counts, a path, and fingerprints only, never a category name or a
  currency value.

`importers/simplefin/budget_proposal.py` remains as an offline cross-check
against canonical history and supplies the shared median window and rounding.
It imports no Wealthfolio client and calls no write endpoint; a contract test
asserts that structurally.

#### Guarded budget promotion

Promotion is the only path that writes a budget, and it writes exactly one kind
of object: a `category` budget target.

```powershell
python -m importers.simplefin.categorize_cli budget-promote `
  --data-dir $env:FINANCE_DATA --base-url http://127.0.0.1:8088 `
  --budget-proposal $env:FINANCE_DATA\normalized\simplefin\budget-proposal.json `
  --proposal-fingerprint <exact> --environment-fingerprint <exact> `
  --allow-production
```

Every one of these must hold or nothing is written:

- `--allow-production` is present, and the base URL is loopback port 8088.
- The operator-supplied proposal fingerprint matches the sealed document
  exactly, and the document's own fingerprint re-derives.
- The operator-supplied environment fingerprint matches both the live instance
  and the fingerprint recorded when the proposal was made.
- The pinned build serves supported budget-target write and delete endpoints;
  a known API gap fails closed.
- Live evidence still hashes to the sealed `evidence` block, so no
  recategorization happened since the proposal.
- No conflicting target exists for a proposed category in that period.
- A database backup is demonstrably fresh: absent from the pre-backup listing,
  present exactly once afterwards, with a positive size and a modification time.
- The proposal file's SHA-256 is unchanged immediately before, and again after,
  the mutation.
- Every written target is re-read from Wealthfolio and matched by id and
  amount, and no target the proposal did not name was altered.

If any step fails, every target created in the run is deleted and the previous
target list is re-read to confirm the rollback; a rollback that does not restore
the previous state is raised as such rather than swallowed.

On success an immutable receipt (`0444`, exclusive create) is written to
`normalized/simplefin/budget-promotion-<proposal-sha256>.json` recording the
endpoints used, the backup, the evidence, and the before/after target lists.
A second run finds that receipt, re-validates it against the live state, and
returns `already-applied` without writing or backing up again. A lock file
serializes concurrent runs.

The client is wrapped in a proxy that permits only reads, backups, and budget
*target* writes. Creating a category, a budget group, or a rule is not a policy
this path declines — it is an operation it cannot perform. Wealthfolio's SQLite
database is never opened.

#### Blocked-plan and blocked-receipt artifacts

If a Spending capability the plan needs (taxonomy, settings, report, search,
or assignment reads) is unsupported, incompatible, or broken on the target
instance, `categorize_cli.py plan` never crashes with a raw HTTP error and
never falls back to a private database. Instead it writes a
`normalized/simplefin/category-plan-blocked-<timestamp>.json` artifact
(`mode: "category-plan-blocked"`, `productionMutation: false`) naming exactly
which capability blocked it, and exits with status code `3` — distinct from
the generic decision-error exit code — so the gap is scriptable and
actionable rather than silent.

The staging rehearsal applies the identical policy for writes: if assignment
reads, assignment writes, or the pre-mutation backup are unsupported partway
through a rehearsal, `rehearse_category_plan` rolls back exactly like any
other mid-rehearsal failure, then returns a receipt with `status: "blocked"`,
`productionMutated: false`, and a `blockedCapability` block naming the exact
capability and endpoint — instead of an unhandled exception. Any other kind
of failure (a genuine network error, an ambiguous PUT response, etc.) is
unaffected and still raises, exactly as before.

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
