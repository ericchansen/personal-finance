from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from finance_store.identity import DEFAULT_POLICY
from importers.rebuild import receipt_repair
from importers.rebuild.decisions import DecisionError
from importers.rebuild.projector import REBUILD_MARKER
from importers.rebuild.safety import instance_fingerprint, plan_fingerprint
from importers.simplefin.application import (
    activity_semantic_fingerprint,
    ledger_fingerprint,
)
from tests.test_identity_source_authority import evidence, policy_with


def repair_identity_policy():
    return policy_with(
        evidence(
            family="ofx", strength="stable-provider-id", count=1,
            account="SYN-CANONICAL",
        ),
        evidence(
            family="simplefin", strength="posted-observation", count=1,
            account="SYN-CANONICAL",
        ),
    )


def repair_features(policy):
    intervals = {
        interval.evidence.source_family: interval.interval_id
        for interval in policy.source_authority.intervals
    }
    return {
        "authorityPolicyVersion": policy.authority_policy.version,
        "authorityPolicyHash": policy.authority_policy.policy_hash,
        "descriptionRelation": "exact",
        "authoritativeSourceFamily": "ofx",
        "suppressedSourceFamily": "simplefin",
        "authoritativeIntervalId": intervals["ofx"],
        "suppressedIntervalId": intervals["simplefin"],
        **{key: "true" for key in (
            "sameCanonicalAccount", "sameSourceDay", "sameSignedAmountAndCurrency",
            "distinctSourceScope", "intervalsOverlap",
        )},
    }


def assignment(
    activity_id: str,
    category_id: str,
    *,
    source: str,
    updated_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "id": f"SYN-ASSIGNMENT-{activity_id}-{category_id}",
        "activityId": activity_id,
        "taxonomyId": "SYN-TAXONOMY",
        "categoryId": category_id,
        "source": source,
        "weight": 10000,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": updated_at,
    }


def activity(
    activity_id: str,
    source_id: str,
    amount: float,
    activity_type: str,
) -> dict:
    return {
        "id": activity_id,
        "accountId": "SYN-ACCOUNT",
        "activityType": activity_type,
        "date": "2026-01-15T00:00:00+00:00",
        "amount": amount,
        "currency": "USD",
        "idempotencyKey": source_id,
        "sourceGroupId": None,
        "assetId": None,
        "subtype": None,
        "metadata": "{}",
    }


def test_assignment_upserts_preserve_manual_over_rule():
    result = receipt_repair._assignment_upserts(
        "SYN-SOURCE",
        [assignment("SYN-SOURCE", "SYN-MANUAL", source="manual")],
        "SYN-SURVIVOR",
        [assignment("SYN-SURVIVOR", "SYN-RULE", source="rule")],
    )

    assert len(result) == 1
    assert result[0]["activityId"] == "SYN-SURVIVOR"
    assert result[0]["categoryId"] == "SYN-MANUAL"
    assert result[0]["reason"] == "higher-precedence-manual-assignment"


def test_assignment_upserts_preserve_manual_provenance_for_same_category():
    result = receipt_repair._assignment_upserts(
        "SYN-SOURCE",
        [assignment("SYN-SOURCE", "SYN-CATEGORY", source="manual")],
        "SYN-SURVIVOR",
        [assignment("SYN-SURVIVOR", "SYN-CATEGORY", source="rule")],
    )

    assert len(result) == 1
    assert result[0]["expectedSource"] == "manual"
    assert result[0]["reason"] == "manual-assignment-provenance"


def test_assignment_upserts_refuse_source_only_rule_provenance():
    with pytest.raises(
        receipt_repair.ReceiptRepairError,
        match="cannot be recreated",
    ):
        receipt_repair._assignment_upserts(
            "SYN-SOURCE",
            [assignment("SYN-SOURCE", "SYN-CATEGORY", source="rule")],
            "SYN-SURVIVOR",
            [],
        )


def test_assignment_upserts_use_the_newer_manual_edit():
    result = receipt_repair._assignment_upserts(
        "SYN-SOURCE",
        [
            assignment(
                "SYN-SOURCE",
                "SYN-NEW",
                source="manual",
                updated_at="2026-01-02T00:00:00Z",
            )
        ],
        "SYN-SURVIVOR",
        [assignment("SYN-SURVIVOR", "SYN-OLD", source="manual")],
    )

    assert result[0]["categoryId"] == "SYN-NEW"
    assert result[0]["reason"] == "newer-manual-assignment"


