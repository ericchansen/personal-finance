"""Live-sourced budget proposals and their guarded promotion.

Milestones 5-6 of the replacement plan seed a budget from reviewed spending
history. An earlier revision computed that from the private canonical
``transactions.csv`` and proposed nothing, because the canonical export carries
this repository's own provisional categories, not the ones a human actually
curated inside Wealthfolio. The live instance is the source of truth for
"which category is this spending in", so a useful proposal has to read it.

Two commands live here, and they are deliberately asymmetric:

``propose`` is read-only. It asks Wealthfolio for one ``POST /spending/report``
per **complete** calendar month in a caller-specified trailing window, reads the
spending taxonomy and the budget document, and proposes a median monthly target
for every category that is (a) assigned to a native Wealthfolio budget group and
(b) backed by at least ``min_months`` months of observed spending. Everything
else is reported as excluded or as insufficient history rather than given a
fabricated number. Nothing is written to Wealthfolio; the proposal, including
every amount, is written only under the private data directory.

``promote`` is the only path that writes, and it writes exactly one kind of
object: a ``category`` budget target. It requires the exact proposal
fingerprint, the exact production environment fingerprint, an explicit allow
flag, a demonstrably fresh backup, live evidence that still hashes to the
sealed proposal, an absence of conflicting targets, budget-target endpoints the
pinned build actually supports, a post-write re-read verification, rollback of
every target it created *and of any target that committed server-side without
being recorded*, and an immutable receipt that makes a repeat run idempotent
instead of duplicative.

Wealthfolio's SQLite database is never opened. Categories and budget groups are
never created: a proposal covers only what a human already curated, and the
client proxy in this module makes any other write physically unreachable.
"""

from __future__ import annotations

import calendar
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlsplit

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint, validate_private_output
from importers.simplefin.budget_proposal import MIN_MONTHS, ROUNDING_STEP, round_up
from importers.simplefin.categorization import (
    REPO_ROOT,
    fresh_backup,
    sha256_file,
    write_immutable_json,
)
from importers.simplefin.spending_adapter import (
    CAP_BUDGET_READ,
    CAP_BUDGET_TARGET_DELETE,
    CAP_BUDGET_TARGET_WRITE,
    DEFAULT_PERIOD_KEY,
    KNOWN_API_GAPS,
    SPENDING_TAXONOMY,
    SUPPORTED_ENDPOINTS,
    UNCATEGORIZED_CATEGORY_IDS,
    SpendingAdapter,
    validate_period_key,
)

PROPOSAL_KIND = "live-budget-proposal"
PROMOTION_MODE = "live-budget-promotion"
SCHEMA_VERSION = 1

#: The one target shape this module is ever allowed to write.
TARGET_TYPE = "category"

#: Categories Wealthfolio reports under a synthetic identity rather than a real
#: curated category. They can never carry a budget target.
SYNTHETIC_CATEGORY_IDS = UNCATEGORIZED_CATEGORY_IDS

#: The only write path the promotion proxy will let out of this process.
_TARGETS_PATH = "/spending/budget/targets"

#: Spending reads Wealthfolio models as POST bodies rather than query strings.
#: They are reads, so the promotion proxy allows exactly these two and no other.
_READ_ONLY_POST_PATHS = frozenset({"/spending/report", "/spending/cash-activities/search"})


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _decimal(raw: Any, what: str) -> Decimal:
    try:
        return Decimal(str(raw if raw not in (None, "") else 0))
    except (InvalidOperation, ValueError, TypeError):
        raise DecisionError(f"Wealthfolio returned a non-numeric {what}") from None


def _count(raw: Any, what: str) -> int:
    try:
        value = int(raw or 0)
    except (ValueError, TypeError):
        raise DecisionError(f"Wealthfolio returned a non-numeric {what}") from None
    if value < 0:
        raise DecisionError(f"Wealthfolio returned a negative {what}")
    return value


def _text(raw: Any) -> str:
    return str(raw or "").strip()


# -- window ------------------------------------------------------------------


