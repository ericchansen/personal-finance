# Canonical transaction identity

PostgreSQL is the evidence authority. Wealthfolio is an application projection:
it may be rebuilt from reviewed canonical state, but it is never a source of
identity truth merely because a row currently exists there.

The resolver in `finance_store/identity.py` is pure and deterministic. It reads
immutable observations, builds a typed evidence graph, emits stable canonical
events and versioned policy decisions, and never deletes an observation. The
canonical CSV publication invokes the same resolver before publishing
`transactions.csv`. The private shadow runner writes only to the external data
directory and has no Wealthfolio mutation client.

## Model boundaries

| Record | Meaning |
|---|---|
| Observation | One immutable assertion from one source artifact |
| Source claim | All versions sharing one provider ID within source connection and account scope |
| Evidence edge | A typed relationship between two claims |
| Canonical event | One economic event supported by one or more claims |
| Decision generation | One policy hash applied to one exact input hash |
| Automatic decision | A reproducible policy result with evidence and cardinality proof |
| Human override | An append-only, higher-precedence ruling |
| Event relationship | Transfer, correction, reversal, or pending lineage without an identity merge |
| Application projection | A disposable binding from one canonical event to an application row |

Raw provider IDs and account IDs are not stored in the identity tables. Their
scoped hashes are stored instead. Transaction observations remain immutable in
their existing evidence tables.

## Invariants

1. **No observation deletion.** Resolution changes membership and projection,
   never source evidence.
2. **Provider IDs are scoped.** An ID is exact only inside its source family,
   source connection, and source account. SimpleFIN identity is account plus
   transaction ID. OFX/QFX identity uses the account-scoped FITID.
3. **Writer timestamps are provenance.** Midnight (`T00`) and noon (`T12`)
   signatures identify writers or export conventions. Matching uses the source
   day, never those synthetic times as transaction time.
4. **Category is enrichment only.** Category agreement or disagreement cannot
   create or block an identity merge.
5. **Transfers are not duplicates.** Opposite signs on different accounts create
   transfer relationships. `sourceGroupId` or an explicit counterpart makes the
   relationship authoritative; an amount/date match alone remains a transfer
   candidate. Neither path merges the legs.
6. **Repeated purchases remain separate.** Same-day, same-amount, same-description
   claims with competing peers form one-to-many or many-to-many components and
   cannot auto-resolve.
7. **Trust boundaries are enforced.** An observation after its account trust
   cutoff remains evidence but cannot produce an active projection. Closed
   accounts retain trusted history.
8. **Human rulings win transitively.** The latest valid append-only human
   override constrains whole connected components. If A must remain distinct
   from B, no sequence through C may join them.
9. **Stable identity is order-independent.** Claim IDs, canonical event IDs,
   generation hashes, decision hashes, and the canonical state hash derive from
   sorted content, never ingestion order or wall-clock time.
10. **Projection uniqueness is structural.** PostgreSQL permits at most one
    active Wealthfolio binding for a canonical event and at most one canonical
    event for an active Wealthfolio activity.
11. **Coverage authority is declared, never inferred.** Source coverage intervals
    come from explicit extraction evidence. The resolver never derives a
    coverage window from the minimum and maximum dates it happens to observe.
12. **Occurrences are conserved across matchers.** One automatic component may
    not contain two distinct claims from the same source family and canonical
    account unless explicit lifecycle lineage or a human merge authorizes it.
    Occurrence capacity is reconciled independently for each reporting source.

## Authoritative source coverage

The authority policy version is `canonical-source-authority-v3`. It is versioned
independently of the identity policy, hashed, and bound to every generation and
to every suppression decision it produces. `IdentityPolicy.document()` always
emits the `sourceAuthority` key, so a deployment that declares no coverage is
distinguishable from one published by a writer that could not declare any. An
empty authority still hashes stably, and the conservative behavior of a
deployment that declares nothing is unchanged.

### Coverage evidence

`build_source_authority` accepts explicit evidence records and refuses anything
implied. Every record must state:

| Field | Meaning |
|---|---|
| `canonical_account_id` | The canonical account the interval speaks for |
| `effective_from` / `effective_through` | The claimed authoritative interval |
| `source_family`, `source_connection_id`, `source_account_id` | The source scope |
| `format_strength` | `stable-provider-id`, `posted-observation`, `synthetic-csv`, or `legacy-export` |
| `stable_id_support`, `replay_stable_ids` | Whether the format carries replay-stable IDs |
| `extraction_requested_from` / `extraction_requested_through` | The window actually requested from the source |
| `extracted_at`, `freshness_as_of` | When the extract ran and how current it is |
| `completeness` | `complete`, `partial`, or `unknown` |
| `source_transaction_count` | The count the source itself declared |
| `source_hashes` | Sorted, unique SHA-256 artifact hashes |
| `trust_cutoff_day` | The account trust cutoff, when one exists |
| `posting_date_tolerance_days` | Optional, default `0`: the declared source-day window this interval may reach across (see below) |

An interval that starts before its extraction request, ends after it, or extends
past the trust cutoff is rejected at construction. Two overlapping intervals for
the same source scope and canonical account are rejected. Output is sorted by
`intervalId`, so evidence order cannot change the result.

### Default rank

Rank is assigned by format strength and only when the `(family, strength)` pair
is explicitly listed in the policy:

| Source | Strength | Rank |
|---|---|---:|
| OFX, QFX | `stable-provider-id` | 4 |
| SimpleFIN | `posted-observation` | 3 |
| Mapped extracts | `synthetic-csv` | 2 |
| Monarch | `legacy-export` | 1 |

A source whose strength claims replay-stable IDs but whose evidence does not
support them loses its rank entirely. If any participant in a comparison bucket
is unranked, nothing in that bucket is suppressed. Ties at the top rank suppress
nothing. Monarch categories remain enrichment only and never participate.

### Proving an interval

An interval is `proven` only when its completeness matches the policy
requirement, its settlement lag (freshness minus interval end) meets the policy
minimum, its declared count meets the policy minimum, and it carries source
hashes. It is `reconciled` only when the number of observations the resolver
actually matched to the interval equals the declared source transaction count.
`authoritative` requires proven, reconciled, and a rank.

### Where an OFX/QFX interval's boundaries come from

An interval's boundaries are never inferred from the data. `build_source_authority`
refuses a record that does not state `effective_from` and `effective_through`,
because the earliest and latest rows a source happened to deliver are evidence of
what arrived, not proof of what the source would have reported. A stable export
covering a quiet month legitimately contains nothing; taking the window from row
extrema would silently erase that month's coverage.

For OFX and QFX the institution states the period itself, in the `DTSTART` and
`DTEND` of the transaction list. `read_ofx_statement_window` reads exactly those
two tags, scoped to the `BANKTRANLIST` / `CCTRANLIST` / `INVTRANLIST` block so a
`DTSTART` belonging to some other part of the document is never mistaken for a
statement window. A record may then bind `ofx_statement_window` instead of literal
dates. The binding is only admitted when:

- the record's `source_family` is `ofx` or `qfx` — a statement window cannot
  license a SimpleFIN or Monarch interval;
- the window names a SHA-256 the record already declares in `source_hashes`, so
  the dates and the bytes that stated them are bound together;
- any literal `effective_from` / `effective_through` the record also carries
  agrees exactly with the header, rather than quietly overriding it.

Binding a window produces the identical `interval_id` as stating the same two
dates by hand: the header is a way of *reading* the claim, not a different claim,
so no existing evidence or policy hash moves.

