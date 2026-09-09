"""Synthetic regressions for the authoritative source-coverage policy.

Every fixture here is invented.  No real institution, account, balance,
merchant, or transaction appears anywhere in this file.
"""

from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from finance_store.domain import content_hash
from finance_store.identity import (
    AUTO_CONFIDENCE_BASIS_POINTS,
    DEFAULT_POLICY,
    DEFAULT_SOURCE_AUTHORITY_POLICY,
    ConfidenceTier,
    DecisionOutcome,
    HumanOverride,
    RelationKind,
    SourceAuthorityPolicy,
    build_source_authority,
    resolve_identity,
)

from tests.test_identity import automatic_merges, observation

ACCOUNT = "SYN-ACCOUNT-CHECKING"
OTHER_ACCOUNT = "SYN-ACCOUNT-SAVINGS"


def evidence(
    *,
    family: str,
    strength: str,
    count: int,
    account: str = ACCOUNT,
    canonical_account: str | None = None,
    connection: str | None = None,
    effective_from: str = "2026-01-01",
    effective_through: str = "2026-01-31",
    requested_from: str = "2026-01-01",
    requested_through: str = "2026-01-31",
    extracted_at: str = "2026-02-10T00:00:00+00:00",
    freshness_as_of: str = "2026-02-10T00:00:00+00:00",
    completeness: str = "complete",
    stable_id_support: bool = True,
    replay_stable_ids: bool = True,
    trust_cutoff: str | None = None,
    source_hashes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "canonical_account_id": canonical_account or account,
        "effective_from": effective_from,
        "effective_through": effective_through,
        "source_family": family,
        "source_connection_id": connection or f"SYN-CONNECTION-{family}",
        "source_account_id": account,
        "format_strength": strength,
        "stable_id_support": stable_id_support,
        "replay_stable_ids": replay_stable_ids,
        "extraction_requested_from": requested_from,
        "extraction_requested_through": requested_through,
        "extracted_at": extracted_at,
        "freshness_as_of": freshness_as_of,
        "completeness": completeness,
        "source_transaction_count": count,
        "source_hashes": source_hashes or [
            content_hash(f"SYN-COVERAGE-{family}-{account}-{count}")
        ],
        "trust_cutoff_day": trust_cutoff,
    }


def policy_with(*records: dict[str, object], **kwargs: object) -> object:
    authority_policy = (
        SourceAuthorityPolicy(**kwargs) if kwargs else DEFAULT_SOURCE_AUTHORITY_POLICY
    )
    return replace(
        DEFAULT_POLICY,
        source_authority=build_source_authority(records, policy=authority_policy),
    )


def suppressions(resolution):
    return [
        decision
        for decision in resolution.decisions
        if decision.outcome is DecisionOutcome.SOURCE_SUPPRESSED
    ]


def unresolved_codes(resolution):
    return sorted(
        decision.rationale_code
        for decision in resolution.decisions
        if decision.outcome is DecisionOutcome.UNRESOLVED
    )


# ---------------------------------------------------------------------------
# Policy and builder surface
# ---------------------------------------------------------------------------


def test_default_policy_publishes_an_explicit_empty_authority_document():
    # v1 omitted the key entirely when nothing was declared, which made
    # "declared no coverage" hash-identical to "published by a writer that
    # could not declare any".  v2 always emits it so the distinction is
    # provable, and an empty authority still hashes stably.
    document = DEFAULT_POLICY.document()
    assert document["version"] == "canonical-identity-v5"
    assert set(document["sourceAuthority"]) == {
        "policy",
        "policyHash",
        "intervals",
    }
    assert document["sourceAuthority"]["intervals"] == []
    assert DEFAULT_POLICY.source_authority.intervals == ()
    empty = replace(DEFAULT_POLICY, source_authority=build_source_authority(()))
    assert empty.policy_hash == DEFAULT_POLICY.policy_hash
    assert empty.document() == document


def test_policy_hash_is_derivable_from_the_published_policy_document():
    # The downstream projector binds to policyHash.  It must be able to
    # re-derive it rather than trust it, or a stale binding is undetectable.
    policy = policy_with(
        evidence(family="qfx", strength="stable-provider-id", count=1)
    )
    document = policy.document()
    assert content_hash(document) == policy.policy_hash
    assert document["version"] == policy.version
    authority = document["sourceAuthority"]
    assert content_hash(authority["policy"]) == authority["policyHash"]
    assert content_hash(authority) == policy.source_authority.authority_hash
    # An interval id is the hash of the interval document that is published,
    # so the safe aggregate report cannot claim an interval the document does
    # not contain.
    assert [content_hash(item) for item in authority["intervals"]] == [
        item.interval_id for item in policy.source_authority.intervals
    ]


