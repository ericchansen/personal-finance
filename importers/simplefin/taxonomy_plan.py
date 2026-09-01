"""Plan a compact, at-most-two-level Wealthfolio spending taxonomy.

Milestone 3 of the replacement plan asks for a *compact* category hierarchy:
few enough top-level groups to read at a glance, and never more than two
visible levels, so a category page and a budget row stay legible.

Wealthfolio does not enforce a depth limit itself -- ``POST
/taxonomies/categories`` and ``POST /taxonomies/categories/move`` accept any
``parentId``, including one that is already a child. The two-level rule is
therefore a policy this repository enforces, and this module is where it is
enforced: it reads the live taxonomy, diffs it against the recommended shape,
and emits an explicit, reviewable operation list.

This module is *planning only*. It performs no writes and holds no reference
to a live client. Applying a plan is a separate, operator-driven step against
``SpendingAdapter(..., allow_configuration_writes=True)``.

The recommended taxonomy below is a generic personal-finance shape. It is not
derived from anyone's transactions and contains no institution names, account
identifiers, merchants, or amounts, so it is safe to keep in this public
repository. A rendered plan may echo live category *names*, so plans are only
ever written under the private data directory.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint, validate_private_output
from importers.simplefin.spending_adapter import (
    INCOME_TAXONOMY,
    SPENDING_TAXONOMY,
)

#: The deepest a category may sit. 0 is a top-level group, 1 is its child.
MAX_DEPTH = 1

#: Recommended compact spending hierarchy: top-level group -> subcategories.
#: Deliberately small. Every leaf should be something a person would actually
#: budget or review, and every top-level group should fit on one screen.
RECOMMENDED_SPENDING_TAXONOMY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Housing", ("Rent & Mortgage", "Utilities", "Home Maintenance")),
    ("Food", ("Groceries", "Dining", "Coffee")),
    (
        "Transportation",
        ("Fuel", "Vehicle Maintenance", "Transit & Rideshare", "Parking & Tolls"),
    ),
    ("Health", ("Medical", "Pharmacy", "Fitness")),
    ("Household & Family", ("Household Supplies", "Childcare", "Pets", "Education")),
    ("Personal", ("Clothing", "Personal Care", "Hobbies")),
    ("Lifestyle", ("Entertainment", "Subscriptions", "Travel")),
    ("Giving & Gifts", ()),
    ("Financial", ("Fees", "Taxes", "Interest", "Insurance")),
)

#: Recommended compact income hierarchy.
RECOMMENDED_INCOME_TAXONOMY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Earned Income", ("Salary", "Bonus", "Business")),
    ("Investment Income", ("Interest & Dividends", "Capital Gains", "Rental")),
    ("Other Income", ("Gifts", "Refunds & Rebates")),
)

RECOMMENDED_TAXONOMIES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    SPENDING_TAXONOMY: RECOMMENDED_SPENDING_TAXONOMY,
    INCOME_TAXONOMY: RECOMMENDED_INCOME_TAXONOMY,
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def normalize_key(name: str) -> str:
    """Derive a stable, comparable key from a human category name."""
    slug = _SLUG_STRIP.sub("_", str(name or "").strip().lower()).strip("_")
    if not slug:
        raise DecisionError(f"category name has no usable key: {name!r}")
    return slug


@dataclass(frozen=True)
class DesiredCategory:
    """One node of the recommended hierarchy."""

    key: str
    name: str
    parent_key: str | None
    depth: int
    sort_order: int


@dataclass(frozen=True)
class TaxonomyOperation:
    """One reviewable change (or non-change) in a taxonomy plan."""

    # "create" | "rename" | "reparent" | "keep" | "review"
    action: str
    key: str
    name: str
    reason: str
    parent_key: str | None = None
    category_id: str | None = None
    parent_category_id: str | None = None
    current_name: str | None = None
    current_parent_id: str | None = None
    sort_order: int = 0


def build_desired_categories(
    spec: Sequence[tuple[str, Sequence[str]]] | None = None,
) -> list[DesiredCategory]:
    """Flatten a ``(group, (child, ...))`` spec into depth-checked nodes."""
    spec = RECOMMENDED_SPENDING_TAXONOMY if spec is None else spec
    desired: list[DesiredCategory] = []
    seen: set[str] = set()
    for group_order, entry in enumerate(spec):
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise DecisionError("taxonomy spec entries must be (name, children) pairs")
        group_name, children = entry
        group_key = normalize_key(group_name)
        if group_key in seen:
            raise DecisionError(f"duplicate category key in taxonomy spec: {group_key}")
        seen.add(group_key)
        desired.append(
            DesiredCategory(group_key, str(group_name), None, 0, group_order)
        )
        for child_order, child_name in enumerate(children or ()):
            child_key = normalize_key(child_name)
            if child_key in seen:
                raise DecisionError(f"duplicate category key in taxonomy spec: {child_key}")
            seen.add(child_key)
            desired.append(
                DesiredCategory(child_key, str(child_name), group_key, 1, child_order)
            )
    for node in desired:
        if node.depth > MAX_DEPTH:
            raise DecisionError(
                f"taxonomy spec exceeds the {MAX_DEPTH + 1}-level limit at {node.key}"
            )
    return desired


def _live_index(live_categories: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in live_categories:
        if not isinstance(row, dict):
            raise DecisionError("live taxonomy contains a non-object category")
        category_id = row.get("id")
        if not isinstance(category_id, str) or not category_id:
            raise DecisionError("live taxonomy contains a category without an id")
        rows[category_id] = row
    return rows


def _live_depth(row: dict[str, Any], rows: dict[str, dict[str, Any]]) -> int:
    """Depth of a live category, saturating on a cycle rather than hanging."""
    depth = 0
    seen: set[str] = {str(row.get("id"))}
    parent = row.get("parentId")
    while isinstance(parent, str) and parent in rows and parent not in seen:
        seen.add(parent)
        depth += 1
        if depth > len(rows):
            break
        parent = rows[parent].get("parentId")
    return depth


def _match_key(row: dict[str, Any]) -> str | None:
    raw_key = row.get("key")
    if isinstance(raw_key, str) and raw_key.strip():
        try:
            return normalize_key(raw_key)
        except DecisionError:
            pass
    name = row.get("name")
    if isinstance(name, str) and name.strip():
        try:
            return normalize_key(name)
        except DecisionError:
            return None
    return None


def plan_taxonomy_difference(
    live_categories: Iterable[dict[str, Any]],
    desired: Sequence[DesiredCategory] | None = None,
    *,
    taxonomy_id: str = SPENDING_TAXONOMY,
) -> dict[str, Any]:
    """Diff a live taxonomy against the recommended compact hierarchy.

    Returns an ordered, reviewable operation list. Creates come before the
    moves that depend on them, and no operation ever deletes a live category:
    anything unrecognised is surfaced as ``review`` for a human to decide,
    because a delete would silently detach historical activity assignments.
    """
    desired = build_desired_categories() if desired is None else list(desired)
    rows = _live_index(live_categories)
    by_key: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for row in rows.values():
        key = _match_key(row)
        if key is None:
            duplicates.append(row)
            continue
        if key in by_key:
            duplicates.append(row)
            continue
        by_key[key] = row

    operations: list[TaxonomyOperation] = []
    matched_ids: set[str] = set()

    # Top-level groups first, so a child's new parent exists before the move.
    for node in sorted(desired, key=lambda item: (item.depth, item.sort_order, item.key)):
        live = by_key.get(node.key)
        parent_live = by_key.get(node.parent_key) if node.parent_key else None
        parent_id = parent_live.get("id") if isinstance(parent_live, dict) else None
        if live is None:
            operations.append(
                TaxonomyOperation(
                    action="create",
                    key=node.key,
                    name=node.name,
                    reason="missing from the live taxonomy",
                    parent_key=node.parent_key,
                    parent_category_id=parent_id,
                    sort_order=node.sort_order,
                )
            )
            continue
        matched_ids.add(str(live.get("id")))
        current_name = live.get("name") if isinstance(live.get("name"), str) else ""
        current_parent = live.get("parentId") if isinstance(live.get("parentId"), str) else None
        # The recommended parent may itself be a create emitted earlier in this
        # same plan, in which case its id is not known yet and ``parent_id`` is
        # None. A live top-level child then has ``current_parent == parent_id
        # == None`` and would look settled, so the plan would create the parent
        # and leave the child stranded at the top level. Track that pending
        # parent explicitly and always emit the dependent reparent, carrying
        # ``parent_key`` so the applier can resolve the id once the create runs.
        parent_pending = bool(node.parent_key) and parent_live is None
        if current_name != node.name:
            operations.append(
                TaxonomyOperation(
                    action="rename",
                    key=node.key,
                    name=node.name,
                    reason="live name differs from the recommended name",
                    parent_key=node.parent_key,
                    category_id=str(live.get("id")),
                    parent_category_id=parent_id,
                    current_name=current_name,
                    current_parent_id=current_parent,
                    sort_order=node.sort_order,
                )
            )
        if parent_pending or current_parent != parent_id:
            operations.append(
                TaxonomyOperation(
                    action="reparent",
                    key=node.key,
                    name=node.name,
                    reason=(
                        "recommended parent is created by this plan"
                        if parent_pending
                        else "live parent differs from the recommended parent"
                        if node.parent_key
                        else "category should be top-level"
                    ),
                    parent_key=node.parent_key,
                    category_id=str(live.get("id")),
                    parent_category_id=parent_id,
                    current_name=current_name,
                    current_parent_id=current_parent,
                    sort_order=node.sort_order,
                )
            )
        if not parent_pending and current_name == node.name and current_parent == parent_id:
            operations.append(
                TaxonomyOperation(
                    action="keep",
                    key=node.key,
                    name=node.name,
                    reason="already matches the recommended hierarchy",
                    parent_key=node.parent_key,
                    category_id=str(live.get("id")),
                    parent_category_id=parent_id,
                    current_name=current_name,
                    current_parent_id=current_parent,
                    sort_order=node.sort_order,
                )
            )

    for row in sorted(rows.values(), key=lambda item: str(item.get("name") or item.get("id"))):
        category_id = str(row.get("id"))
        if category_id in matched_ids:
            continue
        depth = _live_depth(row, rows)
        reason = "not part of the recommended hierarchy; review before removing"
        if depth > MAX_DEPTH:
            reason = (
                f"nested {depth + 1} levels deep, deeper than the "
                f"{MAX_DEPTH + 1}-level limit; flatten or merge it"
            )
        elif row in duplicates:
            reason = "collides with another live category key; rename one of them"
        operations.append(
            TaxonomyOperation(
                action="review",
                key=_match_key(row) or category_id,
                name=str(row.get("name") or ""),
                reason=reason,
                category_id=category_id,
                current_name=str(row.get("name") or ""),
                current_parent_id=(
                    row.get("parentId") if isinstance(row.get("parentId"), str) else None
                ),
            )
        )

    counts: dict[str, int] = {}
    for operation in operations:
        counts[operation.action] = counts.get(operation.action, 0) + 1

    plan = {
        "kind": "taxonomy-plan",
        "version": 1,
        "taxonomyId": taxonomy_id,
        "maxDepth": MAX_DEPTH,
        "counts": {action: counts.get(action, 0) for action in sorted(counts)},
        "liveCategoryCount": len(rows),
        "desiredCategoryCount": len(desired),
        "operations": [asdict(operation) for operation in operations],
    }
    plan["fingerprint"] = plan_fingerprint(plan)
    return plan


def summarize_plan(plan: dict[str, Any]) -> str:
    """A PII-free one-line summary safe to print to a terminal."""
    counts = plan.get("counts") or {}
    parts = [f"{action}={counts[action]}" for action in sorted(counts)]
    return (
        f"{plan.get('taxonomyId')}: {plan.get('liveCategoryCount')} live categories, "
        f"{plan.get('desiredCategoryCount')} recommended; " + ", ".join(parts)
    )


def write_taxonomy_plan(
    plan: dict[str, Any], output: Path, data_dir: Path, repo_root: Path
) -> Path:
    """Write a plan under the private data directory, never into the repo."""
    target = validate_private_output(output, data_dir, repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