def test_assignment_upserts_refuse_tied_manual_conflicts():
    with pytest.raises(
        receipt_repair.ReceiptRepairError,
        match="unique latest edit",
    ):
        receipt_repair._assignment_upserts(
            "SYN-SOURCE",
            [assignment("SYN-SOURCE", "SYN-A", source="manual")],
            "SYN-SURVIVOR",
            [assignment("SYN-SURVIVOR", "SYN-B", source="manual")],
        )


def test_assignment_upserts_compare_edit_instants_not_offset_text():
    result = receipt_repair._assignment_upserts(
        "SYN-SOURCE",
        [assignment(
            "SYN-SOURCE", "SYN-OLDER", source="manual",
            updated_at="2026-01-02T01:00:00+02:00",
        )],
        "SYN-SURVIVOR",
        [assignment(
            "SYN-SURVIVOR", "SYN-NEWER", source="manual",
            updated_at="2026-01-02T00:00:00Z",
        )],
    )
    assert result == []


@pytest.mark.parametrize("invalid_time", ["", "invalid", "2026-01-02"])
def test_assignment_upserts_reject_unknown_survivor_edit_time(invalid_time):
    survivor = assignment("SYN-SURVIVOR", "SYN-B", source="manual")
    survivor["createdAt"] = invalid_time
    survivor["updatedAt"] = invalid_time
    with pytest.raises(receipt_repair.ReceiptRepairError, match="edit time"):
        receipt_repair._assignment_upserts(
            "SYN-SOURCE",
            [assignment("SYN-SOURCE", "SYN-A", source="manual")],
            "SYN-SURVIVOR",
            [survivor],
        )


def test_assignment_edit_time_uses_pinned_api_naive_utc_contract():
    source = assignment(
        "SYN-SOURCE", "SYN-NEW", source="manual",
        updated_at="2026-01-02T01:00:00.123456789",
    )
    survivor = assignment(
        "SYN-SURVIVOR", "SYN-OLD", source="manual",
        updated_at="2026-01-02T01:00:00+02:00",
    )
    changes = receipt_repair._assignment_upserts(
        "SYN-SOURCE", [source], "SYN-SURVIVOR", [survivor]
    )
    assert changes[0]["categoryId"] == "SYN-NEW"
    assert changes[0]["reason"] == "newer-manual-assignment"


class FakeClient:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = deepcopy(rows)
        self.base = "http://127.0.0.1:18091"
        self.account_type = "CASH"
        self.assignments: dict[str, list[dict]] = {}
        self.splits: dict[str, list[dict]] = {}
        self.spending: list[dict] = []
        self.backup_calls = 0
        self.recalculate_calls = 0

    def get(self, path: str):
        if path == "/app/info":
            return {"version": "3.7.0", "dbPath": "/data/wealthfolio-rebuild.db"}
        if path == "/accounts?includeArchived=true":
            return [{"id": "SYN-ACCOUNT", "accountType": self.account_type,
                     "currency": "USD"}]
        if path == "/spending/cash-activities":
            return deepcopy(self.spending)
        if path.startswith("/spending/activities/") and path.endswith("/splits"):
            return deepcopy(self.splits.get(path.split("/")[3], []))
        if path.startswith("/spending/activities/") and path.endswith("/assignments"):
            activity_id = path.split("/")[3]
            return deepcopy(self.assignments.get(activity_id, []))
        raise AssertionError(path)

    def post(self, path: str, payload: dict):
        if path == "/performance/accounts/simple":
            return [{"accountId": "SYN-ACCOUNT", "totalValue": 80}]
        if path == "/portfolio/recalculate":
            self.recalculate_calls += 1
            return {}
        raise AssertionError(path)

    def put(self, path: str, payload: dict):
        if path.startswith("/spending/activities/") and path.endswith("/assignments"):
            activity_id = path.split("/")[3]
            self.assignments[activity_id] = [{
                "id": "SYN-ASSIGNMENT",
                "activityId": activity_id,
                "taxonomyId": payload["taxonomyId"],
                "categoryId": payload["categoryId"],
                "source": "manual",
                "weight": 10000,
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-02T00:00:00Z",
            }]
            return {}
        raise AssertionError(path)

    def iter_activities(self, page_size: int = 500):
        del page_size
        yield from deepcopy(self.rows)

    def save_activities(self, *, updates=None, delete_ids=None):
        delete = set(delete_ids or [])
        by_id = {row["id"]: row for row in self.rows if row["id"] not in delete}
        for update in updates or []:
            current = by_id[update["id"]]
            replacement = {**current, **update}
            if "activityDate" in replacement:
                replacement["date"] = replacement.pop("activityDate")
            by_id[update["id"]] = replacement
        self.rows = list(by_id.values())
        return {"created": [], "updated": updates or [], "deleted": list(delete), "errors": []}


