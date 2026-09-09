"""Unproven cross-account mirror candidates are a relationship, not a duplicate.

Two same-sign observations in *different* canonical accounts that look alike are
a graph observation about the ledger, not a question about whether one of them is
a copy of the other.  Only an explicit, durable provider-error mapping may
collapse such a pair.  Absent that mapping the engine must:

- keep both canonical events, with every observation still attached;
- classify the residual as ``distinct`` rather than ``unresolved``, so the pair
  never inflates the unresolved-duplicate count that gates a projection;
- keep the candidate edge visible and queryable for audit under its own count.

Every fixture in this file is invented.  No real institution, account, balance,
merchant, or transaction appears anywhere here.
"""

from __future__ import annotations

from finance_store.identity import (
    DecisionOutcome,
    DuplicateSummaryMapping,
    observations_from_transaction_rows,
    resolve_identity,
)

MAP_HASH = "b" * 64
LEFT_SOURCE = "SYN-SIMPLEFIN-LEFT"
RIGHT_SOURCE = "SYN-SIMPLEFIN-RIGHT"
LEFT_ACCOUNT = "SYN-ACCOUNT-LEFT"
RIGHT_ACCOUNT = "SYN-ACCOUNT-RIGHT"


def row(
    *,
    source_account: str,
    account_id: str,
    transaction_id: str,
    amount: str = "-42.50",
    description: str = "Synthetic Merchant",
    day: str = "2026-01-15",
) -> dict[str, object]:
    return {
        "date": day,
        "account_id": account_id,
        "amount": amount,
        "currency": "USD",
        "description": description,
        "source_id": f"simplefin:{source_account}:{transaction_id}",
        "source_file": "synthetic/simplefin.json",
        "source_connection_id": "SYN-CONNECTION",
    }


def mirror_pair(suffix: str = "1", **kwargs: object) -> list[dict[str, object]]:
    return [
        row(
            source_account=LEFT_SOURCE,
            account_id=LEFT_ACCOUNT,
            transaction_id=f"SYN-LEFT-{suffix}",
            **kwargs,
        ),
        row(
            source_account=RIGHT_SOURCE,
            account_id=RIGHT_ACCOUNT,
            transaction_id=f"SYN-RIGHT-{suffix}",
            **kwargs,
        ),
    ]


def resolve(rows, *, declared: bool = False):
    summaries = (
        (
            DuplicateSummaryMapping(
                source_family="simplefin",
                source_account_id=RIGHT_SOURCE,
                duplicate_of_source_account_id=LEFT_SOURCE,
                decision="aggregator-account-summary",
                decided_at="2026-02-01",
                map_hash=MAP_HASH,
            ),
        )
        if declared
        else ()
    )
    return resolve_identity(
        observations_from_transaction_rows(rows, duplicate_summaries=summaries)
    )


def mirror_decisions(result):
    return [
        decision
        for decision in result.decisions
        if decision.rationale_code == "cross-account-mirror-candidate"
    ]


def counts(result):
    return result.report_document()["counts"]


def unresolved_codes(result) -> list[str]:
    return sorted(
        decision.rationale_code
        for decision in result.decisions
        if decision.outcome is DecisionOutcome.UNRESOLVED
    )


# ---------------------------------------------------------------------------
# Both legs survive
# ---------------------------------------------------------------------------


def test_both_legs_remain_separate_canonical_events():
    result = resolve(mirror_pair())

    assert len(result.canonical_events) == 2
    assert len(mirror_decisions(result)) == 1
    hashes = {event.canonical_account_hash for event in result.canonical_events}
    assert len(hashes) == 2


def test_no_observation_is_dropped_or_suppressed():
    rows = mirror_pair()
    result = resolve(rows)

    observed = {
        observation_id
        for event in result.canonical_events
        for observation_id in event.member_observation_ids
    }
    assert len(observed) == len(rows)
    assert counts(result)["sourceSuppressedClaims"] == 0
    assert all(
        len(event.member_observation_ids) == 1 for event in result.canonical_events
    )


def test_repeated_mirror_pairs_never_collapse_multiplicity():
    rows = [
        *mirror_pair("1", amount="-42.50"),
        *mirror_pair("2", amount="-19.00"),
        *mirror_pair("3", amount="-7.25"),
    ]
    result = resolve(rows)

    assert len(result.canonical_events) == 6
    assert counts(result)["unresolvedDuplicateGroups"] == 0


# ---------------------------------------------------------------------------
# The residual is `distinct`, not `unresolved`
# ---------------------------------------------------------------------------