An incoherent header is refused, not repaired. Some institutions emit an export
whose `DTEND` precedes its `DTSTART`; `OfxStatementWindow` raises on construction
rather than swapping, clamping, or falling back to the transactions. Falling back
would be precisely the inference this rule exists to forbid. A missing, unreadable,
or impossible date is refused the same way, and a document that declares more than
one statement window must be disambiguated by account rather than resolved by
taking the first or the widest.

The private reader resolves the declared statement path under the private root,
verifies its SHA-256 against the operator's declaration before parsing, and refuses
a path that escapes the root. `importers.extracts.parsers.parse_ofx` also surfaces
`statement_start` / `statement_end` when the header is usable; parsing stays
permissive there, since an unusable window must not cost us the rows, and the
authority builder remains the place where an unusable window is refused.

### Suppression

Within one proven authoritative interval, a lower-ranked overlapping observation
may be classified `source-suppressed` and attached to the authoritative event as
provenance and enrichment, producing no second canonical event. Comparison is
bucketed by canonical account, source day, signed amount, and currency, so an
opposite-sign or different-account transfer can never enter the same bucket.

Repeats are handled by deterministic one-to-one multiset matching, never fuzzy
description guessing.

Once both intervals in a bucket are proven, reconciled, and complete, the
authoritative source is what *supplies* the economic events for that account and
day. Coverage and counts alone still do not prove which lower-source row belongs
to which authoritative occurrence. Pairing therefore requires both occurrence
capacity and deterministic membership evidence:

- lower occurrences whose normalized description exactly matches an unclaimed
  authoritative occurrence are paired with it first, so three coffees and two
  books in one bucket pair like with like;
- non-exact descriptions stay in review. A shared phone number, store identifier,
  or unclassified alphanumeric token is not transaction identity, even if the
  candidate is unique in its day/amount bucket;
- unmatched lower observations stay open rather than being paired by arbitrary
  stable order;
- an authoritative occurrence is claimed at most once, so authoritative
  multiplicity is never reduced;
- if the lower count exceeds the authoritative count, exactly the excess
  occurrences stay open as `ambiguous-lower-source-multiplicity`, naming only
  the unexplained claims rather than blocking the whole bucket;
- if two sources at the top rank disagree about how many events occurred, the
  bucket records `ambiguous-authoritative-multiplicity` and suppresses nothing,
  because there is no single proven multiplicity to pair against.

This never reaches past the bucket key: same-source repeats, different accounts,
opposite signs, different currencies, non-posted rows, and observations outside a
proven interval are unaffected, since they never enter a bucket together in the
first place.

`SourceAuthorityPolicy.count_based_occurrence_pairing` (default `true`, emitted
as `countBasedOccurrencePairing`, with `descriptionParticipates` recording
`exact-only` or `required`) selects this behavior.
Setting it to `false` restores the earlier all-or-nothing description-group
gate. The switch exists so an older generation can be replayed exactly; it is
part of the hashed policy document, so a decision always states which rule
produced it.

An explicit `canonical-source-authority-v2` policy retains the old
`exact-or-shared-discriminating-token` rule only to reproduce historical decisions.
It must not authorize new repairs. Identity v5 and authority v3 remove that rule;
the bounded repair builder refuses v4/v2 evidence rather than silently reusing an
old suppression. Exact description matching still requires independently justified
coverage and occurrence capacity; neither description equality nor replayed hashes
alone certify economic identity.

When a producer can bind a transaction row to the exact source artifact bytes,
the interval counts and selects only claims whose `sourceArtifactSha256` occurs
in the interval's declared source hashes. Repeated exports therefore cannot
make an older or unrelated file satisfy a newer coverage declaration merely
because its account, date, and total count happen to match.

Suppression never applies to:

- opposite-sign or different-account transfers;
- different IDs from the same source;
- observations outside a proven overlapping coverage interval;
- non-posted entries;
- conflicting currencies or amounts;
- claims already carrying a transfer, correction, reversal, pending, or mirror
  edge; or
- ambiguous lower-source observations, which are recorded as
  `ambiguous-lower-source-multiplicity`,
  `ambiguous-authoritative-multiplicity`,
  `ambiguous-authority-description-mapping`, or
  `ambiguous-authority-posting-window` and remain unresolved.

Human overrides take precedence: a pair blocked by an override is never
suppressed. `allow_lower_multiplicity_excess` is refused by construction.

### Declared posting-date window

Two writers can stamp one settlement on different days. A stable `.qfx` extract
records the institution's posting date; a legacy aggregator export records the
day it saw the row. The same economic event then arrives with source dates a day
or two apart, and same-day bucketing leaves it unresolved forever.

The window that closes that gap is evidence, never an inference. An interval's
coverage evidence may declare `posting_date_tolerance_days`, and only then may
that interval reach a lower-ranked observation stamped on a different day. The
field defaults to `0`, is bounded by `MAX_POSTING_DATE_TOLERANCE_DAYS`, and is
emitted into the evidence document only when non-zero — so an interval that
expects same-day agreement hashes exactly as it did before the field existed and
every existing decision is byte-identical.

The window is narrow by construction:

| Rule | Effect |
| --- | --- |
| Only the *authoritative* interval's declaration counts | A lower source cannot widen what may be taken from it |
| `stable_id_support` **or** `replay_stable_ids` is required | Something has to pin which row is which across a widened window |
| Both source days must sit inside *both* intervals | The authority never reaches a day it did not claim to cover |
| Exactly one authoritative day may be reachable | Two competing days record `ambiguous-authority-posting-window` and suppress nothing |
| The pairing is re-proven after the occurrence mapping | The claim finally paired may not be the one that drew the row onto the day |
| Amount, sign, currency, account, posted status, trust cutoff, and reconciled counts are unchanged | The window widens *which day*, nothing else |

A windowed decision records what actually happened rather than pretending the
writers agreed: `sameSourceDay` becomes `false`, and the feature vector gains
`sourceDayDistanceDays`, `postingDateToleranceDays`, `authoritativeSourceDay`,
and `suppressedSourceDay`. The competing-candidate proof gains
`sourceDayDistanceDays` and `maxPostingDateToleranceDays`. All four extra
feature keys and both proof keys appear only where a distance exists.

Migration `0014_authority_posting_window.sql` stores the declared window on
`finance.canonical_identity_authority_intervals` and mirrors both rails as
schema constraints. It rewrites nothing in `0011`, `0012`, or `0013`: the
suppression proof shape is unchanged, because `0012`'s
`authoritativeCount`/`lowerCount`/`bucketParticipantCount` branch already accepts
additional camelCase integer keys.

#### Provider-stable is not the only way to pin a row

`0014` first required provider-stable identifiers, which turned out to be too
narrow. A synthetic CSV extract assigns row identifiers deterministically from
content and occurrence (`extract:synthetic:<content+occurrence hash>`). Those are
*not* provider-stable — the institution never issued them, and they say nothing
about a row's identity outside this extract — but they are **replay-stable**: the
same extract always yields the same identifiers in the same order.

Replay stability is what a widened window actually needs. The window does not
match rows by identifier; it widens which authoritative day a lower row may be
paired against, and the pairing itself is a one-to-one occurrence mapping over a
fixed multiset. Replay-stable identifiers keep that multiset and its order fixed,
so the mapping cannot drift between runs. A source with neither kind of
identifier still may not declare a window, because nothing pins which row is
which at all.

Nothing else relaxes. The occurrence mapping stays one-to-one, multiplicity is
preserved in both directions, both intervals must still prove complete,
reconciled, overlapping coverage of both days, exactly one authoritative day may
be reachable, and descriptions must provide exact or discriminating-token
membership evidence. A source that is merely replay-stable also still fails
`stable_identity_strengths` if its format claims a rank that demands provable
provider identity — `synthetic-csv` does not.

