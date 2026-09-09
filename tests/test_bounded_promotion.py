from __future__ import annotations

import json
import csv
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from copy import deepcopy
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from importers.rebuild import bounded_promotion as bp
from importers.rebuild import bounded_promotion_cli as cli
from importers.rebuild import receipt_repair
from importers.rebuild.safety import instance_fingerprint, plan_fingerprint
from tests.test_receipt_repair import FakeClient, activity, repair_plan


EVIDENCE_KEY = b"synthetic-evidence-key-not-a-secret" * 2
OPERATOR_KEY = b"synthetic-operator-key-not-a-secret" * 2
REVISION = "a" * 40
ASSIGNMENT_TIME_0 = "2026-01-20T00:00:00.123456789Z"
ASSIGNMENT_TIME_1 = "2026-01-20T01:00:00.123456789Z"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Bounded runtime uses Windows mandatory fencing")


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def sql(path: Path, statement: str, parameters=()) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(statement, parameters)


def make_database(path: Path, rows: list[dict]) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("""
        CREATE TABLE __diesel_schema_migrations (version TEXT PRIMARY KEY, run_on TEXT);
        INSERT INTO __diesel_schema_migrations VALUES ('synthetic-v1','2026-01-01');
        CREATE TABLE accounts (id TEXT PRIMARY KEY, account_type TEXT, currency TEXT);
        INSERT INTO accounts VALUES ('SYN-ACCOUNT','CASH','USD');
        CREATE TABLE activities (
          id TEXT PRIMARY KEY, account_id TEXT, activity_type TEXT, activity_date TEXT,
          amount TEXT, currency TEXT, idempotency_key TEXT, source_group_id TEXT,
          asset_id TEXT, subtype TEXT, metadata TEXT, notes TEXT, updated_at TEXT,
          is_user_modified INTEGER);
        CREATE TABLE activity_taxonomy_assignments (
          id TEXT PRIMARY KEY, activity_id TEXT, taxonomy_id TEXT, category_id TEXT,
          weight INTEGER, source TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE assets (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE app_settings (id TEXT PRIMARY KEY, value TEXT);
        INSERT INTO app_settings VALUES ('SYN-THEME','dark');
        CREATE TABLE unknown_user_work (id TEXT PRIMARY KEY, unknown_field TEXT, raw BLOB);
        INSERT INTO unknown_user_work VALUES ('SYN-USER','Keep every byte',X'0001FF');
        CREATE TABLE activity_splits (id TEXT PRIMARY KEY, activity_id TEXT, amount TEXT);
        CREATE TABLE quotes (
          id TEXT PRIMARY KEY, asset_id TEXT, day TEXT, source TEXT,
          open TEXT, high TEXT, low TEXT, close TEXT, adjclose TEXT, volume TEXT,
          currency TEXT, notes TEXT, created_at TEXT, timestamp TEXT);
        INSERT INTO quotes VALUES ('SYN-MANUAL','SYN-ASSET','2026-01-20','MANUAL',
          NULL,NULL,NULL,'3.50','3.50','0','USD','User evidence','2026-01-20T00:00:00Z','2026-01-20');
        INSERT INTO quotes VALUES ('SYN-HISTORICAL','SYN-ASSET','2026-01-10','YAHOO',
          NULL,NULL,NULL,'2.50','2.50','0','USD',NULL,'2026-01-20T00:00:00Z','2026-01-10');
        INSERT INTO quotes VALUES ('SYN-LATEST','SYN-ASSET','2026-01-20','YAHOO',
          NULL,NULL,NULL,'10','10','10','USD',NULL,'2026-01-20T00:00:00Z','2026-01-20');
        CREATE TABLE quote_sync_state (
          asset_id TEXT PRIMARY KEY, position_closed_date TEXT, last_synced_at TEXT,
          data_source TEXT, sync_priority INTEGER, error_count INTEGER, last_error TEXT,
          profile_enriched_at TEXT, created_at TEXT, updated_at TEXT);
        INSERT INTO quote_sync_state VALUES ('SYN-ASSET',NULL,'2026-01-20T00:00:00Z',
          'YAHOO',1,0,NULL,NULL,'2026-01-20T00:00:00Z','2026-01-20T00:00:00Z');
        CREATE TABLE holdings_snapshots (
          id TEXT PRIMARY KEY, account_id TEXT, snapshot_date TEXT, currency TEXT,
          positions TEXT, cash_balances TEXT, cost_basis TEXT, net_contribution TEXT,
          calculated_at TEXT, net_contribution_base TEXT, cash_total_account_currency TEXT,
          cash_total_base_currency TEXT, source TEXT);
        CREATE TABLE daily_account_valuation (
          id TEXT PRIMARY KEY, account_id TEXT, valuation_date TEXT, account_currency TEXT,
          base_currency TEXT, fx_rate_to_base TEXT, cash_balance TEXT,
          investment_market_value TEXT, total_value TEXT, cost_basis TEXT,
          net_contribution TEXT, cash_balance_base TEXT, investment_market_value_base TEXT,
          total_value_base TEXT, cost_basis_base TEXT, net_contribution_base TEXT,
          external_inflow_base TEXT, external_outflow_base TEXT, external_flow_source TEXT,
          performance_eligible_value_base TEXT, value_status TEXT, basis_status TEXT, calculated_at TEXT);
        CREATE TABLE sync_outbox (
          event_id TEXT PRIMARY KEY, entity TEXT, entity_id TEXT, op TEXT,
          client_timestamp TEXT, payload TEXT, payload_key_version INTEGER,
          sent INTEGER, status TEXT, retry_count INTEGER, next_retry_at TEXT,
          last_error TEXT, last_error_code TEXT, device_id TEXT, created_at TEXT);
        CREATE TABLE sync_entity_metadata (
          entity TEXT, entity_id TEXT, last_event_id TEXT, last_client_timestamp TEXT,
          last_op TEXT, last_seq INTEGER, PRIMARY KEY(entity,entity_id));
        CREATE TABLE snapshot_positions (id TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE lots (id TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE lot_disposals (id TEXT PRIMARY KEY, payload TEXT);
        INSERT INTO snapshot_positions VALUES ('SYN-POSITION','preserve');
        INSERT INTO lots VALUES ('SYN-LOT','preserve');
        INSERT INTO lot_disposals VALUES ('SYN-DISPOSAL','preserve');
        """)
        for row in rows:
            connection.execute(
                "INSERT INTO activities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["accountId"], row["activityType"], row["date"],
                 str(row["amount"]), row["currency"], row["idempotencyKey"],
                 row["sourceGroupId"], row["assetId"], row["subtype"], row["metadata"],
                 row.get("comment"), "t0", 0),
            )


class DatabaseClient(FakeClient):
    def __init__(self, target: dict):
        self.path = Path(target["database"])
        self.target = target
        state = bp.sqlite_state(self.path)
        rows = [bp._camel(r) for r in bp._rows(state, "activities")]
        for row in rows:
            row["date"] = row.pop("activityDate")
            row["comment"] = row.pop("notes")
        super().__init__(rows)
        self.base = target["origin"]
        self.assignments = {}
        for row in bp._rows(state, "activity_taxonomy_assignments"):
            self.assignments.setdefault(row["activity_id"], []).append(bp._camel(row))

    def get(self, path: str):
        if path == "/app/info":
            return {"version": "synthetic-1", "dbPath": self.target["containerDatabase"]}
        return super().get(path)


def target(path: Path, *, port: int = 8088, name: str = "synthetic-production") -> dict:
    result = {
        "containerId": name.ljust(64, "0"), "containerName": name,
        "composeProject": "synthetic-finance", "composeService": "app",
        "imageId": "sha256:" + "b" * 64, "origin": f"http://127.0.0.1:{port}",
        "database": str(path), "containerDatabase": "/data/wealthfolio.db",
        "containerPort": "8080/tcp",
    }
    # Fingerprint uses only authenticated app identity, not its ledger.
    class Info:
        def get(self, _path):
            return {"version": "synthetic-1", "dbPath": result["containerDatabase"]}
    result["instanceId"] = instance_fingerprint(Info(), result["origin"])
    return result


class Runtime:
    def __init__(self, target: dict, *, running: bool = True):
        self.target, self.running = target, running
        self.stops = self.starts = 0
        self.stop_edit = None
        self.start_failure = False
        self.post_failure = False
        self.start_edit = None
        self.writer_changed = False

    def check(self, *, production: bool, running: bool | None = None):
        bp._origin(self.target["origin"], production=production)
        if running is not None:
            bp._require(running == self.running, "wrong runtime state")
        return {key: self.target[key] for key in bp.IDENTITY_KEYS}

    def stop(self, expected):
        assert expected == self.target
        self.stops += 1
        if self.stop_edit:
            edit, self.stop_edit = self.stop_edit, None
            edit(Path(self.target["database"]))
        self.running = False

    def start(self, expected):
        assert expected == self.target
        self.starts += 1
        if self.start_failure:
            self.start_failure = False
            raise bp.PromotionError("synthetic startup failure")
        self.running = True
        if self.start_edit:
            edit, self.start_edit = self.start_edit, None
            edit(Path(self.target["database"]))

    def client(self):
        assert self.running
        return DatabaseClient(self.target)

    def verify(self, plan, *, expected):
        if expected == "applied" and self.post_failure:
            raise bp.PromotionError("synthetic postcondition failure")
        client = self.client()
        status, _, fingerprint = receipt_repair._activity_state(client, plan)
        del status
        expected_hash = plan["preconditions" if expected == "ready" else "expected"][
            "globalLedgerFingerprint"
        ]
        bp._require(fingerprint == expected_hash, "synthetic API ledger differs")
        actual, _, _ = receipt_repair._target_status(client, bp.SpendingAdapter(client), plan)
        bp._require(actual == expected, "synthetic API dependent state differs")
        return {"instanceId": self.target["instanceId"], "status": actual,
                "ledgerHash": fingerprint}

    def wait(self, plan, *, expected):
        return self.verify(plan, expected=expected)

    def writer_state(self, tasks):
        bp._require(not self.writer_changed, "synthetic legacy writer enabled")
        return {"tasks": tasks, "definitionHash": "c" * 64}


