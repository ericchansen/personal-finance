"""Synthetic coverage for source-agnostic Wealthfolio categorization.

Every fixture here is invented. No file in this module contains a real merchant,
account, balance, or transaction.
"""

import csv
import json
import stat
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from importers.categorize.identity import (
    SourceResolver,
    build_account_bridge,
    build_canonical_index,
    load_canonical_index,
    parse_source_identity,
)
from importers.categorize.merchant_rules import (
    build_merchant_rules,
    write_merchant_rule_review,
    write_merchant_rules,
)
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import instance_fingerprint, plan_fingerprint
from importers.simplefin.categorization import (
    INCOME_TAXONOMY,
    SPENDING_TAXONOMY,
    HistoryIndex,
    build_category_plan,
    evidence_binding,
    in_source_scope,
    load_history,
    normalize_source_systems,
    promote_category_plan,
    rehearse_category_plan,
    sha256_file,
    staging_idempotency_key,
    validate_category_plan,
    write_category_review,
    write_rehearsal_receipt,
)

from tests.test_simplefin_categorization import ProductionCategoryClient, StageClient

NOW = datetime(2026, 8, 28, tzinfo=timezone.utc)
HASH_KEY = b"synthetic-private-hmac-key-00002"
START = "2026-08-01"
END = "2026-08-31"

LIVE_CHECKING = "live-checking"
LIVE_CARD = "live-card"
CANONICAL_CHECKING = "canonical-checking"
CANONICAL_CARD = "canonical-card"

ACCOUNT_MAP = {
    CANONICAL_CHECKING: LIVE_CHECKING,
    CANONICAL_CARD: LIVE_CARD,
}

ACCOUNTS = [
    {"id": LIVE_CHECKING, "accountType": "CASH"},
    {"id": LIVE_CARD, "accountType": "CREDIT_CARD"},
]


def catalogs():
    return {
        SPENDING_TAXONOMY: [
            {"id": "groceries", "name": "Groceries"},
            {"id": "housing", "name": "Rent/Mortgage"},
            {"id": "fuel", "name": "Gas & Fuel"},
            {"id": "other", "name": "Other Expenses"},
        ],
        INCOME_TAXONOMY: [
            {"id": "salary", "name": "Salary"},
            {"id": "refunds", "name": "Refunds & Rebates"},
        ],
    }


def canonical_row(
    source_id,
    *,
    account_id=CANONICAL_CHECKING,
    date="2026-08-12",
    amount="-42.50",
    description="Synthetic Grocer",
    category="Groceries",
    category_id="groceries",
    transaction_kind="expense",
    transfer_group="",
    excluded="false",
    assignment_source="source",
):
    return {
        "date": date,
        "account_id": account_id,
        "amount": amount,
        "description": description,
        "source_id": source_id,
        "source_file": "synthetic",
        "category": category,
        "transfer_group": transfer_group,
        "symbol": "",
        "quantity": "",
        "price": "",
        "external_flow": "False",
        "excluded": excluded,
        "exclusion_reason": "",
        "transaction_kind": transaction_kind,
        "category_id": category_id,
        "payee_normalized": description.casefold(),
        "assignment_source": assignment_source,
        "assignment_rule_id": "",
        "assignment_confidence": "",
        "split_group": "",
    }


def live_activity(
    activity_id,
    idempotency_key,
    *,
    account_id=LIVE_CHECKING,
    kind="WITHDRAWAL",
    amount="42.50",
    date="2026-08-12",
    description="Synthetic Grocer",
    subtype=None,
    metadata=None,
    group=None,
):
    return {
        "id": activity_id,
        "accountId": account_id,
        "activityType": kind,
        "date": f"{date}T00:00:00Z",
        "amount": amount,
        "currency": "USD",
        "comment": description,
        "idempotencyKey": idempotency_key,
        "subtype": subtype,
        "metadata": metadata,
        "sourceGroupId": group,
    }


def build_plan(
    activities,
    canonical,
    *,
    history=None,
    account_map=None,
    decisions=None,
    assignments=None,
    spending_account_ids=None,
    allow_fallback=True,
    source_systems=("*",),
    evidence=(),
):
    index = build_canonical_index(canonical)
    bridge = build_account_bridge(
        canonical_account_map=ACCOUNT_MAP if account_map is None else account_map
    )
    scoped = [row for row in activities if in_source_scope(row, source_systems)]
    resolver = SourceResolver(index, bridge, scoped, allow_fallback=allow_fallback)
    eligible = [
        row
        for row in scoped
        if row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
    ]
    outflow = sum(Decimal(str(row.get("amount") or 0)) for row in eligible)
    return build_category_plan(
        activities,
        ACCOUNTS,
        assignments if assignments is not None else {row["id"]: [] for row in scoped},
        catalogs(),
        {"accounts": []},
        history if history is not None else HistoryIndex(),
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
        spending_account_ids=(
            {LIVE_CHECKING, LIVE_CARD}
            if spending_account_ids is None
            else spending_account_ids
        ),
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
        current_uncategorized_count=len(eligible),
        generated_at=NOW,
        resolver=resolver,
        source_systems=source_systems,
    )


