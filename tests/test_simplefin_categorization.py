import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint
from importers.simplefin.categorize_cli import cmd_plan
from importers.simplefin.categorization import (
    INCOME_TAXONOMY,
    REPO_ROOT,
    SPENDING_TAXONOMY,
    HistoryIndex,
    build_category_plan,
    evidence_binding,
    load_history,
    load_or_create_hash_key,
    merchant_hash,
    rehearse_category_plan,
    validate_category_plan,
    write_category_review,
)

NOW = datetime(2026, 8, 28, tzinfo=timezone.utc)
HASH_KEY = b"synthetic-private-hmac-key-00001"


def catalogs():
    return {
        SPENDING_TAXONOMY: [
            {"id": "groceries", "name": "Groceries"},
            {"id": "bars", "name": "Bars & Alcohol"},
            {"id": "other", "name": "Other Expenses"},
        ],
        INCOME_TAXONOMY: [{"id": "salary", "name": "Salary"}],
    }


def reviewed():
    return {
        "accounts": [{
            "sourceAccountId": "source-account",
            "assertionAccountId": "canonical-account",
            "wealthfolioAccountId": "live-account",
            "transactions": [
                {"sourceId": f"source-{number}"} for number in range(1, 8)
            ],
        }]
    }


def activity(
    number=1,
    *,
    description="Synthetic Market",
    kind="WITHDRAWAL",
    amount="12.34",
    subtype=None,
    metadata=None,
    group=None,
):
    return {
        "id": f"activity-{number}",
        "accountId": "live-account",
        "activityType": kind,
        "date": "2026-08-26T00:00:00Z",
        "amount": amount,
        "currency": "USD",
        "comment": description,
        "idempotencyKey": f"simplefin:live-account:source-{number}",
        "subtype": subtype,
        "metadata": metadata,
        "sourceGroupId": group,
    }


def build(activities, history, *, decisions=None, assignments=None):
    return build_category_plan(
        activities,
        [{"id": "live-account", "accountType": "CASH"}],
        assignments or {row["id"]: [] for row in activities},
        catalogs(),
        reviewed(),
        history,
        decisions or {
            "schemaVersion": 1,
            "categoryAliases": {},
            "merchantOverrides": [],
            "activityOverrides": [],
        },
        [],
        "production-instance",
        HASH_KEY,
        report_start="2026-08-01",
        report_end="2026-08-31",
        current_report={"current": {"income": 0, "outflow": "12.34", "net": "-12.34"}},
        current_uncategorized_count=1,
        generated_at=NOW,
    )


def test_exact_account_history_with_consensus_is_automatic_and_private():
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")

    plan = build([activity()], history)

    assert plan["metrics"]["autoCount"] == 1
    assert plan["autoCandidates"][0]["categoryId"] == "groceries"
    assert plan["autoCandidates"][0]["confidence"] == "0.98"
    assert "Synthetic Market" not in json.dumps(plan)
    assert plan["cashFlowExcludingTransfersAndReconciliation"]["spending"] == "12.34"


def test_conflicting_merchant_is_never_automatic():
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("other-account", "Synthetic Market", "Alcohol", "history-2")

    plan = build([activity()], history)

    assert plan["metrics"]["autoCount"] == 0
    assert plan["manualItems"][0]["reason"] == "conflicting-history"


def test_single_historical_observation_remains_manual():
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")

    plan = build([activity()], history)

    assert plan["manualItems"][0]["reason"] == "insufficient-history"
    assert plan["manualItems"][0]["candidate"]["categoryId"] == "groceries"


def test_global_history_counts_same_source_id_in_distinct_accounts():
    history = HistoryIndex()
    history.add("history-account-one", "Synthetic Market", "Groceries", "shared-id")
    history.add("history-account-two", "Synthetic Market", "Groceries", "shared-id")

    plan = build([activity()], history)

    assert plan["metrics"]["autoCount"] == 1
    assert plan["autoCandidates"][0]["evidenceKind"] == "global-history"
    assert plan["autoCandidates"][0]["evidenceCount"] == 2


def test_account_aware_private_override_can_resolve_a_conflict():
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("other-account", "Synthetic Market", "Alcohol", "history-2")
    decisions = {
        "schemaVersion": 1,
        "categoryAliases": {},
        "merchantOverrides": [{
            "merchantHash": merchant_hash("Synthetic Market", HASH_KEY),
            "canonicalAccountId": "canonical-account",
            "taxonomyId": SPENDING_TAXONOMY,
            "categoryId": "groceries",
            "rationale": "Reviewed synthetic exception.",
        }],
        "activityOverrides": [],
    }

    plan = build([activity()], history, decisions=decisions)

    assert plan["metrics"]["autoCount"] == 1
    assert plan["autoCandidates"][0]["evidenceKind"] == "merchant-override"
    assert plan["autoCandidates"][0]["confidence"] == "1.00"


