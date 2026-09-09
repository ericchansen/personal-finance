"""Verified, homogeneous production repair history and explicit completed-slot archival."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from importers.simplefin.application import activity_semantic_fingerprint

from . import bounded_promotion as bp
from . import receipt_repair as repair
from .safety import plan_fingerprint

CHAIN_KIND = bp.KIND + "-lineage"
EXTENSION_KIND = bp.KIND + "-continuation"
ARCHIVE_KIND = bp.KIND + "-archive"
ARCHIVE_APPROVAL = "AUTHORIZE_COMPLETED_PROMOTION_ARCHIVE"
REPLAY_GAP_KIND = bp.KIND + "-historical-replay-gap"
REPLAY_GAP_APPROVAL = "ACKNOWLEDGE_ENUMERATED_ANCESTOR_MATCHING_REPLAY_GAPS"
ANCESTOR_CANONICAL_KEYS = frozenset({"canonical", "canonicalTransactions", "canonicalOverlap"})


class HistoricalInputUnavailable(bp.PromotionError):
    """The exact historical bytes, rather than a verified artifact, are unavailable."""


def _gap_descriptor(file: dict, ancestor: dict, binding_key: str, original_root: Path) -> dict:
    return {
        "path": _relative_reference(file["path"], original_root), "sha256": file["sha256"],
        "ancestorPlan": {"path": _relative_reference(ancestor["path"], original_root),
                         "sha256": ancestor["sha256"]},
        "bindingRole": f"schema3-staging-application-plan.evidence.{binding_key}",
    }


def replay_gap_acknowledgment(document: dict, *, root: Path, execution_hash: str,
                              target: dict, operator_key: bytes) -> list[dict]:
    body = bp.unseal(document, operator_key, REPLAY_GAP_KIND)
    bp._require(set(body) == {
        "kind", "schemaVersion", "approval", "executionHash", "targetHash", "verificationScope",
        "operator", "reason", "issuedAt", "missingBindings",
    } and body.get("schemaVersion") == 1 and body.get("approval") == REPLAY_GAP_APPROVAL and
                body.get("executionHash") == execution_hash and
                body.get("targetHash") == plan_fingerprint(target) and
                body.get("verificationScope") == "historical-matching-replay-only" and
                isinstance(body.get("operator"), str) and bool(body["operator"].strip()) and
                isinstance(body.get("reason"), str) and bool(body["reason"].strip()) and
                isinstance(body.get("missingBindings"), list) and bool(body["missingBindings"]),
                "historical replay gap requires an exact operator acknowledgment")
    issued = datetime.fromisoformat(body["issuedAt"].replace("Z", "+00:00"))
    bp._require(issued.tzinfo is not None, "replay gap acknowledgment time must be explicit")
    result = []
    for entry in body["missingBindings"]:
        bp._require(set(entry) == {"path", "sha256", "ancestorPlan", "bindingRole"} and
                    set(entry["ancestorPlan"]) == {"path", "sha256"},
                    "replay gap must enumerate the exact ancestor/path/hash/role")
        key = entry["bindingRole"].removeprefix("schema3-staging-application-plan.evidence.")
        bp._require(key in ANCESTOR_CANONICAL_KEYS and entry["bindingRole"] ==
                    f"schema3-staging-application-plan.evidence.{key}" and
                    all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                        for value in (entry["sha256"], entry["ancestorPlan"]["sha256"])),
                    "replay gap role or digest is not supported")
        normalized = _gap_descriptor(entry, entry["ancestorPlan"], key, root)
        bp._require(normalized["path"].casefold() ==
                    str(Path("normalized") / "canonical" / "transactions.csv").casefold(),
                    "only the old canonical-overlap CSV is eligible for a replay gap")
        result.append(normalized)
    result.sort(key=plan_fingerprint)
    bp._require(len({plan_fingerprint(row) for row in result}) == len(result),
                "replay gap acknowledgment repeats a binding")
    return result


def qualification_fields(value: dict) -> dict:
    if value.get("historicalMatchingReplayAvailable") is not False:
        return {}
    body = {key: value[key] for key in
            ("historicalMatchingReplayAvailable", "missingBindings", "gapAcknowledgments")}
    bp._require(bool(body["missingBindings"]) and bool(body["gapAcknowledgments"]),
                "historical replay qualification is empty")
    return {**body, "historicalReplayQualificationHash": plan_fingerprint(body)}


def keys(evidence_key: bytes | None = None, operator_key: bytes | None = None) -> tuple[bytes, bytes]:
    result = []
    for supplied, name in ((evidence_key, bp.EVIDENCE_KEY_ENV), (operator_key, bp.OPERATOR_KEY_ENV)):
        value = supplied if supplied is not None else bytes.fromhex(os.environ.get(name, ""))
        bp._require(len(value) >= 32, "production lineage requires both verification keys")
        result.append(value)
    return tuple(result)


def paths(root: Path, target: dict) -> dict[str, Path]:
    slots = bp._slots(root, target)
    return {**slots, "head": slots["live"].with_name(slots["live"].name + ".bounded-history.json"),
            "origin": slots["live"].with_name(slots["live"].name + ".bounded-history-origin.json"),
            "archiving": slots["live"].with_name(slots["live"].name + ".bounded-archive.json")}


def _replay(state: dict, changes: list[dict]) -> dict:
    result = copy.deepcopy(state)
    for change in changes:
        table = result["tables"][change["table"]]
        pk = table["primaryKey"]
        bp._require(bool(pk), "history delta requires a declared primary key")
        indexed = {plan_fingerprint([row[k] for k in pk]): row for row in table["rows"]}
        key = change["key"]
        bp._require(indexed.get(key) == change["before"] and
                    table["rowids"].get(key) == change["beforeRowId"], "history delta preimage differs")
        if change["after"] is None:
            indexed.pop(key)
            table["rowids"].pop(key, None)
        else:
            bp._require(plan_fingerprint([change["after"][k] for k in pk]) == key,
                        "history delta changes primary identity")
            indexed[key] = change["after"]
            if change["afterRowId"] is not None:
                table["rowids"][key] = change["afterRowId"]
        table["rows"] = sorted(indexed.values(), key=lambda row: json.dumps(row, sort_keys=True))
    result["stateHash"] = plan_fingerprint({k: v for k, v in result.items() if k != "stateHash"})
    return result


def completed_execution(document: dict, *, root: Path, evidence_key: bytes, operator_key: bytes) -> dict:
    """Verify historical artifacts without reinterpreting a newer canonical publication."""
    execution = bp.unseal(document, evidence_key, bp.KIND + "-execution")
    history = execution.get("history") or []
    phases = [row.get("phase") for row in history]
    required = ["authorized", "candidate-staged", "stop-intent", "original-move-intent",
                "candidate-install-intent", "candidate-installed", "completed"]
    bp._require(execution.get("schemaVersion") == 1 and execution.get("phase") == "completed"
                and phases and phases[-1] == "completed" and not execution.get("freshnessRejected")
                and set(phases) <= set(required), "only an unambiguous completed production execution qualifies")
    cursor = 0
    for phase in phases:
        if cursor < len(required) and phase == required[cursor]:
            cursor += 1
    bp._require(cursor == len(required), "completed execution has an incomplete phase history")
    times = [bp._timestamp(row["at"]) for row in history]
    bp._require(times == sorted(times), "completed execution chronology is uncertain")
    preparation_document = execution["preparation"]
    preparation = bp.unseal(preparation_document, evidence_key, bp.KIND + "-preparation")
    bp._require(execution["preparationId"] == preparation_document["documentHash"] and
                execution["target"] == preparation["target"] and
                preparation["targetHash"] == plan_fingerprint(execution["target"]) and
                preparation.get("writerActivation") is False, "production execution target/preparation differs")
    bp._origin(execution["target"]["origin"], production=True)
    bp.validate_authorization(execution["authorization"], preparation, execution["preparationId"],
                              operator_key=operator_key, rollback_only=True)
    authorization = bp.unseal(execution["authorization"], operator_key, bp.KIND + "-authorization")
    moved_at = bp._timestamp(history[phases.index("original-move-intent")]["at"])
    bp._require(bp._timestamp(authorization["issuedAt"]) <= moved_at <
                bp._timestamp(authorization["expiresAt"]), "production swap was outside its authorization window")
    for evidence in preparation["files"].values():
        bp.check_file(evidence, root)
    plan = repair.load_plan(bp.check_file(preparation["files"]["plan"], root))
    bp._require(plan["schemaVersion"] == 2 and preparation["planHash"] == plan["planHash"],
                "historical policy or preparation plan differs")
    receipt = bp.load(bp.check_file(preparation["files"]["receipt"], root))
    recovery = bp.load(bp.check_file(preparation["files"]["recovery"], root))
    repair._validate_applied_receipt(
        receipt, plan, expected_instance_id=preparation["request"]["appliedCloneInstanceId"],
        data_dir=root, recovery=recovery,
    )
    bp._require(receipt.get("preLedgerFingerprint") == plan["preconditions"]["globalLedgerFingerprint"],
                "historical receipt prestate differs")
    review = bp.unseal(bp.load(bp.check_file(preparation["files"]["review"], root)),
                       evidence_key, bp.KIND + "-review")
    for name in ("planHash", "diffHash", "releaseRevision", "targetHash", "canonicalEvidenceHash"):
        bp._require(review.get(name) == preparation.get(name), "historical review binding differs")
    bp._require(all(review.get(key) == preparation.get(key) == value
                    for key, value in bp.replay_review_fields(plan).items()),
                "completed repair did not explicitly carry its historical replay qualification")
    bp._require(preparation["diffHash"] == plan_fingerprint(preparation["diff"]) and
                preparation["canonicalEvidenceHash"] == plan_fingerprint(preparation["canonicalEvidence"]) and
                preparation["evidenceHash"] == plan_fingerprint(plan["evidence"]) == review["evidenceHash"] and
                review["runtimePolicyHash"] == plan_fingerprint(preparation["runtimeTimestampPolicy"]),
                "historical preservation/evidence hash differs")
    states, restored = {}, {}
    for name in ("original", "candidate"):
        backup = preparation["backups"][name]
        path = bp.check_file(backup, root)
        actual = bp.backup_evidence(path, root)
        bp._require(actual == backup, "historical backup evidence differs")
        states[name] = bp.sqlite_state(path, stopped=True)
        bp._require(states[name]["stateHash"] == preparation[name + "StateHash"],
                    "historical backup logical state differs")
        proof_document = preparation["restoreProofs"][name]
        bp._require(proof_document == bp.load(bp.check_file(preparation["files"][name + "RestoreProof"], root)),
                    "historical restore proof file/embedding differs")
        proof = bp.unseal(proof_document, evidence_key, bp.KIND + "-restore")
        bp._origin(proof["target"]["origin"], production=False)
        stage = proof["stage"]
        bp._require(proof["backup"] == backup and proof["planHash"] == plan["planHash"] and
                    stage["stageHash"] == plan_fingerprint({k: v for k, v in stage.items() if k != "stageHash"})
                    and stage["sourceSha256"] == stage["stagedSha256"] == backup["sha256"]
                    and proof["backupStateHash"] == states[name]["stateHash"]
                    and proof["environmentDiffHash"] == plan_fingerprint(proof["environmentDiff"])
                    and review["restoreDiffHashes"][name] == proof["environmentDiffHash"]
                    and proof["target"]["imageId"] == execution["target"]["imageId"]
                    and proof["target"]["containerId"] != execution["target"]["containerId"]
                    and proof["api"]["instanceId"] == proof["target"]["instanceId"]
                    and proof["api"]["status"] == ("ready" if name == "original" else "applied")
                    and proof["api"]["ledgerHash"] ==
                    plan["preconditions" if name == "original" else "expected"]["globalLedgerFingerprint"],
                    "historical restore proof differs")
        restored[name] = _replay(states[name], proof["environmentDiff"])
        bp._require(restored[name]["stateHash"] == proof["stateHash"] and
                    bp.environment_diff(states[name], restored[name], plan) == proof["environmentDiff"],
                    "historical restore state cannot be reconstructed")
    bp._require(bp.validate_bounded_diff(states["original"], states["candidate"], plan) ==
                preparation["diff"], "historical repair diff is not bounded")
    recovered = bp.sqlite_state(bp.check_file(recovery["backup"], root), stopped=True)
    prestate_diff = bp.environment_diff(states["original"], recovered, plan)
    bp._require(prestate_diff == preparation["clonePrestateDiff"] and
                plan_fingerprint(prestate_diff) == preparation["clonePrestateDiffHash"] ==
                review["clonePrestateDiffHash"], "historical recovery prestate differs")
    for name, field in (("original", "accountValueBefore"), ("candidate", "accountValueAfter")):
        value = sum((repair._signed(bp._camel(row)) for row in bp._rows(states[name], "activities")
                     if row["account_id"] == plan["scope"]["ledgerAccountId"]), Decimal())
        bp._require(receipt["accountValueBasis"] == "cash-ledger" and Decimal(receipt[field]) == value,
                    "production cash-value receipt differs from its actual SQLite state")
    verification = execution["databaseVerification"]
    refs = [states["candidate"], restored["candidate"]]
    reference = next((row for row in refs if row["stateHash"] == verification["referenceStateHash"]), None)
    bp._require(reference is not None, "completed production poststate has no approved reference")
    completed = _replay(reference, verification["timestampDiff"])
    bp._require(completed["stateHash"] == verification["actualStateHash"] and
                plan_fingerprint(verification["timestampDiff"]) == verification["timestampDiffHash"],
                "completed production database verification differs")
    checked = bp._timestamp(verification.get("checkedAt") or history[-1]["at"])
    bp.verify_runtime_state(completed, refs, plan, preparation["runtimeTimestampPolicy"],
                            started_at=bp._timestamp(history[phases.index("candidate-installed")]["at"]),
                            checked_at=checked)
    api = execution["postVerification"]
    bp._require(api.get("instanceId") == execution["target"]["instanceId"] and
                api.get("status") == "applied" and
                api.get("ledgerHash") == plan["expected"]["globalLedgerFingerprint"],
                "completed execution lacks authenticated production postconditions")
    return {"execution": execution, "preparation": preparation, "plan": plan, "receipt": receipt,
            "completedState": completed}


def _history(root: Path, target: dict, evidence_key: bytes) -> dict:
    slots = paths(root, target)
    path = slots["head"]
    if not path.exists():
        bp._require(not slots["origin"].exists(), "production history index is missing")
        return {"kind": ARCHIVE_KIND + "-history", "schemaVersion": 1,
                "targetHash": plan_fingerprint(target), "archives": []}
    body = bp.unseal(bp.load(path), evidence_key, ARCHIVE_KIND + "-history")
    bp._require(body["schemaVersion"] == 1 and body["targetHash"] == plan_fingerprint(target),
                "production archive history target differs")
    if slots["origin"].exists():
        origin = bp.unseal(bp.load(slots["origin"]), evidence_key, ARCHIVE_KIND + "-origin")
        bp._require(origin["targetHash"] == body["targetHash"] and body["archives"] and
                    body["archives"][0] == origin["firstArchive"], "production history origin/prefix differs")
    else:
        bp._require(slots["archiving"].exists(), "production history origin guard is missing")
    return body


def _archive_record(evidence: dict, root: Path, evidence_key: bytes, operator_key: bytes) -> dict:
    receipt_path = bp.check_file(evidence, root)
    document = bp.load(receipt_path)
    receipt = bp.unseal(document, evidence_key, ARCHIVE_KIND + "-receipt")
    if receipt.get("schemaVersion") in {2, 3}:
        return _archive_record_v2(receipt, evidence, root, evidence_key, operator_key)
    bp._require(receipt.get("schemaVersion") == 1 and receipt.get("phase") == "completed",
                "archive is not completed")
    context = bp.private(receipt["evidenceRoot"], root)
    for file in receipt["files"]:
        bp.check_file(file, context)
    original = bp.check_file(receipt["originalSlot"], root)
    execution_document = bp.load(bp.check_file(receipt["execution"], root))
    verified = completed_execution(execution_document, root=context, evidence_key=evidence_key,
                                    operator_key=operator_key)
    bp._require(receipt["executionHash"] == execution_document["documentHash"] and
                bp._hash(original) == verified["execution"]["stoppedOriginalSha256"] and
                receipt["target"] == verified["execution"]["target"],
                "archive original slot or production identity differs")
    _archive_authorization(receipt["authorization"], execution_document["documentHash"],
                           receipt["target"], receipt["archiveDirectory"], operator_key, historical=True)
    bp._require(receipt["finalVerification"]["actualStateHash"] ==
                receipt["archivedLiveStateHash"], "archive closing verification differs")
    previous = receipt["preArchiveVerification"]
    prestate = _replay(verified["completedState"], previous["timestampDiff"])
    bp._require(previous["referenceStateHash"] == verified["completedState"]["stateHash"] and
                prestate["stateHash"] == previous["actualStateHash"] and
                previous["timestampDiffHash"] == plan_fingerprint(previous["timestampDiff"]),
                "archive opening state is not the completed production state")
    bp.verify_runtime_state(
        prestate, [verified["completedState"]], verified["plan"],
        verified["preparation"]["runtimeTimestampPolicy"],
        started_at=bp._timestamp(verified["execution"]["history"][-1]["at"]),
        checked_at=bp._timestamp(previous["checkedAt"]),
    )
    final = receipt["finalVerification"]
    closed = _replay(prestate, final["timestampDiff"])
    bp._require(final["referenceStateHash"] == prestate["stateHash"] and
                closed["stateHash"] == final["actualStateHash"] and
                final["timestampDiffHash"] == plan_fingerprint(final["timestampDiff"]),
                "archive closing state is not reconstructable")
    bp.verify_runtime_state(
        closed, [prestate], verified["plan"], verified["preparation"]["runtimeTimestampPolicy"],
        started_at=bp._timestamp(receipt["restartedAt"]), checked_at=bp._timestamp(final["checkedAt"]),
    )
    bp._require(bp._hash(bp.check_file(receipt["originalJournal"], root)) ==
                receipt["execution"]["sha256"], "original execution journal bytes changed")
    return {**verified, "archive": receipt, "archiveEvidence": evidence, "context": context,
            "originalRoot": Path(receipt["originalDataRoot"]),
            "archivedState": closed,
            "executionHash": execution_document["documentHash"]}


def _validate_application_plan(source: dict) -> None:
    version = source.get("schemaVersion")
    bp._require(version in {1, 3} and source.get("planFingerprint") ==
                plan_fingerprint({k: v for k, v in source.items() if k != "planFingerprint"}),
                "referenced application plan has an unsupported schema or invalid seal")
    if version == 1:
        bp._require(source.get("mode") in {"staging-apply-plan", "production-promotion-plan"} and
                    all(isinstance(source.get(key), expected) for key, expected in {
                        "portableIntent": dict, "ledgerAccountIds": list, "links": list,
                        "manual": list, "monitors": list, "impact": dict, "spendingWindow": dict,
                    }.items()), "historical application plan shape is invalid")


def _anchor(first: dict, root: Path) -> tuple[dict, dict, dict, dict]:
    plan = first["plan"]
    review = bp.load(bp.check_file(first["preparation"]["files"]["review"], first["context"]))
    def artifact(name):
        digest = plan["evidence"][name]
        matches = sorted((file for file in review["independentEvidence"] if file["sha256"] == digest),
                         key=lambda file: file["path"])
        bp._require(bool(matches), "original source application artifact is missing")
        evidence = dict(matches[0])
        declared = Path(evidence["path"])
        if declared.is_absolute():
            original_root = first.get("originalRoot", first["context"])
            evidence["path"] = str(declared.relative_to(original_root))
        path = bp.check_file(evidence, first["context"])
        return bp.load(path), bp.file_evidence(path, root)
    source, source_file = artifact("sourceApplicationPlanSha256")
    receipt, receipt_file = artifact("sourceApplicationReceiptSha256")
    # Historical evidence paths may subsequently move into the archive. Verify
    # the original bytes/seal, not a re-execution of today's importer against them.
    _validate_application_plan(source)
    if source["schemaVersion"] == 1:
        bp._require(receipt.get("schemaVersion") == 3, "historical application receipt schema differs")
    bp._require(receipt.get("status") == "applied" and repair.forensic._receipt_matches_plan(
        {"value": source, "sha256": source_file["sha256"]},
        {"value": receipt, "sha256": receipt_file["sha256"]},
    ), "original source application receipt is not exact/applied")
    return source, receipt, source_file, receipt_file


def build_lineage(*, root: Path, target: dict, evidence_key: bytes, operator_key: bytes) -> dict:
    history = _history(root, target, evidence_key)
    bp._require(bool(history["archives"]), "no completed archived production repairs")
    records = [_archive_record(item, root, evidence_key, operator_key) for item in history["archives"]]
    source, _source_receipt, source_file, receipt_file = _anchor(records[0], root)
    account = records[0]["plan"]["scope"]["ledgerAccountId"]
    reconciliations = [r for r in source["reconciliations"]
                       if r["accountId"] == account and r["action"] == "update"]
    assertions = [r for r in source["assertions"] if r["accountId"] == account]
    bp._require(len(reconciliations) == len(assertions) == 1, "original source account anchor is ambiguous")
    compensation = reconciliations[0]["after"]
    reconciliation_id = reconciliations[0]["activityId"]
    value = Decimal(records[0]["receipt"]["accountValueBefore"])
    creates = {(r["accountId"], r["idempotencyKey"]): r
               for r in source["operations"]["creates"]}
    body = {"kind": CHAIN_KIND, "schemaVersion": 1, "target": target, "targetHash": plan_fingerprint(target),
            "sourcePlan": source_file, "sourceReceipt": receipt_file, "accountId": account,
            "reconciliationId": reconciliation_id, "accountValue": format(value, "f"),
            "steps": [], "headExecutionHash": None, "compensationPayload": compensation, "deleted": []}
    seen_ids, seen_aliases, seen_executions = set(), set(), set()
    for record in records:
        plan, execution = record["plan"], record["execution"]
        previous = body["headExecutionHash"]
        extension = plan.get("productionLineage")
        bp._require(record["executionHash"] not in seen_executions and
                    record["archive"]["previousExecutionHash"] == previous and
                    execution.get("previousExecutionHash") == previous,
                    "production history contains a duplicate, gap or fork")
        if previous is None:
            bp._require(extension is None, "first production step unexpectedly has a predecessor")
        else:
            bp._require(extension is not None and extension["headExecutionHash"] == previous and
                        extension["chainHash"] == bp.seal(body, evidence_key)["documentHash"],
                        "production continuation does not bind the exact preceding chain")
        bp._require(plan["scope"]["ledgerAccountId"] == account and
                    plan["evidence"]["sourceApplicationPlanSha256"] == source_file["sha256"] and
                    plan["evidence"]["sourceApplicationReceiptSha256"] == receipt_file["sha256"] and
                    execution["target"] == target, "production lineage source/account/target differs")
        change = plan["operations"]["reconciliationUpdate"]
        bp._require(change["activityId"] == reconciliation_id and
                    change["beforeFingerprint"] == activity_semantic_fingerprint(compensation) and
                    Decimal(record["receipt"]["accountValueBefore"]) == value ==
                    Decimal(record["receipt"]["accountValueAfter"]),
                    "production compensation/value chain has drifted")
        effect = Decimal()
        for operation in plan["operations"]["repairs"]:
            activity_id = operation["sourceActivityId"]
            payload = operation["sourceRollbackPayload"]
            alias = payload["idempotencyKey"]
            source_create = creates.get((account, alias))
            bp._require(activity_id not in seen_ids and alias not in seen_aliases and
                        payload["accountId"] == account and source_create is not None and
                        repair._activity_core(payload) == repair._activity_core(source_create),
                        "production history repeats or invents a source deletion")
            seen_ids.add(activity_id)
            seen_aliases.add(alias)
            effect += repair._signed(payload)
            body["deleted"].append({"activityId": activity_id, "sourceIdentity": alias})
        bp._require(repair._signed(change["payload"]) == repair._signed(compensation) + effect,
                    "production compensation does not equal only its new deletion effects")
        compensation = change["payload"]
        body["compensationPayload"] = compensation
        body["deleted"] = sorted(body["deleted"], key=lambda row: row["activityId"])
        body["steps"].append({"archive": record["archiveEvidence"], "executionHash": record["executionHash"],
                              "planHash": plan["planHash"]})
        body["headExecutionHash"] = record["executionHash"]
        if record["archive"].get("historicalMatchingReplayAvailable") is False:
            body["schemaVersion"] = 2
            body["historicalMatchingReplayAvailable"] = False
            body.setdefault("missingBindings", []).extend(
                {"executionHash": record["executionHash"], **missing}
                for missing in record["archive"]["missingBindings"]
            )
            body.setdefault("gapAcknowledgments", []).append({
                "executionHash": record["executionHash"],
                "acknowledgmentHash": record["archive"]["historicalReplayGap"]["documentHash"],
            })
            body.update(qualification_fields(body))
        seen_executions.add(record["executionHash"])
    return bp.seal(body, evidence_key)


def extension_for(chain: dict, chain_file: dict) -> dict:
    return {"kind": EXTENSION_KIND, "schemaVersion": chain["schemaVersion"], "file": chain_file,
            "chainHash": chain["documentHash"], "headExecutionHash": chain["headExecutionHash"],
            "targetHash": chain["targetHash"], "accountId": chain["accountId"],
            "sourcePlanSha256": chain["sourcePlan"]["sha256"], "sourceReceiptSha256": chain["sourceReceipt"]["sha256"],
            "reconciliationId": chain["reconciliationId"],
            "compensationFingerprint": activity_semantic_fingerprint(chain["compensationPayload"]),
            "accountValue": chain["accountValue"], "deleted": chain["deleted"], **qualification_fields(chain)}


def load_lineage(path: Path, *, root: Path, evidence_key: bytes, operator_key: bytes) -> tuple[dict, dict]:
    path = bp.private(path, root)
    chain = bp.load(path)
    body = bp.unseal(chain, evidence_key, CHAIN_KIND)
    actual = build_lineage(root=root, target=body["target"], evidence_key=evidence_key, operator_key=operator_key)
    bp._require(actual == chain, "lineage is incomplete, forked, changed or no longer the archive head")
    return chain, extension_for(chain, bp.file_evidence(path, root))


def verify_plan_lineage(plan: dict, root: Path, *, evidence_key=None, operator_key=None) -> dict | None:
    extension = plan.get("productionLineage")
    if extension is None:
        return None
    evidence_key, operator_key = keys(evidence_key, operator_key)
    path = bp.check_file(extension["file"], root)
    chain, expected = load_lineage(path, root=root, evidence_key=evidence_key, operator_key=operator_key)
    bp._require(extension == expected and extension["accountId"] == plan["scope"]["ledgerAccountId"] and
                extension["sourcePlanSha256"] == plan["evidence"]["sourceApplicationPlanSha256"] and
                extension["sourceReceiptSha256"] == plan["evidence"]["sourceApplicationReceiptSha256"] and
                extension["reconciliationId"] == plan["operations"]["reconciliationUpdate"]["activityId"] and
                extension["compensationFingerprint"] ==
                plan["operations"]["reconciliationUpdate"]["beforeFingerprint"],
                "plan continuation binding differs from verified production history")
    prior = {r["activityId"] for r in chain["deleted"]}
    bp._require(not prior & {r["sourceActivityId"] for r in plan["operations"]["repairs"]},
                "plan repeats a prior production deletion")
    return chain


def require_current_head(plan: dict, root: Path, target: dict, evidence_key: bytes) -> None:
    history = _history(root, target, evidence_key)
    slots = paths(root, target)
    bp._require(not slots["archiving"].exists(), "completed-slot archival is still pending")
    extension = plan.get("productionLineage")
    if history["archives"]:
        last = bp.unseal(bp.load(bp.check_file(history["archives"][-1], root)), evidence_key,
                         ARCHIVE_KIND + "-receipt")
        bp._require(extension is not None and extension["targetHash"] == plan_fingerprint(target) and
                    extension["headExecutionHash"] == last["executionHash"],
                    "next production release requires the exact archived history head")
        chain = bp.unseal(bp.load(bp.check_file(extension["file"], root)), evidence_key, CHAIN_KIND)
        bp._require([step["archive"] for step in chain["steps"]] == history["archives"],
                    "archived production history prefix changed")
    else:
        bp._require(extension is None, "continuation has no production archive head")


def _archive_authorization(document, execution_hash, target, destination, operator_key, *, historical=False):
    body = bp.unseal(document, operator_key, ARCHIVE_KIND + "-authorization")
    bp._require(body.get("approval") == ARCHIVE_APPROVAL and body.get("executionHash") == execution_hash and
                body.get("targetHash") == plan_fingerprint(target) and body.get("archiveDirectory") == destination and
                isinstance(body.get("operator"), str) and bool(body["operator"]),
                "archive requires exact execution/target/destination authorization")
    issued, expires = bp._timestamp(body["issuedAt"]), bp._timestamp(body["expiresAt"])
    bp._require(0 < (expires - issued).total_seconds() <= 1800 and
                (historical or issued <= datetime.now(timezone.utc) < expires), "archive authorization expired")


def _validate_next_archive(verified: dict, history: dict, root: Path, evidence_key: bytes, operator_key: bytes,
                           evidence_root: Path | None = None):
    plan = verified["plan"]
    account = plan["scope"]["ledgerAccountId"]
    context = evidence_root or root
    source, _, source_file, receipt_file = _anchor(
        {**verified, "context": context, "originalRoot": root}, root
    )
    if history["archives"]:
        extension = plan.get("productionLineage")
        bp._require(extension is not None, "completed successor lacks a verified lineage")
        chain = bp.load(bp.check_file(extension["file"], context))
        bp.unseal(chain, evidence_key, CHAIN_KIND)
        expected = build_lineage(root=root, target=verified["execution"]["target"],
                                 evidence_key=evidence_key, operator_key=operator_key)
        bp._require(chain == expected and extension["chainHash"] == chain["documentHash"] and
                    extension["headExecutionHash"] == chain["headExecutionHash"],
                    "completed successor does not bind the registered history")
        compensation, value = chain["compensationPayload"], Decimal(chain["accountValue"])
        reconciliation_id = chain["reconciliationId"]
        previous_ids = {r["activityId"] for r in chain["deleted"]}
        previous_aliases = {r["sourceIdentity"] for r in chain["deleted"]}
    else:
        bp._require(plan.get("productionLineage") is None, "initial archive has an unexplained predecessor")
        matches = [r for r in source["reconciliations"] if r["accountId"] == account and r["action"] == "update"]
        assertions = [r for r in source["assertions"] if r["accountId"] == account]
        bp._require(len(matches) == len(assertions) == 1, "original receipt account anchor is ambiguous")
        compensation, reconciliation_id = matches[0]["after"], matches[0]["activityId"]
        value = Decimal(verified["receipt"]["accountValueBefore"])
        previous_ids, previous_aliases = set(), set()
    bp._require(source_file["sha256"] == plan["evidence"]["sourceApplicationPlanSha256"] and
                receipt_file["sha256"] == plan["evidence"]["sourceApplicationReceiptSha256"] and
                Decimal(verified["receipt"]["accountValueBefore"]) == value,
                "completed source/value anchor differs")
    creates = {(r["accountId"], r["idempotencyKey"]): r for r in source["operations"]["creates"]}
    effect = Decimal()
    for row in plan["operations"]["repairs"]:
        payload = row["sourceRollbackPayload"]
        alias = payload["idempotencyKey"]
        original = creates.get((account, alias))
        bp._require(row["sourceActivityId"] not in previous_ids and alias not in previous_aliases and
                    original is not None and repair._activity_core(original) == repair._activity_core(payload),
                    "completed step repeats or invents a source deletion")
        previous_ids.add(row["sourceActivityId"])
        previous_aliases.add(alias)
        effect += repair._signed(payload)
    change = plan["operations"]["reconciliationUpdate"]
    bp._require(change["activityId"] == reconciliation_id and
                change["beforeFingerprint"] == activity_semantic_fingerprint(compensation) and
                repair._signed(change["payload"]) == repair._signed(compensation) + effect,
                "completed compensation is not the next receipt-proven step")


def _relative_reference(value: str, original_root: Path) -> str:
    """Normalize a signed locator lexically, never through today's mutable symlinks."""
    path = Path(value)
    absolute = Path(os.path.abspath(path if path.is_absolute() else original_root / path))
    try:
        return str(absolute.relative_to(Path(os.path.abspath(original_root))))
    except ValueError:
        raise bp.PromotionError("historical evidence locator leaves its original data root") from None