def repair_plan(rows: list[dict]) -> dict:
    source, survivor, reconciliation = rows
    policy = repair_identity_policy()
    reconciliation_update = {
        **reconciliation,
        "activityDate": reconciliation["date"],
        "amount": 90.0,
    }
    reconciliation_update.pop("date")
    expected = [survivor, reconciliation_update]
    document = {
        "schemaVersion": receipt_repair.SCHEMA_VERSION,
        "kind": receipt_repair.KIND,
        "generatedAt": "2026-01-20T00:00:00+00:00",
        "selectionHash": "a" * 64,
        "scope": {
            "schemaVersion": 1,
            "sourceApplicationPlanSha256": "1" * 64,
            "ledgerAccountId": "SYN-ACCOUNT",
            "effectiveFrom": "2026-01-01",
            "effectiveThrough": "2026-01-31",
            "expectedSourceRows": 1,
            "expectedSuppressions": 1,
        },
        "evidence": {
            "baselinePublicationId": "2" * 64,
            "forensicPublicationId": "3" * 64,
            "canonicalManifestSha256": "4" * 64,
            "canonicalGenerationHash": "5" * 64,
            "canonicalStateHash": "6" * 64,
            "identityPolicyHash": policy.policy_hash,
            "identityPolicyDocument": policy.document(),
            "sourceAuthorityHash": policy.source_authority.authority_hash,
            "sourceApplicationPlanSha256": "1" * 64,
            "sourceApplicationReceiptSha256": "9" * 64,
        },
        "counts": {
            "selectedSourceRows": 1,
            "sourceSuppressedRows": 1,
            "preservedSourceRows": 0,
            "assignmentUpserts": 0,
            "reconciliationUpdates": 1,
        },
        "preconditions": {
            "activityCount": 3,
            "dependentAssignments": {source["id"]: [], survivor["id"]: []},
            "accountLedgerFingerprint": ledger_fingerprint(rows, {"SYN-ACCOUNT"}),
            "globalLedgerFingerprint": ledger_fingerprint(rows, {"SYN-ACCOUNT"}),
        },
        "operations": {
            "repairs": [{
                "canonicalTransactionId": "SYN-CANONICAL",
                "decisionId": "SYN-DECISION",
                "decisionHash": "a" * 64,
                "sourceObservationId": "SYN-SOURCE-OBSERVATION",
                "sourceActivityId": source["id"],
                "sourceActivityFingerprint": activity_semantic_fingerprint(source),
                "sourceRollbackPayload": source,
                "survivorObservationId": "SYN-SURVIVOR-OBSERVATION",
                "survivorActivityId": survivor["id"],
                "survivorActivityFingerprint": activity_semantic_fingerprint(survivor),
                "sourceHashes": ["b" * 64],
                "authorityPolicyHash": policy.authority_policy.policy_hash,
                "featureVector": repair_features(policy),
                "competingCandidateProof": {},
            }],
            "assignmentUpserts": [],
            "reconciliationUpdate": {
                "activityId": reconciliation["id"],
                "beforeFingerprint": activity_semantic_fingerprint(reconciliation),
                "payload": reconciliation_update,
            },
        },
        "expected": {
            "activityCount": 2,
            "accountLedgerFingerprint": ledger_fingerprint(
                expected, {"SYN-ACCOUNT"}
            ),
            "globalLedgerFingerprint": ledger_fingerprint(
                expected, {"SYN-ACCOUNT"}
            ),
            "accountEffectConserved": True,
            "selectedDuplicatePairsRemaining": 0,
        },
    }
    document["planHash"] = plan_fingerprint(document)
    return document