def test_authority_policy_document_is_explicit_and_hash_stable():
    document = DEFAULT_SOURCE_AUTHORITY_POLICY.document()
    assert document["version"] == "canonical-source-authority-v3"
    assert document["defaultRank"] == [
        ["ofx", "stable-provider-id"],
        ["qfx", "stable-provider-id"],
        ["simplefin", "posted-observation"],
        ["extract", "synthetic-csv"],
        ["monarch", "legacy-export"],
    ]
    assert document["categoryParticipates"] is False
    assert "opposite-sign-or-cross-account-transfers" in document["neverSuppress"]
    assert (
        DEFAULT_SOURCE_AUTHORITY_POLICY.policy_hash
        == SourceAuthorityPolicy().policy_hash
    )


def test_rank_order_is_ofx_then_simplefin_then_csv_then_monarch():
    ranker = DEFAULT_SOURCE_AUTHORITY_POLICY
    assert ranker.rank("ofx", "stable-provider-id") == ranker.rank(
        "qfx", "stable-provider-id"
    )
    assert ranker.rank("ofx", "stable-provider-id") > ranker.rank(
        "simplefin", "posted-observation"
    )
    assert ranker.rank("simplefin", "posted-observation") > ranker.rank(
        "extract", "synthetic-csv"
    )
    assert ranker.rank("extract", "synthetic-csv") > ranker.rank(
        "monarch", "legacy-export"
    )
    assert ranker.rank("monarch", "stable-provider-id") is None
    assert ranker.rank("unknown", "synthetic-csv") is None


def test_builder_requires_explicit_windows_and_never_infers_them():
    record = evidence(family="ofx", strength="stable-provider-id", count=1)
    del record["effective_through"]
    with pytest.raises(ValueError, match="effective_through"):
        build_source_authority([record])


def test_builder_is_deterministic_and_sorted_by_interval_id():
    records = [
        evidence(family="monarch", strength="legacy-export", count=1),
        evidence(family="ofx", strength="stable-provider-id", count=1),
    ]
    first = build_source_authority(records)
    second = build_source_authority(list(reversed(records)))
    assert first.authority_hash == second.authority_hash
    identifiers = [item.interval_id for item in first.intervals]
    assert identifiers == sorted(identifiers)


def test_interval_outside_extraction_window_is_rejected():
    with pytest.raises(ValueError, match="extraction request window"):
        build_source_authority(
            [
                evidence(
                    family="ofx",
                    strength="stable-provider-id",
                    count=1,
                    effective_through="2026-02-15",
                )
            ]
        )


def test_interval_past_trust_cutoff_is_rejected():
    with pytest.raises(ValueError, match="trust cutoff"):
        build_source_authority(
            [
                evidence(
                    family="ofx",
                    strength="stable-provider-id",
                    count=1,
                    trust_cutoff="2026-01-10",
                )
            ]
        )


def test_one_source_cannot_claim_two_overlapping_intervals():
    with pytest.raises(ValueError, match="overlapping intervals"):
        build_source_authority(
            [
                evidence(family="ofx", strength="stable-provider-id", count=1),
                evidence(
                    family="ofx",
                    strength="stable-provider-id",
                    count=2,
                    effective_from="2026-01-15",
                ),
            ]
        )


def test_allowing_lower_multiplicity_excess_is_refused():
    with pytest.raises(ValueError, match="never permitted"):
        SourceAuthorityPolicy(allow_lower_multiplicity_excess=True)


# ---------------------------------------------------------------------------
# The T00/T12 duplicate pattern
# ---------------------------------------------------------------------------


def t00_t12_observations(**kwargs):
    return [
        observation("obs-ofx", family="ofx", provider_id="FITID-1", hour=0, **kwargs),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            hour=12,
            **kwargs,
        ),
    ]


def test_t00_t12_pattern_is_source_suppressed_under_proven_authority():
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(t00_t12_observations(), policy=policy)

    assert len(resolution.canonical_events) == 1
    decisions = suppressions(resolution)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.confidence_tier is ConfidenceTier.AUTHORITATIVE_SOURCE_COVERAGE
    assert decision.confidence_basis_points == AUTO_CONFIDENCE_BASIS_POINTS
    assert decision.rationale_code == "authoritative-source-coverage-suppression"
    assert decision.residual_classification == "source-suppressed"

    features = dict(decision.feature_vector)
    assert features["authoritativeSourceFamily"] == "ofx"
    assert features["suppressedSourceFamily"] == "monarch"
    assert features["distinctSourceScope"] == "true"
    assert features["intervalsOverlap"] == "true"
    assert features["categoryParticipates"] == "false"
    assert features["writerTimestampParticipates"] == "false"
    assert features["descriptionRelation"] == "exact"
    assert len(features["authoritativeClaimMembershipHash"]) == 64
    assert len(features["suppressedClaimMembershipHash"]) == 64
    assert (
        features["authorityPolicyHash"]
        == DEFAULT_SOURCE_AUTHORITY_POLICY.policy_hash
    )
    assert decision.source_authority_policy_hash == features["authorityPolicyHash"]

    proof = dict(decision.competing_candidate_proof)
    assert proof["authoritativeCount"] == 1
    assert proof["lowerCount"] == 1
    assert proof["occurrenceIndex"] == 0
    assert proof["authoritativeRank"] > proof["suppressedRank"]
    assert decision.source_hashes
    assert all(
        len(dict(item.proof)["observedClaimMembershipHash"]) == 64
        for item in resolution.interval_authorities
    )