def only_candidate(plan):
    assert plan["metrics"]["autoCount"] == 1, plan["manualItems"]
    return plan["autoCandidates"][0]


def manual_reason(plan, activity_id):
    row = next(
        item for item in plan["manualItems"] if item["activityId"] == activity_id
    )
    return row["reason"]


# ---------------------------------------------------------------------------
# Source identity parsing and scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,system,account,source_key",
    [
        ("simplefin:live-checking:txn-9", "simplefin", "live-checking", "txn-9"),
        ("simplefin:sf-account:txn:9", "simplefin", "sf-account", "txn:9"),
        ("extract:live-checking:FIT-1", "extract", "live-checking", "FIT-1"),
        ("extract:stable:FIT-1", "extract", "stable", "FIT-1"),
        ("monarch:row-77", "monarch", "", "row-77"),
        ("monarch:abcdef:2", "monarch", "", "abcdef:2"),
        ("gap:live-checking:2026-08-15", "gap", "live-checking", "2026-08-15"),
    ],
)
def test_source_identity_parses_every_supported_key_shape(
    key, system, account, source_key
):
    parsed = parse_source_identity(key)

    assert parsed is not None
    assert (parsed.source_system, parsed.account_token, parsed.source_key) == (
        system,
        account,
        source_key,
    )


@pytest.mark.parametrize("key", ["", None, "no-colon", ":leading", "trailing:"])
def test_source_identity_refuses_keys_without_a_usable_system(key):
    assert parse_source_identity(key) is None


def test_source_scope_excludes_reconciliation_and_identityless_activities():
    gap = live_activity("gap-1", "gap:live-checking:2026-08-15")
    manual_entry = live_activity("manual-1", "")

    assert in_source_scope(live_activity("a", "monarch:row-1"), ("*",)) is True
    assert in_source_scope(gap, ("*",)) is False
    assert in_source_scope(manual_entry, ("*",)) is False
    assert in_source_scope(live_activity("a", "monarch:row-1"), ("simplefin",)) is False


def test_source_scope_refuses_to_categorize_reconciliation_sources():
    with pytest.raises(DecisionError, match="reported separately"):
        normalize_source_systems(["gap"])


def test_account_bridge_rejects_a_conflicting_mapping():
    with pytest.raises(DecisionError, match="more than one Wealthfolio account"):
        build_account_bridge(
            canonical_account_map={CANONICAL_CHECKING: LIVE_CHECKING},
            reviewed_plan={
                "accounts": [
                    {
                        "assertionAccountId": CANONICAL_CHECKING,
                        "wealthfolioAccountId": LIVE_CARD,
                    }
                ]
            },
        )


# ---------------------------------------------------------------------------
# Per-source exact identity resolution
# ---------------------------------------------------------------------------


def test_monarch_activity_resolves_by_exact_source_identity():
    canonical = [canonical_row("monarch:row-77")]
    activities = [live_activity("activity-monarch", "monarch:row-77")]

    plan = build_plan(activities, canonical)
    candidate = only_candidate(plan)

    assert candidate["sourceSystem"] == "monarch"
    assert candidate["matchKind"] == "identity-exact"
    assert candidate["categoryId"] == "groceries"
    assert candidate["evidenceKind"] == "canonical-category:identity-exact"
    assert candidate["canonicalAccountId"] == CANONICAL_CHECKING
    assert candidate["sourceAccountId"] == CANONICAL_CHECKING
    assert candidate["sourceId"] == "row-77"
    assert "Synthetic Grocer" not in json.dumps(plan)


def test_mapped_extract_activity_resolves_despite_different_account_scoping():
    canonical = [canonical_row("extract:stable:FIT-1")]
    activities = [
        live_activity("activity-extract", f"extract:{LIVE_CHECKING}:FIT-1")
    ]

    candidate = only_candidate(build_plan(activities, canonical))

    assert candidate["sourceSystem"] == "extract"
    assert candidate["matchKind"] == "identity-exact"
    assert candidate["canonicalSourceId"] == "extract:stable:FIT-1"
    assert candidate["categoryId"] == "groceries"


def test_simplefin_activity_resolves_without_a_reviewed_source_plan():
    canonical = [canonical_row("simplefin:sf-account:txn-3")]
    activities = [
        live_activity("activity-simplefin", f"simplefin:{LIVE_CHECKING}:txn-3")
    ]

    candidate = only_candidate(build_plan(activities, canonical))

    assert candidate["sourceSystem"] == "simplefin"
    # The SimpleFIN source account remains the stable override identity even
    # though no reviewed plan supplied it.
    assert candidate["sourceAccountId"] == "sf-account"
    assert candidate["sourceId"] == "txn-3"


