import csv
import json
import stat
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from importers.monarch.wealthfolio_client import WealthfolioError
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import instance_fingerprint, plan_fingerprint
from importers.simplefin.categorize_cli import cmd_plan, main
from importers.simplefin.categorization import (
    INCOME_TAXONOMY,
    REPO_ROOT,
    SPENDING_TAXONOMY,
    HistoryIndex,
    build_category_plan,
    _expected_spending_snapshot,
    evidence_binding,
    load_category_decisions,
    load_history,
    load_or_create_hash_key,
    merchant_hash,
    promote_category_plan,
    rehearse_category_plan,
    spending_report_snapshot,
    sha256_file,
    validate_category_plan,
    write_category_review,
    write_rehearsal_receipt,
)
from importers.simplefin.category_rules import (
    RuleContext,
    parse_rule_engine,
    propose_v2_from_v1,
)
from importers.simplefin.spending_adapter import (
    CAP_ASSIGNMENT_READ,
    CAP_ASSIGNMENT_WRITE,
    CAP_BACKUP,
    CAP_SETTINGS_READ,
)


def test_spending_snapshot_round_trips_uncategorized_refund():
    before = spending_report_snapshot(
        {
            "current": {
                "income": "0",
                "outflow": "90",
                "net": "-90",
                "count": 2,
            },
            "spendingBreakdown": [
                {
                    "taxonomyId": SPENDING_TAXONOMY,
                    "categoryId": "__uncategorized__",
                    "amount": "90",
                    "count": 2,
                },
                {
                    "taxonomyId": SPENDING_TAXONOMY,
                    "categoryId": "unused",
                    "amount": "0",
                    "count": 0,
                },
            ],
            "incomeBreakdown": [],
        },
        2,
    )
    expected = _expected_spending_snapshot(
        before,
        [{
            "taxonomyId": SPENDING_TAXONOMY,
            "categoryId": "shopping",
            "reportAmount": "-10",
        }],
    )
    after = spending_report_snapshot(
        {
            "current": {
                "income": "0",
                "outflow": "90",
                "net": "-90",
                "count": 2,
            },
            "spendingBreakdown": [
                {
                    "taxonomyId": SPENDING_TAXONOMY,
                    "categoryId": "__uncategorized__",
                    "amount": "100",
                    "count": 1,
                },
                {
                    "taxonomyId": SPENDING_TAXONOMY,
                    "categoryId": "shopping",
                    "amount": "-10",
                    "count": 1,
                },
            ],
            "incomeBreakdown": [],
        },
        1,
    )

    assert expected == after

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


def build(
    activities,
    history,
    *,
    decisions=None,
    assignments=None,
    canonical_review=None,
):
    eligible = [
        row
        for row in activities
        if row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
    ]
    spending = sum(Decimal(str(row.get("amount") or 0)) for row in eligible)
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
        current_report={
            "current": {
                "income": 0,
                "outflow": str(spending),
                "net": str(-spending),
                "count": len(eligible),
            },
            "spendingBreakdown": [],
            "incomeBreakdown": [],
        },
        current_uncategorized_count=len([
            row for row in activities
            if row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
        ]),
        generated_at=NOW,
        canonical_review=canonical_review,
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


def test_private_review_summarizes_transfer_memory_and_exact_splits_without_merchants(
    tmp_path,
):
    history = HistoryIndex()
    canonical_review = {
        "transferReview": {
            "windowDays": 5,
            "proposals": [{
                "candidateId": "transfer-candidate-synthetic",
                "evidenceHash": "a" * 64,
                "currency": "USD",
                "dayDistance": 2,
                "ambiguous": True,
                "status": "proposed",
                "merchant": "Synthetic text that must be discarded",
                "outflow": {"date": "2026-08-25"},
                "inflow": {"date": "2026-08-27"},
            }],
            "confirmed": [{
                "candidateId": "transfer-candidate-confirmed",
                "evidenceHash": "b" * 64,
                "decisionId": "confirmed-synthetic",
            }],
            "rejected": [{
                "candidateId": "transfer-candidate-rejected",
                "evidenceHash": "c" * 64,
                "decisionId": "rejected-synthetic",
            }],
            "staleDecisions": [],
        },
        "splitReview": {
            "groups": [{
                "groupId": "split-synthetic",
                "decisionId": "split-reviewed",
                "parentSourceId": "synthetic-parent",
                "date": "2026-08-26",
                "parentAmount": "-12.34",
                "children": [
                    {"sourceId": "synthetic-parent:split:1", "amount": "-5.00"},
                    {"sourceId": "synthetic-parent:split:2", "amount": "-7.34"},
                ],
            }]
        },
    }
    plan = build(
        [activity(description="Synthetic Merchant Must Stay Private")],
        history,
        canonical_review=canonical_review,
    )
    path = write_category_review(tmp_path, plan)
    report = path.read_text(encoding="utf-8")

    assert plan["metrics"]["transferCandidateCount"] == 1
    assert plan["metrics"]["confirmedTransferPairCount"] == 1
    assert plan["metrics"]["rejectedTransferPairCount"] == 1
    assert plan["metrics"]["splitGroupCount"] == 1
    assert "transfer-candidate-synthetic" in report
    assert "rejected/suppressed" in report
    assert "split-synthetic" in report
    assert "Synthetic Merchant Must Stay Private" not in report
    assert "Synthetic text that must be discarded" not in json.dumps(plan)


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