def canonical_publication(root, plan, source_files, description):
    directory = root / "normalized" / "canonical"
    directory.mkdir(parents=True)
    operation = plan["operations"]["repairs"][0]
    records, scope_rows = [], []
    for role, family in (("source", "simplefin"), ("survivor", "ofx")):
        row = {
            "date": "2026-01-15", "account_id": "SYN-CANONICAL", "amount": "-10.00",
            "description": description, "currency": "USD", "source_id": f"{family}:SYN-ACCOUNT:SYN-TXN",
            "source_file": source_files[family]["path"],
        }
        fingerprint = bp.stable_hash(row)
        observation_id = bp.stable_hash({
            "kind": "canonical-source-observation", "fingerprint": fingerprint, "ordinal": 1,
        })
        operation[role + "ObservationId"] = observation_id
        records.append({
            "observationId": observation_id, "observationFingerprint": fingerprint,
            "transaction": row, "canonicalTransactionId": operation["canonicalTransactionId"],
            "decisionId": operation["decisionId"], "sourceReplayOrdinal": 1,
            "disposition": "suppressed" if role == "source" else "active",
        })
        scope_rows.append(row)
    operation["sourceHashes"] = sorted(
        set(operation["sourceHashes"]) | {bp.content_hash(row) for row in scope_rows}
    )
    scope = {"schemaVersion": 1, "kind": "canonical-identity-scope", "private": True,
             "rowCount": len(scope_rows), "scopeHash": bp.content_hash(scope_rows), "rows": scope_rows}
    save(directory / "transaction-observations.json", {
        "schemaVersion": 1, "kind": "canonical-transaction-observations", "private": True,
        "identityScope": scope, "observations": records, "observationCount": len(records),
    })
    identity = {
        "policyDocument": plan["evidence"]["identityPolicyDocument"],
        "policyHash": plan["evidence"]["identityPolicyHash"],
        "generationHash": plan["evidence"]["canonicalGenerationHash"],
        "canonicalStateHash": plan["evidence"]["canonicalStateHash"],
    }
    save(directory / "transaction-lineage.json", {
        "schemaVersion": 1, "kind": "canonical-transaction-lineage", "private": True,
        "identityPolicy": identity, "baselinePublicationId": plan["evidence"]["baselinePublicationId"],
        "forensicPublicationId": plan["evidence"]["forensicPublicationId"],
        "decisionProjections": [{
            "decisionId": operation["decisionId"], "decisionHash": operation["decisionHash"],
            "sourceHashes": operation["sourceHashes"], "featureVector": operation["featureVector"],
            "competingCandidateProof": operation["competingCandidateProof"],
            "sourceAuthorityPolicyHash": operation["authorityPolicyHash"],
            "canonicalTransactionIds": [operation["canonicalTransactionId"]],
        }],
        "canonicalTransactions": [{
            "canonicalTransactionId": operation["canonicalTransactionId"],
            "activeObservationId": operation["survivorObservationId"],
            "memberObservationIds": [row["observationId"] for row in records],
        }],
    })
    with (directory / "transactions.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(scope_rows[1]))
        writer.writeheader()
        writer.writerow(scope_rows[1])
    data_files = {path.name: bp._hash(path) for path in directory.iterdir()}
    manifest = directory / "manifest.json"
    save(manifest, {
        "schemaVersion": 5, "dataFiles": data_files,
        "sourceFiles": [source_files[family] for family in ("ofx", "simplefin")],
        "lineageReview": {"identityPolicy": identity},
    })
    return bp.file_evidence(manifest, root)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    # Synthetic runtime artifacts only; never read the real external finance directory.
    monkeypatch.setattr(bp, "REPO_ROOT", tmp_path / "public-checkout")
    monkeypatch.setattr(receipt_repair, "REPO_ROOT", tmp_path / "public-checkout")
    monkeypatch.setattr(bp, "release_revision", lambda: REVISION)
    rows = [
        activity("SYN-SOURCE", "simplefin:SYN-ACCOUNT:SYN-1", 10, "WITHDRAWAL"),
        activity("SYN-SURVIVOR", "extract:SYN-ACCOUNT:SYN-2", 10, "WITHDRAWAL"),
        activity("SYN-RECON", "reconciliation:SYN", 100, "CREDIT"),
    ]
    for row in rows[:2]:
        row["comment"] = "SYNTHETIC GROCER PURCHASE"
    rows[2]["comment"] = "SYNTHETIC CASH RECONCILIATION"
    plan = repair_plan(rows)
    originals = {}
    for name in ("sourceApplicationPlanSha256", "sourceApplicationReceiptSha256"):
        path = tmp_path / (name + ".json")
        save(path, {"syntheticEvidence": name})
        originals[name] = bp.file_evidence(path, tmp_path)
        plan["evidence"][name] = originals[name]["sha256"]
    policy = plan["evidence"]["identityPolicyDocument"]
    interval_ids = {}
    raw_hashes = []
    for interval in policy["sourceAuthority"]["intervals"]:
        family = interval["evidence"]["sourceFamily"]
        path = tmp_path / f"synthetic-{family}-source.{'ofx' if family == 'ofx' else 'json'}"
        if family == "ofx":
            path.write_text(
                "<OFX><BANKTRANLIST><STMTTRN><TRNAMT>-10.00</TRNAMT>"
                "<FITID>SYN-TXN</FITID><NAME>SYNTHETIC GROCER PURCHASE</NAME>"
                "</STMTTRN></BANKTRANLIST></OFX>", encoding="utf-8",
            )
        else:
            save(path, {"accounts": [{"id": "SYN-ACCOUNT", "currency": "USD", "transactions": [
                {"id": "SYN-TXN", "posted": 1768435200, "amount": "-10.00",
                 "description": "SYNTHETIC GROCER PURCHASE"}
            ]}]})
        evidence = bp.file_evidence(path, tmp_path)
        originals[family] = evidence
        raw_hashes.append(evidence["sha256"])
        interval["evidence"]["sourceHashes"] = [evidence["sha256"]]
        interval_ids[family] = plan_fingerprint(interval)
    plan["evidence"]["identityPolicyHash"] = plan_fingerprint(policy)
    plan["evidence"]["sourceAuthorityHash"] = plan_fingerprint(policy["sourceAuthority"])
    for operation in plan["operations"]["repairs"]:
        operation["sourceHashes"] = raw_hashes
        features = operation["featureVector"]
        features["authoritativeIntervalId"] = interval_ids[features["authoritativeSourceFamily"]]
        features["suppressedIntervalId"] = interval_ids[features["suppressedSourceFamily"]]
    originals["canonicalManifestSha256"] = canonical_publication(
        tmp_path, plan, originals, rows[0]["comment"]
    )
    plan["evidence"]["canonicalManifestSha256"] = originals["canonicalManifestSha256"]["sha256"]
    plan["scope"]["sourceApplicationPlanSha256"] = plan["evidence"]["sourceApplicationPlanSha256"]
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    save(tmp_path / "plan.json", plan)
    original, candidate, live = [tmp_path / name for name in ("original.db", "candidate.db", "live.db")]
    make_database(original, rows)
    shutil.copyfile(original, candidate)
    sql(candidate, "DELETE FROM activities WHERE id='SYN-SOURCE'")
    sql(candidate, "UPDATE activities SET amount='90.0',updated_at='t1' WHERE id='SYN-RECON'")
    shutil.copyfile(original, live)
    runtime = Runtime(target(live))
    clone_instance = target(tmp_path / "applied-clone.db", port=18091)["instanceId"]
    backup = bp.backup_evidence(original, tmp_path)
    recovery = {
        "schemaVersion": 2, "kind": receipt_repair.KIND + "-recovery", "status": "prepared",
        "planHash": plan["planHash"], "instanceId": clone_instance,
        "preLedgerFingerprint": plan["preconditions"]["globalLedgerFingerprint"], "backup": backup,
    }
    recovery["recoveryHash"] = plan_fingerprint(recovery)
    receipt = {
        "schemaVersion": 2, "kind": receipt_repair.KIND + "-receipt", "status": "applied",
        "planHash": plan["planHash"], "instanceId": clone_instance,
        "preLedgerFingerprint": plan["preconditions"]["globalLedgerFingerprint"],
        "postLedgerFingerprint": plan["expected"]["globalLedgerFingerprint"],
        "operationCounts": {"deleted": 1, "assignmentUpserts": 0, "reconciliationUpdates": 1},
        "accountValueConserved": True, "accountValueBasis": "cash-ledger",
        "accountValueBefore": "80", "accountValueAfter": "80",
        "backup": backup, "recoveryHash": recovery["recoveryHash"],
    }
    receipt["receiptHash"] = plan_fingerprint(receipt)
    save(tmp_path / "receipt.json", receipt)
    save(tmp_path / "recovery.json", recovery)
    request = {
        "plan": "plan.json", "receipt": "receipt.json", "recovery": "recovery.json",
        "review": "review.json", "releaseRevision": REVISION, "target": runtime.target,
        "appliedCloneInstanceId": clone_instance,
        "originalBackup": "original.db", "candidateBackup": "candidate.db",
        "originalRestoreProof": "original-proof.json", "candidateRestoreProof": "candidate-proof.json",
    }
    proofs = {}
    for i, (name, source, expected) in enumerate(
        (("original", original, "ready"), ("candidate", candidate, "applied"))
    ):
        clone_runtime = Runtime(target(tmp_path / (name + "-restored.db"),
                                       port=18093 + i, name=f"synthetic-{name}-restore"), running=False)
        proof = bp.restore_proof(root=tmp_path, backup=source, plan=plan, runtime=clone_runtime,
                                 evidence_key=EVIDENCE_KEY, expected=expected)
        assert clone_runtime.starts == clone_runtime.stops == 1
        proofs[name] = proof
        save(tmp_path / (name + "-proof.json"), proof)
    diff = bp.validate_bounded_diff(bp.sqlite_state(original), bp.sqlite_state(candidate), plan)
    tasks = [{"taskPath": "\\Synthetic\\", "taskName": "DisabledLegacyWriter"}]
    review = bp.seal({
        "kind": bp.KIND + "-review", "planHash": plan["planHash"],
        "diffHash": plan_fingerprint(diff), "evidenceHash": plan_fingerprint(plan["evidence"]),
        "releaseRevision": REVISION, "targetHash": plan_fingerprint(runtime.target),
        "clonePrestateDiffHash": plan_fingerprint([]),
        "restoreDiffHashes": {name: proof["environmentDiffHash"] for name, proof in proofs.items()},
        "runtimePolicyHash": plan_fingerprint(bp.build_timestamp_policy(
            diff, *(proof["environmentDiff"] for proof in proofs.values())
        )),
        "canonicalEvidenceHash": plan_fingerprint(bp.canonical_evidence_binding(root=tmp_path, plan=plan)),
        "independentEvidence": list(originals.values()), "legacyWriterTasks": tasks,
        "legacyWriterState": runtime.writer_state(tasks),
    }, EVIDENCE_KEY)
    save(tmp_path / "review.json", review)
    document = bp.prepare(root=tmp_path, request=request, runtime=runtime, evidence_key=EVIDENCE_KEY)
    assert runtime.stops == 0
    now = datetime.now(timezone.utc)
    authorization = bp.seal({
        "kind": bp.KIND + "-authorization", "preparationId": document["documentHash"],
        "planHash": document["planHash"], "releaseRevision": REVISION,
        "targetHash": document["targetHash"], "diffHash": document["diffHash"],
        "approval": bp.APPROVAL, "operator": "Synthetic operator",
        "writeBoundary": "NO_APP_OR_EXTERNAL_WRITES_UNTIL_TERMINAL_RECEIPT",
        "issuedAt": now.isoformat(), "expiresAt": (now + timedelta(minutes=10)).isoformat(),
    }, OPERATOR_KEY)
    return {
        "root": tmp_path, "original": original, "candidate": candidate, "live": live,
        "runtime": runtime, "plan": plan, "request": request, "document": document,
        "authorization": authorization,
    }


def execute(f):
    return bp.execute(root=f["root"], document=f["document"], authorization=f["authorization"],
                      supplied_preparation_id=f["document"]["documentHash"], runtime=f["runtime"],
                      evidence_key=EVIDENCE_KEY, operator_key=OPERATOR_KEY)


def recover(f):
    return bp.recover(root=f["root"], runtime=f["runtime"], evidence_key=EVIDENCE_KEY,
                      operator_key=OPERATOR_KEY,
                      expected_preparation_id=f["document"]["documentHash"])


def refresh_fixture(f, *, candidate_restore_edit=None):
    """Recreate real synthetic copy/start/read/stop proofs after a fixture-only edit."""
    root = f["root"]
    backup = bp.backup_evidence(f["original"], root)
    recovery = bp.load(root / "recovery.json")
    recovery["backup"] = backup
    recovery["recoveryHash"] = plan_fingerprint({k: v for k, v in recovery.items() if k != "recoveryHash"})
    receipt = bp.load(root / "receipt.json")
    receipt["backup"] = backup
    receipt["recoveryHash"] = recovery["recoveryHash"]
    receipt["receiptHash"] = plan_fingerprint({k: v for k, v in receipt.items() if k != "receiptHash"})
    save(root / "recovery.json", recovery)
    save(root / "receipt.json", receipt)
    shutil.copyfile(f["original"], f["live"])
    proofs = {}
    for i, name in enumerate(("original", "candidate")):
        runtime = Runtime(target(root / f"refreshed-{name}.db", port=18110 + i,
                                 name=f"synthetic-refreshed-{name}"), running=False)
        if name == "candidate":
            runtime.start_edit = candidate_restore_edit
        proofs[name] = bp.restore_proof(
            root=root, backup=f[name], plan=f["plan"], runtime=runtime,
            evidence_key=EVIDENCE_KEY, expected="ready" if name == "original" else "applied",
        )
        save(root / f"{name}-proof.json", proofs[name])
    changes = bp.validate_bounded_diff(
        bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), f["plan"]
    )
    review = bp.unseal(bp.load(root / "review.json"), EVIDENCE_KEY, bp.KIND + "-review")
    review.update(
        diffHash=plan_fingerprint(changes),
        restoreDiffHashes={name: proof["environmentDiffHash"] for name, proof in proofs.items()},
        runtimePolicyHash=plan_fingerprint(bp.build_timestamp_policy(
            changes, *(proof["environmentDiff"] for proof in proofs.values())
        )),
    )
    save(root / "review.json", bp.seal(review, EVIDENCE_KEY))
    f["document"] = bp.prepare(
        root=root, request=f["request"], runtime=f["runtime"], evidence_key=EVIDENCE_KEY
    )
    authorization = bp.unseal(f["authorization"], OPERATOR_KEY, bp.KIND + "-authorization")
    authorization.update(preparationId=f["document"]["documentHash"], diffHash=f["document"]["diffHash"])
    f["authorization"] = bp.seal(authorization, OPERATOR_KEY)