def test_every_supported_source_is_planned_in_one_pass():
    canonical = [
        canonical_row("monarch:row-1", description="Alpha Market"),
        canonical_row("extract:stable:FIT-2", description="Beta Fuel", category="Gas"),
        canonical_row("simplefin:sf-account:txn-1", description="Gamma Store"),
    ]
    activities = [
        live_activity("a-1", "monarch:row-1", description="Alpha Market"),
        live_activity("a-2", f"extract:{LIVE_CHECKING}:FIT-2", description="Beta Fuel"),
        live_activity(
            "a-3", f"simplefin:{LIVE_CHECKING}:txn-1", description="Gamma Store"
        ),
    ]

    plan = build_plan(activities, canonical)

    assert plan["sourceSystems"] == ["*"]
    assert plan["metrics"]["sourceSystemCounts"] == {
        "extract": 1,
        "monarch": 1,
        "simplefin": 1,
    }
    assert plan["metrics"]["autoCount"] == 3
    assert plan["metrics"]["canonicalCarryoverCount"] == 3
    assert {row["categoryId"] for row in plan["autoCandidates"]} == {
        "groceries",
        "fuel",
    }


# ---------------------------------------------------------------------------
# Reviewed canonical carryover
# ---------------------------------------------------------------------------


def test_mortgage_category_carries_over_from_legacy_history():
    canonical = [
        canonical_row(
            "monarch:row-mortgage",
            description="Synthetic Mortgage Servicer",
            category="Mortgage",
            category_id="mortgage",
            amount="-1850.00",
        )
    ]
    activities = [
        live_activity(
            "activity-mortgage",
            "monarch:row-mortgage",
            description="Synthetic Mortgage Servicer",
            amount="1850.00",
        )
    ]

    candidate = only_candidate(build_plan(activities, canonical))

    assert candidate["categoryId"] == "housing"
    assert candidate["categoryName"] == "Rent/Mortgage"
    assert candidate["confidence"] == "0.99"


def test_canonical_carryover_outranks_a_conflicting_merchant_consensus():
    canonical = [canonical_row("monarch:row-9", category="Mortgage", category_id="mortgage")]
    history = HistoryIndex()
    history.add(CANONICAL_CHECKING, "Synthetic Grocer", "Groceries", "h-1")
    history.add(CANONICAL_CHECKING, "Synthetic Grocer", "Groceries", "h-2")
    activities = [live_activity("activity-9", "monarch:row-9")]

    candidate = only_candidate(build_plan(activities, canonical, history=history))

    assert candidate["categoryId"] == "housing"


def test_canonical_category_is_ignored_when_the_row_is_not_reviewed_as_spending():
    # The canonical builder writes no ``category_id`` for a structural row, so a
    # stray category label cannot masquerade as a reviewed decision.
    canonical = [
        canonical_row(
            "monarch:row-10",
            category="Groceries",
            category_id="",
            transaction_kind="",
        )
    ]
    activities = [live_activity("activity-10", "monarch:row-10")]

    plan = build_plan(activities, canonical)

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-10") == "no-history"


# ---------------------------------------------------------------------------
# Merchant consensus across sources
# ---------------------------------------------------------------------------


def test_recurring_merchant_consensus_spans_every_source(tmp_path):
    canonical_path = tmp_path / "transactions.csv"
    rows = [
        canonical_row("monarch:row-1", date="2026-05-02"),
        canonical_row("extract:stable:FIT-9", date="2026-06-02"),
        # The live activity's own canonical row carries no reviewed category,
        # so only the cross-source merchant consensus can categorize it.
        canonical_row(
            "simplefin:sf-account:txn-5",
            date="2026-08-12",
            category="",
            category_id="",
        ),
    ]
    _write_canonical(canonical_path, rows)
    history = load_history(canonical_path, None)
    activities = [
        live_activity("activity-5", f"simplefin:{LIVE_CHECKING}:txn-5")
    ]

    candidate = only_candidate(build_plan(activities, rows, history=history))

    assert candidate["evidenceKind"] == "account-history"
    assert candidate["evidenceCount"] == 2
    assert candidate["categoryId"] == "groceries"


def test_single_cross_source_observation_stays_manual(tmp_path):
    canonical_path = tmp_path / "transactions.csv"
    rows = [
        canonical_row("monarch:row-1", date="2026-05-02"),
        canonical_row(
            "simplefin:sf-account:txn-5", date="2026-08-12", category="", category_id=""
        ),
    ]
    _write_canonical(canonical_path, rows)
    history = load_history(canonical_path, None)
    activities = [live_activity("activity-5", f"simplefin:{LIVE_CHECKING}:txn-5")]

    plan = build_plan(activities, rows, history=history)

    assert manual_reason(plan, "activity-5") == "insufficient-history"


# ---------------------------------------------------------------------------
# Direction: deposits, income, refunds and credits
# ---------------------------------------------------------------------------


def test_deposit_income_activity_uses_the_income_taxonomy():
    canonical = [
        canonical_row(
            "monarch:row-pay",
            amount="3200.00",
            description="Synthetic Employer",
            category="Paychecks",
            category_id="paychecks",
            transaction_kind="income",
        )
    ]
    activities = [
        live_activity(
            "activity-pay",
            "monarch:row-pay",
            kind="DEPOSIT",
            amount="3200.00",
            description="Synthetic Employer",
        )
    ]

    plan = build_plan(activities, canonical)
    candidate = only_candidate(plan)

    assert candidate["taxonomyId"] == INCOME_TAXONOMY
    assert candidate["categoryId"] == "salary"
    assert candidate["reportAmount"] == "3200.00"
    assert plan["cashFlowExcludingTransfersAndReconciliation"]["income"] == "3200.00"