Migration `0015_replay_stable_posting_window.sql` replaces the single `0014`
CHECK with `posting_date_tolerance_days = 0 OR stable_id_support OR
replay_stable_ids`. The new predicate is strictly weaker, so every row that
satisfied the old constraint still satisfies the new one; no column, view,
validator, or stored decision changes.

### A relationship is not a licence to reinterpret a leg

Coverage suppression is a statement about *one account*: two writers reported the
same leg, so keep the authoritative copy. A relation between two claims can only
veto that when it asserts something about the economics of the claim itself.

Only five relation kinds do: `correction`, `reversal`, `pending-transition`,
`mirrored-provider-error`, and an already-planned `source-suppressed` link. A
claim touched by any of them is excluded from coverage suppression outright.

Transfers are deliberately not on that list, and neither are transfer or mirror
*candidates*. A transfer is a cross-account statement about two distinct economic
legs; it says nothing about whether one of those legs was written down twice
inside one account. Treating it as a veto meant that an account with many real
transfers — or many coincidental same-amount cross-account neighbours — could
never be deduplicated at all, because almost every claim carried some edge.

Both legs always survive, so nothing is lost by allowing the deduplication. Each
leg's source copies collapse onto that leg's canonical event, and the relation is
remapped onto the surviving events after canonicalization; a relation whose two
sides land on the same event is dropped rather than published as a self-loop.

One guarantee is kept absolutely: collapsing source copies may never merge a
declared transfer's two endpoints into one canonical event. A pair that is itself
a declared transfer — an explicit `transfer` edge or an operator `transfer`
override — is refused before it is ever planned. Because bucket pairing produces
a one-to-one matching, no chain of suppressions can merge two claims that were
not directly paired; `_preserve_transfer_endpoints` enforces that invariant
anyway, applying suppressions in stable sorted order and recording any refusal as
`ambiguous-transfer-endpoint-collapse` so a future change cannot silently erase a
leg.

### Residual classification and proof

Every automatic decision carries a residual classification: `distinct`,
`source-suppressed`, `transfer`, `correction`, `reversal`, or `unresolved`. It is
derived from the outcome and the rationale code, and is deliberately outside the
decision hash body, so existing decision hashes are unchanged.

`unresolved` has to mean exactly one thing — an open duplicate question nothing
has classified — because that is the number the cutover is gated on. Two
populations used to inflate it without being duplicate questions at all:

| Decision | Was | Is | Why |
| --- | --- | --- | --- |
| `transfer-candidate` | `unresolved` | `transfer` | The *relationship* is unproven, not the identity. Both legs are real, separate economic events in different accounts; neither is a duplicate of the other, and nothing about the pair is waiting to be deduplicated |
| `exclude-untrusted` | `unresolved` | `distinct` | A trust cutoff or account exclusion is explicit lineage, not ambiguity. The observation is recorded, kept out of the trusted ledger, and its event stays distinct |

The `transfer-candidate` remap is the only rationale-level override, and it is
the only one that should ever exist: reclassifying by rationale string is how a
real duplicate group could be hidden, so the table stays a single entry that a
test pins. No new residual class was introduced — both populations land in
classes the taxonomy already declares.

Nothing becomes invisible. `unresolvedDecisions` is now built from the residual
classification rather than the raw outcome, and everything it stops carrying is
republished:

- `relationshipCandidateDecisions` publishes every review-required decision that
  is not an open duplicate question — unproven transfer candidates alongside
  cross-account mirror candidates, with the same document shape.
- `counts.transferCandidates`, `counts.crossAccountMirrorCandidates`,
  `counts.reviewRequiredRelationshipCandidates`, and
  `counts.excludedUntrustedObservations` keep each population individually
  countable.
- The excluded observation, its feature vector (`afterTrustCutoff`,
  `accountExcluded`) and its source hash are unchanged.

Projection semantics do not change. `unresolvedDuplicateGroups` counted the same
set before and after — it already excluded transfer candidates by rationale —
and no canonical event, merge, suppression, or relationship differs.

Every automatic suppression stores the authoritative and suppressed interval IDs,
the authority policy hash and version, the full feature vector, the source
hashes, the candidate degrees and occurrence counts, and a stable decision ID.
The resolver additionally asserts, after the fact, that every suppressed claim
landed on the same canonical event as its authoritative claim.

`report_document()` exposes only safe aggregates: interval counts, effective-date
extents, per-class residual counts, `sourceSuppressedClaims`,
`authorityCoveredClaims`, `authorityAmbiguousGroups`, and
`authorityPostingWindowSuppressions`. `source_authority_document()` reports each
interval's `postingDateToleranceDays`, and
`finance_read.identity_posting_window_summary` aggregates windowed decisions by
source family and distance. No account identifier, description, provider
identifier, or amount appears in any of them.

## Declared duplicate-summary accounts

Some aggregator connections expose a *summary* account that restates the same
economic activity already reported by one or more detail accounts. Nothing in
the resolver ever infers that relationship. A cross-account mirror is only
suppressed when the durable private account map declares it, per account, with
all three of:

- `action: "exclude"`,
- `decision: "aggregator-account-summary"` (the recognised decision kinds are an
  explicit parameter, not a guess), and
- `duplicateOfSourceAccountId` naming a different source account in the same
  source family.

`duplicate_summary_mappings(document, map_hash=...)` parses those entries into
`DuplicateSummaryMapping` values. It refuses a self-target, a repeated
declaration for one account, and any chain (an account that is both a declared
duplicate and the target of another declaration). The map hash is the SHA-256 of
the raw map bytes and is required.

`observations_from_transaction_rows(..., duplicate_summaries=...)` joins on the
exact `(source family, source account id)` decoded from the row's `source_id`
— never on a name or a description — and attaches three observation attributes:
`duplicate_summary_of_account`, `duplicate_summary_decision_hash`, and
`duplicate_summary_map_hash`. With the default empty mapping tuple the
observation stream is byte-identical to before.

`importers/lineage_review/canonical.py` reads the declarations once per
projection from `<root>/simplefin/account-map.json`, hashing the raw bytes. A
missing file yields no declarations; an unreadable or invalid file is a review
error, never a silent skip.

### Suppression gates

Within a declared account pair, a duplicate-side claim is only suppressed when
every one of these holds:

- same canonical day, same signed amount, same currency (an opposite sign is
  additionally re-asserted as disqualifying, so a transfer is unreachable),
- non-zero amount,
- both sides `posted`,
- both sides within their trust cutoff,
- neither side carries a `source_group_id` (declared transfer grouping wins),
- neither side participates in an explicit reversal/correction/provider-error
  lineage attribute,
- the authoritative side is not itself excluded or itself a declared duplicate,
- no human `preserve-distinct` or `transfer` override covers the pair.

### One-to-one occurrence mapping

Claims are bucketed by `(family, duplicate account, target account, decision
hash, map hash, day, signed amount, currency)`. Inside a bucket, normalized
descriptions must map uniquely between the two sides — an equivalence or
token-boundary relation, never a fuzzy score, and never double-claiming a
description group. A group where the duplicate side outnumbers the
authoritative side is refused as `ambiguous-declared-duplicate-multiplicity`; an
ambiguous description relation is refused as
`ambiguous-declared-duplicate-description-mapping`. Both are emitted as
`unresolved` decisions and suppress nothing.

Surviving pairs map the i-th sorted duplicate claim to the i-th sorted
authoritative claim, which makes the result replay- and order-stable. Each pair
is re-verified for day, amount and currency before it is emitted.