def realistic_candidate_delta(f):
    for path, cash, stamp in (
        (f["original"], "90", "2026-01-20T00:00:00Z"),
        (f["candidate"], "80", "2026-01-20T01:00:00Z"),
    ):
        sql(path, "INSERT INTO holdings_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("SYN-SNAPSHOT", "SYN-ACCOUNT", "2026-01-16", "USD", "{}", '{"USD":"' + cash + '"}',
             "0", cash, stamp, cash, cash, cash, "CALCULATED"))
        sql(path, "INSERT INTO daily_account_valuation VALUES (" + ",".join(["?"] * 23) + ")",
            ("SYN-DAILY", "SYN-ACCOUNT", "2026-01-16", "USD", "USD", "1", cash, "0",
             cash, "0", cash, cash, "0", cash, "0", cash, "0", "0", "ACTIVITY",
             cash, "OK", "COMPLETE", stamp))
        for activity_id in ("SYN-SOURCE", "SYN-RECON"):
            sql(path, "INSERT INTO sync_entity_metadata VALUES (?,?,?,?,?,?)",
                ("activity", activity_id, "old-" + activity_id, "t0", "create", 7))
    sql(f["candidate"], "UPDATE quotes SET created_at='2026-01-20T01:00:00Z' WHERE source='YAHOO'")
    sql(f["candidate"], "UPDATE quotes SET close='11',adjclose='11',volume='20' WHERE id='SYN-LATEST'")
    sql(f["candidate"], "UPDATE quote_sync_state SET last_synced_at=?,updated_at=?",
        ("2026-01-20T01:00:00Z", "2026-01-20T01:00:00Z"))
    rows = {r["id"]: r for r in bp._rows(bp.sqlite_state(f["candidate"]), "activities")}
    for activity_id, operation in (("SYN-SOURCE", "delete"), ("SYN-RECON", "update")):
        event_id = "new-" + activity_id
        payload = {"id": activity_id} if operation == "delete" else rows[activity_id]
        sql(f["candidate"], "INSERT INTO sync_outbox VALUES (" + ",".join(["?"] * 15) + ")",
            (event_id, "activity", activity_id, operation, "t1", json.dumps(payload),
             1, 0, "pending", 0, None, None, None, None, "t1"))
        sql(f["candidate"], "UPDATE sync_entity_metadata SET last_event_id=?,last_client_timestamp=?,"
            "last_op=? WHERE entity='activity' AND entity_id=?", (event_id, "t1", operation, activity_id))


def refresh_times(path):
    stamp = datetime.now(timezone.utc).isoformat()
    sql(path, "UPDATE quotes SET created_at=? WHERE source='YAHOO'", (stamp,))
    sql(path, "UPDATE quote_sync_state SET last_synced_at=?,updated_at=?", (stamp, stamp))
    sql(path, "UPDATE holdings_snapshots SET calculated_at=?", (stamp,))
    sql(path, "UPDATE daily_account_valuation SET calculated_at=?", (stamp,))


def test_unrelated_rest_decimal_rounding_does_not_require_ledger_reimplementation(fixture):
    from importers.simplefin.application import ledger_fingerprint

    f = fixture
    for path in (f["original"], f["candidate"]):
        sql(
            path,
            "INSERT INTO activities "
            "(id,account_id,activity_type,activity_date,amount,currency,idempotency_key,metadata) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("SYN-INVESTMENT", "SYN-BROKER", "BUY", "2026-01-15T00:00:00Z",
             "1.123456789123456789", "USD", "extract:SYN-BROKER:1", "{}"),
        )
    plan = deepcopy(f["plan"])
    for path, label in ((f["original"], "preconditions"), (f["candidate"], "expected")):
        rows = [bp._camel(row) for row in bp._rows(bp.sqlite_state(path), "activities")]
        raw = ledger_fingerprint(rows, {row["accountId"] for row in rows})
        for row in rows:
            if row["id"] == "SYN-INVESTMENT":
                row["amount"] = float(row["amount"])
        api = ledger_fingerprint(rows, {row["accountId"] for row in rows})
        assert raw != api
        plan[label]["activityCount"] += 1
        plan[label]["globalLedgerFingerprint"] = api
    plan["planHash"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planHash"
    })
    bp.validate_bounded_diff(
        bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan
    )
    sql(f["candidate"], "UPDATE activities SET amount='2' WHERE id='SYN-INVESTMENT'")
    with pytest.raises(bp.PromotionError):
        bp.validate_bounded_diff(
            bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan
        )