def test_credit_card_refund_reports_as_negative_spending():
    canonical = [
        canonical_row(
            "monarch:row-refund",
            account_id=CANONICAL_CARD,
            amount="19.99",
            category="Groceries",
            category_id="groceries",
            transaction_kind="refund",
        )
    ]
    activities = [
        live_activity(
            "activity-refund",
            "monarch:row-refund",
            account_id=LIVE_CARD,
            kind="CREDIT",
            amount="19.99",
        )
    ]

    plan = build_plan(activities, canonical)
    candidate = only_candidate(plan)

    assert candidate["taxonomyId"] == SPENDING_TAXONOMY
    assert candidate["categoryId"] == "groceries"
    assert candidate["reportAmount"] == "-19.99"
    assert plan["cashFlowExcludingTransfersAndReconciliation"]["spending"] == "-19.99"


def test_cash_refund_without_an_income_mapping_abstains_instead_of_guessing():
    canonical = [
        canonical_row(
            "monarch:row-cash-refund",
            amount="25.00",
            category="Groceries",
            category_id="groceries",
            transaction_kind="refund",
        )
    ]
    activities = [
        live_activity(
            "activity-cash-refund",
            "monarch:row-cash-refund",
            kind="CREDIT",
            amount="25.00",
        )
    ]

    plan = build_plan(activities, canonical)

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-cash-refund") == "credit-direction-review"


def test_cash_refund_with_an_income_mapping_is_categorized():
    canonical = [
        canonical_row(
            "monarch:row-rebate",
            amount="25.00",
            category="Refunds & Rebates",
            category_id="refunds_rebates",
            transaction_kind="refund",
        )
    ]
    activities = [
        live_activity(
            "activity-rebate", "monarch:row-rebate", kind="CREDIT", amount="25.00"
        )
    ]

    candidate = only_candidate(build_plan(activities, canonical))

    assert candidate["taxonomyId"] == INCOME_TAXONOMY
    assert candidate["categoryId"] == "refunds"


# ---------------------------------------------------------------------------
# Structural kinds are decided before any category lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,classification",
    [
        ("internal_transfer", "confirmed-internal-transfer"),
        ("cc_payment", "card-payment"),
        ("loan_payment", "loan-payment"),
        ("saving", "saving-transfer"),
        ("investment", "investment-transfer"),
        ("reconciliation", "canonical-reconciliation"),
        ("excluded", "canonically-excluded"),
    ],
)
def test_structural_canonical_kinds_are_never_purchases(kind, classification):
    canonical = [
        canonical_row(
            "monarch:row-structural",
            category="Groceries",
            category_id="groceries",
            transaction_kind=kind,
        )
    ]
    history = HistoryIndex()
    history.add(CANONICAL_CHECKING, "Synthetic Grocer", "Groceries", "h-1")
    history.add(CANONICAL_CHECKING, "Synthetic Grocer", "Groceries", "h-2")
    activities = [live_activity("activity-structural", "monarch:row-structural")]

    plan = build_plan(activities, canonical, history=history)

    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["structuralKindCount"] == 1
    row = plan["transfersAndReconciliation"][0]
    assert row["classification"] == classification
    assert row["categoryAction"] == "none"
    # A structural row is not consumption, so it never enters the eligible pool.
    assert plan["metrics"]["eligibleAmount"] == "0.00"


def test_balance_gap_activity_stays_reconciliation_across_sources():
    canonical = [canonical_row("monarch:row-1")]
    activities = [
        live_activity("activity-1", "monarch:row-1"),
        live_activity(
            "activity-gap",
            f"gap:{LIVE_CHECKING}:2026-08-15",
            kind="TRANSFER_IN",
            amount="500.00",
            date="2026-08-15",
        ),
    ]

    plan = build_plan(activities, canonical)

    assert plan["metrics"]["balanceGapCount"] == 1
    assert plan["balanceGapReconciliation"][0]["categoryAction"] == "none"
    assert {row["activityId"] for row in plan["autoCandidates"]} == {"activity-1"}


def test_live_transfer_activity_is_never_categorized():
    canonical = [
        canonical_row("monarch:row-xfer", category="Groceries", category_id="groceries")
    ]
    activities = [
        live_activity("activity-xfer", "monarch:row-xfer", kind="TRANSFER_OUT")
    ]

    plan = build_plan(activities, canonical)

    assert plan["metrics"]["autoCount"] == 0
    assert plan["transfersAndReconciliation"][0]["classification"] == "unpaired-transfer"


# ---------------------------------------------------------------------------
# Conservative fallback matching
# ---------------------------------------------------------------------------


def test_unique_fallback_match_resolves_a_synthesized_extract_id():
    # The canonical build and the Wealthfolio import synthesize different ids
    # for the same CSV row, so only account/date/amount/description can join.
    canonical = [canonical_row("extract:synthetic:canonical-side-hash")]
    activities = [
        live_activity("activity-fallback", f"extract:{LIVE_CHECKING}:live-side-hash")
    ]

    candidate = only_candidate(build_plan(activities, canonical))

    assert candidate["matchKind"] == "fallback-unique"
    assert candidate["categoryId"] == "groceries"


