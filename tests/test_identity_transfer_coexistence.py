"""Transfers and mirrors must not block same-account coverage suppression.

``_source_authority_plan`` used to exclude every claim touched by any
non-duplicate relation from source-coverage suppression.  That blanket guard was
wrong in kind: a transfer or a mirror candidate is a *cross-account* statement
about two distinct economic legs, and says nothing about whether one leg was
reported twice inside one account by two different writers.  An account with
many real transfers could therefore never be deduplicated.

These regressions pin the refined contract:

* transfer and mirror candidates no longer disqualify same-account suppression;
* both cross-account legs always survive, and the relationship is remapped onto
  the surviving canonical events;
* corrections, reversals, pending transitions and mirrored provider errors
  still disqualify it, because each asserts lineage about the economics of the
  claim itself;
* a suppression chain may never merge a transfer's two endpoints.

Every fixture here is invented.  No real institution, account, balance,
merchant, or transaction appears anywhere in this file.
"""

from __future__ import annotations

from datetime import datetime, timezone

from finance_store.domain import content_hash
from finance_store.identity import (
    ConfidenceTier,
    DecisionOutcome,
    HumanOverride,
    RelationKind,
    resolve_identity,
)
from finance_store.identity import (
    _AuthoritySuppression,
    _preserve_transfer_endpoints,
)

from tests.test_identity import observation
from tests.test_identity_source_authority import (
    ACCOUNT,
    OTHER_ACCOUNT,
    evidence,
    policy_with,
    suppressions,
)

GENERATED_AT = datetime(2026, 2, 12, tzinfo=timezone.utc)
TRANSFER_GROUP = "SYN-TRANSFER-GROUP-1"


def coverage(*, account: str, count: int) -> list[dict[str, object]]:
    return [
        evidence(family="qfx", strength="stable-provider-id", count=count, account=account),
        evidence(family="monarch", strength="legacy-export", count=count, account=account),
    ]


def resolve(observations, *, records, overrides=()):
    return resolve_identity(
        observations,
        policy=policy_with(*records),
        overrides=overrides,
    )


def transfer_legs(
    *,
    out_description: str = "Synthetic Transfer Out",
    in_description: str = "Synthetic Transfer In",
    amount: str = "250.00",
    day: str = "2026-01-15",
    source_group: str = TRANSFER_GROUP,
):
    """Both legs of one cross-account transfer, each seen by two writers."""

    return [
        observation(
            "SYN-QFX-OUT",
            family="qfx",
            provider_id="SYN-FITID-OUT",
            account=ACCOUNT,
            amount=f"-{amount}",
            day=day,
            description=out_description,
            source_group=source_group,
        ),
        observation(
            "SYN-MONARCH-OUT",
            family="monarch",
            provider_id="SYN-MONARCH-ID-OUT",
            account=ACCOUNT,
            amount=f"-{amount}",
            day=day,
            description=out_description,
            source_group=source_group,
        ),
        observation(
            "SYN-QFX-IN",
            family="qfx",
            provider_id="SYN-FITID-IN",
            account=OTHER_ACCOUNT,
            amount=amount,
            day=day,
            description=in_description,
            source_group=source_group,
        ),
        observation(
            "SYN-MONARCH-IN",
            family="monarch",
            provider_id="SYN-MONARCH-ID-IN",
            account=OTHER_ACCOUNT,
            amount=amount,
            day=day,
            description=in_description,
            source_group=source_group,
        ),
    ]


def transfer_records():
    return coverage(account=ACCOUNT, count=1) + coverage(
        account=OTHER_ACCOUNT, count=1
    )


def relationships_of(resolution, kind: RelationKind):
    return [item for item in resolution.relationships if item.kind is kind]


def edges_of(resolution, kind: RelationKind):
    return [item for item in resolution.edges if item.kind is kind]


# ---------------------------------------------------------------------------
# A transfer does not block deduplicating either of its legs
# ---------------------------------------------------------------------------


