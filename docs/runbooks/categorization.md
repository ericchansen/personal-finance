# Source-agnostic spending categorization

Wealthfolio holds cash activity from several importers in this repository:
Monarch (legacy history), mapped CSV/OFX/QFX extracts, and SimpleFIN. Only
SimpleFIN activity used to be categorizable. This runbook covers the
source-agnostic workflow that categorizes **every** spending-enabled cash
activity carrying a stable source identity, using the same sealed
plan → staging rehearsal → production promotion safety machinery.

The SimpleFIN-scoped commands in [`simplefin.md`](simplefin.md) are unchanged
and still work exactly as documented. This is an additional entry point, not a
replacement.

Nothing here contacts an external service, uses a model, or sends a merchant
description anywhere. Every decision is a local, deterministic, explainable
join against private canonical data.

An **optional** local-model layer is documented separately in
[`ollama-categorization.md`](ollama-categorization.md). It is off unless you run
`agent-plan`, it runs strictly after every deterministic source below has
declined, and it talks only to a loopback Ollama server.

## How an activity is joined to canonical evidence

Every importer writes a stable `idempotencyKey` onto the Wealthfolio activities
it creates, and the canonical estate writes a matching `source_id` onto the
transaction rows built from the same extract. The two strings are related but
scoped by different account identifiers:

| source      | live `idempotencyKey`                  | canonical `source_id`                  |
| ----------- | -------------------------------------- | -------------------------------------- |
| `simplefin` | `simplefin:<wealthfolioAccount>:<id>`  | `simplefin:<simplefinAccount>:<id>`    |
| `extract`   | `extract:<wealthfolioAccount>:<id>`    | `extract:<stable\|synthetic>:<id>`     |
| `monarch`   | `monarch:<rowId>`                      | `monarch:<rowId>`                      |
| `gap`       | `gap:<wealthfolioAccount>:<day>`       | balance reconciliation, never a purchase |

The trailing token — the per-transaction identifier the institution or export
supplied — is byte-identical on both sides, so that is what the join uses. A
per-transaction id containing a colon survives intact because the split is
bounded.

Resolution runs in two passes over the whole window, so the outcome never
depends on the order Wealthfolio happens to list activities in:

1. **`identity-exact`** — one canonical row has the same `(system, key)`.
2. **`identity-account`** — several rows share the key; exactly one of them sits
   on the canonical account that the live account maps to.
3. **`fallback-unique`** — only for activities the identity pass left
   unresolved, only against canonical rows the identity pass did not already
   claim, and only when exactly one canonical row has the same normalized
   account, date, absolute amount and description *and* no other live activity
   in the window looks identical. This is what covers a CSV extract with no
   FITID, where the canonical build and the Wealthfolio import synthesize
   *different* ids for the same row.

Because the identity pass completes first and claims its rows, an exact match
always outranks a competing fallback match for the same canonical row.

Everything else abstains and is reported for review rather than guessed:
`ambiguous-source-identity`, `ambiguous-fallback-match`,
`ambiguous-canonical-claim`, `unmapped-account`, `unresolved-source-identity`.
If two live activities would claim the same canonical row, *neither* wins.

Pass `--no-fallback` to require exact source identity and switch the
conservative match off entirely.

## Decision order for a resolved activity

Structural kinds are decided **before** any category lookup, so a money
movement can never be read as a purchase:

1. `gap:` activities — balance-gap reconciliation, reported separately.
2. Live `TRANSFER_IN`/`TRANSFER_OUT`, an `external_transfer` subtype, or a
   canonical `metadata.flow.is_external` marker.
3. Canonical `transaction_kind` of `internal_transfer`, `cc_payment`,
   `loan_payment`, `saving`, `investment`, `reconciliation` or `excluded` →
   listed with a `classification` and `categoryAction: "none"`.

Only what survives that filter is categorized, in this order:

4. A private `category-decisions.json` override or reviewed schema v2 rule.
5. **Reviewed canonical carryover** — the exact canonical row's own reviewed
   category, translated through `categoryAliases`. This is what carries a
   mortgage, housing or utility decision from a legacy Monarch or mapped
   extract row onto the live activity built from that same row. The canonical
   builder only writes a `category_id` for `expense`, `income`, `refund` and
   `reimbursement` rows, so a transfer label can never present itself here.
6. **Reviewed canonical merchant consensus** — unanimous reviewed history for
   that exact normalized merchant, account-scoped first (confidence `0.98`),
   then global (`0.95`), and only at or above the minimum evidence threshold.