def test_stopped_checkpoint_retains_committed_wal_instead_of_discarding_it(fixture):
    f = fixture
    f["runtime"].running = False
    script = (
        "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA journal_mode=WAL'); "
        "c.execute(\"UPDATE unknown_user_work SET unknown_field='SYN-COMMITTED-WAL'\"); "
        "c.commit(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(f["live"])], check=True)
    assert Path(str(f["live"]) + "-wal").exists()
    before = bp.sqlite_state(f["live"])
    result = bp.checkpoint_stopped_database(f["runtime"], production=True)
    assert result["checkpointed"]
    assert bp.sqlite_state(f["live"], stopped=True)["stateHash"] == before["stateHash"]
    assert bp._rows(bp.sqlite_state(f["live"]), "unknown_user_work")[0][
        "unknown_field"
    ] == "SYN-COMMITTED-WAL"


def test_checkpoint_refuses_running_owner_or_live_external_writer(fixture):
    f = fixture
    with pytest.raises(bp.PromotionError, match="runtime state"):
        bp.checkpoint_stopped_database(f["runtime"], production=True)
    f["runtime"].running = False
    with closing(sqlite3.connect(f["live"])) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE unknown_user_work SET unknown_field='SYN-UNCOMMITTED'")
        with pytest.raises(bp.PromotionError, match="WAL"):
            bp.checkpoint_stopped_database(f["runtime"], production=True)
        writer.rollback()
    assert bp._rows(bp.sqlite_state(f["live"]), "unknown_user_work")[0][
        "unknown_field"
    ] == "Keep every byte"


def test_success_preserves_unknown_columns_and_all_tables(fixture):
    f = fixture
    result = execute(f)
    assert result["phase"] == "completed"
    assert bp.sqlite_state(f["live"])["stateHash"] == f["document"]["candidateStateHash"]
    slots = bp._slots(f["root"], f["runtime"].target)
    assert bp._hash(slots["original"]) == bp._hash(f["original"])
    assert bp._rows(bp.sqlite_state(f["live"]), "unknown_user_work") == [
        {"id": "SYN-USER", "unknown_field": "Keep every byte",
         "raw": {"sqliteBlobBase64": "AAH/"}}
    ]
    assert not (f["root"] / "wealthfolio-rebuild" / "writer-ownership.json").exists()
    with pytest.raises(bp.PromotionError):
        execute(f)


def test_merchant_description_is_not_mistaken_for_an_unreviewed_user_note(fixture):
    f = fixture
    source = next(row for row in bp._rows(bp.sqlite_state(f["original"]), "activities")
                  if row["id"] == "SYN-SOURCE")
    assert source["notes"] == "SYNTHETIC GROCER PURCHASE"
    assert source["notes"] == f["plan"]["operations"]["repairs"][0]["sourceRollbackPayload"]["comment"]
    bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), f["plan"])
    sql(f["original"], "UPDATE activities SET notes=notes || ' - user added note' WHERE id='SYN-SOURCE'")
    with pytest.raises(bp.PromotionError, match="user-owned work"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), f["plan"])


def test_reviewed_description_does_not_bypass_user_modified_flag(fixture):
    f = fixture
    sql(f["original"], "UPDATE activities SET is_user_modified=1 WHERE id='SYN-SOURCE'")
    with pytest.raises(bp.PromotionError, match="user-owned work"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), f["plan"])


def test_normalized_row_hashes_bind_scope_not_fictitious_artifact_files(fixture):
    f = fixture
    binding = bp.canonical_evidence_binding(root=f["root"], plan=f["plan"])
    normalized = set(binding["normalizedRowHashes"])
    raw = bp.required_review_hashes(f["plan"], binding)
    assert len(normalized) == 2
    assert normalized.isdisjoint(raw)
    assert normalized <= set(f["plan"]["operations"]["repairs"][0]["sourceHashes"])
    assert all(bp.content_hash(item["row"]) == item["rowHash"] for item in binding["normalizedRows"])
    assert all(not (f["root"] / digest).exists() for digest in normalized)
    assert bp.build_review_inputs(root=f["root"], request=f["request"], evidence_key=EVIDENCE_KEY)[
        "normalizedRowHashes"
    ] == binding["normalizedRowHashes"]


@pytest.mark.parametrize("filename", ["transaction-observations.json", "transaction-lineage.json",
                                     "transactions.csv"])
def test_original_canonical_data_bytes_remain_bound(fixture, filename):
    f = fixture
    path = f["root"] / "normalized" / "canonical" / filename
    with path.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(bp.PromotionError, match="artifact bytes changed"):
        execute(f)
    assert f["runtime"].stops == 0


def test_unknown_normalized_hash_is_not_reclassified_as_a_raw_artifact(fixture):
    f = fixture
    plan = deepcopy(f["plan"])
    operation = plan["operations"]["repairs"][0]
    operation["sourceHashes"].append("0" * 64)
    directory = f["root"] / "normalized" / "canonical"
    lineage = bp.load(directory / "transaction-lineage.json")
    lineage["decisionProjections"][0]["sourceHashes"] = operation["sourceHashes"]
    save(directory / "transaction-lineage.json", lineage)
    manifest = bp.load(directory / "manifest.json")
    manifest["dataFiles"]["transaction-lineage.json"] = bp._hash(directory / "transaction-lineage.json")
    save(directory / "manifest.json", manifest)
    plan["evidence"]["canonicalManifestSha256"] = bp._hash(directory / "manifest.json")
    with pytest.raises(bp.PromotionError, match="no declared raw or normalized-row namespace"):
        bp.canonical_evidence_binding(root=f["root"], plan=plan)


def test_realistic_delta_and_reviewed_row_timestamp_refresh_promote(fixture):
    f = fixture
    realistic_candidate_delta(f)
    refresh_fixture(f)
    f["runtime"].start_edit = refresh_times
    result = execute(f)
    assert result["phase"] == "completed"
    verification = result["databaseVerification"]
    assert verification["timestampDiff"]
    assert verification["referenceStateHash"] == f["document"]["candidateStateHash"]
    assert verification["actualStateHash"] == bp.sqlite_state(f["live"])["stateHash"]
    for table in ("unknown_user_work", "snapshot_positions", "lots", "lot_disposals"):
        assert bp._rows(bp.sqlite_state(f["original"]), table) == bp._rows(bp.sqlite_state(f["live"]), table)


def test_exact_reviewed_restored_market_values_are_an_authorized_reference(fixture):
    f = fixture
    realistic_candidate_delta(f)
    def reviewed_market_refresh(path):
        refresh_times(path)
        sql(path, "UPDATE quotes SET close='12',adjclose='12' WHERE id='SYN-LATEST'")
    refresh_fixture(f, candidate_restore_edit=reviewed_market_refresh)
    proof = f["document"]["restoreProofs"]["candidate"]
    assert proof["environmentDiff"]
    assert proof["backupStateHash"] != proof["stateHash"]
    f["runtime"].start_edit = reviewed_market_refresh
    result = execute(f)
    assert result["databaseVerification"]["referenceStateHash"] == proof["stateHash"]


def test_unreviewed_market_price_during_startup_requires_manual_recovery(fixture):
    f = fixture
    realistic_candidate_delta(f)
    refresh_fixture(f)
    f["runtime"].start_edit = lambda path: sql(
        path, "UPDATE quotes SET close='999' WHERE id='SYN-LATEST'"
    )
    with pytest.raises(bp.ManualRecoveryRequired):
        execute(f)
    assert f["runtime"].running
    row = next(row for row in bp._rows(bp.sqlite_state(f["live"]), "quotes") if row["id"] == "SYN-LATEST")
    assert row["close"] == "999"
    slots = bp._slots(f["root"], f["runtime"].target)
    assert bp.load(slots["journal"])["phase"] == "manual-recovery-required"
    assert bp._hash(slots["original"]) == bp._hash(f["original"])
    assert not slots["rejected"].exists()
    stops = f["runtime"].stops
    with pytest.raises(bp.PromotionError, match="latched"):
        recover(f)
    assert f["runtime"].stops == stops


def test_market_allowances_never_relax_stopped_production_freshness(fixture):
    f = fixture
    realistic_candidate_delta(f)
    refresh_fixture(f)
    f["runtime"].stop_edit = lambda path: sql(
        path, "UPDATE quotes SET close='99' WHERE id='SYN-LATEST'"
    )
    with pytest.raises(bp.PromotionError):
        execute(f)
    assert f["runtime"].running
    assert not bp._slots(f["root"], f["runtime"].target)["original"].exists()
    quote = next(row for row in bp._rows(bp.sqlite_state(f["live"]), "quotes") if row["id"] == "SYN-LATEST")
    assert quote["close"] == "99"


@pytest.mark.parametrize("statement", [
    "UPDATE sync_entity_metadata SET last_seq=8",
    "UPDATE sync_entity_metadata SET last_event_id='unrelated'",
    "UPDATE sync_entity_metadata SET last_client_timestamp='unrelated'",
    "UPDATE sync_entity_metadata SET last_op='create'",
    "UPDATE sync_outbox SET payload_key_version=100",
    "UPDATE sync_outbox SET device_id='unrelated-device'",
    "UPDATE quote_sync_state SET sync_priority=2",
    "UPDATE quote_sync_state SET data_source='MANUAL'",
    "UPDATE snapshot_positions SET payload='lost'",
    "UPDATE lots SET payload='lost'",
    "UPDATE lot_disposals SET payload='lost'",
])
def test_realistic_delta_still_blocks_unrelated_metadata_and_position_edits(fixture, statement):
    f = fixture
    realistic_candidate_delta(f)
    sql(f["candidate"], statement)
    with pytest.raises(bp.PromotionError):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), f["plan"])


