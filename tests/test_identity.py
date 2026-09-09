from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from finance_store.domain import content_hash
from finance_store.identity import (
    AUTO_CONFIDENCE_BASIS_POINTS,
    DEFAULT_POLICY,
    ConfidenceTier,
    DecisionOutcome,
    HumanOverride,
    IdentityObservation,
    RelationKind,
    application_projections,
    observations_from_transaction_rows,
    resolve_identity,
)
from importers.lineage_review import canonical


def observation(
    observation_id: str,
    *,
    family: str,
    provider_id: str,
    account: str = "SYN-ACCOUNT-CHECKING",
    canonical_account: str | None = None,
    amount: str = "-10.00",
    day: str = "2026-01-15",
    description: str = "Synthetic Merchant",
    status: str = "posted",
    category: str = "",
    source_group: str = "",
    connection: str | None = None,
    hour: int = 0,
    attributes: dict[str, str] | None = None,
    provider_kind: str | None = None,
    trust_cutoff: str | None = None,
    account_status: str = "active",
) -> IdentityObservation:
    parsed_day = date.fromisoformat(day)
    kind = provider_kind or (
        "simplefin-id"
        if family == "simplefin"
        else "ofx-fitid"
        if family in {"ofx", "qfx"}
        else "scoped-provider-id"
    )
    return IdentityObservation(
        observation_id=observation_id,
        source_family=family,
        source_connection_id=connection or f"SYN-CONNECTION-{family}",
        source_account_id=account,
        canonical_account_id=canonical_account or account,
        provider_transaction_id=provider_id,
        provider_id_kind=kind,
        source_hash=content_hash(
            {
                "observation": observation_id,
                "family": family,
                "provider": provider_id,
            }
        ),
        source_day=parsed_day,
        observed_at=datetime(
            parsed_day.year,
            parsed_day.month,
            parsed_day.day,
            hour,
            tzinfo=timezone.utc,
        ),
        signed_amount=Decimal(amount),
        currency="USD",
        description=description,
        status=status,
        category=category,
        source_group_id=source_group,
        import_lineage_hash=content_hash(f"SYN-LINEAGE-{observation_id}"),
        trust_cutoff_day=(date.fromisoformat(trust_cutoff) if trust_cutoff else None),
        account_status=account_status,
        attributes=tuple(sorted((attributes or {}).items())),
    )


def automatic_merges(resolution):
    return [
        decision
        for decision in resolution.decisions
        if decision.outcome
        in {
            DecisionOutcome.MERGE_CLAIMS,
            DecisionOutcome.MERGE_OBSERVATIONS,
            DecisionOutcome.SUPPRESS_MIRROR,
        }
        and decision.confidence_tier is not ConfidenceTier.REVIEW_REQUIRED
    ]


def test_policy_score_is_explicit_complete_and_hash_stable():
    document = DEFAULT_POLICY.document()

    assert sum(document["scoreBasisPoints"].values()) == 10_000
    assert document["automaticThresholdBasisPoints"] == 10_000
    assert document["rules"]["uniqueCrossSource"] == {
        "candidateDateWindowDays": 3,
        "automaticDateWindowDays": 0,
        "oneToOneDegrees": [1, 1],
        "componentSize": 2,
        "automatic": False,
        "requiresSourceAuthorityOrScopedProviderIdentity": True,
        "categoryParticipates": False,
        "writerTimestampParticipates": False,
    }
    assert document["version"] == "canonical-identity-v5"
    assert document["rules"]["automaticComponents"] == {
        "preserveDistinctIsTransitive": True,
        "oneOccurrencePerSourceFamilyAndCanonicalAccount": True,
        "matcherPrecedence": [
            "explicit-lineage",
            "authoritative-source-coverage",
            "unique-cross-source",
        ],
        "conflictsRequireReview": True,
    }
    assert DEFAULT_POLICY.policy_hash == content_hash(document)


def test_exact_provider_identity_is_scoped_to_connection_and_account():
    first = observation(
        "SYN-OBS-FIRST", family="simplefin", provider_id="SYN-TXN-SHARED"
    )
    replay = replace(
        first,
        observation_id="SYN-OBS-REPLAY",
        source_hash=content_hash("SYN-OBS-REPLAY"),
        observed_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
    )
    other_account = observation(
        "SYN-OBS-OTHER-ACCOUNT",
        family="simplefin",
        provider_id="SYN-TXN-SHARED",
        account="SYN-ACCOUNT-OTHER",
    )

    result = resolve_identity((other_account, replay, first))

    assert len(result.claims) == 2
    assert len(result.canonical_events) == 2
    exact = next(
        item
        for item in result.decisions
        if item.rationale_code == "exact-scoped-source-identity"
    )
    assert exact.confidence_basis_points == AUTO_CONFIDENCE_BASIS_POINTS
    assert exact.observation_ids == ("SYN-OBS-FIRST", "SYN-OBS-REPLAY")