def test_fallback_can_be_disabled_for_an_exact_identity_only_plan():
    canonical = [canonical_row("extract:synthetic:canonical-side-hash")]
    activities = [
        live_activity("activity-fallback", f"extract:{LIVE_CHECKING}:live-side-hash")
    ]

    plan = build_plan(activities, canonical, allow_fallback=False)

    assert manual_reason(plan, "activity-fallback") == "unresolved-source-identity"


def test_two_identical_live_activities_abstain_from_a_fallback_match():
    canonical = [canonical_row("extract:synthetic:canonical-side-hash")]
    activities = [
        live_activity("activity-a", f"extract:{LIVE_CHECKING}:hash-a"),
        live_activity("activity-b", f"extract:{LIVE_CHECKING}:hash-b"),
    ]

    plan = build_plan(activities, canonical)

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-a") == "ambiguous-fallback-match"
    assert manual_reason(plan, "activity-b") == "ambiguous-fallback-match"
    assert plan["metrics"]["unresolvedIdentityCount"] == 2


def test_two_identical_canonical_rows_abstain_from_a_fallback_match():
    canonical = [
        canonical_row("extract:synthetic:hash-1"),
        canonical_row("extract:synthetic:hash-2"),
    ]
    activities = [
        live_activity("activity-fallback", f"extract:{LIVE_CHECKING}:live-hash")
    ]

    plan = build_plan(activities, canonical)

    assert manual_reason(plan, "activity-fallback") == "ambiguous-fallback-match"


def test_unmapped_account_blocks_a_fallback_match():
    canonical = [canonical_row("extract:synthetic:canonical-side-hash")]
    activities = [
        live_activity("activity-fallback", f"extract:{LIVE_CHECKING}:live-hash")
    ]

    plan = build_plan(activities, canonical, account_map={})

    assert manual_reason(plan, "activity-fallback") == "unmapped-account"


def test_ambiguous_source_identity_is_narrowed_by_account_then_abstains():
    shared = [
        canonical_row("extract:stable:SHARED", account_id=CANONICAL_CHECKING),
        canonical_row("extract:stable:SHARED", account_id=CANONICAL_CARD),
    ]
    narrowed = build_plan(
        [live_activity("activity-narrow", f"extract:{LIVE_CHECKING}:SHARED")], shared
    )
    assert only_candidate(narrowed)["matchKind"] == "identity-account"

    unmapped = build_plan(
        [live_activity("activity-wide", f"extract:{LIVE_CHECKING}:SHARED")],
        shared,
        account_map={},
    )
    assert manual_reason(unmapped, "activity-wide") == "ambiguous-source-identity"


def test_one_canonical_row_is_never_claimed_by_two_live_activities():
    canonical = [canonical_row("monarch:row-shared")]
    activities = [
        live_activity("activity-first", "monarch:row-shared"),
        live_activity("activity-second", "monarch:row-shared"),
    ]

    plan = build_plan(activities, canonical)

    # Neither activity wins: the outcome must not depend on which one the API
    # happened to list first.
    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-first") == "ambiguous-canonical-claim"
    assert manual_reason(plan, "activity-second") == "ambiguous-canonical-claim"


def test_an_exact_identity_match_outranks_a_competing_fallback_match():
    # The fallback candidate is listed first, so a single-pass resolver would
    # let it claim the canonical row and strand the exact match.
    canonical = [canonical_row("extract:stable:FIT-1")]
    activities = [
        live_activity("activity-fallback", f"extract:{LIVE_CHECKING}:synthesized"),
        live_activity("activity-exact", f"extract:{LIVE_CHECKING}:FIT-1"),
    ]

    plan = build_plan(activities, canonical)
    candidate = only_candidate(plan)

    assert candidate["activityId"] == "activity-exact"
    assert candidate["matchKind"] == "identity-exact"
    assert manual_reason(plan, "activity-fallback") == "unresolved-source-identity"


def test_unknown_source_system_without_evidence_is_reported_rather_than_guessed():
    canonical = [canonical_row("monarch:row-1", date="2026-08-02", amount="-10.00")]
    activities = [live_activity("activity-vendor", "vendorx:opaque-1")]

    plan = build_plan(activities, canonical)

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-vendor") == "unresolved-source-identity"


def test_unknown_source_system_still_joins_a_unique_fallback_match():
    # An importer this repository does not know about is not a reason to give
    # up: a unique account/date/amount/description match is still exact.
    canonical = [canonical_row("monarch:row-1")]
    activities = [live_activity("activity-vendor", "vendorx:opaque-1")]

    candidate = only_candidate(build_plan(activities, canonical))

    assert candidate["sourceSystem"] == "vendorx"
    assert candidate["matchKind"] == "fallback-unique"


