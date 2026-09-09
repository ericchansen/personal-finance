# Accepted event addresses and revisions

The [identity resolver](../finance_store/identity.py) determines generation-local
events. Migration [0018](../deploy/postgres/migrations/0018_accepted_event_identity.sql)
adds durable **accepted event addresses**, not another resolver or a replacement
ledger. Here acceptance means an eligible identity-continuity selection, **not
economic certification or source-health admission**. A stable ID is not proof
that an event is economically correct.

## Explicit selection, never implicit promotion

[`persist_identity_resolution`](../finance_store/identity_postgres.py) still appends
diagnostic evidence only. The new `persist_accepted_identity_resolution` accepts
the **same, unchanged `IdentityResolution`** and persists its evidence and accepted
mappings within the caller's PostgreSQL transaction. It does not commit, deploy,
read private artifacts, contact an application, or run a collector.

The caller supplies `expected_previous_generation_hash`:

- `None` selects the first reviewed generation in an empty registry.
- Otherwise it must equal `accepted_current_generation_hash(connection)`, the
  selected predecessor captured when the acceptance was planned.
- Identity writes first acquire the shared backup/migration writer lock, check
  the durable migration gate, then acquire the identity transaction advisory lock.
  Competing selections from the same predecessor cannot both advance.
- Replaying any already accepted generation is a no-op, including an older
  generation. It never resets the selected head or re-evaluates its conflicts.

New acceptance must match the runtime's latest supported identity and
source-authority policy semantics. Comparison uses the policy documents
dynamically, not hardcoded version labels; only coverage intervals are removed
from the comparison because they are reviewed input scope. A historical version,
legacy authority policy, or weakened rule set cannot bootstrap or advance
accepted-current. Historical resolutions remain persistable through the
evidence-only API. Migration 0018 does not backfill or select any generation.

Capture the predecessor with the reviewed plan; do not substitute a newly read
token at apply time merely to bypass a stale-plan rejection. The selection order
is explicit, not inferred from policy rank, generation numbers, or source clocks.
Nontransactional autocommit writes are refused. An explicit active transaction
on an autocommit-configured connection is supported, including the existing
repository transaction wrapper. The caller must propagate errors so its
transaction rolls back.

## Exact continuity and immutable history

[`accepted_identity.py`](../finance_store/accepted_identity.py) consumes the
resolver's existing source claim IDs. A provider-backed claim already includes
source family, connection, source account, provider-ID kind, and provider token.
The registry separately pins canonical-account ownership. Moving the same claim
to another canonical account is a conflict, not a rename or a new identity.
The source claim's canonical-account snapshot is retained separately from the
resolver-selected event owner: an explicitly resolved cross-account provider
mirror is not reinterpreted by this layer, but subsequent ownership drift is
still blocked.

The initial ID is deterministically seeded from account hash and a sorted claim
anchor. Once accepted, the registry—not the seed—defines continuity:

| Transition | Result |
| --- | --- |
| Identical replay / reordered input | No new generation, mapping, or revision |
| New sighting or selected version of an existing claim | Same accepted ID; changed evidence gets a revision |
| Resolver adds corroborating source claims | Same accepted ID when exactly one prior identity is involved |
| Amount, date, status, category, or selected-description change through a claim | Same ID; changed selected evidence gets a revision |
| Independent new event | Independent deterministic accepted ID |
| Emitted event belongs to a resolver-classified unresolved component | Local `unresolved-identity-component`; no new accepted identity or revision |
| Selected source is excluded/untrusted, or its account state is unknown | Local qualification conflict; no accepted-current row |
| Two prior accepted identities become one resolver event | Local `many-existing-merge` conflict |
| One prior identity becomes multiple resolver events | Every affected child gets `one-existing-split` |
| Canonical owner changes | Local `account-ownership-change` conflict |

Conflicts retain their prior IDs, candidate binding IDs, and explicit reason codes, but receive no
accepted mapping, new claim ownership, or selected revision. Unrelated safe
events can still advance. The API returns these local outcomes; it does not
automatically merge or split identities, even when a newer resolver override
merges its own generation-local evidence.

Eligibility consumes the resolver's `residual_classification` and exact
decision memberships, not the fact that an event was emitted. Every open
duplicate decision touching an event is recorded by hash. Historical automatic
decisions still classified as unresolved are not silently treated as superseded
merely because another decision or override also exists.

Revisions reference the original generation event; historical policy hashes,
claim memberships, observation memberships, and application binding origins
remain unchanged. Revision fingerprints include policy, selected fields, and
claim/observation evidence. An unaffected event in a different generation can
reference its existing revision. Missing events are absent from the selected
input scope, **not removed from current reads**. Omission creates no new
observation, mapping, or revision.
Because reviewed coverage intervals participate in the full policy hash,
multiple immutable policy documents may share a semantic version. Migration
0018 removes only the old name/version uniqueness constraint; policy-hash
uniqueness and all existing identity/hash/version references remain enforced.