def test_existing_category_plan_without_milestone_five_summaries_still_validates(
    tmp_path,
):
    evidence = tmp_path / "history.csv"
    evidence.write_text("synthetic", encoding="utf-8")
    plan = build([activity()], HistoryIndex())
    plan["evidence"] = evidence_binding([evidence])
    plan.pop("transferReview")
    plan.pop("splitReview")
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })

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
        if path == "/utilities/database/backups":
            return getattr(self, "backups", [])
        activity_id = path.split("/")[3]
        return self.assignments[activity_id]

    def post(self, path, payload):
        eligible = [
            row
            for row in self.rows
            if row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
        ]
        if path == "/spending/cash-activities/search":
            return {
                "totalCount": sum(
                    not self.assignments[row["id"]] for row in eligible
                )
            }
        if path == "/spending/report":
            spending = sum(Decimal(str(row.get("amount") or 0)) for row in eligible)
            breakdown = {}
            for row in eligible:
                assignment = self.assignments[row["id"]]
                if not assignment:
                    continue
                key = (
                    assignment[0]["taxonomyId"],
                    assignment[0]["categoryId"],
                )
                amount, count = breakdown.get(key, (Decimal(), 0))
                breakdown[key] = (
                    amount + Decimal(str(row.get("amount") or 0)),
                    count + 1,
                )
            return {
                "current": {
                    "income": "0",
                    "outflow": str(spending),
                    "net": str(-spending),
                    "count": len(eligible),
                },
                "spendingBreakdown": [
                    {
                        "taxonomyId": taxonomy,
                        "categoryId": category,
                        "amount": str(amount),
                        "count": count,
                    }
                    for (taxonomy, category), (amount, count) in breakdown.items()
                ],
                "incomeBreakdown": [],
            }
        raise AssertionError(f"unexpected POST {path}")

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
        if not self.backup:
            return None
        filename = f"synthetic-backup-{len(getattr(self, 'backups', []))}.db"
        self.backups = [
            {
                "filename": filename,
                "sizeBytes": 128,
                "modifiedAt": "2026-08-28T00:00:00Z",
            },
            *getattr(self, "backups", []),
        ]
        return {"filename": filename}


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


def test_transfer_proposal_legs_never_become_category_candidates():
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")
    plan = build(
        [activity()],
        history,
        canonical_review={
            "transferReview": {
                "windowDays": 5,
                "proposals": [{
                    "candidateId": "synthetic-transfer-candidate",
                    "evidenceHash": "a" * 64,
                    "currency": "USD",
                    "dayDistance": 1,
                    "ambiguous": False,
                    "status": "proposed",
                    "outflow": {
                        "accountId": "canonical-account",
                        "sourceId": "source-1",
                        "date": "2026-08-26",
                        "amount": "-12.34",
                    },
                    "inflow": {
                        "accountId": "other-account",
                        "sourceId": "other-source",
                        "date": "2026-08-27",
                        "amount": "12.34",
                    },
                }],
                "confirmed": [],
                "rejected": [],
                "staleDecisions": [],
            },
            "splitReview": {"groups": []},
        },
    )

    assert plan["autoCandidates"] == []
    assert plan["manualItems"][0]["reason"] == "transfer-candidate-review"


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


class ProductionCategoryClient(StageClient):
    def get(self, path):
        if path == "/app/info":
            return {
                "version": "3.7.0-synthetic",
                "dbPath": "synthetic-production.db",
            }
        return super().get(path)


def promotion_bundle(tmp_path):
    client = ProductionCategoryClient([activity()])
    environment = instance_fingerprint(client, "http://127.0.0.1:8088")
    plan = automatic_plan(tmp_path)
    plan["environmentFingerprint"] = environment
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    plan_path = tmp_path / "category-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    staging = StageClient([staged_activity()])
    rehearsal = rehearse_category_plan(
        staging,
        plan,
        {"canonical-account": "staging-account"},
        HASH_KEY,
        generated_at=NOW,
    )
    rehearsal_path = write_rehearsal_receipt(tmp_path, rehearsal, plan_path)
    return client, plan, plan_path, rehearsal_path, environment


def run_promotion(
    client,
    plan,
    plan_path,
    rehearsal_path,
    environment,
    tmp_path,
):
    return promote_category_plan(
        client,
        plan,
        plan_path,
        rehearsal_path,
        tmp_path,
        HASH_KEY,
        environment_fingerprint=environment,
        supplied_plan_fingerprint=plan["planFingerprint"],
        supplied_environment_fingerprint=environment,
        allow_production=True,
        wait_seconds=0,
        generated_at=NOW,
    )


def test_production_category_promotion_applies_verifies_and_writes_immutable_receipt(
    tmp_path,
):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)

    receipt, receipt_path, already = run_promotion(
        client, plan, plan_path, rehearsal_path, environment, tmp_path
    )

    assert already is False
    assert client.assignments["activity-1"] == [{
        "taxonomyId": SPENDING_TAXONOMY,
        "categoryId": "groceries",
    }]
    assert receipt["beforeAssignments"][0]["assignment"] is None
    assert receipt["afterAssignments"][0]["assignment"]["categoryId"] == "groceries"
    assert receipt["beforeReport"] == plan["preWealthfolioWindow"]
    assert receipt["afterReport"] == plan["expectedWealthfolioWindow"]
    assert receipt["backup"]["filename"].startswith("synthetic-backup-")
    assert receipt["categoryPlanSha256"] == sha256_file(plan_path)
    assert receipt_path.is_file()
    assert not receipt_path.stat().st_mode & stat.S_IWRITE


def test_production_category_promotion_is_idempotent(tmp_path):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    first, first_path, first_already = run_promotion(
        client, plan, plan_path, rehearsal_path, environment, tmp_path
    )
    puts = client.puts
    backups = list(client.backups)

    second, second_path, second_already = run_promotion(
        client, plan, plan_path, rehearsal_path, environment, tmp_path
    )

    assert first_already is False
    assert second_already is True
    assert second == first
    assert second_path == first_path
    assert client.puts == puts
    assert client.backups == backups


