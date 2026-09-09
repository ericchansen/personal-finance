"""Synthetic regressions for rationale-aware residual classification.

`residualByClass.unresolved` is the number that gates the cutover, so it has to
mean exactly one thing: an open duplicate question nothing has classified.  Two
populations used to inflate it without being duplicate questions at all --
unproven transfer candidates and observations excluded by trust cutoff or
account status.  Both are now classified, and both stay visible.

Every fixture here is invented.  No real institution, account, balance,
merchant, or transaction appears anywhere in this file.
"""

from __future__ import annotations

import pytest

from finance_store.identity import (
    RESIDUAL_CLASSES,
    RESIDUAL_CLASSIFICATION,
    RESIDUAL_CLASSIFICATION_BY_RATIONALE,
    ConfidenceTier,
    DecisionOutcome,
    RelationKind,
    resolve_identity,
)

from tests.test_identity import observation

CHECKING = "SYN-ACCOUNT-CHECKING"
SAVINGS = "SYN-ACCOUNT-SAVINGS"


def transfer_candidate_pair(
    suffix: str = "A", *, amount: str = "-250.00", day: str = "2026-01-15"
):
    """Opposite-sign cross-account legs with no explicit counterpart lineage."""

    return (
        observation(
            f"SYN-OBS-OUT-{suffix}",
            family="simplefin",
            provider_id=f"SYN-OUT-{suffix}",
            account=CHECKING,
            amount=amount,
            day=day,
            description="Synthetic Internal Move",
        ),
        observation(
            f"SYN-OBS-IN-{suffix}",
            family="simplefin",
            provider_id=f"SYN-IN-{suffix}",
            account=SAVINGS,
            amount=amount.lstrip("-"),
            day=day,
            description="Synthetic Internal Move",
        ),
    )


def untrusted_row(suffix: str = "A"):
    return observation(
        f"SYN-OBS-STALE-{suffix}",
        family="simplefin",
        provider_id=f"SYN-STALE-{suffix}",
        amount="-31.00",
        day="2026-01-20",
        trust_cutoff="2026-01-10",
    )


def excluded_row(suffix: str = "A"):
    return observation(
        f"SYN-OBS-CLOSED-{suffix}",
        family="simplefin",
        provider_id=f"SYN-CLOSED-{suffix}",
        account="SYN-ACCOUNT-CLOSED",
        amount="-17.00",
        day="2026-01-21",
        account_status="excluded",
    )


def ambiguous_duplicate_pair():
    """Two sources, same economic tuple, no authority declared: still open."""

    return (
        observation(
            "SYN-OBS-QFX",
            family="qfx",
            provider_id="SYN-QFX-1",
            amount="-88.00",
            day="2026-01-18",
            description="Synthetic Merchant One",
        ),
        observation(
            "SYN-OBS-MONARCH-A",
            family="monarch",
            provider_id="SYN-MONARCH-1",
            amount="-88.00",
            day="2026-01-18",
            description="Synthetic Merchant One",
        ),
        observation(
            "SYN-OBS-MONARCH-B",
            family="monarch",
            provider_id="SYN-MONARCH-2",
            amount="-88.00",
            day="2026-01-18",
            description="Synthetic Merchant One",
        ),
    )


def resolve(observations):
    return resolve_identity(tuple(observations))


def residual(observations):
    return resolve(observations).report_document()["residualByClass"]


def counts(observations):
    return resolve(observations).report_document()["counts"]


def decisions_by_rationale(resolution, rationale: str):
    return [
        decision
        for decision in resolution.decisions
        if decision.rationale_code == rationale
    ]


# ---------------------------------------------------------------------------
# The classification tables themselves
# ---------------------------------------------------------------------------


def test_every_outcome_and_rationale_override_maps_into_a_declared_class():
    assert set(RESIDUAL_CLASSIFICATION) == set(DecisionOutcome)
    assert set(RESIDUAL_CLASSIFICATION.values()) <= set(RESIDUAL_CLASSES)
    assert set(RESIDUAL_CLASSIFICATION_BY_RATIONALE.values()) <= set(RESIDUAL_CLASSES)


def test_no_unrequested_residual_class_was_invented():
    assert RESIDUAL_CLASSES == (
        "distinct",
        "source-suppressed",
        "transfer",
        "correction",
        "reversal",
        "unresolved",
    )


def test_the_rationale_override_is_narrow_by_construction():
    # Exactly one rationale reclassifies its outcome. Anything broader would
    # start hiding real duplicate ambiguity behind a rationale string.
    assert set(RESIDUAL_CLASSIFICATION_BY_RATIONALE) == {"transfer-candidate"}


