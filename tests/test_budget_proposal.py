"""Synthetic tests for proposal-only budget seeding.

Every row below is invented. The module under test must never contact
Wealthfolio and never write a budget, so these tests assert both the maths and
that nothing leaks a category name or an amount into a printable summary.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from importers.rebuild.decisions import DecisionError
from importers.simplefin import budget_proposal
from importers.simplefin.budget_proposal import (
    BUDGETABLE_KINDS,
    OFFSET_KINDS,
    iter_budgetable_rows,
    load_canonical_transactions,
    propose_budget,
    summarize_proposal,
    write_budget_proposal,
)


def row(
    date: str,
    amount: str,
    category: str,
    *,
    kind: str = "expense",
    excluded: str = "",
    transfer_group: str = "",
) -> dict:
    return {
        "date": date,
        "account_id": "synthetic-account",
        "amount": amount,
        "description": "SYNTHETIC MERCHANT",
        "category": category,
        "transaction_kind": kind,
        "excluded": excluded,
        "transfer_group": transfer_group,
    }


def months(*values: str) -> list[dict]:
    return [row(f"{value}-05", "-100.00", "Groceries") for value in values]


def test_only_expense_and_offset_kinds_are_budgetable():
    assert BUDGETABLE_KINDS == {"expense"}
    assert OFFSET_KINDS == {"refund", "reimbursement"}


def test_iter_budgetable_rows_filters_movements_and_noise():
    rows = [
        row("2024-01-05", "-10.00", "Groceries"),
        row("2024-01-06", "-10.00", "Groceries", kind="internal_transfer"),
        row("2024-01-07", "-10.00", "Groceries", kind="cc_payment"),
        row("2024-01-08", "-10.00", "Groceries", kind="saving"),
        row("2024-01-09", "-10.00", "Groceries", kind="investment"),
        row("2024-01-10", "-10.00", "Groceries", excluded="true"),
        row("2024-01-11", "-10.00", "Groceries", transfer_group="tg-1"),
        row("2024-01-12", "-10.00", ""),
        row("2024-01-13", "5.00", "Groceries", kind="refund"),
    ]
    kept = list(iter_budgetable_rows(rows))
    assert len(kept) == 2
    assert {item["transaction_kind"] for item in kept} == {"expense", "refund"}


def test_median_is_computed_per_category_and_rounded_up():
    rows = [
        row("2024-01-05", "-100.00", "Groceries"),
        row("2024-02-05", "-120.00", "Groceries"),
        row("2024-03-05", "-110.00", "Groceries"),
        row("2024-04-05", "-1.00", "Groceries"),
    ]
    proposal = propose_budget(rows, as_of="2024-04-15")
    assert proposal["monthsAnalyzed"] == ["2024-01", "2024-02", "2024-03"]
    groceries = proposal["categories"][0]
    assert groceries["category"] == "Groceries"
    assert groceries["median_monthly"] == "110.00"
    assert groceries["proposed_monthly"] == "110.00"
    assert groceries["minimum_monthly"] == "100.00"
    assert groceries["maximum_monthly"] == "120.00"
    assert groceries["months_observed"] == 3


def test_proposal_rounds_up_to_the_configured_step():
    rows = [
        row("2024-01-05", "-101.00", "Dining"),
        row("2024-02-05", "-101.00", "Dining"),
        row("2024-03-05", "-101.00", "Dining"),
        row("2024-04-05", "-1.00", "Dining"),
    ]
    dining = propose_budget(rows, as_of="2024-04-15")["categories"][0]
    assert dining["median_monthly"] == "101.00"
    assert dining["proposed_monthly"] == "105.00"


def test_the_current_partial_month_is_never_analyzed():
    rows = [
        row("2024-01-05", "-100.00", "Groceries"),
        row("2024-02-05", "-100.00", "Groceries"),
        row("2024-03-05", "-100.00", "Groceries"),
        row("2024-04-01", "-3.00", "Groceries"),
    ]
    proposal = propose_budget(rows, as_of="2024-04-20")
    assert "2024-04" not in proposal["monthsAnalyzed"]
    assert "2024-04" in proposal["monthsAvailable"]


def test_refunds_offset_spending_in_the_same_month():
    rows = [
        row("2024-01-05", "-100.00", "Clothing"),
        row("2024-01-20", "40.00", "Clothing", kind="refund"),
        row("2024-02-05", "-100.00", "Clothing"),
        row("2024-03-05", "-100.00", "Clothing"),
        row("2024-04-05", "-1.00", "Clothing"),
    ]
    clothing = propose_budget(rows, as_of="2024-04-15")["categories"][0]
    assert clothing["minimum_monthly"] == "60.00"
    assert clothing["median_monthly"] == "100.00"


def test_a_month_fully_offset_to_zero_is_not_counted():
    rows = [
        row("2024-01-05", "-50.00", "Travel"),
        row("2024-01-06", "50.00", "Travel", kind="reimbursement"),
        row("2024-02-05", "-50.00", "Travel"),
        row("2024-03-05", "-50.00", "Travel"),
        row("2024-04-05", "-1.00", "Travel"),
    ]
    proposal = propose_budget(rows, min_months=2, as_of="2024-04-15")
    assert proposal["categories"][0]["months_observed"] == 2


def test_categories_below_the_minimum_history_are_reported_not_guessed():
    rows = [
        row("2024-01-05", "-100.00", "Pets"),
        row("2024-02-05", "-100.00", "Pets"),
        row("2024-03-05", "-1.00", "Pets"),
    ]
    proposal = propose_budget(rows, min_months=3, as_of="2024-03-15")
    assert proposal["categories"] == []
    assert proposal["insufficientHistory"] == [
        {"category": "Pets", "monthsObserved": 2, "minimumMonths": 3}
    ]


def test_months_window_limits_the_analysis_to_recent_behaviour():
    rows = [
        row("2024-01-05", "-500.00", "Groceries"),
        row("2024-02-05", "-100.00", "Groceries"),
        row("2024-03-05", "-100.00", "Groceries"),
        row("2024-04-05", "-100.00", "Groceries"),
        row("2024-05-05", "-1.00", "Groceries"),
    ]
    proposal = propose_budget(rows, months=3, as_of="2024-05-15")
    assert proposal["monthsAnalyzed"] == ["2024-02", "2024-03", "2024-04"]
    assert proposal["categories"][0]["maximum_monthly"] == "100.00"


def test_empty_history_produces_an_empty_but_well_formed_proposal():
    proposal = propose_budget([])
    assert proposal["status"] == "proposal-only"
    assert proposal["categories"] == []
    assert proposal["monthsAnalyzed"] == []
    assert proposal["fingerprint"]


def test_invalid_amounts_and_dates_fail_loudly():
    with pytest.raises(DecisionError, match="non-numeric"):
        propose_budget([row("2024-01-05", "abc", "Groceries")])
    with pytest.raises(DecisionError, match="unparseable date"):
        propose_budget([row("01/05/2024", "-10.00", "Groceries")])


def test_invalid_windows_are_rejected():
    with pytest.raises(DecisionError):
        propose_budget(months("2024-01", "2024-02"), months=0)
    with pytest.raises(DecisionError):
        propose_budget(months("2024-01", "2024-02"), min_months=0)


def test_summary_line_never_leaks_a_category_or_an_amount():
    rows = [
        row("2024-01-05", "-100.00", "Confidential Category"),
        row("2024-02-05", "-100.00", "Confidential Category"),
        row("2024-03-05", "-100.00", "Confidential Category"),
        row("2024-04-05", "-1.00", "Confidential Category"),
    ]
    summary = summarize_proposal(propose_budget(rows, as_of="2024-04-15"))
    assert "Confidential" not in summary
    assert "100" not in summary
    assert "1 categories proposed" in summary
    assert "nothing written" in summary


def test_proposal_never_imports_a_wealthfolio_client():
    """Structural guarantee that a proposal cannot become a write."""
    source = Path(budget_proposal.__file__).read_text(encoding="utf-8")
    imports = [
        line
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    assert imports
    for line in imports:
        assert "wealthfolio_client" not in line
        assert "spending_adapter" not in line


def test_load_canonical_transactions_round_trips(tmp_path: Path):
    path = tmp_path / "transactions.csv"
    rows = [row("2024-01-05", "-100.00", "Groceries")]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert load_canonical_transactions(path)[0]["category"] == "Groceries"
    with pytest.raises(DecisionError):
        load_canonical_transactions(tmp_path / "missing.csv")


def test_proposal_is_written_only_under_the_private_data_directory(tmp_path: Path):
    data_dir = tmp_path / "private"
    repo_root = tmp_path / "repo"
    data_dir.mkdir()
    repo_root.mkdir()
    proposal = propose_budget([])

    written = write_budget_proposal(
        proposal, data_dir / "out" / "budget.json", data_dir, repo_root
    )
    assert json.loads(written.read_text(encoding="utf-8"))["status"] == "proposal-only"

    with pytest.raises(DecisionError):
        write_budget_proposal(proposal, repo_root / "budget.json", data_dir, repo_root)


def test_only_the_as_of_month_is_excluded_not_the_newest_observed_month():
    """Dropping ordered_months[-1] silently discarded a complete month."""
    rows = [
        row("2024-01-05", "-100.00", "Groceries"),
        row("2024-02-05", "-120.00", "Groceries"),
        row("2024-03-05", "-110.00", "Groceries"),
    ]
    # March is complete relative to April, so it must survive the filter.
    proposal = propose_budget(rows, as_of="2024-04-15")
    assert proposal["monthsAnalyzed"] == ["2024-01", "2024-02", "2024-03"]

    # The same rows viewed from inside March exclude March alone.
    from_march = propose_budget(rows, as_of="2024-03-31")
    assert from_march["monthsAnalyzed"] == ["2024-01", "2024-02"]


def test_a_gap_before_the_as_of_month_does_not_drop_a_complete_month():
    rows = [
        row("2024-01-05", "-100.00", "Groceries"),
        row("2024-02-05", "-120.00", "Groceries"),
    ]
    # Nothing was spent in March or April; February is still complete.
    proposal = propose_budget(rows, as_of="2024-04-15")
    assert proposal["monthsAnalyzed"] == ["2024-01", "2024-02"]


def test_a_single_complete_month_survives_and_a_single_current_month_does_not():
    complete = propose_budget(
        [row("2024-03-05", "-100.00", "Groceries")], as_of="2024-04-15"
    )
    assert complete["monthsAnalyzed"] == ["2024-03"]

    current = propose_budget(
        [row("2024-04-05", "-100.00", "Groceries")], as_of="2024-04-15"
    )
    assert current["monthsAnalyzed"] == []
    assert current["monthsAvailable"] == ["2024-04"]
    assert current["categories"] == []


@pytest.mark.parametrize(
    "as_of",
    ["2024-13-01", "2024-02-30", "not-a-date", "2024-04", "04/15/2024", ""],
)
def test_an_unparseable_as_of_is_refused(as_of):
    with pytest.raises(DecisionError, match="as_of"):
        propose_budget([row("2024-01-05", "-100.00", "Groceries")], as_of=as_of)


def test_as_of_accepts_a_date_object_as_well_as_an_iso_string():
    from datetime import date

    rows = [row("2024-03-05", "-100.00", "Groceries")]
    assert (
        propose_budget(rows, as_of=date(2024, 4, 15))["monthsAnalyzed"]
        == propose_budget(rows, as_of="2024-04-15")["monthsAnalyzed"]
    )