7. **Wealthfolio's own category history** — see below. Consulted last, only for
   activities still uncategorized, and never to break a canonical conflict.
8. *(optional)* **A local Ollama model** — only when you run `agent-plan`, only
   for what steps 1–7 left unresolved, and only at or above an explicit
   confidence threshold. See
   [`ollama-categorization.md`](ollama-categorization.md).

Direction is handled explicitly. A deposit into a cash account resolves against
the income taxonomy. A credit-card credit reports as negative spending, which is
how a refund reduces the category it originally charged. A cash-account credit
whose canonical category has no income-side meaning abstains as
`credit-direction-review` instead of being filed as income.

## Learning from Wealthfolio's own categorized history

The canonical estate is not the only place a reviewed decision lives.
Wealthfolio holds thousands of them: assignments created by its official
presets, by its categorization rules, and by a human in the Spending UI. A plan
that ignores those asks the operator to re-make every one by hand — which is
what a 244-activity window that auto-categorized six of them was doing.

The planner therefore builds a **private, read-only live history index** before
it decides anything:

- It enumerates already-categorized cash activities over a configurable
  lookback — `--live-history-lookback-months`, default **24**, long enough to
  have seen an annually recurring merchant (insurance, tax prep, memberships,
  registrations) twice — across **every spending-enabled account** and every
  supported source, regardless of the `--source` filter applied to the plan
  window itself.
- Each payee is normalized and immediately replaced by the same keyed
  HMAC-SHA256 digest the plan already uses. **The index never holds merchant
  text**, so no artifact, log line or test can leak a payee.
- Consensus is built account-first, then global, counting **distinct
  activities**: the same activity id observed twice is one observation, so a
  paginated API can never manufacture agreement on its own. Each consensus
  carries its evidence count, first and last dates seen, contributing source
  systems, and Wealthfolio's stated assignment provenance where the build
  exposes one (`unstated` otherwise — it is never invented).

Nothing structural is trained on. Removed *before* a merchant is counted:

| Excluded | Because |
| --- | --- |
| `transfer`, `external-reconciliation` | money movement, not a purchase |
| `structural-subtype` | card payment, loan payment, saving, investment |
| `structural-canonical-kind` | the joined canonical row is a structural kind |
| `reconciliation-source` | `gap:`/`rebuild:` balance reconciliation |
| `excluded-activity` | explicitly excluded in Wealthfolio |
| `synthetic-uncategorized` | Wealthfolio's synthetic "uncategorized" identity |
| `direction-mismatch` | an assignment pointing against the cash-flow direction |
| `not-a-cash-flow`, `account-not-spending-enabled`, `multiple-assignments` | not a single spending/income decision on a spending account |

Every one of those is counted and reported in the plan and the Markdown review,
so it is visible exactly how much candidate evidence each filter removed.

Applying it is deliberately conservative:

- Only activities that are **still uncategorized** are candidates.
- It runs **after** private rules and reviewed canonical carryover/consensus,
  and **before** the activity is written off as manual, so it can only add
  coverage, never overrule stronger evidence.
- Minimum evidence is `--live-history-min-evidence` (default 2). Below it, the
  activity abstains as `insufficient-live-history` and stays visible.
- **Any conflicting category at either the account or the global scope
  abstains.** A merchant that means two things somewhere in the library is
  precisely where a confident guess does damage.
- Direction and taxonomy must agree: expense/refund evidence can only fill a
  spending bucket, deposit/income evidence only an income bucket. A mismatch
  abstains as `live-history-direction-mismatch`.
- Confidence is recorded as `0.97` (account) and `0.94` (global) — a notch
  below the canonical equivalents, so a reviewer can tell at a glance where a
  decision came from.

### Merchant matching without a canonical source identity

Merchant history does not need to know *which* canonical transaction an
activity is; it needs somewhere stable to write the decision and something to
verify byte for byte at promotion time. So when live history is enabled, an
activity whose canonical row is missing, ambiguous or never built is still
reachable: the resolver derives a weaker identity from the live key plus the
account bridge, and the candidate records `identitySource:
"merchant-history"` and `matchKind: "merchant-history-identity"`. Reviewed
canonical carryover still requires a real canonical row and is unaffected. An
activity whose account is not in the account bridge remains manual.

### Sealing and drift

The plan seals the live-history **scope** (lookback, window, account ids,
sources, evidence threshold) and the **exact evidence** each candidate relied
on: scope, merchant digest, taxonomy, category, distinct evidence count, and
the contributing activity ids. The seal carries its own `indexFingerprint` and
sits inside the plan, so it is covered by the plan fingerprint the operator
quotes and the staging rehearsal re-validates.