def test_an_untrusted_exclusion_is_a_decided_outcome_not_an_open_question():
    assert RESIDUAL_CLASSIFICATION[DecisionOutcome.EXCLUDE_UNTRUSTED] == "distinct"


# ---------------------------------------------------------------------------
# Transfer candidates classify as transfer
# ---------------------------------------------------------------------------


def test_a_transfer_candidate_classifies_as_transfer_not_unresolved():
    resolution = resolve(transfer_candidate_pair())
    candidates = decisions_by_rationale(resolution, "transfer-candidate")

    assert len(candidates) == 1
    assert candidates[0].outcome is DecisionOutcome.UNRESOLVED
    assert candidates[0].residual_classification == "transfer"


def test_transfer_candidates_leave_the_unresolved_residual_at_zero():
    document = resolve(transfer_candidate_pair()).report_document()

    assert document["residualByClass"]["unresolved"] == 0
    assert document["residualByClass"]["transfer"] == 1
    assert document["counts"]["unresolvedDuplicateGroups"] == 0
    assert document["unresolvedDecisions"] == []


def test_a_transfer_candidate_still_preserves_both_legs():
    resolution = resolve(transfer_candidate_pair())

    assert len(resolution.canonical_events) == 2
    assert len(set(resolution.observation_to_canonical.values())) == 2


def test_a_transfer_candidate_stays_visible_for_review():
    resolution = resolve(transfer_candidate_pair())
    document = resolution.report_document()
    published = document["relationshipCandidateDecisions"]

    assert len(published) == 1
    assert published[0]["rationaleCode"] == "transfer-candidate"
    assert published[0]["residualClassification"] == "transfer"
    assert published[0]["confidenceTier"] == ConfidenceTier.REVIEW_REQUIRED.value
    assert document["counts"]["transferCandidates"] == 1
    assert document["counts"]["reviewRequiredRelationshipCandidates"] == 1


def test_the_transfer_candidate_edge_is_still_queryable():
    resolution = resolve(transfer_candidate_pair())
    kinds = {edge.kind for edge in resolution.edges}

    assert RelationKind.TRANSFER_CANDIDATE in kinds
    assert any(
        item.kind is RelationKind.TRANSFER_CANDIDATE
        for item in resolution.relationships
    )


def test_mirror_and_transfer_candidates_share_the_review_surface():
    mirror = (
        observation(
            "SYN-OBS-MIRROR-LEFT",
            family="simplefin",
            provider_id="SYN-MIRROR-L",
            account=CHECKING,
            amount="-64.00",
            day="2026-01-22",
            description="Synthetic Mirror",
        ),
        observation(
            "SYN-OBS-MIRROR-RIGHT",
            family="simplefin",
            provider_id="SYN-MIRROR-R",
            account=SAVINGS,
            amount="-64.00",
            day="2026-01-22",
            description="Synthetic Mirror",
        ),
    )
    document = resolve(transfer_candidate_pair() + mirror).report_document()
    published = {
        item["rationaleCode"] for item in document["relationshipCandidateDecisions"]
    }

    assert published == {"transfer-candidate", "cross-account-mirror-candidate"}
    assert document["counts"]["crossAccountMirrorCandidates"] == 1
    assert document["counts"]["transferCandidates"] == 1
    assert document["counts"]["reviewRequiredRelationshipCandidates"] == 2
    assert document["residualByClass"]["unresolved"] == 0


# ---------------------------------------------------------------------------
# Untrusted exclusions classify as distinct
# ---------------------------------------------------------------------------


def test_a_trust_cutoff_exclusion_classifies_as_distinct():
    resolution = resolve((untrusted_row(),))
    excluded = [
        decision
        for decision in resolution.decisions
        if decision.outcome is DecisionOutcome.EXCLUDE_UNTRUSTED
    ]

    assert len(excluded) == 1
    assert excluded[0].residual_classification == "distinct"
    assert excluded[0].rationale_code == "trust-cutoff-or-account-exclusion"


def test_an_account_exclusion_classifies_as_distinct():
    resolution = resolve((excluded_row(),))
    document = resolution.report_document()

    assert document["residualByClass"]["unresolved"] == 0
    assert document["residualByClass"]["distinct"] == 1
    assert document["counts"]["excludedUntrustedObservations"] == 1