@pytest.mark.parametrize(
    ("description_left", "description_right"),
    [
        ("Synthetic Amazon Marketplace", "Synthetic Amazon Marketplace Order"),
        ("Synthetic Steam", "Synthetic Steam Purchase"),
        ("Synthetic CVS", "Synthetic CVS Pharmacy"),
        ("Synthetic Uber", "Synthetic Uber Trip"),
        ("Synthetic Nintendo", "Synthetic Nintendo Online"),
    ],
)
def test_unique_cross_source_prefix_pairs_require_authority(
    description_left, description_right
):
    monarch = observation(
        "SYN-OBS-MONARCH",
        family="monarch",
        provider_id="SYN-MONARCH-ID",
        description=description_left,
        hour=0,
    )
    simplefin = observation(
        "SYN-OBS-SIMPLEFIN",
        family="simplefin",
        provider_id="SYN-SIMPLEFIN-ID",
        description=description_right,
        hour=12,
    )

    result = resolve_identity((simplefin, monarch))

    assert len(result.canonical_events) == 2
    assert not automatic_merges(result)
    edge = next(
        item
        for item in result.edges
        if item.kind is RelationKind.DUPLICATE_CANDIDATE
    )
    assert edge.automatic is False
    features = dict(edge.feature_vector)
    assert features["writerTimestampParticipates"] == "false"
    assert features["categoryParticipates"] == "false"
    assert features["distinctImportLineage"] == "true"
    assert features["descriptionRelation"] in {
        "exact",
        "token-boundary-prefix",
    }
    assert dict(edge.competing_candidate_proof) == {
        "competingCandidateCount": 0,
        "componentSize": 2,
        "leftDegree": 1,
        "rightDegree": 1,
    }


def test_category_disagreement_is_enrichment_only():
    left = observation(
        "SYN-OBS-CATEGORY-LEFT",
        family="monarch",
        provider_id="SYN-CATEGORY-LEFT",
        category="Synthetic Shopping",
    )
    right = observation(
        "SYN-OBS-CATEGORY-RIGHT",
        family="simplefin",
        provider_id="SYN-CATEGORY-RIGHT",
        category="Synthetic Other",
    )

    initial = resolve_identity((left, right))
    result = resolve_identity(
        (left, right),
        overrides=(
            HumanOverride(
                override_id="SYN-CATEGORY-MERGE",
                version=1,
                action="merge",
                claim_ids=tuple(item.claim_id for item in initial.claims),
                rationale_hash=content_hash("Synthetic reviewed category merge"),
                decided_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
            ),
        ),
    )

    assert len(result.canonical_events) == 1
    assert result.canonical_events[0].category == "Synthetic Shopping"


def test_same_day_same_amount_legitimate_repeats_are_many_to_many():
    values = tuple(
        observation(
            f"SYN-OBS-{family.upper()}-{index}",
            family=family,
            provider_id=f"SYN-ID-{family}-{index}",
        )
        for family in ("monarch", "simplefin")
        for index in range(2)
    )

    result = resolve_identity(values)

    assert len(result.canonical_events) == 4
    assert not automatic_merges(result)
    unresolved = [
        item
        for item in result.decisions
        if item.rationale_code == "ambiguous-cross-source-cardinality"
    ]
    assert len(unresolved) == 1
    assert dict(unresolved[0].feature_vector)["cardinality"] == "many-to-many"


def test_same_source_repeats_and_unknown_families_remain_distinct():
    same_source = (
        observation(
            "SYN-OBS-REPEAT-ONE",
            family="monarch",
            provider_id="SYN-REPEAT-ONE",
        ),
        observation(
            "SYN-OBS-REPEAT-TWO",
            family="monarch",
            provider_id="SYN-REPEAT-TWO",
        ),
    )
    unknown = replace(
        same_source[1],
        observation_id="SYN-OBS-UNKNOWN",
        source_family="unknown",
        provider_transaction_id=None,
        provider_id_kind="none",
        source_hash=content_hash("SYN-OBS-UNKNOWN"),
    )

    same_source_result = resolve_identity(same_source)
    unknown_result = resolve_identity((same_source[0], unknown))

    assert len(same_source_result.canonical_events) == 2
    assert len(unknown_result.canonical_events) == 2
    assert not automatic_merges(same_source_result)
    assert not automatic_merges(unknown_result)