def _not_live(path: Path, live: Path) -> None:
    bp._require(path != live and str(path) not in {str(live) + s for s in ("-wal", "-shm", "-journal")},
                "the live database is not an archival input")
    if path.exists() and live.exists():
        bp._require(not path.samefile(live), "archival input aliases the live database")


def _bound_inputs(document: dict, *, original_root: Path, resolve, evidence_key: bytes,
                  replay_gaps: list[dict] | None = None, missing_bindings: list[dict] | None = None) -> list[dict]:
    """Known file-binding namespaces, not arbitrary hashes from transaction payloads."""
    inventory = {}
    primaries = {}
    approved = {plan_fingerprint(row): row for row in (replay_gaps or [])}
    used = {}
    def add(file: dict, *, primary=False, ancestor=None, binding_key=None) -> Path | None:
        bp._require(isinstance(file.get("path"), str) and bool(file["path"]) and
                    isinstance(file.get("sha256"), str) and
                    re.fullmatch(r"[0-9a-f]{64}", file["sha256"]) is not None,
                    "historical file binding is invalid")
        relative = _relative_reference(file["path"], original_root)
        identity = (relative.casefold(), file["sha256"])
        try:
            resolved = resolve({"path": relative, "sha256": file["sha256"]})
        except HistoricalInputUnavailable:
            if primary or ancestor is None or binding_key not in ANCESTOR_CANONICAL_KEYS:
                raise
            descriptor = _gap_descriptor(file, ancestor, binding_key, original_root)
            digest = plan_fingerprint(descriptor)
            if digest not in approved:
                raise
            used[digest] = descriptor
            return None
        entry = inventory.setdefault(identity, {
            "path": relative, "sha256": file["sha256"], "references": [], "primary": False,
        })
        if file["path"] not in entry["references"]:
            entry["references"].append(file["path"])
        if primary:
            old = primaries.setdefault(relative.casefold(), file["sha256"])
            bp._require(old == file["sha256"], "primary historical files have conflicting versions")
            entry["primary"] = True
        return resolved
    execution = bp.unseal(document, evidence_key, bp.KIND + "-execution")
    preparation = bp.unseal(execution["preparation"], evidence_key, bp.KIND + "-preparation")
    for file in preparation["files"].values():
        add(file, primary=True)
    for file in preparation["backups"].values():
        add(file, primary=True)
    plan = bp.load(add(preparation["files"]["plan"], primary=True))
    review = bp.unseal(bp.load(add(preparation["files"]["review"], primary=True)),
                       evidence_key, bp.KIND + "-review")
    recovery = bp.load(add(preparation["files"]["recovery"], primary=True))
    add(recovery["backup"], primary=True)
    for file in review["independentEvidence"]:
        add(file)
    canonical = preparation["canonicalEvidence"]
    manifest_file = canonical["manifest"]
    manifest = bp.load(add(manifest_file, primary=True))
    manifest_parent = Path(_relative_reference(manifest_file["path"], original_root)).parent
    for name, digest in manifest["dataFiles"].items():
        bp._require(Path(name).name == name, "canonical data path leaves its publication")
        add({"path": str(manifest_parent / name), "sha256": digest}, primary=True)
    for file in manifest["sourceFiles"]:
        add(file, primary=True)
    for file in canonical["rowArtifacts"]:
        add(file)
    visited_applications = set()
    def application_inputs(file: dict, *, staging_ancestor=False) -> None:
        identity = (_relative_reference(file["path"], original_root).casefold(), file["sha256"])
        source_path = add(file)
        if identity in visited_applications:
            return
        visited_applications.add(identity)
        source_plan = bp.load(source_path)
        _validate_application_plan(source_plan)
        evidence = source_plan.get("evidence")
        bp._require(isinstance(evidence, dict), "application plan evidence namespace is invalid")
        for name, binding in evidence.items():
            eligible_ancestor = (staging_ancestor and source_plan["schemaVersion"] == 3
                                 and source_plan.get("mode") == "staging-apply-plan")
            add(binding, ancestor=file if eligible_ancestor else None, binding_key=name)
        manual = evidence.get("manualDecisions")
        referenced_hashes = set()
        if manual is not None:
            manual_document = bp.load(add(manual))
            bp._require(manual_document.get("schemaVersion") == 1 and
                        isinstance(manual_document.get("decisions"), list),
                        "manual decision document schema is unsupported")
            for ruling in manual_document["decisions"]:
                bp._require(isinstance(ruling, dict) and isinstance(ruling.get("evidence"), list),
                            "manual ruling evidence namespace is invalid")
                for nested in ruling["evidence"]:
                    add(nested)
                    referenced_hashes.add(nested["sha256"])
        decision = source_plan.get("decisionEvidence")
        if decision:
            bp._require(isinstance(decision, dict) and
                        set(decision) == {"manualDecisionsSha256", "nestedEvidence", "fingerprint"} and
                        isinstance(decision["nestedEvidence"], list) and
                        decision["fingerprint"] == plan_fingerprint(
                            {k: v for k, v in decision.items() if k != "fingerprint"}
                        ), "application decision-evidence binding is invalid")
            bp._require(isinstance(manual, dict) and
                        manual["sha256"] == decision["manualDecisionsSha256"],
                        "manual decision file is not bound by the application plan")
            for nested in decision["nestedEvidence"]:
                add(nested)
            bp._require(referenced_hashes == {nested["sha256"] for nested in decision["nestedEvidence"]},
                        "nested manual evidence differs from the sealed decision binding")
            portable = source_plan.get("portableIntent")
            if isinstance(portable, dict) and "decisionEvidence" in portable:
                bp._require(portable["decisionEvidence"] == decision,
                            "portable decision evidence differs from its application plan")
        if "stagingApplicationPlan" in evidence:
            # Only this explicit schema-3 plan namespace recurses. A raw evidence
            # document containing arbitrary hashes is never interpreted as a plan.
            application_inputs(evidence["stagingApplicationPlan"], staging_ancestor=True)
    for name in ("sourceApplicationPlanSha256", "sourceApplicationReceiptSha256"):
        matches = [file for file in review["independentEvidence"] if file["sha256"] == plan["evidence"][name]]
        bp._require(bool(matches), "original source application artifact is not declared")
        add(sorted(matches, key=lambda row: row["path"])[0], primary=True)
        if name == "sourceApplicationPlanSha256":
            application_inputs(sorted(matches, key=lambda row: row["path"])[0])
    for entry in inventory.values():
        entry["references"].sort()
    bp._require(used.keys() == approved.keys(),
                "replay gap acknowledgment contains an unused, available or non-ancestor binding")
    if missing_bindings is not None:
        missing_bindings.extend(sorted(used.values(), key=plan_fingerprint))
    return sorted(inventory.values(), key=lambda row: (row["path"].casefold(), row["sha256"]))