def test_conflicting_override_types_fail_closed():
    decisions = {
        "schemaVersion": 1,
        "categoryAliases": {},
        "merchantOverrides": [{
            "merchantHash": merchant_hash("Synthetic Market", HASH_KEY),
            "taxonomyId": SPENDING_TAXONOMY,
            "categoryId": "groceries",
            "rationale": "Synthetic merchant ruling.",
        }],
        "activityOverrides": [{
            "sourceAccountId": "source-account",
            "sourceId": "source-1",
            "taxonomyId": SPENDING_TAXONOMY,
            "categoryId": "other",
            "rationale": "Synthetic activity ruling.",
        }],
    }

    with pytest.raises(DecisionError, match="multiple private category overrides"):
        build([activity()], HistoryIndex(), decisions=decisions)


def test_override_taxonomy_must_match_cash_flow_direction():
    decisions = {
        "schemaVersion": 1,
        "categoryAliases": {},
        "merchantOverrides": [],
        "activityOverrides": [{
            "sourceAccountId": "source-account",
            "sourceId": "source-1",
            "taxonomyId": INCOME_TAXONOMY,
            "categoryId": "salary",
            "rationale": "Synthetic wrong-direction ruling.",
        }],
    }

    with pytest.raises(DecisionError, match="cash-flow direction"):
        build([activity()], HistoryIndex(), decisions=decisions)


def test_monarch_statement_text_is_an_alias_for_canonical_category(tmp_path):
    canonical = tmp_path / "transactions.csv"
    with canonical.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "account_id", "description", "source_id", "category",
                "transfer_group", "excluded",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "account_id": "canonical-account",
            "description": "Clean Merchant",
            "source_id": "monarch:mon-1",
            "category": "Groceries",
            "transfer_group": "",
            "excluded": "false",
        })
    monarch = tmp_path / "monarch.csv"
    with monarch.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=["Id", "Merchant", "Original Statement"]
        )
        writer.writeheader()
        writer.writerow({
            "Id": "mon-1",
            "Merchant": "Clean Merchant",
            "Original Statement": "RAW SYNTHETIC MARKET 123",
        })

    history = load_history(canonical, monarch)

    assert "Groceries" in history.by_account[
        ("canonical-account", "raw synthetic market 123")
    ]


def test_monarch_alias_preserves_same_source_id_across_accounts(tmp_path):
    canonical = tmp_path / "transactions.csv"
    with canonical.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "account_id", "description", "source_id", "category",
                "transfer_group", "excluded",
            ],
        )
        writer.writeheader()
        for account_id in ("canonical-one", "canonical-two"):
            writer.writerow({
                "account_id": account_id,
                "description": "Clean Merchant",
                "source_id": "monarch:shared",
                "category": "Groceries",
                "transfer_group": "",
                "excluded": "false",
            })
    monarch = tmp_path / "monarch.csv"
    with monarch.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=["Id", "Merchant", "Original Statement"]
        )
        writer.writeheader()
        writer.writerow({
            "Id": "shared",
            "Merchant": "Clean Merchant",
            "Original Statement": "RAW SYNTHETIC MARKET 123",
        })

    history = load_history(canonical, monarch)

    evidence = history.global_merchants["raw synthetic market 123"]["Groceries"]
    assert evidence == {
        ("canonical-one", "monarch:shared"),
        ("canonical-two", "monarch:shared"),
    }


def test_transfers_and_external_reconciliation_are_not_consumption():
    rows = [
        activity(1, kind="TRANSFER_IN", amount="25", group="pair"),
        activity(
            2,
            kind="TRANSFER_OUT",
            amount="50",
            subtype="external_transfer",
            metadata='{"flow":{"is_external":true}}',
        ),
    ]

    plan = build(rows, HistoryIndex())

    assert plan["metrics"]["eligibleNonTransfers"] == 0
    assert plan["metrics"]["externalReconciliationCount"] == 1
    assert plan["cashFlowExcludingTransfersAndReconciliation"] == {
        "income": "0.00",
        "spending": "0.00",
        "net": "0.00",
    }
    assert {
        row["classification"] for row in plan["transfersAndReconciliation"]
    } == {"paired-transfer", "external-reconciliation"}