The relation reuses `RelationKind.MIRRORED_PROVIDER_ERROR`, so the outcome is
`SUPPRESS_MIRROR` and the residual class is `source-suppressed`; the rationale
code is `declared-duplicate-summary-suppression`. The decision stores the
suppressed and authoritative claim IDs, the map hash, the decision hash, the
source hashes, and a competing-candidate proof of
`{authoritativeCount, lowerCount, bucketParticipantCount, occurrenceIndex}`.

When a bucket resolves completely, the other duplicate/authoritative
combinations inside it are explained by that recorded occurrence proof and are
not re-raised as generic `cross-account-mirror-candidate` review items. When a
bucket is refused, they are left in place as review signal.

`report_document()["counts"]` gains `declaredDuplicateSuppressions` and
`declaredDuplicateAmbiguousGroups`. Neither the map contents nor any account
identifier reaches the report.

Declarations are account-scoped and never resolve same-account cross-source
cardinality ambiguity — that remains the job of proven coverage-interval
authority.

### An unproven mirror is a relationship, not a duplicate

The generic heuristic that notices two similar same-sign observations in
*different* canonical accounts produces a `RelationKind.MIRROR_CANDIDATE` edge.
That edge is a graph observation about the ledger, not a claim that one of the
two rows is a copy of the other: two accounts really can be charged the same
amount on the same day, and only the explicit provider-error mapping above knows
whether a given pair is one economic event or two.

The candidate is therefore classified `PRESERVE_DISTINCT`, with residual class
`distinct` and rationale code `cross-account-mirror-candidate`:

- both canonical events survive, with every observation still attached;
- nothing is suppressed, merged, or dropped;
- the pair does **not** count toward `unresolvedDuplicateGroups`, so it cannot
  block a projection that gates on unresolved duplicates;
- the decision keeps `ConfidenceTier.REVIEW_REQUIRED` and is published under
  `report_document()["relationshipCandidateDecisions"]` with both claim IDs,
  both canonical event IDs, its feature vector, its competing-candidate proof,
  and both source hashes, so it stays queryable for audit;
- `report_document()["counts"]` carries the tally separately as
  `crossAccountMirrorCandidates`.

`transfer-candidate` is unaffected: an opposite-sign cross-account pair is still
an `UNRESOLVED` review residual (and, as before, is excluded from the duplicate
count). Reclassifying mirrors changes which *question* the residual asks, not
how carefully it is asked.

## Scoped shared provider tokens

Two writers can legitimately emit the *same* provider token for the same
economic event. An OFX/QFX extract preserves the institution's `FITID`, and a
SimpleFIN snapshot of the same institution can echo that identifier verbatim
after its own family prefix. The tokens are equal because they came from the
same upstream ledger, not by coincidence.

That is still not a reason to equate identifiers across providers. A token is a
namespace-local string: `MERCHANT-001` from one aggregator and `MERCHANT-001`
from another mean nothing to each other. Unscoped token equality would merge
unrelated events, so it is never used.

The engine therefore only accepts a shared token as lineage when an operator has
recorded a durable, versioned **provider token scope**. The policy version is
`canonical-provider-token-scope-v1`, and the declaration lives in the private
map at `<root>/identity/provider-token-scopes.json`:

```json
{
  "providerTokenScopes": [
    {
      "canonicalAccountId": "<canonical account>",
      "decision": "shared-provider-token-namespace",
      "decidedAt": "2026-02-01",
      "maxDaySkew": 1,
      "namespaces": [
        {
          "sourceFamily": "ofx",
          "sourceAccountId": "<source account>",
          "providerIdKind": "ofx-fitid",
          "tokenPrefix": "extract:stable:"
        },
        {
          "sourceFamily": "simplefin",
          "sourceAccountId": "<simplefin account>",
          "providerIdKind": "simplefin-id",
          "tokenPrefix": "simplefin:<simplefin account>:"
        }
      ]
    }
  ]
}
```

A missing file yields no declarations. An unreadable file raises
`provider-token-scope-map-unreadable`; a file that declares the same namespace
twice raises `provider-token-scope-map-invalid`. Neither ever silently degrades
to a default. An entry whose `decision` is not recognised, or that does not name
exactly two namespaces, is a note rather than an authority and is skipped.

### The three-fact proof

A token counts as scoped lineage only when all three of the following hold for
each side of the pair:

1. The observation's `(sourceFamily, sourceAccountId)` matches a namespace that
   the operator declared, and both namespaces belong to the same scope.
2. The observation's `providerIdKind` matches the namespace's declared kind, and
   that kind is one of `ofx-fitid`, `simplefin-id`, or `scoped-provider-id` —
   the kinds where a provider identifier is stable and replay-safe. A
   `synthetic` or `none` identifier is never eligible, so a synthetic CSV can
   never be token-scoped.
3. The observation's canonical account equals the scope's `canonicalAccountId`.

The token itself is the literal remainder after the namespace's declared
`tokenPrefix`. The prefix is declared, never guessed and never split on the
first `:`, so a token containing separators is never mis-parsed. A row whose
source id does not start with the exact declared prefix contributes no token.

Because all three facts are required, the attributes `provider_token`,
`provider_token_scope_hash`, and `provider_token_scope_map_hash` are only ever
attached inside a proven scope. Attaching nothing is the default, so a
resolution with no declared scopes is byte-identical to one that predates the
feature.

### Settlement gates

Within a scope, a token bucket settles into a single canonical event only when
every one of these holds. Any failure raises a visible ambiguity instead:

| Gate | Failure rationale |
| --- | --- |
| Exactly one claim on each declared side | `ambiguous-shared-token-multiplicity` |
| Every member on a declared side of this scope, same canonical account | `ambiguous-shared-token-scope-membership` |
| Equal signed amount and currency | `ambiguous-shared-token-economic-conflict` |
| Source-day distance within the declared `maxDaySkew` | `ambiguous-shared-token-date-window` |

`maxDaySkew` is capped at three days and defaults to one. It exists because two
writers can stamp the same settlement on adjacent days; it is not a fuzzy
matching window, and it never relaxes the amount, currency, or account gates.

The scope is additionally refused outright — with no ambiguity raised, because a
stronger rule already explains the pair — when either side is a transfer-group
member, is non-posted, sits past a trust cutoff, belongs to an excluded account,
carries a zero signed amount, or already has explicit reversal, correction,
pending, or provider-error lineage. Opposite-sign pairs are never settled. A
human override always wins.

### What is recorded

A settled pair produces a `duplicate-candidate` relation with outcome
`merge-claims`, confidence tier `explicit-lineage`, and rationale code
`scoped-shared-provider-token-lineage`. The competing-candidate proof is
`{leftNamespaceCount, rightNamespaceCount, sharedTokenBucketCount,
dateDistanceDays, maxDaySkewDays}` — the namespace counts are what demonstrate
the occurrence mapping was one-to-one rather than assumed.

The feature vector records `scopedSharedProviderToken`,
`providerTokenScopePolicyVersion`, `providerTokenScopeHash`,
`providerTokenScopeMapHash`, `providerTokenHash`, `canonicalAccountHash`,
`sameCanonicalAccount`, `sameSignedAmountAndCurrency`, `sameSourceDay`,
`dateDistanceDays`, `maxDaySkewDays`, and `sourceFamilyPair`. The token is
stored **hashed**: `providerTokenHash` is a content hash, never the raw value.
Category and writer timestamp never participate.