def test_ofx_fitid_is_exact_only_inside_account_scope():
    first = observation(
        "SYN-OBS-OFX-FIRST",
        family="ofx",
        provider_id="SYN-FITID",
    )
    replay = replace(
        first,
        observation_id="SYN-OBS-OFX-REPLAY",
        source_hash=content_hash("SYN-OBS-OFX-REPLAY"),
    )
    other_account = observation(
        "SYN-OBS-OFX-OTHER",
        family="ofx",
        provider_id="SYN-FITID",
        account="SYN-ACCOUNT-OTHER",
    )

    result = resolve_identity((first, replay, other_account))

    assert len(result.claims) == 2
    exact = next(
        item
        for item in result.decisions
        if item.rationale_code == "exact-scoped-source-identity"
    )
    assert dict(exact.feature_vector)["providerIdKind"] == "ofx-fitid"


def test_transfer_pairs_are_linked_and_never_duplicate_merged():
    outgoing = observation(
        "SYN-OBS-TRANSFER-OUT",
        family="monarch",
        provider_id="SYN-TRANSFER-OUT",
        account="SYN-ACCOUNT-CHECKING",
        amount="-25.00",
        description="Synthetic Account Move",
        source_group="SYN-TRANSFER-GROUP",
    )
    incoming = observation(
        "SYN-OBS-TRANSFER-IN",
        family="monarch",
        provider_id="SYN-TRANSFER-IN",
        account="SYN-ACCOUNT-SAVINGS",
        amount="25.00",
        description="Synthetic Account Move",
        source_group="SYN-TRANSFER-GROUP",
    )

    result = resolve_identity((incoming, outgoing))

    assert len(result.canonical_events) == 2
    assert not automatic_merges(result)
    assert [item.kind for item in result.relationships] == [RelationKind.TRANSFER]
    assert any(
        item.outcome is DecisionOutcome.LINK_TRANSFER for item in result.decisions
    )
    assert result.report_document()["counts"]["unresolvedDuplicateGroups"] == 0


def test_unlinked_opposite_signs_are_transfer_candidates_not_duplicates():
    outgoing = observation(
        "SYN-OBS-CANDIDATE-OUT",
        family="monarch",
        provider_id="SYN-CANDIDATE-OUT",
        account="SYN-ACCOUNT-CHECKING",
        amount="-25.00",
    )
    incoming = observation(
        "SYN-OBS-CANDIDATE-IN",
        family="simplefin",
        provider_id="SYN-CANDIDATE-IN",
        account="SYN-ACCOUNT-SAVINGS",
        amount="25.00",
    )

    result = resolve_identity((outgoing, incoming))
    report = result.report_document()

    assert len(result.canonical_events) == 2
    assert report["counts"]["transferCandidates"] == 1
    assert report["counts"]["unresolvedDuplicateGroups"] == 0


def test_zero_value_source_groups_are_not_transfers():
    left = observation(
        "SYN-OBS-ZERO-LEFT",
        family="monarch",
        provider_id="SYN-ZERO-LEFT",
        account="SYN-ACCOUNT-ONE",
        amount="0.00",
        source_group="SYN-ZERO-GROUP",
    )
    right = observation(
        "SYN-OBS-ZERO-RIGHT",
        family="simplefin",
        provider_id="SYN-ZERO-RIGHT",
        account="SYN-ACCOUNT-TWO",
        amount="0.00",
        source_group="SYN-ZERO-GROUP",
    )

    result = resolve_identity((left, right))

    assert len(result.canonical_events) == 2
    assert not any(item.kind is RelationKind.TRANSFER for item in result.edges)
    assert not any(
        item.outcome is DecisionOutcome.LINK_TRANSFER for item in result.decisions
    )
    assert any(
        item.rationale_code == "cross-account-mirror-candidate"
        for item in result.decisions
    )


def test_cross_account_mirror_needs_explicit_provider_error_lineage():
    original = observation(
        "SYN-OBS-MIRROR-ORIGINAL",
        family="simplefin",
        provider_id="SYN-MIRROR-ORIGINAL",
        account="SYN-ACCOUNT-ONE",
        connection="SYN-CONNECTION-SHARED",
    )
    mirror = observation(
        "SYN-OBS-MIRROR-COPY",
        family="simplefin",
        provider_id="SYN-MIRROR-COPY",
        account="SYN-ACCOUNT-TWO",
        connection="SYN-CONNECTION-SHARED",
    )

    unresolved = resolve_identity((original, mirror))
    assert len(unresolved.canonical_events) == 2
    assert any(
        item.rationale_code == "cross-account-mirror-candidate"
        for item in unresolved.decisions
    )

    explicit = replace(
        mirror,
        attributes=(("provider_error_of", original.observation_id),),
    )
    resolved = resolve_identity((original, explicit))
    assert len(resolved.canonical_events) == 1
    assert any(
        item.outcome is DecisionOutcome.SUPPRESS_MIRROR for item in resolved.decisions
    )