def test_unmapped_category_stays_reviewable():
    canonical = [
        canonical_row(
            "monarch:row-1", category="Untranslatable", category_id="untranslatable"
        )
    ]
    activities = [live_activity("activity-1", "monarch:row-1")]

    plan = build_plan(activities, canonical)

    assert plan["metrics"]["autoCount"] == 0
    assert manual_reason(plan, "activity-1") == "unmapped-category"


# ---------------------------------------------------------------------------
# Learned merchant rules
# ---------------------------------------------------------------------------


def _write_canonical(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_merchant_rules_learn_unanimous_history_with_cross_source_provenance(tmp_path):
    rows = [
        canonical_row("monarch:row-1", date="2026-05-02"),
        canonical_row("extract:stable:FIT-1", date="2026-06-03"),
        canonical_row("simplefin:sf-account:txn-1", date="2026-08-12"),
    ]
    path = _write_canonical(tmp_path / "transactions.csv", rows)
    history = load_history(path, None)

    document = build_merchant_rules(history, HASH_KEY, generated_at=NOW)

    account_rules = [row for row in document["rules"] if row["scope"] == "account"]
    assert len(account_rules) == 1
    rule = account_rules[0]
    assert rule["category"] == "Groceries"
    assert rule["evidenceCount"] == 3
    assert rule["sourceSystems"] == ["extract", "monarch", "simplefin"]
    assert rule["firstSeen"] == "2026-05-02"
    assert rule["lastSeen"] == "2026-08-12"
    assert rule["confidence"] == "0.98"
    assert document["metrics"]["globalRuleCount"] == 1
    assert document["precedence"] == ["account", "global"]


def test_merchant_rules_hold_back_below_the_evidence_threshold(tmp_path):
    rows = [canonical_row("monarch:row-1")]
    path = _write_canonical(tmp_path / "transactions.csv", rows)

    document = build_merchant_rules(
        load_history(path, None), HASH_KEY, generated_at=NOW
    )

    assert document["rules"] == []
    assert document["metrics"]["pendingRuleCount"] == 2
    assert {row["evidenceCount"] for row in document["pendingRules"]} == {1}


def test_merchant_rules_never_learn_a_conflicting_merchant(tmp_path):
    rows = [
        canonical_row("monarch:row-1", category="Groceries"),
        canonical_row("monarch:row-2", category="Mortgage", date="2026-06-01"),
        canonical_row("monarch:row-3", category="Groceries", date="2026-07-01"),
    ]
    path = _write_canonical(tmp_path / "transactions.csv", rows)

    document = build_merchant_rules(
        load_history(path, None), HASH_KEY, generated_at=NOW
    )

    assert document["rules"] == []
    assert document["metrics"]["conflictCount"] >= 1
    assert all(row["categoryCount"] > 1 for row in document["conflicts"])


def test_merchant_rule_artifacts_contain_no_merchant_text(tmp_path):
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()
    rows = [
        canonical_row("monarch:row-1", description="Synthetic Grocer"),
        canonical_row("extract:stable:FIT-1", description="Synthetic Grocer"),
    ]
    path = _write_canonical(tmp_path / "transactions.csv", rows)
    document = build_merchant_rules(
        load_history(path, None), HASH_KEY, generated_at=NOW
    )

    json_path = write_merchant_rules(data_dir, document)
    review_path = write_merchant_rule_review(data_dir, document)

    for written in (json_path, review_path):
        text = written.read_text(encoding="utf-8")
        assert "Synthetic Grocer" not in text
        assert "synthetic grocer" not in text.casefold()
    assert "Groceries" in review_path.read_text(encoding="utf-8")


def test_merchant_rule_set_fingerprint_covers_its_content(tmp_path):
    rows = [
        canonical_row("monarch:row-1"),
        canonical_row("extract:stable:FIT-1", date="2026-06-03"),
    ]
    path = _write_canonical(tmp_path / "transactions.csv", rows)
    document = build_merchant_rules(
        load_history(path, None), HASH_KEY, generated_at=NOW
    )

    recomputed = plan_fingerprint(
        {
            key: value
            for key, value in document.items()
            if key != "ruleSetFingerprint"
        }
    )

    assert document["ruleSetFingerprint"] == recomputed


def test_learned_rules_state_exactly_what_the_plan_decides(tmp_path):
    # The rule set is the auditable rendering of the same history the plan
    # resolves against, so a learned rule and the plan candidate it explains
    # must agree on merchant digest, category, evidence count and confidence.
    rows = [
        canonical_row("monarch:row-1", date="2026-05-02"),
        canonical_row("extract:stable:FIT-1", date="2026-06-03"),
        canonical_row(
            "simplefin:sf-account:txn-5",
            date="2026-08-12",
            category="",
            category_id="",
        ),
    ]
    path = _write_canonical(tmp_path / "transactions.csv", rows)
    history = load_history(path, None)
    document = build_merchant_rules(history, HASH_KEY, generated_at=NOW)
    activities = [live_activity("activity-5", f"simplefin:{LIVE_CHECKING}:txn-5")]

    candidate = only_candidate(build_plan(activities, rows, history=history))
    rule = next(
        row
        for row in document["rules"]
        if row["scope"] == "account"
        and row["canonicalAccountId"] == CANONICAL_CHECKING
    )

    assert rule["merchantHash"] == candidate["merchantHash"]
    assert rule["category"] == "Groceries"
    assert rule["evidenceCount"] == candidate["evidenceCount"]
    assert rule["confidence"] == candidate["confidence"]


def test_history_provenance_separates_account_and_global_observations():
    history = HistoryIndex()
    history.add(
        CANONICAL_CHECKING,
        "Synthetic Grocer",
        "Groceries",
        "monarch:row-1",
        when="2026-05-02",
    )
    history.add(
        CANONICAL_CARD,
        "Synthetic Grocer",
        "Groceries",
        "monarch:row-1",
        when="2026-06-02",
    )

    account = history.evidence(CANONICAL_CHECKING, "synthetic grocer", "Groceries")
    everywhere = history.evidence("", "synthetic grocer", "Groceries")

    assert account.evidence_count == 1
    assert account.source_systems == ("monarch",)
    assert account.first_seen == "2026-05-02"
    # The same source id in two accounts is two independent global observations.
    assert everywhere.evidence_count == 2
    assert everywhere.last_seen == "2026-06-02"


# ---------------------------------------------------------------------------
# Plan artifacts and privacy
# ---------------------------------------------------------------------------


def test_canonical_index_loads_from_the_private_csv(tmp_path):
    rows = [canonical_row("monarch:row-1"), canonical_row("extract:stable:FIT-1")]
    path = _write_canonical(tmp_path / "transactions.csv", rows)

    index = load_canonical_index(path)

    assert index.source_systems == {"monarch", "extract"}
    assert index.by_identity[("monarch", "row-1")][0].categorizable is True


def test_multi_source_review_report_redacts_every_merchant(tmp_path):
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()
    canonical = [
        canonical_row("monarch:row-1", description="Synthetic Grocer"),
        canonical_row(
            "extract:stable:FIT-1", description="Synthetic Fuel", category="Mortgage"
        ),
    ]
    activities = [
        live_activity("activity-1", "monarch:row-1", description="Synthetic Grocer"),
        live_activity(
            "activity-2",
            f"extract:{LIVE_CHECKING}:FIT-1",
            description="Synthetic Fuel",
        ),
    ]
    plan = build_plan(activities, canonical)

    report = write_category_review(data_dir, plan).read_text(encoding="utf-8")

    assert "Synthetic Grocer" not in report
    assert "Synthetic Fuel" not in report
    assert "Rent/Mortgage" in report


def test_source_agnostic_plan_validates_and_seals_its_scope(tmp_path):
    evidence_file = tmp_path / "synthetic-evidence.csv"
    evidence_file.write_text("synthetic", encoding="utf-8")
    canonical = [canonical_row("monarch:row-1")]
    activities = [live_activity("activity-1", "monarch:row-1")]

    plan = build_plan(
        activities, canonical, evidence=evidence_binding([evidence_file])
    )
    validate_category_plan(plan)

    assert plan["sourceSystems"] == ["*"]
    assert plan["preCategoryActivityIds"] == ["activity-1"]


# ---------------------------------------------------------------------------
# Portable staging rehearsal and production promotion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ({"sourceId": "txn-1"}, "simplefin:stage-account:txn-1"),
        (
            {"sourceSystem": "simplefin", "sourceId": "txn-1"},
            "simplefin:stage-account:txn-1",
        ),
        (
            {"sourceSystem": "extract", "sourceId": "FIT-1"},
            "extract:stage-account:FIT-1",
        ),
        ({"sourceSystem": "monarch", "sourceId": "row-1"}, "monarch:row-1"),
    ],
)
def test_staging_key_is_portable_and_defaults_to_simplefin(candidate, expected):
    assert staging_idempotency_key(candidate, "stage-account") == expected


