"""Synthetic regressions for durable source-admission decisions.

Two admission surfaces are covered: Monarch balance-only entities that carry no
transactions, and SimpleFIN connection-scoped freshness when older immutable
snapshots recorded institution errors.  Every fixture here is invented.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest

from finance_store.source_admission import (
    AdmissionError,
    MonarchObservedEntity,
    SnapshotEvidence,
    balance_point,
    connection_id_for,
    evaluate_connection,
    evaluate_connection_scopes,
    evaluate_monarch_entity,
    parse_connection_decisions,
    parse_monarch_account_map,
    organization_scope_id,
    snapshot_scope_for_paths,
)
from finance_store.sources import (
    SourceLoadError,
    _fact_accounts,
    _simplefin_batches,
    canonical_entity_evidence,
    load_source_catalog,
    monarch_observed_entities,
)
from importers.normalized import builder as normalized

from tests.test_postgres_shadow_sources import NOW, fixture, write_json


TARGETS = ("acct-synthetic", "acct-vehicle", "acct-loan")

# The shared fixture snapshot holds one institution, so admission scopes it to
# that organization rather than to a single file-wide connection.
FIXTURE_SCOPE = organization_scope_id({"org": {"domain": "example.invalid"}}, "1")


def decisions_for(document):
    return parse_monarch_account_map(document)


def verdict(observed, entry, *, targets=TARGETS, target_evidence=None, bind=True):
    if bind and isinstance(entry, Mapping) and "observedEvidenceHash" not in entry:
        entry = {**entry, "observedEvidenceHash": observed.evidence_hash}
    document = {observed.source_account: entry} if entry is not None else {}
    parsed = decisions_for(document)
    return evaluate_monarch_entity(
        observed,
        parsed.get(observed.source_account),
        known_targets=targets,
        target_evidence=target_evidence,
    )


def balance_only(
    name="Synthetic Vehicle",
    *,
    balances=1400,
    cutoff=None,
    needs_review=False,
    values=None,
    source_hashes=("sha-balances", "sha-transactions"),
):
    if values is None:
        values = [
            balance_point(date(2026, 1, 1) + timedelta(days=index), "15000.00")
            for index in range(balances)
        ]
    return MonarchObservedEntity(
        source_account=name,
        transaction_count=0,
        balance_count=balances,
        trust_cutoff_day=cutoff,
        needs_review=needs_review,
        account_type="VEHICLE",
        balance_points=tuple(sorted(values)),
        source_hashes=tuple(source_hashes),
    )


def complete_observe(**overrides):
    entry = {
        "action": "observe",
        "canonicalAccountId": "acct-vehicle",
        "decision": "Vehicle tracked as an alternative asset; balances only.",
        "decidedAt": "2026-09-01",
        "entityKind": "VEHICLE",
        "observedCounts": {"transactions": 0, "balances": 1400},
        "trustCutoffUnknown": True,
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# Monarch: parsing
# ---------------------------------------------------------------------------


def test_legacy_string_map_still_parses_as_an_import_decision():
    parsed = decisions_for({"Synthetic Checking": "acct-synthetic"})
    entity = parsed["Synthetic Checking"]
    assert entity.legacy_string_form is True
    assert entity.action == "import"
    assert entity.target == "acct-synthetic"


def test_object_and_string_entries_coexist_in_one_map():
    parsed = decisions_for(
        {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Vehicle": complete_observe(),
        }
    )
    assert parsed["Synthetic Checking"].legacy_string_form is True
    assert parsed["Synthetic Vehicle"].legacy_string_form is False
    assert parsed["Synthetic Vehicle"].action == "observe"


@pytest.mark.parametrize("document", [[], "text", 5, None])
def test_non_mapping_documents_are_structural_failures(document):
    with pytest.raises(AdmissionError):
        decisions_for(document)


def test_entry_that_is_neither_string_nor_object_is_refused():
    with pytest.raises(AdmissionError):
        decisions_for({"Synthetic Vehicle": ["acct-vehicle"]})


def test_decision_id_is_stable_and_order_independent():
    first = decisions_for({"A": complete_observe()})["A"]
    second = decisions_for(
        {"A": dict(reversed(list(complete_observe().items())))}
    )["A"]
    assert first.decision_id == second.decision_id


# ---------------------------------------------------------------------------
# Monarch: balance-only admission
# ---------------------------------------------------------------------------


def test_unmapped_balance_only_entity_is_blocked_and_never_dropped():
    result = verdict(balance_only(), None)
    assert result.admitted is False
    assert result.blocker == "monarch-entity-decision-missing"
    assert result.preserve_balances is True
    assert result.proof["balanceOnly"] is True
    assert result.proof["observedBalanceCount"] == 1400


def test_complete_observe_decision_admits_a_balance_only_vehicle():
    result = verdict(balance_only(), complete_observe())
    assert result.admitted is True
    assert result.blocker is None
    assert result.canonicalize_transactions is False
    assert result.preserve_balances is True
    assert result.gap == "monarch-observe-observed-not-canonicalized"


def test_complete_exclusion_needs_no_target_but_needs_a_rationale():
    admitted = verdict(
        balance_only(),
        {
            "action": "exclude",
            "decision": "Vehicle sold in 2024; balances retained as history.",
            "decidedAt": "2026-09-01",
            "entityKind": "VEHICLE",
            "observedCounts": {"transactions": 0, "balances": 1400},
            "trustCutoffUnknown": True,
            "balanceInvariant": {"allBalancesZero": False},
        },
    )
    assert admitted.admitted is True
    assert admitted.gap == "monarch-exclude-observed-not-canonicalized"

    refused = verdict(
        balance_only(),
        {
            "action": "exclude",
            "decidedAt": "2026-09-01",
            "entityKind": "VEHICLE",
            "observedCounts": {"transactions": 0, "balances": 1400},
            "trustCutoffUnknown": True,
            "balanceInvariant": {"allBalancesZero": False},
        },
    )
    assert refused.blocker == "monarch-entity-rationale-missing"


def test_exclusion_must_declare_a_balance_invariant_it_can_prove():
    base = {
        "action": "exclude",
        "decision": "Zeroed profile retired; balances retained as history.",
        "decidedAt": "2026-09-01",
        "entityKind": "VEHICLE",
        "observedCounts": {"transactions": 0, "balances": 3},
        "trustCutoffUnknown": True,
    }
    zeroed = balance_only(
        balances=3,
        values=[
            balance_point(date(2026, 1, 1), "0.00"),
            balance_point(date(2026, 2, 1), "0"),
            balance_point(date(2026, 3, 1), "0.0000"),
        ],
    )
    assert verdict(zeroed, base).blocker == "monarch-entity-balance-invariant-missing"
    proven = verdict(zeroed, {**base, "balanceInvariant": {"allBalancesZero": True}})
    assert proven.admitted is True
    assert proven.proof["allBalancesZero"] is True

    live = balance_only(
        balances=3,
        values=[
            balance_point(date(2026, 1, 1), "0.00"),
            balance_point(date(2026, 2, 1), "0.00"),
            balance_point(date(2026, 3, 1), "12.34"),
        ],
    )
    disproven = verdict(live, {**base, "balanceInvariant": {"allBalancesZero": True}})
    assert disproven.blocker == "monarch-entity-balance-invariant-unproven"
    assert disproven.preserve_balances is True


def test_rewritten_values_invalidate_a_decision_that_kept_its_counts():
    entry = complete_observe(
        observedCounts={"transactions": 0, "balances": 3},
        trustCutoffDay="2023-10-03",
        trustCutoffUnknown=False,
    )
    original = balance_only(
        balances=3,
        cutoff=date(2023, 10, 3),
        values=[
            balance_point(date(2026, 1, 1), "15000.00"),
            balance_point(date(2026, 2, 1), "14500.00"),
            balance_point(date(2026, 3, 1), "14000.00"),
        ],
    )
    bound = {**entry, "observedEvidenceHash": original.evidence_hash}
    assert verdict(original, bound, bind=False).admitted is True

    # Same count, same cutoff, same review state; one value silently restated.
    restated = balance_only(
        balances=3,
        cutoff=date(2023, 10, 3),
        values=[
            balance_point(date(2026, 1, 1), "15000.00"),
            balance_point(date(2026, 2, 1), "14500.00"),
            balance_point(date(2026, 3, 1), "9000.00"),
        ],
    )
    stale = verdict(restated, bound, bind=False)
    assert stale.admitted is False
    assert stale.blocker == "monarch-entity-observed-evidence-unreconciled"
    assert stale.preserve_balances is True


def test_a_rewritten_source_file_invalidates_the_same_values():
    entry = complete_observe(observedCounts={"transactions": 0, "balances": 2})
    values = [
        balance_point(date(2026, 1, 1), "15000.00"),
        balance_point(date(2026, 2, 1), "14500.00"),
    ]
    original = balance_only(balances=2, values=values, source_hashes=("sha-a",))
    bound = {**entry, "observedEvidenceHash": original.evidence_hash}
    assert verdict(original, bound, bind=False).admitted is True
    reissued = balance_only(balances=2, values=values, source_hashes=("sha-b",))
    assert (
        verdict(reissued, bound, bind=False).blocker
        == "monarch-entity-observed-evidence-unreconciled"
    )


def test_an_object_decision_without_an_evidence_hash_is_refused():
    refused = verdict(balance_only(), complete_observe(), bind=False)
    assert refused.blocker == "monarch-entity-observed-evidence-hash-missing"
    assert refused.preserve_balances is True


def test_evidence_hash_never_leaks_the_values_it_binds():
    observed = balance_only(
        balances=2,
        values=[
            balance_point(date(2026, 1, 1), "15000.00"),
            balance_point(date(2026, 2, 1), "14500.00"),
        ],
    )
    result = verdict(observed, complete_observe(
        observedCounts={"transactions": 0, "balances": 2}
    ))
    published = json.dumps(result.proof, sort_keys=True)
    assert "15000.00" not in published
    assert "Synthetic Vehicle" not in published
    assert result.proof["observedEvidenceHash"] == observed.evidence_hash


def test_alternative_entity_redirects_balances_to_an_existing_fact():
    result = verdict(
        balance_only(),
        complete_observe(
            action="alternative-entity",
            canonicalAccountId="acct-loan",
            entityKind="ACCOUNT",
            targetEvidenceHash="fact-hash-loan",
        ),
        target_evidence={"acct-loan": "fact-hash-loan"},
    )
    assert result.admitted is True
    assert result.decision.target == "acct-loan"
    assert result.canonicalize_transactions is False


def test_alternative_entity_must_bind_the_target_fact_it_was_made_against():
    entry = complete_observe(
        action="alternative-entity",
        canonicalAccountId="acct-loan",
        entityKind="ACCOUNT",
    )
    unbound = verdict(balance_only(), entry)
    assert unbound.blocker == "monarch-entity-target-evidence-hash-missing"

    bound = {**entry, "targetEvidenceHash": "fact-hash-loan"}
    assert (
        verdict(balance_only(), bound).blocker
        == "monarch-entity-target-evidence-unavailable"
    )

    # The target fact was edited after the decision was written.
    changed = verdict(
        balance_only(),
        bound,
        target_evidence={"acct-loan": "fact-hash-loan-restated"},
    )
    assert changed.blocker == "monarch-entity-target-evidence-unreconciled"
    assert changed.preserve_balances is True


def test_alternative_entity_may_target_a_non_account_canonical_entity():
    kinds = {
        "acct-synthetic": "account",
        "loan:Synthetic Mortgage": "loan",
        "vehicle:Synthetic Vehicle": "vehicle",
    }
    observed = balance_only(name="Synthetic Mortgage", balances=378)
    admitted = verdict(
        observed,
        complete_observe(
            action="alternative-entity",
            canonicalAccountId="loan:Synthetic Mortgage",
            entityKind="LOAN",
            observedCounts={"transactions": 0, "balances": 378},
            targetEvidenceHash="fact-hash-mortgage",
        ),
        targets=kinds,
        target_evidence={"loan:Synthetic Mortgage": "fact-hash-mortgage"},
    )
    assert admitted.admitted is True
    assert admitted.blocker is None
    assert admitted.decision.target == "loan:Synthetic Mortgage"
    assert admitted.proof["targetEntityKind"] == "loan"
    assert admitted.proof["targetEvidenceHash"] == "fact-hash-mortgage"
    assert admitted.canonicalize_transactions is False
    assert admitted.preserve_balances is True


def test_alternative_entity_refuses_a_target_of_a_different_kind():
    observed = balance_only(name="Synthetic Mortgage", balances=378)
    refused = verdict(
        observed,
        complete_observe(
            action="alternative-entity",
            canonicalAccountId="vehicle:Synthetic Vehicle",
            entityKind="LOAN",
            observedCounts={"transactions": 0, "balances": 378},
            targetEvidenceHash="fact-hash-vehicle",
        ),
        targets={"vehicle:Synthetic Vehicle": "vehicle"},
        target_evidence={"vehicle:Synthetic Vehicle": "fact-hash-vehicle"},
    )
    assert refused.admitted is False
    assert refused.blocker == "monarch-entity-kind-target-mismatch"
    assert refused.preserve_balances is True


def test_import_refuses_a_valuation_only_entity_as_its_ledger():
    refused = evaluate_monarch_entity(
        MonarchObservedEntity(
            source_account="Synthetic Checking",
            transaction_count=12,
            balance_count=0,
            trust_cutoff_day=None,
            needs_review=False,
        ),
        decisions_for({"Synthetic Checking": "vehicle:Synthetic Vehicle"})[
            "Synthetic Checking"
        ],
        known_targets={"vehicle:Synthetic Vehicle": "vehicle"},
    )
    assert refused.admitted is False
    assert refused.blocker == "monarch-entity-target-not-transaction-capable"


def test_unknown_targets_stay_unresolved_whatever_their_shape():
    observed = balance_only(name="Synthetic Mortgage", balances=378)
    refused = evaluate_monarch_entity(
        observed,
        decisions_for(
            {
                observed.source_account: complete_observe(
                    action="alternative-entity",
                    canonicalAccountId="loan:Absent Mortgage",
                    entityKind="LOAN",
                    observedCounts={"transactions": 0, "balances": 378},
                )
            }
        )[observed.source_account],
        known_targets={"loan:Synthetic Mortgage": "loan"},
    )
    assert refused.blocker == "monarch-entity-target-unresolved"


def test_bare_target_sets_still_admit_without_kind_checks():
    observed = balance_only()
    result = verdict(observed, complete_observe())
    assert result.admitted is True
    assert result.proof["targetEntityKind"] is None


def test_loan_with_a_cutoff_must_state_and_reconcile_that_cutoff():
    observed = balance_only(
        name="Synthetic Loan",
        balances=378,
        cutoff=date(2023, 10, 3),
        needs_review=True,
    )
    base = complete_observe(
        canonicalAccountId="acct-loan",
        entityKind="LOAN",
        observedCounts={"transactions": 0, "balances": 378},
    )

    unstated = dict(base)
    assert verdict(observed, unstated).blocker == (
        "monarch-entity-trust-cutoff-unreconciled"
    )

    wrong = dict(base, trustCutoffUnknown=False, trustCutoffDay="2024-01-01")
    assert verdict(observed, wrong).blocker == (
        "monarch-entity-trust-cutoff-unreconciled"
    )

    unacknowledged = dict(
        base, trustCutoffUnknown=False, trustCutoffDay="2023-10-03"
    )
    assert verdict(observed, unacknowledged).blocker == (
        "monarch-entity-review-unacknowledged"
    )

    complete = dict(unacknowledged, reviewAcknowledged=True)
    assert verdict(observed, complete).admitted is True


def test_trust_cutoff_must_be_stated_exactly_once():
    both = complete_observe(trustCutoffUnknown=True, trustCutoffDay="2023-10-03")
    assert verdict(balance_only(), both).blocker == (
        "monarch-entity-trust-cutoff-unstated"
    )
    neither = complete_observe()
    neither.pop("trustCutoffUnknown")
    assert verdict(balance_only(), neither).blocker == (
        "monarch-entity-trust-cutoff-unstated"
    )


@pytest.mark.parametrize(
    "override, code",
    [
        ({"action": "guess"}, "monarch-entity-action-invalid"),
        ({"canonicalAccountId": ""}, "monarch-entity-target-missing"),
        ({"canonicalAccountId": "acct-missing"}, "monarch-entity-target-unresolved"),
        ({"decision": ""}, "monarch-entity-rationale-missing"),
        ({"decidedAt": ""}, "monarch-entity-decided-at-missing"),
        ({"entityKind": ""}, "monarch-entity-kind-missing"),
    ],
)
def test_incomplete_decisions_keep_the_blocker(override, code):
    assert verdict(balance_only(), complete_observe(**override)).blocker == code


@pytest.mark.parametrize(
    "counts, code",
    [
        ({}, "monarch-entity-declared-counts-missing"),
        ({"transactions": 0}, "monarch-entity-declared-counts-missing"),
        ({"balances": 1400}, "monarch-entity-declared-counts-missing"),
        (
            {"transactions": 2, "balances": 1400},
            "monarch-entity-transaction-count-unreconciled",
        ),
        (
            {"transactions": 0, "balances": 7},
            "monarch-entity-balance-count-unreconciled",
        ),
    ],
)
def test_declared_counts_must_reconcile_with_observation(counts, code):
    assert verdict(balance_only(), complete_observe(observedCounts=counts)).blocker == code


def test_balance_only_action_cannot_be_used_on_a_transaction_entity():
    observed = MonarchObservedEntity(
        source_account="Synthetic Checking",
        transaction_count=3,
        balance_count=1400,
        trust_cutoff_day=None,
        needs_review=False,
    )
    entry = complete_observe(observedCounts={"transactions": 3, "balances": 1400})
    assert verdict(observed, entry).blocker == (
        "monarch-balance-only-action-on-transaction-entity"
    )


def test_import_action_canonicalizes_and_needs_no_entity_kind():
    observed = MonarchObservedEntity(
        source_account="Synthetic Checking",
        transaction_count=1,
        balance_count=1,
        trust_cutoff_day=None,
        needs_review=False,
    )
    result = verdict(
        observed,
        {
            "action": "import",
            "canonicalAccountId": "acct-synthetic",
            "decision": "Primary checking, imported.",
            "decidedAt": "2026-09-01",
            "observedCounts": {"transactions": 1, "balances": 1},
            "trustCutoffUnknown": True,
        },
    )
    assert result.admitted is True
    assert result.canonicalize_transactions is True
    assert result.gap is None


def test_legacy_string_entity_without_transactions_still_works():
    result = verdict(balance_only(), "acct-vehicle")
    assert result.admitted is True
    assert result.blocker is None


# ---------------------------------------------------------------------------
# SimpleFIN: connection-scoped freshness
# ---------------------------------------------------------------------------


def snapshot(
    sha,
    day,
    *,
    errors=(),
    connection="default",
    start=date(2026, 8, 1),
    end=date(2026, 9, 1),
):
    return SnapshotEvidence(
        connection_id=connection,
        snapshot_sha256=sha,
        observed_at=datetime(2026, 9, day, 12, tzinfo=timezone.utc),
        errors=tuple(errors),
        requested_start=start,
        requested_end=end,
    )


def fallback_decision(*, connection="default", **overrides):
    entry = {
        "action": "fallback",
        "decision": "Institution connection is re-authing; prior snapshot stands.",
        "decidedAt": "2026-09-03",
        "currentErrorHash": "",
        "fallbackSnapshotSha256": "sha-clean",
        "fallbackRequestedStart": "2026-08-01",
        "fallbackRequestedEnd": "2026-09-01",
        "maxStalenessDays": 7,
    }
    entry.update(overrides)
    return parse_connection_decisions({connection: entry})[connection]


def test_current_clean_evidence_supersedes_historical_connection_errors():
    admission = evaluate_connection(
        [
            snapshot("sha-old", 1, errors=("Connection to Example Bank failed",)),
            snapshot("sha-mid", 2),
            snapshot("sha-new", 3),
        ],
        None,
        as_of=NOW,
    )
    assert admission.blocker is None
    assert admission.admitted_snapshot_sha256 == "sha-new"
    assert admission.fresh is True
    assert admission.stale is False
    assert admission.gap == "simplefin-historical-connection-error-superseded"
    assert admission.superseded_error_snapshots == ("sha-old",)
    # Error history is preserved in full, never erased.
    assert [item["errorCount"] for item in admission.proof["snapshots"]] == [1, 0, 0]


def test_clean_history_reports_no_supersession_gap():
    admission = evaluate_connection(
        [snapshot("sha-old", 1), snapshot("sha-new", 3)], None, as_of=NOW
    )
    assert admission.gap is None
    assert admission.superseded_error_snapshots == ()


def test_old_clean_snapshot_is_admitted_but_explicitly_stale():
    admission = evaluate_connection(
        [
            SnapshotEvidence(
                connection_id="default",
                snapshot_sha256="sha-old-clean",
                observed_at=datetime(2020, 1, 1, 12, tzinfo=timezone.utc),
                errors=(),
                requested_start=date(2019, 12, 1),
                requested_end=date(2020, 1, 1),
            )
        ],
        None,
        as_of=NOW,
    )

    assert admission.admitted_snapshot_sha256 == "sha-old-clean"
    assert admission.fresh is False
    assert admission.stale is True
    assert admission.staleness_days > 1
    assert admission.gap == "simplefin-connection-latest-clean-stale"
    assert admission.proof["cleanSnapshotMaxAgeDays"] == 1


def test_latest_error_without_a_decision_stays_blocked():
    admission = evaluate_connection(
        [snapshot("sha-clean", 1), snapshot("sha-bad", 3, errors=("boom",))],
        None,
        as_of=NOW,
    )
    assert admission.blocker == "simplefin-connection-error-undecided"
    assert admission.admitted is False
    assert admission.stale is True
    assert admission.current_errors == ("boom",)


def test_latest_error_with_a_complete_decision_admits_prior_evidence():
    latest = snapshot("sha-bad", 3, errors=("boom",))
    admission = evaluate_connection(
        [snapshot("sha-clean", 1), latest],
        fallback_decision(currentErrorHash=latest.error_hash),
        as_of=NOW,
    )
    assert admission.blocker is None
    assert admission.admitted_snapshot_sha256 == "sha-clean"
    assert admission.fresh is False
    assert admission.stale is True
    assert admission.staleness_days == 2
    assert admission.gap == "simplefin-connection-fallback-admitted-stale"
    assert admission.proof["currentErrorHash"] == latest.error_hash
    assert admission.proof["fallbackSnapshotSha256"] == "sha-clean"
    assert admission.proof["fallbackRequestedStart"] == "2026-08-01"
    assert admission.proof["fallbackRequestedEnd"] == "2026-09-01"


@pytest.mark.parametrize(
    "override, code",
    [
        ({"action": "ignore"}, "simplefin-connection-action-invalid"),
        ({"decision": ""}, "simplefin-connection-rationale-missing"),
        ({"decidedAt": ""}, "simplefin-connection-decided-at-missing"),
        ({"currentErrorHash": "wrong"}, "simplefin-connection-current-error-unbound"),
        (
            {"fallbackSnapshotSha256": "sha-other"},
            "simplefin-connection-fallback-snapshot-unbound",
        ),
        (
            {"fallbackRequestedStart": "2026-01-01"},
            "simplefin-connection-fallback-window-unbound",
        ),
        (
            {"fallbackRequestedEnd": "2026-01-01"},
            "simplefin-connection-fallback-window-unbound",
        ),
        (
            {"maxStalenessDays": None},
            "simplefin-connection-staleness-tolerance-missing",
        ),
        ({"maxStalenessDays": 1}, "simplefin-connection-fallback-too-stale"),
    ],
)
def test_incomplete_fallback_decisions_keep_the_blocker(override, code):
    latest = snapshot("sha-bad", 3, errors=("boom",))
    base = {"currentErrorHash": latest.error_hash}
    base.update(override)
    admission = evaluate_connection(
        [snapshot("sha-clean", 1), latest],
        fallback_decision(**base),
        as_of=NOW,
    )
    assert admission.blocker == code
    assert admission.admitted is False


def test_fallback_without_any_prior_clean_evidence_is_blocked():
    latest = snapshot("sha-bad", 3, errors=("boom",))
    admission = evaluate_connection(
        [snapshot("sha-worse", 1, errors=("older boom",)), latest],
        fallback_decision(currentErrorHash=latest.error_hash),
        as_of=NOW,
    )
    assert admission.blocker == "simplefin-connection-fallback-evidence-missing"


def test_accounts_are_never_blended_across_connection_scope():
    with pytest.raises(AdmissionError, match="simplefin-connection-scope-mixed"):
        evaluate_connection(
            [snapshot("sha-a", 1), snapshot("sha-b", 2, connection="other")],
            None,
            as_of=NOW,
        )


def test_empty_connection_evidence_is_refused():
    with pytest.raises(AdmissionError):
        evaluate_connection([], None, as_of=NOW)


def test_snapshot_order_is_stable_regardless_of_input_order():
    items = [
        snapshot("sha-old", 1, errors=("boom",)),
        snapshot("sha-new", 3),
        snapshot("sha-mid", 2),
    ]
    first = evaluate_connection(items, None, as_of=NOW)
    second = evaluate_connection(list(reversed(items)), None, as_of=NOW)
    assert first.proof == second.proof
    assert first.admitted_snapshot_sha256 == second.admitted_snapshot_sha256


def test_error_hash_is_order_independent_but_content_sensitive():
    left = snapshot("sha", 1, errors=("a", "b"))
    right = snapshot("sha", 1, errors=("b", "a"))
    other = snapshot("sha", 1, errors=("a", "c"))
    assert left.error_hash == right.error_hash
    assert left.error_hash != other.error_hash


def test_connection_scope_defaults_when_metadata_omits_it():
    assert connection_id_for(None) == "default"
    assert connection_id_for({}) == "default"
    assert connection_id_for({"connectionId": "  "}) == "default"
    assert connection_id_for({"connectionId": "bank-a"}) == "bank-a"


@pytest.mark.parametrize("document", ["text", [], 3])
def test_invalid_connection_decision_blocks_are_structural_failures(document):
    with pytest.raises(AdmissionError):
        parse_connection_decisions(document)


def test_absent_connection_block_parses_to_no_decisions():
    assert parse_connection_decisions(None) == {}


# ---------------------------------------------------------------------------
# End-to-end through the source catalog
# ---------------------------------------------------------------------------


def add_balance_only_profile(root: Path, name: str = "Synthetic Vehicle") -> None:
    balances = root / "legacy" / "monarch" / "Balances_2026.csv"
    rows = balances.read_text(encoding="utf-8").rstrip("\n").split("\n")
    rows.extend(
        f"2026-0{month}-01,{name},15000.00" for month in range(1, 9)
    )
    balances.write_text("\n".join(rows) + "\n", encoding="utf-8")


def monarch_map(root: Path, document) -> None:
    write_json(root / "normalized" / "monarch-account-map.json", document)


def bind_evidence(root: Path, document: dict) -> dict:
    """Fill each object entry's evidence hashes from what the fixture observes.

    This mirrors how a real durable decision is written: read the observed
    entity, copy its hash, and let the evaluator refuse the moment either the
    values or the target fact move underneath it.
    """

    observed = monarch_observed_entities(root)
    targets = canonical_entity_evidence(root)
    bound: dict = {}
    for source_account, entry in document.items():
        if not isinstance(entry, Mapping):
            bound[source_account] = entry
            continue
        filled = dict(entry)
        filled.setdefault(
            "observedEvidenceHash", observed[source_account].evidence_hash
        )
        target = filled.get("canonicalAccountId")
        if filled.get("action") == "alternative-entity" and target in targets:
            filled.setdefault("targetEvidenceHash", targets[target])
        bound[source_account] = filled
    return bound


def vehicle_fact(root: Path) -> None:
    write_json(
        root / "facts" / "vehicle.json",
        {
            "type": "account",
            "id": "acct-vehicle",
            "institution": "Example Motors",
            "displayName": "Household Vehicle",
            "maskedNumber": None,
            "kind": "ALTERNATIVE",
            "opened": "2020-01-01",
            "closed": None,
            "excluded": False,
            "reason": None,
            "source": "synthetic fixture",
            "sourcePath": None,
            "notes": "synthetic",
        },
    )


def loan_fact(root: Path, name: str = "Synthetic Mortgage") -> None:
    write_json(
        root / "facts" / "residence.json",
        {
            "type": "property",
            "name": "Synthetic Residence",
            "address": "1 Example Way",
            "purchaseDate": "2020-01-15",
            "purchasePrice": "250000.00",
            "saleDate": None,
            "salePrice": None,
            "netProceeds": None,
            "appraisals": [],
            "source": "synthetic fixture",
            "sourcePath": None,
            "notes": "synthetic",
        },
    )
    write_json(
        root / "facts" / "loan.json",
        {
            "type": "loan",
            "name": name,
            "principal": "200000.00",
            "annualRate": "0.0500",
            "termMonths": 360,
            "originationDate": "2020-01-15",
            "firstPayment": "2020-03-01",
            "lender": "Example Community Bank",
            "linkedTo": "Synthetic Residence",
            "payoffAmount": None,
            "payoffDate": None,
            "pmi": False,
            "source": "synthetic fixture",
            "sourcePath": None,
            "notes": "synthetic",
        },
    )


def test_catalog_maps_a_balance_only_profile_onto_an_existing_loan_fact(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root, "Synthetic Mortgage")
    loan_fact(root)
    monarch_map(
        root,
        bind_evidence(root, {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Mortgage": complete_observe(
                action="alternative-entity",
                canonicalAccountId="loan:Synthetic Mortgage",
                entityKind="LOAN",
                decision="Mortgage already carried as a loan fact; balances only.",
                observedCounts={"transactions": 0, "balances": 8},
            ),
        }),
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert not [b for b in catalog.blockers if b.startswith("monarch-")]
    balances = [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "monarch-balance"
        and artifact.payload.get("canonicalAccountId") == "loan:Synthetic Mortgage"
    ]
    assert len(balances) == 8


def test_catalog_refuses_a_loan_decision_pointed_at_the_wrong_kind(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root, "Synthetic Mortgage")
    loan_fact(root)
    vehicle_fact(root)
    monarch_map(
        root,
        bind_evidence(root, {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Mortgage": complete_observe(
                action="alternative-entity",
                canonicalAccountId="acct-vehicle",
                entityKind="LOAN",
                decision="Mistakenly redirected to the vehicle account.",
                observedCounts={"transactions": 0, "balances": 8},
                targetEvidenceHash="unused-once-the-kind-check-fires",
            ),
        }),
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "monarch-entity-kind-target-mismatch" in catalog.blockers


def test_catalog_refuses_a_decision_after_values_move_under_the_same_count(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root)
    vehicle_fact(root)
    monarch_map(
        root,
        bind_evidence(root, {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Vehicle": complete_observe(
                observedCounts={"transactions": 0, "balances": 8}
            ),
        }),
    )
    assert not [
        b for b in load_source_catalog(root, generated_at=NOW).blockers
        if b.startswith("monarch-")
    ]

    # Same eight rows, same dates, same cutoff; one value quietly restated.
    balances = root / "legacy" / "monarch" / "Balances_2026.csv"
    balances.write_text(
        balances.read_text(encoding="utf-8").replace(
            "2026-04-01,Synthetic Vehicle,15000.00",
            "2026-04-01,Synthetic Vehicle,9000.00",
        ),
        encoding="utf-8",
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "monarch-entity-observed-evidence-unreconciled" in catalog.blockers


def test_catalog_refuses_a_decision_after_its_target_fact_changes(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root, "Synthetic Mortgage")
    loan_fact(root)
    monarch_map(
        root,
        bind_evidence(root, {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Mortgage": complete_observe(
                action="alternative-entity",
                canonicalAccountId="loan:Synthetic Mortgage",
                entityKind="LOAN",
                decision="Mortgage already carried as a loan fact; balances only.",
                observedCounts={"transactions": 0, "balances": 8},
            ),
        }),
    )
    assert not [
        b for b in load_source_catalog(root, generated_at=NOW).blockers
        if b.startswith("monarch-")
    ]

    # The loan the decision was made against is restated after the fact.
    loan = root / "facts" / "loan.json"
    document = json.loads(loan.read_text(encoding="utf-8"))
    document["principal"] = "180000.00"
    write_json(loan, document)
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "monarch-entity-target-evidence-unreconciled" in catalog.blockers


def test_catalog_publishes_hashes_and_counts_but_never_the_values(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root)
    vehicle_fact(root)
    monarch_map(
        root,
        bind_evidence(root, {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Vehicle": complete_observe(
                observedCounts={"transactions": 0, "balances": 8}
            ),
        }),
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    admissions = [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "monarch-entity-admission"
    ]
    published = json.dumps([a.payload for a in admissions], sort_keys=True)
    assert "Synthetic Vehicle" not in published
    assert "15000" not in published
    proofs = [a.payload["proof"] for a in admissions]
    assert all(proof["observedEvidenceHash"] for proof in proofs)
    assert any(proof["observedBalanceCount"] == 8 for proof in proofs)


def test_catalog_keeps_the_blocker_for_an_undecided_balance_only_profile(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root)
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "monarch-account-unmapped-count:1" in catalog.blockers


def test_imported_simplefin_observations_inherit_excluded_fact_status(tmp_path):
    root = fixture(tmp_path)
    fact_path = root / "facts" / "accounts.json"
    fact = json.loads(fact_path.read_text(encoding="utf-8"))
    fact["excluded"] = True
    fact["reason"] = "Synthetic account excluded by durable fact."
    write_json(fact_path, fact)
    facts, _parsed = _fact_accounts(root)

    batches, _files, _gaps, blockers = _simplefin_batches(root, NOW, facts)

    assert blockers == []
    accounts = [
        account
        for batch in batches
        for account in batch.accounts
        if account.canonical_key == "acct-synthetic"
    ]
    assert accounts
    assert {account.status for account in accounts} == {"excluded"}


def test_catalog_clears_the_blocker_once_a_complete_decision_exists(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root)
    vehicle_fact(root)
    monarch_map(
        root,
        bind_evidence(root, {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Vehicle": complete_observe(
                observedCounts={"transactions": 0, "balances": 8}
            ),
        }),
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert not [
        blocker
        for blocker in catalog.blockers
        if blocker.startswith("monarch-")
    ]
    assert "monarch-observe-observed-not-canonicalized" in catalog.gaps


def test_decided_balance_only_profile_keeps_its_balances_as_artifacts(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root)
    vehicle_fact(root)
    monarch_map(
        root,
        bind_evidence(root, {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Vehicle": complete_observe(
                observedCounts={"transactions": 0, "balances": 8}
            ),
        }),
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    balances = [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "monarch-balance"
        and artifact.payload.get("canonicalAccountId") == "acct-vehicle"
    ]
    assert len(balances) == 8
    assert {item.payload["admissionAction"] for item in balances} == {"observe"}
    assert all(item.payload["canonicalized"] is False for item in balances)
    assert all(item.payload["admissionDecisionId"] for item in balances)


def test_incomplete_decision_reports_its_specific_blocker(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root)
    vehicle_fact(root)
    monarch_map(
        root,
        {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Vehicle": complete_observe(
                observedCounts={"transactions": 0, "balances": 999}
            ),
        },
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "monarch-entity-balance-count-unreconciled" in catalog.blockers


def test_object_decision_targeting_an_unknown_fact_is_blocked(tmp_path):
    root = fixture(tmp_path)
    add_balance_only_profile(root)
    monarch_map(
        root,
        {
            "Synthetic Checking": "acct-synthetic",
            "Synthetic Vehicle": complete_observe(
                observedCounts={"transactions": 0, "balances": 8}
            ),
        },
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "monarch-entity-target-unresolved" in catalog.blockers


def test_legacy_string_map_catalog_behaviour_is_unchanged(tmp_path):
    root = fixture(tmp_path)
    catalog = load_source_catalog(root, generated_at=NOW)
    assert not [
        blocker
        for blocker in catalog.blockers
        if blocker.startswith("monarch-")
    ]
    mapping = [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "monarch-account-mapping"
    ]
    assert [item.payload for item in mapping] == [
        {
            "sourceAccount": "Synthetic Checking",
            "canonicalAccountId": "acct-synthetic",
        }
    ]


def test_legacy_string_map_that_names_an_unknown_fact_still_fails_hard(tmp_path):
    root = fixture(tmp_path)
    monarch_map(root, {"Synthetic Checking": "acct-missing"})
    with pytest.raises(SourceLoadError, match="monarch-account-unresolved"):
        load_source_catalog(root, generated_at=NOW)


def add_erroring_snapshot(root: Path, *, day: str, stem: str, errors) -> Path:
    path = write_json(
        root / "raw" / "simplefin" / day / f"simplefin-{stem}.json",
        {"accounts": [], "errors": list(errors)},
    )
    write_json(
        path.with_name(f"request-{stem}.json"),
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "snapshotSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "requestedStart": "2026-08-01",
            "requestedEnd": "2026-09-01",
            "pendingIncluded": True,
        },
    )
    return path


def test_historical_snapshot_error_does_not_block_a_clean_latest(tmp_path):
    root = fixture(tmp_path)
    add_erroring_snapshot(
        root,
        day="2026-08-30",
        stem="090000-000001",
        errors=["Connection to Example Bank needs attention"],
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert not [
        blocker
        for blocker in catalog.blockers
        if blocker.startswith("simplefin-")
    ]
    assert any(
        gap.startswith("simplefin-connection-latest-clean-stale")
        for gap in catalog.gaps
    )
    admissions = [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "simplefin-connection-admission"
    ]
    # An errors-only snapshot names no organization, so its error is global.
    assert {item.payload["connectionId"] for item in admissions} == {
        "default",
        FIXTURE_SCOPE,
    }
    payload = next(
        item.payload
        for item in admissions
        if item.payload["connectionId"] == "default"
    )
    assert payload["fresh"] is False
    assert payload["stale"] is True
    assert payload["supersededErrorSnapshotCount"] == 1
    # The error history stays visible in the admission proof.
    assert sum(item["errorCount"] for item in payload["proof"]["snapshots"]) == 1


def test_latest_snapshot_error_without_a_decision_still_blocks(tmp_path):
    root = fixture(tmp_path)
    add_erroring_snapshot(
        root, day="2026-09-02", stem="090000-000001", errors=["boom"]
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "simplefin-connection-error-undecided" in catalog.blockers
    assert "simplefin-institution-error-count:1" in catalog.blockers


def test_latest_snapshot_error_with_a_fallback_decision_is_admitted_stale(tmp_path):
    root = fixture(tmp_path)
    add_erroring_snapshot(
        root, day="2026-09-02", stem="090000-000001", errors=["boom"]
    )
    clean = (
        root
        / "raw"
        / "simplefin"
        / "2026-09-01"
        / "simplefin-120000-000001.json"
    )
    clean_sha = hashlib.sha256(clean.read_bytes()).hexdigest()
    mapping_path = root / "simplefin" / "account-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    from finance_store.source_admission import SnapshotEvidence as _Evidence

    error_hash = _Evidence(
        connection_id="default",
        snapshot_sha256="x",
        observed_at=NOW,
        errors=("boom",),
        requested_start=None,
        requested_end=None,
    ).error_hash
    mapping["connections"] = {
        "default": {
            "action": "fallback",
            "decision": "Bank re-auth in flight; prior verified snapshot stands.",
            "decidedAt": "2026-09-03",
            "currentErrorHash": error_hash,
            "fallbackSnapshotSha256": clean_sha,
            "fallbackRequestedStart": "2026-08-01",
            "fallbackRequestedEnd": "2026-09-01",
            "maxStalenessDays": 7,
        }
    }
    write_json(mapping_path, mapping)

    catalog = load_source_catalog(root, generated_at=NOW)
    assert not [
        blocker
        for blocker in catalog.blockers
        if blocker.startswith("simplefin-")
    ]
    assert "simplefin-connection-fallback-admitted-stale" in catalog.gaps
    assert any(
        gap.startswith("simplefin-connection-staleness-days:2")
        for gap in catalog.gaps
    )
    admissions = [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "simplefin-connection-admission"
    ]
    payload = admissions[0].payload
    assert payload["stale"] is True
    assert payload["fresh"] is False
    assert payload["admittedSnapshotSha256"] == clean_sha
    assert payload["proof"]["currentErrorHash"] == error_hash
    assert payload["proof"]["fallbackRequestedStart"] == "2026-08-01"
    assert payload["proof"]["fallbackRequestedEnd"] == "2026-09-01"


def test_builder_reads_the_clean_snapshot_when_history_had_errors(tmp_path):
    root = fixture(tmp_path)
    add_erroring_snapshot(
        root,
        day="2026-08-30",
        stem="090000-000001",
        errors=["Connection to Example Bank needs attention"],
    )
    candidates = sorted((root / "raw" / "simplefin").rglob("simplefin-*.json"))
    mapping_doc = json.loads(
        (root / "simplefin" / "account-map.json").read_text(encoding="utf-8")
    )
    admitted = normalized._admitted_simplefin_snapshots(candidates, mapping_doc)
    assert {path.name for _, path, *_ in admitted} == {
        "simplefin-120000-000001.json"
    }


def test_builder_still_fails_when_the_latest_snapshot_errors_undecided(tmp_path):
    root = fixture(tmp_path)
    add_erroring_snapshot(
        root, day="2026-09-02", stem="090000-000001", errors=["boom"]
    )
    candidates = sorted((root / "raw" / "simplefin").rglob("simplefin-*.json"))
    mapping_doc = json.loads(
        (root / "simplefin" / "account-map.json").read_text(encoding="utf-8")
    )
    with pytest.raises(normalized.BuildError, match="not admitted"):
        normalized._admitted_simplefin_snapshots(candidates, mapping_doc)


def test_builder_admits_prior_evidence_under_a_fallback_decision(tmp_path):
    root = fixture(tmp_path)
    add_erroring_snapshot(
        root, day="2026-09-02", stem="090000-000001", errors=["boom"]
    )
    clean = (
        root
        / "raw"
        / "simplefin"
        / "2026-09-01"
        / "simplefin-120000-000001.json"
    )
    clean_sha = hashlib.sha256(clean.read_bytes()).hexdigest()
    mapping_path = root / "simplefin" / "account-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["connections"] = {
        "default": {
            "action": "fallback",
            "decision": "Bank re-auth in flight; prior verified snapshot stands.",
            "decidedAt": "2026-09-03",
            "currentErrorHash": snapshot("x", 1, errors=("boom",)).error_hash,
            "fallbackSnapshotSha256": clean_sha,
            "fallbackRequestedStart": "2026-08-01",
            "fallbackRequestedEnd": "2026-09-01",
            "maxStalenessDays": 3650,
        }
    }
    write_json(mapping_path, mapping)
    candidates = sorted((root / "raw" / "simplefin").rglob("simplefin-*.json"))
    admitted = normalized._admitted_simplefin_snapshots(candidates, mapping)
    assert {path for _, path, *_ in admitted} == {clean}
    stale = {
        scope for scope, _, admission, *_ in admitted if admission.stale
    }
    assert stale == {"default", FIXTURE_SCOPE}

# ---------------------------------------------------------------------------
# Multiple connection scopes
# ---------------------------------------------------------------------------


def add_scoped_snapshot(root, *, day, stem, connection, errors=(), accounts=None):
    """One immutable snapshot whose sidecar declares its connection scope."""

    path = write_json(
        root / "raw" / "simplefin" / day / f"simplefin-{stem}.json",
        {"accounts": list(accounts or []), "errors": list(errors)},
    )
    write_json(
        path.with_name(f"request-{stem}.json"),
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "connectionId": connection,
            "snapshotSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "requestedStart": "2026-08-01",
            "requestedEnd": "2026-09-01",
            "pendingIncluded": True,
        },
    )
    return path


def test_each_connection_selects_its_own_snapshot_independently():
    """A healthy connection advances while a sibling connection is failing."""

    healthy = [snapshot("sha-a1", 1, connection="alpha"), snapshot("sha-a2", 3, connection="alpha")]
    failing = [
        snapshot("sha-b1", 1, connection="beta"),
        snapshot("sha-b2", 3, connection="beta", errors=("boom",)),
    ]
    scopes = evaluate_connection_scopes(
        {"alpha": healthy, "beta": failing},
        {
            "beta": fallback_decision(
                connection="beta",
                currentErrorHash=failing[-1].error_hash,
                fallbackSnapshotSha256="sha-b1",
            )
        },
        as_of=NOW,
    )
    by_id = {item.connection_id: item for item in scopes.admissions}
    # The healthy connection is not frozen by its failing sibling.
    assert by_id["alpha"].admitted_snapshot_sha256 == "sha-a2"
    assert by_id["alpha"].fresh is True
    assert by_id["alpha"].stale is False
    # The failing connection falls back on its own approved evidence only.
    assert by_id["beta"].admitted_snapshot_sha256 == "sha-b1"
    assert by_id["beta"].fresh is False
    assert by_id["beta"].stale is True
    assert scopes.blockers == ()
    assert scopes.stale_connections == ("beta",)


def test_one_failing_connection_blocks_only_itself():
    scopes = evaluate_connection_scopes(
        {
            "alpha": [snapshot("sha-a1", 3, connection="alpha")],
            "beta": [
                snapshot("sha-b1", 1, connection="beta"),
                snapshot("sha-b2", 3, connection="beta", errors=("boom",)),
            ],
        },
        {},
        as_of=NOW,
    )
    assert scopes.blockers == ("beta: simplefin-connection-error-undecided",)
    assert [item.connection_id for item in scopes.admitted] == ["alpha"]


def test_a_recovered_connection_stops_using_its_fallback():
    """Reappearance recovery: fresh clean evidence supersedes the fallback."""

    failing = [
        snapshot("sha-b1", 1, connection="beta"),
        snapshot("sha-b2", 2, connection="beta", errors=("boom",)),
    ]
    decision = {
        "beta": fallback_decision(
            connection="beta",
            currentErrorHash=failing[-1].error_hash,
            fallbackSnapshotSha256="sha-b1",
        )
    }
    stale = evaluate_connection_scopes({"beta": failing}, decision, as_of=NOW)
    assert stale.admissions[0].admitted_snapshot_sha256 == "sha-b1"
    assert stale.admissions[0].stale is True

    recovered_evidence = [*failing, snapshot("sha-b3", 3, connection="beta")]
    recovered = evaluate_connection_scopes(
        {"beta": recovered_evidence}, decision, as_of=NOW
    )
    admission = recovered.admissions[0]
    assert admission.admitted_snapshot_sha256 == "sha-b3"
    assert admission.fresh is True
    assert admission.stale is False
    assert admission.decision is None
    # The error history is superseded, never erased.
    assert admission.superseded_error_snapshots == ("sha-b2",)
    assert admission.gap == "simplefin-historical-connection-error-superseded"


def test_scope_selection_is_order_independent():
    grouped = {
        "beta": [snapshot("sha-b1", 3, connection="beta")],
        "alpha": [snapshot("sha-a1", 3, connection="alpha")],
    }
    first = evaluate_connection_scopes(grouped, {}, as_of=NOW)
    second = evaluate_connection_scopes(dict(reversed(list(grouped.items()))), {}, as_of=NOW)
    assert first.document() == second.document()
    assert [item.connection_id for item in first.admissions] == ["alpha", "beta"]


def test_scope_document_binds_hashes_windows_and_freshness_per_connection():
    failing = [
        snapshot("sha-b1", 1, connection="beta"),
        snapshot("sha-b2", 3, connection="beta", errors=("boom",)),
    ]
    scopes = evaluate_connection_scopes(
        {
            "alpha": [snapshot("sha-a1", 3, connection="alpha")],
            "beta": failing,
        },
        {
            "beta": fallback_decision(
                connection="beta",
                currentErrorHash=failing[-1].error_hash,
                fallbackSnapshotSha256="sha-b1",
            )
        },
        as_of=NOW,
    )
    document = scopes.document()
    assert document["connectionCount"] == 2
    assert document["admittedConnectionCount"] == 2
    assert document["staleConnections"] == ["beta"]
    entries = {item["connectionId"]: item for item in document["connections"]}
    assert entries["alpha"]["admittedSnapshotSha256"] == "sha-a1"
    assert entries["alpha"]["stale"] is False
    assert entries["beta"]["admittedSnapshotSha256"] == "sha-b1"
    assert entries["beta"]["latestSnapshotSha256"] == "sha-b2"
    assert entries["beta"]["stalenessDays"] == 2
    assert entries["beta"]["admittedRequestedStart"] == "2026-08-01"
    assert entries["beta"]["admittedRequestedEnd"] == "2026-09-01"
    assert len(entries["beta"]["decisionId"]) == 64
    beta = next(item for item in scopes.admissions if item.connection_id == "beta")
    assert beta.proof["currentErrorHash"] == failing[-1].error_hash
    assert beta.proof["fallbackSnapshotSha256"] == "sha-b1"


def test_empty_scope_mapping_is_refused():
    with pytest.raises(AdmissionError):
        evaluate_connection_scopes({}, {}, as_of=NOW)


def test_builder_admits_one_snapshot_per_connection(tmp_path):
    """Two connections, each contributing its own newest clean snapshot."""

    root = fixture(tmp_path)
    alpha = add_scoped_snapshot(
        root, day="2026-09-10", stem="090000-000001", connection="alpha"
    )
    beta = add_scoped_snapshot(
        root, day="2026-09-11", stem="090000-000002", connection="beta"
    )
    mapping_doc = json.loads(
        (root / "simplefin" / "account-map.json").read_text(encoding="utf-8")
    )
    admitted = normalized._admitted_simplefin_snapshots(
        sorted((root / "raw" / "simplefin").rglob("simplefin-*.json")), mapping_doc
    )
    selected = {connection: path for connection, path, *_ in admitted}
    assert selected["alpha"] == alpha
    assert selected["beta"] == beta
    # The pre-existing default-scope snapshot is still admitted on its own.
    assert "default" in selected


def test_builder_keeps_a_healthy_connection_while_a_sibling_falls_back(tmp_path):
    root = fixture(tmp_path)
    beta_clean = add_scoped_snapshot(
        root, day="2026-09-10", stem="090000-000002", connection="beta"
    )
    add_scoped_snapshot(
        root,
        day="2026-09-12",
        stem="090000-000003",
        connection="beta",
        errors=["Connection to Example Bank requires reauthentication"],
    )
    alpha_new = add_scoped_snapshot(
        root, day="2026-09-13", stem="090000-000004", connection="alpha"
    )
    mapping_path = root / "simplefin" / "account-map.json"
    mapping_doc = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping_doc["connections"] = {
        "beta": {
            "action": "fallback",
            "decision": "Beta institution re-auth in flight; prior snapshot stands.",
            "decidedAt": "2026-09-13",
            "currentErrorHash": snapshot(
                "x",
                1,
                errors=("Connection to Example Bank requires reauthentication",),
            ).error_hash,
            "fallbackSnapshotSha256": hashlib.sha256(
                beta_clean.read_bytes()
            ).hexdigest(),
            "fallbackRequestedStart": "2026-08-01",
            "fallbackRequestedEnd": "2026-09-01",
            "maxStalenessDays": 3650,
        }
    }
    write_json(mapping_path, mapping_doc)
    admitted = normalized._admitted_simplefin_snapshots(
        sorted((root / "raw" / "simplefin").rglob("simplefin-*.json")), mapping_doc
    )
    selected = {connection: path for connection, path, *_ in admitted}
    stale = {connection: item.stale for connection, _, item, *_ in admitted}
    # The healthy connection advances to its own newest snapshot.
    assert selected["alpha"] == alpha_new
    assert stale["alpha"] is False
    # The failing connection uses only its own last verified evidence.
    assert selected["beta"] == beta_clean
    assert stale["beta"] is True


def test_builder_refuses_when_one_connection_has_no_safe_decision(tmp_path):
    root = fixture(tmp_path)
    add_scoped_snapshot(
        root, day="2026-09-10", stem="090000-000002", connection="beta"
    )
    add_scoped_snapshot(
        root, day="2026-09-12", stem="090000-000003", connection="beta", errors=["boom"]
    )
    add_scoped_snapshot(
        root, day="2026-09-13", stem="090000-000004", connection="alpha"
    )
    mapping_doc = json.loads(
        (root / "simplefin" / "account-map.json").read_text(encoding="utf-8")
    )
    with pytest.raises(normalized.BuildError) as excinfo:
        normalized._admitted_simplefin_snapshots(
            sorted((root / "raw" / "simplefin").rglob("simplefin-*.json")), mapping_doc
        )
    message = str(excinfo.value)
    assert "beta" in message
    assert "simplefin-connection-error-undecided" in message
    # Only the offending connection is named; the healthy one is not implicated.
    assert "alpha:" not in message



def test_a_decision_cannot_be_spent_on_another_connection():
    """A fallback approval is scoped; it must not admit a different connection."""

    failing = [
        snapshot("sha-b1", 1, connection="beta"),
        snapshot("sha-b2", 3, connection="beta", errors=("boom",)),
    ]
    misfiled = {
        "beta": fallback_decision(
            connection="alpha",
            currentErrorHash=failing[-1].error_hash,
            fallbackSnapshotSha256="sha-b1",
        )
    }
    with pytest.raises(
        AdmissionError, match="simplefin-connection-decision-scope-mismatch"
    ):
        evaluate_connection_scopes({"beta": failing}, misfiled, as_of=NOW)


def test_byte_identical_snapshots_stay_in_their_own_connection(tmp_path):
    """Identical content in two connections must not cross-assign files."""

    root = fixture(tmp_path)
    alpha = add_scoped_snapshot(
        root, day="2026-09-10", stem="090000-000001", connection="alpha"
    )
    beta = add_scoped_snapshot(
        root, day="2026-09-10", stem="090000-000002", connection="beta"
    )
    assert alpha.read_bytes() == beta.read_bytes()
    mapping_doc = json.loads(
        (root / "simplefin" / "account-map.json").read_text(encoding="utf-8")
    )
    admitted = normalized._admitted_simplefin_snapshots(
        sorted((root / "raw" / "simplefin").rglob("simplefin-*.json")), mapping_doc
    )
    selected = {connection: path for connection, path, *_ in admitted}
    assert selected["alpha"] == alpha
    assert selected["beta"] == beta


def test_scope_index_reads_the_declared_connection_per_snapshot(tmp_path):
    root = fixture(tmp_path)
    alpha = add_scoped_snapshot(
        root, day="2026-09-10", stem="090000-000001", connection="alpha"
    )
    scopes = snapshot_scope_for_paths(
        sorted((root / "raw" / "simplefin").rglob("simplefin-*.json"))
    )
    assert scopes[alpha] == "alpha"
    # The pre-existing snapshot has no declared scope and keeps the default.
    assert set(scopes.values()) == {"alpha", "default"}



def test_catalog_reports_one_admission_per_connection(tmp_path):
    """The safe catalog must show every scope, not a single global verdict."""

    root = fixture(tmp_path)
    add_scoped_snapshot(
        root, day="2026-09-10", stem="090000-000001", connection="alpha"
    )
    add_scoped_snapshot(
        root, day="2026-09-11", stem="090000-000002", connection="beta"
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    admissions = [
        artifact
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "simplefin-connection-admission"
    ]
    scopes = sorted(item.payload["connectionId"] for item in admissions)
    assert scopes == sorted(["alpha", "beta", "default", FIXTURE_SCOPE])
    freshness = {
        item.payload["connectionId"]: item.payload["fresh"]
        for item in admissions
    }
    assert freshness["alpha"] is True
    assert freshness["beta"] is True
    assert freshness["default"] is False
    assert freshness[FIXTURE_SCOPE] is False
    assert not [
        blocker for blocker in catalog.blockers if "simplefin-connection" in blocker
    ]


def test_catalog_blocker_names_only_the_failing_connection(tmp_path):
    root = fixture(tmp_path)
    add_scoped_snapshot(
        root, day="2026-09-10", stem="090000-000001", connection="alpha"
    )
    add_scoped_snapshot(
        root, day="2026-09-11", stem="090000-000002", connection="beta"
    )
    add_scoped_snapshot(
        root,
        day="2026-09-12",
        stem="090000-000003",
        connection="beta",
        errors=["Connection to Example Bank requires reauthentication"],
    )
    catalog = load_source_catalog(root, generated_at=NOW)
    assert "simplefin-connection-blocked:beta" in catalog.blockers
    assert "simplefin-connection-blocked:alpha" not in catalog.blockers
    assert "simplefin-connection-error-undecided" in catalog.blockers
    # The healthy connections still publish their own fresh admission.
    fresh = {
        artifact.payload["connectionId"]
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind == "simplefin-connection-admission"
        and artifact.payload["fresh"]
    }
    assert fresh == {"alpha"}