def test_clone_apply_is_exact_and_replay_is_noop(tmp_path: Path, monkeypatch):
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    client = FakeClient(rows)
    plan = repair_plan(rows)
    base_url = "http://127.0.0.1:18091"
    target_id = instance_fingerprint(client, base_url)
    monkeypatch.setattr(
        receipt_repair,
        "require_unique_backup",
        lambda _client: {"filename": "wealthfolio_backup_20260101_000000.db"},
    )
    monkeypatch.setattr(
        receipt_repair,
        "download_backup",
        lambda *_args, **_kwargs: {
            "path": "backups/synthetic.db",
            "size": 100,
            "sha256": "b" * 64,
            "schemaMigration": "synthetic",
        },
    )
    monkeypatch.setattr(
        receipt_repair,
        "verify_backup_file",
        lambda *_args, **_kwargs: {
            "path": "backups/synthetic.db",
            "size": 100,
            "sha256": "b" * 64,
            "schemaMigration": "synthetic",
        },
    )
    recoveries = []
    settled = []
    monkeypatch.setattr(
        receipt_repair,
        "wait_for_recalculation",
        lambda _client, account_ids: settled.append(account_ids),
    )

    receipt = receipt_repair.apply_plan(
        client,
        plan,
        base_url=base_url,
        expected_instance_id=target_id,
        supplied_plan_hash=plan["planHash"],
        marker=REBUILD_MARKER,
        backup_download=tmp_path / "backup.db",
        data_dir=tmp_path,
        record_recovery=recoveries.append,
    )
    verified = receipt_repair.verify_target(
        client,
        plan,
        receipt,
        base_url=base_url,
        expected_instance_id=target_id,
        supplied_plan_hash=plan["planHash"],
        data_dir=tmp_path,
        recovery=recoveries[0],
        marker=REBUILD_MARKER,
    )
    replay = receipt_repair.verify_target(
        client,
        plan,
        receipt,
        base_url=base_url,
        expected_instance_id=target_id,
        supplied_plan_hash=plan["planHash"],
        data_dir=tmp_path,
        recovery=recoveries[0],
        marker=REBUILD_MARKER,
    )

    assert receipt["status"] == "applied"
    assert verified["selectedDuplicatePairsRemaining"] == 0
    assert replay["globalLedgerFingerprint"] == verified[
        "globalLedgerFingerprint"
    ]
    assert {row["id"] for row in client.rows} == {"SYN-SURVIVOR", "SYN-RECON"}
    assert client.recalculate_calls == 0
    assert settled == [["SYN-ACCOUNT"]]


@pytest.mark.parametrize("activity_id", ["SYN-SOURCE", "SYN-SURVIVOR"])
@pytest.mark.parametrize("change", ["assignment", "split", "event"])
def test_ready_state_rejects_new_dependent_state(activity_id, change):
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    client = FakeClient(rows)
    if change == "assignment":
        client.assignments[activity_id] = [
            assignment(activity_id, "SYN-NEW-CATEGORY", source="manual")
        ]
    elif change == "split":
        client.splits[activity_id] = [{"id": "SYN-NEW-SPLIT"}]
    else:
        client.spending = [{"id": activity_id, "eventId": "SYN-NEW-EVENT"}]
    with pytest.raises(receipt_repair.ReceiptRepairError, match="changed"):
        receipt_repair._target_status(
            client, receipt_repair.SpendingAdapter(client), repair_plan(rows)
        )


def test_post_state_rejects_loss_of_untouched_survivor_assignment():
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    plan = repair_plan(rows)
    plan["preconditions"]["dependentAssignments"]["SYN-SURVIVOR"] = [
        assignment("SYN-SURVIVOR", "SYN-CATEGORY", source="manual")
    ]
    client = FakeClient(rows)
    client.save_activities(
        updates=[plan["operations"]["reconciliationUpdate"]["payload"]],
        delete_ids=["SYN-SOURCE"],
    )
    with pytest.raises(
        receipt_repair.ReceiptRepairError, match="dependent assignments changed"
    ):
        receipt_repair._target_status(
            client, receipt_repair.SpendingAdapter(client), plan
        )