def test_runtime_timestamp_allowance_is_row_specific_and_time_bounded(fixture):
    f = fixture
    realistic_candidate_delta(f)
    before = bp.sqlite_state(f["original"])
    reference = bp.sqlite_state(f["candidate"])
    policy = bp.build_timestamp_policy(bp.validate_bounded_diff(before, reference, f["plan"]))
    sql(f["candidate"], "UPDATE quotes SET created_at='2039-01-01T00:00:00Z' WHERE id='SYN-LATEST'")
    now = datetime.now(timezone.utc)
    with pytest.raises(bp.PromotionError):
        bp.verify_runtime_state(bp.sqlite_state(f["candidate"]), [reference], f["plan"], policy,
                                started_at=now, checked_at=now)
    sql(f["candidate"], "UPDATE quotes SET created_at=? WHERE id='SYN-LATEST'",
        ("2026-01-20T01:00:00Z",))
    sql(f["candidate"], "UPDATE quotes SET created_at=? WHERE id='SYN-MANUAL'", (now.isoformat(),))
    with pytest.raises(bp.PromotionError):
        bp.verify_runtime_state(bp.sqlite_state(f["candidate"]), [reference], f["plan"], policy,
                                started_at=now, checked_at=now)

def test_stale_user_edit_after_prepare_before_stop_is_not_overwritten(fixture):
    f = fixture
    f["runtime"].stop_edit = lambda path: sql(
        path, "UPDATE app_settings SET value='light' WHERE id='SYN-THEME'"
    )
    with pytest.raises(bp.PromotionError):
        execute(f)
    assert f["runtime"].running
    assert bp._rows(bp.sqlite_state(f["live"]), "app_settings")[0]["value"] == "light"
    slots = bp._slots(f["root"], f["runtime"].target)
    assert not slots["original"].exists()
    assert bp.load(slots["journal"])["phase"] == "rolled-back"


def test_stale_edit_after_sqlite_snapshot_before_mandatory_fence_is_preserved(fixture, monkeypatch):
    f = fixture
    fence = bp.exclusive_database
    edited = False
    @contextmanager
    def late_edit(path):
        nonlocal edited
        if path == f["live"] and not edited:
            edited = True
            sql(path, "UPDATE unknown_user_work SET unknown_field='Newest user edit'")
        with fence(path) as digest:
            yield digest
    monkeypatch.setattr(bp, "exclusive_database", late_edit)
    with pytest.raises(bp.PromotionError):
        execute(f)
    assert f["runtime"].running
    assert bp._rows(bp.sqlite_state(f["live"]), "unknown_user_work")[0]["unknown_field"] == "Newest user edit"
    assert not bp._slots(f["root"], f["runtime"].target)["original"].exists()


@pytest.mark.parametrize("artifact", ["candidate.db", "plan.json", "receipt.json", "recovery.json",
                                    "candidate-proof.json", "review.json"])
def test_changed_bound_artifact_rejected_before_stop(fixture, artifact):
    path = fixture["root"] / artifact
    with path.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(Exception):
        execute(fixture)
    assert fixture["runtime"].stops == 0


def test_raw_source_bytes_are_rechecked_not_only_the_publication_manifest(fixture):
    source = fixture["root"] / "synthetic-ofx-source.ofx"
    with source.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(bp.PromotionError, match="artifact bytes changed"):
        execute(fixture)
    assert fixture["runtime"].stops == 0


def test_independent_review_cannot_omit_selected_raw_source_files(fixture):
    f = fixture
    path = f["root"] / "review.json"
    review = bp.unseal(bp.load(path), EVIDENCE_KEY, bp.KIND + "-review")
    review["independentEvidence"] = [
        row for row in review["independentEvidence"]
        if row["path"] != "synthetic-ofx-source.ofx"
    ]
    save(path, bp.seal(review, EVIDENCE_KEY))
    with pytest.raises(bp.PromotionError, match="selected raw sources"):
        bp.prepare(root=f["root"], request=f["request"], runtime=f["runtime"],
                   evidence_key=EVIDENCE_KEY)
    assert f["runtime"].stops == 0


@pytest.mark.parametrize("artifact", ["plan.json", "synthetic-ofx-source.ofx"])
def test_changed_input_while_staging_is_rejected_before_stop(fixture, monkeypatch, artifact):
    f = fixture
    copy = bp._copy_verified
    def edit_input_after_copy(source, destination, expected):
        copy(source, destination, expected)
        with (f["root"] / artifact).open("ab") as stream:
            stream.write(b"\n")
    monkeypatch.setattr(bp, "_copy_verified", edit_input_after_copy)
    with pytest.raises(bp.PromotionError):
        execute(f)
    assert f["runtime"].stops == 0
    assert bp.load(bp._slots(f["root"], f["runtime"].target)["journal"])["phase"] == "aborted"


@pytest.mark.parametrize("field", ["preparationId", "planHash", "targetHash", "releaseRevision",
                                  "diffHash", "approval"])
def test_even_resigned_wrong_authorization_cannot_stop(fixture, field):
    auth = bp.unseal(fixture["authorization"], OPERATOR_KEY, bp.KIND + "-authorization")
    auth[field] = "incorrect"
    fixture["authorization"] = bp.seal(auth, OPERATOR_KEY)
    with pytest.raises(bp.PromotionError):
        execute(fixture)
    assert fixture["runtime"].stops == 0


def test_expired_authorization_cannot_stop(fixture):
    auth = bp.unseal(fixture["authorization"], OPERATOR_KEY, bp.KIND + "-authorization")
    auth["issuedAt"] = "2020-01-01T00:00:00+00:00"
    auth["expiresAt"] = "2020-01-01T00:10:00+00:00"
    fixture["authorization"] = bp.seal(auth, OPERATOR_KEY)
    with pytest.raises(bp.PromotionError, match="expired"):
        execute(fixture)
    assert fixture["runtime"].stops == 0


def test_invalid_operator_signature_cannot_stop(fixture):
    fixture["authorization"]["signature"] = "0" * 64
    with pytest.raises(bp.PromotionError, match="signature"):
        execute(fixture)
    assert fixture["runtime"].stops == 0


def test_disabled_writer_precondition_is_verified_not_a_boolean(fixture):
    fixture["runtime"].writer_changed = True
    with pytest.raises(bp.PromotionError, match="writer"):
        execute(fixture)
    assert fixture["runtime"].stops == 0


def test_real_restore_startup_drift_fails_proof(fixture):
    f = fixture
    runtime = Runtime(target(f["root"] / "fresh-restore.db", port=18096, name="synthetic-fresh"),
                      running=False)
    runtime.start_edit = lambda path: sql(path, "UPDATE quotes SET close='9'")
    with pytest.raises(bp.PromotionError, match="quote"):
        bp.restore_proof(root=f["root"], backup=f["candidate"], plan=f["plan"], runtime=runtime,
                         evidence_key=EVIDENCE_KEY, expected="applied")
    assert not runtime.running


def test_missing_restore_database_fails_before_stop(fixture):
    (fixture["root"] / "candidate-restored.db").unlink()
    with pytest.raises(bp.PromotionError):
        execute(fixture)
    assert fixture["runtime"].stops == 0


def test_a_verified_boolean_is_not_a_restore_proof(fixture):
    save(fixture["root"] / "candidate-proof.json", {"verified": True})
    with pytest.raises(bp.PromotionError):
        bp.prepare(root=fixture["root"], request=fixture["request"], runtime=fixture["runtime"],
                   evidence_key=EVIDENCE_KEY)
    assert fixture["runtime"].stops == 0


@pytest.mark.parametrize("statement", [
    "UPDATE activities SET notes='Lost survivor note' WHERE id='SYN-SURVIVOR'",
    "UPDATE activities SET notes='Lost compensation note' WHERE id='SYN-RECON'",
    "UPDATE app_settings SET value='light'",
    "UPDATE quotes SET close='99' WHERE id='SYN-MANUAL'",
    "UPDATE quotes SET close='99' WHERE id='SYN-HISTORICAL'",
    "UPDATE unknown_user_work SET unknown_field='overwritten'",
    "UPDATE unknown_user_work SET rowid=100",
    "INSERT INTO activity_splits VALUES ('SYN-SPLIT','SYN-SURVIVOR','2')",
    "ALTER TABLE app_settings ADD COLUMN unexpected TEXT",
    "INSERT INTO assets VALUES ('SYN-EXTRA','Unexpected asset')",
    "INSERT INTO activity_taxonomy_assignments VALUES "
    "('SYN-ASSIGN','SYN-SURVIVOR','SYN-TAX','SYN-CAT',10000,'manual','t0','t1')",
])
def test_unrelated_candidate_edits_are_rejected(fixture, statement):
    f = fixture
    sql(f["candidate"], statement)
    with pytest.raises(bp.PromotionError):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]),
                                 f["plan"])


def test_preexisting_assignment_and_split_loss_are_rejected(fixture):
    f = fixture
    assignment = (
        "SYN-ASSIGN", "SYN-SURVIVOR", "SYN-TAX", "SYN-CAT", 10000, "manual",
        ASSIGNMENT_TIME_0, ASSIGNMENT_TIME_0,
    )
    sql(f["original"], "INSERT INTO activity_taxonomy_assignments VALUES (?,?,?,?,?,?,?,?)", assignment)
    row = bp._camel(bp._rows(bp.sqlite_state(f["original"]), "activity_taxonomy_assignments")[0])
    plan = deepcopy(f["plan"])
    plan["preconditions"]["dependentAssignments"]["SYN-SURVIVOR"] = [row]
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    with pytest.raises(bp.PromotionError, match="assignment loss"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)
    sql(f["candidate"], "INSERT INTO activity_taxonomy_assignments VALUES (?,?,?,?,?,?,?,?)", assignment)
    sql(f["original"], "INSERT INTO activity_splits VALUES ('SYN-SPLIT','SYN-SURVIVOR','2')")
    with pytest.raises(bp.PromotionError, match="dependent"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)


