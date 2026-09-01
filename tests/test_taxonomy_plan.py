"""Synthetic tests for compact taxonomy difference planning.

Every category here is invented. The planner never contacts Wealthfolio and
never writes anything, so these tests exercise it purely in memory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from importers.rebuild.decisions import DecisionError
from importers.simplefin.spending_adapter import SPENDING_TAXONOMY
from importers.simplefin.taxonomy_plan import (
    MAX_DEPTH,
    RECOMMENDED_INCOME_TAXONOMY,
    RECOMMENDED_SPENDING_TAXONOMY,
    build_desired_categories,
    normalize_key,
    plan_taxonomy_difference,
    summarize_plan,
    write_taxonomy_plan,
)

SPEC = (
    ("Food", ("Groceries", "Dining")),
    ("Housing", ()),
)


def category(
    category_id: str, name: str, key: str | None = None, parent_id: str | None = None
) -> dict:
    return {
        "id": category_id,
        "taxonomyId": SPENDING_TAXONOMY,
        "name": name,
        "key": key if key is not None else normalize_key(name),
        "parentId": parent_id,
        "sortOrder": 0,
    }


def actions(plan: dict, action: str) -> list[dict]:
    return [op for op in plan["operations"] if op["action"] == action]


def test_normalize_key_slugifies_and_rejects_empty():
    assert normalize_key("Parking & Tolls") == "parking_tolls"
    assert normalize_key("  Dining  ") == "dining"
    with pytest.raises(DecisionError):
        normalize_key("   ")


def test_recommended_hierarchies_never_exceed_two_levels():
    for spec in (RECOMMENDED_SPENDING_TAXONOMY, RECOMMENDED_INCOME_TAXONOMY):
        nodes = build_desired_categories(spec)
        assert nodes, "recommended hierarchy must not be empty"
        assert max(node.depth for node in nodes) <= MAX_DEPTH
        assert len({node.key for node in nodes}) == len(nodes)


def test_recommended_spending_hierarchy_stays_compact():
    top_level = [node for node in build_desired_categories() if node.depth == 0]
    assert 5 <= len(top_level) <= 12


def test_build_desired_categories_rejects_duplicate_keys():
    with pytest.raises(DecisionError, match="duplicate"):
        build_desired_categories((("Food", ("Food",)),))


def test_empty_live_taxonomy_produces_only_creates():
    plan = plan_taxonomy_difference([], build_desired_categories(SPEC))
    assert plan["counts"] == {"create": 4}
    assert plan["liveCategoryCount"] == 0
    assert {op["key"] for op in actions(plan, "create")} == {
        "food",
        "groceries",
        "dining",
        "housing",
    }


def test_creates_are_ordered_parent_before_child():
    plan = plan_taxonomy_difference([], build_desired_categories(SPEC))
    keys = [op["key"] for op in plan["operations"]]
    assert keys.index("food") < keys.index("groceries")
    assert keys.index("food") < keys.index("dining")


def test_matching_hierarchy_produces_only_keeps():
    live = [
        category("c-food", "Food"),
        category("c-groc", "Groceries", parent_id="c-food"),
        category("c-dine", "Dining", parent_id="c-food"),
        category("c-house", "Housing"),
    ]
    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))
    assert plan["counts"] == {"keep": 4}


def test_rename_is_planned_when_only_the_name_differs():
    live = [category("c-food", "FOOD & DRINK", key="food")]
    plan = plan_taxonomy_difference(live, build_desired_categories((("Food", ()),)))
    renames = actions(plan, "rename")
    assert len(renames) == 1
    assert renames[0]["category_id"] == "c-food"
    assert renames[0]["current_name"] == "FOOD & DRINK"
    assert renames[0]["name"] == "Food"


def test_reparent_is_planned_and_carries_the_new_parent_id():
    live = [
        category("c-food", "Food"),
        category("c-groc", "Groceries"),
    ]
    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))
    reparents = actions(plan, "reparent")
    assert [op["key"] for op in reparents] == ["groceries"]
    assert reparents[0]["parent_category_id"] == "c-food"
    assert reparents[0]["current_parent_id"] is None


def test_a_child_that_should_be_top_level_is_reparented_to_none():
    live = [
        category("c-food", "Food"),
        category("c-house", "Housing", parent_id="c-food"),
    ]
    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))
    reparents = {op["key"]: op for op in actions(plan, "reparent")}
    assert reparents["housing"]["parent_category_id"] is None
    assert reparents["housing"]["reason"].endswith("top-level")


def test_unknown_live_categories_are_reviewed_never_deleted():
    live = [category("c-x", "Legacy Bucket")]
    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))
    assert not [op for op in plan["operations"] if op["action"] == "delete"]
    reviews = actions(plan, "review")
    assert [op["category_id"] for op in reviews] == ["c-x"]
    assert "review before removing" in reviews[0]["reason"]


def test_too_deep_live_categories_are_flagged_for_flattening():
    live = [
        category("c-food", "Food"),
        category("c-groc", "Groceries", parent_id="c-food"),
        category("c-prod", "Produce", parent_id="c-groc"),
    ]
    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))
    reviews = {op["category_id"]: op for op in actions(plan, "review")}
    assert "c-prod" in reviews
    assert "deeper than the 2-level limit" in reviews["c-prod"]["reason"]


def test_a_parent_cycle_does_not_hang_the_planner():
    live = [
        {**category("c-a", "Alpha"), "parentId": "c-b"},
        {**category("c-b", "Beta"), "parentId": "c-a"},
    ]
    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))
    assert len(actions(plan, "review")) == 2


def test_duplicate_live_keys_are_flagged_rather_than_silently_dropped():
    live = [
        category("c-1", "Food"),
        {**category("c-2", "food"), "key": "food"},
    ]
    plan = plan_taxonomy_difference(live, build_desired_categories((("Food", ()),)))
    reviews = actions(plan, "review")
    assert len(reviews) == 1
    assert "collides" in reviews[0]["reason"]


def test_live_categories_must_carry_an_id():
    with pytest.raises(DecisionError, match="without an id"):
        plan_taxonomy_difference([{"name": "Food"}], build_desired_categories(SPEC))


def test_plan_fingerprint_is_stable_and_shape_sensitive():
    live = [category("c-food", "Food")]
    first = plan_taxonomy_difference(live, build_desired_categories(SPEC))
    second = plan_taxonomy_difference(live, build_desired_categories(SPEC))
    assert first["fingerprint"] == second["fingerprint"]
    changed = plan_taxonomy_difference([], build_desired_categories(SPEC))
    assert changed["fingerprint"] != first["fingerprint"]


def test_summary_line_carries_counts_only():
    plan = plan_taxonomy_difference([category("c-x", "Legacy")], build_desired_categories(SPEC))
    summary = summarize_plan(plan)
    assert "Legacy" not in summary
    assert "review=1" in summary


def test_plan_is_written_only_under_the_private_data_directory(tmp_path: Path):
    data_dir = tmp_path / "private"
    repo_root = tmp_path / "repo"
    data_dir.mkdir()
    repo_root.mkdir()
    plan = plan_taxonomy_difference([], build_desired_categories(SPEC))

    written = write_taxonomy_plan(plan, data_dir / "out" / "plan.json", data_dir, repo_root)
    assert json.loads(written.read_text(encoding="utf-8"))["kind"] == "taxonomy-plan"

    with pytest.raises(DecisionError):
        write_taxonomy_plan(plan, repo_root / "plan.json", data_dir, repo_root)


def test_a_child_whose_parent_this_plan_creates_is_reparented_not_kept():
    """Emitting create-parent + keep-child left the child stranded top-level."""
    live = [category("c-groc", "Groceries")]

    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))

    creates = actions(plan, "create")
    assert "food" in {op["key"] for op in creates}
    assert "groceries" not in {op["key"] for op in creates}

    kept = {op["key"] for op in actions(plan, "keep")}
    assert "groceries" not in kept

    reparents = {op["key"]: op for op in actions(plan, "reparent")}
    groceries = reparents["groceries"]
    assert groceries["category_id"] == "c-groc"
    assert groceries["current_parent_id"] is None
    # The parent has no server id yet, so the plan names it by key instead.
    assert groceries["parent_category_id"] is None
    assert groceries["parent_key"] == "food"
    assert "created by this plan" in groceries["reason"]

    # The dependency is only satisfiable if the parent is created first.
    order = [op["key"] for op in plan["operations"]]
    assert order.index("food") < order.index("groceries")


def test_a_reparent_onto_a_live_parent_still_carries_the_server_id():
    live = [category("c-food", "Food"), category("c-groc", "Groceries")]

    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))

    groceries = next(op for op in actions(plan, "reparent") if op["key"] == "groceries")
    assert groceries["parent_category_id"] == "c-food"
    assert groceries["parent_key"] == "food"


def test_a_child_already_under_a_live_parent_is_kept_not_reparented():
    live = [
        category("c-food", "Food"),
        category("c-groc", "Groceries", parent_id="c-food"),
    ]

    plan = plan_taxonomy_difference(live, build_desired_categories(SPEC))

    assert "groceries" in {op["key"] for op in actions(plan, "keep")}
    assert "groceries" not in {op["key"] for op in actions(plan, "reparent")}