def test_the_decision_outcome_preserves_both_claims():
    decision = mirror_decisions(resolve(mirror_pair()))[0]

    assert decision.outcome is DecisionOutcome.PRESERVE_DISTINCT
    assert decision.residual_classification == "distinct"
    assert len(decision.claim_ids) == 2
    assert len(decision.canonical_event_ids) == 2


def test_the_pair_does_not_count_as_an_unresolved_duplicate_group():
    result = resolve(mirror_pair())

    assert unresolved_codes(result) == []
    assert counts(result)["unresolvedDuplicateGroups"] == 0
    assert result.report_document()["residualByClass"].get("unresolved", 0) == 0


def test_the_residual_class_records_the_pair_as_distinct():
    residual = resolve(mirror_pair()).report_document()["residualByClass"]

    assert residual["distinct"] >= 1


def test_a_projection_gate_on_unresolved_duplicates_is_not_blocked():
    """The count a rebuild projection refuses to advance past stays at zero."""

    rows = [
        *mirror_pair("1", amount="-42.50"),
        *mirror_pair("2", amount="-19.00"),
    ]
    result = resolve(rows)

    assert counts(result)["unresolvedDuplicateGroups"] == 0
    assert counts(result)["authorityAmbiguousGroups"] == 0


# ---------------------------------------------------------------------------
# The candidate stays queryable
# ---------------------------------------------------------------------------


def test_the_candidate_is_counted_separately_for_audit():
    result = resolve(
        [
            *mirror_pair("1", amount="-42.50"),
            *mirror_pair("2", amount="-19.00"),
        ]
    )

    assert counts(result)["crossAccountMirrorCandidates"] == 2
    assert counts(result)["unresolvedDuplicateGroups"] == 0


def test_the_candidate_decision_is_published_with_its_proof():
    document = resolve(mirror_pair()).report_document()
    published = document["relationshipCandidateDecisions"]

    assert [item["rationaleCode"] for item in published] == [
        "cross-account-mirror-candidate"
    ]
    decision = published[0]
    assert decision["outcome"] == "preserve-distinct"
    assert decision["residualClassification"] == "distinct"
    assert decision["confidenceTier"] == "review-required"
    assert len(decision["claimIds"]) == 2
    assert len(decision["canonicalEventIds"]) == 2
    assert len(decision["sourceHashes"]) == 2
    assert decision["decisionHash"] in document["decisionHashes"]


def test_the_candidate_is_absent_from_the_unresolved_publication():
    document = resolve(mirror_pair()).report_document()

    assert document["unresolvedDecisions"] == []
    assert document["automaticDecisions"] == []
    assert len(document["relationshipCandidateDecisions"]) == 1


def test_the_candidate_decision_is_replay_stable():
    first = mirror_decisions(resolve(mirror_pair()))[0]
    second = mirror_decisions(resolve(list(reversed(mirror_pair()))))[0]

    assert first.decision_hash == second.decision_hash


# ---------------------------------------------------------------------------
# Only an explicit mapping may still suppress
# ---------------------------------------------------------------------------


def test_an_explicit_provider_error_mapping_still_suppresses():
    result = resolve(mirror_pair(), declared=True)

    assert len(result.canonical_events) == 1
    assert counts(result)["declaredDuplicateSuppressions"] == 1
    assert counts(result)["crossAccountMirrorCandidates"] == 0
    assert counts(result)["unresolvedDuplicateGroups"] == 0


def test_a_transfer_candidate_is_still_a_review_residual():
    """Reclassifying mirrors must not quietly reclassify opposite-sign pairs."""

    rows = [
        row(
            source_account=LEFT_SOURCE,
            account_id=LEFT_ACCOUNT,
            transaction_id="SYN-OUT",
            amount="-42.50",
        ),
        row(
            source_account=RIGHT_SOURCE,
            account_id=RIGHT_ACCOUNT,
            transaction_id="SYN-IN",
            amount="42.50",
        ),
    ]
    result = resolve(rows)

    transfers = [
        decision
        for decision in result.decisions
        if decision.rationale_code == "transfer-candidate"
    ]
    assert len(transfers) == 1
    assert transfers[0].outcome is DecisionOutcome.UNRESOLVED
    assert len(result.canonical_events) == 2
    # A transfer candidate has always been excluded from the duplicate count.
    assert counts(result)["unresolvedDuplicateGroups"] == 0
    assert counts(result)["transferCandidates"] == 1
