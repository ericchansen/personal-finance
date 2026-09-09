"""Declared duplicate-summary account suppression regressions.

Every case here is synthetic.  The point of the suite is that an explicit,
durable operator mapping is the *only* thing that lets two observations in
different source accounts collapse into one canonical event, and that even with
the mapping in hand nothing is dropped unless a unique occurrence mapping
preserves multiplicity.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from finance_store.identity import (
    DUPLICATE_SUMMARY_DECISION_ATTRIBUTE,
    DUPLICATE_SUMMARY_MAP_ATTRIBUTE,
    DUPLICATE_SUMMARY_TARGET_ATTRIBUTE,
    DuplicateSummaryMapping,
    HumanOverride,
    duplicate_summary_mappings,
    observations_from_transaction_rows,
    resolve_identity,
)

MAP_HASH = hashlib.sha256(b"synthetic-account-map").hexdigest()
DETAIL = "SYN-SIMPLEFIN-DETAIL"
SUMMARY = "SYN-SIMPLEFIN-SUMMARY"
DETAIL_ACCOUNT = "SYN-ACCOUNT-DETAIL"
SUMMARY_ACCOUNT = "SYN-ACCOUNT-SUMMARY"


def mapping(
    source_account_id: str = SUMMARY,
    target: str = DETAIL,
) -> DuplicateSummaryMapping:
    return DuplicateSummaryMapping(
        source_family="simplefin",
        source_account_id=source_account_id,
        duplicate_of_source_account_id=target,
        decision="aggregator-account-summary",
        decided_at="2026-02-01",
        map_hash=MAP_HASH,
    )


def row(
    *,
    source_account: str,
    account_id: str,
    transaction_id: str,
    amount: str = "-42.50",
    description: str = "Synthetic Merchant",
    day: str = "2026-01-15",
    excluded: bool = False,
    **extra: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "date": day,
        "account_id": account_id,
        "amount": amount,
        "currency": "USD",
        "description": description,
        "source_id": f"simplefin:{source_account}:{transaction_id}",
        "source_file": "synthetic/simplefin.json",
        "source_connection_id": "SYN-CONNECTION",
    }
    if excluded:
        values["excluded"] = True
        values["exclusion_reason"] = "duplicate summary account"
    values.update(extra)
    return values


def pair(
    *,
    amount: str = "-42.50",
    description: str = "Synthetic Merchant",
    summary_description: str | None = None,
    day: str = "2026-01-15",
    summary_day: str | None = None,
    suffix: str = "1",
) -> list[dict[str, object]]:
    return [
        row(
            source_account=DETAIL,
            account_id=DETAIL_ACCOUNT,
            transaction_id=f"SYN-DETAIL-{suffix}",
            amount=amount,
            description=description,
            day=day,
        ),
        row(
            source_account=SUMMARY,
            account_id=SUMMARY_ACCOUNT,
            transaction_id=f"SYN-SUMMARY-{suffix}",
            amount=amount,
            description=summary_description or description,
            day=summary_day or day,
            excluded=True,
        ),
    ]


def resolve(rows, *, declared=True, overrides=()):
    observations = observations_from_transaction_rows(
        rows,
        duplicate_summaries=(mapping(),) if declared else (),
    )
    return resolve_identity(observations, overrides=overrides)


def rationales(result) -> list[str]:
    return sorted(decision.rationale_code for decision in result.decisions)


def unresolved(result) -> list[str]:
    return sorted(
        decision.rationale_code
        for decision in result.decisions
        if decision.outcome.value == "unresolved"
    )


def test_parses_only_explicit_duplicate_summary_entries():
    document = {
        "version": 1,
        "accounts": {
            DETAIL: {"action": "import", "wealthfolioAccountId": "SYN-WF"},
            SUMMARY: {
                "action": "exclude",
                "decision": "aggregator-account-summary",
                "duplicateOfSourceAccountId": DETAIL,
            },
            "SYN-DORMANT": {
                "action": "exclude",
                "decision": "dormant-zero-balance-account",
            },
            "SYN-CARD": {
                "action": "exclude",
                "decision": "employer-corporate-card",
                "duplicateOfSourceAccountId": DETAIL,
            },
        },
    }

    parsed = duplicate_summary_mappings(document, map_hash=MAP_HASH)

    assert [item.source_account_id for item in parsed] == [SUMMARY]
    assert parsed[0].duplicate_of_source_account_id == DETAIL
    assert parsed[0].map_hash == MAP_HASH


def test_parses_nothing_without_a_named_duplicate_target():
    document = {
        "version": 1,
        "accounts": {
            SUMMARY: {
                "action": "exclude",
                "decision": "aggregator-account-summary",
            }
        },
    }

    assert duplicate_summary_mappings(document, map_hash=MAP_HASH) == ()


def test_refuses_a_chained_duplicate_summary_declaration():
    document = {
        "version": 1,
        "accounts": {
            SUMMARY: {
                "action": "exclude",
                "decision": "aggregator-account-summary",
                "duplicateOfSourceAccountId": DETAIL,
            },
            DETAIL: {
                "action": "exclude",
                "decision": "aggregator-account-summary",
                "duplicateOfSourceAccountId": "SYN-THIRD",
            },
        },
    }

    with pytest.raises(ValueError):
        duplicate_summary_mappings(document, map_hash=MAP_HASH)


def test_mapping_cannot_target_itself():
    with pytest.raises(ValueError):
        mapping(source_account_id=DETAIL, target=DETAIL)


def test_mapping_requires_a_recorded_map_hash():
    with pytest.raises(ValueError):
        DuplicateSummaryMapping(
            source_family="simplefin",
            source_account_id=SUMMARY,
            duplicate_of_source_account_id=DETAIL,
            decision="aggregator-account-summary",
            decided_at="2026-02-01",
            map_hash="not-a-hash",
        )


def test_declaration_reaches_observation_attributes():
    observations = observations_from_transaction_rows(
        pair(), duplicate_summaries=(mapping(),)
    )
    declared = [
        item
        for item in observations
        if item.attribute(DUPLICATE_SUMMARY_TARGET_ATTRIBUTE)
    ]

    assert len(declared) == 1
    assert declared[0].source_account_id == SUMMARY
    assert declared[0].attribute(DUPLICATE_SUMMARY_TARGET_ATTRIBUTE) == DETAIL
    assert declared[0].attribute(DUPLICATE_SUMMARY_MAP_ATTRIBUTE) == MAP_HASH
    assert (
        declared[0].attribute(DUPLICATE_SUMMARY_DECISION_ATTRIBUTE)
        == mapping().decision_hash
    )


def test_undeclared_rows_are_byte_identical_to_the_legacy_adapter():
    rows = pair()

    assert observations_from_transaction_rows(rows) == (
        observations_from_transaction_rows(rows, duplicate_summaries=())
    )


def test_declared_pair_is_source_suppressed_into_one_event():
    result = resolve(pair())

    assert len(result.canonical_events) == 1
    assert unresolved(result) == []
    assert "declared-duplicate-summary-suppression" in rationales(result)
    assert result.report_document()["counts"][
        "declaredDuplicateSuppressions"
    ] == 1
    assert result.report_document()["residualByClass"]["source-suppressed"] == 1


def test_suppression_binds_map_and_decision_hashes_with_counts():
    result = resolve(pair())
    decision = next(
        item
        for item in result.decisions
        if item.rationale_code == "declared-duplicate-summary-suppression"
    )
    features = dict(decision.feature_vector)
    proof = dict(decision.competing_candidate_proof)

    assert features["duplicateSummaryMapHash"] == MAP_HASH
    assert features["duplicateSummaryDecisionHash"] == mapping().decision_hash
    assert features["declaredDuplicateSummary"] == "true"
    assert features["suppressedClaimId"] != features["authoritativeClaimId"]
    assert proof == {
        "authoritativeCount": 1,
        "lowerCount": 1,
        "bucketParticipantCount": 2,
        "occurrenceIndex": 0,
    }
    assert len(decision.source_hashes) == 2


def test_without_the_declaration_the_pair_stays_a_distinct_mirror_candidate():
    result = resolve(pair(), declared=False)

    # Without the explicit provider-error mapping the two legs are a
    # relationship observation, not a duplicate question: both survive, the
    # candidate stays queryable, and nothing lands in the unresolved bucket.
    assert unresolved(result) == []
    assert len(result.canonical_events) == 2
    counts = result.report_document()["counts"]
    assert counts["crossAccountMirrorCandidates"] == 1
    assert counts["unresolvedDuplicateGroups"] == 0
    assert counts["declaredDuplicateSuppressions"] == 0


def test_equal_repeat_counts_map_one_to_one_and_preserve_multiplicity():
    rows = [*pair(suffix="1"), *pair(suffix="2"), *pair(suffix="3")]

    result = resolve(rows)

    assert len(result.canonical_events) == 3
    assert unresolved(result) == []
    assert result.report_document()["counts"][
        "declaredDuplicateSuppressions"
    ] == 3


def test_extra_summary_side_repeat_refuses_to_drop_an_occurrence():
    rows = [
        *pair(suffix="1"),
        row(
            source_account=SUMMARY,
            account_id=SUMMARY_ACCOUNT,
            transaction_id="SYN-SUMMARY-EXTRA",
            excluded=True,
        ),
    ]

    result = resolve(rows)

    assert "ambiguous-declared-duplicate-multiplicity" in unresolved(result)
    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert result.report_document()["counts"][
        "declaredDuplicateSuppressions"
    ] == 0
    assert len(result.canonical_events) == 3


def test_extra_detail_side_repeat_still_suppresses_the_declared_occurrence():
    rows = [
        *pair(suffix="1"),
        row(
            source_account=DETAIL,
            account_id=DETAIL_ACCOUNT,
            transaction_id="SYN-DETAIL-EXTRA",
        ),
    ]

    result = resolve(rows)

    assert unresolved(result) == []
    assert len(result.canonical_events) == 2


def test_opposite_sign_transfer_across_the_declared_pair_is_never_suppressed():
    rows = [
        row(
            source_account=DETAIL,
            account_id=DETAIL_ACCOUNT,
            transaction_id="SYN-DETAIL-OUT",
            amount="-42.50",
        ),
        row(
            source_account=SUMMARY,
            account_id=SUMMARY_ACCOUNT,
            transaction_id="SYN-SUMMARY-IN",
            amount="42.50",
            excluded=True,
        ),
    ]

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert unresolved(result) == ["transfer-candidate"]
    assert len(result.canonical_events) == 2


def test_transfer_group_membership_blocks_declared_suppression():
    rows = pair()
    rows[0]["transfer_group"] = "SYN-GROUP"
    rows[1]["transfer_group"] = "SYN-GROUP"

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_pending_observations_are_never_declared_duplicates():
    rows = pair()
    rows[1]["status"] = "pending"

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_currency_conflict_blocks_declared_suppression():
    rows = pair()
    rows[1]["currency"] = "EUR"

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_amount_conflict_blocks_declared_suppression():
    rows = pair()
    rows[1]["amount"] = "-42.51"

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_zero_amount_observations_are_never_declared_duplicates():
    rows = pair(amount="0.00")

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_different_days_block_declared_suppression():
    rows = pair(summary_day="2026-01-16")

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_unrelated_descriptions_block_declared_suppression():
    rows = pair(summary_description="Completely Different Payee")

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_ambiguous_description_mapping_is_reported_not_guessed():
    rows = [
        row(
            source_account=DETAIL,
            account_id=DETAIL_ACCOUNT,
            transaction_id="SYN-DETAIL-A",
            description="Synthetic Merchant Store",
        ),
        row(
            source_account=DETAIL,
            account_id=DETAIL_ACCOUNT,
            transaction_id="SYN-DETAIL-B",
            description="Synthetic Merchant Store Online",
        ),
        row(
            source_account=SUMMARY,
            account_id=SUMMARY_ACCOUNT,
            transaction_id="SYN-SUMMARY-A",
            description="Synthetic Merchant Store",
            excluded=True,
        ),
    ]

    result = resolve(rows)

    assert "ambiguous-declared-duplicate-description-mapping" in unresolved(result)
    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 3


def test_explicit_reversal_lineage_wins_over_the_declaration():
    rows = pair()
    rows[1]["reversal_of"] = "simplefin:{}:SYN-DETAIL-1".format(DETAIL)

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert "explicit-reversal-lineage" in rationales(result)


def test_explicit_correction_lineage_wins_over_the_declaration():
    rows = pair()
    rows[1]["correction_of"] = "simplefin:{}:SYN-DETAIL-1".format(DETAIL)

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert "explicit-correction-lineage" in rationales(result)


def test_human_preserve_distinct_override_beats_the_declaration():
    observations = observations_from_transaction_rows(
        pair(), duplicate_summaries=(mapping(),)
    )
    claims = resolve_identity(observations).claims
    override = HumanOverride(
        override_id="SYN-OVERRIDE-DECLARED",
        version=1,
        action="preserve-distinct",
        claim_ids=tuple(sorted(item.claim_id for item in claims)),
        rationale_hash=hashlib.sha256(b"synthetic-rationale").hexdigest(),
        decided_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    result = resolve_identity(observations, overrides=(override,))

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_declared_resolution_is_replay_and_order_stable():
    rows = [*pair(suffix="1"), *pair(suffix="2")]
    observations = observations_from_transaction_rows(
        rows, duplicate_summaries=(mapping(),)
    )

    forward = resolve_identity(observations)
    reverse = resolve_identity(tuple(reversed(observations)))

    assert forward == reverse
    assert forward.report_document() == reverse.report_document()


def test_declaration_never_reaches_an_unmapped_account_pair():
    other = "SYN-SIMPLEFIN-OTHER"
    rows = [
        row(
            source_account=DETAIL,
            account_id=DETAIL_ACCOUNT,
            transaction_id="SYN-DETAIL-1",
        ),
        row(
            source_account=other,
            account_id="SYN-ACCOUNT-OTHER",
            transaction_id="SYN-OTHER-1",
        ),
    ]

    result = resolve(rows)

    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert len(result.canonical_events) == 2


def test_monarch_category_stays_enrichment_under_a_declared_suppression():
    rows = pair()
    rows[0]["category"] = "Groceries"
    rows[1]["category"] = "Groceries"

    result = resolve(rows)
    event = result.canonical_events[0]

    assert event.category == "Groceries"
    decision = next(
        item
        for item in result.decisions
        if item.rationale_code == "declared-duplicate-summary-suppression"
    )
    assert dict(decision.feature_vector)["categoryParticipates"] == "false"


def test_project_reads_declared_mappings_from_the_private_map(tmp_path):
    from importers.lineage_review.canonical import declared_duplicate_summaries

    root = tmp_path / "private"
    (root / "simplefin").mkdir(parents=True)
    payload = {
        "version": 1,
        "accounts": {
            DETAIL: {"action": "import", "wealthfolioAccountId": "SYN-WF"},
            SUMMARY: {
                "action": "exclude",
                "decision": "aggregator-account-summary",
                "duplicateOfSourceAccountId": DETAIL,
            },
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    (root / "simplefin" / "account-map.json").write_bytes(raw)

    parsed = declared_duplicate_summaries(root)

    assert [item.source_account_id for item in parsed] == [SUMMARY]
    assert parsed[0].map_hash == hashlib.sha256(raw).hexdigest()


def test_missing_private_map_yields_no_declarations(tmp_path):
    from importers.lineage_review.canonical import declared_duplicate_summaries

    assert declared_duplicate_summaries(tmp_path) == ()


def test_declaration_never_resolves_cross_source_cardinality_ambiguity():
    """The declared map is account-scoped; it must not touch same-account
    cross-family cardinality ambiguity, which only proven coverage-interval
    authority may resolve."""
    rows = [
        row(
            source_account=DETAIL,
            account_id=DETAIL_ACCOUNT,
            transaction_id=f"SYN-DETAIL-{index}",
        )
        for index in range(2)
    ]
    rows.extend(
        {
            "date": "2026-01-15",
            "account_id": DETAIL_ACCOUNT,
            "amount": "-42.50",
            "currency": "USD",
            "description": "Synthetic Merchant",
            "source_id": f"monarch:SYN-MONARCH-{index}",
            "source_file": "synthetic/monarch.csv",
        }
        for index in range(2)
    )

    result = resolve(rows)

    assert len(result.canonical_events) == 4
    assert "declared-duplicate-summary-suppression" not in rationales(result)
    assert "ambiguous-cross-source-cardinality" in unresolved(result)


def test_declared_suppression_never_drops_a_canonical_event():
    """Suppression attaches provenance; it never reduces the economic count
    below the authoritative source count."""
    rows = [*pair(suffix="1"), *pair(suffix="2")]

    result = resolve(rows)

    detail_rows = [
        item
        for item in rows
        if str(item["source_id"]).startswith(f"simplefin:{DETAIL}:")
    ]
    assert len(result.canonical_events) == len(detail_rows)
    assert all(
        len(event.member_claim_ids) == 2 for event in result.canonical_events
    )