def previous_month(month_key: str) -> str:
    year, month = int(month_key[:4]), int(month_key[5:])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def trailing_complete_months(as_of: date, months: int) -> list[str]:
    """The ``months`` complete calendar months ending before ``as_of``'s month.

    The month containing ``as_of`` is always excluded: a partial month would
    drag every median down. A month is "complete" only once it has fully
    elapsed, so on the last day of a month that month is still in progress.
    """
    if months < 1:
        raise DecisionError("the trailing window must be at least one month")
    keys: list[str] = []
    cursor = previous_month(f"{as_of.year}-{as_of.month:02d}")
    for _ in range(months):
        keys.append(cursor)
        cursor = previous_month(cursor)
    return list(reversed(keys))


def month_window(month_key: str) -> dict[str, str]:
    """The inclusive UTC request window Wealthfolio expects for one month."""
    checked = validate_period_key(month_key)
    if checked is None or checked == DEFAULT_PERIOD_KEY:
        raise DecisionError(f"invalid analysis month: {month_key!r}")
    year, month = int(checked[:4]), int(checked[5:])
    last_day = calendar.monthrange(year, month)[1]
    return {
        "startDate": f"{checked}-01T00:00:00Z",
        "endDate": f"{checked}-{last_day:02d}T23:59:59Z",
    }


# -- live evidence -----------------------------------------------------------


@dataclass
class LiveBudgetEvidence:
    """Exactly what was read live, and the hashes that seal it."""

    taxonomy_id: str
    period_key: str
    months: list[str]
    monthly: list[dict[str, Any]] = field(default_factory=list)
    categories: dict[str, str] = field(default_factory=dict)
    group_names: dict[str, str] = field(default_factory=dict)
    group_assignments: dict[str, str] = field(default_factory=dict)
    existing_targets: list[dict[str, Any]] = field(default_factory=list)
    category_sha256: str = ""
    budget_sha256: str = ""

    @property
    def monthly_hashes(self) -> list[dict[str, str]]:
        return [{"month": row["month"], "sha256": row["sha256"]} for row in self.monthly]

    def evidence_block(self) -> dict[str, Any]:
        block = {
            "source": "wealthfolio-live",
            "taxonomyId": self.taxonomy_id,
            "periodKey": self.period_key,
            "monthlyReports": self.monthly_hashes,
            "categorySha256": self.category_sha256,
            "budgetCurationSha256": self.budget_sha256,
        }
        block["evidenceSha256"] = plan_fingerprint(block)
        return block


def _report_rows(report: Any, taxonomy_id: str, month_key: str) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        raise DecisionError(f"Wealthfolio spending report for {month_key} is not an object")
    breakdown = report.get("spendingBreakdown")
    if not isinstance(breakdown, list):
        raise DecisionError(f"Wealthfolio spending report for {month_key} has no breakdown")
    totals: dict[str, tuple[Decimal, int]] = {}
    for row in breakdown:
        if not isinstance(row, dict):
            raise DecisionError(f"Wealthfolio spending breakdown for {month_key} is malformed")
        if _text(row.get("taxonomyId")) != taxonomy_id:
            continue
        category_id = _text(row.get("categoryId"))
        if category_id in SYNTHETIC_CATEGORY_IDS:
            continue
        amount, count = totals.get(category_id, (Decimal(), 0))
        totals[category_id] = (
            # Wealthfolio emits signed breakdown amounts: a refunded category
            # can be net negative for a month. Keep the sign so the proposal's
            # "amount > 0" filter can exclude that month instead of turning a
            # refund into spending.
            amount + _decimal(row.get("amount"), "spending amount"),
            count + _count(row.get("count"), "activity count"),
        )
    return [
        {"categoryId": category_id, "amount": _money(amount), "count": count}
        for category_id, (amount, count) in sorted(totals.items())
    ]