`validate_category_plan` then requires the seal to explain its own candidates:
a candidate citing unsealed evidence, or disagreeing with the evidence it
cites, is rejected. Promotion goes further and **re-reads exactly those sealed
assignments**. If a human has since re-categorized or cleared one of them, the
consensus that produced the candidate no longer exists and the promotion is
refused with `live category history changed after planning` rather than writing
a decision the operator already reversed. Plans that never consulted live
history carry no seal, need none, and are unaffected.

Pass `--no-live-history` to plan from private canonical evidence alone.

## Learn merchant rules (offline, no session)

Actual Budget's payee rules are the model: an exact normalized merchant maps to
one category, with nothing hidden behind a model or a remote service.

```powershell
python -m importers.categorize.cli merchant-rules `
  --data-dir D:\documents\finance-data `
  --min-evidence 2
```

This reads only private canonical history, learns a rule for every merchant
whose reviewed category is unanimous at or above `--min-evidence`
observations, and writes both a JSON rule set and a Markdown companion under
`normalized\categorize\`. Each rule carries its provenance: evidence count,
which source systems contributed, and the first and last dates observed.

A merchant whose category conflicts *anywhere* produces no rule at either
scope; it is listed under conflicts for a human to resolve, because a
conflicting merchant is exactly where guessing does damage. Merchants below the
threshold are listed separately as pending.

Merchants appear only as the keyed HMAC-SHA256 digest the sealed plan already
uses, so a rule set can be reviewed, diffed and archived without ever writing a
payee string down. The rule set is descriptive, not a second opinion: it is
generated from the same history index the plan resolves against, and a test
asserts a learned rule and the plan candidate it explains agree on digest,
category, evidence count and confidence.

## Plan (read-only)

```powershell
python -m importers.categorize.cli plan `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:8088 `
  --start-date 2026-08-01 `
  --end-date 2026-08-31 `
  --live-history-lookback-months 24
```

Read-only: it enumerates activities, reads assignments (for the window and for
the live-history lookback), reads the Spending report, and writes a private plan
plus a merchant-redacted Markdown companion under `normalized\simplefin\` (the
folder name is historical; the plan itself is source-agnostic).

Useful flags:

- `--source monarch --source extract` — restrict the scope. Repeatable. The
  default is every activity carrying a stable source identity. This narrows the
  *plan window* only; live history always learns from every supported source.
- `--live-history-lookback-months N` — how many calendar months of
  Wealthfolio's own categorized history to learn from, ending on `--end-date`
  (default 24).
- `--live-history-min-evidence N` — distinct already-categorized activities
  required before a live merchant consensus may be applied (default 2).
- `--no-live-history` — ignore Wealthfolio's own assignments entirely.
- `--account-map <path>` — canonical-to-Wealthfolio account map. Defaults to
  `canonical-account-map.json` under the data root, the same file the rebuild
  commands use. A reviewed SimpleFIN source plan contributes the same relation
  for SimpleFIN accounts and is discovered automatically; it is no longer
  required, so a Monarch-only or extract-only library is plannable.
- `--no-fallback` — exact source identity only.
- `--decisions`, `--monarch-history`, `--reviewed-plan` — explicit paths.

A conflicting account mapping (one canonical account pointing at two
Wealthfolio accounts, or the reverse) is a fail-closed error, not a
last-writer-wins merge.

The plan seals its own scope under `sourceSystems`, and its metrics report
`scopedActivities`, `sourceSystemCounts`, `canonicalCarryoverCount`,
`structuralKindCount` and `unresolvedIdentityCount` alongside the existing
counters. Each automatic candidate records `sourceSystem`, `matchKind`,
`canonicalSourceId` and `canonicalTransactionKind` so a reviewer can see exactly
which evidence produced it.

Coverage is reported by evidence kind, and abstention by reason, both entirely
merchant-redacted — every value is a count or a digest:

```
coverageByEvidence=canonical-category:identity-exact=12,account-history=31,live-account-history=104,live-global-history=57
abstentionsByReason=already-categorized=9,conflicting-live-history=6,no-history=14
liveHistory lookbackMonths=24 accounts=7 observed=4180 merchants=612 conflicts=23 sealedEvidence=118
liveHistoryApplied=161 (account=104 global=57) merchantIdentity=88
```

The same breakdown appears in the Markdown review as *Coverage by evidence
kind*, *Abstentions by reason*, *Training exclusions* and *Sealed live-history
evidence*, plus the corresponding `metrics.evidenceKindCounts`,
`metrics.manualReasonCounts`, `metrics.liveHistory*` and
`metrics.merchantIdentityCount` in the plan.

An unsupported or broken Spending endpoint writes the same actionable
`category-plan-blocked-*.json` artifact and returns exit code 3, rather than
crashing or falling back to Wealthfolio's private database.

## Rehearse and promote

The staging rehearsal and production promotion are the same guarded workflow
the SimpleFIN flow uses, and are exposed here for convenience:

```powershell
python -m importers.categorize.cli rehearse `
  --data-dir D:\documents\finance-data `
  --staging-data-dir D:\documents\finance-data-staging `
  --base-url http://127.0.0.1:8089 `
  --category-plan <plan> `
  --rebuild-map <staging account map> `
  --plan-fingerprint <fingerprint printed by plan>

