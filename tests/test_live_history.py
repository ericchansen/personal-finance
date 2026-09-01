"""Synthetic coverage for the private live category-history index.

Every account, merchant, amount and identifier here is invented. Nothing in this
module -- or in anything it asserts about -- contains a real merchant, balance,
account number or transaction.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from importers.categorize.identity import (
    SourceResolver,
    build_account_bridge,
    build_canonical_index,
)
from importers.categorize.live_history import (
    DEFAULT_LOOKBACK_MONTHS,
    LIVE_ACCOUNT_CONFIDENCE,
    LIVE_GLOBAL_CONFIDENCE,
    MAX_SEALED_EVIDENCE_IDS,
    MIN_LIVE_EVIDENCE,
    build_live_history_index,
    build_live_history_scope,
    lookback_start,
    summarize_live_history,
)
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import instance_fingerprint, plan_fingerprint
from importers.simplefin.categorization import (
    INCOME_TAXONOMY,
    SPENDING_TAXONOMY,
    HistoryIndex,
    _require_no_live_history_drift,
    build_category_plan,
    evidence_binding,
    in_source_scope,
    live_history_drift,
    live_history_evidence_activity_ids,
    merchant_hash,
    promote_category_plan,
    rehearse_category_plan,
    staging_idempotency_key,
    validate_category_plan,
    write_category_review,
    write_rehearsal_receipt,
)
from importers.simplefin.spending_adapter import SpendingAdapter

from tests.test_simplefin_categorization import StageClient
from tests.test_source_categorization import (
    ACCOUNT_MAP,
    ACCOUNTS,
    CANONICAL_CHECKING,
    END,
    HASH_KEY,
    LIVE_CARD,
    LIVE_CHECKING,
    NOW,
    START,
    canonical_row,
    catalogs,
    live_activity,
)

SPENDING_ACCOUNTS = {LIVE_CHECKING, LIVE_CARD}


def spending(category_id):
    return [{"taxonomyId": SPENDING_TAXONOMY, "categoryId": category_id}]


def income(category_id):
    return [{"taxonomyId": INCOME_TAXONOMY, "categoryId": category_id}]


def history_activity(activity_id, *, date="2026-05-14", **kwargs):
    """One already-categorized activity, well inside the default lookback."""
    kwargs.setdefault("idempotency_key", f"monarch:{activity_id}")
    key = kwargs.pop("idempotency_key")
    return live_activity(activity_id, key, date=date, **kwargs)


def make_scope(**overrides):
    options = {
        "end_date": END,
        "lookback_months": DEFAULT_LOOKBACK_MONTHS,
        "account_ids": sorted(SPENDING_ACCOUNTS),
        "min_evidence": MIN_LIVE_EVIDENCE,
    }
    options.update(overrides)
    return build_live_history_scope(**options)


def make_index(activities, assignments, *, scope=None, structural_ids=None, accounts=None):
    return build_live_history_index(
        activities,
        assignments,
        ACCOUNTS if accounts is None else accounts,
        HASH_KEY,
        scope=scope or make_scope(),
        spending_account_ids=SPENDING_ACCOUNTS,
        structural_activity_ids=structural_ids,
    )


def build_plan(
    window,
    *,
    history=(),
    history_assignments=None,
    window_assignments=None,
    canonical=(),
    live_history="auto",
    decisions=None,
    canonical_history=None,
    scope=None,
    structural_ids=None,
    evidence=(),
    accounts=None,
    allow_merchant_identity=None,
):
    """Plan one window against a live-history index built from ``history``."""
    account_rows = ACCOUNTS if accounts is None else accounts
    index = build_canonical_index(canonical)
    bridge = build_account_bridge(canonical_account_map=ACCOUNT_MAP)
    scoped = [row for row in window if in_source_scope(row, ("*",))]
    resolver = SourceResolver(index, bridge, scoped)
    assignments = (
        {row["id"]: [] for row in scoped}
        if window_assignments is None
        else window_assignments
    )
    if live_history == "auto":
        live_history = make_index(
            list(history) + list(window),
            {**(history_assignments or {}), **assignments},
            scope=scope,
            structural_ids=structural_ids,
            accounts=account_rows,
        )
    eligible = [
        row
        for row in scoped
        if row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
    ]
    outflow = sum(Decimal(str(row.get("amount") or 0)) for row in eligible)
    uncategorized = sum(1 for row in eligible if not assignments.get(row["id"]))
    return build_category_plan(
        list(window) + list(history),
        account_rows,
        assignments,
        catalogs(),
        {"accounts": []},
        canonical_history if canonical_history is not None else HistoryIndex(),
        decisions
        or {
            "schemaVersion": 1,
            "categoryAliases": {},
            "merchantOverrides": [],
            "activityOverrides": [],
        },
        list(evidence),
        "synthetic-instance",
        HASH_KEY,
        report_start=START,
        report_end=END,
        spending_account_ids=SPENDING_ACCOUNTS,
        current_report={
            "current": {
                "income": "0",
                "outflow": str(outflow),
                "net": str(-outflow),
                "count": len(eligible),
            },
            "spendingBreakdown": [],
            "incomeBreakdown": [],
        },
        current_uncategorized_count=uncategorized,
        generated_at=NOW,
        resolver=resolver,
        source_systems=("*",),
        live_history=live_history,
        allow_merchant_identity=allow_merchant_identity,
    )


def only_candidate(plan):
    assert plan["metrics"]["autoCount"] == 1, plan["manualItems"]
    return plan["autoCandidates"][0]


def manual_reason(plan, activity_id):
    return next(
        row for row in plan["manualItems"] if row["activityId"] == activity_id
    )["reason"]


# ---------------------------------------------------------------------------
# Lookback scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "end,months,expected",
    [
        ("2026-08-31", 24, "2024-09-01"),
        ("2026-08-31", 1, "2026-08-01"),
        ("2026-01-15", 24, "2024-02-01"),
        ("2026-03-31", 12, "2025-04-01"),
    ],
)
def test_lookback_reaches_back_whole_calendar_months(end, months, expected):
    assert lookback_start(end, months) == expected


def test_default_lookback_has_seen_an_annual_merchant_twice():
    start = date.fromisoformat(lookback_start(END, DEFAULT_LOOKBACK_MONTHS))
    end = date.fromisoformat(END)

    assert (end.year - start.year) * 12 + end.month - start.month >= 12


def test_lookback_refuses_a_non_positive_window():
    with pytest.raises(DecisionError, match="at least one month"):
        lookback_start(END, 0)


def test_history_outside_the_lookback_never_trains_the_index():
    activities = [
        history_activity("old-1", date="2019-05-02"),
        history_activity("old-2", date="2019-06-02"),
    ]
    index = make_index(
        activities, {row["id"]: spending("groceries") for row in activities}
    )

    assert index.merchant_count == 0
    assert index.observed_activity_ids == set()


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------


def test_account_consensus_categorizes_a_repeat_merchant():
    history = [
        history_activity("h-1", date="2025-11-04"),
        history_activity("h-2", date="2026-05-04"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["evidenceKind"] == "live-account-history"
    assert candidate["categoryId"] == "groceries"
    assert candidate["confidence"] == LIVE_ACCOUNT_CONFIDENCE
    assert candidate["evidenceCount"] == 2
    assert candidate["liveHistoryScope"] == "account"
    assert plan["metrics"]["liveHistoryAccountCount"] == 1


def test_global_consensus_categorizes_a_merchant_seen_on_another_account():
    history = [
        history_activity("h-1", account_id=LIVE_CARD, kind="EXPENSE"),
        history_activity("h-2", account_id=LIVE_CARD, kind="EXPENSE", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["evidenceKind"] == "live-global-history"
    assert candidate["confidence"] == LIVE_GLOBAL_CONFIDENCE
    assert candidate["liveHistoryScope"] == "global"
    assert plan["metrics"]["liveHistoryGlobalCount"] == 1


def test_account_consensus_outranks_a_different_global_consensus():
    history = [
        history_activity("h-1", date="2025-11-04"),
        history_activity("h-2", date="2026-05-04"),
        history_activity("h-3", account_id=LIVE_CARD, kind="EXPENSE"),
        history_activity("h-4", account_id=LIVE_CARD, kind="EXPENSE", date="2026-06-14"),
    ]
    index = make_index(
        history,
        {
            "h-1": spending("groceries"),
            "h-2": spending("groceries"),
            "h-3": spending("fuel"),
            "h-4": spending("fuel"),
        },
    )
    digest = merchant_hash("Synthetic Grocer", HASH_KEY)

    lookup = index.lookup(LIVE_CHECKING, digest, SPENDING_TAXONOMY)

    # A merchant meaning two things across the library is a conflict, and a
    # conflict abstains at every scope rather than letting the narrower scope
    # quietly win.
    assert lookup.matched is False
    assert lookup.reason == "conflicting-live-history"


def test_one_activity_listed_twice_is_one_observation():
    activity = history_activity("h-1")
    index = make_index([activity, dict(activity)], {"h-1": spending("groceries")})
    digest = merchant_hash("Synthetic Grocer", HASH_KEY)

    lookup = index.lookup(LIVE_CHECKING, digest, SPENDING_TAXONOMY)

    assert lookup.matched is False
    assert lookup.reason == "insufficient-live-history"
    assert lookup.candidate.evidence_count == 1


def test_a_repeated_activity_never_manufactures_a_plan_candidate():
    activity = history_activity("h-1")
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=[activity, dict(activity)],
        history_assignments={"h-1": spending("groceries")},
    )

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-1") == "no-history"
    assert plan["metrics"]["liveHistoryAbstentionCounts"] == {
        "insufficient-live-history": 1
    }


def test_a_conflicting_account_merchant_abstains():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=history,
        history_assignments={
            "h-1": spending("groceries"),
            "h-2": spending("fuel"),
        },
    )

    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["liveHistoryAbstentionCounts"] == {
        "conflicting-live-history": 1
    }


def test_a_conflict_anywhere_globally_abstains_even_with_a_clean_account():
    history = [
        history_activity("h-1", date="2025-11-04"),
        history_activity("h-2", date="2026-05-04"),
        history_activity("h-3", account_id=LIVE_CARD, kind="EXPENSE"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=history,
        history_assignments={
            "h-1": spending("groceries"),
            "h-2": spending("groceries"),
            "h-3": spending("fuel"),
        },
    )

    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["liveHistoryAbstentionCounts"] == {
        "conflicting-live-history": 1
    }


def test_evidence_below_the_threshold_abstains_and_is_counted():
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=[history_activity("h-1")],
        history_assignments={"h-1": spending("groceries")},
    )

    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["liveHistoryAbstentionCounts"] == {
        "insufficient-live-history": 1
    }


def test_the_evidence_threshold_is_configurable():
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=[history_activity("h-1")],
        history_assignments={"h-1": spending("groceries")},
        scope=make_scope(min_evidence=1),
    )

    assert only_candidate(plan)["evidenceCount"] == 1


def test_an_unknown_category_id_is_never_assigned():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=history,
        history_assignments={
            row["id"]: spending("retired-category") for row in history
        },
    )

    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["liveHistoryAbstentionCounts"] == {
        "live-history-unknown-category": 1
    }


def test_consensus_records_its_date_range_and_assignment_provenance():
    history = [
        history_activity("h-1", date="2025-11-04"),
        history_activity("h-2", date="2026-06-14"),
    ]
    index = make_index(
        history,
        {
            "h-1": [{
                "taxonomyId": SPENDING_TAXONOMY,
                "categoryId": "groceries",
                "source": "preset",
            }],
            "h-2": [{
                "taxonomyId": SPENDING_TAXONOMY,
                "categoryId": "groceries",
                "source": "manual",
            }],
        },
    )

    consensus = index.lookup(
        LIVE_CHECKING, merchant_hash("Synthetic Grocer", HASH_KEY), SPENDING_TAXONOMY
    ).consensus

    assert consensus.first_seen == "2025-11-04"
    assert consensus.last_seen == "2026-06-14"
    assert consensus.assignment_provenance == ("manual", "preset")
    assert consensus.activity_ids == ("h-1", "h-2")


def test_an_assignment_with_no_stated_provenance_is_not_invented():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    index = make_index(
        history, {row["id"]: spending("groceries") for row in history}
    )

    consensus = index.lookup(
        LIVE_CHECKING, merchant_hash("Synthetic Grocer", HASH_KEY), SPENDING_TAXONOMY
    ).consensus

    assert consensus.assignment_provenance == ("unstated",)


def test_sealed_evidence_samples_are_bounded_but_the_count_is_exact():
    history = [
        history_activity(f"h-{index:03d}", date="2026-06-14")
        for index in range(MAX_SEALED_EVIDENCE_IDS + 5)
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    sealed = plan["liveHistory"]["evidence"][0]
    assert sealed["evidenceCount"] == MAX_SEALED_EVIDENCE_IDS + 5
    assert len(sealed["sampleActivityIds"]) == MAX_SEALED_EVIDENCE_IDS
    assert only_candidate(plan)["evidenceCount"] == MAX_SEALED_EVIDENCE_IDS + 5


# ---------------------------------------------------------------------------
# Direction and taxonomy compatibility
# ---------------------------------------------------------------------------


def test_spending_history_never_categorizes_an_income_deposit():
    history = [
        history_activity("h-1", description="Synthetic Payer"),
        history_activity("h-2", description="Synthetic Payer", date="2026-06-14"),
    ]
    plan = build_plan(
        [
            live_activity(
                "activity-1",
                "monarch:row-9",
                kind="DEPOSIT",
                description="Synthetic Payer",
            )
        ],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["liveHistoryAbstentionCounts"] == {
        "live-history-direction-mismatch": 1
    }


def test_income_history_categorizes_an_income_deposit():
    history = [
        history_activity("h-1", kind="DEPOSIT", description="Synthetic Payer"),
        history_activity(
            "h-2", kind="DEPOSIT", description="Synthetic Payer", date="2026-06-14"
        ),
    ]
    plan = build_plan(
        [
            live_activity(
                "activity-1",
                "monarch:row-9",
                kind="DEPOSIT",
                description="Synthetic Payer",
            )
        ],
        history=history,
        history_assignments={row["id"]: income("salary") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["taxonomyId"] == INCOME_TAXONOMY
    assert candidate["categoryId"] == "salary"


def test_an_assignment_pointing_the_wrong_way_never_trains_the_index():
    history = [
        history_activity("h-1", kind="DEPOSIT"),
        history_activity("h-2", kind="DEPOSIT", date="2026-06-14"),
    ]
    index = make_index(
        history, {row["id"]: spending("groceries") for row in history}
    )

    assert index.merchant_count == 0
    assert index.excluded_counts["direction-mismatch"] == 2


def test_a_card_credit_reports_as_negative_spending():
    history = [
        history_activity("h-1", account_id=LIVE_CARD, kind="EXPENSE"),
        history_activity(
            "h-2", account_id=LIVE_CARD, kind="EXPENSE", date="2026-06-14"
        ),
    ]
    plan = build_plan(
        [
            live_activity(
                "activity-1", "monarch:row-9", account_id=LIVE_CARD, kind="CREDIT"
            )
        ],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["taxonomyId"] == SPENDING_TAXONOMY
    assert candidate["reportAmount"] == "-42.50"


# ---------------------------------------------------------------------------
# Structural exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"kind": "TRANSFER_OUT"}, "transfer"),
        ({"subtype": "external_transfer"}, "external-reconciliation"),
        ({"subtype": "cc_payment"}, "structural-subtype"),
        ({"subtype": "saving"}, "structural-subtype"),
        ({"subtype": "investment"}, "structural-subtype"),
        ({"subtype": "loan_payment"}, "structural-subtype"),
        ({"metadata": {"flow": {"is_external": True}}}, "external-reconciliation"),
        ({"metadata": {"excluded": True}}, "excluded-activity"),
        ({"metadata": {"transactionKind": "cc_payment"}}, "structural-canonical-kind"),
        ({"idempotency_key": "gap:live-checking:2026-05-14"}, "reconciliation-source"),
        ({"idempotency_key": "hand-entered-row"}, "unknown-source-identity"),
        ({"kind": "BUY"}, "not-a-cash-flow"),
    ],
)
def test_structural_activities_never_train_the_index(overrides, expected):
    activities = [
        history_activity("h-1", **overrides),
        history_activity("h-2", date="2026-06-14", **overrides),
    ]
    index = make_index(
        activities, {row["id"]: spending("groceries") for row in activities}
    )

    assert index.merchant_count == 0
    assert index.excluded_counts[expected] == 2


def test_a_canonical_structural_kind_never_trains_the_index():
    activities = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    index = make_index(
        activities,
        {row["id"]: spending("groceries") for row in activities},
        structural_ids={"h-1", "h-2"},
    )

    assert index.merchant_count == 0
    assert index.excluded_counts["structural-canonical-kind"] == 2


def test_the_structural_filter_reads_canonical_transaction_kinds():
    activities = [
        history_activity("h-1", idempotency_key=f"extract:{LIVE_CHECKING}:FIT-1"),
        history_activity("h-2", idempotency_key=f"extract:{LIVE_CHECKING}:FIT-2"),
    ]
    canonical = [
        canonical_row("extract:stable:FIT-1", transaction_kind="cc_payment"),
        canonical_row("extract:stable:FIT-2", transaction_kind="cc_payment"),
    ]
    resolver = SourceResolver(
        build_canonical_index(canonical),
        build_account_bridge(canonical_account_map=ACCOUNT_MAP),
        activities,
        allow_fallback=False,
    )

    assert {resolver(row).transaction_kind for row in activities} == {"cc_payment"}


@pytest.mark.parametrize("category_id", ["__uncategorized__", "uncategorized", ""])
def test_the_synthetic_uncategorized_identity_never_trains_the_index(category_id):
    activities = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    index = make_index(
        activities, {row["id"]: spending(category_id) for row in activities}
    )

    assert index.merchant_count == 0
    assert index.excluded_counts["synthetic-uncategorized"] == 2


def test_an_uncategorized_activity_never_trains_the_index():
    activities = [history_activity("h-1"), history_activity("h-2")]
    index = make_index(activities, {"h-1": [], "h-2": None})

    assert index.merchant_count == 0
    assert index.excluded_counts["uncategorized"] == 2


def test_an_account_that_is_not_spending_enabled_never_trains_the_index():
    activities = [
        history_activity("h-1", account_id="live-brokerage"),
        history_activity("h-2", account_id="live-brokerage"),
    ]
    index = make_index(
        activities, {row["id"]: spending("groceries") for row in activities}
    )

    assert index.merchant_count == 0
    assert index.excluded_counts["account-not-spending-enabled"] == 2


def test_two_cash_flow_assignments_on_one_activity_never_train_the_index():
    activities = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    index = make_index(
        activities,
        {
            row["id"]: spending("groceries") + income("salary")
            for row in activities
        },
    )

    assert index.merchant_count == 0
    assert index.excluded_counts["multiple-assignments"] == 2


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_reviewed_canonical_carryover_outranks_live_history():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-1")],
        canonical=[
            canonical_row(
                "monarch:row-1", category="Mortgage", category_id="mortgage"
            )
        ],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["evidenceKind"] == "canonical-category:identity-exact"
    assert candidate["categoryId"] == "housing"
    assert "liveHistoryEvidenceHash" not in candidate
    assert plan["metrics"]["liveHistoryCount"] == 0


def test_a_private_override_outranks_live_history():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-1")],
        canonical=[canonical_row("monarch:row-1", category="", category_id="")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
        decisions={
            "schemaVersion": 1,
            "categoryAliases": {},
            "merchantOverrides": [
                {
                    "merchantHash": merchant_hash("Synthetic Grocer", HASH_KEY),
                    "taxonomyId": SPENDING_TAXONOMY,
                    "categoryId": "fuel",
                    "rationale": "synthetic decision",
                }
            ],
            "activityOverrides": [],
        },
    )

    candidate = only_candidate(plan)
    assert candidate["evidenceKind"] == "merchant-override"
    assert candidate["categoryId"] == "fuel"


def test_reviewed_canonical_merchant_consensus_outranks_live_history():
    canonical_history = HistoryIndex()
    for source_id in ("monarch:old-1", "monarch:old-2"):
        canonical_history.add(
            CANONICAL_CHECKING,
            "Synthetic Grocer",
            "Gas & Fuel",
            source_id,
            when="2025-01-02",
        )
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-1")],
        canonical=[canonical_row("monarch:row-1", category="", category_id="")],
        canonical_history=canonical_history,
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["evidenceKind"] == "account-history"
    assert candidate["categoryId"] == "fuel"


def test_conflicting_reviewed_canonical_history_is_never_broken_by_live_history():
    canonical_history = HistoryIndex()
    canonical_history.add(
        CANONICAL_CHECKING, "Synthetic Grocer", "Groceries", "monarch:old-1"
    )
    canonical_history.add(
        CANONICAL_CHECKING, "Synthetic Grocer", "Gas & Fuel", "monarch:old-2"
    )
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-1")],
        canonical=[canonical_row("monarch:row-1", category="", category_id="")],
        canonical_history=canonical_history,
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-1") == "conflicting-history"


def test_live_history_rescues_insufficient_reviewed_canonical_history():
    canonical_history = HistoryIndex()
    canonical_history.add(
        CANONICAL_CHECKING, "Synthetic Grocer", "Gas & Fuel", "monarch:old-1"
    )
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-1")],
        canonical=[canonical_row("monarch:row-1", category="", category_id="")],
        canonical_history=canonical_history,
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["evidenceKind"] == "live-account-history"
    assert candidate["categoryId"] == "groceries"


def test_live_history_rescues_a_canonical_category_the_catalog_cannot_name():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-1")],
        canonical=[
            canonical_row(
                "monarch:row-1",
                category="Retired Vocabulary",
                category_id="retired",
            )
        ],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["evidenceKind"] == "live-account-history"
    assert candidate["categoryId"] == "groceries"


def test_an_already_categorized_activity_is_never_a_candidate():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    window = [live_activity("activity-1", "monarch:row-9")]
    plan = build_plan(
        window,
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
        window_assignments={"activity-1": spending("fuel")},
    )

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-1") == "already-categorized"


# ---------------------------------------------------------------------------
# Identity: merchant history without a canonical source identity
# ---------------------------------------------------------------------------


def test_merchant_history_categorizes_an_activity_with_no_canonical_row():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", f"simplefin:{LIVE_CHECKING}:txn-77")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["identitySource"] == "merchant-history"
    assert candidate["matchKind"] == "merchant-history-identity"
    assert candidate["canonicalAccountId"] == CANONICAL_CHECKING
    assert candidate["sourceId"] == "txn-77"
    assert plan["metrics"]["merchantIdentityCount"] == 1


def test_merchant_history_still_needs_a_mapped_account():
    history = [
        history_activity("h-1", account_id=LIVE_CARD, kind="EXPENSE"),
        history_activity(
            "h-2", account_id=LIVE_CARD, kind="EXPENSE", date="2026-06-14"
        ),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9", account_id="live-orphan")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
        accounts=ACCOUNTS + [{"id": "live-orphan", "accountType": "CASH"}],
    )

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-1") == "account-not-spending-enabled"


def test_merchant_identity_is_off_without_a_live_history_index():
    plan = build_plan(
        [live_activity("activity-1", f"simplefin:{LIVE_CHECKING}:txn-77")],
        live_history=None,
    )

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-1") == "unresolved-source-identity"
    assert "liveHistory" not in plan


def test_a_structural_key_never_receives_a_merchant_identity():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    plan = build_plan(
        [live_activity("activity-1", f"gap:{LIVE_CHECKING}:2026-08-12")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["balanceGapCount"] == 1


# ---------------------------------------------------------------------------
# Cross-source matching
# ---------------------------------------------------------------------------


def test_live_history_matches_across_every_source():
    history = [
        history_activity("h-1", idempotency_key="monarch:legacy-1"),
        history_activity(
            "h-2",
            idempotency_key=f"extract:{LIVE_CHECKING}:FIT-9",
            date="2026-06-14",
        ),
    ]
    plan = build_plan(
        [live_activity("activity-1", f"simplefin:{LIVE_CHECKING}:txn-77")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    candidate = only_candidate(plan)
    assert candidate["sourceSystem"] == "simplefin"
    assert candidate["liveHistorySourceSystems"] == ["extract", "monarch"]


def test_every_source_in_one_window_is_covered_by_live_history():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    window = [
        live_activity("activity-1", "monarch:row-9"),
        live_activity("activity-2", f"extract:{LIVE_CHECKING}:FIT-77"),
        live_activity("activity-3", f"simplefin:{LIVE_CHECKING}:txn-77"),
    ]
    plan = build_plan(
        window,
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )

    assert plan["metrics"]["autoCount"] == 3
    assert plan["metrics"]["evidenceKindCounts"] == {"live-account-history": 3}
    assert plan["metrics"]["sourceSystemCounts"] == {
        "extract": 1,
        "monarch": 1,
        "simplefin": 1,
    }


# ---------------------------------------------------------------------------
# Sealing and drift
# ---------------------------------------------------------------------------


def live_history_plan(tmp_path):
    evidence_file = tmp_path / "synthetic-evidence.csv"
    evidence_file.write_text("synthetic", encoding="utf-8")
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    return build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
        evidence=evidence_binding([evidence_file]),
    )


def test_a_plan_seals_its_live_history_scope_and_evidence(tmp_path):
    plan = live_history_plan(tmp_path)

    seal = plan["liveHistory"]
    assert seal["scope"]["lookbackMonths"] == DEFAULT_LOOKBACK_MONTHS
    assert seal["scope"]["startDate"] == lookback_start(END, DEFAULT_LOOKBACK_MONTHS)
    assert seal["scope"]["accountIds"] == sorted(SPENDING_ACCOUNTS)
    assert seal["metrics"]["observedActivityCount"] == 2
    assert len(seal["evidence"]) == 1
    assert seal["evidence"][0]["sampleActivityIds"] == ["h-1", "h-2"]
    assert seal["evidence"][0]["evidenceHash"] == only_candidate(plan)[
        "liveHistoryEvidenceHash"
    ]
    validate_category_plan(plan)


def reseal(plan):
    """Re-stamp a tampered plan so seal validation, not the outer hash, fires."""
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    return plan


def test_the_sealed_index_fingerprint_covers_its_own_content(tmp_path):
    plan = live_history_plan(tmp_path)
    plan["liveHistory"]["evidence"][0]["categoryId"] = "fuel"

    with pytest.raises(DecisionError, match="live history fingerprint is invalid"):
        validate_category_plan(reseal(plan))


def test_tampering_with_the_seal_breaks_the_plan_fingerprint(tmp_path):
    plan = live_history_plan(tmp_path)
    tampered = json.loads(json.dumps(plan))
    tampered["liveHistory"]["scope"]["lookbackMonths"] = 1

    assert plan_fingerprint({
        key: value for key, value in tampered.items() if key != "planFingerprint"
    }) != plan["planFingerprint"]
    with pytest.raises(DecisionError, match="plan fingerprint is invalid"):
        validate_category_plan(tampered)


def test_a_candidate_may_not_cite_unsealed_live_history(tmp_path):
    plan = live_history_plan(tmp_path)
    plan["autoCandidates"][0]["liveHistoryEvidenceHash"] = "not-sealed"

    with pytest.raises(DecisionError, match="cites unsealed live category history"):
        validate_category_plan(reseal(plan))


def test_a_candidate_may_not_disagree_with_its_sealed_live_history(tmp_path):
    plan = live_history_plan(tmp_path)
    plan["autoCandidates"][0]["categoryId"] = "fuel"

    with pytest.raises(DecisionError, match="disagrees with its sealed"):
        validate_category_plan(reseal(plan))


def test_a_live_history_candidate_without_a_seal_is_rejected(tmp_path):
    plan = live_history_plan(tmp_path)
    del plan["liveHistory"]

    with pytest.raises(DecisionError, match="without sealing it"):
        validate_category_plan(reseal(plan))


def test_a_plan_that_never_used_live_history_needs_no_seal(tmp_path):
    evidence_file = tmp_path / "synthetic-evidence.csv"
    evidence_file.write_text("synthetic", encoding="utf-8")
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-1")],
        canonical=[canonical_row("monarch:row-1")],
        live_history=None,
        evidence=evidence_binding([evidence_file]),
    )

    assert "liveHistory" not in plan
    validate_category_plan(plan)


def test_sealed_evidence_names_exactly_the_activities_to_re_read(tmp_path):
    plan = live_history_plan(tmp_path)

    assert live_history_evidence_activity_ids(plan) == ["h-1", "h-2"]


@pytest.mark.parametrize(
    "assignments,reason",
    [
        ({"h-1": (SPENDING_TAXONOMY, "groceries")}, "evidence-activity-missing"),
        (
            {"h-1": (SPENDING_TAXONOMY, "groceries"), "h-2": None},
            "evidence-activity-uncategorized",
        ),
        (
            {
                "h-1": (SPENDING_TAXONOMY, "groceries"),
                "h-2": (SPENDING_TAXONOMY, "fuel"),
            },
            "evidence-activity-recategorized",
        ),
    ],
)
def test_drift_is_reported_when_the_evidence_moves(tmp_path, assignments, reason):
    plan = live_history_plan(tmp_path)

    drift = live_history_drift(plan, assignments)

    assert [row["reason"] for row in drift] == [reason]
    assert drift[0]["activityId"] == "h-2"


def test_unchanged_evidence_reports_no_drift(tmp_path):
    plan = live_history_plan(tmp_path)

    assert live_history_drift(
        plan,
        {
            "h-1": (SPENDING_TAXONOMY, "groceries"),
            "h-2": (SPENDING_TAXONOMY, "groceries"),
        },
    ) == []


class AssignmentReadClient:
    """A read-only stand-in exposing only the assignment endpoint."""

    def __init__(self, assignments):
        self.assignments = assignments
        self.reads = []

    def get(self, path):
        activity_id = path.split("/")[3]
        self.reads.append(activity_id)
        return self.assignments.get(activity_id, [])


def test_promotion_refuses_a_plan_whose_live_history_was_recategorized(tmp_path):
    plan = live_history_plan(tmp_path)
    client = AssignmentReadClient({
        "h-1": spending("groceries"),
        "h-2": spending("fuel"),
    })

    with pytest.raises(DecisionError, match="live category history changed"):
        _require_no_live_history_drift(SpendingAdapter(client), plan)

    assert sorted(client.reads) == ["h-1", "h-2"]


def test_promotion_accepts_a_plan_whose_live_history_is_unchanged(tmp_path):
    plan = live_history_plan(tmp_path)
    client = AssignmentReadClient({
        "h-1": spending("groceries"),
        "h-2": spending("groceries"),
    })

    _require_no_live_history_drift(SpendingAdapter(client), plan)


def test_promotion_skips_the_drift_read_for_a_plan_without_live_history():
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-1")],
        canonical=[canonical_row("monarch:row-1")],
        live_history=None,
    )
    client = AssignmentReadClient({})

    _require_no_live_history_drift(SpendingAdapter(client), plan)

    assert client.reads == []


# ---------------------------------------------------------------------------
# End-to-end: rehearse and promote a live-history plan
# ---------------------------------------------------------------------------


class WindowedStageClient(StageClient):
    """A staging/production stand-in whose Spending report respects the window.

    The shared fixture reports over every row it holds. A live-history plan
    deliberately carries two years of already-categorized history alongside one
    month of candidates, so the report has to be scoped the way Wealthfolio
    scopes it or the sealed pre-state could never match.
    """

    def post(self, path, payload):
        start = str(payload.get("startDate") or "")[:10]
        end = str(payload.get("endDate") or "")[:10]
        held = self.rows
        try:
            self.rows = [
                row
                for row in held
                if start <= str(row.get("date") or "")[:10] <= end
            ]
            return super().post(path, payload)
        finally:
            self.rows = held


class WindowedProductionClient(WindowedStageClient):
    def get(self, path):
        if path == "/app/info":
            return {"version": "3.7.0-synthetic", "dbPath": "synthetic-production.db"}
        return super().get(path)


def promotion_bundle(tmp_path):
    plan = live_history_plan(tmp_path)
    candidate = only_candidate(plan)
    window = [live_activity("activity-1", "monarch:row-9")]
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
    ]
    client = WindowedProductionClient(window + history)
    for row in history:
        client.assignments[row["id"]] = spending("groceries")
    environment = instance_fingerprint(client, "http://127.0.0.1:8088")
    plan["environmentFingerprint"] = environment
    reseal(plan)
    plan_path = tmp_path / "category-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    staged = dict(window[0])
    staged["id"] = "staging-activity-1"
    staged["accountId"] = "stage-account"
    staged["idempotencyKey"] = staging_idempotency_key(
        {"sourceSystem": candidate["sourceSystem"], "sourceId": candidate["sourceId"]},
        "stage-account",
    )
    rehearsal = rehearse_category_plan(
        WindowedStageClient([staged]),
        plan,
        {CANONICAL_CHECKING: "stage-account"},
        HASH_KEY,
        generated_at=NOW,
    )
    assert rehearsal["status"] == "applied"
    receipt_path = write_rehearsal_receipt(tmp_path, rehearsal, plan_path)
    return client, plan, plan_path, receipt_path, environment


def test_a_live_history_plan_rehearses_and_promotes(tmp_path):
    client, plan, plan_path, receipt_path, environment = promotion_bundle(tmp_path)

    _receipt, written, already = promote_category_plan(
        client,
        plan,
        plan_path,
        receipt_path,
        tmp_path,
        HASH_KEY,
        environment_fingerprint=environment,
        supplied_plan_fingerprint=plan["planFingerprint"],
        supplied_environment_fingerprint=environment,
        allow_production=True,
        wait_seconds=0,
        generated_at=NOW,
    )

    assert already is False
    assert client.assignments["activity-1"] == spending("groceries")
    assert written.is_file()


def test_promotion_refuses_a_plan_whose_sealed_history_moved(tmp_path):
    client, plan, plan_path, receipt_path, environment = promotion_bundle(tmp_path)
    client.assignments["h-2"] = spending("fuel")

    with pytest.raises(DecisionError, match="live category history changed"):
        promote_category_plan(
            client,
            plan,
            plan_path,
            receipt_path,
            tmp_path,
            HASH_KEY,
            environment_fingerprint=environment,
            supplied_plan_fingerprint=plan["planFingerprint"],
            supplied_environment_fingerprint=environment,
            allow_production=True,
            wait_seconds=0,
            generated_at=NOW,
        )

    assert client.assignments["activity-1"] == []
    assert not list(
        (tmp_path / "normalized" / "simplefin").glob("category-promotion-*.json")
    )


def test_no_artifact_contains_merchant_text(tmp_path):
    history = [
        history_activity("h-1", description="Confidential Payee"),
        history_activity("h-2", description="Confidential Payee", date="2026-06-14"),
    ]
    evidence_file = tmp_path / "synthetic-evidence.csv"
    evidence_file.write_text("synthetic", encoding="utf-8")
    plan = build_plan(
        [
            live_activity(
                "activity-1", "monarch:row-9", description="Confidential Payee"
            )
        ],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
        evidence=evidence_binding([evidence_file]),
    )
    report = write_category_review(tmp_path, plan)

    serialized = json.dumps(plan)
    rendered = report.read_text(encoding="utf-8")
    assert plan["metrics"]["liveHistoryCount"] == 1
    for leak in ("Confidential", "confidential", "payee"):
        assert leak not in serialized
        assert leak not in rendered
    assert "Wealthfolio's own category history" in rendered
    assert "Coverage by evidence kind" in rendered


def test_the_review_report_states_why_coverage_stopped(tmp_path):
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14", description="Other Merchant"),
    ]
    plan = build_plan(
        [live_activity("activity-1", "monarch:row-9")],
        history=history,
        history_assignments={row["id"]: spending("groceries") for row in history},
    )
    rendered = write_category_review(tmp_path, plan).read_text(encoding="utf-8")

    assert plan["metrics"]["manualReasonCounts"] == {"no-history": 1}
    assert "| no-history | 1 |" in rendered
    assert "Training exclusions" in rendered


def test_the_live_history_summary_is_privacy_safe(tmp_path):
    plan = live_history_plan(tmp_path)

    summary = summarize_live_history(plan["liveHistory"])

    assert summary.startswith("liveHistory lookbackMonths=24")
    assert "observed=2" in summary
    assert "sealedEvidence=1" in summary


def test_coverage_metrics_separate_every_evidence_kind():
    history = [
        history_activity("h-1"),
        history_activity("h-2", date="2026-06-14"),
        history_activity("h-3", description="Beta Merchant", account_id=LIVE_CARD, kind="EXPENSE"),
        history_activity(
            "h-4",
            description="Beta Merchant",
            account_id=LIVE_CARD,
            kind="EXPENSE",
            date="2026-06-14",
        ),
    ]
    window = [
        live_activity("activity-1", "monarch:row-1"),
        live_activity("activity-2", "monarch:row-9"),
        live_activity("activity-3", "monarch:row-10", description="Beta Merchant"),
    ]
    plan = build_plan(
        window,
        canonical=[
            canonical_row(
                "monarch:row-1", category="Mortgage", category_id="mortgage"
            )
        ],
        history=history,
        history_assignments={
            "h-1": spending("groceries"),
            "h-2": spending("groceries"),
            "h-3": spending("fuel"),
            "h-4": spending("fuel"),
        },
    )

    assert plan["metrics"]["evidenceKindCounts"] == {
        "canonical-category:identity-exact": 1,
        "live-account-history": 1,
        "live-global-history": 1,
    }
    assert plan["metrics"]["liveHistoryCount"] == 2
    assert plan["metrics"]["canonicalCarryoverCount"] == 1