def multi_source_plan(tmp_path):
    evidence_file = tmp_path / "synthetic-evidence.csv"
    evidence_file.write_text("synthetic", encoding="utf-8")
    canonical = [
        canonical_row("monarch:row-1", description="Alpha Market"),
        canonical_row("extract:stable:FIT-1", description="Beta Market"),
    ]
    activities = [
        live_activity("activity-1", "monarch:row-1", description="Alpha Market"),
        live_activity(
            "activity-2", f"extract:{LIVE_CHECKING}:FIT-1", description="Beta Market"
        ),
    ]
    plan = build_plan(
        activities, canonical, evidence=evidence_binding([evidence_file])
    )
    assert plan["metrics"]["autoCount"] == 2
    return plan, activities


def staged_rows(activities):
    rows = []
    for activity in activities:
        row = dict(activity)
        row["id"] = f"staging-{activity['id']}"
        row["accountId"] = "stage-account"
        parsed = parse_source_identity(activity["idempotencyKey"])
        row["idempotencyKey"] = staging_idempotency_key(
            {"sourceSystem": parsed.source_system, "sourceId": parsed.source_key},
            "stage-account",
        )
        rows.append(row)
    return rows


def test_multi_source_staging_rehearsal_applies_and_is_idempotent(tmp_path):
    plan, activities = multi_source_plan(tmp_path)
    client = StageClient(staged_rows(activities))

    first = rehearse_category_plan(
        client, plan, {CANONICAL_CHECKING: "stage-account"}, HASH_KEY, generated_at=NOW
    )
    second = rehearse_category_plan(
        client, plan, {CANONICAL_CHECKING: "stage-account"}, HASH_KEY, generated_at=NOW
    )

    assert first["status"] == "applied"
    assert first["appliedCount"] == 2
    assert second["status"] == "already-applied"
    assert client.puts == 2