python -m importers.categorize.cli promote `
  --data-dir D:\documents\finance-data `
  --base-url http://127.0.0.1:8088 `
  --category-plan <plan> `
  --rehearsal-receipt <receipt> `
  --plan-fingerprint <fingerprint> `
  --environment-fingerprint <fingerprint> `
  --allow-production
```

Both reconstruct each candidate's idempotency key from its own
`sourceSystem`, so a Monarch candidate looks for `monarch:<key>` and an extract
or SimpleFIN candidate looks for `<system>:<stagingAccount>:<key>`. A candidate
that predates this change carries no `sourceSystem` and is treated as SimpleFIN,
which is the only source it could have come from.

Everything else is unchanged: a fresh confirmed backup, exact plan and
environment fingerprints, a verified pre-state, full rollback on any failure, an
immutable promotion receipt keyed by the plan's SHA-256, and a promotion lock so
a concurrent retry cannot undo a successful run. Promotion remains restricted to
loopback port 8088, and that check runs before any client is constructed, so the
admin password is never sent to a rejected target.

## Backward compatibility

- Plans sealed before this change carry no `sourceSystems` key. They keep the
  original SimpleFIN-only production scope and the original per-transaction key
  derivation, so they still validate, rehearse and promote unchanged.
- Plans sealed before live history existed carry no `liveHistory` key. They stay
  valid, need no seal, and skip the promotion-time drift re-read entirely.
- Plans sealed without a local-model pass carry no `ollamaAgent` key and stay
  valid forever. `build_category_plan` without `agent_suggestions` never
  constructs a model client, and `plan` never runs one at all.
- `python -m importers.simplefin.categorize_cli plan` behaves exactly as before,
  including its requirement for a reviewed SimpleFIN source plan.
- `build_category_plan` without a resolver is byte-for-byte the previous
  SimpleFIN behaviour, and without `live_history` neither the live index nor
  merchant-only identities are ever consulted.

## Residual limitations

- **Activities with no `idempotencyKey`** — hand-entered Wealthfolio rows have
  no stable identity to join on and are always reported as manual. This is
  deliberate: there is nothing to verify a match against at promotion time.
  They are also excluded from live-history training, counted as
  `unknown-source-identity`.
- **Merchants that are new everywhere** — a payee with no reviewed canonical
  history *and* no existing Wealthfolio assignment cannot be resolved by any
  local, deterministic method. It abstains as `no-history`. This is the one case
  the optional local-model pass in
  [`ollama-categorization.md`](ollama-categorization.md) exists to narrow;
  without it, it needs a human.
- **Live-history evidence is a sample at promotion time** — at most 64
  contributing activity ids are sealed per evidence set, so the drift re-read
  stays bounded. The evidence hash covers the full set, so the sample cannot be
  gamed, but a change confined to unsampled observations of a very common
  merchant would not be detected.
- **Split transactions** — exact category splits are summarized in the plan for
  review, but a split parent is still categorized as one row. Wealthfolio
  exposes one category assignment per activity.
- **Tags and events** — schema v2 `decorate` actions remain recorded as
  unapplied; the pinned Wealthfolio build exposes no supported write endpoint.
- **Cash-account refunds without an income-side category** — abstained as
  `credit-direction-review` rather than guessed. Add a `categoryAliases` entry
  mapping the canonical category to an income category to resolve them.
- **Receipt folder name** — plans, rehearsal receipts and promotion receipts are
  still written under `normalized\simplefin\`. Moving them would break
  idempotency detection for receipts already on disk.