def test_exclusions_keep_their_evidence_and_their_observation():
    resolution = resolve((untrusted_row(), excluded_row()))
    excluded = [
        decision
        for decision in resolution.decisions
        if decision.outcome is DecisionOutcome.EXCLUDE_UNTRUSTED
    ]
    features = [dict(decision.feature_vector) for decision in excluded]

    assert len(resolution.observations) == 2
    assert {item["afterTrustCutoff"] for item in features} == {"true", "false"}
    assert {item["accountExcluded"] for item in features} == {"true", "false"}
    assert all(decision.source_hashes for decision in excluded)


def test_an_exclusion_is_not_published_as_a_relationship_candidate():
    # It is decided by explicit lineage, not awaiting review.
    document = resolve((untrusted_row(),)).report_document()

    assert document["relationshipCandidateDecisions"] == []
    assert document["counts"]["reviewRequiredRelationshipCandidates"] == 0


# ---------------------------------------------------------------------------
# The combined shape the private replay reports
# ---------------------------------------------------------------------------


def test_only_candidates_and_exclusions_means_a_zero_unresolved_residual():
    document = resolve(
        transfer_candidate_pair("A")
        + transfer_candidate_pair("B", amount="-410.00", day="2026-01-16")
        + (untrusted_row(), excluded_row())
    ).report_document()

    assert document["residualByClass"]["unresolved"] == 0
    assert document["residualByClass"]["transfer"] == 2
    assert document["residualByClass"]["distinct"] == 2
    assert document["counts"]["unresolvedDuplicateGroups"] == 0
    assert document["counts"]["transferCandidates"] == 2
    assert document["counts"]["excludedUntrustedObservations"] == 2


def test_genuine_duplicate_ambiguity_still_counts_as_unresolved():
    document = resolve(ambiguous_duplicate_pair()).report_document()

    assert document["residualByClass"]["unresolved"] > 0
    assert document["counts"]["unresolvedDuplicateGroups"] > 0
    assert document["unresolvedDecisions"]


def test_ambiguity_survives_alongside_candidates_and_exclusions():
    document = resolve(
        ambiguous_duplicate_pair()
        + transfer_candidate_pair()
        + (untrusted_row(), excluded_row())
    ).report_document()
    open_codes = {item["rationaleCode"] for item in document["unresolvedDecisions"]}

    assert document["residualByClass"]["unresolved"] == len(
        document["unresolvedDecisions"]
    )
    assert document["residualByClass"]["unresolved"] > 0
    assert "transfer-candidate" not in open_codes
    assert "trust-cutoff-or-account-exclusion" not in open_codes


def test_every_decision_lands_in_exactly_one_declared_class():
    resolution = resolve(
        ambiguous_duplicate_pair()
        + transfer_candidate_pair()
        + (untrusted_row(), excluded_row())
    )
    document = resolution.report_document()

    assert sum(document["residualByClass"].values()) == len(resolution.decisions)
    for decision in resolution.decisions:
        assert decision.residual_classification in RESIDUAL_CLASSES


def test_unresolved_decisions_are_exactly_the_unresolved_residual():
    resolution = resolve(
        ambiguous_duplicate_pair()
        + transfer_candidate_pair()
        + (untrusted_row(), excluded_row())
    )
    document = resolution.report_document()

    assert len(document["unresolvedDecisions"]) == document["counts"][
        "unresolvedDuplicateGroups"
    ]
    assert all(
        item["residualClassification"] == "unresolved"
        for item in document["unresolvedDecisions"]
    )


# ---------------------------------------------------------------------------
# Projection semantics are untouched
# ---------------------------------------------------------------------------


def test_reclassification_changes_no_canonical_event():
    observations = (
        transfer_candidate_pair() + (untrusted_row(), excluded_row())
    )
    resolution = resolve(observations)

    # One event per observation: nothing merged, nothing dropped.
    assert len(resolution.canonical_events) == len(observations)
    assert len(resolution.observation_to_canonical) == len(observations)


def test_reclassification_is_replay_stable():
    observations = list(
        ambiguous_duplicate_pair()
        + transfer_candidate_pair()
        + (untrusted_row(), excluded_row())
    )
    forward = resolve(observations)
    backward = resolve(list(reversed(observations)))

    assert forward.generation_hash == backward.generation_hash
    assert forward.canonical_state_hash == backward.canonical_state_hash
    assert (
        forward.report_document()["residualByClass"]
        == backward.report_document()["residualByClass"]
    )


@pytest.mark.parametrize("rationale", sorted(RESIDUAL_CLASSIFICATION_BY_RATIONALE))
def test_an_overridden_rationale_never_lands_in_unresolved(rationale: str):
    assert RESIDUAL_CLASSIFICATION_BY_RATIONALE[rationale] != "unresolved"
