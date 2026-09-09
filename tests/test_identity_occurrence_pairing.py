"""Description-bound occurrence pairing inside proven source coverage.

Source coverage and occurrence capacity are prerequisites, not permission to
pair unrelated observations or treat a merchant identifier as transaction identity.
Multiplicity is preserved in both directions: an authoritative
occurrence is claimed at most once, and a lower occurrence the authority never
covered stays open instead of being suppressed or dropped.

Every fixture here is invented.  No real institution, account, balance,
merchant, or transaction appears anywhere in this file.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from finance_store.identity import (
    DEFAULT_SOURCE_AUTHORITY_POLICY,
    DecisionOutcome,
    resolve_identity,
)

from tests.test_identity import observation
from tests.test_identity_source_authority import (
    ACCOUNT,
    OTHER_ACCOUNT,
    evidence,
    policy_with,
    suppressions,
    unresolved_codes,
)

MERCHANTS = ("Synthetic Grocer", "Synthetic Hardware", "Synthetic Cafe")


def authoritative_interval(count: int, **kwargs: object) -> dict[str, object]:
    return evidence(
        family="ofx", strength="stable-provider-id", count=count, **kwargs
    )


def legacy_interval(count: int, **kwargs: object) -> dict[str, object]:
    kwargs.setdefault("stable_id_support", False)
    kwargs.setdefault("replay_stable_ids", False)
    return evidence(family="monarch", strength="legacy-export", count=count, **kwargs)


def mixed(
    authoritative: list[str],
    lower: list[str],
    *,
    account: str = ACCOUNT,
    amount: str = "-42.00",
):
    """Repeats of the same amount on the same day, with chosen descriptions."""

    items = []
    for index, description in enumerate(authoritative):
        items.append(
            observation(
                f"obs-ofx-{index}",
                family="ofx",
                provider_id=f"FITID-{index}",
                account=account,
                amount=amount,
                description=description,
                hour=index,
            )
        )
    for index, description in enumerate(lower):
        items.append(
            observation(
                f"obs-monarch-{index}",
                family="monarch",
                provider_id=f"MONARCH-{index}",
                provider_kind="synthetic",
                account=account,
                amount=amount,
                description=description,
                hour=12 + index,
            )
        )
    return items


def resolve(authoritative: list[str], lower: list[str], **kwargs):
    return resolve_identity(
        mixed(authoritative, lower, **kwargs),
        policy=policy_with(
            authoritative_interval(len(authoritative)),
            legacy_interval(len(lower)),
        ),
    )


def pairs(resolution) -> list[tuple[str, str]]:
    result = []
    for decision in suppressions(resolution):
        vector = dict(decision.feature_vector)
        result.append(
            (vector["suppressedClaimId"], vector["authoritativeClaimId"])
        )
    return sorted(result)


def observed(resolution) -> set[str]:
    return {
        observation_id
        for event in resolution.canonical_events
        for observation_id in event.member_observation_ids
    }


# ---------------------------------------------------------------------------
# The policy surface
# ---------------------------------------------------------------------------


def test_count_based_pairing_is_the_declared_default():
    assert DEFAULT_SOURCE_AUTHORITY_POLICY.count_based_occurrence_pairing is True


def test_the_policy_document_records_how_description_participates():
    document = DEFAULT_SOURCE_AUTHORITY_POLICY.document()
    assert document["countBasedOccurrencePairing"] is True
    assert (
        document["descriptionParticipates"]
        == "exact-only"
    )


def test_disabling_count_based_pairing_changes_the_policy_hash():
    legacy = policy_with(
        authoritative_interval(1),
        legacy_interval(1),
        count_based_occurrence_pairing=False,
    )
    current = policy_with(authoritative_interval(1), legacy_interval(1))
    assert (
        legacy.authority_policy.policy_hash
        != current.authority_policy.policy_hash
    )
    assert (
        legacy.authority_policy.document()["descriptionParticipates"] == "required"
    )


# ---------------------------------------------------------------------------
# Many-to-many repeats with equal counts
# ---------------------------------------------------------------------------


def test_three_repeats_against_three_repeats_collapse_to_three_events():
    resolution = resolve(list(MERCHANTS), list(MERCHANTS))

    assert len(suppressions(resolution)) == 3
    assert len(resolution.canonical_events) == 3
    assert len(observed(resolution)) == 6
    assert all(
        len(event.member_observation_ids) == 2
        for event in resolution.canonical_events
    )


def test_equal_counts_with_wholly_different_descriptions_stay_open():
    """Equal counts do not prove membership between unrelated observations."""

    resolution = resolve(
        list(MERCHANTS), ["Payee Alpha", "Payee Bravo", "Payee Charlie"]
    )

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 6
    assert unresolved_codes(resolution) == [
        "ambiguous-lower-source-multiplicity"
    ]


def test_an_exact_description_claims_its_own_counterpart_first():
    """Tie evidence: a matching payee pairs with its twin, not by claim order."""

    resolution = resolve(
        ["Synthetic Grocer", "Synthetic Hardware"],
        ["Synthetic Hardware", "Payee Alpha"],
    )

    relations = {
        dict(decision.feature_vector)["suppressedClaimId"]: dict(
            decision.feature_vector
        )["descriptionRelation"]
        for decision in suppressions(resolution)
    }
    assert sorted(relations.values()) == ["exact"]
    assert len(resolution.canonical_events) == 3


@pytest.mark.parametrize(
    "token",
    ["8005550100", "800-555-0100", "STORE7A82B", "REF7A82B"],
)
def test_unclassified_shared_token_cannot_bind_a_prefix_pair(token):
    resolution = resolve(
        [f"Synthetic Merchant {token}"],
        [f"Synthetic Merchant {token} Detail"],
    )

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_old_token_rule_requires_an_explicit_historical_policy():
    policy = replace(
        policy_with(
            authoritative_interval(1),
            legacy_interval(1),
            version="canonical-source-authority-v2",
        ),
        version="canonical-identity-v4",
    )
    resolution = resolve_identity(
        mixed(
            ["Synthetic Merchant REF7A82B"],
            ["Synthetic Merchant REF7A82B Detail"],
        ),
        policy=policy,
    )
    assert policy.authority_policy.document()["descriptionParticipates"] == (
        "exact-or-shared-discriminating-token"
    )
    assert len(suppressions(resolution)) == 1
    assert (
        dict(suppressions(resolution)[0].feature_vector)["descriptionRelation"]
        == "token-boundary-prefix"
    )


def test_repeated_identical_descriptions_pair_by_multiset_not_by_set():
    """Two identical legacy rows must consume two authoritative occurrences."""

    resolution = resolve(
        ["Synthetic Grocer", "Synthetic Grocer", "Synthetic Hardware"],
        ["Synthetic Grocer", "Synthetic Grocer"],
    )

    assert len(suppressions(resolution)) == 2
    assert len(resolution.canonical_events) == 3
    assert all(
        dict(decision.feature_vector)["descriptionRelation"] == "exact"
        for decision in suppressions(resolution)
    )


def test_every_authoritative_occurrence_is_claimed_at_most_once():
    resolution = resolve(list(MERCHANTS), list(MERCHANTS))
    authoritative_claims = [item[1] for item in pairs(resolution)]
    assert len(set(authoritative_claims)) == len(authoritative_claims)


def test_the_occurrence_index_covers_the_whole_bucket():
    resolution = resolve(list(MERCHANTS), list(MERCHANTS))
    indexes = sorted(
        dict(decision.feature_vector)["occurrenceIndex"]
        for decision in suppressions(resolution)
    )
    assert indexes == ["0", "1", "2"]


def test_the_suppression_proof_records_both_counts():
    resolution = resolve(list(MERCHANTS), list(MERCHANTS))
    values = dict(suppressions(resolution)[0].competing_candidate_proof)
    assert values["authoritativeCount"] == 3
    assert values["lowerCount"] == 3
    assert values["pairedOccurrenceCount"] == 3
    assert values["excessLowerOccurrenceCount"] == 0
    assert values["bucketParticipantCount"] == 6


# ---------------------------------------------------------------------------
# Excess and shortfall
# ---------------------------------------------------------------------------


def test_an_extra_low_source_repeat_leaves_only_itself_unresolved():
    resolution = resolve(
        ["Synthetic Grocer", "Synthetic Grocer"],
        ["Synthetic Grocer", "Synthetic Grocer", "Synthetic Grocer"],
    )

    assert len(suppressions(resolution)) == 2
    assert "ambiguous-lower-source-multiplicity" in unresolved_codes(resolution)
    decision = next(
        item
        for item in resolution.decisions
        if item.rationale_code == "ambiguous-lower-source-multiplicity"
    )
    assert decision.outcome is DecisionOutcome.UNRESOLVED
    assert len(decision.claim_ids) == 1
    assert len(observed(resolution)) == 5
    assert len(resolution.canonical_events) == 3


def test_the_unexplained_repeat_is_the_last_in_stable_claim_order():
    """Which occurrence stays open must be deterministic, not incidental."""

    first = resolve(
        ["Synthetic Grocer"],
        ["Synthetic Grocer", "Payee Alpha", "Payee Bravo"],
    )
    second = resolve(
        ["Synthetic Grocer"],
        ["Synthetic Grocer", "Payee Alpha", "Payee Bravo"],
    )
    open_claims = [
        item.claim_ids
        for item in first.decisions
        if item.rationale_code == "ambiguous-lower-source-multiplicity"
    ]
    assert len(open_claims) == 1
    assert len(open_claims[0]) == 2
    assert open_claims[0] == [
        item.claim_ids
        for item in second.decisions
        if item.rationale_code == "ambiguous-lower-source-multiplicity"
    ][0]


def test_fewer_low_rows_never_reduce_the_authoritative_multiplicity():
    resolution = resolve(list(MERCHANTS), ["Synthetic Grocer"])

    assert len(suppressions(resolution)) == 1
    assert len(resolution.canonical_events) == 3
    assert unresolved_codes(resolution) == []


# ---------------------------------------------------------------------------
# Competing authoritative sources
# ---------------------------------------------------------------------------


def test_top_sources_that_disagree_on_multiplicity_block_the_bucket():
    """Two equally ranked sources, two different counts: nothing is decided."""

    items = [
        observation(
            "obs-ofx-a-0",
            family="ofx",
            provider_id="FITID-A0",
            account="SYN-SOURCE-A",
            canonical_account=ACCOUNT,
            connection="SYN-CONNECTION-A",
            description="Synthetic Grocer",
            hour=0,
        ),
        observation(
            "obs-ofx-a-1",
            family="ofx",
            provider_id="FITID-A1",
            account="SYN-SOURCE-A",
            canonical_account=ACCOUNT,
            connection="SYN-CONNECTION-A",
            description="Synthetic Grocer",
            hour=1,
        ),
        observation(
            "obs-ofx-b-0",
            family="ofx",
            provider_id="FITID-B0",
            account="SYN-SOURCE-B",
            canonical_account=ACCOUNT,
            connection="SYN-CONNECTION-B",
            description="Synthetic Grocer",
            hour=2,
        ),
        observation(
            "obs-monarch-0",
            family="monarch",
            provider_id="MONARCH-0",
            provider_kind="synthetic",
            description="Synthetic Grocer",
            hour=12,
        ),
    ]
    policy = policy_with(
        authoritative_interval(2, account="SYN-SOURCE-A", canonical_account=ACCOUNT, connection="SYN-CONNECTION-A"),
        authoritative_interval(1, account="SYN-SOURCE-B", canonical_account=ACCOUNT, connection="SYN-CONNECTION-B"),
        legacy_interval(1),
    )
    resolution = resolve_identity(items, policy=policy)

    assert suppressions(resolution) == []
    assert "ambiguous-authoritative-multiplicity" in unresolved_codes(resolution)
    decision = next(
        item
        for item in resolution.decisions
        if item.rationale_code == "ambiguous-authoritative-multiplicity"
    )
    values = dict(decision.competing_candidate_proof)
    assert values["authoritativeSourceScopeCount"] == 2
    assert values["maxAuthoritativeScopeCount"] == 2
    assert values["minAuthoritativeScopeCount"] == 1


def test_top_sources_that_agree_on_multiplicity_still_suppress():
    items = [
        observation(
            "obs-ofx-a-0",
            family="ofx",
            provider_id="FITID-A0",
            account="SYN-SOURCE-A",
            canonical_account=ACCOUNT,
            connection="SYN-CONNECTION-A",
            description="Synthetic Grocer",
            hour=0,
        ),
        observation(
            "obs-ofx-b-0",
            family="ofx",
            provider_id="FITID-B0",
            account="SYN-SOURCE-B",
            canonical_account=ACCOUNT,
            connection="SYN-CONNECTION-B",
            description="Synthetic Grocer",
            hour=2,
        ),
        observation(
            "obs-monarch-0",
            family="monarch",
            provider_id="MONARCH-0",
            provider_kind="synthetic",
            description="Synthetic Grocer",
            hour=12,
        ),
    ]
    policy = policy_with(
        authoritative_interval(1, account="SYN-SOURCE-A", canonical_account=ACCOUNT, connection="SYN-CONNECTION-A"),
        authoritative_interval(1, account="SYN-SOURCE-B", canonical_account=ACCOUNT, connection="SYN-CONNECTION-B"),
        legacy_interval(1),
    )
    resolution = resolve_identity(items, policy=policy)

    assert len(suppressions(resolution)) == 1
    assert "ambiguous-authoritative-multiplicity" not in unresolved_codes(resolution)
    assert len(resolution.canonical_events) == 2


# ---------------------------------------------------------------------------
# The guards count-based pairing must never reach past
# ---------------------------------------------------------------------------


def test_a_different_account_is_never_paired_on_counts():
    items = mixed(["Synthetic Grocer"], []) + mixed(
        [], ["Synthetic Grocer"], account=OTHER_ACCOUNT
    )
    policy = policy_with(
        authoritative_interval(1),
        legacy_interval(1, canonical_account=OTHER_ACCOUNT),
    )
    resolution = resolve_identity(items, policy=policy)

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_an_opposite_sign_row_is_never_paired_on_counts():
    items = mixed(["Synthetic Grocer"], []) + mixed(
        [], ["Synthetic Grocer"], amount="42.00"
    )
    resolution = resolve_identity(
        items, policy=policy_with(authoritative_interval(1), legacy_interval(1))
    )

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_a_same_source_repeat_is_never_paired_on_counts():
    """Two rows from one writer are two purchases, whatever the counts say."""

    resolution = resolve(["Synthetic Grocer", "Synthetic Grocer"], [])

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_a_pending_row_is_never_paired_on_counts():
    items = mixed(["Synthetic Grocer"], [])
    items.append(
        observation(
            "obs-monarch-pending",
            family="monarch",
            provider_id="MONARCH-P",
            provider_kind="synthetic",
            description="Synthetic Grocer",
            status="pending",
            hour=12,
        )
    )
    resolution = resolve_identity(
        items, policy=policy_with(authoritative_interval(1), legacy_interval(1))
    )

    assert suppressions(resolution) == []


def test_a_row_outside_the_interval_is_never_paired_on_counts():
    items = mixed(["Synthetic Grocer"], [])
    items.append(
        observation(
            "obs-monarch-outside",
            family="monarch",
            provider_id="MONARCH-OUT",
            provider_kind="synthetic",
            description="Synthetic Grocer",
            day="2026-03-15",
            hour=12,
        )
    )
    resolution = resolve_identity(
        items, policy=policy_with(authoritative_interval(1), legacy_interval(1))
    )

    assert suppressions(resolution) == []


def test_unreconciled_counts_never_reach_the_pairing_at_all():
    """The bucket is only entered once both intervals reconcile."""

    resolution = resolve_identity(
        mixed(list(MERCHANTS), list(MERCHANTS)),
        policy=policy_with(authoritative_interval(3), legacy_interval(2)),
    )

    assert suppressions(resolution) == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("repeats", [2, 3, 4])
def test_pairing_is_replay_stable_across_input_order(repeats):
    descriptions = [MERCHANTS[index % len(MERCHANTS)] for index in range(repeats)]
    forward = mixed(descriptions, descriptions)
    reverse = list(reversed(forward))
    policy = policy_with(
        authoritative_interval(repeats), legacy_interval(repeats)
    )

    first = resolve_identity(forward, policy=policy)
    second = resolve_identity(reverse, policy=policy)

    assert pairs(first) == pairs(second)
    assert first.generation_hash == second.generation_hash
    assert first.canonical_state_hash == second.canonical_state_hash


def test_replaying_identical_inputs_is_byte_stable():
    first = resolve(list(MERCHANTS), list(MERCHANTS))
    second = resolve(list(MERCHANTS), list(MERCHANTS))

    assert first.generation_hash == second.generation_hash
    assert [item.decision_hash for item in suppressions(first)] == [
        item.decision_hash for item in suppressions(second)
    ]


def test_no_authority_declared_still_changes_nothing():
    resolution = resolve_identity(mixed(list(MERCHANTS), list(MERCHANTS)))

    assert suppressions(resolution) == []
    assert (
        resolution.report_document()["counts"]["sourceSuppressedClaims"] == 0
    )
