"""Scoped shared provider-token lineage regressions.

Two writers can emit the *same* provider token for the same economic event: an
OFX extract keeps the institution's FITID and a SimpleFIN snapshot of the same
institution can echo it.  That is only lineage when an operator has proven the
two namespaces are the same scoped account and import provenance.  Every test
here is synthetic, and the suite exists to prove three things:

* token equality is never applied globally across providers,
* a declared scope only settles a pair whose canonical account, economic tuple,
  and one-to-one occurrence mapping all hold,
* anything else stays visible as an ambiguity rather than being merged or
  dropped.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from finance_store.identity import (
    MAX_PROVIDER_TOKEN_DAY_SKEW,
    PROVIDER_TOKEN_ATTRIBUTE,
    PROVIDER_TOKEN_SCOPE_ATTRIBUTE,
    PROVIDER_TOKEN_SCOPE_MAP_ATTRIBUTE,
    PROVIDER_TOKEN_SCOPE_POLICY_VERSION,
    HumanOverride,
    ProviderTokenNamespace,
    ProviderTokenScope,
    observations_from_transaction_rows,
    provider_token_scopes,
    resolve_identity,
)

MAP_HASH = hashlib.sha256(b"synthetic-provider-token-scope-map").hexdigest()
OTHER_MAP_HASH = hashlib.sha256(b"synthetic-other-map").hexdigest()

ACCOUNT = "SYN-CANONICAL-CHECKING"
OTHER_ACCOUNT = "SYN-CANONICAL-SAVINGS"
SIMPLEFIN_ACCOUNT = "SYN-SIMPLEFIN-ACCOUNT"
TOKEN = "SYN-PROVIDER-TOKEN-1"
DECIDED_AT = "2026-02-01"


def ofx_namespace(
    *,
    source_account_id: str = ACCOUNT,
    prefix: str = "extract:stable:",
    provider_id_kind: str = "ofx-fitid",
) -> ProviderTokenNamespace:
    return ProviderTokenNamespace(
        source_family="ofx",
        source_account_id=source_account_id,
        provider_id_kind=provider_id_kind,
        token_prefix=prefix,
    )


def simplefin_namespace(
    *,
    source_account_id: str = SIMPLEFIN_ACCOUNT,
) -> ProviderTokenNamespace:
    return ProviderTokenNamespace(
        source_family="simplefin",
        source_account_id=source_account_id,
        provider_id_kind="simplefin-id",
        token_prefix=f"simplefin:{source_account_id}:",
    )


def scope(
    *,
    canonical_account_id: str = ACCOUNT,
    left: ProviderTokenNamespace | None = None,
    right: ProviderTokenNamespace | None = None,
    max_day_skew: int = 1,
    map_hash: str = MAP_HASH,
) -> ProviderTokenScope:
    return ProviderTokenScope(
        canonical_account_id=canonical_account_id,
        left=left or ofx_namespace(),
        right=right or simplefin_namespace(),
        decision="shared-provider-token-namespace",
        decided_at=DECIDED_AT,
        map_hash=map_hash,
        max_day_skew=max_day_skew,
    )


def ofx_row(
    *,
    token: str = TOKEN,
    account_id: str = ACCOUNT,
    day: str = "2026-01-15",
    amount: str = "-42.50",
    currency: str = "USD",
    description: str = "Synthetic Merchant",
    prefix: str = "extract:stable:",
    source_file: str = "synthetic/extract.ofx",
    **extra: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "date": day,
        "account_id": account_id,
        "amount": amount,
        "currency": currency,
        "description": description,
        "source_id": f"{prefix}{token}",
        "source_file": source_file,
    }
    values.update(extra)
    return values


def simplefin_row(
    *,
    token: str = TOKEN,
    account_id: str = ACCOUNT,
    source_account: str = SIMPLEFIN_ACCOUNT,
    day: str = "2026-01-16",
    amount: str = "-42.50",
    currency: str = "USD",
    description: str = "SYNTHETIC MERCHANT",
    **extra: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "date": day,
        "account_id": account_id,
        "amount": amount,
        "currency": currency,
        "description": description,
        "source_id": f"simplefin:{source_account}:{token}",
        "source_file": "synthetic/simplefin.json",
        "source_connection_id": "SYN-CONNECTION",
    }
    values.update(extra)
    return values


def resolve(rows, *, scopes=(), overrides=()):
    observations = observations_from_transaction_rows(rows, token_scopes=scopes)
    return resolve_identity(observations, token_scopes=scopes, overrides=overrides)


def rationales(result) -> list[str]:
    return sorted(decision.rationale_code for decision in result.decisions)


def unresolved(result) -> list[str]:
    return sorted(
        decision.rationale_code
        for decision in result.decisions
        if decision.outcome.value == "unresolved"
    )


def attributes(observation) -> dict[str, str]:
    return dict(observation.attributes)


# --------------------------------------------------------------------------
# Declaration parsing: nothing is ever inferred.
# --------------------------------------------------------------------------


def test_parses_only_explicit_scope_entries():
    document = {
        "version": 1,
        "providerTokenScopes": [
            {
                "canonicalAccountId": ACCOUNT,
                "decision": "shared-provider-token-namespace",
                "decidedAt": DECIDED_AT,
                "maxDaySkew": 1,
                "namespaces": [
                    {
                        "sourceFamily": "ofx",
                        "sourceAccountId": ACCOUNT,
                        "providerIdKind": "ofx-fitid",
                        "tokenPrefix": "extract:stable:",
                    },
                    {
                        "sourceFamily": "simplefin",
                        "sourceAccountId": SIMPLEFIN_ACCOUNT,
                        "providerIdKind": "simplefin-id",
                        "tokenPrefix": f"simplefin:{SIMPLEFIN_ACCOUNT}:",
                    },
                ],
            },
            {
                # An unrecognised decision is a note, not an authority.
                "canonicalAccountId": OTHER_ACCOUNT,
                "decision": "looks-similar",
                "decidedAt": DECIDED_AT,
                "namespaces": [
                    {
                        "sourceFamily": "ofx",
                        "sourceAccountId": OTHER_ACCOUNT,
                        "providerIdKind": "ofx-fitid",
                        "tokenPrefix": "extract:stable:",
                    },
                    {
                        "sourceFamily": "monarch",
                        "sourceAccountId": "SYN-MONARCH",
                        "providerIdKind": "scoped-provider-id",
                        "tokenPrefix": "monarch:",
                    },
                ],
            },
        ],
    }

    parsed = provider_token_scopes(document, map_hash=MAP_HASH)

    assert len(parsed) == 1
    assert parsed[0].canonical_account_id == ACCOUNT
    assert parsed[0].map_hash == MAP_HASH
    assert parsed[0].left.provider_id_kind in {"ofx-fitid", "simplefin-id"}


def test_parses_nothing_from_an_absent_or_empty_document():
    assert provider_token_scopes({}, map_hash=MAP_HASH) == ()
    assert (
        provider_token_scopes({"providerTokenScopes": []}, map_hash=MAP_HASH) == ()
    )


def test_refuses_a_scope_with_other_than_two_namespaces():
    document = {
        "providerTokenScopes": [
            {
                "canonicalAccountId": ACCOUNT,
                "decision": "shared-provider-token-namespace",
                "decidedAt": DECIDED_AT,
                "namespaces": [
                    {
                        "sourceFamily": "ofx",
                        "sourceAccountId": ACCOUNT,
                        "providerIdKind": "ofx-fitid",
                        "tokenPrefix": "extract:stable:",
                    }
                ],
            }
        ]
    }

    assert provider_token_scopes(document, map_hash=MAP_HASH) == ()


def test_refuses_a_namespace_declared_twice():
    side = {
        "sourceFamily": "ofx",
        "sourceAccountId": ACCOUNT,
        "providerIdKind": "ofx-fitid",
        "tokenPrefix": "extract:stable:",
    }
    other = {
        "sourceFamily": "simplefin",
        "sourceAccountId": SIMPLEFIN_ACCOUNT,
        "providerIdKind": "simplefin-id",
        "tokenPrefix": f"simplefin:{SIMPLEFIN_ACCOUNT}:",
    }
    document = {
        "providerTokenScopes": [
            {
                "canonicalAccountId": ACCOUNT,
                "decision": "shared-provider-token-namespace",
                "decidedAt": DECIDED_AT,
                "namespaces": [side, other],
            },
            {
                "canonicalAccountId": OTHER_ACCOUNT,
                "decision": "shared-provider-token-namespace",
                "decidedAt": DECIDED_AT,
                "namespaces": [side, dict(other, sourceAccountId="SYN-OTHER-SF")],
            },
        ]
    }

    with pytest.raises(ValueError, match="declared twice"):
        provider_token_scopes(document, map_hash=MAP_HASH)


def test_refuses_a_namespace_without_a_stable_provider_id_kind():
    with pytest.raises(ValueError):
        ProviderTokenNamespace(
            source_family="monarch",
            source_account_id="SYN-MONARCH",
            provider_id_kind="synthetic",
            token_prefix="monarch:",
        )
    with pytest.raises(ValueError):
        ProviderTokenNamespace(
            source_family="monarch",
            source_account_id="SYN-MONARCH",
            provider_id_kind="none",
            token_prefix="monarch:",
        )


def test_refuses_a_namespace_without_an_explicit_delimited_prefix():
    with pytest.raises(ValueError):
        ofx_namespace(prefix="")
    with pytest.raises(ValueError):
        ofx_namespace(prefix="extract")


def test_refuses_a_scope_that_pairs_a_namespace_with_itself():
    with pytest.raises(ValueError):
        scope(left=ofx_namespace(), right=ofx_namespace())


def test_refuses_a_scope_with_an_unhashed_map_or_out_of_range_skew():
    with pytest.raises(ValueError):
        scope(map_hash="not-a-hash")
    with pytest.raises(ValueError):
        scope(max_day_skew=-1)
    with pytest.raises(ValueError):
        scope(max_day_skew=MAX_PROVIDER_TOKEN_DAY_SKEW + 1)


def test_refuses_the_same_scope_declared_twice_to_the_resolver():
    observations = observations_from_transaction_rows([ofx_row()])
    with pytest.raises(ValueError, match="declared twice"):
        resolve_identity(observations, token_scopes=(scope(), scope()))


# --------------------------------------------------------------------------
# Attribute plumbing: the token never leaks and never attaches by accident.
# --------------------------------------------------------------------------


def test_attaches_scope_attributes_only_inside_the_declared_namespace():
    rows = [ofx_row(), simplefin_row()]
    scoped = observations_from_transaction_rows(rows, token_scopes=(scope(),))

    for observation in scoped:
        values = attributes(observation)
        assert values[PROVIDER_TOKEN_ATTRIBUTE] == TOKEN
        assert values[PROVIDER_TOKEN_SCOPE_ATTRIBUTE] == scope().scope_hash
        assert values[PROVIDER_TOKEN_SCOPE_MAP_ATTRIBUTE] == MAP_HASH


def test_attaches_nothing_without_a_declared_scope():
    rows = [ofx_row(), simplefin_row()]
    bare = observations_from_transaction_rows(rows)

    for observation in bare:
        assert PROVIDER_TOKEN_ATTRIBUTE not in attributes(observation)


def test_default_call_is_byte_identical_to_the_pre_scope_behaviour():
    rows = [ofx_row(), simplefin_row()]

    without = observations_from_transaction_rows(rows)
    empty = observations_from_transaction_rows(rows, token_scopes=())

    assert [item.observation_id for item in without] == [
        item.observation_id for item in empty
    ]
    assert resolve_identity(without).generation_hash == (
        resolve_identity(empty).generation_hash
    )


def test_attaches_nothing_when_the_row_lands_on_a_different_canonical_account():
    # Same declared source account, but the canonical account the scope was
    # decided for is not the account this row belongs to.
    rows = [ofx_row(account_id=OTHER_ACCOUNT), simplefin_row(account_id=OTHER_ACCOUNT)]
    scoped = observations_from_transaction_rows(rows, token_scopes=(scope(),))

    for observation in scoped:
        assert PROVIDER_TOKEN_ATTRIBUTE not in attributes(observation)


def test_attaches_nothing_when_the_prefix_does_not_match_literally():
    # A synthetic extract shares the family but not the declared prefix.
    rows = [ofx_row(prefix="extract:synthetic:")]
    scoped = observations_from_transaction_rows(rows, token_scopes=(scope(),))

    assert PROVIDER_TOKEN_ATTRIBUTE not in attributes(scoped[0])


def test_a_synthetic_csv_row_is_never_eligible_for_a_scope():
    namespace = ProviderTokenNamespace(
        source_family="ofx",
        source_account_id=ACCOUNT,
        provider_id_kind="ofx-fitid",
        token_prefix="extract:synthetic:",
    )
    rows = [ofx_row(prefix="extract:synthetic:"), simplefin_row()]
    scoped = observations_from_transaction_rows(
        rows, token_scopes=(scope(left=namespace),)
    )

    synthetic = [item for item in scoped if item.provider_id_kind == "synthetic"]
    assert synthetic
    for observation in synthetic:
        assert PROVIDER_TOKEN_ATTRIBUTE not in attributes(observation)


# --------------------------------------------------------------------------
# The headline case, and the safety rail underneath it.
# --------------------------------------------------------------------------


def test_scoped_shared_token_settles_a_one_day_skew_pair():
    result = resolve([ofx_row(), simplefin_row()], scopes=(scope(),))

    assert len(result.canonical_events) == 1
    assert "scoped-shared-provider-token-lineage" in rationales(result)
    assert unresolved(result) == []

    decision = next(
        item
        for item in result.decisions
        if item.rationale_code == "scoped-shared-provider-token-lineage"
    )
    assert decision.confidence_tier.value == "explicit-lineage"
    assert decision.outcome.value == "merge-claims"

    features = dict(decision.feature_vector)
    assert features["scopedSharedProviderToken"] == "true"
    assert features["providerTokenScopePolicyVersion"] == (
        PROVIDER_TOKEN_SCOPE_POLICY_VERSION
    )
    assert features["providerTokenScopeHash"] == scope().scope_hash
    assert features["providerTokenScopeMapHash"] == MAP_HASH
    assert features["sameCanonicalAccount"] == "true"
    assert features["sameSignedAmountAndCurrency"] == "true"
    assert features["dateDistanceDays"] == "1"
    assert features["maxDaySkewDays"] == "1"
    assert features["categoryParticipates"] == "false"
    assert features["writerTimestampParticipates"] == "false"

    # The raw token is proof material, never a stored value.
    assert TOKEN not in str(features)
    assert features["providerTokenHash"] != TOKEN

    proof = dict(decision.competing_candidate_proof)
    assert proof == {
        "leftNamespaceCount": 1,
        "rightNamespaceCount": 1,
        "sharedTokenBucketCount": 2,
        "dateDistanceDays": 1,
        "maxDaySkewDays": 1,
    }


def test_the_same_token_without_a_declared_scope_never_merges():
    result = resolve([ofx_row(), simplefin_row()])

    assert len(result.canonical_events) == 2
    assert "scoped-shared-provider-token-lineage" not in rationales(result)


def test_the_same_token_under_a_different_scope_pair_never_merges():
    # A scope declared for a different canonical account must not reach in.
    other = scope(
        canonical_account_id=OTHER_ACCOUNT,
        left=ofx_namespace(source_account_id=OTHER_ACCOUNT),
        right=simplefin_namespace(source_account_id="SYN-OTHER-SF"),
    )
    result = resolve([ofx_row(), simplefin_row()], scopes=(other,))

    assert len(result.canonical_events) == 2
    assert "scoped-shared-provider-token-lineage" not in rationales(result)


def test_scoped_lineage_survives_a_competing_duplicate_candidate():
    # A third, genuinely ambiguous row must not downgrade the proven link.
    rows = [
        ofx_row(),
        simplefin_row(),
        {
            "date": "2026-01-16",
            "account_id": ACCOUNT,
            "amount": "-42.50",
            "currency": "USD",
            "description": "Synthetic Merchant",
            "source_id": "monarch:SYN-MONARCH-1",
            "source_file": "synthetic/monarch.csv",
        },
    ]
    result = resolve(rows, scopes=(scope(),))

    assert "scoped-shared-provider-token-lineage" in rationales(result)
    linked = next(
        item
        for item in result.decisions
        if item.rationale_code == "scoped-shared-provider-token-lineage"
    )
    assert linked.outcome.value == "merge-claims"
    assert linked.confidence_tier.value == "explicit-lineage"


# --------------------------------------------------------------------------
# Ambiguities: visible, never merged, never dropped.
# --------------------------------------------------------------------------


def test_a_token_repeated_inside_one_namespace_is_ambiguous_multiplicity():
    # Two OFX imports of the same token under different connection scopes stay
    # distinct claims, so the token bucket carries two left-side members and the
    # occurrence mapping is no longer one-to-one.
    rows = [
        ofx_row(source_connection_id="SYN-OFX-A"),
        ofx_row(source_connection_id="SYN-OFX-B", day="2026-01-16"),
        simplefin_row(),
    ]
    result = resolve(rows, scopes=(scope(),))

    assert "ambiguous-shared-token-multiplicity" in unresolved(result)
    assert "scoped-shared-provider-token-lineage" not in rationales(result)
    assert len(result.canonical_events) == 3


def test_an_amount_conflict_is_ambiguous_rather_than_merged():
    result = resolve(
        [ofx_row(), simplefin_row(amount="-43.50")], scopes=(scope(),)
    )

    assert "ambiguous-shared-token-economic-conflict" in unresolved(result)
    assert len(result.canonical_events) == 2


def test_a_currency_conflict_is_ambiguous_rather_than_merged():
    result = resolve(
        [ofx_row(), simplefin_row(currency="EUR")], scopes=(scope(),)
    )

    assert "ambiguous-shared-token-economic-conflict" in unresolved(result)
    assert len(result.canonical_events) == 2


def test_a_gap_beyond_the_declared_skew_is_ambiguous_rather_than_merged():
    result = resolve(
        [ofx_row(), simplefin_row(day="2026-01-17")], scopes=(scope(max_day_skew=1),)
    )

    assert "ambiguous-shared-token-date-window" in unresolved(result)
    assert len(result.canonical_events) == 2


def test_a_wider_declared_skew_settles_the_same_pair():
    result = resolve(
        [ofx_row(), simplefin_row(day="2026-01-17")], scopes=(scope(max_day_skew=2),)
    )

    assert "scoped-shared-provider-token-lineage" in rationales(result)
    assert len(result.canonical_events) == 1


def test_a_zero_skew_scope_requires_the_same_source_day():
    same_day = resolve(
        [ofx_row(), simplefin_row(day="2026-01-15")], scopes=(scope(max_day_skew=0),)
    )
    assert len(same_day.canonical_events) == 1

    next_day = resolve([ofx_row(), simplefin_row()], scopes=(scope(max_day_skew=0),))
    assert "ambiguous-shared-token-date-window" in unresolved(next_day)
    assert len(next_day.canonical_events) == 2


# --------------------------------------------------------------------------
# Structural blocks: a scope never overrides a stronger safety rule.
# --------------------------------------------------------------------------


def test_a_transfer_group_blocks_scoped_token_settlement():
    rows = [
        ofx_row(transfer_group="SYN-TRANSFER"),
        simplefin_row(transfer_group="SYN-TRANSFER"),
    ]
    result = resolve(rows, scopes=(scope(),))

    assert "scoped-shared-provider-token-lineage" not in rationales(result)


def test_a_pending_observation_blocks_scoped_token_settlement():
    result = resolve(
        [ofx_row(), simplefin_row(status="pending")], scopes=(scope(),)
    )

    assert "scoped-shared-provider-token-lineage" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_an_excluded_account_blocks_scoped_token_settlement():
    result = resolve([ofx_row(), simplefin_row(excluded=True)], scopes=(scope(),))

    assert "scoped-shared-provider-token-lineage" not in rationales(result)


def test_a_zero_amount_never_settles_on_a_shared_token():
    result = resolve(
        [ofx_row(amount="0.00"), simplefin_row(amount="0.00")], scopes=(scope(),)
    )

    assert "scoped-shared-provider-token-lineage" not in rationales(result)


def test_an_explicit_reversal_keeps_its_own_lineage():
    rows = [
        ofx_row(),
        simplefin_row(amount="42.50", reversal_of=f"extract:stable:{TOKEN}"),
    ]
    result = resolve(rows, scopes=(scope(),))

    assert "scoped-shared-provider-token-lineage" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_an_explicit_correction_keeps_its_own_lineage():
    rows = [
        ofx_row(),
        simplefin_row(correction_of=f"extract:stable:{TOKEN}"),
    ]
    result = resolve(rows, scopes=(scope(),))

    assert "scoped-shared-provider-token-lineage" not in rationales(result)


def test_an_opposite_sign_transfer_pair_is_never_settled_by_a_token():
    rows = [ofx_row(), simplefin_row(amount="42.50")]
    result = resolve(rows, scopes=(scope(),))

    assert "scoped-shared-provider-token-lineage" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_a_human_preserve_distinct_override_wins_over_a_declared_scope():
    rows = [ofx_row(), simplefin_row()]
    observations = observations_from_transaction_rows(rows, token_scopes=(scope(),))
    baseline = resolve_identity(observations, token_scopes=(scope(),))
    claim_ids = sorted({claim.claim_id for claim in baseline.claims})
    assert len(claim_ids) == 2
    override = HumanOverride(
        override_id="SYN-OVERRIDE-1",
        version=1,
        action="preserve-distinct",
        claim_ids=tuple(claim_ids),
        rationale_hash=hashlib.sha256(b"kept the two writers apart").hexdigest(),
        decided_at=datetime(2026, 2, 2, tzinfo=timezone.utc),
    )

    result = resolve_identity(
        observations, token_scopes=(scope(),), overrides=(override,)
    )

    assert len(result.canonical_events) == 2
    assert "scoped-shared-provider-token-lineage" not in rationales(result)


# --------------------------------------------------------------------------
# The residual the scope must *not* touch.
# --------------------------------------------------------------------------


def test_extract_monarch_pairs_with_different_tokens_stay_unresolved():
    # The two extract/Monarch residuals: different provider ids, two-day gap.
    # Only proven source-coverage authority may ever settle these, so a token
    # scope must leave them exactly as they were.
    rows = [
        ofx_row(),
        {
            "date": "2026-01-17",
            "account_id": ACCOUNT,
            "amount": "-42.50",
            "currency": "USD",
            "description": "Synthetic Merchant",
            "source_id": "monarch:SYN-MONARCH-DIFFERENT",
            "source_file": "synthetic/monarch.csv",
        },
    ]

    with_scope = resolve(rows, scopes=(scope(),))
    without_scope = resolve(rows)

    assert len(with_scope.canonical_events) == 2
    assert rationales(with_scope) == rationales(without_scope)
    assert "scoped-shared-provider-token-lineage" not in rationales(with_scope)


def test_token_equality_is_never_applied_across_undeclared_providers():
    # Monarch and an OFX extract happen to carry the same trailing token.  With
    # no declaration naming both namespaces, nothing links.
    rows = [
        ofx_row(),
        {
            "date": "2026-01-16",
            "account_id": ACCOUNT,
            "amount": "-42.50",
            "currency": "USD",
            "description": "Synthetic Merchant",
            "source_id": f"monarch:{TOKEN}",
            "source_file": "synthetic/monarch.csv",
        },
    ]
    result = resolve(rows, scopes=(scope(),))

    assert "scoped-shared-provider-token-lineage" not in rationales(result)
    assert len(result.canonical_events) == 2


# --------------------------------------------------------------------------
# Determinism and reporting.
# --------------------------------------------------------------------------


def test_resolution_is_stable_under_row_reordering():
    rows = [ofx_row(), simplefin_row()]
    forward = resolve(rows, scopes=(scope(),))
    backward = resolve(list(reversed(rows)), scopes=(scope(),))

    assert forward.generation_hash == backward.generation_hash
    assert forward.canonical_state_hash == backward.canonical_state_hash
    assert [item.decision_hash for item in forward.decisions] == [
        item.decision_hash for item in backward.decisions
    ]


def test_replaying_the_same_input_reproduces_the_same_decision_ids():
    rows = [ofx_row(), simplefin_row()]
    first = resolve(rows, scopes=(scope(),))
    second = resolve(rows, scopes=(scope(),))

    assert [item.decision_hash for item in first.decisions] == [
        item.decision_hash for item in second.decisions
    ]


def test_a_different_map_hash_changes_the_generation_hash():
    rows = [ofx_row(), simplefin_row()]
    first = resolve(rows, scopes=(scope(),))
    second = resolve(rows, scopes=(scope(map_hash=OTHER_MAP_HASH),))

    assert first.generation_hash != second.generation_hash


def test_report_counts_expose_links_and_ambiguities():
    settled = resolve([ofx_row(), simplefin_row()], scopes=(scope(),))
    counts = settled.report_document()["counts"]
    assert counts["scopedSharedTokenLinks"] == 1
    assert counts["scopedSharedTokenAmbiguousGroups"] == 0

    conflicted = resolve(
        [ofx_row(), simplefin_row(amount="-43.50")], scopes=(scope(),)
    )
    conflicted_counts = conflicted.report_document()["counts"]
    assert conflicted_counts["scopedSharedTokenLinks"] == 0
    assert conflicted_counts["scopedSharedTokenAmbiguousGroups"] == 1


def test_scope_report_is_a_safe_aggregate():
    result = resolve([ofx_row(), simplefin_row()], scopes=(scope(),))
    document = result.provider_token_scope_document()

    assert document["declared"] is True
    assert document["scopeCount"] == 1
    assert document["linkedDecisionCount"] == 1
    assert document["ambiguousGroupCount"] == 0

    serialized = str(document)
    assert TOKEN not in serialized
    assert ACCOUNT not in serialized
    assert SIMPLEFIN_ACCOUNT not in serialized


def test_scope_report_is_empty_when_nothing_is_declared():
    result = resolve([ofx_row(), simplefin_row()])
    document = result.provider_token_scope_document()

    assert document["declared"] is False
    assert document["scopeCount"] == 0
    assert document["scopes"] == []


def test_no_observation_is_ever_dropped_by_a_scope():
    rows = [ofx_row(), simplefin_row()]
    result = resolve(rows, scopes=(scope(),))

    covered = {
        observation_id
        for event in result.canonical_events
        for observation_id in event.member_observation_ids
    }
    assert covered == {item.observation_id for item in result.observations}
    assert len(result.observations) == 2


# --------------------------------------------------------------------------
# Production plumbing: the declaration is read from the private map only.
# --------------------------------------------------------------------------


def _scope_document() -> dict[str, object]:
    return {
        "version": 1,
        "providerTokenScopes": [
            {
                "canonicalAccountId": ACCOUNT,
                "decision": "shared-provider-token-namespace",
                "decidedAt": DECIDED_AT,
                "maxDaySkew": 1,
                "namespaces": [
                    {
                        "sourceFamily": "ofx",
                        "sourceAccountId": ACCOUNT,
                        "providerIdKind": "ofx-fitid",
                        "tokenPrefix": "extract:stable:",
                    },
                    {
                        "sourceFamily": "simplefin",
                        "sourceAccountId": SIMPLEFIN_ACCOUNT,
                        "providerIdKind": "simplefin-id",
                        "tokenPrefix": f"simplefin:{SIMPLEFIN_ACCOUNT}:",
                    },
                ],
            }
        ],
    }


def test_project_reads_token_scopes_from_the_private_map(tmp_path):
    from importers.lineage_review.canonical import declared_token_scopes

    root = tmp_path / "private"
    (root / "identity").mkdir(parents=True)
    raw = json.dumps(_scope_document()).encode("utf-8")
    (root / "identity" / "provider-token-scopes.json").write_bytes(raw)

    parsed = declared_token_scopes(root)

    assert len(parsed) == 1
    assert parsed[0].canonical_account_id == ACCOUNT
    assert parsed[0].map_hash == hashlib.sha256(raw).hexdigest()


def test_missing_private_token_scope_map_yields_no_declarations(tmp_path):
    from importers.lineage_review.canonical import declared_token_scopes

    assert declared_token_scopes(tmp_path) == ()


def test_an_unreadable_token_scope_map_blocks_rather_than_defaulting(tmp_path):
    from importers.lineage_review.canonical import ReviewError, declared_token_scopes

    root = tmp_path / "private"
    (root / "identity").mkdir(parents=True)
    (root / "identity" / "provider-token-scopes.json").write_bytes(b"{not json")

    with pytest.raises(ReviewError, match="provider-token-scope-map-unreadable"):
        declared_token_scopes(root)


def test_an_invalid_token_scope_map_blocks_rather_than_defaulting(tmp_path):
    from importers.lineage_review.canonical import ReviewError, declared_token_scopes

    root = tmp_path / "private"
    (root / "identity").mkdir(parents=True)
    document = _scope_document()
    entries = document["providerTokenScopes"]
    assert isinstance(entries, list)
    entries.append(dict(entries[0]))
    (root / "identity" / "provider-token-scopes.json").write_bytes(
        json.dumps(document).encode("utf-8")
    )

    with pytest.raises(ReviewError, match="provider-token-scope-map-invalid"):
        declared_token_scopes(root)
