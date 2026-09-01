"""Median/rounding primitives and a canonical-history budget cross-check.

Milestone 5 of the replacement plan is explicit that a budget may be seeded
from reviewed historical spending but must **never** be written automatically.
This module honours that literally: it reads the private canonical
``transactions.csv``, computes a per-category monthly median, and writes a
proposal document under the private data directory. It never imports a
Wealthfolio client, never constructs one, and never calls a write endpoint.

It is no longer what the ``budget-propose`` command runs. The canonical export
carries this repository's own provisional categories, not the ones a human
curated inside Wealthfolio, so a canonical-sourced proposal covered nothing a
live budget group actually contains. :mod:`importers.simplefin.live_budget`
reads the live instance instead and reuses this module's median window
(:data:`MIN_MONTHS`) and rounding (:func:`round_up`, :data:`ROUNDING_STEP`) so
both paths shape an amount identically. What remains here is an offline
cross-check that needs no running Wealthfolio.

Turning a proposal into real budget targets is a separate, deliberate act by a
human -- see :func:`importers.simplefin.live_budget.promote_live_budget`.

Amounts are private. Everything this module returns for display -- the
:func:`summarize_proposal` line -- carries counts and paths only, never a
category name or a currency value.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Iterator

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint, validate_private_output

#: Canonical transaction kinds that represent real outflow a budget covers.
#: Transfers, credit-card and loan payments, savings contributions, investment
#: activity, and reconciliation rows are movements of money rather than
#: consumption, so budgeting them would double-count the spending they fund.
BUDGETABLE_KINDS = frozenset({"expense"})

#: Kinds that offset spending in the same category within a month.
OFFSET_KINDS = frozenset({"refund", "reimbursement"})

#: A category needs at least this many observed months before a median is
#: meaningful enough to propose. Below it, the category is reported as
#: insufficient history rather than given a fabricated target.
MIN_MONTHS = 3

#: Proposals are rounded up to this granularity so a target is a usable round
#: number instead of a spuriously precise median.
ROUNDING_STEP = Decimal("5")


@dataclass(frozen=True)
class CategoryProposal:
    """A proposed monthly target for one category."""

    category: str
    months_observed: int
    transactions: int
    median_monthly: str
    proposed_monthly: str
    minimum_monthly: str
    maximum_monthly: str


def _parse_amount(raw: str) -> Decimal:
    try:
        return Decimal(str(raw).strip() or "0")
    except InvalidOperation:
        raise DecisionError("canonical transactions contain a non-numeric amount") from None


def _month_of(raw: str) -> str:
    value = str(raw or "").strip()
    if len(value) < 7 or value[4] != "-":
        raise DecisionError(f"canonical transaction has an unparseable date: {value!r}")
    return value[:7]


def _is_truthy(raw: Any) -> bool:
    return str(raw or "").strip().casefold() in {"1", "true", "yes", "y"}


def round_up(value: Decimal) -> Decimal:
    """Round a median up to :data:`ROUNDING_STEP` so a target is a usable number."""
    if value <= 0:
        return Decimal("0")
    steps = (value / ROUNDING_STEP).to_integral_value(rounding="ROUND_CEILING")
    return (steps * ROUNDING_STEP).quantize(Decimal("0.01"))


def iter_budgetable_rows(rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield only the canonical rows a spending budget should be built from."""
    for row in rows:
        kind = str(row.get("transaction_kind") or "").strip()
        if kind not in BUDGETABLE_KINDS and kind not in OFFSET_KINDS:
            continue
        if _is_truthy(row.get("excluded")):
            continue
        if str(row.get("transfer_group") or "").strip():
            continue
        if not str(row.get("category") or "").strip():
            continue
        yield row


def _as_of_month(as_of: date | str | None) -> str:
    """Resolve the month that must be excluded because it is still in progress."""
    if as_of is None:
        return date.today().strftime("%Y-%m")
    if isinstance(as_of, date):
        return as_of.strftime("%Y-%m")
    value = str(as_of).strip()
    try:
        return date.fromisoformat(value[:10]).strftime("%Y-%m")
    except ValueError:
        raise DecisionError(f"as_of is not an ISO date: {as_of!r}") from None