def test_production_category_promotion_rolls_back_committed_write_failure(tmp_path):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    client.fail_on_put = 1
    client.commit_then_fail = True

    with pytest.raises(RuntimeError, match="synthetic failure"):
        run_promotion(
            client, plan, plan_path, rehearsal_path, environment, tmp_path
        )

    assert client.assignments["activity-1"] == []
    assert client.deletes == 1
    assert not list(
        (tmp_path / "normalized" / "simplefin").glob("category-promotion-*.json")
    )


@pytest.mark.parametrize("verification_fault", ["cash-flow", "category-total", "uncategorized"])
def test_production_category_promotion_rolls_back_report_verification_failure(
    tmp_path, verification_fault
):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    normal_post = client.post

    def wrong_report(path, payload):
        result = normal_post(path, payload)
        applied = bool(client.assignments["activity-1"])
        if path == "/spending/report" and applied and verification_fault == "cash-flow":
            result["current"]["outflow"] = "99.99"
        if (
            path == "/spending/report"
            and applied
            and verification_fault == "category-total"
        ):
            result["spendingBreakdown"][0]["amount"] = "99.99"
        if (
            path == "/spending/cash-activities/search"
            and applied
            and verification_fault == "uncategorized"
        ):
            result["totalCount"] = 7
        return result

    client.post = wrong_report
    with pytest.raises(DecisionError, match="category totals, cash flow"):
        run_promotion(
            client, plan, plan_path, rehearsal_path, environment, tmp_path
        )

    assert client.assignments["activity-1"] == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("categoryPlanSha256", "0" * 64, "exact successful"),
        ("categoryPlanFingerprint", "0" * 64, "exact successful"),
        ("postCategoryFingerprint", "0" * 64, "exact successful"),
    ],
)
def test_production_category_promotion_rejects_mismatched_rehearsal(
    tmp_path, field, value, message
):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    receipt = json.loads(rehearsal_path.read_text(encoding="utf-8"))
    receipt[field] = value
    receipt["receiptFingerprint"] = plan_fingerprint({
        key: item for key, item in receipt.items() if key != "receiptFingerprint"
    })
    rehearsal_path.chmod(0o644)
    rehearsal_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(DecisionError, match=message):
        run_promotion(
            client, plan, plan_path, rehearsal_path, environment, tmp_path
        )

    assert client.puts == 0
    assert not getattr(client, "backups", [])


def test_production_category_promotion_rejects_stale_evidence_and_environment(
    tmp_path,
):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    evidence = Path(plan["evidence"][0]["path"])
    evidence.write_text("changed synthetic evidence", encoding="utf-8")
    with pytest.raises(DecisionError, match="evidence hash changed"):
        run_promotion(
            client, plan, plan_path, rehearsal_path, environment, tmp_path
        )
    assert client.puts == 0

    evidence.write_text("synthetic", encoding="utf-8")
    with pytest.raises(DecisionError, match="environment fingerprint"):
        promote_category_plan(
            client,
            plan,
            plan_path,
            rehearsal_path,
            tmp_path,
            HASH_KEY,
            environment_fingerprint=environment,
            supplied_plan_fingerprint=plan["planFingerprint"],
            supplied_environment_fingerprint="wrong-environment",
            allow_production=True,
            wait_seconds=0,
        )


def test_production_category_promotion_requires_explicit_plan_fingerprint(tmp_path):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)

    with pytest.raises(DecisionError, match="operator-supplied"):
        promote_category_plan(
            client,
            plan,
            plan_path,
            rehearsal_path,
            tmp_path,
            HASH_KEY,
            environment_fingerprint=environment,
            supplied_plan_fingerprint="wrong-plan",
            supplied_environment_fingerprint=environment,
            allow_production=True,
            wait_seconds=0,
        )
    assert client.puts == 0


def test_production_category_promotion_requires_new_listed_backup(tmp_path):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    client.backups = [{
        "filename": "existing-synthetic.db",
        "sizeBytes": 128,
        "modifiedAt": "2026-08-28T00:00:00Z",
    }]

    def stale_backup():
        return {"filename": "existing-synthetic.db"}

    client.backup_database = stale_backup
    with pytest.raises(DecisionError, match="not demonstrably fresh"):
        run_promotion(
            client, plan, plan_path, rehearsal_path, environment, tmp_path
        )
    assert client.puts == 0


def test_production_category_promotion_rejects_concurrent_run(tmp_path):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    lock = (
        tmp_path
        / "normalized"
        / "simplefin"
        / f".category-promotion-{sha256_file(plan_path)}.lock"
    )
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("synthetic lock", encoding="utf-8")

    with pytest.raises(DecisionError, match="already in progress"):
        run_promotion(
            client, plan, plan_path, rehearsal_path, environment, tmp_path
        )
    assert client.puts == 0


def test_production_category_promotion_rejects_changed_pre_category_state(tmp_path):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    client.rows.append({
        **activity(2),
        "comment": "Synthetic Other",
        "idempotencyKey": "simplefin:live-account:source-unplanned",
    })
    client.assignments["activity-2"] = []

    with pytest.raises(
        DecisionError,
        match="pre-category fingerprint|Spending report changed",
    ):
        run_promotion(
            client, plan, plan_path, rehearsal_path, environment, tmp_path
        )
    assert client.puts == 0