Because the link is proven rather than inferred, the graph-degree heuristic that
downgrades ordinary duplicate candidates to
`ambiguous-cross-source-cardinality` skips scoped-token edges. A competing
candidate elsewhere in the same component stays a competing candidate; it does
not weaken the proof.

`report_document()["counts"]` gains `scopedSharedTokenLinks` and
`scopedSharedTokenAmbiguousGroups`. `provider_token_scope_document()` reports
the declared scopes as a safe aggregate: hashes, kinds, skew, and per-scope
settled counts. No token, source account, or canonical account value appears in
either.

Migration `0013_scoped_provider_token_lineage.sql` stores the declaration in
`finance.canonical_identity_provider_token_scopes` — namespaces and accounts
hashed, append-only — extends `valid_competing_candidate_proof` with the new
proof shape without narrowing any shape `0012` already accepted, and exposes
`finance_read.identity_provider_token_scope_summary`. No relation kind, outcome,
or confidence tier needed widening.

### What a scope cannot do

A scope settles a token bucket and nothing else. Two observations with different
provider tokens — the extract/Monarch residuals, for instance — are untouched by
any declaration and can only be resolved by proven source-coverage authority and
reconciliation. Declaring a scope never changes the outcome for such a pair.

## Automatic confidence rules

The policy version is `canonical-identity-v5`. Its complete policy document is
hashed and stored with every generation.

### Publishing the policy binding

The rebuild projector binds to `identityPolicy` and re-derives every hash from
the published policy document rather than trusting the published values, so
`policyDocument` is part of the block and
`_validate_published_policy_document` refuses a publication whose document does
not produce its own hash:

| Field | Derivation |
| --- | --- |
| `identityPolicy.policyHash` | `sha256(canonical_json(policyDocument))` |
| `policyDocument.version` | equals `identityPolicy.policyVersion` |
| `policyDocument.sourceAuthority.policyHash` | `sha256(canonical_json(.sourceAuthority.policy))` |
| `identityPolicy.sourceAuthority.authorityHash` | `sha256(canonical_json(policyDocument.sourceAuthority))` |

`identityPolicy.sourceAuthority` is a safe aggregate — it hashes account
identifiers and reports counts — and must also hold together on its own:
`intervalCount`, the `declared` flag, and the interval ids have to agree with
the intervals it lists, with no id repeated.

Each aggregate entry's `intervalId` is the hash of the corresponding interval
document in `policyDocument.sourceAuthority.intervals`, so an aggregate can
never claim an interval its own evidence does not contain.
`AuthorityInterval.document()` therefore does not carry its own id — the id
*is* that hash.

Because producer and consumer live in different modules, the published block is
checked against the projector's own validator in
`test_published_identity_policy_matches_the_projector_binding_contract`. Each
side previously had passing tests against its own fixtures while disagreeing
about the key set, and neither suite caught it.