def test_a_transfer_seen_by_two_writers_yields_two_events_not_four():
    resolution = resolve(transfer_legs(), records=transfer_records())

    assert len(resolution.canonical_events) == 2
    accounts = sorted(event.canonical_account_hash for event in resolution.canonical_events)
    assert len(set(accounts)) == 2
    amounts = sorted(str(event.signed_amount) for event in resolution.canonical_events)
    assert amounts == ["-250.00", "250.00"]


def test_both_legacy_copies_are_source_suppressed_with_proof():
    resolution = resolve(transfer_legs(), records=transfer_records())

    planned = suppressions(resolution)
    assert len(planned) == 2
    for decision in planned:
        features = dict(decision.feature_vector)
        assert decision.rationale_code == "authoritative-source-coverage-suppression"
        assert decision.confidence_tier is ConfidenceTier.AUTHORITATIVE_SOURCE_COVERAGE
        assert features["authoritativeSourceFamily"] == "qfx"
        assert features["suppressedSourceFamily"] == "monarch"
        assert decision.source_hashes
        assert decision.decision_hash
        assert decision.competing_candidate_proof


def test_the_transfer_relationship_survives_on_the_two_surviving_events():
    resolution = resolve(transfer_legs(), records=transfer_records())

    transfers = relationships_of(resolution, RelationKind.TRANSFER)
    assert transfers
    endpoints = {
        (item.left_canonical_event_id, item.right_canonical_event_id)
        for item in transfers
    }
    assert len(endpoints) == 1
    left, right = next(iter(endpoints))
    assert left != right
    event_ids = {event.canonical_event_id for event in resolution.canonical_events}
    assert {left, right} == event_ids


def test_the_deduplicated_transfer_leaves_no_unresolved_duplicate_group():
    document = resolve(transfer_legs(), records=transfer_records()).report_document()

    assert document["counts"]["unresolvedDuplicateGroups"] == 0
    assert document["counts"]["sourceSuppressedClaims"] == 2
    assert document["counts"]["authorityAmbiguousGroups"] == 0
    assert document["counts"]["canonicalEventsAfter"] == 2
    assert document["counts"]["canonicalEventsBefore"] == 4


def test_the_transfer_candidate_form_also_permits_suppression():
    # No explicit source group, so the cross-account relation is only a
    # candidate.  A candidate is even weaker evidence about the legs, so it
    # must not block same-account deduplication either.
    resolution = resolve(
        transfer_legs(source_group=""), records=transfer_records()
    )

    assert len(resolution.canonical_events) == 2
    assert len(suppressions(resolution)) == 2
    assert relationships_of(resolution, RelationKind.TRANSFER_CANDIDATE)


def test_transfer_deduplication_is_order_stable():
    records = transfer_records()
    forward = resolve(transfer_legs(), records=records)
    reversed_input = resolve(list(reversed(transfer_legs())), records=records)

    assert forward.generation_hash == reversed_input.generation_hash
    assert forward.canonical_state_hash == reversed_input.canonical_state_hash


# ---------------------------------------------------------------------------
# Cross-account same-sign mirrors stay distinct and non-blocking
# ---------------------------------------------------------------------------


def mirror_legs():
    """Two same-sign cross-account legs sharing a declared source group."""

    return [
        observation(
            "SYN-QFX-MIRROR-LEFT",
            family="qfx",
            provider_id="SYN-FITID-MIRROR-LEFT",
            account=ACCOUNT,
            amount="-90.00",
            day="2026-01-20",
            description="Synthetic Mirror",
            source_group="SYN-MIRROR-GROUP-1",
        ),
        observation(
            "SYN-MONARCH-MIRROR-LEFT",
            family="monarch",
            provider_id="SYN-MONARCH-ID-MIRROR-LEFT",
            account=ACCOUNT,
            amount="-90.00",
            day="2026-01-20",
            description="Synthetic Mirror",
            source_group="SYN-MIRROR-GROUP-1",
        ),
        observation(
            "SYN-QFX-MIRROR-RIGHT",
            family="qfx",
            provider_id="SYN-FITID-MIRROR-RIGHT",
            account=OTHER_ACCOUNT,
            amount="-90.00",
            day="2026-01-20",
            description="Synthetic Mirror",
            source_group="SYN-MIRROR-GROUP-1",
        ),
    ]