def test_conflicting_matchers_cannot_collapse_authoritative_occurrences():
    rows = [
        observation(
            "SYN-AUTHORITY-A1",
            family="ofx",
            provider_id="SYN-AUTHORITY-A1-2",
            description="Synthetic Alpha",
        ),
        observation(
            "SYN-AUTHORITY-A2",
            family="ofx",
            provider_id="SYN-AUTHORITY-A2",
            description="Synthetic Beta",
        ),
        observation(
            "SYN-LOWER-L1",
            family="monarch",
            provider_id="SYN-LOWER-L1",
            description="Synthetic Alpha Extra",
        ),
        observation(
            "SYN-LOWER-L2",
            family="monarch",
            provider_id="SYN-LOWER-L2",
            description="Unrelated Gamma",
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=2),
        evidence(family="monarch", strength="legacy-export", count=2),
    )

    result = resolve_identity(rows, policy=policy)

    assert len(result.canonical_events) == 4
    assert sorted(
        sum(
            (
                list(event.member_observation_ids)
                for event in result.canonical_events
            ),
            [],
        )
    ) == sorted(item.observation_id for item in rows)
    assert all(len(event.member_observation_ids) == 1 for event in result.canonical_events)


def test_t00_t12_selected_observation_is_the_authoritative_one():
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(t00_t12_observations(), policy=policy)
    event = resolution.canonical_events[0]
    assert event.selected_observation_id == "obs-ofx"
    assert set(event.member_observation_ids) == {"obs-ofx", "obs-monarch"}


def test_authority_counts_distinct_source_claims_not_observation_versions():
    original = observation(
        "SYN-OFX-VERSION-ONE",
        family="ofx",
        provider_id="SYN-FITID-VERSIONED",
        description="Synthetic Authority Purchase",
    )
    replay = replace(
        original,
        observation_id="SYN-OFX-VERSION-TWO",
        source_hash=content_hash("SYN-OFX-VERSION-TWO"),
        observed_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
    )
    lower = observation(
        "SYN-MONARCH-VERSION-COPY",
        family="monarch",
        provider_id="SYN-MONARCH-VERSION-COPY",
        description="Synthetic Authority Purchase",
    )
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )

    resolution = resolve_identity((original, replay, lower), policy=policy)

    assert len(resolution.claims) == 2
    assert len(resolution.canonical_events) == 1
    assert len(suppressions(resolution)) == 1
    ofx_interval = next(
        item
        for item in resolution.interval_authorities
        if item.interval.evidence.source_family == "ofx"
    )
    assert ofx_interval.observed_source_transactions == 1
    assert ofx_interval.reconciled is True


def test_each_reporting_source_has_independent_occurrence_capacity():
    rows = [
        observation(
            "SYN-OCCURRENCE-OFX",
            family="ofx",
            provider_id="SYN-OCCURRENCE-OFX",
        ),
        observation(
            "SYN-OCCURRENCE-SIMPLEFIN",
            family="simplefin",
            provider_id="SYN-OCCURRENCE-SIMPLEFIN",
        ),
        observation(
            "SYN-OCCURRENCE-MONARCH",
            family="monarch",
            provider_id="SYN-OCCURRENCE-MONARCH",
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="simplefin",
            strength="posted-observation",
            count=1,
        ),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )

    resolution = resolve_identity(rows, policy=policy)

    assert len(resolution.canonical_events) == 1
    assert len(suppressions(resolution)) == 2
    assert "ambiguous-lower-source-multiplicity" not in unresolved_codes(
        resolution
    )
    assert set(resolution.canonical_events[0].member_observation_ids) == {
        item.observation_id for item in rows
    }


def test_without_authority_the_same_pattern_stays_conservative():
    resolution = resolve_identity(t00_t12_observations(), policy=DEFAULT_POLICY)
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2
    assert resolution.interval_authorities == ()


def test_source_suppressed_edge_is_recorded_with_full_proof():
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(t00_t12_observations(), policy=policy)
    edges = [
        edge
        for edge in resolution.edges
        if edge.kind is RelationKind.SOURCE_SUPPRESSED
    ]
    assert len(edges) == 1
    assert edges[0].automatic is True
    assert edges[0].confidence_basis_points == AUTO_CONFIDENCE_BASIS_POINTS
    assert dict(edges[0].competing_candidate_proof)["authoritativeCount"] == 1
    # A suppression never produces a second event, so it produces no relationship.
    assert all(
        item.kind is not RelationKind.SOURCE_SUPPRESSED
        for item in resolution.relationships
    )