def test_assignment_rfc3339_to_naive_utc_keeps_all_nine_fractional_digits(fixture):
    f = fixture
    assignment = (
        "SYN-PRECISE", "SYN-SURVIVOR", "SYN-TAX", "SYN-CAT", 10000, "manual",
        "2026-01-01T00:15:00.123456789+02:00", "2026-01-20T05:30:00.987654321+05:30",
    )
    for path in (f["original"], f["candidate"]):
        sql(path, "INSERT INTO activity_taxonomy_assignments VALUES (?,?,?,?,?,?,?,?)", assignment)
    plan = deepcopy(f["plan"])
    row = bp._camel(bp._rows(bp.sqlite_state(f["original"]), "activity_taxonomy_assignments")[0])
    row["createdAt"] = "2025-12-31T22:15:00.123456789"
    row["updatedAt"] = "2026-01-20T00:00:00.987654321"
    plan["preconditions"]["dependentAssignments"]["SYN-SURVIVOR"] = [row]
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)
    row["updatedAt"] = "2026-01-20T00:00:00.987654320"
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    with pytest.raises(bp.PromotionError, match="dependent assignment snapshot"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)
    row["updatedAt"] = "2026-01-20T00:00:00.987654321"
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    # Equivalent API rendering is NOT permission to alter an unrelated raw SQLite row.
    sql(f["candidate"], "UPDATE activity_taxonomy_assignments SET updated_at=? WHERE id='SYN-PRECISE'",
        (row["updatedAt"],))
    with pytest.raises(bp.PromotionError, match="unplanned assignment"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)


@pytest.mark.parametrize("stored,api", [
    ("2026-01-20T00:00:00Z", "2026-01-20T00:00:00"),
    ("2026-01-20T00:00:00.123000000+00:00", "2026-01-20T00:00:00.123"),
    ("2026-01-20T00:00:00.000000001-04:00", "2026-01-20T04:00:00.000000001"),
])
def test_assignment_pinned_serializer_equivalents(stored, api):
    assert bp._assignment_timestamp(stored) == bp._assignment_timestamp(api)

def test_exact_manual_assignment_migration_is_supported(fixture):
    f = fixture
    sql(f["original"], "INSERT INTO activity_taxonomy_assignments VALUES (?,?,?,?,?,?,?,?)",
        ("SYN-OLD", "SYN-SOURCE", "SYN-TAX", "SYN-CAT", 10000, "manual",
         ASSIGNMENT_TIME_0, ASSIGNMENT_TIME_0))
    sql(f["candidate"], "INSERT INTO activity_taxonomy_assignments VALUES (?,?,?,?,?,?,?,?)",
        ("SYN-NEW", "SYN-SURVIVOR", "SYN-TAX", "SYN-CAT", 10000, "manual",
         ASSIGNMENT_TIME_1, ASSIGNMENT_TIME_1))
    plan = deepcopy(f["plan"])
    source = bp._camel(bp._rows(bp.sqlite_state(f["original"]), "activity_taxonomy_assignments")[0])
    plan["preconditions"]["dependentAssignments"]["SYN-SOURCE"] = [source]
    plan["operations"]["assignmentUpserts"] = [{
        "activityId": "SYN-SURVIVOR", "taxonomyId": "SYN-TAX", "categoryId": "SYN-CAT",
        "expectedSource": "manual", "expectedWeight": 10000, "before": None,
    }]
    plan["counts"]["assignmentUpserts"] = 1
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    changes = bp.validate_bounded_diff(bp.sqlite_state(f["original"]),
                                      bp.sqlite_state(f["candidate"]), plan)
    assert sum(c["table"] == "activity_taxonomy_assignments" for c in changes) == 2
    row = bp._rows(bp.sqlite_state(f["candidate"]), "activity_taxonomy_assignments")[0]
    sql(f["candidate"], "INSERT INTO sync_outbox VALUES (" + ",".join(["?"] * 15) + ")",
        ("SYN-ASSIGN-EVENT", "activity_taxonomy_assignment", "SYN-NEW", "create", "t1",
         json.dumps(row), 1, 0, "pending", 0, None, None, None, None, "t1"))
    sql(f["candidate"], "INSERT INTO sync_entity_metadata VALUES (?,?,?,?,?,?)",
        ("activity_taxonomy_assignment", "SYN-NEW", "SYN-ASSIGN-EVENT", "t1", "create", 0))
    bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)
    sql(f["candidate"], "UPDATE sync_outbox SET payload=?", (json.dumps(bp._camel(row)),))
    with pytest.raises(bp.PromotionError, match="sync row payload differs"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)
    sql(f["candidate"], "UPDATE sync_outbox SET payload=?,op='update'", (json.dumps(row),))
    sql(f["candidate"], "UPDATE sync_entity_metadata SET last_op='update'")
    with pytest.raises(bp.PromotionError, match="planned operation"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)
    sql(f["candidate"], "UPDATE sync_outbox SET op='create'")
    sql(f["candidate"], "UPDATE sync_entity_metadata SET last_op='create'")
    sql(f["candidate"], "DELETE FROM sync_entity_metadata")
    with pytest.raises(bp.PromotionError, match="metadata counterparts"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)


@pytest.mark.parametrize("failure", ["restart", "postcondition"])
def test_caught_failures_restore_original_and_preserve_rejected_candidate(fixture, failure):
    f = fixture
    if failure == "restart":
        f["runtime"].start_failure = True
    elif failure == "postcondition":
        f["runtime"].post_failure = True
    with pytest.raises(bp.PromotionError):
        execute(f)
    slots = bp._slots(f["root"], f["runtime"].target)
    assert f["runtime"].running
    assert bp.sqlite_state(f["live"])["stateHash"] == f["document"]["originalStateHash"]
    assert slots["rejected"].exists()
    assert bp.load(slots["journal"])["phase"] == "rolled-back"


NEW_USER_TRANSACTION = (
    "INSERT INTO activities SELECT 'SYN-NEW-USER',account_id,'DEPOSIT',activity_date,'5',currency,"
    "'user:synthetic:new',NULL,NULL,NULL,'{}','User accepted transaction',updated_at,1 "
    "FROM activities WHERE id='SYN-RECON'"
)


@pytest.mark.parametrize("statement", [
    "UPDATE app_settings SET value='new user setting'",
    "UPDATE activities SET notes='New user note',is_user_modified=1 WHERE id='SYN-SURVIVOR'",
    "UPDATE activities SET amount='91' WHERE id='SYN-RECON'",
    NEW_USER_TRANSACTION,
    "INSERT INTO activity_taxonomy_assignments VALUES "
    "('SYN-USER-ASSIGN','SYN-SURVIVOR','SYN-TAX','SYN-USER-CATEGORY',10000,'manual',"
    "'2026-01-20T00:00:00Z','2026-01-20T01:00:00Z')",
])
def test_user_changes_accepted_after_restart_block_automatic_rollback(fixture, statement):
    f = fixture
    recorded = {}
    def edit(path):
        sql(path, statement)
        recorded["hash"] = bp.sqlite_state(path)["stateHash"]
    f["runtime"].start_edit = edit
    with pytest.raises(bp.ManualRecoveryRequired):
        execute(f)
    slots = bp._slots(f["root"], f["runtime"].target)
    assert f["runtime"].running
    assert f["runtime"].stops == 1
    assert bp.sqlite_state(f["live"])["stateHash"] == recorded["hash"]
    assert bp._hash(slots["original"]) == bp._hash(f["original"])
    assert not slots["rejected"].exists()
    journal = bp.load(slots["journal"])
    assert journal["phase"] == "manual-recovery-required"
    assert journal["manualRecovery"]["boundary"] == "before-stop"


def test_user_edit_during_rollback_stop_is_rechecked_before_any_rename(fixture, monkeypatch):
    f = fixture
    f["runtime"].post_failure = True
    stop = f["runtime"].stop
    def late_edit(expected):
        stop(expected)
        if f["runtime"].stops == 2:
            sql(f["live"], "UPDATE app_settings SET value='accepted just before shutdown'")
    monkeypatch.setattr(f["runtime"], "stop", late_edit)
    with pytest.raises(bp.ManualRecoveryRequired):
        execute(f)
    slots = bp._slots(f["root"], f["runtime"].target)
    assert not f["runtime"].running
    assert bp._rows(bp.sqlite_state(f["live"]), "app_settings")[0]["value"] == "accepted just before shutdown"
    assert bp._hash(slots["original"]) == bp._hash(f["original"])
    assert not slots["rejected"].exists()
    assert bp.load(slots["journal"])["manualRecovery"]["boundary"] == "after-stop"


def test_rollback_does_not_reuse_an_earlier_successful_post_verification(fixture, monkeypatch):
    f = fixture
    writer_state = f["runtime"].writer_state
    calls = 0
    def late_failure(tasks):
        nonlocal calls
        calls += 1
        if calls == 3:
            sql(f["live"], "UPDATE app_settings SET value='accepted after post-verification'")
            raise bp.PromotionError("synthetic final precondition failed")
        return writer_state(tasks)
    monkeypatch.setattr(f["runtime"], "writer_state", late_failure)
    with pytest.raises(bp.ManualRecoveryRequired):
        execute(f)
    assert calls == 3
    assert f["runtime"].running
    assert f["runtime"].stops == 1
    assert bp._rows(bp.sqlite_state(f["live"]), "app_settings")[0]["value"] == "accepted after post-verification"
    assert bp._slots(f["root"], f["runtime"].target)["original"].exists()


def test_user_edit_after_rollback_snapshot_is_detected_by_file_fence(fixture, monkeypatch):
    f = fixture
    f["runtime"].post_failure = True
    fence = bp.exclusive_database
    @contextmanager
    def late_edit(path):
        if path == f["live"] and f["runtime"].stops == 2:
            sql(path, "UPDATE app_settings SET value='accepted before file fence'")
        with fence(path) as digest:
            yield digest
    monkeypatch.setattr(bp, "exclusive_database", late_edit)
    with pytest.raises(bp.ManualRecoveryRequired):
        execute(f)
    slots = bp._slots(f["root"], f["runtime"].target)
    assert bp._rows(bp.sqlite_state(f["live"]), "app_settings")[0]["value"] == "accepted before file fence"
    assert bp._hash(slots["original"]) == bp._hash(f["original"])
    assert not slots["rejected"].exists()
    assert bp.load(slots["journal"])["manualRecovery"]["boundary"] == "file-fence"