`v2` supersedes `canonical-identity-v1`, which omitted the `sourceAuthority`
key when nothing was declared. Because the policy document is embedded in every
generation and decision, the version bump is a deliberate policy migration; see
[Replay and policy migration](#replay-and-policy-migration).

### Exact scoped identity

Observations with the same source family, source connection, source account,
provider ID kind, and provider transaction ID form one source claim. This is
10,000 basis points (`exact-scoped-identity`). Replays, pending-to-posted
versions, and changed source semantics remain separate observations inside that
claim. Changed posted semantics produce a correction decision instead of
rewriting history.

Synthetic fallback IDs and unscoped/unknown IDs do not qualify as exact provider
identity. Canonical row inputs that lack a source connection therefore do not
reuse generic provider IDs. The only account-scoped exceptions are the explicit
contracts above: SimpleFIN account plus transaction ID and OFX/QFX account plus
FITID.

### Unique cross-source duplicate

The score is the sum of six required binary terms:

| Feature | Basis points |
|---|---:|
| Same canonical account | 2,500 |
| Same signed amount and currency | 2,500 |
| Same source day | 1,500 |
| Exact normalized description or token-boundary prefix | 2,000 |
| Distinct known source families | 1,000 |
| One-to-one graph component | 500 |
| **Required total** | **10,000** |

The first five features create a candidate edge worth 9,500 basis points.
One-to-one cardinality raises its evidence score to 10,000, but description and
cardinality do not establish source lineage: this edge remains review-only.
Automatic cross-source merging requires either a declared scoped provider token
or complete, reconciled, exact-artifact-bound source authority. Every
one-to-many or many-to-many component remains unresolved regardless of textual
similarity. Otherwise-similar observations up to three source days apart are
also retained as review-only candidates.

Description normalization is Unicode-normalized and punctuation-insensitive.
Prefix evidence must end on a token boundary and meet the policy's minimum
length. Source connection, import/receipt lineage, and writer signature are
recorded in the feature vector, but do not substitute for a required feature.

### Explicit lineage

An explicit provider-error pointer can suppress a mirrored claim only when the
economic tuple also agrees. Explicit pending lineage may bridge changed IDs when
account, signed amount, and status transition agree. Correction and reversal
pointers create relationships; they do not erase either event. Missing pointers
or heuristic pending matches remain unresolved.

## Source precedence

Precedence selects representative fields after identity has been established; it
never supplies identity evidence:

1. human-reviewed facts
2. receipt-bound evidence
3. OFX
4. QFX
5. Monarch
6. SimpleFIN
7. other mapped extracts
8. prior canonical publications
9. Wealthfolio projections
10. unknown sources

The longest normalized description is retained, with source precedence as its
tie-breaker. Status prefers trusted posted evidence. Categories remain
enrichment and use their own precedence: human-reviewed facts, receipt-bound
evidence, Monarch, SimpleFIN, prior canonical publications, OFX/QFX type labels,
other extracts, Wealthfolio, then unknown sources.

## Source admission decisions

Coverage authority decides which of two *admitted* observations wins. A separate
policy — `finance_store/source_admission.py`, version `source-admission-v2` —
decides whether a source is admitted at all. Both follow the same rule: a
blocker is cleared only when a durable decision file states a complete decision
whose declared evidence reconciles against what the loader actually observed.
Nothing is guessed and nothing is silently dropped.

### Monarch balance-only entities

The canonical builder imports Monarch *transactions*. A profile carrying only
balances cannot be admitted by a transaction mapping, yet it is still real
evidence about a real asset or liability, so it must be decided rather than
ignored.

`normalized/monarch-account-map.json` accepts two entry shapes. A bare string
stays a valid import mapping and behaves exactly as before, including its
hard `monarch-account-unresolved` failure when it names an unknown fact. An
object entry additionally names an action:

| action | canonicalizes transactions | needs a target fact | balance artifacts |
| --- | --- | --- | --- |
| `import` | yes | yes | preserved |
| `observe` | no | yes | preserved |
| `alternative-entity` | no | yes | preserved, under the alternative fact |
| `exclude` | no | no | preserved |

A target is any canonical entity the normalized builder materializes, not only an
account fact: an account fact keeps its own id, while loan, property, and vehicle
facts are targeted under the namespaced id the fact parser assigns
(`loan:<name>`, `property:<name>`, `vehicle:<name>`). That is what lets a
balance-only mortgage profile be redirected onto the loan fact that already
carries it. Two compatibility rules follow. `import` canonicalizes transactions,
so its target must be transaction-capable — an account or a loan, never a
valuation-only property or vehicle
(`monarch-entity-target-not-transaction-capable`). `alternative-entity` names
what the observed entity actually is, so its declared `entityKind` must match the
target's kind (`monarch-entity-kind-target-mismatch`). SimpleFIN account
validation is unchanged and remains account-fact-only.

An object entry must carry a rationale (`decision`), a `decidedAt` date, an
`observedCounts` block declaring transaction and balance counts, and exactly one
of `trustCutoffDay` or `trustCutoffUnknown`. A balance-only action must also name
an `entityKind`, and a profile flagged `needs_review` must carry
`reviewAcknowledged`. Declared counts and the declared trust cutoff must match
what the loader observed; a mismatch is a blocker, never a coercion. Balance
artifacts always survive, whatever the verdict, and decided ones bind
`admissionAction`, `admissionDecisionId`, and `canonicalized`.

#### Binding the evidence, not only its shape

Counts and a trust cutoff describe the *shape* of what was observed, not its
content: 1,400 balance values could all be restated while the count and the
cutoff held constant, and a decision that only reconciled those would keep
applying to evidence it never saw. So every object entry must additionally bind
`observedEvidenceHash`, the SHA-256 of the canonical JSON of the observed
entity: policy version, source-account hash, account type, transaction count,
balance count, the sorted `(date, value)` balance multiset, the trust cutoff and
review state, and the sorted SHA-256 of every Monarch file the values came from.
Any drift in any of those refuses the decision
(`monarch-entity-observed-evidence-unreconciled`) rather than inheriting it. A
missing hash is itself a blocker
(`monarch-entity-observed-evidence-hash-missing`); legacy string entries are
exempt and keep working unchanged.

A targeted decision may also bind `targetEvidenceHash`, the hash of the fact
behind its canonical id, so editing that fact invalidates the decision made
against it. `alternative-entity` **must** bind it — redirecting an entity onto
another fact is a claim about that fact. If nothing can resolve the target's
hash the decision is refused (`monarch-entity-target-evidence-unavailable`)
rather than admitted unverified.

`exclude` must declare an explicit, provable balance invariant:
`"balanceInvariant": {"allBalancesZero": true}` is checked against the observed
values, in both directions — a `true` claim over a non-zero series and a `false`
claim over an all-zero series are equally refused
(`monarch-entity-balance-invariant-unproven`). Omitting the invariant is
`monarch-entity-balance-invariant-missing`.

The values themselves stay private. `finance_store.sources` exposes
`monarch_observed_entities(root)` and `canonical_entity_evidence(root)` so a
durable decision can be written against the hashes it must bind, while public
reports carry only hashes, counts, and verdicts.

Blocker codes: `monarch-entity-decision-missing` (rolled up as
`monarch-account-unmapped-count:N`), `monarch-entity-action-invalid`,
`monarch-entity-target-missing`, `monarch-entity-target-unresolved`,
`monarch-entity-rationale-missing`, `monarch-entity-decided-at-missing`,
`monarch-entity-declared-counts-missing`,
`monarch-entity-transaction-count-unreconciled`,
`monarch-entity-balance-count-unreconciled`,
`monarch-entity-kind-missing`, `monarch-entity-trust-cutoff-unstated`,
`monarch-entity-kind-target-mismatch`,
`monarch-entity-target-not-transaction-capable`,
`monarch-entity-trust-cutoff-unreconciled`,
`monarch-entity-review-unacknowledged`,
`monarch-entity-observed-evidence-hash-missing`,
`monarch-entity-observed-evidence-unreconciled`,
`monarch-entity-target-evidence-hash-missing`,
`monarch-entity-target-evidence-unavailable`,
`monarch-entity-target-evidence-unreconciled`,
`monarch-entity-balance-invariant-missing`,
`monarch-entity-balance-invariant-unproven`,
`monarch-balance-only-action-on-transaction-entity`.

### SimpleFIN connection freshness

Snapshots are immutable, so an institution error recorded in an older snapshot
is permanent history for that snapshot. Freshness is therefore judged per
connection scope: an explicit `connectionId` in the sibling `request-*.json`
wins, otherwise the scope is derived per account (see below), and anything left
over belongs to the global `default` scope. Accounts are never blended across
scopes: mixed evidence raises `simplefin-connection-scope-mixed`.

Within one scope:

- The newest snapshot is admitted whenever it is clean. Historical errors are
  superseded, not erased — they stay in their own snapshots, in the admission
  proof, and in the `simplefin-historical-connection-error-superseded` gap.
- When the newest snapshot is itself erroring, the last verified clean snapshot
  may be admitted only under a `connections` entry in
  `simplefin/account-map.json` with `action: fallback`, a rationale, a
  `decidedAt`, the current `currentErrorHash`, the prior
  `fallbackSnapshotSha256`, the prior `fallbackRequestedStart` /
  `fallbackRequestedEnd` window, and an explicit `maxStalenessDays`. Every one
  of those must bind to observed evidence.
- An admitted fallback is marked stale, never fresh, and reports
  `simplefin-connection-fallback-admitted-stale` plus
  `simplefin-connection-staleness-days:N`.
- With no safe decision or no verified prior evidence the blocker stands, and
  `simplefin-institution-error-count:N` is still reported for the current errors.

Blocker codes: `simplefin-connection-error-undecided`,
`simplefin-connection-action-invalid`,
`simplefin-connection-rationale-missing`,
`simplefin-connection-decided-at-missing`,
`simplefin-connection-fallback-evidence-missing`,
`simplefin-connection-current-error-unbound`,
`simplefin-connection-fallback-snapshot-unbound`,
`simplefin-connection-fallback-window-unbound`,
`simplefin-connection-staleness-tolerance-missing`,
`simplefin-connection-fallback-too-stale`.

The normalized builder applies the same policy when choosing which snapshot to
read, so the store loader and the canonical builder cannot disagree about which
evidence is current.

### Institution scope in the v1 envelope

SimpleFIN v1 has no connection object at all. One response carries every
institution, accounts have no `conn_id`, and the request sidecar names no
connection. A scope that could only be a file path or the `default` bucket
would therefore be forced to choose whole snapshots globally: one institution
needing re-authentication would either freeze every other institution at an old
snapshot or drop that institution's accounts. Neither is acceptable, so scope is
derived from the account's own organization.

`organization_scope` is the single basis, shared with
`scoped_account_identity` so an account and its scope can never disagree:

- v2 uses the account's `conn_id`.
- v1 uses `org.domain`, else `org.sfin-url|org.name`, else `org.url`.

`organization_scope_id` hashes that basis to `org:<16 hex>`. The scope is a
hash, never a name, so no institution name reaches a public report. Accounts
with no organization at all fall into the global `default` scope, which is
always present.

Selection and merge then happen **per organization**, not per file: each
institution admits its own newest clean snapshot, or its own explicitly approved
prior snapshot, and only that institution's account subset is read from it.
A healthy institution advances while a failing sibling falls back, and because
the subsets are disjoint no account is ever loaded twice.

#### Attributing an error to an institution

A provider error is scoped only when the attribution is proven:

- A structured error carrying its own `org` (or `conn_id`) scopes directly.
- Otherwise the error text and each institution's `org.name` / `org.domain` are
  normalized identically — casefolded, with every run of non-alphanumeric
  characters collapsed to a single space — and the name must appear in the text
  delimited by token boundaries. This is exact containment, not similarity:
  there is no prefix rule, no token-subset rule, and no edit distance. A partial
  name (`Synthetic Institution` against `Synthetic Institution 02`) does not
  match, and a name embedded in a longer token (`Bravo` inside `Bravocorp`) does
  not match.
- Exactly one matching institution scopes the error. Zero matches, two or more
  matches, or a name shorter than two characters leave it **unscopable**.

Unscopable errors are attached to the global `default` scope, where they block
on their own until later clean evidence supersedes them. An ambiguous error is
never guessed onto an institution, and it is never silently dropped.

### Provider advisories are not institution errors

A provider may return a message that warns about the *request* while still
answering for every institution — for example `Requested date range exceeds
recommended range of 45 days. In the future, this may be capped.` when a
controlled current-source pull deliberately requests a wider window than the
provider recommends. Nothing is unavailable and nothing needs a human, so this
is an **advisory**, not an institution error.

`classify_connection_error` decides the class, and
`CONNECTION_ADVISORY_PATTERNS` lists the recognised advisory wordings. The list
is deliberately narrow and matched case-insensitively against the whole message:
anything unrecognised stays **actionable**, so an unfamiliar provider error can
never be downgraded by resemblance. The same predicate governs the store loader,
the connection admission policy and the normalized builder, and a regression
asserts it agrees message-for-message with the SimpleFIN plan builder's existing
`institution-error` blocker.

An advisory:

- is preserved verbatim in the snapshot's `errors` and hashed into `error_hash`,
  so a durable `currentErrorHash` binding still covers it and no history is lost;
- leaves the snapshot **clean**, so an advisory-only pull is admitted, is `fresh`,
  has `stalenessDays` 0, and needs no fallback decision;
- never counts toward `simplefin-institution-error-count:N`;
- is reported as the gap `simplefin-connection-advisory-count:N` and as
  `advisoryCount` / `advisoryHash` / `actionableErrorCount` on the evidence
  document, plus `latestAdvisoryCount`, `latestAdvisoryHash` and
  `latestActionableErrorCount` in the admission proof. Those keys are emitted
  only when an advisory exists, so evidence observed before this policy revision
  keeps its exact document hash;
- is never itself a *superseded* error: only actionable errors appear in
  `superseded_error_snapshots`.

An advisory alongside a genuine error changes nothing about the error: the
snapshot is not clean, the blocker stands, `current_errors` names only the
actionable messages, and the builder's failure message quotes only those.

### Supersession and the admitted coverage window

Supersession only ever runs forward in time within one connection scope. A
historical actionable error — say an auth-required failure on 2026-09-01 — is
superseded only by *later* clean evidence for the same scope; an earlier clean
snapshot never excuses a later error.

`ConnectionAdmission` reports the request window of the snapshot it actually
admitted as `admitted_requested_start` / `admitted_requested_end`, surfaced by
the loader as `admittedRequestedStart` / `admittedRequestedEnd`. For a fresh
pull that is the latest sealed `request-*.json` sidecar, so a controlled 90-day
window is accepted as proven coverage even though it exceeds the provider's
recommended range. For an admitted fallback it is the *prior* snapshot's window,
never the erroring latest one, so a stale admission can never claim coverage it
did not observe.

## Replay and policy migration

`generationHash = SHA-256(policyHash, inputHash, active overrides)`. Running the
same policy over the same observations and overrides produces byte-identical
decisions and canonical state. A policy change creates a new append-only
generation. Existing decisions, events, claims, relationships, and overrides
remain queryable.

Every automatic decision stores:

- policy version and policy hash;
- exact feature vector and confidence basis points;
- competing-candidate proof;
- source observation hashes;
- resulting canonical event IDs;
- a fixed rationale code;
- its residual classification; and
- a decision hash over the policy-bound body.

A `source-suppressed` decision additionally stores the authority policy hash it
was decided under.

Migration `0011_canonical_identity_engine.sql` enforces these structures and
publishes `finance_read.identity_decision_audit` and
`finance_read.identity_generation_summary`. Migration
`0012_source_coverage_authority.sql` layers coverage authority on top without
rewriting `0011`: it extends the competing-candidate proof function, widens the
relation-kind, outcome, and confidence-tier constraints, adds
`residual_classification` and `source_authority_policy_hash` to automatic
decisions, and adds the append-only tables
`canonical_identity_source_authority_policies`,
`canonical_identity_authority_intervals`, and
`canonical_identity_source_suppressions` plus the
`finance_read.identity_source_authority_summary` view.

## Private shadow operation

After the verified forensic publication and the schema-v5 canonical publication
are both current:

```powershell
python -m finance_store.identity_shadow --data-dir <external-data-root> run
python -m finance_store.identity_shadow --data-dir <external-data-root> verify
```

The content-addressed publication is written under
`postgres-shadow/reports/canonical-identity/`. Console output contains only
counts and hashes. The private report contains hashed decision evidence, never
amounts, descriptions, account identifiers, provider identifiers, or source
paths. This command does not connect to or mutate Wealthfolio.

### Which evidence the shadow may read

The shadow has two possible inputs, and only one of them can answer the question
it is asked.

The canonical publication carries `identityScope`: the exact rows its resolver
saw, each with the source family, connection scope, and provider identity that
observed it. The forensic publication carries Wealthfolio projection rows —
what the application kept *after* import. Those rows have no source provenance
at all. A statement row and an aggregator posting of the same purchase arrive
indistinguishable, so no coverage interval can be reconciled against them and no
authority decision can be justified from them. On real household evidence the
difference is not marginal: the projection input reconciled a small minority of
the declared intervals and reported unresolved duplicate groups that canonical
had already resolved, because it was answering a weaker question.

So `build_report_document` replays the published scope through
`finance_store.canonical_identity.verified_resolution` and emits that exact
generation — same policy hash, same generation hash, same canonical state hash,
same counts — whenever all of the following hold:

- a canonical publication exists with an `identityScope` that verifies;
- current forensic evidence verifies fully, with no baseline-refresh blocker;
- the publication's `forensicPublicationId` is the currently verified forensic
  publication;
- the publication's `baselinePublicationId` is the baseline that forensic
  publication is itself bound to.

The report then records `identitySource: canonical-publication-scope` and the
bindings it proved, and carries no blockers.

Otherwise the runner falls back to the forensic projection rows as an explicit
**pre-publication diagnostic**. That mode always reports the
`canonical-publication-scope-required` blocker, together with the specific
reason the publication could not be replayed — for example
`canonical-publication-missing`,
`canonical-publication-forensic-binding-stale`,
`canonical-publication-baseline-binding-stale`,
`canonical-publication-evidence-unverified`, or any
`canonical-identity-*` drift code. The blocker is what stops an authority apply;
the diagnostic exists to be read before a publication is available, not to
authorize anything. Nothing in this path invents a source family for an
application row: projection observations keep `unknown`.

The diagnostic mode still prefers a fully current forensic verification. If the
only failure is that the upstream baseline inventory has changed, it may analyze
the last content-addressed forensic publication after independently re-hashing
every file in that publication, and records
`current-evidence-baseline-refresh-required` as an additional blocker.

Because the shadow replays what canonical sealed, build and verify the canonical
publication *before* running the identity shadow. Its authority gate requires
`identitySource == canonical-publication-scope` before a PostgreSQL apply.
Routine scoped updates use the separate
[incremental worker](runbooks/incremental-finance.md), not a full-rebuild cycle.

### One declaration reader, two producers

Two producers derive canonical identity from the same household evidence.
`importers.lineage_review.canonical` reads canonical transaction rows.
`finance_store.identity_shadow` reads sealed forensic activities for the
PostgreSQL shadow. Both must reach the same conclusions about the same events,
because the shadow exists to be compared against canonical.

They therefore share one reader, `finance_store.identity_declarations`, which is
the only code that opens a durable private identity declaration:

| Declaration | Private path |
| --- | --- |
| Coverage-authority intervals | `identity/source-authority.json` |
| Scoped provider-token namespaces | `identity/provider-token-scopes.json` |
| Duplicate-summary account decisions | `simplefin/account-map.json` |

`load_declarations` returns the resolved policy, the duplicate-summary
mappings, and the token scopes as one value. A malformed or unreadable
declaration raises `DeclarationError` carrying a stable code; canonical
re-raises it as `ReviewError` with that exact code, and the shadow as
`IdentityShadowError`. Neither producer may silently fall back to the default
policy when a declaration exists but cannot be read.

Three derivations are shared rather than reimplemented per producer, because a
producer that disagreed about any of them would miss a decision the other
applied:

- `source_account_scope` derives `(source account, connection scope)`, the key a
  durable decision is recorded against. A shadow that keyed a SimpleFIN
  observation on its canonical account would never match a decision recorded
  against the provider account.
- `provider_id_kind` classifies identifier strength. A shadow that called an OFX
  identifier weak would refuse a scoped-token lineage canonical had accepted.
- `apply_declarations` attaches declaration attributes to observations a
  producer built by some other route.

When no declaration is present, both producers keep the conservative default
policy and leave open groups open.

#### What parity does and does not mean

A canonical row and a forensic activity are different bytes, so their
`sourceHash` values — and therefore `generationHash` and `canonicalStateHash` —
are legitimately different. Forcing those to agree would only prove the fixtures
were rigged. That argument applies to the **pre-publication diagnostic** mode
only. Once a canonical publication exists, the shadow replays its scope, so the
generation and canonical state hashes are not merely comparable but identical,
which `tests/test_identity_shadow_canonical_scope.py` asserts directly.

Parity in the diagnostic mode is asserted where it is real, in
`tests/test_identity_shadow_canonical_parity.py`:

- the bound policy: `policyVersion`, `policyHash`, `policyDocument`, and the
  whole `sourceAuthority` document must be identical
- the outcome: the surviving canonical events' economics and the multiset of
  decision rationale codes must be identical
- the residual profile: `residualByClass` and the unresolved, source-suppressed,
  and authority-covered counts must be identical
- the shadow's own replay: the same evidence must publish byte-for-byte

The shadow report's `evidence.declarations` block publishes only the policy
version, the policy and authority hashes, the interval count, and the SHA-256 of
the exact declaration bytes with their entry counts. It never publishes an
account, a description, or a value.

### Persisting the published generation, not another one

The sealed PostgreSQL apply writes the ledger and the canonical identity
generation in one transaction. Persisting *a* resolution rather than *the*
published one would be worse than persisting nothing: the stored evidence would
agree with the publication only by coincidence, and the first query that
compared them would be a silent lie.

A second derivation cannot be trusted because the canonical publication is not
the input the resolver saw. `_automatic_identity_projection` resolves identity
over the pre-suppression rows, then suppresses non-survivors and rewrites the
survivors' description, category, and transfer group. `transactions.csv` is what
is left afterwards. Replaying identity from it reaches a different observation
set, and therefore a different `generationHash`, whenever any automatic decision
applied — which is exactly the interesting case.

The producer therefore publishes the input it actually resolved. The private
`transaction-observations.json` carries an `identityScope` document:

| Field | Meaning |
| --- | --- |
| `kind` | `canonical-identity-scope` |
| `rows` | the exact row dictionaries handed to the resolver |
| `rowCount` | their number |
| `scopeHash` | `content_hash` over those rows |

Both the producer and the apply reach a generation only through
`importers.lineage_review.canonical.resolve_canonical_identity`, so there is one
code path rather than two that must be kept in step.

`finance_store.canonical_identity.verified_resolution` replays that scope with
the shared declarations and refuses unless every one of these matches the
publication's `identityPolicy`:

- `policyVersion`, `policyHash`, `generationHash`, `canonicalStateHash`
- `automaticScopeRows`, `appliedAutomaticDecisions`, `safeAutomaticResolutions`,
  `unresolvedDuplicateGroups`, `sourceSuppressedClaims`,
  `authorityCoveredClaims`, `authorityAmbiguousGroups`
- the whole `sourceAuthority` document and the whole `residualByClass` block

It additionally refuses a generation with any unresolved duplicate group or
authority ambiguity. Durable authority is for settled evidence; an open question
belongs in the review queue, not in the identity tables.

Each failure is a stable code, never a partial comparison:
`canonical-identity-scope-missing`, `canonical-identity-scope-invalid`,
`canonical-identity-generation-drift`, `canonical-identity-count-drift`,
`canonical-identity-authority-drift`, `canonical-identity-residual-drift`,
`canonical-identity-unresolved-residual`.

The plan seals the proof. `create_plan` records the replayed hashes, the
`identityScopeHash`, and the claim, event, decision, and interval counts under
`identity`, and adds the identity counts to the plan's safe counts. Because
`planHash` covers the whole body, the binding is sealed with everything else. A
publication that cannot produce a binding contributes its failure code as a plan
blocker, so the plan is simply not ready — it never becomes an apply that
guesses.

Inside the sealed unit of work, after the state digest is re-checked and before
any control record is written, the apply replays the publication again, compares
the fresh proof to the sealed one, calls
`identity_postgres.persist_identity_resolution` on the *same* connection, and
reads the generation row back. Any mismatch raises `ShadowSafetyError`, which
rolls back the ledger rows too. Replay is idempotent: an already-persisted
generation returns the existing row rather than writing a second one, and the
apply reports `inserted: false`.

The apply result and the `apply-succeeded` event carry the persisted generation
number, hashes, and counts. The sealed plan record keeps the counts it sealed,
unchanged.

### Two shapes of observation identity

Migration 0011 assumed every canonical identity observation is a row in
`finance.transaction_observations`, and made both membership tables carry a
`uuid` foreign key to it. That assumption is wrong, and a live apply proved it:
the resolver runs over the published `identityScope`, whose observation
identifiers are `content_hash` values, and most of those rows come from artifact
observations — statement extracts, aggregator snapshots — that were never
ingested as `transaction_observations`.

There were two tempting repairs and both are unsafe. Deriving a `uuid` from the
hash fabricates a foreign key to a row that does not exist, which is the kind of
reference that reads as evidence and is not. Dropping the constraint would let a
genuinely broken reference through unnoticed. Migration 0017 instead widens the
model to record which identity actually exists:

| Column | Set when |
| --- | --- |
| `transaction_observation_id` | the identifier is a `uuid` **and** that row exists |
| `observation_identity_hash` | otherwise — the exact published identifier |

Exactly one of the two is present on every observation membership and every
`transaction_observation` event member; a row with neither would dangle and a row
with both would let two readers disagree, so a check constraint rejects each.
Source-claim event members keep exactly the 0011 rule and carry neither column.

Existence is not assumed. `persist_identity_resolution` parses the
`uuid`-shaped identifiers, asks the database once — a single batched read on the
caller's connection, inside the caller's transaction — and binds the foreign key
only for identifiers that came back. A `uuid` that is absent is recorded as an
identity hash rather than as a reference that would fail at insert time or, worse,
succeed against an unrelated row later.

Because SQL treats every `NULL` as distinct, 0011's uniqueness over
`(generation, claim, transaction_observation_id)` stops covering hash-identified
members. Partial unique indexes restore the same guarantee on the other branch,
so a generation still cannot record the same observation twice by either route.

`finance_read.identity_observation_binding_summary` reports, per generation,
scope, and role, how many members are ingested rows and how many are published
identity hashes. It is counts and kinds only: no hash, account, amount, or
description leaves the view. An operator can see that a generation resolved
artifact evidence without being able to read any of it.

Nothing in 0011 through 0016 is rewritten and no existing row changes: every row
already written carries a real `uuid` and keeps it, and the append-only triggers
stay in force.