def mirror_records():
    return [
        evidence(family="qfx", strength="stable-provider-id", count=1, account=ACCOUNT),
        evidence(family="monarch", strength="legacy-export", count=1, account=ACCOUNT),
        evidence(family="qfx", strength="stable-provider-id", count=1, account=OTHER_ACCOUNT),
    ]


def test_a_mirror_candidate_does_not_block_the_same_account_suppression():
    resolution = resolve(mirror_legs(), records=mirror_records())

    assert len(suppressions(resolution)) == 1
    assert len(resolution.canonical_events) == 2


def test_the_mirror_candidate_stays_distinct_and_queryable():
    resolution = resolve(mirror_legs(), records=mirror_records())
    document = resolution.report_document()

    mirrors = [
        decision
        for decision in resolution.decisions
        if decision.rationale_code == "cross-account-mirror-candidate"
    ]
    assert mirrors
    for decision in mirrors:
        assert decision.outcome is DecisionOutcome.PRESERVE_DISTINCT
        assert decision.confidence_tier is ConfidenceTier.REVIEW_REQUIRED
    assert document["counts"]["crossAccountMirrorCandidates"] == len(mirrors)
    assert document["counts"]["unresolvedDuplicateGroups"] == 0
    assert document["relationshipCandidateDecisions"]


# ---------------------------------------------------------------------------
# Lineage relations still disqualify coverage suppression
# ---------------------------------------------------------------------------


def guarded_pair(*, attribute: str, other_account: bool, status: str = "posted"):
    """An authoritative/legacy pair plus a third claim asserting lineage."""

    third_account = OTHER_ACCOUNT if other_account else ACCOUNT
    return [
        observation(
            "SYN-QFX-GUARDED",
            family="qfx",
            provider_id="SYN-FITID-GUARDED",
            account=ACCOUNT,
            amount="-45.00",
            day="2026-01-18",
            description="Synthetic Guarded",
        ),
        observation(
            "SYN-MONARCH-GUARDED",
            family="monarch",
            provider_id="SYN-MONARCH-ID-GUARDED",
            account=ACCOUNT,
            amount="-45.00",
            day="2026-01-18",
            description="Synthetic Guarded",
        ),
        observation(
            "SYN-QFX-LINEAGE",
            family="qfx",
            provider_id="SYN-FITID-LINEAGE",
            account=third_account,
            amount="-45.00",
            day="2026-01-19",
            description="Synthetic Guarded",
            status=status,
            attributes={attribute: "SYN-QFX-GUARDED"},
        ),
    ]


def guarded_records(*, other_account: bool):
    records = [
        evidence(family="qfx", strength="stable-provider-id", count=2, account=ACCOUNT),
        evidence(family="monarch", strength="legacy-export", count=1, account=ACCOUNT),
    ]
    if other_account:
        records = [
            evidence(family="qfx", strength="stable-provider-id", count=1, account=ACCOUNT),
            evidence(family="monarch", strength="legacy-export", count=1, account=ACCOUNT),
            evidence(
                family="qfx", strength="stable-provider-id", count=1, account=OTHER_ACCOUNT
            ),
        ]
    return records


def test_a_correction_still_blocks_coverage_suppression():
    resolution = resolve(
        guarded_pair(attribute="correction_of", other_account=False),
        records=guarded_records(other_account=False),
    )

    assert suppressions(resolution) == []
    assert relationships_of(resolution, RelationKind.CORRECTION)


def test_a_reversal_still_blocks_coverage_suppression():
    resolution = resolve(
        guarded_pair(attribute="reversal_of", other_account=False),
        records=guarded_records(other_account=False),
    )

    assert suppressions(resolution) == []
    assert relationships_of(resolution, RelationKind.REVERSAL)