@pytest.mark.parametrize(
    "marker",
    (
        {"subtype": "external_transfer"},
        {"metadata": '{"flow":{"is_external":true}}'},
        {"metadata": {"flow": {"is_external": True}}},
    ),
)
def test_external_marker_excludes_nontransfer_activity_from_categories(marker):
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")

    plan = build([{**activity(kind="WITHDRAWAL"), **marker}], history)

    assert plan["metrics"]["eligibleNonTransfers"] == 0
    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["manualCount"] == 0
    assert plan["metrics"]["externalReconciliationCount"] == 1
    assert plan["transfersAndReconciliation"][0]["classification"] == (
        "external-reconciliation"
    )


def test_known_gap_activity_is_reported_separately():
    gap = {
        **activity(7, kind="TRANSFER_OUT", amount="50"),
        "id": "known-gap",
        "idempotencyKey": "gap:canonical-account:2026-08-26",
        "subtype": "external_transfer",
        "metadata": '{"flow":{"is_external":true}}',
    }

    plan = build_category_plan(
        [activity(), gap],
        [{"id": "live-account", "accountType": "CASH"}],
        {"activity-1": []},
        catalogs(),
        reviewed(),
        HistoryIndex(),
        {
            "schemaVersion": 1,
            "categoryAliases": {},
            "merchantOverrides": [],
            "activityOverrides": [],
        },
        [],
        "production-instance",
        HASH_KEY,
        report_start="2026-08-01",
        report_end="2026-08-31",
        known_artifact_activity_ids={"known-gap"},
        current_report={"current": {"income": 0, "outflow": "50", "net": "-50"}},
        generated_at=NOW,
    )

    assert plan["metrics"]["simplefinActivities"] == 1
    assert plan["metrics"]["knownGapArtifactCount"] == 1
    assert plan["metrics"]["knownGapArtifactAmount"] == "50.00"
    assert plan["comparison"]["wealthfolioSpendingExcludingKnownGapArtifact"] == (
        "0.00"
    )
    assert plan["balanceGapReconciliation"][0]["categoryAction"] == "none"


def test_private_markdown_review_contains_hashes_not_merchants(tmp_path):
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")
    plan = build([activity()], history)

    path = write_category_review(tmp_path, plan)
    report = path.read_text(encoding="utf-8")

    assert "Synthetic Market" not in report
    assert merchant_hash("Synthetic Market", HASH_KEY)[:16] in report


def test_changed_evidence_invalidates_plan(tmp_path):
    evidence = tmp_path / "history.csv"
    evidence.write_text("before", encoding="utf-8")
    history = HistoryIndex()
    plan = build([activity()], history)
    plan["evidence"] = evidence_binding([evidence])
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    evidence.write_text("after", encoding="utf-8")

    with pytest.raises(DecisionError, match="evidence hash changed"):
        validate_category_plan(plan)


class StageClient:
    def __init__(
        self, rows, fail_on_put=None, commit_then_fail=False, backup=True
    ):
        self.rows = rows
        self.assignments = {row["id"]: [] for row in rows}
        self.fail_on_put = fail_on_put
        self.commit_then_fail = commit_then_fail
        self.backup = backup
        self.puts = 0
        self.deletes = 0

    def iter_activities(self):
        return iter(self.rows)

    def get(self, path):
        activity_id = path.split("/")[3]
        return self.assignments[activity_id]

    def put(self, path, payload):
        activity_id = path.split("/")[3]
        self.puts += 1
        if self.puts == self.fail_on_put:
            if self.commit_then_fail:
                self.assignments[activity_id] = [payload]
            raise RuntimeError("synthetic failure")
        self.assignments[activity_id] = [payload]

    def delete(self, path):
        activity_id = path.split("/")[3]
        self.deletes += 1
        self.assignments[activity_id] = []

    def backup_database(self):
        return {"filename": "synthetic-backup.db"} if self.backup else None


def staged_activity(source_id="source-1", activity_id="staging-activity"):
    row = activity()
    row["id"] = activity_id
    row["accountId"] = "staging-account"
    row["idempotencyKey"] = f"simplefin:staging-account:{source_id}"
    return row


def automatic_plan(tmp_path):
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")
    evidence = tmp_path / "synthetic-evidence.csv"
    evidence.write_text("synthetic", encoding="utf-8")
    plan = build([activity()], history)
    plan["evidence"] = evidence_binding([evidence])
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    return plan


def test_staging_rehearsal_is_idempotent(tmp_path):
    plan = automatic_plan(tmp_path)
    client = StageClient([staged_activity()])

    first = rehearse_category_plan(
        client,
        plan,
        {"canonical-account": "staging-account"},
        HASH_KEY,
        generated_at=NOW,
    )
    second = rehearse_category_plan(
        client,
        plan,
        {"canonical-account": "staging-account"},
        HASH_KEY,
        generated_at=NOW,
    )

    assert first["status"] == "applied"
    assert second["status"] == "already-applied"
    assert client.puts == 1