def _prior_inputs(history: dict, root: Path, evidence_key: bytes, operator_key: bytes) -> dict:
    result = {}
    for reference in history["archives"]:
        record = _archive_record(reference, root, evidence_key, operator_key)
        archive = record["archive"]
        if archive["schemaVersion"] in {2, 3}:
            for relocation in archive["relocations"]:
                key = (relocation["path"].casefold(), relocation["sha256"])
                result[key] = bp.check_file(relocation["blob"], root)
        else:
            for file in archive["files"]:
                result[(file["path"].casefold(), file["sha256"])] = bp.check_file(file, record["context"])
    return result


def _freeze(source: Path, destination: Path, digest: str) -> None:
    """Atomic, resumable, per-file freezing; never hard-link the working input."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        bp._require(bp._hash(destination) == digest, "immutable archived bytes changed")
        return
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists() and bp._hash(partial) != digest:
        partial.unlink()  # Only this incomplete, unverified archival copy.
    if not partial.exists():
        with source.open("rb") as incoming, partial.open("xb") as outgoing:
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    bp._require(bp._hash(partial) == digest, "bound input changed while freezing")
    bp._rename(partial, destination)
    destination.chmod(stat.S_IREAD)


def resolve_archived_reference(receipt: dict, reference: dict, *, root: Path, evidence_key: bytes) -> Path:
    """Resolve an original (path, byte hash) pair, including multiple versions of a path."""
    body = bp.unseal(receipt, evidence_key, ARCHIVE_KIND + "-receipt")
    bp._require(body["schemaVersion"] in {2, 3} and body.get("phase") == "completed" and
                body["relocationHash"] == plan_fingerprint(body["relocations"]),
                "content-addressed resolution requires a completed schema-2 archive")
    relative = _relative_reference(reference["path"], Path(body["originalDataRoot"]))
    matches = [row for row in body["relocations"] if row["path"].casefold() == relative.casefold()
               and row["sha256"] == reference["sha256"]]
    bp._require(len(matches) == 1, "archived path/hash version is missing or ambiguous")
    bp._require(re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is not None and
                matches[0]["blob"]["sha256"] == reference["sha256"] and
                bp.private(matches[0]["blob"]["path"], root) ==
                bp.private(body["archiveDirectory"], root) / "blobs" / reference["sha256"],
                "archived reference does not address its content hash")
    return bp.check_file(matches[0]["blob"], root)


def _archive_record_v2(receipt, evidence, root, evidence_key, operator_key):
    bp._require(receipt["phase"] == "completed" and receipt.get("liveDatabaseAccess") == "none",
                "historical archive is not complete")
    context = bp.private(receipt["evidenceRoot"], root)
    directory = bp.private(receipt["archiveDirectory"], root)
    lookup = {}
    for row in receipt["relocations"]:
        bp._require(isinstance(row.get("sha256"), str) and
                    re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is not None,
                    "archive content address is invalid")
        key = (row["path"].casefold(), row["sha256"])
        bp._require(key not in lookup and row["blob"]["sha256"] == row["sha256"] and
                    bp.private(row["blob"]["path"], root) == directory / "blobs" / row["sha256"],
                    "archive relocation is duplicated or does not address its content hash")
        blob = bp.check_file(row["blob"], root)
        lookup[key] = blob
        if row["primary"]:
            mirror = bp.private(row["path"], context)
            bp._require(row["mirror"] == bp.file_evidence(mirror, root) and
                        row["mirror"]["sha256"] == row["sha256"], "archive mirror differs from its bound bytes")
        else:
            bp._require(row["mirror"] is None, "secondary evidence has an ambiguous unversioned mirror")
    def resolve(file):
        key = (file["path"].casefold(), file["sha256"])
        if key not in lookup:
            raise HistoricalInputUnavailable("archive omits a bound historical input")
        return lookup[key]
    document = bp.load(bp.check_file(receipt["execution"], root))
    approved = []
    if receipt["schemaVersion"] == 3:
        bp._require(receipt.get("historicalMatchingReplayAvailable") is False and
                    receipt.get("historicalInputClosureVerified") is False,
                    "qualified archive must not claim full historical replay/input closure")
        approved = replay_gap_acknowledgment(
            receipt["historicalReplayGap"], root=Path(receipt["originalDataRoot"]),
            execution_hash=document["documentHash"], target=receipt["target"], operator_key=operator_key,
        )
    else:
        bp._require("historicalReplayGap" not in receipt and "missingBindings" not in receipt,
                    "schema-2 archives cannot acquire a replay-gap qualification")
    missing = []
    required = _bound_inputs(document, original_root=Path(receipt["originalDataRoot"]),
                             resolve=resolve, evidence_key=evidence_key,
                             replay_gaps=approved, missing_bindings=missing)
    if approved:
        bp._require(missing == receipt["missingBindings"], "archive replay-gap qualification differs")
    declared = [{k: row[k] for k in ("path", "sha256", "references", "primary")} for row in receipt["relocations"]]
    bp._require(required == declared and receipt["relocationHash"] == plan_fingerprint(receipt["relocations"]),
                "archive relocation manifest does not cover the exact bound inputs")
    bp._require(receipt["files"] == [
        {"path": row["path"], "sha256": row["sha256"]} for row in declared if row["primary"]
    ], "archive primary file inventory differs")
    verified = completed_execution(document, root=context, evidence_key=evidence_key, operator_key=operator_key)
    bp._require(receipt["executionHash"] == document["documentHash"] and
                receipt["target"] == verified["execution"]["target"] and
                receipt["historicalStateHash"] == verified["completedState"]["stateHash"] and
                bp._hash(bp.check_file(receipt["originalSlot"], root)) ==
                verified["execution"]["stoppedOriginalSha256"] and
                receipt["originalJournal"]["sha256"] == receipt["execution"]["sha256"],
                "historical execution/original-slot binding differs")
    bp.check_file(receipt["originalJournal"], root)
    _archive_authorization(receipt["authorization"], document["documentHash"], receipt["target"],
                           receipt["archiveDirectory"], operator_key, historical=True)
    return {**verified, "archive": receipt, "archiveEvidence": evidence, "context": context,
            "originalRoot": Path(receipt["originalDataRoot"]), "executionHash": document["documentHash"],
            "archivedState": verified["completedState"]}


def archive_completed(*, root: Path, runtime, destination: Path, authorization: dict,
                      supplied_execution_hash: str, evidence_key: bytes, operator_key: bytes,
                      historical_replay_gap: dict | None = None) -> dict:
    """Freeze historical evidence and retire completed side slots; never access the live DB."""
    root = Path(root).resolve()
    destination = bp.private(destination, root)
    directory = str(destination.relative_to(root))
    _archive_authorization(authorization, supplied_execution_hash, runtime.target, directory, operator_key)
    approved_gaps = (
        replay_gap_acknowledgment(historical_replay_gap, root=root, execution_hash=supplied_execution_hash,
                                  target=runtime.target, operator_key=operator_key)
        if historical_replay_gap is not None else []
    )
    slots = paths(root, runtime.target)
    with bp._execution_lock(slots["lock"]):
        runtime.check(production=True)  # Docker identity/mount inspection only, not app/database access.
        history = _history(root, runtime.target, evidence_key)
        prior = _prior_inputs(history, root, evidence_key, operator_key)
        def resolve(file):
            blob = destination / "blobs" / file["sha256"]
            bp._require(not blob.is_symlink(), "archive blobs cannot be symbolic links")
            if blob.exists():
                _not_live(blob, slots["live"])
                bp._require(bp._hash(blob) == file["sha256"], "immutable archive blob changed")
                return blob
            source = bp.private(file["path"], root)
            try:
                _not_live(source, slots["live"])
                if source.is_file() and bp._hash(source) == file["sha256"]:
                    return source
            except bp.PromotionError:
                pass
            old = prior.get((file["path"].casefold(), file["sha256"]))
            if old is not None:
                _not_live(old, slots["live"])
            if old is None:
                raise HistoricalInputUnavailable(
                    "exact historical input bytes are unavailable; never substitute current inputs"
                )
            bp._require(bp._hash(old) == file["sha256"], "previously archived input bytes changed")
            return old
        if slots["archiving"].exists():
            intent = bp.unseal(bp.load(slots["archiving"]), evidence_key, ARCHIVE_KIND + "-intent")
            bp._require(intent.get("schemaVersion") == (3 if approved_gaps else 2) and
                        intent.get("historicalReplayGap") == historical_replay_gap and
                        intent["executionHash"] == supplied_execution_hash and
                        intent["archiveDirectory"] == directory and intent["target"] == runtime.target,
                        "another or legacy archival transaction requires explicit recovery")
        else:
            bp._require(not destination.exists() and not slots["incoming"].exists() and
                        not slots["rejected"].exists(), "completed side-slot inventory is not clean")
            document = bp.load(slots["journal"])
            execution = bp.unseal(document, evidence_key, bp.KIND + "-execution")
            bp._require(execution.get("phase") == "completed" and document["documentHash"] == supplied_execution_hash
                        and execution["target"] == runtime.target,
                        "only the exact completed production journal can be archived")
            _not_live(slots["original"], slots["live"])
            bp.no_sidecars(slots["original"])
            bp._require(bp._hash(slots["original"]) == execution["stoppedOriginalSha256"],
                        "original rollback slot changed")
            missing = []
            files = _bound_inputs(document, original_root=root, resolve=resolve, evidence_key=evidence_key,
                                  replay_gaps=approved_gaps, missing_bindings=missing)
            previous = None if not history["archives"] else bp.load(
                bp.check_file(history["archives"][-1], root))["executionHash"]
            bp._require(execution.get("previousExecutionHash") == previous,
                        "archival would introduce a fork or gap")
            intent = {"kind": ARCHIVE_KIND + "-intent", "schemaVersion": 3 if approved_gaps else 2,
                      "executionHash": supplied_execution_hash, "archiveDirectory": directory,
                      "target": runtime.target, "previousExecutionHash": previous, "historyBefore": history,
                      "files": files, "executionFileSha256": bp._hash(slots["journal"]),
                      "originalSha256": execution["stoppedOriginalSha256"]}
            if approved_gaps:
                intent.update(historicalReplayGap=historical_replay_gap, missingBindings=missing,
                              historicalMatchingReplayAvailable=False, historicalInputClosureVerified=False)
            bp.write(slots["archiving"], bp.seal(intent, evidence_key))
        destination.mkdir(parents=True, exist_ok=True)
        context = destination / "evidence"
        relocations = []
        for file in intent["files"]:
            blob = bp.private(destination / "blobs" / file["sha256"], destination)
            _freeze(resolve(file), blob, file["sha256"])
            mirror = None
            if file["primary"]:
                mirror_path = bp.private(file["path"], context)
                mirror_path.parent.mkdir(parents=True, exist_ok=True)
                if not mirror_path.exists():
                    os.link(blob, mirror_path)
                    bp.fsync_directory(mirror_path.parent)
                bp._require(bp._hash(mirror_path) == file["sha256"], "immutable archive mirror changed")
                mirror = bp.file_evidence(mirror_path, root)
            relocations.append({**file, "blob": bp.file_evidence(blob, root), "mirror": mirror})
        execution_path = destination / "execution.json"
        if not execution_path.exists():
            _freeze(slots["journal"], execution_path, intent["executionFileSha256"])
        bp._require(bp._hash(execution_path) == intent["executionFileSha256"], "archived journal changed")
        document = bp.load(execution_path)
        verified = completed_execution(document, root=context, evidence_key=evidence_key, operator_key=operator_key)
        frozen = {(row["path"].casefold(), row["sha256"]): bp.check_file(row["blob"], root)
                  for row in relocations}
        def frozen_reference(file):
            key = (file["path"].casefold(), file["sha256"])
            if key not in frozen:
                raise HistoricalInputUnavailable("bound input was not frozen before slot retirement")
            return frozen[key]
        frozen_missing = []
        bp._require(_bound_inputs(document, original_root=root, resolve=frozen_reference,
                                   evidence_key=evidence_key, replay_gaps=approved_gaps,
                                   missing_bindings=frozen_missing) == intent["files"] and
                    frozen_missing == intent.get("missingBindings", []),
                    "frozen evidence closure differs from the archival intent")
        receipt_path = destination / "receipt.json"
        if receipt_path.exists():
            _archive_record(bp.file_evidence(receipt_path, root), root, evidence_key, operator_key)
            return _finish_archive(slots, intent, destination, receipt_path, root, runtime.target, evidence_key)
        _validate_next_archive(verified, history, root, evidence_key, operator_key, evidence_root=context)
        _archive_authorization(authorization, supplied_execution_hash, runtime.target, directory, operator_key)
        runtime.check(production=True)
        # Only the detached historical original and terminal journal are renamed.
        archived_original = destination / "original-slot.db"
        if slots["original"].exists():
            _not_live(slots["original"], slots["live"])
            with bp.exclusive_database(slots["original"]) as digest:
                bp._require(digest == intent["originalSha256"], "original rollback slot changed")
                bp._rename(slots["original"], archived_original)
        bp._require(bp._hash(archived_original) == intent["originalSha256"], "archived original changed")
        if slots["journal"].exists():
            bp._require(bp._hash(slots["journal"]) == intent["executionFileSha256"], "completed journal changed")
            bp._rename(slots["journal"], destination / "original-journal.json")
        body = {
            "kind": ARCHIVE_KIND + "-receipt", "schemaVersion": intent["schemaVersion"], "phase": "completed",
            "executionHash": supplied_execution_hash, "previousExecutionHash": intent["previousExecutionHash"],
            "target": runtime.target, "archiveDirectory": directory, "authorization": authorization,
            "originalDataRoot": str(root), "evidenceRoot": str(context.relative_to(root)),
            "files": [{"path": f["path"], "sha256": f["sha256"]} for f in intent["files"] if f["primary"]],
            "relocations": relocations, "relocationHash": plan_fingerprint(relocations),
            "execution": bp.file_evidence(execution_path, root),
            "originalSlot": bp.file_evidence(archived_original, root),
            "originalJournal": bp.file_evidence(destination / "original-journal.json", root),
            "historicalStateHash": verified["completedState"]["stateHash"], "liveDatabaseAccess": "none",
        }
        if approved_gaps:
            body.update(historicalReplayGap=historical_replay_gap, missingBindings=intent["missingBindings"],
                        historicalMatchingReplayAvailable=False, historicalInputClosureVerified=False)
        bp.write(receipt_path, bp.seal(body, evidence_key))
        _archive_record(bp.file_evidence(receipt_path, root), root, evidence_key, operator_key)
        return _finish_archive(slots, intent, destination, receipt_path, root, runtime.target, evidence_key)


def _finish_archive(slots, intent, destination, receipt_path, root, target, evidence_key):
    history = _history(root, target, evidence_key)
    next_history = {**intent["historyBefore"], "archives": [
        *intent["historyBefore"]["archives"], bp.file_evidence(receipt_path, root)
    ]}
    bp._require(history in (intent["historyBefore"], next_history), "archive head moved concurrently")
    if slots["head"].exists():
        bp._write_cutover_state(slots["head"], bp.seal(next_history, evidence_key), create=False)
    else:
        bp.write(slots["head"], bp.seal(next_history, evidence_key))
    if not slots["origin"].exists():
        bp.write(slots["origin"], bp.seal({
            "kind": ARCHIVE_KIND + "-origin", "schemaVersion": 1,
            "targetHash": next_history["targetHash"], "firstArchive": next_history["archives"][0],
        }, evidence_key))
    bp._rename(slots["archiving"], destination / "intent.json")
    return bp.load(receipt_path)