@pytest.mark.parametrize("phase", ["stop-intent", "original-move-intent",
                                  "candidate-install-intent", "candidate-installed"])
def test_interrupted_journal_explicit_rollback_recovery(fixture, monkeypatch, phase):
    f = fixture
    transition = bp._transition
    class Interrupted(BaseException):
        pass
    def interrupt(slots, state, next_phase, key, **evidence):
        transition(slots, state, next_phase, key, **evidence)
        if next_phase == phase:
            raise Interrupted()
    monkeypatch.setattr(bp, "_transition", interrupt)
    with pytest.raises(Interrupted):
        execute(f)
    monkeypatch.setattr(bp, "_transition", transition)
    assert recover(f)["phase"] == "rolled-back"
    assert f["runtime"].running
    assert bp.sqlite_state(f["live"])["stateHash"] == f["document"]["originalStateHash"]


@pytest.mark.parametrize("slot", ["original", "live"])
def test_interruption_after_rename_before_next_journal_write_recovers(fixture, monkeypatch, slot):
    f = fixture
    rename = bp._rename
    slots = bp._slots(f["root"], f["runtime"].target)
    class Interrupted(BaseException):
        pass
    def crash_after_move(source, destination):
        rename(source, destination)
        if destination == slots[slot]:
            raise Interrupted()
    monkeypatch.setattr(bp, "_rename", crash_after_move)
    with pytest.raises(Interrupted):
        execute(f)
    monkeypatch.setattr(bp, "_rename", rename)
    assert recover(f)["phase"] == "rolled-back"
    assert f["runtime"].running
    assert bp.sqlite_state(f["live"])["stateHash"] == f["document"]["originalStateHash"]


@pytest.mark.parametrize("statement", [
    "UPDATE activities SET notes='Accepted after the interrupted release',is_user_modified=1 "
    "WHERE id='SYN-SURVIVOR'",
    NEW_USER_TRANSACTION,
])
def test_expired_old_journal_cannot_erase_work_after_candidate_served(fixture, monkeypatch, statement):
    f = fixture
    transition = bp._transition
    class Interrupted(BaseException):
        pass
    def lose_final_receipt(slots, state, phase, key, **kw):
        if phase == "completed":
            raise Interrupted()
        transition(slots, state, phase, key, **kw)
    monkeypatch.setattr(bp, "_transition", lose_final_receipt)
    with pytest.raises(Interrupted):
        execute(f)
    monkeypatch.setattr(bp, "_transition", transition)
    assert f["runtime"].running
    sql(f["live"], statement)
    current_hash = bp.sqlite_state(f["live"])["stateHash"]
    slots = bp._slots(f["root"], f["runtime"].target)
    journal = bp.unseal(bp.load(slots["journal"]), EVIDENCE_KEY, bp.KIND + "-execution")
    authorization = bp.unseal(journal["authorization"], OPERATOR_KEY, bp.KIND + "-authorization")
    authorization.update(issuedAt="2020-01-01T00:00:00+00:00", expiresAt="2020-01-01T00:10:00+00:00")
    journal["authorization"] = bp.seal(authorization, OPERATOR_KEY)
    save(slots["journal"], bp.seal(journal, EVIDENCE_KEY))
    stops = f["runtime"].stops
    with pytest.raises(bp.ManualRecoveryRequired):
        recover(f)
    assert f["runtime"].running
    assert f["runtime"].stops == stops
    assert bp.sqlite_state(f["live"])["stateHash"] == current_hash
    assert bp._hash(slots["original"]) == bp._hash(f["original"])
    assert not slots["rejected"].exists()
    with pytest.raises(bp.PromotionError, match="latched"):
        recover(f)
    assert f["runtime"].stops == stops


def test_old_interrupted_rollback_preserves_work_after_original_served(fixture, monkeypatch):
    f = fixture
    f["runtime"].post_failure = True
    start = f["runtime"].start
    class Interrupted(BaseException):
        pass
    def lose_original_restart_receipt(expected):
        start(expected)
        if f["runtime"].starts == 2:
            raise Interrupted()
    monkeypatch.setattr(f["runtime"], "start", lose_original_restart_receipt)
    with pytest.raises(Interrupted):
        execute(f)
    monkeypatch.setattr(f["runtime"], "start", start)
    assert f["runtime"].running
    sql(f["live"], NEW_USER_TRANSACTION)
    current_hash = bp.sqlite_state(f["live"])["stateHash"]
    slots = bp._slots(f["root"], f["runtime"].target)
    rejected_hash = bp._hash(slots["rejected"])
    stops = f["runtime"].stops
    with pytest.raises(bp.ManualRecoveryRequired):
        recover(f)
    assert f["runtime"].running
    assert f["runtime"].stops == stops
    assert bp.sqlite_state(f["live"])["stateHash"] == current_hash
    assert bp._hash(slots["rejected"]) == rejected_hash
    assert not slots["original"].exists()


def test_interrupted_original_restart_with_approved_timestamps_is_recoverable(fixture, monkeypatch):
    f = fixture
    realistic_candidate_delta(f)
    refresh_fixture(f)
    f["runtime"].post_failure = True
    start = f["runtime"].start
    class Interrupted(BaseException):
        pass
    def interrupt_second_start(expected):
        start(expected)
        if f["runtime"].starts == 2:
            refresh_times(f["live"])
            raise Interrupted()
    monkeypatch.setattr(f["runtime"], "start", interrupt_second_start)
    with pytest.raises(Interrupted):
        execute(f)
    monkeypatch.setattr(f["runtime"], "start", start)
    time.sleep(2.1)
    result = recover(f)
    assert result["phase"] == "rolled-back"
    assert f["runtime"].running
    assert result["resumedOriginalVerification"]["timestampDiff"]
    assert result["rollbackVerification"]["referenceStateHash"] == \
        result["resumedOriginalVerification"]["actualStateHash"]


def test_recovery_rejects_changed_original_authorization(fixture, monkeypatch):
    f = fixture
    transition = bp._transition
    class Interrupted(BaseException):
        pass
    def interrupt(slots, state, phase, key, **kw):
        transition(slots, state, phase, key, **kw)
        if phase == "candidate-installed":
            raise Interrupted()
    monkeypatch.setattr(bp, "_transition", interrupt)
    with pytest.raises(Interrupted):
        execute(f)
    monkeypatch.setattr(bp, "_transition", transition)
    slots = bp._slots(f["root"], f["runtime"].target)
    journal = bp.unseal(bp.load(slots["journal"]), EVIDENCE_KEY, bp.KIND + "-execution")
    journal["authorization"]["signature"] = "0" * 64
    save(slots["journal"], bp.seal(journal, EVIDENCE_KEY))
    stops = f["runtime"].stops
    with pytest.raises(bp.PromotionError):
        recover(f)
    assert f["runtime"].stops == stops


def test_recovery_rejects_unknown_signed_phase_without_touching_runtime(fixture):
    f = fixture
    slots = bp._slots(f["root"], f["runtime"].target)
    state = bp.seal({
        "kind": bp.KIND + "-execution", "schemaVersion": 1,
        "preparationId": f["document"]["documentHash"], "preparation": f["document"],
        "authorization": f["authorization"], "target": f["runtime"].target,
        "phase": "unknown", "history": [{"phase": "unknown", "at": datetime.now(timezone.utc).isoformat()}],
    }, EVIDENCE_KEY)
    save(slots["journal"], state)
    with pytest.raises(bp.PromotionError, match="phase/history"):
        recover(f)
    assert f["runtime"].stops == 0


def test_legacy_schema_one_is_not_promotable(fixture):
    plan = deepcopy(fixture["plan"])
    plan["schemaVersion"] = 1
    plan["evidence"].pop("identityPolicyDocument")
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    receipt_repair.validate_plan(plan)
    with pytest.raises(bp.PromotionError, match="legacy"):
        bp.validate_bounded_diff(bp.sqlite_state(fixture["original"]),
                                 bp.sqlite_state(fixture["candidate"]), plan)


def test_schema_two_with_old_policy_is_rejected(fixture):
    plan = deepcopy(fixture["plan"])
    plan["evidence"]["identityPolicyDocument"]["version"] = "v4"
    plan["evidence"]["identityPolicyHash"] = plan_fingerprint(plan["evidence"]["identityPolicyDocument"])
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    with pytest.raises(receipt_repair.ReceiptRepairError):
        bp.validate_bounded_diff(bp.sqlite_state(fixture["original"]),
                                 bp.sqlite_state(fixture["candidate"]), plan)


@pytest.mark.parametrize("stale_binding", [
    "operation-policy", "feature-policy", "policy-version", "description-relation",
    "same-source-day", "authority-interval", "authority-source", "suppressed-source",
    "authority-document",
])
def test_current_policy_wrapper_cannot_relabel_old_operation_evidence(
    fixture, monkeypatch, stale_binding
):
    f = fixture
    plan = deepcopy(f["plan"])
    operation = plan["operations"]["repairs"][0]
    features = operation["featureVector"]
    if stale_binding == "operation-policy":
        operation["authorityPolicyHash"] = "0" * 64
    elif stale_binding == "feature-policy":
        features["authorityPolicyHash"] = "0" * 64
    elif stale_binding == "policy-version":
        features["authorityPolicyVersion"] = "source-authority-v2"
    elif stale_binding == "description-relation":
        features["descriptionRelation"] = "shared-discriminating-token"
    elif stale_binding == "same-source-day":
        features["sameSourceDay"] = "false"
    elif stale_binding == "authority-interval":
        features["authoritativeIntervalId"] = "0" * 64
    elif stale_binding == "authority-source":
        features["authoritativeSourceFamily"] = "csv"
    elif stale_binding == "suppressed-source":
        features["suppressedSourceFamily"] = "other-source"
    else:
        plan["evidence"]["sourceAuthorityHash"] = "0" * 64
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    save(f["root"] / "plan.json", plan)
    monkeypatch.setattr(
        f["runtime"], "check",
        lambda **_: pytest.fail("stale operation evidence reached runtime inspection"),
    )
    with pytest.raises(receipt_repair.ReceiptRepairError):
        bp.prepare(root=f["root"], request=f["request"], runtime=f["runtime"],
                   evidence_key=EVIDENCE_KEY)
    assert f["runtime"].stops == 0