def test_a_pending_transition_still_blocks_coverage_suppression():
    resolution = resolve(
        [
            observation(
                "SYN-QFX-GUARDED",
                family="qfx",
                provider_id="SYN-FITID-GUARDED",
                account=ACCOUNT,
                amount="-45.00",
                day="2026-01-18",
                description="Synthetic Guarded",
            ),
            observation(
                "SYN-MONARCH-GUARDED",
                family="monarch",
                provider_id="SYN-MONARCH-ID-GUARDED",
                account=ACCOUNT,
                amount="-45.00",
                day="2026-01-18",
                description="Synthetic Guarded",
            ),
            observation(
                "SYN-QFX-PENDING",
                family="qfx",
                provider_id="SYN-FITID-PENDING",
                account=ACCOUNT,
                amount="-45.00",
                day="2026-01-17",
                description="Synthetic Guarded",
                status="pending",
                attributes={"pending_of": "SYN-QFX-GUARDED"},
            ),
        ],
        records=guarded_records(other_account=False),
    )

    assert suppressions(resolution) == []
    assert edges_of(resolution, RelationKind.PENDING_TRANSITION)


def test_a_mirrored_provider_error_still_blocks_coverage_suppression():
    resolution = resolve(
        guarded_pair(attribute="provider_error_of", other_account=True),
        records=guarded_records(other_account=True),
    )

    assert suppressions(resolution) == []
    assert edges_of(resolution, RelationKind.MIRRORED_PROVIDER_ERROR)


# ---------------------------------------------------------------------------
# A suppression chain may never collapse a transfer's endpoints
# ---------------------------------------------------------------------------


def same_account_transfer_with_third_copy():
    """Two operator-declared same-account transfer legs plus a third copy."""

    return [
        observation(
            "SYN-QFX-ENDPOINT-A",
            family="qfx",
            provider_id="SYN-FITID-ENDPOINT-A",
            account=ACCOUNT,
            amount="-125.00",
            day="2026-01-22",
            description="Synthetic Sweep",
        ),
        observation(
            "SYN-QFX-ENDPOINT-B",
            family="qfx",
            provider_id="SYN-FITID-ENDPOINT-B",
            account=ACCOUNT,
            amount="-125.00",
            day="2026-01-22",
            description="Synthetic Sweep",
        ),
        observation(
            "SYN-MONARCH-ENDPOINT-C",
            family="monarch",
            provider_id="SYN-MONARCH-ID-ENDPOINT-C",
            account=ACCOUNT,
            amount="-125.00",
            day="2026-01-22",
            description="Synthetic Sweep",
        ),
    ]


def endpoint_override(resolution) -> HumanOverride:
    claims = sorted(
        claim.claim_id
        for claim in resolution.claims
        if claim.selected_observation_id
        in {"SYN-QFX-ENDPOINT-A", "SYN-QFX-ENDPOINT-B"}
    )
    return HumanOverride(
        override_id="SYN-OVERRIDE-TRANSFER-1",
        version=1,
        action="transfer",
        claim_ids=tuple(claims),
        rationale_hash=content_hash("SYN-OVERRIDE-RATIONALE"),
        decided_at=GENERATED_AT,
    )


def endpoint_records():
    return [
        evidence(family="qfx", strength="stable-provider-id", count=2, account=ACCOUNT),
        evidence(family="monarch", strength="legacy-export", count=1, account=ACCOUNT),
    ]


def test_a_declared_transfer_pair_is_never_directly_suppressed():
    records = endpoint_records()
    observations = same_account_transfer_with_third_copy()
    override = endpoint_override(resolve(observations, records=records))

    resolution = resolve(observations, records=records, overrides=[override])

    claim_of = {
        claim.selected_observation_id: claim.claim_id for claim in resolution.claims
    }
    endpoints = {
        claim_of["SYN-QFX-ENDPOINT-A"],
        claim_of["SYN-QFX-ENDPOINT-B"],
    }
    for decision in suppressions(resolution):
        assert set(decision.claim_ids) != endpoints