All six acceptance tables are append-only. Foreign keys pin account ownership
and source evidence; unique constraints prevent multiple owners for one claim,
multiple selected mappings of one accepted ID in a generation, competing
successors, duplicate revision numbers, and application-target reuse.

## Existing projection identities

`persist_application_projection_bindings` records already-applied application
identities; it still performs **no HTTP writes**. For accepted generations it
uses the stable mapping and preserves the original binding row instead of
creating another active generation-dependent binding.

At initial acceptance, a historical active binding can be attached if all its
source claims map unambiguously to this event and its owner agrees. Historical
bindings that straddle children, have unaccounted members, introduce a separately
projected identity into another accepted event, or offer competing targets are
local conflicts. No historical binding or generation ID is rewritten. Inactive
bindings and target changes require a future reviewed transition mechanism;
they are not silently replaced.

The schema provisioning grant list is extended for the six tables, so
re-provisioning after restore does not revoke their insertion privileges.

## Read contract and limits

`finance_read.accepted_identity_current` uses each accepted identity's most recent
disposition across all accepted selections, rather than filtering identities to
the global head. It exposes qualified selections,
stable ID, revision, generation event ID, policy hashes, source day, amount,
currency, status, trusted flag, observation/description/category hashes, and any
known projection binding. `finance_read.accepted_identity_history` includes all
accepted transitions and conflict reasons, including selections no longer current.
New unresolved events are absent from current. When an ambiguous transition
touches an existing identity, the last accepted revision remains visible with
`selection_status = 'retained-prior'`, `identity_status = 'needs-review'`, the
current candidate event IDs, and conflict reasons. Its sourced amount, status,
policy, and binding remain those of the prior revision, not the ambiguous new
candidate. `selection_generation_hash` distinguishes the current selection
from that revision's historical `generation_hash`. Explicit exclusion, trust
cutoff, unknown account state, or ownership drift does **not** reactivate prior
state through this fallback.

Bounded and failed-source selections do not assert completeness. An identity
omitted from the latest selection is retained with
`selection_status = 'not-observed-in-current-selection'`; its amount, currency,
source evidence, revision, and binding are carried forward without manufacturing
a zero or a fresh sighting. `disposition_status` and
`disposition_generation_hash` retain its last actual qualification or conflict.
An unresolved identity remains `needs-review` through subsequent omission.
An explicitly excluded, untrusted, unknown-owner, or ownership-drift identity
stays absent through omission: the view checks its most recent disposition,
not merely its highest revision. Later observed, eligible evidence can continue
the same registered identity.

`disposition_event_ids` identifies the last actual disposition's event evidence.
`current_candidate_event_ids` is empty for omitted identities, so an earlier
candidate is never described as a current observation. These read qualifications
do not append replacement evidence or imply source-health certification.

All rows explicitly report `source_admission_status = 'not-evaluated'`,
`currency_evidence_status = 'resolver-supplied-unverified'`, and
`is_economically_certified = false`. `IdentityResolution` does not carry a source
health admission proof or proof that its currency was explicitly supplied rather
than defaulted by an upstream adapter. This layer neither infers those proofs
from `trusted` nor substitutes a default currency: it exposes the resolver's
value with the missing-proof qualification.
The current view is not a replacement projection plan: an absent or conflicted
event does not authorize deleting its existing application activity.
The separate [incremental qualification and operation views](runbooks/incremental-agent-queries.md)
bind eligible scoped source events to explicit currency, freshness, cash-anchor,
and observed application evidence. Identity acceptance alone does not replace
those checks.

These views do not invent plaintext descriptions, account names, household
joins, or relationships to `canonical_transactions` / `projection_records`.
Those older tables require real account and projection provenance unavailable
from every resolver generation. A hash is not a replacement for that provenance.

No migration backfills or selects old diagnostic generations. A caller must
explicitly select reviewed evidence. There is no automatic accepted-identity
merge, split, retirement, account remap, re-selection of an old generation, or
application rebind in this slice. Source claims without stable provider identity
remain observation-scoped; changed IDs with no shared claim require new evidence
or a future reviewed continuity decision. IDs are stable within a persisted
acceptance history, not guaranteed to match a different first-acceptance history.

## Synthetic verification

The focused tests exercise the real resolver, including declared shared-provider
token corroboration, and the real PostgreSQL adapter. Native tests opt in with:

```powershell
$env:FINANCE_ACCEPTED_IDENTITY_POSTGRES_TEST = '1'
python -m pytest tests\test_accepted_identity.py tests\test_accepted_identity_postgres.py -q
```

The suite creates its own uniquely named PostgreSQL container and loopback port,
applies all immutable migrations, exercises existing provisioning SQL and schema
integration checks, and clones an isolated synthetic database for each test.
It verifies replay, revisions, policy history, local conflicts, preserved
bindings, bounded-scope omission, persistent exclusions, later return continuity,
rollback, concurrent writers, and SQL guards; its resources are removed
in fixture cleanup. It accepts no shared database DSN.