def test_explicit_parent_source_subset_keeps_the_same_bounded_contract(fixture):
    f = fixture
    plan = deepcopy(f["plan"])
    plan["scope"]["sourceActivityIds"] = ["SYN-SOURCE"]
    plan["planHash"] = plan_fingerprint({k: v for k, v in plan.items() if k != "planHash"})
    assert bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), plan)


def test_source_unknown_populated_field_cannot_be_lost(fixture):
    for path in (fixture["original"], fixture["candidate"]):
        sql(path, "ALTER TABLE activities ADD COLUMN future_user_value TEXT")
    sql(fixture["original"], "UPDATE activities SET future_user_value='user work' WHERE id='SYN-SOURCE'")
    with pytest.raises(bp.PromotionError, match="unknown populated"):
        bp.validate_bounded_diff(bp.sqlite_state(fixture["original"]),
                                 bp.sqlite_state(fixture["candidate"]), fixture["plan"])


def test_scoped_calculated_cash_addition_and_matching_sync_delete(fixture):
    f = fixture
    sql(f["candidate"], "INSERT INTO holdings_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("SYN-SNAPSHOT", "SYN-ACCOUNT", "2026-01-16", "USD", "{}", '{"USD":"80"}',
         "0", "80", "2026-01-20", "80", "80", "80", "CALCULATED"))
    sql(f["candidate"], "INSERT INTO sync_outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("SYN-EVENT", "activity", "SYN-SOURCE", "delete", "t1", '{"id":"SYN-SOURCE"}',
         1, 0, "pending", 0, None, None, None, None, "t1"))
    sql(f["candidate"], "INSERT INTO sync_entity_metadata VALUES (?,?,?,?,?,?)",
        ("activity", "SYN-SOURCE", "SYN-EVENT", "t1", "delete", 0))
    diff = bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), f["plan"])
    assert {r["table"] for r in diff} == {
        "activities", "holdings_snapshots", "sync_outbox", "sync_entity_metadata"
    }


@pytest.mark.parametrize("account,source", [("SYN-OTHER", "CALCULATED"), ("SYN-ACCOUNT", "MANUAL_ENTRY")])
def test_cross_account_daily_and_manual_snapshots_rejected(fixture, account, source):
    f = fixture
    sql(f["candidate"], "INSERT INTO holdings_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("SYN-SNAPSHOT", account, "2026-01-16", "USD", "{}", '{"USD":"80"}',
         "0", "80", "2026-01-20", "80", "80", "80", source))
    with pytest.raises(bp.PromotionError, match="scoped calculated"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), f["plan"])


@pytest.mark.parametrize("entity,payload", [("quote", '{"id":"SYN-SOURCE"}'), ("activity", '{"id":"wrong"}')])
def test_unrelated_sync_events_rejected(fixture, entity, payload):
    f = fixture
    sql(f["candidate"], "INSERT INTO sync_outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("SYN-EVENT", entity, "SYN-SOURCE", "delete", "t1", payload,
         1, 0, "pending", 0, None, None, None, None, "t1"))
    with pytest.raises(bp.PromotionError, match="sync"):
        bp.validate_bounded_diff(bp.sqlite_state(f["original"]), bp.sqlite_state(f["candidate"]), f["plan"])


@pytest.mark.parametrize("bad", ["container", "path", "image", "origin", "peer"])
def test_actual_docker_adapter_rejects_wrong_target_without_stop(fixture, bad):
    f = fixture
    target_spec = f["runtime"].target
    row = {
        "Id": target_spec["containerId"], "Image": target_spec["imageId"],
        "Name": "/" + target_spec["containerName"], "State": {"Running": True},
        "Config": {"Env": ["WF_AUTH_PASSWORD_HASH=synthetic-argon2-hash"],
                   "Labels": {"com.docker.compose.project": target_spec["composeProject"],
                              "com.docker.compose.service": target_spec["composeService"]}},
        "HostConfig": {"AutoRemove": False, "PortBindings": {
            "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8088"}]}},
        "Mounts": [{"Type": "bind", "RW": True, "Source": str(f["root"]), "Destination": "/data"}],
    }
    # Authorized filename maps to /data/wealthfolio.db for the real adapter.
    target_spec = {**target_spec, "database": str(f["root"] / "wealthfolio.db")}
    if bad == "container":
        row["Name"] = "/other"
    elif bad == "path":
        row["Mounts"][0]["Source"] = str(f["root"] / "wrong")
    elif bad == "image":
        row["Image"] = "sha256:wrong"
    elif bad == "origin":
        row["HostConfig"]["PortBindings"]["8080/tcp"][0]["HostIp"] = "0.0.0.0"
    runtime = bp.GuardedDockerRuntime(target_spec, f["root"], "synthetic-password")
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["context", "inspect"]:
            output = json.dumps([{"Endpoints": {"docker": {"Host": "npipe:////./pipe/synthetic"}}}])
        elif command[1:3] == ["ps", "-q"]:
            output = "peer" if bad == "peer" else ""
        elif command[-1] == "peer":
            output = json.dumps([{"Id": "peer", "Mounts": row["Mounts"]}])
        else:
            output = json.dumps([row])
        return subprocess.CompletedProcess(command, 0, output, "")
    runtime._runner = runner
    with pytest.raises(Exception):
        runtime.check(production=True, running=True)
    assert not any("stop" in command for command in commands)


def test_windows_fence_rejects_active_sqlite_writer(fixture):
    connection = sqlite3.connect(fixture["live"])
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(bp.PromotionError):
            with bp.exclusive_database(fixture["live"]):
                pytest.fail("active writer was not fenced")
    finally:
        connection.close()


def test_cli_inspect_requires_no_password_and_no_runtime(fixture, monkeypatch, capsys):
    f = fixture
    save(f["root"] / "prepared.json", f["document"])
    monkeypatch.setenv(bp.EVIDENCE_KEY_ENV, EVIDENCE_KEY.hex())
    monkeypatch.delenv("WEALTHFOLIO_PASSWORD", raising=False)
    assert cli.main(["inspect", "--data-dir", str(f["root"]), "--preparation", "prepared.json"]) == 0
    assert f["document"]["documentHash"] in capsys.readouterr().out
    assert f["runtime"].stops == 0


def test_offline_review_inputs_supply_exact_review_bindings_without_runtime(fixture, monkeypatch):
    f = fixture
    realistic_candidate_delta(f)
    refresh_fixture(f)
    expected = bp.unseal(bp.load(f["root"] / "review.json"), EVIDENCE_KEY, bp.KIND + "-review")
    (f["root"] / "review.json").unlink()
    save(f["root"] / "request.json", f["request"])
    monkeypatch.setenv(bp.EVIDENCE_KEY_ENV, EVIDENCE_KEY.hex())
    monkeypatch.delenv("WEALTHFOLIO_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "_runtime", lambda *_: pytest.fail("offline command used a runtime"))
    original_state = bp.sqlite_state
    def refuse_live(path, **kw):
        assert Path(path) != f["live"]
        return original_state(path, **kw)
    monkeypatch.setattr(bp, "sqlite_state", refuse_live)
    assert cli.main(["review-inputs", "--data-dir", str(f["root"]), "--request", "request.json",
                     "--output", "review-inputs.json"]) == 0
    result = bp.load(f["root"] / "review-inputs.json")
    assert {k: result[k] for k in bp.REVIEW_BINDINGS} == {k: expected[k] for k in bp.REVIEW_BINDINGS}
    assert "signature" not in result
    assert result["runtimeTimestampPolicy"]["rows"]
    assert f["runtime"].stops == 0


def test_actual_authenticated_http_poststate_verification(fixture):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    f = fixture
    requests = []
    state = DatabaseClient({**f["runtime"].target, "database": str(f["candidate"])})
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value, *, login=False):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if login:
                self.send_header("Set-Cookie", "wf_session=synthetic-session; HttpOnly; Path=/")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(self.path)
            if self.path == "/api/v1/auth/login":
                self.send(200 if body == {"password": "synthetic-password"} else 401,
                          {}, login=body == {"password": "synthetic-password"})
            elif self.headers.get("Cookie") != "wf_session=synthetic-session":
                self.send(401, {})
            elif self.path == "/api/v1/activities/search":
                self.send(200, {"data": state.rows})
            elif self.path == "/api/v1/performance/accounts/simple":
                self.send(200, [{"accountId": "SYN-ACCOUNT", "totalValue": 80}])
            else:
                self.send(404, {})

        def do_GET(self):
            requests.append(self.path)
            if self.headers.get("Cookie") != "wf_session=synthetic-session":
                self.send(401, {})
            elif self.path == "/api/v1/healthz":
                self.send(200, "ok")
            else:
                self.send(200, state.get(self.path.removeprefix("/api/v1")))

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        selected = target(f["candidate"], port=server.server_port)
        runtime = bp.GuardedDockerRuntime(selected, f["root"], "synthetic-password")
        assert runtime.wait(f["plan"], expected="applied")["status"] == "applied"
        runtime.password = "incorrect-synthetic-password"
        with pytest.raises(Exception):
            runtime.verify(f["plan"], expected="applied")
        assert "/api/v1/activities/search" in requests
        assert "/api/v1/performance/accounts/simple" in requests
        assert "/api/v1/portfolio/recalculate" not in requests
        assert "/api/v1/activities/bulk" not in requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