def _category_evidence(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for category in categories:
        if not isinstance(category, dict):
            raise DecisionError("Wealthfolio taxonomy contains a non-object category")
        category_id = _text(category.get("id"))
        if not category_id:
            raise DecisionError("Wealthfolio taxonomy contains a category without an id")
        rows.append(
            {
                "id": category_id,
                "parentId": _text(category.get("parentId")) or None,
                "name": _text(category.get("name")),
            }
        )
    return sorted(rows, key=lambda row: row["id"])


def _budget_evidence(snapshot: dict[str, Any], taxonomy_id: str) -> dict[str, Any]:
    """Split the budget document into curation (sealed) and targets (mutable).

    ``groups`` and ``groupAssignments`` are human curation: a proposal is only
    meaningful while they are unchanged, so they are hashed into the sealed
    evidence. ``targets`` are precisely what promotion writes, so they are
    returned separately and checked by the conflict rules instead — sealing
    them would make a promotion invalidate its own receipt.
    """
    state = snapshot["state"]
    groups = []
    for group in state["groups"]:
        if not isinstance(group, dict) or not _text(group.get("id")):
            raise DecisionError("Wealthfolio budget group is malformed")
        groups.append({"id": _text(group["id"]), "name": _text(group.get("name"))})
    assignments = []
    for row in state["groupAssignments"]:
        if not isinstance(row, dict):
            raise DecisionError("Wealthfolio budget group assignment is malformed")
        row_taxonomy = _text(row.get("taxonomyId")) or taxonomy_id
        category_id = _text(row.get("categoryId"))
        group_id = _text(row.get("groupId"))
        if not category_id or not group_id:
            raise DecisionError("Wealthfolio budget group assignment is incomplete")
        assignments.append(
            {"taxonomyId": row_taxonomy, "categoryId": category_id, "groupId": group_id}
        )
    targets = []
    for target in state["targets"]:
        if not isinstance(target, dict):
            raise DecisionError("Wealthfolio budget target is malformed")
        targets.append(
            {
                "id": _text(target.get("id")),
                "periodKey": _text(target.get("periodKey")),
                "targetType": _text(target.get("targetType")),
                "taxonomyId": _text(target.get("taxonomyId")) or None,
                "categoryId": _text(target.get("categoryId")) or None,
                "groupId": _text(target.get("groupId")) or None,
                "amount": _money(_decimal(target.get("amount"), "budget target amount")),
            }
        )
    return {
        "groups": sorted(groups, key=lambda row: row["id"]),
        "groupAssignments": sorted(
            assignments, key=lambda row: (row["taxonomyId"], row["categoryId"])
        ),
        "targets": sorted(
            targets,
            key=lambda row: (
                row["periodKey"],
                row["targetType"],
                row["categoryId"] or "",
                row["groupId"] or "",
                row["id"],
            ),
        ),
    }


def read_live_evidence(
    adapter: SpendingAdapter,
    months: list[str],
    *,
    taxonomy_id: str = SPENDING_TAXONOMY,
    period_key: str = DEFAULT_PERIOD_KEY,
) -> LiveBudgetEvidence:
    """Read every live value a live budget proposal depends on. Read-only."""
    if not months:
        raise DecisionError("a live budget proposal needs at least one complete month")
    checked_period = validate_period_key(period_key)
    if checked_period is None:
        raise DecisionError("a live budget proposal requires an explicit budget period")
    evidence = LiveBudgetEvidence(
        taxonomy_id=taxonomy_id,
        period_key=checked_period,
        months=list(months),
    )
    for month_key in months:
        report = adapter.report(month_window(month_key))
        rows = _report_rows(report, taxonomy_id, month_key)
        evidence.monthly.append(
            {
                "month": month_key,
                "rows": rows,
                "sha256": plan_fingerprint(
                    {"month": month_key, "taxonomyId": taxonomy_id, "rows": rows}
                ),
            }
        )
    catalog = _category_evidence(adapter.taxonomy(taxonomy_id))
    evidence.categories = {row["id"]: row["name"] for row in catalog}
    evidence.category_sha256 = plan_fingerprint(
        {"taxonomyId": taxonomy_id, "categories": catalog}
    )
    budget = _budget_evidence(adapter.budget_snapshot(checked_period), taxonomy_id)
    evidence.group_names = {row["id"]: row["name"] for row in budget["groups"]}
    evidence.group_assignments = {
        row["categoryId"]: row["groupId"]
        for row in budget["groupAssignments"]
        if row["taxonomyId"] == taxonomy_id
    }
    evidence.existing_targets = budget["targets"]
    evidence.budget_sha256 = plan_fingerprint(
        {
            "periodKey": checked_period,
            "groups": budget["groups"],
            "groupAssignments": budget["groupAssignments"],
        }
    )
    return evidence


# -- proposal ----------------------------------------------------------------


def propose_live_budget(
    evidence: LiveBudgetEvidence,
    *,
    min_months: int = MIN_MONTHS,
    as_of: date,
    environment_fingerprint: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Turn sealed live evidence into a proposal-only budget document.

    Targets are proposed into the same budget period the evidence was read
    from, so the "no conflicting target" check at promotion time is asking
    about the very targets that were read here.
    """
    if min_months < 1:
        raise DecisionError("min_months must be at least 1")
    if min_months > len(evidence.months):
        raise DecisionError("min_months cannot exceed the analyzed window")
    checked_period = validate_period_key(evidence.period_key)
    if checked_period is None:
        raise DecisionError("a live budget proposal requires an explicit budget period")

    per_category: dict[str, dict[str, tuple[Decimal, int]]] = {}
    for month in evidence.monthly:
        for row in month["rows"]:
            bucket = per_category.setdefault(row["categoryId"], {})
            bucket[month["month"]] = (Decimal(row["amount"]), row["count"])

    targets: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for category_id in sorted(per_category):
        group_id = evidence.group_assignments.get(category_id)
        observations = per_category[category_id]
        transactions = sum(count for _amount, count in observations.values())
        if not group_id:
            excluded.append(
                {
                    "categoryId": category_id,
                    "categoryName": evidence.categories.get(category_id, ""),
                    "reason": "not-assigned-to-a-budget-group",
                    "monthsObserved": len(observations),
                    "transactions": transactions,
                }
            )
            continue
        if group_id not in evidence.group_names:
            excluded.append(
                {
                    "categoryId": category_id,
                    "categoryName": evidence.categories.get(category_id, ""),
                    "reason": "assigned-to-an-unknown-budget-group",
                    "monthsObserved": len(observations),
                    "transactions": transactions,
                }
            )
            continue
        if category_id not in evidence.categories:
            excluded.append(
                {
                    "categoryId": category_id,
                    "categoryName": "",
                    "reason": "not-present-in-the-live-taxonomy",
                    "monthsObserved": len(observations),
                    "transactions": transactions,
                }
            )
            continue
        spent = [amount for amount, _count in observations.values() if amount > 0]
        if len(spent) < min_months:
            insufficient.append(
                {
                    "categoryId": category_id,
                    "categoryName": evidence.categories.get(category_id, ""),
                    "groupId": group_id,
                    "monthsObserved": len(spent),
                    "minimumMonths": min_months,
                }
            )
            continue
        monthly_median = Decimal(str(median(sorted(spent)))).quantize(Decimal("0.01"))
        targets.append(
            {
                "targetType": TARGET_TYPE,
                "periodKey": checked_period,
                "taxonomyId": evidence.taxonomy_id,
                "categoryId": category_id,
                "categoryName": evidence.categories.get(category_id, ""),
                "groupId": group_id,
                "groupName": evidence.group_names.get(group_id, ""),
                "monthsObserved": len(spent),
                "transactions": transactions,
                "medianMonthly": _money(monthly_median),
                "proposedMonthly": _money(round_up(monthly_median)),
                "minimumMonthly": _money(min(spent)),
                "maximumMonthly": _money(max(spent)),
            }
        )

    # A category curated into a budget group with no spending at all in the
    # window never reaches the loop above; report it so the human sees it.
    for category_id, group_id in sorted(evidence.group_assignments.items()):
        if category_id in per_category:
            continue
        insufficient.append(
            {
                "categoryId": category_id,
                "categoryName": evidence.categories.get(category_id, ""),
                "groupId": group_id,
                "monthsObserved": 0,
                "minimumMonths": min_months,
            }
        )
    insufficient.sort(key=lambda row: row["categoryId"])

    proposal = {
        "kind": PROPOSAL_KIND,
        "schemaVersion": SCHEMA_VERSION,
        "status": "proposal-only",
        "note": (
            "Advisory only. Nothing here has been written to Wealthfolio. "
            "Review every line, then promote deliberately."
        ),
        "generatedAt": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "asOf": as_of.isoformat(),
        "environmentFingerprint": environment_fingerprint,
        "taxonomyId": evidence.taxonomy_id,
        "periodKey": checked_period,
        "monthsAnalyzed": list(evidence.months),
        "minimumMonths": min_months,
        "roundingStep": str(ROUNDING_STEP),
        "evidence": evidence.evidence_block(),
        "targets": targets,
        "insufficientHistory": insufficient,
        "excluded": excluded,
    }
    proposal["proposalFingerprint"] = plan_fingerprint(proposal)
    return proposal


def summarize_live_proposal(proposal: dict[str, Any]) -> str:
    """A PII-free one-line summary: counts only, never a category or an amount."""
    return (
        f"{len(proposal.get('targets') or [])} targets proposed, "
        f"{len(proposal.get('insufficientHistory') or [])} with insufficient history, "
        f"{len(proposal.get('excluded') or [])} outside a budget group, "
        f"across {len(proposal.get('monthsAnalyzed') or [])} complete months "
        "(proposal only, nothing written)"
    )


def write_live_proposal(
    proposal: dict[str, Any], output: Path, data_dir: Path, repo_root: Path = REPO_ROOT
) -> Path:
    """Write a proposal under the private data directory, never into the repo."""
    target = validate_private_output(output, data_dir, repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def validate_live_proposal(proposal: Any) -> dict[str, Any]:
    """Structurally validate a proposal and re-derive its fingerprint."""
    if not isinstance(proposal, dict):
        raise DecisionError("live budget proposal must be an object")
    if proposal.get("kind") != PROPOSAL_KIND or proposal.get("schemaVersion") != SCHEMA_VERSION:
        raise DecisionError("live budget proposal has an unexpected kind or schema version")
    for field_name in ("targets", "insufficientHistory", "excluded", "monthsAnalyzed"):
        if not isinstance(proposal.get(field_name), list):
            raise DecisionError(f"live budget proposal {field_name} must be an array")
    if not isinstance(proposal.get("evidence"), dict):
        raise DecisionError("live budget proposal has no evidence block")
    evidence = proposal["evidence"]
    sealed = {key: value for key, value in evidence.items() if key != "evidenceSha256"}
    if evidence.get("evidenceSha256") != plan_fingerprint(sealed):
        raise DecisionError("live budget proposal evidence hash is invalid")
    if validate_period_key(proposal.get("periodKey")) is None:
        raise DecisionError("live budget proposal has no explicit target period")
    seen: set[str] = set()
    for target in proposal["targets"]:
        if not isinstance(target, dict):
            raise DecisionError("live budget proposal target must be an object")
        if target.get("targetType") != TARGET_TYPE:
            raise DecisionError("live budget proposals only ever write category targets")
        category_id = _text(target.get("categoryId"))
        if not category_id or category_id in SYNTHETIC_CATEGORY_IDS:
            raise DecisionError("live budget proposal target has no usable category")
        if category_id in seen:
            raise DecisionError("live budget proposal targets a category twice")
        seen.add(category_id)
        if not _text(target.get("groupId")):
            raise DecisionError("live budget proposal target is not in a budget group")
        if _text(target.get("taxonomyId")) != _text(proposal.get("taxonomyId")):
            raise DecisionError("live budget proposal target taxonomy is inconsistent")
        if target.get("periodKey") != proposal["periodKey"]:
            raise DecisionError("live budget proposal target period is inconsistent")
        amount = _decimal(target.get("proposedMonthly"), "proposed amount")
        if amount <= 0:
            raise DecisionError("live budget proposal target amount must be positive")
    expected = plan_fingerprint(
        {key: value for key, value in proposal.items() if key != "proposalFingerprint"}
    )
    if proposal.get("proposalFingerprint") != expected:
        raise DecisionError("live budget proposal fingerprint is invalid")
    return proposal


# -- promotion ---------------------------------------------------------------


class BudgetTargetOnlyClient:
    """A write-narrowing proxy around a Wealthfolio client.

    Promotion needs ``allow_configuration_writes=True`` to write a budget
    target, which would otherwise also unlock category, rule, and budget-group
    mutation. This proxy makes those physically unreachable: any request that
    is not a read, a backup, or a budget *target* write raises before a socket
    is touched. Auto-creating a category or a budget group is therefore not a
    policy this module follows, it is an operation it cannot perform.
    """

    def __init__(self, client: Any):
        self._client = client

    @staticmethod
    def _route(path: str) -> str:
        return urlsplit(str(path)).path

    @classmethod
    def _is_target_path(cls, path: str) -> bool:
        route = cls._route(path)
        return route == _TARGETS_PATH or route.startswith(f"{_TARGETS_PATH}/")

    def _reject(self, method: str, path: str) -> None:
        raise DecisionError(
            f"budget promotion may only write budget targets; refusing {method} {path}"
        )

    def get(self, path: str) -> Any:
        return self._client.get(path)

    def post(self, path: str, payload: Any) -> Any:
        if self._route(path) not in _READ_ONLY_POST_PATHS and not self._is_target_path(path):
            self._reject("POST", path)
        return self._client.post(path, payload)

    def put(self, path: str, payload: Any) -> Any:
        self._reject("PUT", path)

    def delete(self, path: str, payload: Any = None) -> Any:
        if not self._is_target_path(path):
            self._reject("DELETE", path)
        return self._client.delete(path, payload)

    def backup_database(self) -> Any:
        return self._client.backup_database()


def _require_budget_target_endpoints(adapter: SpendingAdapter) -> dict[str, str]:
    """Fail closed unless the pinned build serves the endpoints we need."""
    routes: dict[str, str] = {}
    for capability in (CAP_BUDGET_TARGET_WRITE, CAP_BUDGET_TARGET_DELETE):
        if capability in KNOWN_API_GAPS or capability not in SUPPORTED_ENDPOINTS:
            raise DecisionError(
                f"this Wealthfolio build exposes no supported endpoint for {capability}"
            )
        method, path = SUPPORTED_ENDPOINTS[capability]
        routes[capability] = f"{method} {path}"
    adapter.require(CAP_BUDGET_READ)
    return routes


def _target_identity(target: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _text(target.get("periodKey")),
        _text(target.get("taxonomyId")),
        _text(target.get("categoryId")),
    )


def _category_targets(
    evidence: LiveBudgetEvidence, period_key: str, taxonomy_id: str
) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        _target_identity(target): target
        for target in evidence.existing_targets
        if target.get("targetType") == TARGET_TYPE
        and _text(target.get("periodKey")) == period_key
        and _text(target.get("taxonomyId")) == taxonomy_id
    }


def _classify_existing_targets(
    proposal: dict[str, Any], evidence: LiveBudgetEvidence
) -> tuple[bool, bool]:
    """Decide whether the proposal is fully applied, absent, or in conflict."""
    period_key = proposal["periodKey"]
    taxonomy_id = proposal["taxonomyId"]
    existing = _category_targets(evidence, period_key, taxonomy_id)
    matched = 0
    present = 0
    for target in proposal["targets"]:
        identity = (period_key, taxonomy_id, _text(target["categoryId"]))
        live = existing.get(identity)
        if live is None:
            continue
        present += 1
        if _money(_decimal(live.get("amount"), "budget target amount")) == _money(
            _decimal(target["proposedMonthly"], "proposed amount")
        ):
            matched += 1
    if present and present != matched:
        raise DecisionError(
            "production already has conflicting budget targets for proposed categories"
        )
    total = len(proposal["targets"])
    return matched == total and total > 0, present == 0


def _live_evidence_for(
    adapter: SpendingAdapter, proposal: dict[str, Any]
) -> LiveBudgetEvidence:
    return read_live_evidence(
        adapter,
        list(proposal["monthsAnalyzed"]),
        taxonomy_id=proposal["taxonomyId"],
        period_key=proposal["periodKey"],
    )


def _require_unchanged_evidence(
    proposal: dict[str, Any], evidence: LiveBudgetEvidence
) -> None:
    if evidence.evidence_block() != proposal["evidence"]:
        raise DecisionError("live Wealthfolio evidence changed after the proposal was sealed")


def _snapshot_targets(
    adapter: SpendingAdapter, proposal: dict[str, Any]
) -> list[dict[str, Any]]:
    snapshot = adapter.budget_snapshot(proposal["periodKey"])
    return _budget_evidence(snapshot, proposal["taxonomyId"])["targets"]


def _find_written_target(
    targets: list[dict[str, Any]], identity: tuple[str, str, str]
) -> dict[str, Any] | None:
    matches = [
        target
        for target in targets
        if target.get("targetType") == TARGET_TYPE and _target_identity(target) == identity
    ]
    if len(matches) > 1:
        raise DecisionError("Wealthfolio reports duplicate targets for one category")
    return matches[0] if matches else None


def _proposed_identities(proposal: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (proposal["periodKey"], proposal["taxonomyId"], _text(target["categoryId"]))
        for target in proposal["targets"]
    }


def _delete_uncommitted_targets(
    adapter: SpendingAdapter, proposal: dict[str, Any], before: list[dict[str, Any]]
) -> None:
    """Remove targets that committed server-side but were never recorded.

    A write can land in Wealthfolio and still raise before the caller records
    it (a dropped response, or a response body this module refuses to trust).
    Such a target is invisible to ``created``, so rollback re-reads the live
    targets and removes anything that matches a proposed identity and is not
    part of the pre-promotion state.
    """
    before_ids = {_text(target.get("id")) for target in before}
    identities = _proposed_identities(proposal)
    for target in _snapshot_targets(adapter, proposal):
        target_id = _text(target.get("id"))
        if (
            target.get("targetType") != TARGET_TYPE
            or not target_id
            or target_id in before_ids
            or _target_identity(target) not in identities
        ):
            continue
        adapter.delete_budget_target(target_id, period_key=proposal["periodKey"])


def _rollback_targets(
    adapter: SpendingAdapter,
    proposal: dict[str, Any],
    created: list[dict[str, Any]],
    before: list[dict[str, Any]],
) -> None:
    for target in reversed(created):
        adapter.delete_budget_target(target["targetId"], period_key=proposal["periodKey"])
    _delete_uncommitted_targets(adapter, proposal, before)
    if _snapshot_targets(adapter, proposal) != before:
        raise DecisionError("budget target rollback did not restore the previous targets")


def _validate_existing_receipt(
    receipt: dict[str, Any],
    proposal: dict[str, Any],
    proposal_sha256: str,
    environment_fingerprint: str,
) -> None:
    expected = plan_fingerprint(
        {key: value for key, value in receipt.items() if key != "receiptFingerprint"}
    )
    if (
        receipt.get("receiptFingerprint") != expected
        or receipt.get("schemaVersion") != SCHEMA_VERSION
        or receipt.get("mode") != PROMOTION_MODE
        or receipt.get("status") != "applied"
        or receipt.get("proposalFingerprint") != proposal["proposalFingerprint"]
        or receipt.get("proposalSha256") != proposal_sha256
        or receipt.get("environmentFingerprint") != environment_fingerprint
        or receipt.get("periodKey") != proposal["periodKey"]
        or receipt.get("evidence") != proposal["evidence"]
        or not receipt.get("backup")
    ):
        raise DecisionError("existing budget promotion receipt is invalid")


def _receipt_folder(data_dir: Path) -> Path:
    folder = data_dir / "normalized" / "simplefin"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _promote_live_budget_locked(
    client: Any,
    proposal: dict[str, Any],
    proposal_path: Path,
    data_dir: Path,
    *,
    environment_fingerprint: str,
    supplied_proposal_fingerprint: str,
    supplied_environment_fingerprint: str,
    allow_production: bool,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], Path, bool]:
    validate_live_proposal(proposal)
    if not allow_production:
        raise DecisionError("--allow-production is required")
    if supplied_proposal_fingerprint != proposal["proposalFingerprint"]:
        raise DecisionError("operator-supplied budget proposal fingerprint is not exact")
    if (
        supplied_environment_fingerprint != proposal.get("environmentFingerprint")
        or environment_fingerprint != proposal.get("environmentFingerprint")
    ):
        raise DecisionError("production environment fingerprint is not exact")
    if not proposal["targets"]:
        raise DecisionError("live budget proposal has no targets to promote")

    proposal_sha256 = sha256_file(proposal_path)
    adapter = SpendingAdapter(
        BudgetTargetOnlyClient(client), allow_configuration_writes=True
    )
    evidence = _live_evidence_for(adapter, proposal)
    endpoints = _require_budget_target_endpoints(adapter)
    _require_unchanged_evidence(proposal, evidence)
    all_applied, none_applied = _classify_existing_targets(proposal, evidence)

    receipt_path = _receipt_folder(data_dir) / f"budget-promotion-{proposal_sha256}.json"
    if receipt_path.exists():
        if receipt_path.stat().st_mode & stat.S_IWRITE:
            raise DecisionError("existing budget promotion receipt is not immutable")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        _validate_existing_receipt(
            receipt, proposal, proposal_sha256, environment_fingerprint
        )
        if not all_applied:
            raise DecisionError("production budget targets drifted after promotion")
        return receipt, receipt_path, True
    if all_applied:
        raise DecisionError(
            "production budget targets are applied without an immutable receipt"
        )
    if not none_applied:
        raise DecisionError("production has a partially applied budget proposal")

    before = list(evidence.existing_targets)
    backup = fresh_backup(adapter)
    if sha256_file(proposal_path) != proposal_sha256:
        raise DecisionError("budget proposal file changed before production mutation")
    recheck = _live_evidence_for(adapter, proposal)
    _require_unchanged_evidence(proposal, recheck)
    if list(recheck.existing_targets) != before:
        raise DecisionError("production budget targets changed before mutation")

    generated_at = generated_at or datetime.now(timezone.utc)
    created: list[dict[str, Any]] = []
    try:
        for target in proposal["targets"]:
            identity = (
                proposal["periodKey"],
                proposal["taxonomyId"],
                _text(target["categoryId"]),
            )
            snapshot = adapter.upsert_budget_target(
                {
                    "targetType": TARGET_TYPE,
                    "periodKey": proposal["periodKey"],
                    "taxonomyId": proposal["taxonomyId"],
                    "categoryId": target["categoryId"],
                    "amount": target["proposedMonthly"],
                },
                period_key=proposal["periodKey"],
            )
            written = _find_written_target(
                _budget_evidence(snapshot, proposal["taxonomyId"])["targets"], identity
            )
            if written is None or not written["id"]:
                raise DecisionError("Wealthfolio did not return the written budget target")
            created.append(
                {
                    "targetId": written["id"],
                    "taxonomyId": proposal["taxonomyId"],
                    "categoryId": target["categoryId"],
                    "amount": target["proposedMonthly"],
                }
            )
        after = _snapshot_targets(adapter, proposal)
        for record in created:
            identity = (
                proposal["periodKey"],
                proposal["taxonomyId"],
                record["categoryId"],
            )
            verified = _find_written_target(after, identity)
            if (
                verified is None
                or verified["id"] != record["targetId"]
                or verified["amount"] != record["amount"]
            ):
                raise DecisionError("budget target verification failed after promotion")
        untouched_before = [
            target for target in before if _target_identity(target) not in {
                (
                    proposal["periodKey"],
                    proposal["taxonomyId"],
                    record["categoryId"],
                )
                for record in created
            }
        ]
        untouched_after = [
            target
            for target in after
            if target["id"] not in {record["targetId"] for record in created}
        ]
        if untouched_after != untouched_before:
            raise DecisionError("budget promotion changed a target it did not propose")
        if sha256_file(proposal_path) != proposal_sha256:
            raise DecisionError("budget proposal file changed during production mutation")
        receipt = {
            "schemaVersion": SCHEMA_VERSION,
            "mode": PROMOTION_MODE,
            "generatedAt": generated_at.isoformat(),
            "status": "applied",
            "proposal": str(proposal_path),
            "proposalSha256": proposal_sha256,
            "proposalFingerprint": proposal["proposalFingerprint"],
            "environmentFingerprint": environment_fingerprint,
            "taxonomyId": proposal["taxonomyId"],
            "periodKey": proposal["periodKey"],
            "endpoints": endpoints,
            "evidence": proposal["evidence"],
            "backup": backup,
            "beforeTargets": before,
            "afterTargets": after,
            "writtenTargets": sorted(created, key=lambda row: row["categoryId"]),
        }
        receipt["receiptFingerprint"] = plan_fingerprint(receipt)
        write_immutable_json(receipt_path, receipt)
    except Exception as exc:
        try:
            _rollback_targets(adapter, proposal, created, before)
        except Exception as rollback_exc:
            raise DecisionError(
                f"budget promotion failed and rollback failed: {rollback_exc}"
            ) from exc
        raise
    return receipt, receipt_path, False


def promote_live_budget(
    client: Any,
    proposal: dict[str, Any],
    proposal_path: Path,
    data_dir: Path,
    *,
    environment_fingerprint: str,
    supplied_proposal_fingerprint: str,
    supplied_environment_fingerprint: str,
    allow_production: bool,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], Path, bool]:
    """Serialize promotions so a concurrent retry cannot duplicate targets."""
    lock_path = (
        _receipt_folder(data_dir) / f".budget-promotion-{sha256_file(proposal_path)}.lock"
    )
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        raise DecisionError("another budget promotion is already in progress") from None
    try:
        return _promote_live_budget_locked(
            client,
            proposal,
            proposal_path,
            data_dir,
            environment_fingerprint=environment_fingerprint,
            supplied_proposal_fingerprint=supplied_proposal_fingerprint,
            supplied_environment_fingerprint=supplied_environment_fingerprint,
            allow_production=allow_production,
            generated_at=generated_at,
        )
    finally:
        os.close(descriptor)
        lock_path.chmod(0o600)
        lock_path.unlink(missing_ok=True)