def test_production_category_promotion_rejects_unresolved_transfer_decisions(
    tmp_path,
):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    plan["transferReview"]["staleDecisions"] = [{"candidateId": "synthetic-candidate"}]
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(DecisionError, match="unresolved blocking transfer"):
        promote_category_plan(
            client,
            plan,
            plan_path,
            rehearsal_path,
            tmp_path,
            HASH_KEY,
            environment_fingerprint=environment,
            supplied_plan_fingerprint=plan["planFingerprint"],
            supplied_environment_fingerprint=environment,
            allow_production=True,
            wait_seconds=0,
        )
    assert client.puts == 0


def test_production_category_promotion_rejects_invalid_reconciliation(tmp_path):
    client, plan, plan_path, rehearsal_path, environment = promotion_bundle(tmp_path)
    plan["balanceGapReconciliation"] = [{
        "activityId": "synthetic-gap",
        "date": "2026-08-26",
        "knownSpendingArtifact": False,
        "classification": "ordinary-spending",
        "categoryAction": "assign",
    }]
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(DecisionError, match="invalid reconciliation"):
        promote_category_plan(
            client,
            plan,
            plan_path,
            rehearsal_path,
            tmp_path,
            HASH_KEY,
            environment_fingerprint=environment,
            supplied_plan_fingerprint=plan["planFingerprint"],
            supplied_environment_fingerprint=environment,
            allow_production=True,
            wait_seconds=0,
        )
    assert client.puts == 0


# ---------------------------------------------------------------------------
# Milestone 6: capability-gated Spending adapter blocked behavior
# ---------------------------------------------------------------------------


class BlockedCapabilityStageClient(StageClient):
    """A StageClient variant where one specific call raises a genuine
    `WealthfolioError` (404), instead of the plain `RuntimeError` StageClient
    uses elsewhere to simulate an ordinary rollback failure. This exercises
    the "actionable blocked" path rather than the "unexpected error" path."""

    def __init__(
        self,
        rows,
        *,
        block_get=False,
        block_put_on=None,
        block_backup=False,
    ):
        super().__init__(rows)
        self.block_get = block_get
        self.block_put_on = block_put_on
        self.block_backup = block_backup

    def get(self, path):
        if self.block_get:
            raise WealthfolioError(404, path, "spending assignments unavailable")
        return super().get(path)

    def put(self, path, payload):
        self.puts += 1
        if self.puts == self.block_put_on:
            raise WealthfolioError(404, path, "spending assignment write unavailable")
        activity_id = path.split("/")[3]
        self.assignments[activity_id] = [payload]

    def backup_database(self):
        if self.block_backup:
            raise WealthfolioError(404, "/utilities/database/backup", "backup unavailable")
        return super().backup_database()


def test_rehearsal_returns_blocked_receipt_when_assignment_read_unsupported(tmp_path):
    plan = automatic_plan(tmp_path)
    client = BlockedCapabilityStageClient([staged_activity()], block_get=True)

    receipt = rehearse_category_plan(
        client,
        plan,
        {"canonical-account": "staging-account"},
        HASH_KEY,
        generated_at=NOW,
    )

    assert receipt["status"] == "blocked"
    assert receipt["productionMutated"] is False
    assert receipt["postCategoryFingerprint"] is None
    assert receipt["blockedCapability"]["capability"] == CAP_ASSIGNMENT_READ
    assert receipt["blockedCapability"]["status"] == "unsupported"
    assert client.puts == 0
    assert client.deletes == 0


def test_rehearsal_returns_blocked_receipt_when_backup_unsupported(tmp_path):
    plan = automatic_plan(tmp_path)
    client = BlockedCapabilityStageClient([staged_activity()], block_backup=True)

    receipt = rehearse_category_plan(
        client,
        plan,
        {"canonical-account": "staging-account"},
        HASH_KEY,
        generated_at=NOW,
    )

    assert receipt["status"] == "blocked"
    assert receipt["blockedCapability"]["capability"] == CAP_BACKUP
    assert client.puts == 0


def test_rehearsal_rolls_back_and_returns_blocked_receipt_when_assignment_write_unsupported(
    tmp_path,
):
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")
    plan = build([activity(1), activity(2)], history)
    evidence = tmp_path / "synthetic-evidence.csv"
    evidence.write_text("synthetic", encoding="utf-8")
    plan["evidence"] = evidence_binding([evidence])
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    client = BlockedCapabilityStageClient(
        [
            staged_activity("source-1", "staging-activity-1"),
            staged_activity("source-2", "staging-activity-2"),
        ],
        block_put_on=2,
    )

    receipt = rehearse_category_plan(
        client,
        plan,
        {"canonical-account": "staging-account"},
        HASH_KEY,
        generated_at=NOW,
    )

    assert receipt["status"] == "blocked"
    assert receipt["blockedCapability"]["capability"] == CAP_ASSIGNMENT_WRITE
    assert receipt["appliedCount"] == 0
    # The first activity's assignment was rolled back after the second failed.
    assert client.assignments["staging-activity-1"] == []
    assert client.deletes == 1


