"""Synthetic regressions for the declared posting-date window.

A stable ``.qfx`` extract records the institution's posting date while a legacy
aggregator export records the day it saw the row, so one settlement can carry
two different source dates.  This module proves that an authoritative interval
may only reach across that gap when an operator declared the window, and that
the resulting decision records the real difference instead of pretending the two
writers agreed on a day.

Every fixture here is invented.  No real institution, account, balance,
merchant, or transaction appears anywhere in this file.
"""

from __future__ import annotations

import json

import pytest

from finance_store.identity import (
    MAX_POSTING_DATE_TOLERANCE_DAYS,
    DecisionOutcome,
    build_source_authority,
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

QFX_DAY = "2026-01-15"
MONARCH_DAY = "2026-01-17"
EXTRACT_DAY = "2026-01-15"
EXTRACT_MONARCH_DAY = "2026-01-16"


def qfx_interval(*, tolerance: int = 2, **kwargs: object) -> dict[str, object]:
    record = evidence(
        family="qfx",
        strength="stable-provider-id",
        count=kwargs.pop("count", 1),
        **kwargs,
    )
    record["posting_date_tolerance_days"] = tolerance
    return record


def monarch_interval(**kwargs: object) -> dict[str, object]:
    return evidence(
        family="monarch",
        strength="legacy-export",
        count=kwargs.pop("count", 1),
        **kwargs,
    )


def extract_interval(*, tolerance: int = 1, **kwargs: object) -> dict[str, object]:
    """A filename-declared synthetic CSV window: replay-stable, not provider-stable."""

    record = evidence(
        family="extract",
        strength="synthetic-csv",
        count=kwargs.pop("count", 1),
        stable_id_support=False,
        replay_stable_ids=True,
        **kwargs,
    )
    record["posting_date_tolerance_days"] = tolerance
    return record


def extract_row(
    observation_id: str = "OBS-EXTRACT",
    *,
    day: str = EXTRACT_DAY,
    amount: str = "-42.00",
    description: str = "Synthetic Merchant",
    account: str = ACCOUNT,
    provider_id: str = "extract:synthetic:0000000000000001",
):
    return observation(
        observation_id,
        family="extract",
        provider_id=provider_id,
        account=account,
        amount=amount,
        day=day,
        description=description,
    )


def qfx_row(
    observation_id: str = "OBS-QFX",
    *,
    day: str = QFX_DAY,
    amount: str = "-42.00",
    description: str = "Synthetic Merchant",
    account: str = ACCOUNT,
    provider_id: str = "QFX-FITID-1",
):
    return observation(
        observation_id,
        family="qfx",
        provider_id=provider_id,
        account=account,
        amount=amount,
        day=day,
        description=description,
    )


def monarch_row(
    observation_id: str = "OBS-MONARCH",
    *,
    day: str = MONARCH_DAY,
    amount: str = "-42.00",
    description: str = "Synthetic Merchant",
    account: str = ACCOUNT,
    provider_id: str = "MONARCH-ROW-1",
    category: str = "",
):
    return observation(
        observation_id,
        family="monarch",
        provider_id=provider_id,
        account=account,
        amount=amount,
        day=day,
        description=description,
        category=category,
    )


def resolve(observations, records, **kwargs):
    return resolve_identity(observations, policy=policy_with(*records), **kwargs)


def features(decision) -> dict[str, str]:
    return dict(decision.feature_vector)


def proof(decision) -> dict[str, str]:
    return dict(decision.competing_candidate_proof)


# ---------------------------------------------------------------------------
# Evidence surface
# ---------------------------------------------------------------------------


def test_tolerance_defaults_to_zero_and_does_not_change_the_evidence_hash():
    plain = build_source_authority([evidence(family="qfx", strength="stable-provider-id", count=1)])
    declared = build_source_authority([qfx_interval(tolerance=0)])
    assert plain.intervals[0].posting_date_tolerance_days == 0
    assert (
        plain.intervals[0].evidence.evidence_hash
        == declared.intervals[0].evidence.evidence_hash
    )
    assert "postingDateToleranceDays" not in plain.intervals[0].evidence.document()


def test_declared_tolerance_is_recorded_in_the_evidence_document():
    interval = build_source_authority([qfx_interval(tolerance=2)]).intervals[0]
    assert interval.posting_date_tolerance_days == 2
    assert interval.evidence.document()["postingDateToleranceDays"] == 2


def test_declared_tolerance_changes_the_authority_hash():
    without = build_source_authority([qfx_interval(tolerance=0)])
    with_window = build_source_authority([qfx_interval(tolerance=2)])
    assert without.authority_hash != with_window.authority_hash


def test_tolerance_beyond_the_ceiling_is_rejected():
    with pytest.raises(ValueError, match="posting date tolerance"):
        build_source_authority(
            [qfx_interval(tolerance=MAX_POSTING_DATE_TOLERANCE_DAYS + 1)]
        )


def test_negative_tolerance_is_rejected():
    with pytest.raises(ValueError, match="posting date tolerance"):
        build_source_authority([qfx_interval(tolerance=-1)])


def test_a_source_with_neither_identifier_kind_may_not_declare_a_window():
    record = monarch_interval()
    record["stable_id_support"] = False
    record["replay_stable_ids"] = False
    record["posting_date_tolerance_days"] = 2
    with pytest.raises(ValueError, match="stable or replay-stable"):
        build_source_authority([record])


def test_replay_stable_identifiers_alone_admit_a_declared_window():
    # A synthetic CSV extract derives its row identifiers deterministically from
    # content and occurrence.  They are not provider-stable, but they are fixed
    # across replays, which is what pins the occurrence mapping.
    interval = build_source_authority([extract_interval(tolerance=2)]).intervals[0]

    assert interval.posting_date_tolerance_days == 2
    assert interval.evidence.stable_id_support is False
    assert interval.evidence.replay_stable_ids is True
    assert interval.evidence.document()["postingDateToleranceDays"] == 2


def test_provider_stable_identifiers_alone_still_admit_a_declared_window():
    record = qfx_interval(tolerance=2)
    record["replay_stable_ids"] = False

    interval = build_source_authority([record]).intervals[0]

    assert interval.posting_date_tolerance_days == 2


def test_interval_report_exposes_the_declared_window():
    resolution = resolve(
        [qfx_row(), monarch_row()], [qfx_interval(), monarch_interval()]
    )
    document = resolution.source_authority_document()
    windows = {
        item["sourceFamily"]: item["postingDateToleranceDays"]
        for item in document["intervals"]
    }
    assert windows == {"qfx": 2, "monarch": 0}


# ---------------------------------------------------------------------------
# A replay-stable synthetic CSV window
# ---------------------------------------------------------------------------


def test_a_synthetic_csv_window_suppresses_legacy_monarch_one_day_apart():
    resolution = resolve(
        [
            extract_row(),
            monarch_row(day=EXTRACT_MONARCH_DAY),
        ],
        [extract_interval(), monarch_interval()],
    )

    planned = suppressions(resolution)
    assert len(planned) == 1
    assert features(planned[0])["authoritativeSourceFamily"] == "extract"
    assert features(planned[0])["suppressedSourceFamily"] == "monarch"
    assert features(planned[0])["sourceDayDistanceDays"] == "1"
    assert features(planned[0])["postingDateToleranceDays"] == "1"
    assert unresolved_codes(resolution) == []
    assert len(resolution.canonical_events) == 1


def test_the_synthetic_csv_row_is_the_one_that_survives():
    resolution = resolve(
        [extract_row(), monarch_row(day=EXTRACT_MONARCH_DAY)],
        [extract_interval(), monarch_interval()],
    )
    event = resolution.canonical_events[0]

    assert event.source_day.isoformat() == EXTRACT_DAY
    assert set(resolution.observation_to_canonical) == {"OBS-EXTRACT", "OBS-MONARCH"}


def test_a_synthetic_csv_window_still_needs_a_declared_tolerance():
    resolution = resolve(
        [extract_row(), monarch_row(day=EXTRACT_MONARCH_DAY)],
        [extract_interval(tolerance=0), monarch_interval()],
    )

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_a_synthetic_csv_window_never_reaches_beyond_its_declared_days():
    resolution = resolve(
        [extract_row(), monarch_row(day=MONARCH_DAY)],
        [extract_interval(tolerance=1), monarch_interval()],
    )

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_an_extra_legacy_repeat_across_the_synthetic_window_stays_open():
    # Multiplicity is preserved in both directions: one authoritative occurrence
    # can explain one legacy row, never two.
    resolution = resolve(
        [
            extract_row(),
            monarch_row("OBS-MONARCH-A", day=EXTRACT_MONARCH_DAY),
            monarch_row(
                "OBS-MONARCH-B",
                day=EXTRACT_MONARCH_DAY,
                provider_id="MONARCH-ROW-2",
            ),
        ],
        [extract_interval(count=1), monarch_interval(count=2)],
    )

    assert len(suppressions(resolution)) <= 1
    assert unresolved_codes(resolution)
    assert len(resolution.canonical_events) >= 2


def test_an_opposite_sign_row_is_never_suppressed_across_the_synthetic_window():
    resolution = resolve(
        [
            extract_row(),
            monarch_row(day=EXTRACT_MONARCH_DAY, amount="42.00"),
        ],
        [extract_interval(), monarch_interval()],
    )

    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_the_synthetic_window_decision_is_replay_stable():
    records = [extract_interval(), monarch_interval()]
    rows = [extract_row(), monarch_row(day=EXTRACT_MONARCH_DAY)]

    forward = resolve(rows, records)
    backward = resolve(list(reversed(rows)), records)

    assert forward.generation_hash == backward.generation_hash
    assert forward.canonical_state_hash == backward.canonical_state_hash


# ---------------------------------------------------------------------------
# The extract/Monarch residual this policy exists to resolve
# ---------------------------------------------------------------------------


def test_stable_qfx_suppresses_legacy_monarch_two_days_apart():
    resolution = resolve(
        [qfx_row(), monarch_row()], [qfx_interval(), monarch_interval()]
    )
    assert unresolved_codes(resolution) == []
    decisions = suppressions(resolution)
    assert len(decisions) == 1
    decision = decisions[0]
    vector = features(decision)
    assert vector["sameSourceDay"] == "false"
    assert vector["sourceDayDistanceDays"] == "2"
    assert vector["postingDateToleranceDays"] == "2"
    assert vector["authoritativeSourceDay"] == QFX_DAY
    assert vector["suppressedSourceDay"] == MONARCH_DAY
    assert vector["authoritativeSourceFamily"] == "qfx"
    assert vector["suppressedSourceFamily"] == "monarch"
    assert vector["authoritativeFormatStrength"] == "stable-provider-id"


def test_the_decision_binds_interval_authority_and_source_hashes():
    resolution = resolve(
        [qfx_row(), monarch_row()], [qfx_interval(), monarch_interval()]
    )
    decision = suppressions(resolution)[0]
    authority = build_source_authority([qfx_interval(), monarch_interval()])
    interval_ids = {item.interval_id for item in authority.intervals}
    vector = features(decision)
    assert vector["authoritativeIntervalId"] in interval_ids
    assert vector["suppressedIntervalId"] in interval_ids
    assert vector["authorityPolicyVersion"] == "canonical-source-authority-v3"
    evidence_hashes = {
        item.evidence.evidence_hash for item in authority.intervals
    }
    assert vector["authoritativeEvidenceHash"] in evidence_hashes
    assert vector["suppressedEvidenceHash"] in evidence_hashes
    assert len(decision.source_hashes) == 2
    assert decision.decision_hash


def test_the_proof_records_the_distance_and_the_permitted_maximum():
    resolution = resolve(
        [qfx_row(), monarch_row()], [qfx_interval(), monarch_interval()]
    )
    values = proof(suppressions(resolution)[0])
    assert values["sourceDayDistanceDays"] == 2
    assert values["maxPostingDateToleranceDays"] == 2
    assert values["authoritativeCount"] == 1
    assert values["lowerCount"] == 1
    assert values["occurrenceIndex"] == 0


def test_the_qfx_observation_stays_and_the_monarch_row_becomes_provenance():
    resolution = resolve(
        [qfx_row(), monarch_row(category="Synthetic Category")],
        [qfx_interval(), monarch_interval()],
    )
    # One economic event, not two: the suppressed row is attached as
    # provenance rather than producing a second event.
    assert len(resolution.canonical_events) == 1
    event = resolution.canonical_events[0]
    assert event.member_observation_ids == ("OBS-MONARCH", "OBS-QFX")
    assert event.selected_observation_id == "OBS-QFX"
    assert event.source_day.isoformat() == QFX_DAY


def test_category_enrichment_survives_a_windowed_suppression():
    resolution = resolve(
        [qfx_row(), monarch_row(category="Synthetic Category")],
        [qfx_interval(), monarch_interval()],
    )
    assert suppressions(resolution)
    # The suppressed observation is still carried by the resolution so its
    # category can enrich the surviving event.
    identifiers = {item.observation_id for item in resolution.observations}
    assert {"OBS-QFX", "OBS-MONARCH"} <= identifiers


def test_report_counts_expose_the_windowed_suppression():
    resolution = resolve(
        [qfx_row(), monarch_row()], [qfx_interval(), monarch_interval()]
    )
    counts = resolution.report_document()["counts"]
    assert counts["sourceSuppressedClaims"] == 1
    assert counts["authorityPostingWindowSuppressions"] == 1
    assert counts["authorityAmbiguousGroups"] == 0


# ---------------------------------------------------------------------------
# The window never widens what may be suppressed
# ---------------------------------------------------------------------------


def test_without_a_declared_window_a_two_day_gap_stays_distinct():
    resolution = resolve(
        [qfx_row(), monarch_row()],
        [qfx_interval(tolerance=0), monarch_interval()],
    )
    assert suppressions(resolution) == []
    assert resolution.report_document()["counts"]["authorityPostingWindowSuppressions"] == 0


def test_a_gap_wider_than_the_declared_window_stays_distinct():
    resolution = resolve(
        [qfx_row(), monarch_row(day="2026-01-19")],
        [qfx_interval(tolerance=2), monarch_interval()],
    )
    assert suppressions(resolution) == []


def test_only_the_authoritative_interval_may_open_the_window():
    # The lower source declares a window; the authority does not.
    record = monarch_interval()
    record["posting_date_tolerance_days"] = 2
    resolution = resolve(
        [qfx_row(), monarch_row()], [qfx_interval(tolerance=0), record]
    )
    assert suppressions(resolution) == []


def test_a_day_outside_proven_coverage_is_never_reached():
    resolution = resolve(
        [qfx_row(day="2026-01-31"), monarch_row(day="2026-02-02")],
        [
            qfx_interval(tolerance=2),
            monarch_interval(
                effective_through="2026-02-02",
                requested_through="2026-02-02",
            ),
        ],
    )
    assert suppressions(resolution) == []


def test_opposite_signs_are_never_suppressed_across_the_window():
    resolution = resolve(
        [qfx_row(amount="-42.00"), monarch_row(amount="42.00")],
        [qfx_interval(), monarch_interval()],
    )
    assert suppressions(resolution) == []


def test_a_different_canonical_account_is_never_suppressed():
    resolution = resolve(
        [qfx_row(), monarch_row(account=OTHER_ACCOUNT)],
        [
            qfx_interval(),
            monarch_interval(account=OTHER_ACCOUNT),
        ],
    )
    assert suppressions(resolution) == []


def test_a_non_posted_lower_row_is_never_suppressed_across_the_window():
    pending = observation(
        "OBS-MONARCH",
        family="monarch",
        provider_id="MONARCH-ROW-1",
        amount="-42.00",
        day=MONARCH_DAY,
        status="pending",
    )
    resolution = resolve(
        [qfx_row(), pending], [qfx_interval(), monarch_interval()]
    )
    assert suppressions(resolution) == []


def test_an_unrelated_description_across_the_window_stays_open():
    """A posting window and equal amount do not prove source membership."""

    resolution = resolve(
        [qfx_row(), monarch_row(description="Totally Different Payee")],
        [qfx_interval(), monarch_interval()],
    )
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2
    assert "ambiguous-lower-source-multiplicity" in unresolved_codes(resolution)


# ---------------------------------------------------------------------------
# Multiplicity and competing candidates
# ---------------------------------------------------------------------------


def test_two_authoritative_days_inside_the_window_are_ambiguous_not_suppressed():
    resolution = resolve(
        [
            qfx_row("OBS-QFX-A", day="2026-01-15", provider_id="QFX-FITID-1"),
            qfx_row("OBS-QFX-B", day="2026-01-18", provider_id="QFX-FITID-2"),
            monarch_row(day="2026-01-17"),
        ],
        [qfx_interval(count=2), monarch_interval()],
    )
    assert suppressions(resolution) == []
    assert "ambiguous-authority-posting-window" in unresolved_codes(resolution)
    counts = resolution.report_document()["counts"]
    assert counts["authorityAmbiguousGroups"] >= 1


def test_the_posting_window_ambiguity_records_its_proof():
    resolution = resolve(
        [
            qfx_row("OBS-QFX-A", day="2026-01-15", provider_id="QFX-FITID-1"),
            qfx_row("OBS-QFX-B", day="2026-01-18", provider_id="QFX-FITID-2"),
            monarch_row(day="2026-01-17"),
        ],
        [qfx_interval(count=2), monarch_interval()],
    )
    decision = next(
        item
        for item in resolution.decisions
        if item.rationale_code == "ambiguous-authority-posting-window"
    )
    assert decision.outcome is DecisionOutcome.UNRESOLVED
    values = proof(decision)
    assert values["reachableAuthoritativeDayCount"] == 2
    assert values["maxPostingDateToleranceDays"] == 2
    assert features(decision)["sameSourceDay"] == "false"


def test_equal_repeat_counts_across_the_window_map_one_to_one():
    observations = [
        qfx_row("OBS-QFX-A", provider_id="QFX-FITID-1"),
        qfx_row("OBS-QFX-B", provider_id="QFX-FITID-2"),
        monarch_row("OBS-MONARCH-A", provider_id="MONARCH-ROW-1"),
        monarch_row("OBS-MONARCH-B", provider_id="MONARCH-ROW-2"),
    ]
    resolution = resolve(
        observations, [qfx_interval(count=2), monarch_interval(count=2)]
    )
    decisions = suppressions(resolution)
    assert len(decisions) == 2
    assert sorted(
        features(item)["occurrenceIndex"] for item in decisions
    ) == ["0", "1"]
    assert all(features(item)["sourceDayDistanceDays"] == "2" for item in decisions)


def test_an_extra_legitimate_lower_repeat_leaves_only_the_excess_open():
    observations = [
        qfx_row("OBS-QFX-A", provider_id="QFX-FITID-1"),
        monarch_row("OBS-MONARCH-A", provider_id="MONARCH-ROW-1"),
        monarch_row("OBS-MONARCH-B", provider_id="MONARCH-ROW-2"),
    ]
    resolution = resolve(
        observations, [qfx_interval(count=1), monarch_interval(count=2)]
    )
    assert len(suppressions(resolution)) == 1
    assert "ambiguous-lower-source-multiplicity" in unresolved_codes(resolution)
    # Both legacy rows survive: one explained by the authority, one open.
    observed = {
        observation_id
        for event in resolution.canonical_events
        for observation_id in event.member_observation_ids
    }
    assert observed == {"OBS-QFX-A", "OBS-MONARCH-A", "OBS-MONARCH-B"}
    assert len(resolution.canonical_events) == 2


def test_a_same_day_pair_keeps_its_historical_decision_shape():
    resolution = resolve(
        [qfx_row(day=QFX_DAY), monarch_row(day=QFX_DAY)],
        [qfx_interval(tolerance=2), monarch_interval()],
    )
    decision = suppressions(resolution)[0]
    vector = features(decision)
    assert vector["sameSourceDay"] == "true"
    assert "sourceDayDistanceDays" not in vector
    assert "postingDateToleranceDays" not in vector
    assert "sourceDayDistanceDays" not in proof(decision)


# ---------------------------------------------------------------------------
# Replay stability
# ---------------------------------------------------------------------------


def test_the_windowed_decision_is_order_independent():
    forward = resolve(
        [qfx_row(), monarch_row()], [qfx_interval(), monarch_interval()]
    )
    reverse = resolve(
        [monarch_row(), qfx_row()], [monarch_interval(), qfx_interval()]
    )
    assert [item.decision_hash for item in suppressions(forward)] == [
        item.decision_hash for item in suppressions(reverse)
    ]
    assert forward.generation_hash == reverse.generation_hash


def test_replaying_the_same_inputs_is_byte_stable():
    first = resolve([qfx_row(), monarch_row()], [qfx_interval(), monarch_interval()])
    second = resolve([qfx_row(), monarch_row()], [qfx_interval(), monarch_interval()])
    assert first.generation_hash == second.generation_hash
    assert [item.feature_vector for item in suppressions(first)] == [
        item.feature_vector for item in suppressions(second)
    ]


def test_no_authority_declared_means_no_windowed_suppression_at_all():
    resolution = resolve_identity([qfx_row(), monarch_row()])
    assert suppressions(resolution) == []
    assert (
        resolution.report_document()["counts"]["authorityPostingWindowSuppressions"]
        == 0
    )


# ---------------------------------------------------------------------------
# Production wiring: declared intervals reach the canonical projector
# ---------------------------------------------------------------------------


def _authority_document() -> dict[str, object]:
    return {"coverageIntervals": [qfx_interval(), monarch_interval()]}


def test_project_reads_coverage_intervals_from_the_private_map(tmp_path):
    from importers.lineage_review.canonical import declared_source_authority

    root = tmp_path / "private"
    (root / "identity").mkdir(parents=True)
    (root / "identity" / "source-authority.json").write_bytes(
        json.dumps(_authority_document()).encode("utf-8")
    )

    policy = declared_source_authority(root)

    intervals = policy.source_authority.intervals
    assert len(intervals) == 2
    windows = {
        item.evidence.source_family: item.posting_date_tolerance_days
        for item in intervals
    }
    assert windows == {"qfx": 2, "monarch": 0}


def test_a_missing_authority_map_keeps_the_conservative_default(tmp_path):
    from importers.lineage_review.canonical import (
        DEFAULT_POLICY,
        declared_source_authority,
    )

    policy = declared_source_authority(tmp_path)

    assert policy is DEFAULT_POLICY
    assert policy.source_authority.intervals == ()


def test_an_empty_authority_map_keeps_the_conservative_default(tmp_path):
    from importers.lineage_review.canonical import (
        DEFAULT_POLICY,
        declared_source_authority,
    )

    root = tmp_path / "private"
    (root / "identity").mkdir(parents=True)
    (root / "identity" / "source-authority.json").write_bytes(
        json.dumps({"coverageIntervals": []}).encode("utf-8")
    )

    assert declared_source_authority(root) is DEFAULT_POLICY


def test_an_unreadable_authority_map_blocks_rather_than_defaulting(tmp_path):
    from importers.lineage_review.canonical import (
        ReviewError,
        declared_source_authority,
    )

    root = tmp_path / "private"
    (root / "identity").mkdir(parents=True)
    (root / "identity" / "source-authority.json").write_bytes(b"{not json")

    with pytest.raises(ReviewError, match="source-authority-map-unreadable"):
        declared_source_authority(root)


def test_an_invalid_authority_map_blocks_rather_than_defaulting(tmp_path):
    from importers.lineage_review.canonical import (
        ReviewError,
        declared_source_authority,
    )

    root = tmp_path / "private"
    (root / "identity").mkdir(parents=True)
    document = _authority_document()
    records = document["coverageIntervals"]
    assert isinstance(records, list)
    del records[0]["effective_through"]
    (root / "identity" / "source-authority.json").write_bytes(
        json.dumps(document).encode("utf-8")
    )

    with pytest.raises(ReviewError, match="source-authority-map-invalid"):
        declared_source_authority(root)


def test_a_declared_window_beyond_the_ceiling_blocks_the_projection(tmp_path):
    from importers.lineage_review.canonical import (
        ReviewError,
        declared_source_authority,
    )

    root = tmp_path / "private"
    (root / "identity").mkdir(parents=True)
    (root / "identity" / "source-authority.json").write_bytes(
        json.dumps(
            {
                "coverageIntervals": [
                    qfx_interval(tolerance=MAX_POSTING_DATE_TOLERANCE_DAYS + 1)
                ]
            }
        ).encode("utf-8")
    )

    with pytest.raises(ReviewError, match="source-authority-map-invalid"):
        declared_source_authority(root)
