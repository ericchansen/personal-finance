import csv
from pathlib import Path

import pytest

from importers.rebuild.decisions import DecisionError, apply_plan, build_plan


FIELDS = [
    "date", "account_id", "amount", "description", "source_id", "source_file",
    "category", "transfer_group", "excluded", "exclusion_reason",
]


def write_canonical(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.posts = []
        self.deletes = []

    def iter_activities(self, page_size=1000):
        yield from self.rows

    def post(self, path, payload):
        self.posts.append((path, payload))

    def put(self, path, payload):
        self.posts.append((path, payload))

    def save_activities(self, delete_ids):
        self.deletes.extend(delete_ids)


def row(source_id, *, amount="1", group="", excluded="false"):
    return {
        "date": "2025-01-01", "account_id": "account", "amount": amount,
        "description": "synthetic", "source_id": source_id, "source_file": "fixture.csv",
        "category": "", "transfer_group": group, "excluded": excluded,
        "exclusion_reason": "",
    }


def test_plan_unlinks_exact_exclusion_and_links_canonical_pair(tmp_path):
    canonical = tmp_path / "transactions.csv"
    write_canonical(canonical, [
        row("monarch:duplicate", excluded="true"),
        row("monarch:in", group="transfer-1"),
        row("monarch:out", amount="-1", group="transfer-1"),
    ])
    client = FakeClient([
        {"id": "drop", "idempotencyKey": "monarch:duplicate", "sourceGroupId": "old"},
        {"id": "keep", "idempotencyKey": "monarch:keep", "sourceGroupId": "old"},
        {"id": "in", "idempotencyKey": "monarch:in", "sourceGroupId": None,
         "accountId": "account", "activityType": "DEPOSIT", "date": "2025-01-01",
         "currency": "USD", "amount": 1},
        {"id": "out", "idempotencyKey": "monarch:out", "sourceGroupId": None,
         "accountId": "account", "activityType": "WITHDRAWAL", "date": "2025-01-01",
         "currency": "USD", "amount": 1},
    ])

    plan = build_plan(client, canonical)
    assert plan.delete_ids == ("drop",)
    assert plan.unlink_pairs == (("drop", "keep"),)
    assert plan.unresolved_linked_exclusions == ("monarch:duplicate",)
    assert len(plan.type_updates) == 2
    assert plan.link_pairs == (("in", "out"),)

    with pytest.raises(DecisionError, match="replacement transfer pair"):
        apply_plan(client, plan)
    assert client.posts == []
    assert client.deletes == []


def test_apply_refuses_broad_delete(tmp_path):
    canonical = tmp_path / "transactions.csv"
    write_canonical(canonical, [row(f"monarch:{i}", excluded="true") for i in range(3)])
    client = FakeClient([
        {"id": str(i), "idempotencyKey": f"monarch:{i}", "sourceGroupId": None}
        for i in range(3)
    ])
    with pytest.raises(DecisionError, match="limit"):
        apply_plan(client, build_plan(client, canonical), max_deletes=2)


def test_apply_refuses_partial_transfer_plan(tmp_path):
    canonical = tmp_path / "transactions.csv"
    write_canonical(canonical, [
        row("monarch:in", group="transfer-1"),
        row("monarch:missing", amount="-1", group="transfer-1"),
    ])
    client = FakeClient([{
        "id": "in", "idempotencyKey": "monarch:in", "sourceGroupId": None,
        "accountId": "account", "activityType": "TRANSFER_IN",
        "date": "2025-01-01", "currency": "USD", "amount": 1,
    }])

    plan = build_plan(client, canonical)
    assert plan.missing_transfer_legs == ("monarch:missing",)
    with pytest.raises(DecisionError, match="transfer legs are missing"):
        apply_plan(client, plan)
    assert client.posts == []
    assert client.deletes == []


def test_plan_relinks_survivor_after_duplicate_leg_is_deleted(tmp_path):
    canonical = tmp_path / "transactions.csv"
    write_canonical(canonical, [
        row("monarch:duplicate", excluded="true"),
        row("monarch:survivor", group="replacement"),
        row("monarch:replacement", amount="-1", group="replacement"),
    ])
    client = FakeClient([
        {"id": "drop", "idempotencyKey": "monarch:duplicate", "sourceGroupId": "old"},
        {"id": "survivor", "idempotencyKey": "monarch:survivor",
         "sourceGroupId": "old", "accountId": "card", "activityType": "TRANSFER_IN",
         "date": "2025-01-01", "currency": "USD", "amount": 1},
        {"id": "replacement", "idempotencyKey": "monarch:replacement",
         "sourceGroupId": None, "accountId": "cash", "activityType": "TRANSFER_OUT",
         "date": "2025-01-02", "currency": "USD", "amount": 1},
    ])

    plan = build_plan(client, canonical)

    assert plan.unresolved_linked_exclusions == ()
    assert plan.unlink_pairs == (("drop", "survivor"),)
    assert plan.link_pairs == (("survivor", "replacement"),)


def test_zero_value_asset_transfer_uses_key_direction(tmp_path):
    canonical = tmp_path / "transactions.csv"
    write_canonical(canonical, [
        row("vanguard-history:x:transfer_in:fund", amount="0", group="transfer"),
        row("vanguard-history:x:transfer_out:fund", amount="0", group="transfer"),
    ])
    client = FakeClient([
        {"id": "in", "idempotencyKey": "vanguard-history:x:transfer_in:fund",
         "sourceGroupId": None, "accountId": "one", "activityType": "TRANSFER_IN",
         "date": "2025-01-01", "currency": "USD", "amount": 0},
        {"id": "out", "idempotencyKey": "vanguard-history:x:transfer_out:fund",
         "sourceGroupId": None, "accountId": "two", "activityType": "TRANSFER_OUT",
         "date": "2025-01-01", "currency": "USD", "amount": 0},
    ])
    plan = build_plan(client, canonical)
    assert plan.type_updates == ()
    assert plan.link_pairs == (("in", "out"),)