def test_cmd_plan_writes_blocked_plan_when_spending_settings_unsupported(tmp_path):
    """`cmd_plan` never crashes on an unsupported Spending endpoint; it
    persists an actionable blocked-plan artifact and returns exit code 3."""

    class BlockedPlanClient:
        def iter_activities(self):
            return iter([])

        def get(self, path):
            if path == "/app/info":
                return {"version": "0.0.0-synthetic", "dbPath": "synthetic.db"}
            if path == "/spending/settings":
                raise WealthfolioError(404, path, "spending module disabled")
            raise AssertionError(f"unexpected GET {path}")

        def post(self, path, payload):
            raise AssertionError(f"unexpected POST {path}")

        def list_accounts(self):
            raise AssertionError("list_accounts should not be reached in the blocked path")

    data_dir = tmp_path / "private-data"
    canonical_dir = data_dir / "normalized" / "canonical"
    canonical_dir.mkdir(parents=True)
    canonical = canonical_dir / "transactions.csv"
    canonical.write_text(
        "account_id,description,category,source_id,excluded,transfer_group\n",
        encoding="utf-8",
    )
    monarch_dir = data_dir / "legacy" / "monarch"
    monarch_dir.mkdir(parents=True)
    monarch = monarch_dir / "Transactions.csv"
    monarch.write_text("Id,Merchant,Original Statement\n", encoding="utf-8")
    reviewed_path = data_dir / "reviewed-plan.json"
    reviewed_path.write_text(
        json.dumps({"accounts": [], "snapshot": True}), encoding="utf-8"
    )

    args = SimpleNamespace(
        data_dir=data_dir,
        base_url="http://127.0.0.1:9999",
        reviewed_plan=reviewed_path,
        monarch_history=None,
        decisions=None,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
    )

    exit_code = cmd_plan(args, BlockedPlanClient())

    assert exit_code == 3
    blocked_paths = list(
        (data_dir / "normalized" / "simplefin").glob("category-plan-blocked-*.json")
    )
    assert len(blocked_paths) == 1
    blocked = json.loads(blocked_paths[0].read_text(encoding="utf-8"))
    assert blocked["mode"] == "category-plan-blocked"
    assert blocked["productionMutation"] is False
    assert blocked["blockedCapabilities"][0]["capability"] == CAP_SETTINGS_READ
    assert blocked["blockedCapabilities"][0]["status"] == "unsupported"
    assert blocked["reviewWindow"] == {
        "startDate": "2026-08-01",
        "endDate": "2026-08-31",
    }


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
        current_report={
            "current": {
                "income": "0",
                "outflow": "12.34",
                "net": "-12.34",
                "count": 1,
            },
            "spendingBreakdown": [],
            "incomeBreakdown": [],
        },
        current_uncategorized_count=1,
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


# ---------------------------------------------------------------------------
# Milestone 4: schema v2 staged private rule engine
# ---------------------------------------------------------------------------


def _rule(
    rule_id,
    stage,
    when,
    actions,
    *,
    priority=10,
    enabled=True,
    reviewed=True,
    stop_processing=True,
    rationale="synthetic rule rationale",
):
    return {
        "id": rule_id,
        "stage": stage,
        "priority": priority,
        "enabled": enabled,
        "reviewed": reviewed,
        "stopProcessing": stop_processing,
        "rationale": rationale,
        "when": when,
        "actions": actions,
    }


def v2_decisions(rules, category_aliases=None):
    engine = parse_rule_engine({
        "schemaVersion": 2,
        "categoryAliases": category_aliases or {},
        "rules": rules,
    })
    return {
        "schemaVersion": 2,
        "categoryAliases": category_aliases or {},
        "merchantOverrides": [],
        "activityOverrides": [],
        "ruleEngine": engine,
    }


def test_v1_decisions_still_load_without_a_rule_engine(tmp_path):
    path = tmp_path / "category-decisions.json"
    path.write_text(json.dumps({
        "schemaVersion": 1,
        "categoryAliases": {},
        "merchantOverrides": [],
        "activityOverrides": [],
    }), encoding="utf-8")

    decisions = load_category_decisions(path)

    assert decisions["schemaVersion"] == 1
    assert decisions["ruleEngine"] is None


