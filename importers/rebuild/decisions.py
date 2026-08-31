"""Plan and apply canonical exclusions and transfer links."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol


class DecisionError(RuntimeError):
    """A durable decision cannot be applied safely."""


class Client(Protocol):
    def iter_activities(self, page_size: int = 1000): ...
    def post(self, path: str, payload: dict): ...
    def put(self, path: str, payload: dict): ...
    def save_activities(self, delete_ids: list[str]): ...


@dataclass(frozen=True)
class DecisionPlan:
    delete_ids: tuple[str, ...]
    unlink_pairs: tuple[tuple[str, str], ...]
    unresolved_linked_exclusions: tuple[str, ...]
    type_updates: tuple[dict, ...]
    link_pairs: tuple[tuple[str, str], ...]
    missing_exclusions: tuple[str, ...]
    missing_transfer_legs: tuple[str, ...]


def _true(value: str | None) -> bool:
    return (value or "").strip().casefold() == "true"


def load_decisions(path: Path) -> tuple[set[str], list[tuple[tuple[str, str], tuple[str, str]]]]:
    """Load exact activity exclusions and two-leg transfer groups."""
    exclusions: set[str] = set()
    groups: dict[str, list[tuple[str, str]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            source_id = (row.get("source_id") or "").strip()
            if not source_id:
                continue
            if _true(row.get("excluded")):
                exclusions.add(source_id)
            group = (row.get("transfer_group") or "").strip()
            if group and not _true(row.get("excluded")):
                amount = Decimal(row.get("amount") or "0")
                if amount == 0 and ":transfer_in:" in source_id.casefold():
                    direction = "TRANSFER_IN"
                elif amount == 0 and ":transfer_out:" in source_id.casefold():
                    direction = "TRANSFER_OUT"
                else:
                    direction = "TRANSFER_IN" if amount > 0 else "TRANSFER_OUT"
                groups.setdefault(group, []).append((source_id, direction))

    invalid = {group: legs for group, legs in groups.items() if len(legs) != 2}
    if invalid:
        detail = ", ".join(f"{group} ({len(legs)} legs)" for group, legs in invalid.items())
        raise DecisionError(f"transfer groups must have exactly two legs: {detail}")
    return exclusions, [tuple(legs) for _, legs in sorted(groups.items())]


def build_plan(client: Client, canonical_transactions: Path) -> DecisionPlan:
    exclusions, transfer_pairs = load_decisions(canonical_transactions)
    rows = list(client.iter_activities(page_size=1000))
    by_key = {row.get("idempotencyKey"): row for row in rows if row.get("idempotencyKey")}
    by_group: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("sourceGroupId"):
            by_group.setdefault(row["sourceGroupId"], []).append(row)

    delete_rows = [by_key[key] for key in sorted(exclusions) if key in by_key]
    missing_exclusions = tuple(sorted(exclusions - by_key.keys()))
    unlink: set[tuple[str, str]] = set()
    durable_transfer_keys = {
        key for pair in transfer_pairs for key, _direction in pair
    }
    unresolved_linked: list[str] = []
    for row in delete_rows:
        group = row.get("sourceGroupId")
        if not group:
            continue
        members = by_group.get(group, [])
        if len(members) != 2:
            raise DecisionError(f"linked exclusion {row['id']} has {len(members)} group members")
        unlink.add(tuple(sorted((members[0]["id"], members[1]["id"]))))
        counterpart = next(member for member in members if member["id"] != row["id"])
        if counterpart.get("idempotencyKey") not in durable_transfer_keys:
            unresolved_linked.append(str(row.get("idempotencyKey") or row["id"]))

    links: list[tuple[str, str]] = []
    type_updates: list[dict] = []
    missing_legs: list[str] = []
    scheduled_unlink_ids = {activity_id for pair in unlink for activity_id in pair}
    for ((left_key, left_type), (right_key, right_type)) in transfer_pairs:
        left, right = by_key.get(left_key), by_key.get(right_key)
        if not left or not right:
            missing_legs.extend(key for key, row in ((left_key, left), (right_key, right)) if not row)
            continue
        left_group = (
            None if left["id"] in scheduled_unlink_ids else left.get("sourceGroupId")
        )
        right_group = (
            None if right["id"] in scheduled_unlink_ids else right.get("sourceGroupId")
        )
        if left_group and left_group == right_group:
            continue
        if left_group or right_group:
            raise DecisionError(
                f"canonical transfer leg is already linked to another activity: "
                f"{left_key if left_group else right_key}"
            )
        for row, wanted in ((left, left_type), (right, right_type)):
            if row.get("activityType") != wanted:
                type_updates.append(
                    {
                        "id": row["id"],
                        "accountId": row["accountId"],
                        "activityType": wanted,
                        "activityDate": row["date"],
                        "currency": row["currency"],
                        "amount": row["amount"],
                        "isDraft": False,
                    }
                )
        links.append((left["id"], right["id"]))

    return DecisionPlan(
        tuple(row["id"] for row in delete_rows),
        tuple(sorted(unlink)),
        tuple(sorted(unresolved_linked)),
        tuple(type_updates),
        tuple(links),
        missing_exclusions,
        tuple(sorted(set(missing_legs))),
    )


def apply_plan(client: Client, plan: DecisionPlan, max_deletes: int = 500) -> None:
    """Apply only exact-id operations, unlinking before any surgical deletion."""
    if plan.missing_transfer_legs:
        raise DecisionError(
            f"{len(plan.missing_transfer_legs)} canonical transfer legs are missing"
        )
    if plan.unresolved_linked_exclusions:
        raise DecisionError(
            f"{len(plan.unresolved_linked_exclusions)} linked exclusions have no "
            "durable replacement transfer pair"
        )
    if len(plan.delete_ids) > max_deletes:
        raise DecisionError(
            f"refusing {len(plan.delete_ids)} deletions; limit is {max_deletes}"
        )
    for left, right in plan.unlink_pairs:
        client.post("/activities/unlink", {"activityAId": left, "activityBId": right})
    for start in range(0, len(plan.delete_ids), 100):
        client.save_activities(delete_ids=list(plan.delete_ids[start : start + 100]))
    for update in plan.type_updates:
        client.put("/activities", update)
    for left, right in plan.link_pairs:
        client.post("/activities/link", {"activityAId": left, "activityBId": right})
