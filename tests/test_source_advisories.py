"""Provider advisories are evidence, not institution blockers.

A controlled current-source pull can return a provider *advisory* — a warning
about the shape of the request that still answers for every institution — while
no institution is unavailable.  The advisory must be preserved verbatim, must
never be counted as an institution error, must never make a connection stale or
demand a fallback decision, and must never let a genuine actionable error be
downgraded.  Historical actionable errors stay superseded only by later clean
evidence in the same connection scope, and the latest sealed request-window
sidecar is what proves coverage.

Everything here is synthetic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from finance_store.source_admission import (
    CONNECTION_ADVISORY_PATTERNS,
    SnapshotEvidence,
    classify_connection_error,
    evaluate_connection,
    partition_connection_errors,
)
from finance_store.sources import load_source_catalog
from importers.normalized import builder as normalized
from importers.simplefin.pipeline import build_plan

from tests.test_postgres_shadow_sources import NOW, fixture, write_json
from tests.test_source_admission import add_erroring_snapshot, snapshot

# The exact provider advisory observed on a controlled current-source pull.
ADVISORY = (
    "Requested date range exceeds recommended range of 45 days. "
    "In the future, this may be capped."
)
AUTH_REQUIRED = "Connection to Example Bank requires reauthentication"


def advisory_snapshot(sha, day, *, extra=(), **kwargs):
    return snapshot(sha, day, errors=(ADVISORY, *extra), **kwargs)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_the_observed_provider_advisory_classifies_as_advisory():
    assert classify_connection_error(ADVISORY) == "advisory"


def test_an_unfamiliar_provider_message_stays_actionable():
    assert classify_connection_error(AUTH_REQUIRED) == "actionable"
    assert classify_connection_error("Connection to Example Bank failed") == (
        "actionable"
    )
    assert classify_connection_error("") == "actionable"


def test_classification_is_case_insensitive_but_narrow():
    assert classify_connection_error(ADVISORY.upper()) == "advisory"
    # Superficially similar wording that is not the advisory stays actionable,
    # so nothing is downgraded by resemblance.
    assert classify_connection_error("Date range rejected") == "actionable"
    assert classify_connection_error("recommended range unavailable") == (
        "actionable"
    )


def test_partition_preserves_order_and_every_message():
    messages = (AUTH_REQUIRED, ADVISORY, "boom")
    advisories, actionable = partition_connection_errors(messages)
    assert advisories == (ADVISORY,)
    assert actionable == (AUTH_REQUIRED, "boom")
    assert len(advisories) + len(actionable) == len(messages)


def test_the_advisory_patterns_match_the_existing_pipeline_classifier():
    """The loader and the SimpleFIN plan must agree on the same message class."""

    for message in (ADVISORY, AUTH_REQUIRED, "boom", "totally unknown"):
        plan = build_plan([], [message], {})
        pipeline_blocks = any(
            blocker["code"] == "institution-error" for blocker in plan["blockers"]
        )
        assert pipeline_blocks == (
            classify_connection_error(message) == "actionable"
        ), message


def test_advisory_patterns_are_declared_narrowly():
    assert CONNECTION_ADVISORY_PATTERNS == ("exceeds recommended range",)


# ---------------------------------------------------------------------------
# Evidence surface
# ---------------------------------------------------------------------------


def test_advisory_only_evidence_is_clean():
    evidence = advisory_snapshot("sha-advisory", 3)
    assert evidence.clean is True
    assert evidence.advisories == (ADVISORY,)
    assert evidence.actionable_errors == ()


def test_evidence_with_a_real_error_is_not_clean_even_alongside_an_advisory():
    evidence = advisory_snapshot("sha-mixed", 3, extra=(AUTH_REQUIRED,))
    assert evidence.clean is False
    assert evidence.advisories == (ADVISORY,)
    assert evidence.actionable_errors == (AUTH_REQUIRED,)


def test_the_error_hash_still_covers_every_message():
    """Durable fallback decisions bind the full error hash; history is intact."""

    mixed = advisory_snapshot("sha-mixed", 3, extra=(AUTH_REQUIRED,))
    without = snapshot("sha-mixed", 3, errors=(AUTH_REQUIRED,))
    assert mixed.error_hash != without.error_hash
    assert mixed.actionable_error_hash == without.actionable_error_hash
    assert mixed.advisory_hash != without.advisory_hash


def test_evidence_documents_keep_their_hash_when_no_advisory_exists():
    clean = snapshot("sha-clean", 3)
    assert clean.document() == {
        "connectionId": "default",
        "snapshotSha256": "sha-clean",
        "observedAt": "2026-09-03T12:00:00+00:00",
        "errorCount": 0,
        "errorHash": clean.error_hash,
        "requestedStart": "2026-08-01",
        "requestedEnd": "2026-09-01",
    }


def test_an_advisory_is_reported_in_the_evidence_document():
    document = advisory_snapshot("sha-advisory", 3).document()
    assert document["errorCount"] == 1
    assert document["advisoryCount"] == 1
    assert document["actionableErrorCount"] == 0
    assert document["advisoryHash"]


# ---------------------------------------------------------------------------
# Connection admission
# ---------------------------------------------------------------------------


def test_an_advisory_only_pull_is_fresh_and_unblocked():
    admission = evaluate_connection(
        [snapshot("sha-old", 1), advisory_snapshot("sha-new", 3)],
        None,
        as_of=NOW,
    )
    assert admission.blocker is None
    assert admission.admitted_snapshot_sha256 == "sha-new"
    assert admission.fresh is True
    assert admission.stale is False
    assert admission.staleness_days == 0
    assert admission.current_errors == ()
    assert admission.advisories == (ADVISORY,)
    assert admission.decision is None


def test_an_advisory_only_pull_supersedes_a_historical_actionable_error():
    """2026-09-01 auth-required, superseded by clean 09-02 and 09-03."""

    admission = evaluate_connection(
        [
            snapshot("sha-0901", 1, errors=(AUTH_REQUIRED,)),
            snapshot("sha-0902", 2),
            advisory_snapshot("sha-0903", 3),
        ],
        None,
        as_of=NOW,
    )
    assert admission.blocker is None
    assert admission.admitted_snapshot_sha256 == "sha-0903"
    assert admission.superseded_error_snapshots == ("sha-0901",)
    assert admission.gap == "simplefin-historical-connection-error-superseded"
    # The superseded error is still in the record, not erased.
    assert [item["errorCount"] for item in admission.proof["snapshots"]] == [
        1,
        0,
        1,
    ]
    assert admission.proof["latestAdvisoryCount"] == 1
    assert admission.proof["latestActionableErrorCount"] == 0


def test_a_historical_advisory_is_never_reported_as_a_superseded_error():
    admission = evaluate_connection(
        [advisory_snapshot("sha-old", 1), snapshot("sha-new", 3)],
        None,
        as_of=NOW,
    )
    assert admission.superseded_error_snapshots == ()
    assert admission.gap is None


def test_a_real_latest_error_still_blocks_even_with_an_advisory_present():
    admission = evaluate_connection(
        [
            snapshot("sha-clean", 1),
            advisory_snapshot("sha-bad", 3, extra=(AUTH_REQUIRED,)),
        ],
        None,
        as_of=NOW,
    )
    assert admission.blocker == "simplefin-connection-error-undecided"
    assert admission.admitted is False
    assert admission.stale is True
    # The blocker counts only the actionable error, never the advisory.
    assert admission.current_errors == (AUTH_REQUIRED,)
    assert admission.advisories == (ADVISORY,)
    assert admission.proof["currentErrorCount"] == 2
    assert admission.proof["currentActionableErrorCount"] == 1


def test_a_later_actionable_error_is_not_superseded_by_an_earlier_clean_pull():
    """Supersession only ever runs forward in time."""

    admission = evaluate_connection(
        [advisory_snapshot("sha-0902", 2), snapshot("sha-0903", 3, errors=("boom",))],
        None,
        as_of=NOW,
    )
    assert admission.blocker == "simplefin-connection-error-undecided"
    assert admission.admitted is False


def test_an_advisory_only_history_with_no_clean_snapshot_still_admits():
    admission = evaluate_connection(
        [advisory_snapshot("sha-a", 1), advisory_snapshot("sha-b", 3)],
        None,
        as_of=NOW,
    )
    assert admission.blocker is None
    assert admission.admitted_snapshot_sha256 == "sha-b"
    assert admission.superseded_error_snapshots == ()


def test_an_advisory_only_snapshot_may_serve_as_fallback_evidence():
    latest = snapshot("sha-bad", 3, errors=(AUTH_REQUIRED,))
    prior = advisory_snapshot("sha-prior", 1)
    from tests.test_source_admission import fallback_decision

    admission = evaluate_connection(
        [prior, latest],
        fallback_decision(
            currentErrorHash=latest.error_hash,
            fallbackSnapshotSha256="sha-prior",
            maxStalenessDays=3650,
        ),
        as_of=NOW,
    )
    assert admission.blocker is None
    assert admission.admitted_snapshot_sha256 == "sha-prior"
    assert admission.stale is True


def test_advisories_never_blend_across_connection_scopes():
    with pytest.raises(Exception):
        evaluate_connection(
            [
                advisory_snapshot("sha-a", 1, connection="alpha"),
                snapshot("sha-b", 3, connection="beta"),
            ],
            None,
            as_of=NOW,
        )


# ---------------------------------------------------------------------------
# Request-window sidecar coverage
# ---------------------------------------------------------------------------


def test_the_admitted_window_is_the_latest_sealed_sidecar():
    admission = evaluate_connection(
        [
            snapshot("sha-old", 1, start=date(2026, 8, 20), end=date(2026, 9, 1)),
            advisory_snapshot(
                "sha-new", 3, start=date(2026, 6, 5), end=date(2026, 9, 3)
            ),
        ],
        None,
        as_of=NOW,
    )
    assert admission.admitted_requested_start == date(2026, 6, 5)
    assert admission.admitted_requested_end == date(2026, 9, 3)
    assert (
        admission.admitted_requested_end - admission.admitted_requested_start
    ).days == 90


def test_a_stale_fallback_reports_the_fallback_window_not_the_latest():
    latest = advisory_snapshot(
        "sha-bad",
        3,
        extra=(AUTH_REQUIRED,),
        start=date(2026, 6, 5),
        end=date(2026, 9, 3),
    )
    from tests.test_source_admission import fallback_decision

    admission = evaluate_connection(
        [snapshot("sha-clean", 1), latest],
        fallback_decision(
            currentErrorHash=latest.error_hash, maxStalenessDays=3650
        ),
        as_of=NOW,
    )
    assert admission.blocker is None
    assert admission.admitted_requested_start == date(2026, 8, 1)
    assert admission.admitted_requested_end == date(2026, 9, 1)


def test_a_missing_sidecar_leaves_the_admitted_window_unknown():
    admission = evaluate_connection(
        [advisory_snapshot("sha-new", 3, start=None, end=None)], None, as_of=NOW
    )
    assert admission.admitted_requested_start is None
    assert admission.admitted_requested_end is None


# ---------------------------------------------------------------------------
# Catalog loader
# ---------------------------------------------------------------------------


def add_advisory_snapshot(root: Path, *, day: str, stem: str, extra=()) -> Path:
    return add_erroring_snapshot(
        root, day=day, stem=stem, errors=[ADVISORY, *extra]
    )


def admission_records(catalog):
    return [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "simplefin-connection-admission"
    ]


def global_record(catalog):
    """The admission for the global scope.

    An advisory names no institution, and an errors-only snapshot carries no
    organization at all, so both land in the global scope rather than being
    attributed to whichever institution happened to answer.
    """

    (record,) = [
        item
        for item in admission_records(catalog)
        if item.payload["connectionId"] == "default"
    ]
    return record


def test_catalog_does_not_block_on_an_advisory_only_latest(tmp_path):
    root = fixture(tmp_path)
    add_advisory_snapshot(root, day="2026-09-30", stem="090000-000001")
    catalog = load_source_catalog(root, generated_at=NOW)
    assert not [
        blocker for blocker in catalog.blockers if blocker.startswith("simplefin-")
    ]
    assert "simplefin-connection-advisory-count:1" in catalog.gaps


def test_catalog_reports_the_advisory_and_the_admitted_window(tmp_path):
    root = fixture(tmp_path)
    add_advisory_snapshot(root, day="2026-09-30", stem="090000-000001")
    catalog = load_source_catalog(root, generated_at=NOW)
    record = global_record(catalog)
    assert record.payload["advisoryCount"] == 1
    assert record.payload["currentErrorCount"] == 0
    assert record.payload["blocker"] is None
    assert record.payload["admittedRequestedStart"] == "2026-08-01"
    assert record.payload["admittedRequestedEnd"] == "2026-09-01"
    assert record.payload["proof"]["latestAdvisoryHash"]


def test_catalog_supersedes_a_historical_error_with_an_advisory_only_latest(
    tmp_path,
):
    root = fixture(tmp_path)
    add_erroring_snapshot(
        root, day="2026-09-20", stem="090000-000001", errors=[AUTH_REQUIRED]
    )
    add_advisory_snapshot(root, day="2026-09-30", stem="090000-000002")
    catalog = load_source_catalog(root, generated_at=NOW)
    assert not [
        blocker for blocker in catalog.blockers if blocker.startswith("simplefin-")
    ]
    assert "simplefin-historical-connection-error-superseded" in catalog.gaps
    assert "simplefin-connection-advisory-count:1" in catalog.gaps
    record = global_record(catalog)
    assert record.payload["supersededErrorSnapshotCount"] == 1
    assert [item["errorCount"] for item in record.payload["proof"]["snapshots"]][
        -1
    ] == 1


def test_catalog_still_blocks_when_the_latest_has_a_real_error(tmp_path):
    root = fixture(tmp_path)
    add_advisory_snapshot(
        root, day="2026-09-30", stem="090000-000001", extra=[AUTH_REQUIRED]
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "simplefin-connection-error-undecided" in catalog.blockers
    # Only the actionable error is counted.
    assert "simplefin-institution-error-count:1" in catalog.blockers


def test_catalog_reports_no_advisory_gap_when_there_is_no_advisory(tmp_path):
    root = fixture(tmp_path)
    catalog = load_source_catalog(root, generated_at=NOW)
    assert not [
        gap
        for gap in catalog.gaps
        if gap.startswith("simplefin-connection-advisory-count")
    ]


def test_catalog_accepts_the_sealed_ninety_day_sidecar_for_coverage(tmp_path):
    root = fixture(tmp_path)
    path = add_advisory_snapshot(root, day="2026-09-30", stem="090000-000001")
    sidecar = path.with_name("request-090000-000001.json")
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    metadata["requestedStart"] = "2026-07-02"
    metadata["requestedEnd"] = "2026-09-30"
    write_json(sidecar, metadata)
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "simplefin-overlap-window-missing" not in catalog.gaps
    record = global_record(catalog)
    assert record.payload["admittedRequestedStart"] == "2026-07-02"
    assert record.payload["admittedRequestedEnd"] == "2026-09-30"
    windows = [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "simplefin-request-window"
    ]
    assert any(
        artifact.payload.get("requestedStart") == "2026-07-02"
        for artifact in windows
    )


# ---------------------------------------------------------------------------
# Normalized builder
# ---------------------------------------------------------------------------


def mapping_document(root: Path):
    return json.loads(
        (root / "simplefin" / "account-map.json").read_text(encoding="utf-8")
    )


def candidates_for(root: Path):
    return sorted((root / "raw" / "simplefin").rglob("simplefin-*.json"))


def test_builder_reads_an_advisory_only_latest_snapshot(tmp_path):
    root = fixture(tmp_path)
    latest = add_advisory_snapshot(root, day="2026-09-30", stem="090000-000001")
    admitted = normalized._admitted_simplefin_snapshots(
        candidates_for(root), mapping_document(root)
    )
    selected = {scope: path for scope, path, *_ in admitted}
    assert selected["default"] == latest


def test_builder_still_refuses_an_actionable_latest_error(tmp_path):
    root = fixture(tmp_path)
    add_advisory_snapshot(
        root, day="2026-09-30", stem="090000-000001", extra=[AUTH_REQUIRED]
    )
    with pytest.raises(normalized.BuildError, match="not admitted"):
        normalized._admitted_simplefin_snapshots(
            candidates_for(root), mapping_document(root)
        )


def test_builder_error_message_names_only_the_actionable_error(tmp_path):
    root = fixture(tmp_path)
    add_advisory_snapshot(
        root, day="2026-09-30", stem="090000-000001", extra=[AUTH_REQUIRED]
    )
    with pytest.raises(normalized.BuildError) as excinfo:
        normalized._admitted_simplefin_snapshots(
            candidates_for(root), mapping_document(root)
        )
    assert AUTH_REQUIRED in str(excinfo.value)
    assert "exceeds recommended range" not in str(excinfo.value)


def test_builder_load_accepts_an_advisory_only_snapshot(tmp_path):
    """The second, redundant any-error gate must also be advisory-tolerant."""

    root = fixture(tmp_path)
    source = json.loads(
        candidates_for(root)[-1].read_text(encoding="utf-8")
    )
    latest = write_json(
        root / "raw" / "simplefin" / "2026-09-30" / "simplefin-090000-000001.json",
        {**source, "errors": [ADVISORY]},
    )
    write_json(
        latest.with_name("request-090000-000001.json"),
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "snapshotSha256": hashlib.sha256(latest.read_bytes()).hexdigest(),
            "requestedStart": "2026-07-02",
            "requestedEnd": "2026-09-30",
            "pendingIncluded": True,
        },
    )
    estate, identity, _facts = normalized._load_fact_rows(root)
    normalized._load_simplefin(root, estate, identity)
    assert latest in estate.source_files


def test_builder_load_still_refuses_an_actionable_latest_error(tmp_path):
    root = fixture(tmp_path)
    source = json.loads(candidates_for(root)[-1].read_text(encoding="utf-8"))
    latest = write_json(
        root / "raw" / "simplefin" / "2026-09-30" / "simplefin-090000-000001.json",
        {**source, "errors": [ADVISORY, AUTH_REQUIRED]},
    )
    write_json(
        latest.with_name("request-090000-000001.json"),
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "snapshotSha256": hashlib.sha256(latest.read_bytes()).hexdigest(),
            "requestedStart": "2026-07-02",
            "requestedEnd": "2026-09-30",
            "pendingIncluded": True,
        },
    )
    estate, identity, _facts = normalized._load_fact_rows(root)
    with pytest.raises(normalized.BuildError, match="not admitted"):
        normalized._load_simplefin(root, estate, identity)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_admission_is_stable_under_snapshot_ordering():
    evidence = [
        snapshot("sha-0901", 1, errors=(AUTH_REQUIRED,)),
        snapshot("sha-0902", 2),
        advisory_snapshot("sha-0903", 3),
    ]
    first = evaluate_connection(evidence, None, as_of=NOW)
    second = evaluate_connection(list(reversed(evidence)), None, as_of=NOW)
    assert first == second


def test_admission_is_stable_across_repeated_evaluation():
    evidence = [advisory_snapshot("sha-new", 3)]
    assert evaluate_connection(evidence, None, as_of=NOW) == evaluate_connection(
        evidence, None, as_of=NOW
    )


def test_advisory_classification_does_not_depend_on_message_order():
    forward = SnapshotEvidence(
        connection_id="default",
        snapshot_sha256="sha",
        observed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        errors=(ADVISORY, AUTH_REQUIRED),
        requested_start=None,
        requested_end=None,
    )
    backward = SnapshotEvidence(
        connection_id="default",
        snapshot_sha256="sha",
        observed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        errors=(AUTH_REQUIRED, ADVISORY),
        requested_start=None,
        requested_end=None,
    )
    assert forward.clean == backward.clean is False
    assert forward.error_hash == backward.error_hash
    assert forward.actionable_errors == backward.actionable_errors