def test_apply_rechecks_user_edits_after_backup_before_mutation(tmp_path, monkeypatch):
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    client = FakeClient(rows)
    plan = repair_plan(rows)

    def backup_with_concurrent_edit(_client):
        client.assignments["SYN-SOURCE"] = [
            assignment("SYN-SOURCE", "SYN-NEW-CATEGORY", source="manual")
        ]
        return {"filename": "wealthfolio_backup_20260101_000000.db"}

    monkeypatch.setattr(receipt_repair, "require_unique_backup", backup_with_concurrent_edit)
    monkeypatch.setattr(
        receipt_repair, "download_backup",
        lambda *_args, **_kwargs: {
            "path": "backups/synthetic.db", "size": 100,
            "sha256": "b" * 64, "schemaMigration": "synthetic",
        },
    )
    recoveries = []
    with pytest.raises(
        receipt_repair.ReceiptRepairError, match="dependent assignments changed"
    ):
        receipt_repair.apply_plan(
            client, plan,
            base_url=client.base,
            expected_instance_id=instance_fingerprint(client, client.base),
            supplied_plan_hash=plan["planHash"],
            marker=REBUILD_MARKER,
            backup_download=tmp_path / "backup.db",
            data_dir=tmp_path,
            record_recovery=recoveries.append,
        )
    assert client.rows == rows
    assert client.recalculate_calls == 0
    assert len(recoveries) == 1


def test_plan_fingerprint_rejects_tampering():
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    plan = repair_plan(rows)
    plan["counts"]["sourceSuppressedRows"] = 2

    with pytest.raises(
        receipt_repair.ReceiptRepairError,
        match="fingerprint",
    ):
        receipt_repair.validate_plan(plan)


def test_historical_plan_remains_readable_but_cannot_mutate(tmp_path):
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    plan = repair_plan(rows)
    plan["schemaVersion"] = 1
    plan["evidence"].pop("identityPolicyDocument")
    plan["planHash"] = plan_fingerprint({
        k: v for k, v in plan.items() if k != "planHash"
    })
    receipt_repair.validate_plan(plan)
    client = FakeClient(rows)
    with pytest.raises(
        receipt_repair.ReceiptRepairError, match="verification-only"
    ):
        receipt_repair.apply_plan(
            client, plan, base_url=client.base,
            expected_instance_id="unused",
            supplied_plan_hash=plan["planHash"], marker=REBUILD_MARKER,
            backup_download=tmp_path / "backup.db", data_dir=tmp_path,
            record_recovery=lambda _: pytest.fail("must not prepare mutation"),
        )
    assert client.rows == rows


@pytest.mark.parametrize(
    "field,value",
    [
        ("descriptionRelation", "token-boundary-prefix"),
        ("authorityPolicyVersion", "canonical-source-authority-v2"),
        ("authorityPolicyHash", "0" * 64),
        ("sameSourceDay", "false"),
        ("authoritativeIntervalId", "0" * 64),
    ],
)
def test_current_policy_wrapper_cannot_authorize_an_incompatible_operation(field, value):
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    plan = repair_plan(rows)
    plan["operations"]["repairs"][0]["featureVector"][field] = value
    plan["planHash"] = plan_fingerprint({
        key: item for key, item in plan.items() if key != "planHash"
    })
    with pytest.raises(receipt_repair.ReceiptRepairError, match="contradicts"):
        receipt_repair.validate_plan(plan)


def test_credit_card_value_uses_strict_cash_ledger_not_investment_performance():
    client = FakeClient([
        activity("SYN-SPEND", "extract:SYN:1", 10, "WITHDRAWAL"),
        activity("SYN-PAYMENT", "extract:SYN:2", 25, "CREDIT"),
    ])
    client.account_type = "CREDIT_CARD"
    client.post = lambda *_args: pytest.fail("credit cards are not investment accounts")
    assert receipt_repair._account_value(client, "SYN-ACCOUNT") == (
        Decimal("15"), "cash-ledger"
    )
    client.rows[0]["currency"] = "EUR"
    with pytest.raises(receipt_repair.ReceiptRepairError, match="cash ledger"):
        receipt_repair._account_value(client, "SYN-ACCOUNT")


def test_apply_rejects_client_origin_and_unconfirmed_plan_hash(tmp_path: Path):
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    plan = repair_plan(rows)
    client = FakeClient(rows)
    target_id = instance_fingerprint(client, client.base)

    with pytest.raises(
        receipt_repair.ReceiptRepairError,
        match="origin differs",
    ):
        receipt_repair.apply_plan(
            client,
            plan,
            base_url="http://127.0.0.1:18092",
            expected_instance_id=target_id,
            supplied_plan_hash=plan["planHash"],
            marker=REBUILD_MARKER,
            backup_download=tmp_path / "backup.db",
            data_dir=tmp_path,
            record_recovery=lambda _value: None,
        )

    with pytest.raises(DecisionError, match="fingerprint"):
        receipt_repair.apply_plan(
            client,
            plan,
            base_url=client.base,
            expected_instance_id=target_id,
            supplied_plan_hash="0" * 64,
            marker=REBUILD_MARKER,
            backup_download=tmp_path / "backup.db",
            data_dir=tmp_path,
            record_recovery=lambda _value: None,
        )