def test_pending_correction_and_reversal_lifecycles_remain_auditable():
    pending = observation(
        "SYN-OBS-PENDING",
        family="simplefin",
        provider_id="SYN-LIFECYCLE-ID",
        status="pending",
    )
    posted = replace(
        pending,
        observation_id="SYN-OBS-POSTED",
        source_hash=content_hash("SYN-OBS-POSTED"),
        status="posted",
        observed_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
    )
    corrected = replace(
        posted,
        observation_id="SYN-OBS-CORRECTED",
        source_hash=content_hash("SYN-OBS-CORRECTED"),
        signed_amount=Decimal("-11.00"),
        observed_at=datetime(2026, 1, 17, tzinfo=timezone.utc),
    )
    reversal = observation(
        "SYN-OBS-REVERSAL",
        family="simplefin",
        provider_id="SYN-REVERSAL-ID",
        amount="11.00",
        attributes={"reversal_of": corrected.observation_id},
    )

    result = resolve_identity((reversal, corrected, pending, posted))

    assert len(result.claims) == 2
    assert len(result.canonical_events) == 2
    rationale_codes = {item.rationale_code for item in result.decisions}
    assert "same-id-correction" in rationale_codes
    assert "explicit-reversal-lineage" in rationale_codes
    assert RelationKind.REVERSAL in {item.kind for item in result.relationships}


def test_newer_reversed_version_wins_over_older_posted_version():
    posted = observation(
        "SYN-OBS-VERSION-POSTED",
        family="simplefin",
        provider_id="SYN-VERSION-ID",
        status="posted",
    )
    reversed_version = replace(
        posted,
        observation_id="SYN-OBS-VERSION-REVERSED",
        source_hash=content_hash("SYN-OBS-VERSION-REVERSED"),
        status="reversed",
        observed_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
    )

    result = resolve_identity((posted, reversed_version))

    assert result.claims[0].selected_observation_id == reversed_version.observation_id
    assert result.canonical_events[0].selected_observation_id == (
        reversed_version.observation_id
    )
    assert result.canonical_events[0].status == "reversed"


def test_changed_pending_id_or_date_is_not_merged_without_lineage():
    pending = observation(
        "SYN-OBS-PENDING-CHANGED",
        family="simplefin",
        provider_id="SYN-PENDING-OLD",
        status="pending",
    )
    posted = observation(
        "SYN-OBS-POSTED-CHANGED",
        family="simplefin",
        provider_id="SYN-POSTED-NEW",
        day="2026-01-16",
    )

    result = resolve_identity((pending, posted))

    assert len(result.canonical_events) == 2
    assert not automatic_merges(result)


def test_cross_source_date_window_is_review_only():
    left = observation(
        "SYN-OBS-WINDOW-LEFT",
        family="monarch",
        provider_id="SYN-WINDOW-LEFT",
    )
    right = observation(
        "SYN-OBS-WINDOW-RIGHT",
        family="simplefin",
        provider_id="SYN-WINDOW-RIGHT",
        day="2026-01-16",
    )

    result = resolve_identity((left, right))

    assert len(result.canonical_events) == 2
    unresolved = next(
        item
        for item in result.decisions
        if item.rationale_code == "ambiguous-cross-source-cardinality"
    )
    assert unresolved.confidence_basis_points == 8_500
    assert dict(unresolved.feature_vector)["cardinality"] == "one-to-one"


def test_explicit_pending_lineage_can_bridge_changed_provider_ids():
    pending = observation(
        "SYN-OBS-PENDING-LINKED",
        family="simplefin",
        provider_id="SYN-PENDING-LINKED-ID",
        status="pending",
    )
    posted = observation(
        "SYN-OBS-POSTED-LINKED",
        family="simplefin",
        provider_id="SYN-POSTED-LINKED-ID",
        attributes={"pending_of": pending.observation_id},
    )

    result = resolve_identity((pending, posted))

    assert len(result.canonical_events) == 1
    assert any(
        item.rationale_code == "explicit-pending-lineage" for item in result.decisions
    )