def test_a_declared_transfer_keeps_its_endpoints_on_distinct_events():
    records = endpoint_records()
    observations = same_account_transfer_with_third_copy()
    override = endpoint_override(resolve(observations, records=records))

    resolution = resolve(observations, records=records, overrides=[override])

    mapping = resolution.observation_to_canonical
    assert (
        mapping["SYN-QFX-ENDPOINT-A"] != mapping["SYN-QFX-ENDPOINT-B"]
    ), "collapsing source copies must never merge a declared transfer's legs"


def test_the_endpoint_guard_refuses_a_chain_that_would_merge_a_transfer():
    # The bucket matching in ``_source_authority_plan`` can only ever produce a
    # matching from distinct lower claims onto distinct authoritative ones, so
    # this chain is not reachable through the planner today.  The guard is the
    # invariant that keeps it unreachable if that ever changes, so it is
    # exercised directly rather than left unproven.
    suppression = _AuthoritySuppression(
        suppressed_claim_id="SYN-CLAIM-A",
        authoritative_claim_id="SYN-CLAIM-C",
        feature_vector=(("authoritativeSourceFamily", "qfx"),),
        proof=(("authoritativeSourceTransactionCount", 1),),
        source_hashes=(content_hash("SYN-CHAIN-LEFT"),),
    )
    chained = _AuthoritySuppression(
        suppressed_claim_id="SYN-CLAIM-B",
        authoritative_claim_id="SYN-CLAIM-C",
        feature_vector=(("authoritativeSourceFamily", "qfx"),),
        proof=(("authoritativeSourceTransactionCount", 1),),
        source_hashes=(content_hash("SYN-CHAIN-RIGHT"),),
    )

    kept, dropped = _preserve_transfer_endpoints(
        [suppression, chained], {("SYN-CLAIM-A", "SYN-CLAIM-B")}
    )

    assert [item.suppressed_claim_id for item in kept] == ["SYN-CLAIM-A"]
    assert [item.suppressed_claim_id for item in dropped] == ["SYN-CLAIM-B"]


def test_the_endpoint_guard_is_order_stable():
    items = [
        _AuthoritySuppression(
            suppressed_claim_id=suppressed,
            authoritative_claim_id="SYN-CLAIM-C",
            feature_vector=(),
            proof=(),
            source_hashes=(content_hash(suppressed),),
        )
        for suppressed in ("SYN-CLAIM-B", "SYN-CLAIM-A")
    ]
    transfers = {("SYN-CLAIM-A", "SYN-CLAIM-B")}

    forward = _preserve_transfer_endpoints(items, transfers)
    backward = _preserve_transfer_endpoints(list(reversed(items)), transfers)

    assert [item.suppressed_claim_id for item in forward[0]] == [
        item.suppressed_claim_id for item in backward[0]
    ]
    assert [item.suppressed_claim_id for item in forward[1]] == [
        item.suppressed_claim_id for item in backward[1]
    ]


def test_the_endpoint_guard_leaves_unrelated_suppressions_alone():
    items = [
        _AuthoritySuppression(
            suppressed_claim_id="SYN-CLAIM-D",
            authoritative_claim_id="SYN-CLAIM-E",
            feature_vector=(),
            proof=(),
            source_hashes=(content_hash("SYN-UNRELATED"),),
        )
    ]

    kept, dropped = _preserve_transfer_endpoints(
        items, {("SYN-CLAIM-A", "SYN-CLAIM-B")}
    )

    assert len(kept) == 1
    assert dropped == []


def test_without_the_declared_transfer_the_third_copy_is_suppressed():
    resolution = resolve(
        same_account_transfer_with_third_copy(), records=endpoint_records()
    )

    assert len(suppressions(resolution)) == 1
    assert len(resolution.canonical_events) == 2