def test_verify_rejects_unsealed_receipt(tmp_path: Path):
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    plan = repair_plan(rows)
    client = FakeClient(rows)
    target_id = instance_fingerprint(client, client.base)
    fabricated = {
        "schemaVersion": 1,
        "kind": f"{receipt_repair.KIND}-receipt",
        "status": "applied",
        "planHash": plan["planHash"],
        "instanceId": target_id,
    }

    with pytest.raises(
        receipt_repair.ReceiptRepairError,
        match="receipt binding",
    ):
        receipt_repair.verify_target(
            client,
            plan,
            fabricated,
            base_url=client.base,
            expected_instance_id=target_id,
            supplied_plan_hash=plan["planHash"],
            data_dir=tmp_path,
            recovery={},
            marker=REBUILD_MARKER,
        )


def test_repair_policy_rejects_legacy_v3_evidence():
    unsafe = {
        "policyVersion": "canonical-identity-v3",
        "policyDocument": {
            "rules": {
                "uniqueCrossSource": {
                    "automatic": True,
                    "requiresSourceAuthorityOrScopedProviderIdentity": False,
                }
            },
            "sourceAuthority": {
                "policy": {"descriptionParticipates": "tie-evidence"}
            },
        },
        "sourceAuthority": {
            "policyVersion": "canonical-source-authority-v1"
        },
    }

    with pytest.raises(
        receipt_repair.ReceiptRepairError,
        match="requires canonical identity v5",
    ):
        receipt_repair._validate_repair_policy(unsafe)


def test_repair_policy_rejects_v4_shared_token_suppression():
    unsafe = {
        "policyVersion": "canonical-identity-v4",
        "policyDocument": {"sourceAuthority": {"policy": {
            "descriptionParticipates": "exact-or-shared-discriminating-token",
        }}},
        "sourceAuthority": {"policyVersion": "canonical-source-authority-v2"},
    }
    with pytest.raises(
        receipt_repair.ReceiptRepairError, match="requires canonical identity v5"
    ):
        receipt_repair._validate_repair_policy(unsafe)


def test_current_version_cannot_weaken_component_safety_rules():
    document = DEFAULT_POLICY.document()
    document["rules"]["automaticComponents"]["preserveDistinctIsTransitive"] = False
    with pytest.raises(receipt_repair.ReceiptRepairError, match="semantics are unsafe"):
        receipt_repair._validate_repair_policy({
            "policyVersion": DEFAULT_POLICY.version,
            "policyDocument": document,
            "sourceAuthority": {
                "policyVersion": DEFAULT_POLICY.authority_policy.version,
            },
        })


def test_v5_repair_currency_uses_the_verified_account_currency():
    assert receipt_repair._repair_currency({}, "USD") == "USD"
    assert receipt_repair._repair_currency({"currency": "EUR"}, "USD") == "EUR"


@pytest.mark.parametrize("currency", ["", "EUR", "CAD"])
def test_v5_repair_currency_refuses_unproved_identity_currency(currency):
    with pytest.raises(receipt_repair.ReceiptRepairError, match="currency"):
        receipt_repair._repair_currency({}, currency)