def test_trust_cutoff_preserves_observation_but_excludes_projection():
    stale = observation(
        "SYN-OBS-STALE",
        family="simplefin",
        provider_id="SYN-STALE-ID",
        day="2026-01-16",
        trust_cutoff="2026-01-15",
    )

    result = resolve_identity((stale,))
    projections = application_projections(result)

    assert len(result.observations) == 1
    assert result.canonical_events[0].status == "excluded"
    assert projections[0].active is False
    assert any(
        item.outcome is DecisionOutcome.EXCLUDE_UNTRUSTED for item in result.decisions
    )


def test_human_override_has_precedence_over_automatic_edge():
    left = observation(
        "SYN-OBS-OVERRIDE-LEFT",
        family="monarch",
        provider_id="SYN-OVERRIDE-LEFT",
    )
    right = observation(
        "SYN-OBS-OVERRIDE-RIGHT",
        family="simplefin",
        provider_id="SYN-OVERRIDE-RIGHT",
    )
    initial = resolve_identity((left, right))
    claim_ids = tuple(item.claim_id for item in initial.claims)
    override = HumanOverride(
        override_id="SYN-OVERRIDE-DISTINCT",
        version=1,
        action="preserve-distinct",
        claim_ids=claim_ids,
        rationale_hash=content_hash("SYN reviewed evidence"),
        decided_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
    )

    result = resolve_identity((left, right), overrides=(override,))

    assert len(result.canonical_events) == 2
    assert not any(
        item.outcome is DecisionOutcome.MERGE_CLAIMS and item.human_override_id is None
        for item in result.decisions
    )
    human = next(item for item in result.decisions if item.human_override_id)
    assert human.outcome is DecisionOutcome.PRESERVE_DISTINCT
    assert human.confidence_tier is ConfidenceTier.HUMAN_OVERRIDE