def test_v2_categorize_rule_matching_reviewed_is_automatic():
    payee_hash = merchant_hash("Synthetic Market", HASH_KEY)
    decisions = v2_decisions([
        _rule(
            "coffee-groceries",
            "categorize",
            {"field": "payeeHash", "op": "exact", "value": payee_hash},
            [{"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}],
        ),
    ])

    plan = build([activity()], HistoryIndex(), decisions=decisions)

    assert plan["metrics"]["autoCount"] == 1
    assert plan["autoCandidates"][0]["categoryId"] == "groceries"
    assert plan["autoCandidates"][0]["confidence"] == "1.00"
    assert plan["autoCandidates"][0]["evidenceKind"] == "rule:coffee-groceries"
    assert "Synthetic Market" not in json.dumps(plan)


def test_v2_categorize_rule_unreviewed_is_manual_and_traceable():
    payee_hash = merchant_hash("Synthetic Market", HASH_KEY)
    decisions = v2_decisions([
        _rule(
            "unreviewed-rule",
            "categorize",
            {"field": "payeeHash", "op": "exact", "value": payee_hash},
            [{"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}],
            reviewed=False,
        ),
    ])

    plan = build([activity()], HistoryIndex(), decisions=decisions)

    assert plan["metrics"]["autoCount"] == 0
    assert plan["manualItems"][0]["reason"] == "unreviewed-rule-match"
    assert plan["manualItems"][0]["ruleId"] == "unreviewed-rule"
    assert plan["metrics"]["unreviewedRuleMatchCount"] == 1


def test_v2_classify_rule_excludes_without_losing_cash_flow_total():
    decisions = v2_decisions([
        _rule(
            "exclude-market",
            "classify",
            {"field": "payee", "op": "contains", "value": "synthetic market"},
            [{"type": "setTransactionKind", "value": "excluded"}],
        ),
    ])

    plan = build([activity()], HistoryIndex(), decisions=decisions)

    assert plan["metrics"]["autoCount"] == 0
    assert plan["manualItems"][0]["reason"] == "rule-excluded"
    assert plan["manualItems"][0]["ruleId"] == "exclude-market"
    assert plan["metrics"]["ruleExcludedCount"] == 1
    # Cash-flow totals are structural and must not be distorted by a rule.
    assert plan["cashFlowExcludingTransfersAndReconciliation"]["spending"] == "12.34"


def test_v2_decorate_actions_are_recorded_as_unsupported_and_never_applied():
    decisions = v2_decisions([
        _rule(
            "tag-market",
            "decorate",
            {"field": "payee", "op": "contains", "value": "synthetic market"},
            [{"type": "addTag", "value": "synthetic-tag"}],
            stop_processing=False,
        ),
    ])
    history = HistoryIndex()
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-1")
    history.add("canonical-account", "Synthetic Market", "Groceries", "history-2")

    plan = build([activity()], history, decisions=decisions)

    trace = plan["autoCandidates"][0]["ruleTrace"]
    assert trace["decorate"][0]["ruleId"] == "tag-market"
    assert trace["decorate"][0]["actions"][0] == {
        "type": "addTag",
        "applied": False,
        "reason": "unsupported-destination",
    }
    assert "synthetic-tag" not in json.dumps(plan["manualItems"])
    # Tag values are rule metadata, not merchant text; still redacted-safe.
    assert "Synthetic Market" not in json.dumps(plan)


def test_v2_normalize_rule_rewrites_payee_for_later_conditions_only():
    decisions = v2_decisions([
        _rule(
            "normalize-market",
            "normalize",
            {"field": "payee", "op": "contains", "value": "synthetic market"},
            [{"type": "setPayee", "value": "Clean Market"}],
        ),
        _rule(
            "categorize-on-normalized",
            "categorize",
            {"field": "payee", "op": "exact", "value": "Clean Market"},
            [{"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}],
        ),
    ])

    plan = build([activity()], HistoryIndex(), decisions=decisions)

    assert plan["metrics"]["autoCount"] == 1
    assert plan["autoCandidates"][0]["categoryId"] == "groceries"
    # merchantHash is always derived from the raw description, never from a
    # normalize-stage rewrite, so privacy/consistency with v1 is preserved.
    assert plan["autoCandidates"][0]["merchantHash"] == merchant_hash(
        "Synthetic Market", HASH_KEY
    )


def test_v2_rule_priority_and_stop_processing_are_deterministic():
    payee_hash = merchant_hash("Synthetic Market", HASH_KEY)
    decisions = v2_decisions([
        _rule(
            "second",
            "categorize",
            {"field": "payeeHash", "op": "exact", "value": payee_hash},
            [{"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "other"}],
            priority=20,
        ),
        _rule(
            "first",
            "categorize",
            {"field": "payeeHash", "op": "exact", "value": payee_hash},
            [{"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}],
            priority=5,
            stop_processing=True,
        ),
    ])

    plan = build([activity()], HistoryIndex(), decisions=decisions)

    assert plan["autoCandidates"][0]["categoryId"] == "groceries"
    assert plan["autoCandidates"][0]["evidenceKind"] == "rule:first"


def test_v2_rule_category_must_match_cash_flow_direction():
    payee_hash = merchant_hash("Synthetic Market", HASH_KEY)
    decisions = v2_decisions([
        _rule(
            "wrong-direction",
            "categorize",
            {"field": "payeeHash", "op": "exact", "value": payee_hash},
            [{"type": "setCategory", "taxonomyId": INCOME_TAXONOMY, "categoryId": "salary"}],
        ),
    ])

    with pytest.raises(DecisionError, match="cash-flow direction"):
        build([activity()], HistoryIndex(), decisions=decisions)


@pytest.mark.parametrize(
    "when,message",
    (
        (
            {"field": "payee", "op": "regex", "value": "("},
            "invalid regex",
        ),
        (
            {"field": "payee", "op": "sillyOp", "value": "x"},
            "op must be one of",
        ),
        (
            {"field": "unknownField", "op": "exact", "value": "x"},
            "unknown field",
        ),
        (
            {
                "all": [
                    {"any": [{"all": [{"field": "payee", "op": "exact", "value": "x"}]}]}
                ]
            },
            "nesting exceeds",
        ),
    ),
)
def test_v2_invalid_conditions_fail_closed(when, message):
    with pytest.raises(DecisionError, match=message):
        parse_rule_engine({
            "schemaVersion": 2,
            "categoryAliases": {},
            "rules": [_rule("bad", "categorize", when, [
                {"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"},
            ])],
        })


def test_v2_unknown_action_type_fails_closed():
    with pytest.raises(DecisionError, match="only supports"):
        parse_rule_engine({
            "schemaVersion": 2,
            "categoryAliases": {},
            "rules": [_rule(
                "bad",
                "categorize",
                {"field": "payee", "op": "exact", "value": "x"},
                [{"type": "addTag", "value": "nope"}],
            )],
        })


def test_v2_unknown_category_fails_closed_at_plan_time():
    decisions = v2_decisions([
        _rule(
            "unknown-category",
            "categorize",
            {"field": "payee", "op": "exact", "value": "synthetic market"},
            [{"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "does-not-exist"}],
        ),
    ])

    with pytest.raises(DecisionError, match="unknown category"):
        build([activity()], HistoryIndex(), decisions=decisions)


def test_v2_duplicate_rule_ids_fail_closed():
    rules = [
        _rule("same-id", "categorize", {"field": "payee", "op": "exact", "value": "a"}, [
            {"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"},
        ]),
        _rule("same-id", "categorize", {"field": "payee", "op": "exact", "value": "b"}, [
            {"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"},
        ]),
    ]
    with pytest.raises(DecisionError, match="duplicate rule id"):
        parse_rule_engine({"schemaVersion": 2, "categoryAliases": {}, "rules": rules})


def test_v2_reviewed_field_is_required_with_no_default():
    rule = _rule("no-reviewed", "categorize", {"field": "payee", "op": "exact", "value": "a"}, [
        {"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"},
    ])
    del rule["reviewed"]
    with pytest.raises(DecisionError, match="reviewed must be an explicit boolean"):
        parse_rule_engine({"schemaVersion": 2, "categoryAliases": {}, "rules": [rule]})


def test_v2_direction_condition_matches_the_rule_context():
    engine = parse_rule_engine({
        "schemaVersion": 2,
        "categoryAliases": {},
        "rules": [
            _rule(
                "debit-only",
                "categorize",
                {
                    "all": [
                        {"field": "direction", "op": "is", "value": "debit"},
                        {"field": "amount", "op": "range", "min": "1.00", "max": "20.00"},
                    ]
                },
                [{"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}],
            ),
        ],
    }).for_stage("categorize")
    assert len(engine) == 1


def test_v2_unsupported_schema_version_fails_closed(tmp_path):
    path = tmp_path / "category-decisions.json"
    path.write_text(json.dumps({"schemaVersion": 3, "rules": []}), encoding="utf-8")

    with pytest.raises(DecisionError, match="schemaVersion 1 or 2"):
        load_category_decisions(path)


def test_migrate_v1_overrides_to_v2_proposal_does_not_mutate_source():
    v1_decisions = {
        "schemaVersion": 1,
        "categoryAliases": {"Groceries": "groceries"},
        "merchantOverrides": [{
            "merchantHash": merchant_hash("Synthetic Market", HASH_KEY),
            "canonicalAccountId": "canonical-account",
            "taxonomyId": SPENDING_TAXONOMY,
            "categoryId": "groceries",
            "rationale": "Reviewed synthetic exception.",
        }],
        "activityOverrides": [{
            "sourceAccountId": "source-account",
            "sourceId": "source-1",
            "taxonomyId": INCOME_TAXONOMY,
            "categoryId": "salary",
            "rationale": "Reviewed synthetic income.",
        }],
    }
    source_copy = json.loads(json.dumps(v1_decisions))

    proposal = propose_v2_from_v1(v1_decisions)

    assert v1_decisions == source_copy
    assert proposal["schemaVersion"] == 2
    assert proposal["categoryAliases"] == {"Groceries": "groceries"}
    rule_ids = {rule["id"] for rule in proposal["rules"]}
    assert rule_ids == {"migrated-merchant-override-1", "migrated-activity-override-1"}
    assert all(rule["reviewed"] is True for rule in proposal["rules"])
    # Self-validates: parse_rule_engine(proposal) must not raise.
    parse_rule_engine(proposal)


def test_migrate_v1_proposal_reproduces_equivalent_plan_via_rule_engine():
    v1_decisions = {
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
    proposal = propose_v2_from_v1(v1_decisions)
    v2 = {
        "schemaVersion": 2,
        "categoryAliases": {},
        "merchantOverrides": [],
        "activityOverrides": [],
        "ruleEngine": parse_rule_engine(proposal),
    }

    v1_plan = build([activity()], HistoryIndex(), decisions=v1_decisions)
    v2_plan = build([activity()], HistoryIndex(), decisions=v2)

    assert v1_plan["autoCandidates"][0]["categoryId"] == v2_plan["autoCandidates"][0]["categoryId"]
    assert v1_plan["autoCandidates"][0]["confidence"] == v2_plan["autoCandidates"][0]["confidence"]


def test_migrate_cli_command_writes_proposal_without_touching_source(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "simplefin").mkdir(parents=True)
    decisions_path = data_dir / "simplefin" / "category-decisions.json"
    decisions_path.write_text(json.dumps({
        "schemaVersion": 1,
        "categoryAliases": {},
        "merchantOverrides": [{
            "merchantHash": merchant_hash("Synthetic Market", HASH_KEY),
            "taxonomyId": SPENDING_TAXONOMY,
            "categoryId": "groceries",
            "rationale": "Reviewed synthetic exception.",
        }],
        "activityOverrides": [],
    }), encoding="utf-8")
    before = decisions_path.read_text(encoding="utf-8")

    rc = main(["migrate-decisions", "--data-dir", str(data_dir)])

    assert rc == 0
    assert decisions_path.read_text(encoding="utf-8") == before
    proposal_path = data_dir / "simplefin" / "category-decisions.v2-proposal.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal["schemaVersion"] == 2
    assert proposal["rules"][0]["id"] == "migrated-merchant-override-1"


def test_migrate_cli_refuses_to_overwrite_existing_proposal_without_force(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "simplefin").mkdir(parents=True)
    decisions_path = data_dir / "simplefin" / "category-decisions.json"
    decisions_path.write_text(json.dumps({
        "schemaVersion": 1,
        "categoryAliases": {},
        "merchantOverrides": [],
        "activityOverrides": [],
    }), encoding="utf-8")
    proposal_path = data_dir / "simplefin" / "category-decisions.v2-proposal.json"
    proposal_path.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit):
        main(["migrate-decisions", "--data-dir", str(data_dir)])


def test_rule_context_matches_amount_range_and_pending_state_directly():
    engine = parse_rule_engine({
        "schemaVersion": 2,
        "categoryAliases": {},
        "rules": [
            _rule(
                "pending-large",
                "classify",
                {
                    "all": [
                        {"field": "pendingState", "op": "is", "value": "pending"},
                        {"field": "amount", "op": "range", "min": "100.00"},
                    ]
                },
                [{"type": "setTransactionKind", "value": "excluded"}],
            ),
        ],
    })
    posted_small = RuleContext(
        payee="anything",
        payee_hash="0" * 64,
        account_id="canonical-account",
        activity_identity={"sourceAccountId": "source-account", "sourceId": "source-1"},
        amount=Decimal("5.00"),
        direction="debit",
        cash_bucket="spending",
        date="2026-08-26",
        pending=False,
    )
    pending_large = RuleContext(
        payee="anything",
        payee_hash="0" * 64,
        account_id="canonical-account",
        activity_identity={"sourceAccountId": "source-account", "sourceId": "source-1"},
        amount=Decimal("150.00"),
        direction="debit",
        cash_bucket="spending",
        date="2026-08-26",
        pending=True,
    )
    from importers.simplefin.category_rules import run_normalize_and_classify

    run_normalize_and_classify(engine, posted_small)
    run_normalize_and_classify(engine, pending_large)

    assert posted_small.excluded is False
    assert pending_large.excluded is True
    assert pending_large.excluded_rule_id == "pending-large"


def _payee_hash_rule(value):
    return _rule(
        "hash-rule",
        "categorize",
        {"field": "payeeHash", "op": "exact", "value": value},
        [{"type": "setCategory", "taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}],
    )


def test_payee_hash_condition_requires_hexadecimal_not_just_length():
    """A 64-character non-hex value can never equal a sha256 digest."""
    engine = parse_rule_engine({
        "schemaVersion": 2,
        "categoryAliases": {},
        "rules": [_payee_hash_rule("ab" * 32)],
    })
    assert len(engine.rules) == 1

    for bad in ("z" * 64, "g" * 64, f"{'a' * 63}-", f"{'a' * 63} ", "a" * 63, "a" * 65):
        with pytest.raises(DecisionError, match="sha256 hex digest"):
            parse_rule_engine({
                "schemaVersion": 2,
                "categoryAliases": {},
                "rules": [_payee_hash_rule(bad)],
            })


def test_payee_hash_condition_accepts_uppercase_hex_and_folds_it():
    engine = parse_rule_engine({
        "schemaVersion": 2,
        "categoryAliases": {},
        "rules": [_payee_hash_rule("AB" * 32)],
    })
    assert engine.rules[0].condition.value == "ab" * 32


def test_v1_migration_refuses_a_merchant_override_without_a_sha256_hash():
    with pytest.raises(DecisionError, match="sha256 hex digest"):
        propose_v2_from_v1({
            "schemaVersion": 1,
            "categoryAliases": {},
            "merchantOverrides": [
                {"merchantHash": "not-a-digest", "categoryId": "groceries"}
            ],
            "activityOverrides": [],
        })


def _amount_rule(**bounds):
    return _rule(
        "amount-rule",
        "classify",
        {"field": "amount", "op": "range", **bounds},
        [{"type": "setTransactionKind", "value": "excluded"}],
    )


@pytest.mark.parametrize(
    "bounds",
    [
        {"min": "NaN"},
        {"max": "NaN"},
        {"min": "Infinity"},
        {"max": "-Infinity"},
        {"min": "0.00", "max": "Infinity"},
        {"min": "nan", "max": "10.00"},
    ],
)
def test_amount_range_rejects_non_finite_bounds(bounds):
    """Decimal() accepts NaN/Infinity; a rule with one silently never fires."""
    with pytest.raises(DecisionError, match="finite decimal"):
        parse_rule_engine({
            "schemaVersion": 2,
            "categoryAliases": {},
            "rules": [_amount_rule(**bounds)],
        })


def test_amount_range_still_accepts_ordinary_finite_bounds():
    engine = parse_rule_engine({
        "schemaVersion": 2,
        "categoryAliases": {},
        "rules": [_amount_rule(min="0.00", max="100.00")],
    })
    assert engine.rules[0].condition.value == (Decimal("0.00"), Decimal("100.00"))


def _date_rule(start, end):
    return _rule(
        "date-rule",
        "classify",
        {"field": "date", "op": "range", "start": start, "end": end},
        [{"type": "setTransactionKind", "value": "excluded"}],
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2024-99-99", "2025-99-99"),
        ("2024-01-32", "2024-02-01"),
        ("2024-02-30", "2024-03-01"),
        ("2024-13-01", "2024-14-01"),
        ("2024-01-01", "not-a-date"),
        ("01/01/2024", "01/31/2024"),
        ("2024-03-01", "2024-02-01"),
    ],
)
def test_date_range_rejects_impossible_or_misordered_endpoints(start, end):
    """Lexicographic comparison accepted well-ordered but impossible dates."""
    with pytest.raises(DecisionError, match="ISO dates"):
        parse_rule_engine({
            "schemaVersion": 2,
            "categoryAliases": {},
            "rules": [_date_rule(start, end)],
        })


def test_date_range_accepts_real_calendar_dates():
    engine = parse_rule_engine({
        "schemaVersion": 2,
        "categoryAliases": {},
        "rules": [_date_rule("2024-02-29", "2024-03-01")],
    })
    assert engine.rules[0].condition.value == ("2024-02-29", "2024-03-01")


def test_v2_example_decisions_file_parses_and_has_no_pii_markers():
    example_path = (
        Path(__file__).resolve().parents[1]
        / "importers"
        / "simplefin"
        / "category-decisions.example.json"
    )
    raw = example_path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    engine = parse_rule_engine(payload)

    assert payload["schemaVersion"] == 2
    assert len(engine.rules) >= 1
    # Only placeholder "example*" text and synthetic hashes belong in a
    # committed example; guard against accidental real account/merchant data.
    lowered = raw.lower()
    for marker in ("wealthfolio", "monarch", "simplefin.org"):
        assert marker not in lowered