# ---------------------------------------------------------------------------
# Repeated purchases: multiplicity is preserved, never guessed
# ---------------------------------------------------------------------------


def repeated_observations(authoritative: int, lower: int):
    items = []
    for index in range(authoritative):
        items.append(
            observation(
                f"obs-ofx-{index}",
                family="ofx",
                provider_id=f"FITID-R{index}",
                hour=index,
            )
        )
    for index in range(lower):
        items.append(
            observation(
                f"obs-monarch-{index}",
                family="monarch",
                provider_id=f"MONARCH-R{index}",
                provider_kind="synthetic",
                hour=12 + index,
            )
        )
    return items


def test_equal_count_repeated_purchases_map_one_to_one():
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=3),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=3,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(repeated_observations(3, 3), policy=policy)

    assert len(suppressions(resolution)) == 3
    assert len(resolution.canonical_events) == 3
    indexes = sorted(
        dict(decision.competing_candidate_proof)["occurrenceIndex"]
        for decision in suppressions(resolution)
    )
    assert indexes == [0, 1, 2]
    # Multiplicity preserved: three purchases in, three canonical events out.
    assert all(
        len(event.member_observation_ids) == 2 for event in resolution.canonical_events
    )


def test_extra_legitimate_low_source_repeat_is_never_dropped():
    """Only the excess stays open; the covered occurrences still collapse.

    Three legacy rows against two authoritative purchases means the authority
    proves two events happened.  Two legacy rows are explained by them; the
    third is an occurrence the authority never covered, so it survives for
    review instead of being suppressed or silently dropped.
    """

    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=2),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=3,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(repeated_observations(2, 3), policy=policy)

    assert len(suppressions(resolution)) == 2
    assert "ambiguous-lower-source-multiplicity" in unresolved_codes(resolution)
    observed = {
        observation_id
        for event in resolution.canonical_events
        for observation_id in event.member_observation_ids
    }
    assert len(observed) == 5
    assert len(resolution.canonical_events) == 3


def test_the_excess_decision_names_only_the_unexplained_occurrence():
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=2),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=3,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(repeated_observations(2, 3), policy=policy)
    decision = next(
        item
        for item in resolution.decisions
        if item.rationale_code == "ambiguous-lower-source-multiplicity"
    )

    assert len(decision.claim_ids) == 1
    values = dict(decision.competing_candidate_proof)
    assert values["authoritativeCount"] == 2
    assert values["lowerCount"] == 3
    assert values["pairedOccurrenceCount"] == 2
    assert values["excessLowerOccurrenceCount"] == 1


def test_fewer_low_source_repeats_still_preserve_every_authoritative_event():
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=3),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=2,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(repeated_observations(3, 2), policy=policy)
    assert len(suppressions(resolution)) == 2
    assert len(resolution.canonical_events) == 3


def test_differing_descriptions_no_longer_block_a_proven_bucket():
    """Description is tie evidence inside proven coverage, not a gate.

    Two authoritative purchases and one legacy row: the legacy row cannot make
    a third event, so it is paired with the authoritative occurrence whose
    description it matches exactly and suppressed.  Both real events survive.
    """

    items = [
        observation(
            "obs-ofx-a",
            family="ofx",
            provider_id="FITID-A",
            description="Synthetic Merchant",
        ),
        observation(
            "obs-ofx-b",
            family="ofx",
            provider_id="FITID-B",
            description="Synthetic Merchant Store",
            hour=1,
        ),
        observation(
            "obs-monarch-a",
            family="monarch",
            provider_id="MONARCH-A",
            provider_kind="synthetic",
            description="Synthetic Merchant",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=2),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)

    assert len(suppressions(resolution)) == 1
    assert "ambiguous-authority-description-mapping" not in unresolved_codes(resolution)
    assert len(resolution.canonical_events) == 2
    # The exact description claimed its own counterpart rather than the engine
    # pairing on claim order alone.
    assert (
        dict(suppressions(resolution)[0].feature_vector)["descriptionRelation"]
        == "exact"
    )