def test_preserve_distinct_constrains_transitive_merged_components():
    rows = (
        observation(
            "SYN-OBS-TRANSITIVE-A",
            family="ofx",
            provider_id="SYN-TRANSITIVE-A",
            description="Synthetic unrelated",
        ),
        observation(
            "SYN-OBS-TRANSITIVE-B",
            family="monarch",
            provider_id="SYN-TRANSITIVE-B",
        ),
        observation(
            "SYN-OBS-TRANSITIVE-C",
            family="simplefin",
            provider_id="SYN-TRANSITIVE-C",
        ),
    )
    initial = resolve_identity(rows)
    claim_by_observation = {
        observation_id: claim.claim_id
        for claim in initial.claims
        for observation_id in claim.observation_ids
    }

    def override(
        override_id: str, action: str, observation_ids: tuple[str, str]
    ) -> HumanOverride:
        return HumanOverride(
            override_id=override_id,
            version=1,
            action=action,
            claim_ids=tuple(
                claim_by_observation[observation_id]
                for observation_id in observation_ids
            ),
            rationale_hash=content_hash(override_id),
            decided_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

    result = resolve_identity(
        rows,
        overrides=(
            override(
                "SYN-OVERRIDE-PRESERVE",
                "preserve-distinct",
                ("SYN-OBS-TRANSITIVE-A", "SYN-OBS-TRANSITIVE-B"),
            ),
            override(
                "SYN-OVERRIDE-MERGE",
                "merge",
                ("SYN-OBS-TRANSITIVE-A", "SYN-OBS-TRANSITIVE-C"),
            ),
        ),
    )

    assert len(result.canonical_events) == 2
    event_by_observation = result.observation_to_canonical
    assert (
        event_by_observation["SYN-OBS-TRANSITIVE-A"]
        == event_by_observation["SYN-OBS-TRANSITIVE-C"]
    )
    assert (
        event_by_observation["SYN-OBS-TRANSITIVE-A"]
        != event_by_observation["SYN-OBS-TRANSITIVE-B"]
    )


def test_policy_upgrade_creates_new_generation_without_changing_stable_event_id():
    values = (
        observation(
            "SYN-OBS-POLICY-LEFT",
            family="monarch",
            provider_id="SYN-POLICY-LEFT",
        ),
        observation(
            "SYN-OBS-POLICY-RIGHT",
            family="simplefin",
            provider_id="SYN-POLICY-RIGHT",
        ),
    )
    first = resolve_identity(values)
    second = resolve_identity(
        values,
        policy=replace(DEFAULT_POLICY, version="synthetic-policy-upgrade"),
    )

    assert first.generation_hash != second.generation_hash
    assert first.canonical_events == second.canonical_events
    assert {item.decision_hash for item in first.decisions} != {
        item.decision_hash for item in second.decisions
    }


def test_replay_and_ingestion_order_are_byte_stable():
    values = (
        observation(
            "SYN-OBS-ORDER-A",
            family="monarch",
            provider_id="SYN-ORDER-A",
        ),
        observation(
            "SYN-OBS-ORDER-B",
            family="simplefin",
            provider_id="SYN-ORDER-B",
        ),
        observation(
            "SYN-OBS-ORDER-C",
            family="ofx",
            provider_id="SYN-ORDER-C",
            account="SYN-ACCOUNT-OTHER",
            description="Synthetic Separate",
        ),
    )

    forward = resolve_identity(values)
    reverse = resolve_identity(reversed(values))

    assert forward == reverse
    assert forward.report_document() == reverse.report_document()


def test_generation_hash_binds_normalized_identity_semantics():
    original = observation(
        "SYN-OBS-SEMANTICS",
        family="monarch",
        provider_id="SYN-SEMANTICS",
    )
    reinterpreted = replace(
        original,
        source_connection_id="SYN-DIFFERENT-CONNECTION",
        provider_id_kind="none",
    )

    first = resolve_identity((original,))
    second = resolve_identity((reinterpreted,))

    assert first.input_hash != second.input_hash
    assert first.generation_hash != second.generation_hash


def test_row_adapter_treats_t00_and_t12_as_provenance_not_transaction_time():
    rows = [
        {
            "date": "2026-01-15T00:00:00Z",
            "account_id": "SYN-ACCOUNT",
            "amount": "-12.34",
            "currency": "USD",
            "description": "Synthetic Provider Merchant",
            "source_id": "monarch:SYN-MONARCH",
            "source_file": "synthetic/monarch.csv",
        },
        {
            "date": "2026-01-15T12:00:00Z",
            "account_id": "SYN-ACCOUNT",
            "amount": "-12.34",
            "currency": "USD",
            "description": "Synthetic Provider Merchant Detail",
            "source_id": "simplefin:SYN-ACCOUNT:SYN-SIMPLEFIN",
            "source_file": "synthetic/simplefin.json",
        },
    ]

    observations = observations_from_transaction_rows(rows)
    result = resolve_identity(observations)

    assert {item.source_day.isoformat() for item in observations} == {"2026-01-15"}
    assert {item.attribute("writer_timestamp_signature") for item in observations} == {
        "T00",
        "T12",
    }
    assert len(result.canonical_events) == 2
    assert not automatic_merges(result)


def test_row_adapter_uses_snapshot_time_for_version_ordering():
    rows = [
        {
            "date": "2026-01-15",
            "account_id": "SYN-ACCOUNT",
            "amount": "-12.34",
            "currency": "USD",
            "description": "Synthetic posted version",
            "source_id": "simplefin:SYN-ACCOUNT:SYN-VERSION",
            "source_file": (
                "raw/simplefin/2026-01-16/simplefin-010203-000001.json"
            ),
            "status": "posted",
        },
        {
            "date": "2026-01-15",
            "account_id": "SYN-ACCOUNT",
            "amount": "-12.34",
            "currency": "USD",
            "description": "Synthetic reversed version",
            "source_id": "simplefin:SYN-ACCOUNT:SYN-VERSION",
            "source_file": (
                "raw/simplefin/2026-01-17/simplefin-040506-000002.json"
            ),
            "status": "reversed",
        },
    ]

    observations = observations_from_transaction_rows(rows)
    result = resolve_identity(observations)

    observed_by_status = {
        item.status: item.observed_at.isoformat() for item in observations
    }
    assert observed_by_status == {
        "posted": "2026-01-16T01:02:03.000001+00:00",
        "reversed": "2026-01-17T04:05:06.000002+00:00",
    }
    reversed_observation = next(
        item for item in observations if item.status == "reversed"
    )
    assert result.canonical_events[0].status == "reversed"
    assert (
        result.canonical_events[0].selected_observation_id
        == reversed_observation.observation_id
    )


def test_row_adapter_binds_dated_source_file_observation_and_content_hash():
    source_file = "extracts/synthetic/Synthetic Activity - 2026-01-17.qfx"
    source_hash = content_hash("SYN-SOURCE-FILE")
    rows = [{
        "date": "2026-01-15",
        "account_id": "SYN-ACCOUNT",
        "amount": "-12.34",
        "currency": "USD",
        "description": "Synthetic Merchant",
        "source_id": "extract:SYN-ACCOUNT:SYN-FITID",
        "source_file": source_file,
    }]

    observations = observations_from_transaction_rows(
        rows,
        source_artifact_hashes={source_file: source_hash},
    )

    assert observations[0].observed_at.isoformat() == (
        "2026-01-17T00:00:00+00:00"
    )
    assert observations[0].attribute("sourceArtifactSha256") == source_hash


def test_canonical_publication_preserves_unproved_cross_source_candidates(tmp_path):
    root = tmp_path / "private"
    repo = tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    rows = [
        {
            "date": "2026-01-15T00:00:00Z",
            "account_id": "SYN-ACCOUNT",
            "amount": "-12.34",
            "currency": "USD",
            "description": "Synthetic Merchant",
            "source_id": "monarch:SYN-MONARCH",
            "source_file": "synthetic/monarch.csv",
        },
        {
            "date": "2026-01-15T12:00:00Z",
            "account_id": "SYN-ACCOUNT",
            "amount": "-12.34",
            "currency": "USD",
            "description": "Synthetic Merchant Detail",
            "source_id": "simplefin:SYN-ACCOUNT:SYN-SIMPLEFIN",
            "source_file": "synthetic/simplefin.json",
        },
    ]

    projected = canonical.project(
        root,
        rows,
        rows,
        (),
        repo_root=repo,
    )

    assert len(projected["rows"]) == 2
    assert projected["observations"]["observationCount"] == 2
    assert projected["lineage"]["counts"]["canonical-transactions"] == 2
    assert projected["lineage"]["identityPolicy"]["safeAutomaticResolutions"] == 0
    assert projected["lineage"]["decisionProjections"] == []
    canonical.validate_documents(
        projected["observations"],
        projected["lineage"],
        projected["rows"],
    )


def test_canonical_projection_suppresses_older_same_id_correction():
    rows = [
        {
            "date": "2026-01-15",
            "account_id": "SYN-ACCOUNT",
            "amount": "-10.00",
            "currency": "USD",
            "description": "Synthetic original",
            "source_id": "simplefin:SYN-ACCOUNT:SYN-CORRECTION",
            "source_file": "synthetic/simplefin-old.json",
        },
        {
            "date": "2026-01-16",
            "account_id": "SYN-ACCOUNT",
            "amount": "-11.00",
            "currency": "USD",
            "description": "Synthetic corrected",
            "source_id": "simplefin:SYN-ACCOUNT:SYN-CORRECTION",
            "source_file": "synthetic/simplefin-new.json",
        },
    ]

    projection = canonical._automatic_identity_projection(rows, set())

    assert len(set(projection["canonicalIds"].values())) == 1
    assert projection["suppressed"] == {0}
    assert rows[1]["source_id"].endswith("SYN-CORRECTION")
    assert rows[1]["amount"] == "-11.00"


def test_canonical_publication_keeps_many_to_many_repeats(tmp_path):
    root = tmp_path / "private"
    repo = tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    rows = [
        {
            "date": "2026-01-15",
            "account_id": "SYN-ACCOUNT",
            "amount": "-9.99",
            "currency": "USD",
            "description": "Synthetic Repeated Purchase",
            "source_id": f"{family}:SYN-{family}-{index}",
            "source_file": f"synthetic/{family}-{index}.json",
        }
        for family in ("monarch", "simplefin")
        for index in range(2)
    ]

    projected = canonical.project(
        root,
        rows,
        rows,
        (),
        repo_root=repo,
    )

    assert len(projected["rows"]) == 4
    assert projected["lineage"]["counts"]["canonical-transactions"] == 4
    assert projected["lineage"]["identityPolicy"]["safeAutomaticResolutions"] == 0
    assert projected["lineage"]["identityPolicy"]["unresolvedDuplicateGroups"] == 1


def test_empty_review_decision_state_retains_identity_policy(tmp_path, monkeypatch):
    root = tmp_path / "private"
    repo = tmp_path / "repo"
    output = root / canonical.OUTPUT_RELATIVE
    output.mkdir(parents=True)
    (output / "current.json").write_text("{}")
    repo.mkdir()
    queue_id = "a" * 64
    monkeypatch.setattr(
        canonical,
        "verified_state",
        lambda *_args, **_kwargs: {
            "queue": {
                "baselinePublicationId": "b" * 64,
                "forensicPublicationId": "c" * 64,
                "candidateGraphHash": "d" * 64,
                "groups": [],
            },
            "queuePublicationId": queue_id,
            "decisions": [],
            "decisionPublicationId": None,
            "activities": {},
            "sourcePaths": (),
        },
    )
    rows = [
        {
            "date": "2026-01-15",
            "account_id": "SYN-ACCOUNT",
            "amount": "-1.00",
            "currency": "USD",
            "description": "Synthetic Merchant",
            "source_id": "simplefin:SYN-ACCOUNT:SYN-ROW",
            "source_file": "synthetic/simplefin.json",
        }
    ]

    projected = canonical.project(root, rows, rows, (), repo_root=repo)

    assert projected["lineage"]["queuePublicationId"] == queue_id
    assert projected["lineage"]["identityPolicy"]["policyHash"]
    canonical.validate_documents(
        projected["observations"],
        projected["lineage"],
        projected["rows"],
    )


def test_unscoped_row_provider_ids_never_form_exact_claims():
    base = {
        "date": "2026-01-15",
        "account_id": "SYN-ACCOUNT",
        "amount": "-1.00",
        "currency": "USD",
        "description": "Synthetic Merchant",
        "source_id": "monarch:SYN-REUSED-ID",
    }
    unscoped_rows = [
        {**base, "source_file": "synthetic/connection-one.csv"},
        {**base, "source_file": "synthetic/connection-two.csv"},
    ]
    scoped_rows = [
        {
            **row,
            "source_connection_id": "SYN-CONNECTION-SHARED",
            "source_account_id": "SYN-SOURCE-ACCOUNT",
        }
        for row in unscoped_rows
    ]
    distinct_connection_rows = [
        {
            **row,
            "source_connection_id": f"SYN-CONNECTION-{index}",
            "source_account_id": "SYN-SOURCE-ACCOUNT",
        }
        for index, row in enumerate(unscoped_rows, 1)
    ]

    unscoped = resolve_identity(observations_from_transaction_rows(unscoped_rows))
    scoped = resolve_identity(observations_from_transaction_rows(scoped_rows))
    distinct_connections = resolve_identity(
        observations_from_transaction_rows(distinct_connection_rows)
    )

    assert len(unscoped.claims) == len(unscoped.canonical_events) == 2
    assert not automatic_merges(unscoped)
    assert (
        len(distinct_connections.claims)
        == len(distinct_connections.canonical_events)
        == 2
    )
    assert len(scoped.claims) == len(scoped.canonical_events) == 1
    assert any(
        item.rationale_code == "exact-scoped-source-identity"
        for item in scoped.decisions
    )


def test_synthetic_collision_corpus_has_zero_false_positive_merges():
    observations = []
    true_duplicate_ids = set()
    legitimate_repeat_ids = set()
    for index in range(50):
        account = f"SYN-ACCOUNT-TRUE-{index}"
        left_id = f"SYN-TRUE-M-{index}"
        right_id = f"SYN-TRUE-S-{index}"
        true_duplicate_ids.update((left_id, right_id))
        observations.extend(
            (
                observation(
                    left_id,
                    family="monarch",
                    provider_id=f"SYN-M-{index}",
                    account=account,
                    amount=f"-{index + 1}.00",
                    description=f"Synthetic Unique Merchant {index}",
                ),
                observation(
                    right_id,
                    family="simplefin",
                    provider_id=f"SYN-S-{index}",
                    account=account,
                    amount=f"-{index + 1}.00",
                    description=f"Synthetic Unique Merchant {index} Detail",
                ),
            )
        )
    for index in range(50):
        account = f"SYN-ACCOUNT-COLLISION-{index}"
        for family in ("monarch", "simplefin"):
            for repeat in range(2):
                observation_id = f"SYN-LEGIT-{family.upper()}-{index}-{repeat}"
                legitimate_repeat_ids.add(observation_id)
                observations.append(
                    observation(
                        observation_id,
                        family=family,
                        provider_id=(f"SYN-LEGIT-ID-{family}-{index}-{repeat}"),
                        account=account,
                        amount="-9.99",
                        description="Synthetic Repeated Purchase",
                    )
                )

    result = resolve_identity(reversed(observations))
    merged_observation_sets = [
        set(item.member_observation_ids)
        for item in result.canonical_events
        if len(item.member_claim_ids) > 1
    ]
    false_positives = sum(
        bool(members & legitimate_repeat_ids) for members in merged_observation_sets
    )

    assert merged_observation_sets == []
    assert false_positives == 0
    assert len(result.canonical_events) == len(observations)
    assert true_duplicate_ids <= {
        observation_id
        for event in result.canonical_events
        for observation_id in event.member_observation_ids
    }


def test_invalid_source_hash_and_unscoped_identity_fail_closed():
    template = observation(
        "SYN-OBS-BAD-HASH",
        family="simplefin",
        provider_id="SYN-BAD",
    )
    with pytest.raises(ValueError, match="SHA-256"):
        replace(template, source_hash="bad")

    with pytest.raises(ValueError, match="provider identity kind"):
        observation(
            "SYN-OBS-BAD-SCOPE",
            family="simplefin",
            provider_id="",
            provider_kind="simplefin-id",
        )