def test_multi_source_staging_rehearsal_rolls_back_a_partial_failure(tmp_path):
    plan, activities = multi_source_plan(tmp_path)
    client = StageClient(staged_rows(activities), fail_on_put=2)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        rehearse_category_plan(
            client,
            plan,
            {CANONICAL_CHECKING: "stage-account"},
            HASH_KEY,
            generated_at=NOW,
        )

    assert all(rows == [] for rows in client.assignments.values())
    assert client.deletes == 1


def test_multi_source_rehearsal_requires_a_matching_staging_activity(tmp_path):
    plan, activities = multi_source_plan(tmp_path)
    rows = staged_rows(activities)
    rows[1]["idempotencyKey"] = "extract:stage-account:WRONG"
    client = StageClient(rows)

    with pytest.raises(DecisionError, match="missing from staging"):
        rehearse_category_plan(
            client,
            plan,
            {CANONICAL_CHECKING: "stage-account"},
            HASH_KEY,
            generated_at=NOW,
        )


def multi_source_promotion_bundle(tmp_path):
    plan, activities = multi_source_plan(tmp_path)
    client = ProductionCategoryClient(activities)
    environment = instance_fingerprint(client, "http://127.0.0.1:8088")
    plan["environmentFingerprint"] = environment
    plan["planFingerprint"] = plan_fingerprint(
        {key: value for key, value in plan.items() if key != "planFingerprint"}
    )
    plan_path = tmp_path / "category-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    staging = StageClient(staged_rows(activities))
    rehearsal = rehearse_category_plan(
        staging,
        plan,
        {CANONICAL_CHECKING: "stage-account"},
        HASH_KEY,
        generated_at=NOW,
    )
    receipt_path = write_rehearsal_receipt(tmp_path, rehearsal, plan_path)
    return client, plan, plan_path, receipt_path, environment


def run_promotion(client, plan, plan_path, receipt_path, environment, tmp_path):
    return promote_category_plan(
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


def test_multi_source_promotion_applies_and_writes_an_immutable_receipt(tmp_path):
    bundle = multi_source_promotion_bundle(tmp_path)
    client, plan, plan_path, receipt_path, environment = bundle

    receipt, written, already = run_promotion(
        client, plan, plan_path, receipt_path, environment, tmp_path
    )

    assert already is False
    assert client.assignments["activity-1"] == [
        {"taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}
    ]
    assert client.assignments["activity-2"] == [
        {"taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}
    ]
    assert receipt["afterReport"] == plan["expectedWealthfolioWindow"]
    assert receipt["categoryPlanSha256"] == sha256_file(plan_path)
    assert written.is_file()
    assert not written.stat().st_mode & stat.S_IWRITE


def test_multi_source_promotion_is_idempotent(tmp_path):
    bundle = multi_source_promotion_bundle(tmp_path)
    client, plan, plan_path, receipt_path, environment = bundle
    first, first_path, first_already = run_promotion(
        client, plan, plan_path, receipt_path, environment, tmp_path
    )
    puts = client.puts

    second, second_path, second_already = run_promotion(
        client, plan, plan_path, receipt_path, environment, tmp_path
    )

    assert (first_already, second_already) == (False, True)
    assert second == first
    assert second_path == first_path
    assert client.puts == puts


def test_multi_source_promotion_rolls_back_a_committed_write_failure(tmp_path):
    bundle = multi_source_promotion_bundle(tmp_path)
    client, plan, plan_path, receipt_path, environment = bundle
    client.fail_on_put = 2
    client.commit_then_fail = True

    with pytest.raises(RuntimeError, match="synthetic failure"):
        run_promotion(client, plan, plan_path, receipt_path, environment, tmp_path)

    assert client.assignments["activity-1"] == []
    assert client.assignments["activity-2"] == []
    assert not list(
        (tmp_path / "normalized" / "simplefin").glob("category-promotion-*.json")
    )


def test_legacy_simplefin_plan_scope_is_unchanged():
    canonical = [canonical_row("simplefin:sf-account:txn-1")]
    activities = [
        live_activity("activity-1", "monarch:row-1"),
        live_activity("activity-2", f"simplefin:{LIVE_CHECKING}:txn-1"),
    ]

    plan = build_plan(activities, canonical, source_systems=("simplefin",))

    assert plan["sourceSystems"] == ["simplefin"]
    assert plan["metrics"]["scopedActivities"] == 1
    assert {row["activityId"] for row in plan["autoCandidates"]} == {"activity-2"}
    # The legacy scope keeps the original candidate shape exactly.
    assert "sourceSystem" not in plan["autoCandidates"][0]