def test_the_legacy_description_gate_is_still_reachable_for_replay():
    items = [
        observation(
            "obs-ofx-a",
            family="ofx",
            provider_id="FITID-A",
            description="Synthetic Merchant",
        ),
        observation(
            "obs-ofx-b",
            family="ofx",
            provider_id="FITID-B",
            description="Synthetic Merchant Store",
            hour=1,
        ),
        observation(
            "obs-monarch-a",
            family="monarch",
            provider_id="MONARCH-A",
            provider_kind="synthetic",
            description="Synthetic Merchant",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=2),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
        count_based_occurrence_pairing=False,
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []
    assert "ambiguous-authority-description-mapping" in unresolved_codes(resolution)


def test_unrelated_descriptions_inside_proven_coverage_stay_distinct():
    """Coverage counts cannot prove membership between unrelated observations."""

    items = [
        observation("obs-ofx", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            description="Different Synthetic Payee",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2
    assert "ambiguous-lower-source-multiplicity" in unresolved_codes(resolution)


def test_authority_interval_binds_selected_source_artifact_when_available():
    authoritative_hash = content_hash("SYN-AUTHORITY-ARTIFACT")
    stale_hash = content_hash("SYN-STALE-ARTIFACT")
    lower_hash = content_hash("SYN-LOWER-ARTIFACT")
    authoritative = observation(
        "obs-ofx",
        family="ofx",
        provider_id="FITID-1",
        attributes={"sourceArtifactSha256": stale_hash},
    )
    lower = observation(
        "obs-monarch",
        family="monarch",
        provider_id="MONARCH-1",
        provider_kind="synthetic",
        attributes={"sourceArtifactSha256": lower_hash},
    )
    policy = policy_with(
        evidence(
            family="ofx",
            strength="stable-provider-id",
            count=1,
            source_hashes=[authoritative_hash],
        ),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
            source_hashes=[lower_hash],
        ),
    )

    stale = resolve_identity((authoritative, lower), policy=policy)
    current = resolve_identity(
        (
            replace(
                authoritative,
                attributes=(("sourceArtifactSha256", authoritative_hash),),
            ),
            lower,
        ),
        policy=policy,
    )

    assert suppressions(stale) == []
    assert any(
        item.observed_source_transactions == 0
        for item in stale.interval_authorities
        if item.interval.evidence.source_family == "ofx"
    )
    assert len(suppressions(current)) == 1


def test_unrelated_descriptions_still_block_under_the_legacy_gate():
    items = [
        observation("obs-ofx", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            description="Different Synthetic Payee",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
        count_based_occurrence_pairing=False,
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


# ---------------------------------------------------------------------------
# Never-suppress guards
# ---------------------------------------------------------------------------


def test_opposite_sign_transfer_is_never_suppressed():
    items = [
        observation("obs-ofx-out", family="ofx", provider_id="FITID-OUT"),
        observation(
            "obs-monarch-in",
            family="monarch",
            provider_id="MONARCH-IN",
            provider_kind="synthetic",
            account=OTHER_ACCOUNT,
            amount="10.00",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            account=OTHER_ACCOUNT,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2
    assert any(
        item.kind
        in {RelationKind.TRANSFER, RelationKind.TRANSFER_CANDIDATE}
        for item in resolution.relationships
    )


def test_different_canonical_accounts_are_never_suppressed():
    items = [
        observation("obs-ofx", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            account=OTHER_ACCOUNT,
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            account=OTHER_ACCOUNT,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_same_source_family_different_provider_ids_are_never_suppressed():
    items = [
        observation("obs-ofx-a", family="ofx", provider_id="FITID-A"),
        observation("obs-ofx-b", family="ofx", provider_id="FITID-B", hour=12),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=2),
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_non_posted_entries_are_never_suppressed():
    items = [
        observation("obs-ofx", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            status="pending",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []


def test_conflicting_amounts_are_never_suppressed():
    items = [
        observation("obs-ofx", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            amount="-10.01",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_observations_outside_the_proven_interval_are_never_suppressed():
    items = t00_t12_observations(day="2026-02-05")
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=0),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=0,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []


def test_untrusted_observations_past_the_trust_cutoff_are_never_suppressed():
    items = [
        observation("obs-ofx", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            trust_cutoff="2026-01-01",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []


def test_incomplete_coverage_is_not_proven():
    policy = policy_with(
        evidence(
            family="ofx",
            strength="stable-provider-id",
            count=1,
            completeness="partial",
        ),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(t00_t12_observations(), policy=policy)
    assert suppressions(resolution) == []
    proven = {
        item.interval.evidence.source_family: item.proven
        for item in resolution.interval_authorities
    }
    assert proven == {"ofx": False, "monarch": True}


def test_stale_freshness_is_not_proven():
    policy = policy_with(
        evidence(
            family="ofx",
            strength="stable-provider-id",
            count=1,
            freshness_as_of="2026-02-05T00:00:00+00:00",
            extracted_at="2026-02-05T00:00:00+00:00",
        ),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    # Five settled days clears the three-day default settlement requirement.
    resolution = resolve_identity(t00_t12_observations(), policy=policy)
    assert len(suppressions(resolution)) == 1

    tight = policy_with(
        evidence(
            family="ofx",
            strength="stable-provider-id",
            count=1,
            freshness_as_of="2026-02-05T00:00:00+00:00",
            extracted_at="2026-02-05T00:00:00+00:00",
        ),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
        settlement_days=30,
    )
    stale = resolve_identity(t00_t12_observations(), policy=tight)
    assert suppressions(stale) == []
    assert all(not item.proven for item in stale.interval_authorities)


def test_unreconciled_source_counts_block_suppression():
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=9),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(t00_t12_observations(), policy=policy)
    assert suppressions(resolution) == []
    reconciled = {
        item.interval.evidence.source_family: item.reconciled
        for item in resolution.interval_authorities
    }
    assert reconciled == {"ofx": False, "monarch": True}


def test_missing_replay_stable_ids_removes_the_authority_rank():
    policy = policy_with(
        evidence(
            family="ofx",
            strength="stable-provider-id",
            count=1,
            replay_stable_ids=False,
        ),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(t00_t12_observations(), policy=policy)
    assert suppressions(resolution) == []
    ranks = {
        item.interval.evidence.source_family: item.rank
        for item in resolution.interval_authorities
    }
    assert ranks["ofx"] is None


def test_correction_within_one_claim_is_never_source_suppressed():
    items = [
        observation("obs-ofx-a", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-ofx-b",
            family="ofx",
            provider_id="FITID-1",
            amount="-12.00",
            hour=6,
        ),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=2),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    outcomes = {decision.outcome for decision in resolution.decisions}
    assert DecisionOutcome.LINK_CORRECTION in outcomes
    assert suppressions(resolution) == []


def test_reversal_pair_is_never_source_suppressed():
    items = [
        observation("obs-ofx", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-ofx-reversal",
            family="ofx",
            provider_id="FITID-2",
            amount="10.00",
            hour=6,
            attributes={"reversal_of": "obs-ofx"},
        ),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=2),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    assert suppressions(resolution) == []
    assert any(
        item.kind is RelationKind.REVERSAL for item in resolution.relationships
    )


# ---------------------------------------------------------------------------
# Human overrides, determinism, and enrichment
# ---------------------------------------------------------------------------


def test_human_separate_override_beats_proven_authority():
    items = t00_t12_observations()
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    baseline = resolve_identity(items, policy=policy)
    claim_ids = tuple(sorted(claim.claim_id for claim in baseline.claims))
    override = HumanOverride(
        override_id="SYN-OVERRIDE-1",
        version=1,
        action="preserve-distinct",
        claim_ids=claim_ids,
        decided_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        rationale_hash=content_hash("SYN-RATIONALE"),
    )
    resolution = resolve_identity(items, policy=policy, overrides=[override])
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_resolution_is_replay_and_order_stable():
    items = repeated_observations(3, 3)
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=3),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=3,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    forward = resolve_identity(items, policy=policy)
    backward = resolve_identity(list(reversed(items)), policy=policy)
    assert forward.canonical_state_hash == backward.canonical_state_hash
    assert forward.generation_hash == backward.generation_hash
    assert [item.decision_hash for item in forward.decisions] == [
        item.decision_hash for item in backward.decisions
    ]
    assert forward.report_document() == backward.report_document()


def test_monarch_category_survives_as_enrichment_on_the_suppressed_side():
    items = [
        observation("obs-ofx", family="ofx", provider_id="FITID-1"),
        observation(
            "obs-monarch",
            family="monarch",
            provider_id="MONARCH-1",
            provider_kind="synthetic",
            category="Synthetic Category",
            hour=12,
        ),
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    event = resolution.canonical_events[0]
    assert event.selected_observation_id == "obs-ofx"
    assert event.category == "Synthetic Category"
    assert "obs-monarch" in event.member_observation_ids


def test_no_observation_is_ever_dropped_by_suppression():
    items = repeated_observations(3, 3) + [
        observation("obs-other", family="ofx", provider_id="FITID-OTHER", amount="-99.00")
    ]
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=4),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=3,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(items, policy=policy)
    projected = {
        observation_id
        for event in resolution.canonical_events
        for observation_id in event.member_observation_ids
    }
    assert projected == {item.observation_id for item in items}


# ---------------------------------------------------------------------------
# Report surface
# ---------------------------------------------------------------------------


def test_report_exposes_safe_authority_aggregates_only():
    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    document = resolve_identity(t00_t12_observations(), policy=policy).report_document()

    authority = document["sourceAuthority"]
    assert authority["policyVersion"] == "canonical-source-authority-v3"
    assert authority["intervalCount"] == 2
    assert authority["provenIntervalCount"] == 2
    assert authority["authoritativeIntervalCount"] == 2
    assert document["counts"]["sourceSuppressedClaims"] == 1
    assert document["counts"]["authorityAmbiguousGroups"] == 0
    assert document["residualByClass"]["source-suppressed"] == 1

    serialized = str(document)
    assert ACCOUNT not in serialized
    assert "Synthetic Merchant" not in serialized
    assert "FITID-1" not in serialized
    assert "MONARCH-1" not in serialized
    for interval in authority["intervals"]:
        assert interval["canonicalAccountHash"] != ACCOUNT
        assert len(interval["canonicalAccountHash"]) == 64


def test_residual_classification_covers_every_outcome():
    from finance_store.identity import RESIDUAL_CLASSES, RESIDUAL_CLASSIFICATION

    assert set(RESIDUAL_CLASSIFICATION) == set(DecisionOutcome)
    assert set(RESIDUAL_CLASSIFICATION.values()) <= set(RESIDUAL_CLASSES)


def test_conservative_merges_still_reported_when_authority_is_absent():
    resolution = resolve_identity(t00_t12_observations(), policy=DEFAULT_POLICY)
    assert automatic_merges(resolution) == []
    document = resolution.report_document()
    assert document["sourceAuthority"]["intervalCount"] == 0
    assert document["counts"]["sourceSuppressedClaims"] == 0


def test_report_exposes_declared_flag_and_effective_extent():
    empty = resolve_identity(t00_t12_observations(), policy=DEFAULT_POLICY)
    empty_authority = empty.report_document()["sourceAuthority"]
    assert empty_authority["declared"] is False
    assert empty_authority["earliestEffectiveFrom"] is None
    assert empty_authority["latestEffectiveThrough"] is None

    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    declared = resolve_identity(t00_t12_observations(), policy=policy)
    authority = declared.report_document()["sourceAuthority"]
    assert authority["declared"] is True
    assert authority["earliestEffectiveFrom"] == "2026-01-01"
    assert authority["latestEffectiveThrough"] == "2026-01-31"


def test_lineage_review_validator_accepts_a_source_suppressed_projection():
    from importers.lineage_review.canonical import (
        ReviewError,
        _validate_automatic_identity,
    )

    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(
        [
            observation(
                content_hash("obs-ofx"),
                family="ofx",
                provider_id="FITID-1",
                hour=0,
            ),
            observation(
                content_hash("obs-monarch"),
                family="monarch",
                provider_id="MONARCH-1",
                provider_kind="synthetic",
                hour=12,
            ),
        ],
        policy=policy,
    )
    decision = suppressions(resolution)[0]
    event = resolution.canonical_events[0]
    report = resolution.report_document()
    projection = {
        "decisionId": decision.decision_hash,
        "decisionType": "automatic-identity",
        "policyVersion": decision.policy_version,
        "policyHash": decision.policy_hash,
        "generationHash": decision.generation_hash,
        "confidenceTier": decision.confidence_tier.value,
        "confidenceBasisPoints": decision.confidence_basis_points,
        "rationaleCode": decision.rationale_code,
        "outcome": decision.outcome.value,
        "claimIds": list(decision.claim_ids),
        "observationIds": list(decision.observation_ids),
        "canonicalTransactionIds": [event.canonical_event_id],
        "featureVector": dict(decision.feature_vector),
        "competingCandidateProof": dict(decision.competing_candidate_proof),
        "sourceHashes": list(decision.source_hashes),
        "residualClassification": decision.residual_classification,
        "sourceAuthorityPolicyHash": decision.source_authority_policy_hash,
        "decisionHash": decision.decision_hash,
    }
    lineage = {
        "identityPolicy": {
            "policyVersion": report["policyVersion"],
            "policyHash": report["policyHash"],
            "policyDocument": report["policyDocument"],
            "generationHash": report["generationHash"],
            "canonicalStateHash": report["canonicalStateHash"],
            "automaticScopeRows": 2,
            "appliedAutomaticDecisions": 1,
            "safeAutomaticResolutions": report["counts"]["safeAutomaticResolutions"],
            "unresolvedDuplicateGroups": report["counts"]["unresolvedDuplicateGroups"],
            "sourceAuthority": report["sourceAuthority"],
            "residualByClass": report["residualByClass"],
            "sourceSuppressedClaims": report["counts"]["sourceSuppressedClaims"],
            "authorityCoveredClaims": report["counts"]["authorityCoveredClaims"],
            "authorityAmbiguousGroups": report["counts"]["authorityAmbiguousGroups"],
        },
        "decisionProjections": [projection],
    }

    assert projection["confidenceTier"] == "authoritative-source-coverage"
    assert projection["residualClassification"] == "source-suppressed"
    _validate_automatic_identity(lineage)

    stripped = {
        **lineage,
        "decisionProjections": [
            {**projection, "sourceAuthorityPolicyHash": None}
        ],
    }
    with pytest.raises(ReviewError):
        _validate_automatic_identity(stripped)

    mislabelled = {
        **lineage,
        "decisionProjections": [
            {**projection, "residualClassification": "distinct"}
        ],
    }
    with pytest.raises(ReviewError):
        _validate_automatic_identity(mislabelled)

    # The published document must actually produce the published hash, or a
    # downstream projector binding to policyHash is binding to nothing.
    tampered = {
        **lineage,
        "identityPolicy": {
            **lineage["identityPolicy"],
            "policyDocument": {
                **report["policyDocument"],
                "prefixMinimumCharacters": 99,
            },
        },
    }
    with pytest.raises(ReviewError):
        _validate_automatic_identity(tampered)

    missing = {
        **lineage,
        "identityPolicy": {
            key: value
            for key, value in lineage["identityPolicy"].items()
            if key != "policyDocument"
        },
    }
    with pytest.raises(ReviewError):
        _validate_automatic_identity(missing)


def test_lineage_review_validator_requires_a_bound_authority_document():
    from importers.lineage_review.canonical import (
        ReviewError,
        _validate_published_policy_document,
    )

    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1)
    )
    report = resolve_identity([], policy=policy).report_document()
    identity = {
        "policyVersion": report["policyVersion"],
        "policyHash": report["policyHash"],
        "policyDocument": report["policyDocument"],
        "sourceAuthority": report["sourceAuthority"],
    }
    _validate_published_policy_document(identity)

    def rejects(mutate):
        broken = deepcopy(identity)
        mutate(broken)
        with pytest.raises(ReviewError):
            _validate_published_policy_document(broken)

    # Dropping the authority block would make "declared nothing" and "could not
    # declare anything" indistinguishable again.
    rejects(lambda item: item["policyDocument"].pop("sourceAuthority"))
    # A safe aggregate may never claim an interval the document does not carry.
    rejects(lambda item: item["policyDocument"]["sourceAuthority"]["intervals"].clear())
    rejects(
        lambda item: item["sourceAuthority"]["intervals"].__setitem__(
            0, {**item["sourceAuthority"]["intervals"][0], "intervalId": "0" * 64}
        )
    )
    rejects(lambda item: item["sourceAuthority"].__setitem__("intervalCount", 5))
    rejects(lambda item: item["sourceAuthority"].__setitem__("declared", False))
    rejects(
        lambda item: item["sourceAuthority"].__setitem__("authorityHash", "0" * 64)
    )
    rejects(lambda item: item["sourceAuthority"].__setitem__("policyHash", "0" * 64))
    rejects(
        lambda item: item["policyDocument"]["sourceAuthority"]["policy"].__setitem__(
            "requiredCompleteness", "partial"
        )
    )


def test_authority_aggregate_must_hold_together_and_bind_its_document():
    """A block without the document is one the projector will reject."""

    from importers.lineage_review.canonical import (
        ReviewError,
        _validate_published_policy_document,
    )

    policy = policy_with(
        evidence(family="ofx", strength="stable-provider-id", count=1)
    )
    report = resolve_identity([], policy=policy).report_document()
    identity = {
        "policyVersion": report["policyVersion"],
        "policyHash": report["policyHash"],
        "policyDocument": report["policyDocument"],
        "sourceAuthority": report["sourceAuthority"],
    }
    _validate_published_policy_document(identity)

    def rejects(mutate):
        broken = deepcopy(identity)
        mutate(broken)
        with pytest.raises(ReviewError):
            _validate_published_policy_document(broken)

    rejects(lambda item: item.pop("policyDocument"))
    rejects(lambda item: item["sourceAuthority"].__setitem__("intervalCount", 5))
    rejects(lambda item: item["sourceAuthority"].__setitem__("declared", False))
    rejects(lambda item: item["sourceAuthority"].__setitem__("policyHash", "nope"))
    rejects(lambda item: item["sourceAuthority"].__setitem__("authorityHash", "nope"))
    rejects(lambda item: item["sourceAuthority"].__setitem__("policyVersion", ""))
    rejects(
        lambda item: item["sourceAuthority"]["intervals"].__setitem__(
            0, {**item["sourceAuthority"]["intervals"][0], "intervalId": "short"}
        )
    )
    # A duplicated interval id would let one interval be counted twice.
    rejects(
        lambda item: item["sourceAuthority"]["intervals"].extend(
            item["sourceAuthority"]["intervals"]
        )
        or item["sourceAuthority"].__setitem__("intervalCount", 2)
    )


def test_empty_authority_still_publishes_a_provable_document():
    from importers.lineage_review.canonical import _validate_published_policy_document

    # The conservative default path -- no coverage declared anywhere -- must
    # still satisfy the binding contract rather than being a special case.
    report = resolve_identity([]).report_document()
    identity = {
        "policyVersion": report["policyVersion"],
        "policyHash": report["policyHash"],
        "policyDocument": report["policyDocument"],
        "sourceAuthority": report["sourceAuthority"],
    }
    _validate_published_policy_document(identity)
    assert identity["sourceAuthority"]["declared"] is False
    assert identity["policyDocument"]["sourceAuthority"]["intervals"] == []
