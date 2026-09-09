# Read-only incremental evidence

Use the existing `finance_shadow_agent` login and `finance_read` views.
Migration 0021 adds operation history without granting access to writer tables.
These queries describe configured cash/card scopes, not complete household
coverage. Bind parameters through the database driver; never interpolate IDs.

## Latest source balance and delivery health

```sql
SELECT scope_id, state, source_balance, currency_code,
       source_observed_at, balance_effective_at, processed_at,
       last_source_run_hash, evidence
FROM finance_read.incremental_scope_status
WHERE scope_id = %(scope_id)s;
```

This is an observed source balance, not a sum of accepted transactions.
Report both source and balance timestamps and the current state/reason.
A failed later run can retain the last known balance: it does not make that
balance fresh. Compare the timestamps with the scope's pinned freshness policy.

## What the worker actually changed

```sql
SELECT operation_id, accepted_event_id, revision_number, operation_kind,
       source_day, description, currency_code, source_amount,
       planned_cash_effect, operation_state, observed_activity_id,
       attempt_recorded_at, run_hash, snapshot_hash, source_anchor_transition
FROM finance_read.incremental_projection_history
WHERE scope_id = %(scope_id)s
  AND attempt_recorded_at >= %(since)s
  AND attempt_recorded_at < %(until)s
ORDER BY attempt_recorded_at, operation_id
LIMIT 100;
```

Only `operation_state = 'applied'` records an acknowledged app effect.
Pending, prepared, held, and uncertain intentions are not delivered cash.
`planned_cash_effect` is the operation's signed effect; `source_amount` is the
complete underlying source amount. They can differ for an explicitly qualified
source-delta representation. Do not count both.

This view retains historical operations after a later no-op refresh. Current
qualification or an adopted app row is not evidence that the worker created it.
Inspect `source_proof`, `source_anchor_transition`, and `operation_evidence`
for the exact source membership, checkpoint, and observed app result.

## Current event qualifications and exceptions

```sql
SELECT accepted_event_id, source_day, description, signed_amount, currency_code,
       qualification_status, reason, projection_status, activity_id,
       collection_observed_at, financial_observed_at, last_sighted_at,
       snapshot_hash, manifest_hash, policy_hash, proof
FROM finance_read.incremental_cash_events
WHERE scope_id = %(scope_id)s
  AND source_day >= %(from_day)s
  AND source_day < %(through_day_exclusive)s
ORDER BY source_day DESC, accepted_event_id
LIMIT 100;
```

Held and pending events are visible evidence, not certified financial totals.
Unqualified history is not zero; current eligible rows do not establish complete
spending coverage. Pair these results with scope status and known interval
coverage before answering an aggregate question.

For one source identity's revisions, query
`finance_read.incremental_source_history` with exact `scope_id` and `source_id`
filters, order by `version_number`, and bound the result count. That history
retains unqualified legacy pending dates and the reason they were not admitted.
The anchor status and cash-representation views distinguish observed posting
progress from request coverage and full events from source-delta cash.