def propose_budget(
    rows: Iterable[dict[str, Any]],
    *,
    months: int | None = None,
    min_months: int = MIN_MONTHS,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """Compute a per-category monthly median proposal from canonical rows.

    ``months`` limits the analysis to the most recent N observed months, so a
    proposal reflects current behaviour rather than a long-stale average.

    ``as_of`` names the day the proposal is being made and defaults to today.
    Only *its* month is dropped, because only the month in progress is partial.
    Dropping the newest observed month instead would silently discard a
    complete month whenever the data stops short of today, and would keep a
    partial month whenever the caller passes a single month of history.
    """
    if min_months < 1:
        raise DecisionError("min_months must be at least 1")
    current_month = _as_of_month(as_of)
    per_month: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    observed_months: set[str] = set()

    for row in iter_budgetable_rows(rows):
        month = _month_of(row.get("date"))
        category = str(row["category"]).strip()
        amount = abs(_parse_amount(row.get("amount")))
        kind = str(row.get("transaction_kind") or "").strip()
        signed = -amount if kind in OFFSET_KINDS else amount
        per_month[category][month] += signed
        counts[category][month] += 1
        observed_months.add(month)

    if not observed_months:
        return _empty_proposal(min_months)

    ordered_months = sorted(observed_months)
    # Drop the month in progress -- and only that month -- because a
    # half-finished month would drag every median down.
    complete_months = [month for month in ordered_months if month != current_month]
    if months is not None:
        if months < 1:
            raise DecisionError("months must be at least 1")
        complete_months = complete_months[-months:]
    window = set(complete_months)

    proposals: list[CategoryProposal] = []
    insufficient: list[dict[str, Any]] = []
    for category in sorted(per_month):
        totals = [
            per_month[category][month]
            for month in complete_months
            if month in per_month[category]
        ]
        totals = [total for total in totals if total > 0]
        transactions = sum(
            count for month, count in counts[category].items() if month in window
        )
        if len(totals) < min_months:
            insufficient.append(
                {
                    "category": category,
                    "monthsObserved": len(totals),
                    "minimumMonths": min_months,
                }
            )
            continue
        monthly_median = Decimal(str(median(sorted(totals)))).quantize(Decimal("0.01"))
        proposals.append(
            CategoryProposal(
                category=category,
                months_observed=len(totals),
                transactions=transactions,
                median_monthly=str(monthly_median),
                proposed_monthly=str(round_up(monthly_median)),
                minimum_monthly=str(min(totals).quantize(Decimal("0.01"))),
                maximum_monthly=str(max(totals).quantize(Decimal("0.01"))),
            )
        )

    proposal = {
        "kind": "budget-proposal",
        "version": 1,
        "status": "proposal-only",
        "note": (
            "Advisory only. Nothing here has been written to Wealthfolio. "
            "Review every line, then create targets deliberately."
        ),
        "minimumMonths": min_months,
        "roundingStep": str(ROUNDING_STEP),
        "monthsAnalyzed": complete_months,
        "monthsAvailable": ordered_months,
        "categories": [asdict(item) for item in proposals],
        "insufficientHistory": insufficient,
    }
    proposal["fingerprint"] = plan_fingerprint(proposal)
    return proposal


def _empty_proposal(min_months: int) -> dict[str, Any]:
    proposal = {
        "kind": "budget-proposal",
        "version": 1,
        "status": "proposal-only",
        "note": "No budgetable history was found; nothing is proposed.",
        "minimumMonths": min_months,
        "roundingStep": str(ROUNDING_STEP),
        "monthsAnalyzed": [],
        "monthsAvailable": [],
        "categories": [],
        "insufficientHistory": [],
    }
    proposal["fingerprint"] = plan_fingerprint(proposal)
    return proposal


def load_canonical_transactions(path: Path) -> list[dict[str, Any]]:
    """Read the private canonical ``transactions.csv``."""
    if not path.exists():
        raise DecisionError(f"canonical transactions not found: {path.name}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def summarize_proposal(proposal: dict[str, Any]) -> str:
    """A PII-free one-line summary: counts only, never categories or amounts."""
    months = proposal.get("monthsAnalyzed") or []
    return (
        f"{len(proposal.get('categories') or [])} categories proposed, "
        f"{len(proposal.get('insufficientHistory') or [])} with insufficient history, "
        f"across {len(months)} complete months (proposal only, nothing written)"
    )


def write_budget_proposal(
    proposal: dict[str, Any], output: Path, data_dir: Path, repo_root: Path
) -> Path:
    """Write a proposal under the private data directory, never into the repo."""
    target = validate_private_output(output, data_dir, repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