def test_staging_rehearsal_rolls_back_partial_failure(tmp_path):
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")
    plan = build([activity(1), activity(2)], history)
    evidence = tmp_path / "synthetic-evidence.csv"
    evidence.write_text("synthetic", encoding="utf-8")
    plan["evidence"] = evidence_binding([evidence])
    from importers.rebuild.safety import plan_fingerprint

    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    client = StageClient(
        [
            staged_activity("source-1", "staging-activity-1"),
            staged_activity("source-2", "staging-activity-2"),
        ],
        fail_on_put=2,
    )

    with pytest.raises(RuntimeError, match="synthetic failure"):
        rehearse_category_plan(
            client,
            plan,
            {"canonical-account": "staging-account"},
            HASH_KEY,
            generated_at=NOW,
        )

    assert client.assignments["staging-activity-1"] == []
    assert client.deletes == 1


def test_ambiguous_put_is_rolled_back(tmp_path):
    plan = automatic_plan(tmp_path)
    client = StageClient(
        [staged_activity()],
        fail_on_put=1,
        commit_then_fail=True,
    )

    with pytest.raises(RuntimeError, match="synthetic failure"):
        rehearse_category_plan(
            client,
            plan,
            {"canonical-account": "staging-account"},
            HASH_KEY,
            generated_at=NOW,
        )

    assert client.assignments["staging-activity"] == []
    assert client.deletes == 1


def test_rehearsal_requires_confirmed_backup(tmp_path):
    plan = automatic_plan(tmp_path)
    client = StageClient([staged_activity()], backup=False)

    with pytest.raises(DecisionError, match="backup was not confirmed"):
        rehearse_category_plan(
            client,
            plan,
            {"canonical-account": "staging-account"},
            HASH_KEY,
            generated_at=NOW,
        )

    assert client.puts == 0


def test_report_window_excludes_activities_before_classification_and_rehearsal(
    tmp_path,
):
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")
    plan = build_category_plan(
        [
            activity(1, amount="12.34"),
            {**activity(2, amount="40.00"), "date": "2025-08-26T00:00:00Z"},
        ],
        [{"id": "live-account", "accountType": "CASH"}],
        {"activity-1": [], "activity-2": []},
        catalogs(),
        reviewed(),
        history,
        {
            "schemaVersion": 1,
            "categoryAliases": {},
            "merchantOverrides": [],
            "activityOverrides": [],
        },
        [],
        "production-instance",
        HASH_KEY,
        report_start="2026-08-01",
        report_end="2026-08-31",
        spending_account_ids={"live-account"},
        generated_at=NOW,
    )

    assert plan["metrics"]["simplefinActivities"] == 1
    assert plan["metrics"]["eligibleNonTransfers"] == 1
    assert plan["metrics"]["autoCount"] == 1
    assert plan["metrics"]["manualCount"] == 0
    assert plan["cashFlowExcludingTransfersAndReconciliation"]["spending"] == "12.34"
    assert "activity-2" not in json.dumps(plan)

    evidence = tmp_path / "synthetic-evidence.csv"
    evidence.write_text("synthetic", encoding="utf-8")
    plan["evidence"] = evidence_binding([evidence])
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    receipt = rehearse_category_plan(
        StageClient([staged_activity()]),
        plan,
        {"canonical-account": "staging-account"},
        HASH_KEY,
        generated_at=NOW,
    )
    assert receipt["targetCount"] == 1
    assert receipt["appliedCount"] == 1


def test_rehearsal_rejects_candidate_outside_sealed_window(tmp_path):
    plan = automatic_plan(tmp_path)
    plan["autoCandidates"][0]["date"] = "2025-08-26"
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })

    with pytest.raises(DecisionError, match="escapes its review window"):
        rehearse_category_plan(
            StageClient([staged_activity()]),
            plan,
            {"canonical-account": "staging-account"},
            HASH_KEY,
            generated_at=NOW,
        )


def test_outside_repo_invocation_rejects_data_dir_inside_repo(
    tmp_path, monkeypatch
):
    private_path = REPO_ROOT / ".synthetic-private-data"
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DecisionError, match="inside the repository"):
        cmd_plan(
            SimpleNamespace(data_dir=private_path),
            client=object(),
        )
    assert not private_path.exists()


def test_hash_key_creation_rejects_data_dir_inside_repo(tmp_path, monkeypatch):
    private_path = REPO_ROOT / ".synthetic-private-key"
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DecisionError, match="inside the repository"):
        load_or_create_hash_key(private_path)
    assert not private_path.exists()