def test_build_plan_consumes_v5_account_currency_and_binds_all_assignments(
    tmp_path, monkeypatch
):
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    source, survivor, reconciliation = rows
    selection = repair_plan(rows)["scope"]
    create = {**source, "activityDate": source["date"]}
    create.pop("date")
    application = {
        "sha256": selection["sourceApplicationPlanSha256"],
        "value": {
            "assertions": [{"accountId": "SYN-ACCOUNT", "canonicalAccountId": "SYN-CANONICAL"}],
            "operations": {"creates": [create]},
            "reconciliations": [{
                "accountId": "SYN-ACCOUNT", "activityId": reconciliation["id"],
                "action": "update", "after": reconciliation,
            }],
        },
    }
    evidence = {
        "baselinePointer": {"publicationId": "2" * 64},
        "forensicPointer": {"publicationId": "3" * 64},
        "baselinePublication": tmp_path,
        "baselineManifest": {},
        "plans": [application],
        "receipts": [],
        "forensicDetail": {"activities": [
            {
                "activityId": row["id"], "sourceIdentity": row["idempotencyKey"],
                "raw": row,
                "lineage": {"status": "receipt-proven",
                            "applicationPlanSha256": application["sha256"]},
                "dependentState": {"reasonCodes": [], "categoryAssignments": []},
            }
            for row in rows
        ]},
    }
    policy = repair_identity_policy()
    policy_document = policy.document()
    authority = policy_document["sourceAuthority"]
    identity = {
        "policyVersion": DEFAULT_POLICY.version,
        "policyHash": policy.policy_hash,
        "policyDocument": policy_document,
        "generationHash": "5" * 64,
        "canonicalStateHash": "6" * 64,
        "sourceAuthority": {
            "policyVersion": authority["policy"]["version"],
            "policyHash": authority["policyHash"],
            "authorityHash": policy.source_authority.authority_hash,
            "intervals": [
                {"intervalId": interval, "proven": True, "reconciled": True,
                 "authorityProof": {"sourceArtifactBound": "true"}}
                for interval in (
                    value.interval_id for value in policy.source_authority.intervals
                )
            ],
        },
    }
    observations = {"observations": [
        {
            "observationId": "SYN-SOURCE-OBS", "disposition": "suppressed",
            "decisionType": "automatic-identity", "decisionId": "SYN-DECISION",
            "canonicalTransactionId": "SYN-EVENT",
            "transaction": {"account_id": "SYN-CANONICAL",
                            "source_id": source["idempotencyKey"]},
        },
        {
            "observationId": "SYN-SURVIVOR-OBS",
            "transaction": {"account_id": "SYN-CANONICAL",
                            "source_id": survivor["idempotencyKey"],
                            "date": "2026-01-15", "amount": "-10.00"},
        },
    ]}
    lineage = {
        "baselinePublicationId": "2" * 64,
        "forensicPublicationId": "3" * 64,
        "identityPolicy": identity,
        "canonicalTransactions": [
            {"canonicalTransactionId": "SYN-EVENT", "activeObservationId": "SYN-SURVIVOR-OBS"}
        ],
        "decisionProjections": [{
            "decisionId": "SYN-DECISION", "decisionHash": "a" * 64,
            "policyVersion": DEFAULT_POLICY.version, "outcome": "source-suppressed",
            "rationaleCode": receipt_repair.AUTHORITY_RATIONALE,
            "confidenceTier": receipt_repair.AUTHORITY_CONFIDENCE,
            "featureVector": repair_features(policy),
            "competingCandidateProof": {}, "sourceHashes": ["b" * 64],
            "sourceAuthorityPolicyHash": authority["policyHash"],
        }],
    }
    monkeypatch.setattr(receipt_repair, "_current_evidence", lambda _root: evidence)
    monkeypatch.setattr(
        receipt_repair, "_canonical_documents",
        lambda _root: (
            observations, lineage,
            [{"account_id": "SYN-CANONICAL", "currency": "USD"}], "4" * 64,
        ),
    )
    monkeypatch.setattr(
        receipt_repair.forensic, "_receipt_for_plan",
        lambda *_args: ("proved", {"sha256": "9" * 64}),
    )
    monkeypatch.setattr(
        receipt_repair.forensic, "_domain",
        lambda *_args: [
            {"id": "SYN-ACCOUNT", "accountType": "CREDIT_CARD", "currency": "USD"}
        ],
    )
    plan = receipt_repair.build_plan(tmp_path, selection)
    receipt_repair.validate_plan(plan)
    assert plan["counts"]["sourceSuppressedRows"] == 1
    assert plan["preconditions"]["dependentAssignments"] == {
        source["id"]: [], survivor["id"]: [],
    }
    client = FakeClient(rows)
    assert receipt_repair._target_status(
        client, receipt_repair.SpendingAdapter(client), plan
    )[0] == "ready"
    selected_plan = receipt_repair.build_plan(
        tmp_path, {**selection, "sourceActivityIds": ["SYN-SOURCE"]}
    )
    assert selected_plan["operations"] == plan["operations"]
    with pytest.raises(receipt_repair.ReceiptRepairError, match="suppression count changed"):
        receipt_repair.build_plan(
            tmp_path, {**selection, "sourceActivityIds": ["SYN-UNKNOWN"]}
        )
