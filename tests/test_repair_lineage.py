"""Synthetic completed-production history; no private files or Docker processes."""

from __future__ import annotations

import copy
import hashlib
import stat
import os
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from importers.rebuild import bounded_promotion as bp
from importers.rebuild import bounded_promotion_cli as cli
from importers.rebuild import receipt_repair as repair
from importers.rebuild import repair_lineage as rl
from importers.rebuild import immutable_metadata
from importers.simplefin.application import ledger_fingerprint
from tests import test_bounded_promotion as common

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows production file fencing")
EKEY, OKEY = common.EVIDENCE_KEY, common.OPERATOR_KEY


def reseal(value, field):
    value[field] = bp.plan_fingerprint({k: v for k, v in value.items() if k != field})


def rows(path):
    return common.DatabaseClient(common.target(path)).rows


def insert_activity(path, template, *, identifier, alias, amount):
    row = dict(template, id=identifier, idempotency_key=alias, amount=amount)
    with common.closing(common.sqlite3.connect(path)) as connection, connection:
        columns = list(row)
        connection.execute(
            "INSERT INTO activities (" + ",".join(map(bp._quote, columns)) + ") VALUES (" +
            ",".join("?" for _ in columns) + ")", [row[k] for k in columns],
        )


@pytest.fixture
def batch(tmp_path, monkeypatch):
    f = common.fixture.__wrapped__(tmp_path, monkeypatch)
    for path in (f["original"], f["candidate"]):
        common.sql(path, "UPDATE activities SET activity_type='TRANSFER_IN',subtype='external_transfer',"
                   """metadata='{"flow":{"is_external":true}}' WHERE id='SYN-RECON'""")
        template = next(row for row in bp._rows(bp.sqlite_state(path), "activities") if row["id"] == "SYN-SURVIVOR")
        insert_activity(path, template, identifier="SYN-SOURCE-B",
                        alias="simplefin:SYN-ACCOUNT:SYN-B", amount="20")
        insert_activity(path, template, identifier="SYN-SURVIVOR-B",
                        alias="extract:SYN-ACCOUNT:SYN-BANK-B", amount="20")
        common.sql(path, "UPDATE activities SET activity_date='2026-01-16T00:00:00+00:00' "
                   "WHERE id IN ('SYN-SOURCE-B','SYN-SURVIVOR-B')")
    before, after = rows(f["original"]), rows(f["candidate"])
    plan = f["plan"]
    first_recon = next(row for row in before if row["id"] == "SYN-RECON")
    next_recon = next(row for row in after if row["id"] == "SYN-RECON")
    plan["operations"]["reconciliationUpdate"].update(
        beforeFingerprint=repair.activity_semantic_fingerprint(first_recon),
        payload=repair.activity_payload(next_recon),
    )
    for name, values in (("preconditions", before), ("expected", after)):
        plan[name]["activityCount"] = len(values)
        for field in ("accountLedgerFingerprint", "globalLedgerFingerprint"):
            plan[name][field] = ledger_fingerprint(values, {"SYN-ACCOUNT"})
    plan["counts"].update(selectedSourceRows=2, preservedSourceRows=1)
    plan["scope"]["expectedSourceRows"] = 2
    recon = next(row for row in before if row["id"] == "SYN-RECON")
    prior_recon = {**recon, "amount": "70"}
    creates = [repair.activity_payload(row) for row in before if row["id"] in {"SYN-SOURCE", "SYN-SOURCE-B"}]
    source = {
        "schemaVersion": 3, "evidence": {}, "environmentFingerprint": "e" * 64,
        "intentFingerprint": "f" * 64, "ledgerFingerprint": ledger_fingerprint(
            [r for r in before if r["id"] in {"SYN-SURVIVOR", "SYN-SURVIVOR-B"}] + [prior_recon],
            {"SYN-ACCOUNT"},
        ),
        "expectedPostLedgerFingerprint": plan["preconditions"]["globalLedgerFingerprint"],
        "decisionEvidence": {}, "operations": {"creates": creates, "updates": [repair.activity_payload(recon)],
                                              "metadataFinalizations": []},
        "assertions": [{"accountId": "SYN-ACCOUNT", "canonicalAccountId": "SYN-CANONICAL",
                        "beforeBalance": "40", "sourceBalance": "40"}],
        "reconciliations": [{"accountId": "SYN-ACCOUNT", "activityId": "SYN-RECON",
                             "action": "update", "before": prior_recon, "after": recon}],
    }
    reseal(source, "planFingerprint")
    source_path = tmp_path / "sourceApplicationPlanSha256.json"
    common.save(source_path, source)
    source_receipt = {
        "status": "applied", "applicationPlanSha256": bp._hash(source_path),
        "planFingerprint": source["planFingerprint"], "intentFingerprint": source["intentFingerprint"],
        "environmentFingerprint": source["environmentFingerprint"],
        "preLedgerFingerprint": source["ledgerFingerprint"],
        "postLedgerFingerprint": source["expectedPostLedgerFingerprint"],
        "decisionEvidence": {}, "operations": {k: len(v) for k, v in source["operations"].items()},
        "reconciliations": source["reconciliations"],
    }
    source_receipt_path = tmp_path / "sourceApplicationReceiptSha256.json"
    common.save(source_receipt_path, source_receipt)
    plan["evidence"]["sourceApplicationPlanSha256"] = bp._hash(source_path)
    plan["scope"]["sourceApplicationPlanSha256"] = bp._hash(source_path)
    plan["evidence"]["sourceApplicationReceiptSha256"] = bp._hash(source_receipt_path)
    reseal(plan, "planHash")
    common.save(tmp_path / "plan.json", plan)
    recovery = bp.load(tmp_path / "recovery.json")
    recovery.update(planHash=plan["planHash"], preLedgerFingerprint=plan["preconditions"]["globalLedgerFingerprint"])
    reseal(recovery, "recoveryHash")
    common.save(tmp_path / "recovery.json", recovery)
    receipt = bp.load(tmp_path / "receipt.json")
    receipt.update(planHash=plan["planHash"], preLedgerFingerprint=plan["preconditions"]["globalLedgerFingerprint"],
                   postLedgerFingerprint=plan["expected"]["globalLedgerFingerprint"],
                   accountValueBefore="40", accountValueAfter="40", recoveryHash=recovery["recoveryHash"])
    reseal(receipt, "receiptHash")
    common.save(tmp_path / "receipt.json", receipt)
    review = bp.unseal(bp.load(tmp_path / "review.json"), EKEY, bp.KIND + "-review")
    for evidence in review["independentEvidence"]:
        if evidence["path"] in {source_path.name, source_receipt_path.name}:
            evidence["sha256"] = bp._hash(tmp_path / evidence["path"])
    review.update(planHash=plan["planHash"], evidenceHash=bp.plan_fingerprint(plan["evidence"]))
    common.save(tmp_path / "review.json", bp.seal(review, EKEY))
    common.refresh_fixture(f)
    authorization = bp.unseal(f["authorization"], OKEY, bp.KIND + "-authorization")
    authorization["planHash"] = plan["planHash"]
    f["authorization"] = bp.seal(authorization, OKEY)
    monkeypatch.setenv(bp.EVIDENCE_KEY_ENV, EKEY.hex())
    monkeypatch.setenv(bp.OPERATOR_KEY_ENV, OKEY.hex())
    return f


def archive(f, execution, destination=None, *, replay_gap=None):
    destination = destination or f["root"] / "history" / execution["documentHash"]
    now = datetime.now(timezone.utc)
    authorization = bp.seal({
        "kind": rl.ARCHIVE_KIND + "-authorization", "approval": rl.ARCHIVE_APPROVAL,
        "executionHash": execution["documentHash"], "targetHash": bp.plan_fingerprint(f["runtime"].target),
        "archiveDirectory": str(destination.relative_to(f["root"])), "operator": "Synthetic reviewer",
        "issuedAt": now.isoformat(), "expiresAt": (now + timedelta(minutes=10)).isoformat(),
    }, OKEY)
    return rl.archive_completed(root=f["root"], runtime=f["runtime"], destination=destination,
                                authorization=authorization, supplied_execution_hash=execution["documentHash"],
                                evidence_key=EKEY, operator_key=OKEY, historical_replay_gap=replay_gap)


def first_chain(f):
    execution = common.execute(f)
    archived = archive(f, execution)
    chain = rl.build_lineage(root=f["root"], target=f["runtime"].target, evidence_key=EKEY, operator_key=OKEY)
    path = f["root"] / "chain-1.json"
    bp.write(path, chain)
    return execution, archived, chain, path


def bind_mutable_inputs(f, *, extend_source=None):
    """Real path/hash bindings, deliberately absent from the independent-review list."""
    root = f["root"]
    mapping = root / "extracts" / "mapping.json"
    authority = root / "identity" / "source-authority.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    authority.parent.mkdir(parents=True, exist_ok=True)
    common.save(mapping, {"accounts": {"SYN-ACCOUNT": {"canonicalAccountId": "SYN-CANONICAL",
                                                    "displayName": "Original synthetic account"}}})
    common.save(authority, {"schemaVersion": 1, **f["plan"]["evidence"]["identityPolicyDocument"]["sourceAuthority"]})
    bindings = {name: {"path": str(path), "sha256": bp._hash(path)}
                for name, path in (("mapping", mapping), ("authority", authority))}
    source_path = root / "sourceApplicationPlanSha256.json"
    source_receipt_path = root / "sourceApplicationReceiptSha256.json"
    source = bp.load(source_path)
    source["evidence"] = dict(bindings)
    if extend_source is not None:
        extend_source(source)
    reseal(source, "planFingerprint")
    common.save(source_path, source)
    source_receipt = bp.load(source_receipt_path)
    source_receipt.update(applicationPlanSha256=bp._hash(source_path), planFingerprint=source["planFingerprint"],
                          decisionEvidence=source.get("decisionEvidence"), intentFingerprint=source["intentFingerprint"])
    if source["schemaVersion"] == 1:
        source_receipt["schemaVersion"] = 3
    source_receipt["operations"] = {key: len(value) for key, value in source["operations"].items()}
    common.save(source_receipt_path, source_receipt)
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = bp.load(manifest_path)
    manifest["sourceFiles"].extend(bp.file_evidence(path, root) for path in (mapping, authority))
    common.save(manifest_path, manifest)
    plan = f["plan"]
    plan["evidence"].update(sourceApplicationPlanSha256=bp._hash(source_path),
                            sourceApplicationReceiptSha256=bp._hash(source_receipt_path),
                            canonicalManifestSha256=bp._hash(manifest_path))
    plan["scope"]["sourceApplicationPlanSha256"] = bp._hash(source_path)
    reseal(plan, "planHash")
    common.save(root / "plan.json", plan)
    recovery = bp.load(root / "recovery.json")
    recovery["planHash"] = plan["planHash"]
    reseal(recovery, "recoveryHash")
    common.save(root / "recovery.json", recovery)
    receipt = bp.load(root / "receipt.json")
    receipt.update(planHash=plan["planHash"], recoveryHash=recovery["recoveryHash"])
    reseal(receipt, "receiptHash")
    common.save(root / "receipt.json", receipt)
    for i, name in enumerate(("original", "candidate")):
        runtime = common.Runtime(common.target(root / f"input-{name}-restore.db", port=18410+i,
                                               name=f"synthetic-input-{name}"), running=False)
        proof = bp.restore_proof(root=root, backup=f[name], plan=plan, runtime=runtime,
                                evidence_key=EKEY, expected="ready" if name == "original" else "applied")
        filename = f"input-{name}-proof.json"
        common.save(root / filename, proof)
        f["request"][name + "RestoreProof"] = filename
    inputs = bp.build_review_inputs(root=root, request=f["request"], evidence_key=EKEY)
    review = bp.unseal(bp.load(root / "review.json"), EKEY, bp.KIND + "-review")
    review.update({key: inputs[key] for key in bp.REVIEW_BINDINGS})
    for file in review["independentEvidence"]:
        if file["path"] in {str(path.relative_to(root)) for path in (source_path, source_receipt_path, manifest_path)}:
            file["sha256"] = bp._hash(root / file["path"])
    common.save(root / "review.json", bp.seal(review, EKEY))
    f["document"] = bp.prepare(root=root, request=f["request"], runtime=f["runtime"], evidence_key=EKEY)
    authorization = bp.unseal(f["authorization"], OKEY, bp.KIND + "-authorization")
    authorization.update(preparationId=f["document"]["documentHash"], planHash=plan["planHash"],
                         diffHash=f["document"]["diffHash"])
    f["authorization"] = bp.seal(authorization, OKEY)
    return mapping, authority, bindings


def next_plan(f, chain_path, monkeypatch, *, selected="SYN-SOURCE-B"):
    root = f["root"]
    current = rows(f["live"])
    source = next(r for r in current if r["id"] == "SYN-SOURCE-B")
    survivor = next(r for r in current if r["id"] == "SYN-SURVIVOR-B")
    policy = copy.deepcopy(f["plan"]["evidence"]["identityPolicyDocument"])
    authority = policy["sourceAuthority"]
    artifacts = {}
    interval_ids = {}
    for interval in authority["intervals"]:
        family = interval["evidence"]["sourceFamily"]
        path = root / f"batch2-source-{family}.json"
        common.save(path, {"sourceFamily": family, "date": "2026-01-16", "amount": "-20",
                           "description": source["comment"]})
        artifacts[family] = bp.file_evidence(path, root)
        interval["evidence"]["sourceHashes"] = [artifacts[family]["sha256"]]
        interval_ids[family] = bp.content_hash(interval)
    features = copy.deepcopy(f["plan"]["operations"]["repairs"][0]["featureVector"])
    features.update(authoritativeIntervalId=interval_ids["ofx"], suppressedIntervalId=interval_ids["simplefin"])
    decision_id = bp.plan_fingerprint({"synthetic-exact-decision": 2})
    event_id = bp.plan_fingerprint({"synthetic-event": 2})
    observations = []
    scope_rows = []
    for role, row, family in (("source", source, "simplefin"), ("survivor", survivor, "ofx")):
        transaction = {"account_id": "SYN-CANONICAL", "source_id": row["idempotencyKey"],
                       "date": "2026-01-16", "amount": "-20", "currency": "USD",
                       "description": row["comment"], "source_file": artifacts[family]["path"]}
        fingerprint = bp.stable_hash(transaction)
        observation_id = bp.stable_hash({"kind": "canonical-source-observation",
                                        "fingerprint": fingerprint, "ordinal": 1})
        observations.append({
            "observationId": observation_id, "observationFingerprint": fingerprint,
            "transaction": transaction, "canonicalTransactionId": event_id, "decisionId": decision_id,
            "decisionType": "automatic-identity", "disposition": "suppressed" if role == "source" else "active",
        })
        scope_rows.append(transaction)
    identity = {
        "policyVersion": policy["version"], "policyHash": bp.content_hash(policy), "policyDocument": policy,
        "generationHash": "7" * 64, "canonicalStateHash": "8" * 64,
        "sourceAuthority": {"policyVersion": authority["policy"]["version"],
            "policyHash": authority["policyHash"], "authorityHash": bp.content_hash(authority),
            "intervals": [{"intervalId": value, "proven": True, "reconciled": True,
                          "authorityProof": {"sourceArtifactBound": "true"}} for value in interval_ids.values()]},
    }
    document = {"schemaVersion": 1, "kind": "canonical-transaction-observations", "private": True,
                "observationCount": len(observations), "observations": observations,
                "identityScope": {"schemaVersion": 1, "kind": "canonical-identity-scope", "private": True,
                                  "rows": scope_rows, "rowCount": 2, "scopeHash": bp.content_hash(scope_rows)}}
    lineage = {
        "schemaVersion": 1, "kind": "canonical-transaction-lineage", "private": True,
        "baselinePublicationId": "2" * 64, "forensicPublicationId": "3" * 64, "identityPolicy": identity,
        "canonicalTransactions": [{"canonicalTransactionId": event_id,
                                   "activeObservationId": observations[1]["observationId"],
                                   "memberObservationIds": [r["observationId"] for r in observations]}],
        "decisionProjections": [{
            "decisionId": decision_id, "decisionHash": decision_id, "policyVersion": policy["version"],
            "outcome": "source-suppressed", "rationaleCode": repair.AUTHORITY_RATIONALE,
            "confidenceTier": repair.AUTHORITY_CONFIDENCE, "featureVector": features,
            "competingCandidateProof": {}, "sourceAuthorityPolicyHash": authority["policyHash"],
            "canonicalTransactionIds": [event_id],
            "sourceHashes": sorted({bp.content_hash(r) for r in scope_rows} |
                                   {r["sha256"] for r in artifacts.values()}),
        }],
    }
    directory = root / "canonical-batch2"
    directory.mkdir(exist_ok=True)
    common.save(directory / "transaction-observations.json", document)
    common.save(directory / "transaction-lineage.json", lineage)
    common.save(directory / "manifest.json", {
        "schemaVersion": 5, "sourceFiles": list(artifacts.values()),
        "dataFiles": {name: bp._hash(directory / name) for name in
                      ("transaction-observations.json", "transaction-lineage.json")},
    })
    source_path = root / "sourceApplicationPlanSha256.json"
    source_receipt = root / "sourceApplicationReceiptSha256.json"
    evidence = {
        "baselinePointer": {"publicationId": "2" * 64}, "forensicPointer": {"publicationId": "3" * 64},
        "baselinePublication": root, "baselineManifest": {},
        "plans": [{"sha256": bp._hash(source_path), "value": bp.load(source_path)}],
        "receipts": [{"sha256": bp._hash(source_receipt), "value": bp.load(source_receipt)}],
        "forensicDetail": {"activities": [{
            "activityId": r["id"], "sourceIdentity": r["idempotencyKey"], "raw": r,
            "lineage": {"status": "receipt-proven", "applicationPlanSha256": bp._hash(source_path)},
            "dependentState": {"reasonCodes": [], "categoryAssignments": []},
        } for r in current]},
    }
    monkeypatch.setattr(repair, "_current_evidence", lambda _root: evidence)
    monkeypatch.setattr(repair, "_canonical_documents", lambda _root: (
        document, lineage, [{"account_id": "SYN-CANONICAL", "currency": "USD"}],
        bp._hash(directory / "manifest.json"),
    ))
    monkeypatch.setattr(repair.forensic, "_domain", lambda *_: [
        {"id": "SYN-ACCOUNT", "accountType": "CASH", "currency": "USD"}
    ])
    selection = {**f["plan"]["scope"], "sourceActivityIds": [selected]}
    result = repair.build_plan(root, selection, generated_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
                               production_lineage=chain_path, lineage_evidence_key=EKEY,
                               lineage_operator_key=OKEY)
    return result, artifacts, directory / "manifest.json"


def second_preparation(f, plan, artifacts, manifest, *, acknowledge_replay=True):
    root = f["root"]
    original, candidate = root / "batch2-original.db", root / "batch2-candidate.db"
    shutil.copyfile(f["live"], original)
    shutil.copyfile(original, candidate)
    common.sql(candidate, "DELETE FROM activities WHERE id='SYN-SOURCE-B'")
    common.sql(candidate, "UPDATE activities SET amount='70' WHERE id='SYN-RECON'")
    common.save(root / "plan-2.json", plan)
    clone = common.target(root / "batch2-apply.db", port=18300, name="synthetic-batch2-apply")
    recovery = {
        "schemaVersion": 2, "kind": repair.KIND + "-recovery", "status": "prepared",
        "planHash": plan["planHash"], "instanceId": clone["instanceId"],
        "preLedgerFingerprint": plan["preconditions"]["globalLedgerFingerprint"],
        "backup": bp.backup_evidence(original, root),
    }
    reseal(recovery, "recoveryHash")
    receipt = {
        "schemaVersion": 2, "kind": repair.KIND + "-receipt", "status": "applied",
        "planHash": plan["planHash"], "instanceId": clone["instanceId"],
        "preLedgerFingerprint": plan["preconditions"]["globalLedgerFingerprint"],
        "postLedgerFingerprint": plan["expected"]["globalLedgerFingerprint"],
        "operationCounts": {"deleted": 1, "assignmentUpserts": 0, "reconciliationUpdates": 1},
        "accountValueConserved": True, "accountValueBasis": "cash-ledger",
        "accountValueBefore": "40", "accountValueAfter": "40",
        "backup": recovery["backup"], "recoveryHash": recovery["recoveryHash"],
    }
    reseal(receipt, "receiptHash")
    common.save(root / "recovery-2.json", recovery)
    common.save(root / "receipt-2.json", receipt)
    request = {
        "plan": "plan-2.json", "receipt": "receipt-2.json", "recovery": "recovery-2.json",
        "review": "review-2.json", "canonicalManifest": str(manifest.relative_to(root)),
        "releaseRevision": common.REVISION, "target": f["runtime"].target,
        "originalBackup": str(original.relative_to(root)), "candidateBackup": str(candidate.relative_to(root)),
        "appliedCloneInstanceId": clone["instanceId"],
    }
    for i, name in enumerate(("original", "candidate")):
        runtime = common.Runtime(common.target(root / f"batch2-{name}-restore.db", port=18301+i,
                                               name=f"synthetic-batch2-{name}"), running=False)
        proof = bp.restore_proof(root=root, backup=original if name == "original" else candidate,
                                plan=plan, runtime=runtime, evidence_key=EKEY,
                                expected="ready" if name == "original" else "applied")
        request[name + "RestoreProof"] = f"batch2-{name}-proof.json"
        common.save(root / request[name + "RestoreProof"], proof)
    inputs = bp.build_review_inputs(root=root, request=request, evidence_key=EKEY, operator_key=OKEY)
    tasks = [{"taskPath": "\\Synthetic\\", "taskName": "DisabledLegacyWriter"}]
    review = bp.review_bindings(inputs) if acknowledge_replay else {
        key: inputs[key] for key in bp.REVIEW_BINDINGS
    }
    review.update(kind=bp.KIND + "-review", legacyWriterTasks=tasks,
                  legacyWriterState=f["runtime"].writer_state(tasks),
                  independentEvidence=[*artifacts.values(), bp.file_evidence(manifest, root),
                    bp.file_evidence(root / "sourceApplicationPlanSha256.json", root),
                    bp.file_evidence(root / "sourceApplicationReceiptSha256.json", root)])
    common.save(root / "review-2.json", bp.seal(review, EKEY))
    preparation = bp.prepare(root=root, request=request, runtime=f["runtime"],
                             evidence_key=EKEY, operator_key=OKEY)
    authorization = bp.unseal(f["authorization"], OKEY, bp.KIND + "-authorization")
    authorization.update(preparationId=preparation["documentHash"], planHash=plan["planHash"],
                         diffHash=preparation["diffHash"])
    return preparation, bp.seal(authorization, OKEY)


def test_completed_archive_is_immutable_and_frees_only_completed_slots(batch):
    f = batch
    execution, archived, chain, path = first_chain(f)
    slots = rl.paths(f["root"], f["runtime"].target)
    assert f["runtime"].running
    assert not slots["journal"].exists() and not slots["original"].exists()
    assert not slots["archiving"].exists()
    assert slots["head"].exists()
    assert chain["headExecutionHash"] == execution["documentHash"]
    assert repair._signed(chain["compensationPayload"]) == 90
    assert chain["accountValue"] == "40"
    assert chain["deleted"] == [{"activityId": "SYN-SOURCE",
                                  "sourceIdentity": "simplefin:SYN-ACCOUNT:SYN-1"}]
    assert bp._hash(bp.check_file(archived["originalSlot"], f["root"])) == execution["stoppedOriginalSha256"]
    assert rl.load_lineage(path, root=f["root"], evidence_key=EKEY, operator_key=OKEY)[0] == chain
    with pytest.raises(bp.PromotionError):
        common.execute(f)  # old first-batch authority cannot silently reuse the freed slots


@pytest.mark.parametrize("edit", ["phase", "source-plan"])
def test_archive_refuses_uncertain_or_changed_evidence(batch, edit):
    f = batch
    execution = common.execute(f)
    if edit == "phase":
        body = bp.unseal(execution, EKEY, bp.KIND + "-execution")
        body["phase"] = "manual-recovery-required"
        body["history"][-1]["phase"] = body["phase"]
        execution = bp.seal(body, EKEY)
        common.save(bp._slots(f["root"], f["runtime"].target)["journal"], execution)
    else:
        with (f["root"] / "sourceApplicationPlanSha256.json").open("ab") as stream:
            stream.write(b"\n")
    stops = f["runtime"].stops
    with pytest.raises(Exception):
        archive(f, execution)
    assert f["runtime"].stops == stops
    assert bp._slots(f["root"], f["runtime"].target)["journal"].exists()
    assert bp._slots(f["root"], f["runtime"].target)["original"].exists()


def test_clone_receipt_is_not_production_history(batch):
    f = batch
    with pytest.raises(bp.PromotionError):
        rl.completed_execution(bp.load(f["root"] / "receipt.json"), root=f["root"],
                               evidence_key=EKEY, operator_key=OKEY)


def test_history_duplicate_or_fork_is_rejected(batch):
    f = batch
    _, _, _, _path = first_chain(f)
    head_path = rl.paths(f["root"], f["runtime"].target)["head"]
    body = bp.unseal(bp.load(head_path), EKEY, rl.ARCHIVE_KIND + "-history")
    body["archives"].append(body["archives"][0])
    common.save(head_path, bp.seal(body, EKEY))
    with pytest.raises(bp.PromotionError, match="duplicate, gap or fork"):
        rl.build_lineage(root=f["root"], target=f["runtime"].target, evidence_key=EKEY, operator_key=OKEY)


def test_two_completed_cycles_use_only_new_effects_and_preserve_history(batch, monkeypatch):
    f = batch
    first, _archive, chain, chain_path = first_chain(f)
    common.sql(f["live"], "UPDATE activities SET notes='User note after batch one',is_user_modified=1 "
               "WHERE id='SYN-SURVIVOR'")
    common.sql(f["live"], "UPDATE unknown_user_work SET unknown_field='Preserve across the next cycle'")
    plan, artifacts, manifest = next_plan(f, chain_path, monkeypatch)
    assert plan["counts"]["previouslyRepairedSourceRows"] == 1
    assert plan["counts"]["preservedSourceRows"] == 0
    assert plan["counts"]["liveSourceRowsBefore"] == 1
    assert plan["operations"]["reconciliationUpdate"]["beforeEffect"] == "90.0"
    assert repair._signed(plan["operations"]["reconciliationUpdate"]["payload"]) == 70
    again, _, _ = next_plan(f, chain_path, monkeypatch)
    assert again["planHash"] == plan["planHash"]
    preparation, authorization = second_preparation(f, plan, artifacts, manifest)
    second = bp.execute(root=f["root"], document=preparation, authorization=authorization,
                        supplied_preparation_id=preparation["documentHash"], runtime=f["runtime"],
                        evidence_key=EKEY, operator_key=OKEY)
    assert second["phase"] == "completed" and second["previousExecutionHash"] == first["documentHash"]
    assert sum(repair._signed(r) for r in rows(f["live"])) == 40
    assert next(r for r in rows(f["live"]) if r["id"] == "SYN-SURVIVOR")["comment"] == "User note after batch one"
    assert bp._rows(bp.sqlite_state(f["live"]), "unknown_user_work")[0]["unknown_field"] == \
        "Preserve across the next cycle"
    archive(f, second)
    final = rl.build_lineage(root=f["root"], target=f["runtime"].target, evidence_key=EKEY, operator_key=OKEY)
    assert len(final["steps"]) == 2 and len(final["deleted"]) == 2
    assert repair._signed(final["compensationPayload"]) == 70
    assert bp.check_file(chain["steps"][0]["archive"], f["root"]).exists()
    with pytest.raises(bp.PromotionError, match="no longer the archive head"):
        rl.load_lineage(chain_path, root=f["root"], evidence_key=EKEY, operator_key=OKEY)
    history_path = rl.paths(f["root"], f["runtime"].target)["head"]
    history = bp.unseal(bp.load(history_path), EKEY, rl.ARCHIVE_KIND + "-history")
    history["archives"] = history["archives"][1:]
    common.save(history_path, bp.seal(history, EKEY))
    with pytest.raises(bp.PromotionError, match="gap or fork|predecessor|origin/prefix"):
        rl.build_lineage(root=f["root"], target=f["runtime"].target, evidence_key=EKEY, operator_key=OKEY)


@pytest.mark.parametrize("change", ["old-id", "edited-compensation", "source-alias"])
def test_continuation_refuses_repeats_and_drift(batch, monkeypatch, change):
    f = batch
    _, _, _, path = first_chain(f)
    if change == "edited-compensation":
        common.sql(f["live"], "UPDATE activities SET amount='91' WHERE id='SYN-RECON'")
    elif change == "source-alias":
        common.sql(f["live"], "UPDATE activities SET idempotency_key='simplefin:OTHER:SYN-1' WHERE id='SYN-SOURCE-B'")
    with pytest.raises((repair.ReceiptRepairError, bp.PromotionError)):
        next_plan(f, path, monkeypatch, selected="SYN-SOURCE" if change == "old-id" else "SYN-SOURCE-B")


def test_archive_never_accesses_or_controls_live_database_even_after_user_edits(batch, monkeypatch):
    f = batch
    execution = common.execute(f)
    common.sql(f["live"], "UPDATE app_settings SET value='legitimate later user edit'")
    common.sql(f["live"], common.NEW_USER_TRANSACTION)
    before = bp._hash(f["live"])
    original_hash, original_state = bp._hash, bp.sqlite_state
    def guarded_hash(path):
        assert path != f["live"], "archival read live database bytes"
        return original_hash(path)
    def guarded_state(path, **kw):
        assert path != f["live"], "archival opened live SQLite"
        return original_state(path, **kw)
    monkeypatch.setattr(bp, "_hash", guarded_hash)
    monkeypatch.setattr(bp, "sqlite_state", guarded_state)
    for method in ("stop", "start", "wait", "verify", "client", "writer_state"):
        monkeypatch.setattr(f["runtime"], method, lambda *_a, **_kw: pytest.fail("archive touched live runtime"))
    monkeypatch.setattr(bp, "checkpoint_stopped_database", lambda *_a, **_kw: pytest.fail("archive checkpointed"))
    result = archive(f, execution)
    slots = rl.paths(f["root"], f["runtime"].target)
    assert f["runtime"].running
    assert not slots["original"].exists() and not slots["journal"].exists()
    assert slots["head"].exists() and not slots["archiving"].exists()
    assert result["schemaVersion"] == 2 and result["liveDatabaseAccess"] == "none"
    assert original_hash(f["live"]) == before


def test_archive_freeze_interruption_resumes_without_rewriting_history(batch, monkeypatch):
    f = batch
    execution = common.execute(f)
    freeze = rl._freeze
    copied = []
    class Interrupted(BaseException):
        pass
    def stop_after_first(source, destination, digest):
        freeze(source, destination, digest)
        copied.append(source)
        raise Interrupted()
    monkeypatch.setattr(rl, "_freeze", stop_after_first)
    with pytest.raises(Interrupted):
        archive(f, execution)
    slots = rl.paths(f["root"], f["runtime"].target)
    assert slots["archiving"].exists() and slots["journal"].exists()
    assert not slots["head"].exists()
    copied[0].write_bytes(b"legitimate later working-file replacement")
    monkeypatch.setattr(rl, "_freeze", freeze)
    stops = f["runtime"].stops
    result = archive(f, execution)
    assert result["phase"] == "completed" and f["runtime"].running
    assert f["runtime"].stops == stops
    history = bp.unseal(bp.load(slots["head"]), EKEY, rl.ARCHIVE_KIND + "-history")
    assert len(history["archives"]) == 1 and not slots["archiving"].exists()


def test_closed_archival_receipt_resumes_only_the_head_publication(batch, monkeypatch):
    f = batch
    execution = common.execute(f)
    finish = rl._finish_archive
    class Interrupted(BaseException):
        pass
    monkeypatch.setattr(rl, "_finish_archive", lambda *_: (_ for _ in ()).throw(Interrupted()))
    with pytest.raises(Interrupted):
        archive(f, execution)
    monkeypatch.setattr(rl, "_finish_archive", finish)
    stops = f["runtime"].stops
    result = archive(f, execution)
    assert result["phase"] == "completed" and f["runtime"].stops == stops


@pytest.mark.parametrize("field", ["accountId", "sourcePlan", "sourceReceipt", "target"])
def test_forged_chain_scope_is_rejected(batch, field):
    f = batch
    _, _, chain, path = first_chain(f)
    forged = bp.unseal(chain, EKEY, rl.CHAIN_KIND)
    if field == "accountId":
        forged[field] = "SYN-OTHER-ACCOUNT"
    elif field == "target":
        forged[field]["containerId"] = "0" * 64
    else:
        forged[field]["sha256"] = "0" * 64
    common.save(path, bp.seal(forged, EKEY))
    with pytest.raises(bp.PromotionError):
        rl.load_lineage(path, root=f["root"], evidence_key=EKEY, operator_key=OKEY)


def test_archive_copied_receipts_remain_verifiable_after_original_paths_advance(batch):
    f = batch
    _, _, chain, path = first_chain(f)
    for name in ("plan.json", "receipt.json", "recovery.json", "review.json",
                 "sourceApplicationPlanSha256.json", "sourceApplicationReceiptSha256.json"):
        (f["root"] / name).unlink()
    assert rl.load_lineage(path, root=f["root"], evidence_key=EKEY, operator_key=OKEY)[0] == chain


def test_absolute_original_evidence_paths_are_relocated_without_rewriting_documents(batch):
    f = batch
    review = bp.unseal(bp.load(f["root"] / "review.json"), EKEY, bp.KIND + "-review")
    for evidence in review["independentEvidence"]:
        evidence["path"] = str(f["root"] / evidence["path"])
    common.save(f["root"] / "review.json", bp.seal(review, EKEY))
    f["document"] = bp.prepare(root=f["root"], request=f["request"], runtime=f["runtime"], evidence_key=EKEY)
    authorization = bp.unseal(f["authorization"], OKEY, bp.KIND + "-authorization")
    authorization["preparationId"] = f["document"]["documentHash"]
    f["authorization"] = bp.seal(authorization, OKEY)
    _, archived, chain, path = first_chain(f)
    assert archived["originalDataRoot"] == str(f["root"])
    (f["root"] / "sourceApplicationPlanSha256.json").unlink()
    (f["root"] / "sourceApplicationReceiptSha256.json").unlink()
    assert rl.load_lineage(path, root=f["root"], evidence_key=EKEY, operator_key=OKEY)[0] == chain


def test_missing_history_index_is_not_treated_as_a_fresh_first_release(batch):
    f = batch
    first_chain(f)
    slots = rl.paths(f["root"], f["runtime"].target)
    slots["head"].unlink()
    stops = f["runtime"].stops
    with pytest.raises(bp.PromotionError, match="history index is missing"):
        rl.require_current_head(f["plan"], f["root"], f["runtime"].target, EKEY)
    assert f["runtime"].stops == stops and slots["origin"].exists()


def test_mapping_and_authority_updates_do_not_change_archived_evidence(batch):
    f = batch
    mapping, authority, bindings = bind_mutable_inputs(f)
    original = {name: path.read_bytes() for name, path in (("mapping", mapping), ("authority", authority))}
    execution, archived, chain, chain_path = first_chain(f)
    for name, reference in bindings.items():
        blob = rl.resolve_archived_reference(archived, reference, root=f["root"], evidence_key=EKEY)
        assert blob.name == reference["sha256"] and blob.read_bytes() == original[name]
    common.save(mapping, {"accounts": {"SYN-ACCOUNT": {"canonicalAccountId": "SYN-CANONICAL",
                                                    "displayName": "Legitimate updated account name"}}})
    updated = bp.load(authority)
    updated["intervals"][0]["evidence"]["freshnessAsOf"] = "2026-02-01"
    common.save(authority, updated)
    assert rl.load_lineage(chain_path, root=f["root"], evidence_key=EKEY, operator_key=OKEY)[0] == chain
    archived_execution = bp.load(bp.check_file(archived["execution"], f["root"]))
    assert archived_execution == execution  # No rewritten journal or preparation.
    for name, reference in bindings.items():
        assert rl.resolve_archived_reference(archived, reference, root=f["root"],
                                             evidence_key=EKEY).read_bytes() == original[name]


def test_second_archive_preserves_two_versions_of_the_same_input_path(batch, monkeypatch):
    f = batch
    mapping, authority, old = bind_mutable_inputs(f)
    _, _, _, chain_path = first_chain(f)
    common.save(mapping, {"accounts": {"SYN-ACCOUNT": {"canonicalAccountId": "SYN-CANONICAL",
                                                    "displayName": "Second legitimate configuration"}}})
    changed = bp.load(authority)
    changed["intervals"][0]["evidence"]["freshnessAsOf"] = "2026-02-01"
    common.save(authority, changed)
    new = {name: {"path": str(path), "sha256": bp._hash(path)}
           for name, path in (("mapping", mapping), ("authority", authority))}
    plan, artifacts, manifest_path = next_plan(f, chain_path, monkeypatch)
    manifest = bp.load(manifest_path)
    manifest["sourceFiles"].extend(bp.file_evidence(path, f["root"]) for path in (mapping, authority))
    common.save(manifest_path, manifest)
    plan["evidence"]["canonicalManifestSha256"] = bp._hash(manifest_path)
    reseal(plan, "planHash")
    preparation, authorization = second_preparation(f, plan, artifacts, manifest_path)
    execution = bp.execute(root=f["root"], document=preparation, authorization=authorization,
                           supplied_preparation_id=preparation["documentHash"], runtime=f["runtime"],
                           evidence_key=EKEY, operator_key=OKEY)
    archived = archive(f, execution)
    for name in old:
        assert old[name]["sha256"] != new[name]["sha256"]
        old_blob = rl.resolve_archived_reference(archived, old[name], root=f["root"], evidence_key=EKEY)
        new_blob = rl.resolve_archived_reference(archived, new[name], root=f["root"], evidence_key=EKEY)
        assert old_blob != new_blob and old_blob.read_bytes() != new_blob.read_bytes()
    common.save(mapping, {"accounts": {"SYN-ACCOUNT": {"displayName": "Another later update"}}})
    assert len(rl.build_lineage(root=f["root"], target=f["runtime"].target,
                               evidence_key=EKEY, operator_key=OKEY)["steps"]) == 2


def test_changed_content_addressed_bytes_are_rejected(batch):
    f = batch
    _mapping, _authority, references = bind_mutable_inputs(f)
    _, archived, _, chain_path = first_chain(f)
    blob = rl.resolve_archived_reference(archived, references["mapping"], root=f["root"], evidence_key=EKEY)
    blob.chmod(stat.S_IREAD | stat.S_IWRITE)
    blob.write_bytes(b"tampered immutable input")
    with pytest.raises(bp.PromotionError):
        rl.load_lineage(chain_path, root=f["root"], evidence_key=EKEY, operator_key=OKEY)


def test_unfrozen_changed_input_is_not_replaced_by_its_current_version(batch):
    f = batch
    mapping, _authority, _bindings = bind_mutable_inputs(f)
    execution = common.execute(f)
    common.save(mapping, {"accounts": {"SYN-ACCOUNT": {"displayName": "Changed before freezing"}}})
    stops = f["runtime"].stops
    with pytest.raises(bp.PromotionError, match="exact historical input bytes"):
        archive(f, execution)
    slots = rl.paths(f["root"], f["runtime"].target)
    assert slots["journal"].exists() and slots["original"].exists()
    assert not slots["archiving"].exists() and f["runtime"].stops == stops


def test_historical_archival_does_not_start_a_stopped_service(batch):
    f = batch
    execution = common.execute(f)
    f["runtime"].running = False
    starts, stops = f["runtime"].starts, f["runtime"].stops
    assert archive(f, execution)["schemaVersion"] == 2
    assert not f["runtime"].running
    assert (f["runtime"].starts, f["runtime"].stops) == (starts, stops)


def test_archive_cli_needs_no_app_password_or_login(batch, monkeypatch):
    f = batch
    execution = common.execute(f)
    now = datetime.now(timezone.utc)
    target_path = f["root"] / "target.json"
    authorization_path = f["root"] / "archive-authorization.json"
    common.save(target_path, f["runtime"].target)
    common.save(authorization_path, bp.seal({
        "kind": rl.ARCHIVE_KIND + "-authorization", "approval": rl.ARCHIVE_APPROVAL,
        "executionHash": execution["documentHash"], "targetHash": bp.plan_fingerprint(f["runtime"].target),
        "archiveDirectory": "history-cli", "operator": "Synthetic reviewer",
        "issuedAt": now.isoformat(), "expiresAt": (now + timedelta(minutes=10)).isoformat(),
    }, OKEY))
    monkeypatch.delenv("WEALTHFOLIO_PASSWORD", raising=False)
    def metadata_runtime(target, root, password):
        assert password == "" and target == f["runtime"].target and root == f["root"]
        return f["runtime"]
    monkeypatch.setattr(bp, "GuardedDockerRuntime", metadata_runtime)
    monkeypatch.setattr(cli, "_runtime", lambda *_: pytest.fail("archive requested app authentication"))
    starts, stops = f["runtime"].starts, f["runtime"].stops
    assert cli.main([
        "archive", "--data-dir", str(f["root"]), "--target", str(target_path),
        "--execution-hash", execution["documentHash"], "--destination", "history-cli",
        "--authorization", str(authorization_path),
    ]) == 0
    assert (f["runtime"].starts, f["runtime"].stops) == (starts, stops)


def test_legacy_completed_archive_receipt_remains_readable(batch):
    f = batch
    execution, archived, _chain, _path = first_chain(f)
    record = rl.completed_execution(execution, root=f["root"], evidence_key=EKEY, operator_key=OKEY)
    now = datetime.now(timezone.utc)
    point = bp.verify_runtime_state(
        record["completedState"], [record["completedState"]], f["plan"],
        f["document"]["runtimeTimestampPolicy"], started_at=now, checked_at=now,
    )
    fields = (
        "kind", "phase", "executionHash", "previousExecutionHash", "target", "archiveDirectory",
        "authorization", "originalDataRoot", "evidenceRoot", "files", "execution",
        "originalSlot", "originalJournal",
    )
    legacy = {key: archived[key] for key in fields}
    legacy.update(schemaVersion=1, preArchiveVerification=point, finalVerification=point,
                  restartedAt=now.isoformat(), archivedLiveStateHash=point["actualStateHash"])
    path = f["root"] / "legacy-archive-receipt.json"
    common.save(path, bp.seal(legacy, EKEY))
    assert rl._archive_record(bp.file_evidence(path, f["root"]), f["root"], EKEY, OKEY)[
        "executionHash"
    ] == execution["documentHash"]


@pytest.mark.parametrize("target_name", ["intent", "receipt", "head", "origin"])
def test_byte_write_failure_never_publishes_truncated_archival_metadata(batch, monkeypatch, target_name):
    f = batch
    execution = common.execute(f)
    slots = rl.paths(f["root"], f["runtime"].target)
    destination = f["root"] / "history" / execution["documentHash"]
    target = {"intent": slots["archiving"], "receipt": destination / "receipt.json",
              "head": slots["head"], "origin": slots["origin"]}[target_name]
    write_block = immutable_metadata._write_block
    interrupted = []
    class PowerLoss(BaseException):
        pass
    def partial_bytes(stream, block):
        if target.name in str(stream.name) and not interrupted:
            stream.write(block[:max(1, len(block) // 2)])
            stream.flush()
            os.fsync(stream.fileno())
            interrupted.append(str(stream.name))
            raise PowerLoss()
        return write_block(stream, block)
    monkeypatch.setattr(immutable_metadata, "_write_block", partial_bytes)
    starts, stops = f["runtime"].starts, f["runtime"].stops
    with pytest.raises(PowerLoss):
        archive(f, execution)
    assert interrupted and not target.exists()
    if target_name != "intent":
        bp.unseal(bp.load(slots["archiving"]), EKEY, rl.ARCHIVE_KIND + "-intent")
    monkeypatch.setattr(immutable_metadata, "_write_block", write_block)
    result = archive(f, execution)
    assert result["phase"] == "completed"
    assert (f["runtime"].starts, f["runtime"].stops) == (starts, stops)
    assert not slots["archiving"].exists()
    assert rl.build_lineage(root=f["root"], target=f["runtime"].target,
                            evidence_key=EKEY, operator_key=OKEY)["headExecutionHash"] == execution["documentHash"]


@pytest.mark.parametrize("missing_manual_input", [False, True])
def test_legacy_application_archival_keeps_direct_manual_evidence_mandatory(batch, missing_manual_input):
    f = batch

    def legacy(source):
        source.update(schemaVersion=1, mode="production-promotion-plan", portableIntent={},
                      ledgerAccountIds=["SYN-ACCOUNT"], links=[], manual=[], monitors=[],
                      impact={}, spendingWindow={})
        source["operations"].pop("metadataFinalizations", None)
        if missing_manual_input:
            binding = {"path": str(f["root"] / "normalized" / "canonical" / "transactions.csv"),
                       "sha256": "9" * 64}
            path = f["root"] / "legacy-manual-decisions.json"
            common.save(path, {"schemaVersion": 1, "decisions": [{"evidence": [binding]}]})
            source["evidence"]["manualDecisions"] = {"path": str(path), "sha256": bp._hash(path)}
            evidence = {"manualDecisionsSha256": bp._hash(path), "nestedEvidence": [binding]}
            reseal(evidence, "fingerprint")
            source["decisionEvidence"] = evidence
            source["portableIntent"]["decisionEvidence"] = evidence

    bind_mutable_inputs(f, extend_source=legacy)
    execution = common.execute(f)
    if missing_manual_input:
        with pytest.raises(rl.HistoricalInputUnavailable):
            archive(f, execution)
        slots = rl.paths(f["root"], f["runtime"].target)
        assert slots["journal"].exists() and slots["original"].exists()
        assert not slots["archiving"].exists()
    else:
        archived = archive(f, execution)
        assert archived["phase"] == "completed"
        chain = rl.build_lineage(root=f["root"], target=f["runtime"].target,
                                 evidence_key=EKEY, operator_key=OKEY)
        assert chain["headExecutionHash"] == execution["documentHash"]


def nested_application_inputs(f, *, unsupported_stage=False):
    root = f["root"]
    note = root / "manual-review-note.json"
    common.save(note, {"kind": "synthetic-independent-note", "text": "Original reviewed evidence",
                       "normalizedRowHash": "0" * 64})
    note_reference = {"path": str(note), "sha256": bp._hash(note)}
    decisions = root / "manual-decisions.json"
    common.save(decisions, {"schemaVersion": 1, "decisions": [{
        "decisionId": "SYN-RULING", "sourceAccountId": "SYN-ACCOUNT", "sourceId": "SYN-HELD",
        "ruling": "hold", "rationale": "Synthetic independently reviewed hold",
        "evidence": [note_reference],
    }]})
    manual_reference = {"path": str(decisions), "sha256": bp._hash(decisions)}
    binding = {"manualDecisionsSha256": manual_reference["sha256"], "nestedEvidence": [note_reference]}
    reseal(binding, "fingerprint")
    stage_input = root / "stage-only-input.json"
    common.save(stage_input, {"schemaVersion": 1, "syntheticInput": "original-stage-value"})
    stage_reference = {"path": str(stage_input), "sha256": bp._hash(stage_input)}
    older_input = root / "older-stage-input.json"
    common.save(older_input, {"schemaVersion": 1, "syntheticInput": "original-older-stage-value"})
    older_reference = {"path": str(older_input), "sha256": bp._hash(older_input)}
    def extend(source):
        source["evidence"]["manualDecisions"] = manual_reference
        source["decisionEvidence"] = binding
        source["portableIntent"] = {"decisionEvidence": binding}
        source["intentFingerprint"] = bp.plan_fingerprint(source["portableIntent"])
        older = copy.deepcopy(source)
        older["schemaVersion"] = 999 if unsupported_stage else 3
        older["environmentFingerprint"] = "b" * 64
        older["evidence"]["olderStageInput"] = older_reference
        reseal(older, "planFingerprint")
        older_plan = root / "older-staging-plan.json"
        common.save(older_plan, older)
        stage = copy.deepcopy(source)
        stage["environmentFingerprint"] = "c" * 64
        stage["evidence"]["stageOnlyInput"] = stage_reference
        stage["evidence"]["stagingApplicationPlan"] = {
            "path": str(older_plan), "sha256": bp._hash(older_plan),
        }
        reseal(stage, "planFingerprint")
        stage_plan = root / "staging-application-plan.json"
        common.save(stage_plan, stage)
        source["evidence"]["stagingApplicationPlan"] = {"path": str(stage_plan), "sha256": bp._hash(stage_plan)}
    bind_mutable_inputs(f, extend_source=extend)
    return note_reference, stage_reference, older_reference


def test_nested_decision_and_recursive_staging_inputs_survive_working_file_updates(batch):
    f = batch
    references = nested_application_inputs(f)
    original = {row["path"]: bp.check_file(row, f["root"]).read_bytes() for row in references}
    _, archived, chain, chain_path = first_chain(f)
    for row in references:
        assert rl.resolve_archived_reference(archived, row, root=f["root"],
                                             evidence_key=EKEY).read_bytes() == original[row["path"]]
        bp.check_file(row, f["root"]).write_bytes(b"legitimate later input update")
    assert rl.load_lineage(chain_path, root=f["root"], evidence_key=EKEY, operator_key=OKEY)[0] == chain
    assert all(entry["sha256"] != "0" * 64 for entry in archived["relocations"])
    assert not (f["root"] / ("0" * 64)).exists()


def test_referenced_staging_plan_with_unknown_schema_is_not_silently_skipped(batch):
    f = batch
    nested_application_inputs(f, unsupported_stage=True)
    execution = common.execute(f)
    slots = rl.paths(f["root"], f["runtime"].target)
    with pytest.raises(bp.PromotionError, match="unsupported schema"):
        archive(f, execution)
    assert slots["original"].exists() and slots["journal"].exists()


def ancestor_overlap_gap(f, *, binding_key="canonical", direct=False, available=False,
                         ancestor_mode="staging-apply-plan"):
    root = f["root"]
    missing = {"path": str(root / "normalized" / "canonical" / "transactions.csv"),
               "sha256": hashlib.sha256(b"synthetic retired overlap CSV, never a bank source\n").hexdigest()}
    if available:
        missing["sha256"] = bp._hash(root / "normalized" / "canonical" / "transactions.csv")
    ancestor = {}
    def extend(source):
        stage = copy.deepcopy(source)
        stage["mode"] = ancestor_mode
        stage["environmentFingerprint"] = "d" * 64
        stage["evidence"][binding_key] = missing
        reseal(stage, "planFingerprint")
        stage_path = root / "gap-ancestor-staging-plan.json"
        common.save(stage_path, stage)
        ancestor.update(path=str(stage_path), sha256=bp._hash(stage_path))
        source["evidence"]["stagingApplicationPlan"] = dict(ancestor)
        if direct:
            source["evidence"][binding_key] = missing
    bind_mutable_inputs(f, extend_source=extend)
    return missing, ancestor


def gap_acknowledgment(f, execution, missing, ancestor, *, binding_key="canonical"):
    return bp.seal({
        "kind": rl.REPLAY_GAP_KIND, "schemaVersion": 1, "approval": rl.REPLAY_GAP_APPROVAL,
        "executionHash": execution["documentHash"], "targetHash": bp.plan_fingerprint(f["runtime"].target),
        "verificationScope": "historical-matching-replay-only",
        "operator": "Synthetic historical reviewer",
        "reason": "Retired canonical overlap input unavailable; applied effects and current evidence retained",
        "issuedAt": datetime.now(timezone.utc).isoformat(),
        "missingBindings": [{
            **missing, "ancestorPlan": ancestor,
            "bindingRole": f"schema3-staging-application-plan.evidence.{binding_key}",
        }],
    }, OKEY)


def test_qualified_ancestor_gap_is_explicit_and_flows_into_next_plan_and_review(batch, monkeypatch):
    f = batch
    missing, ancestor = ancestor_overlap_gap(f)
    execution = common.execute(f)
    with pytest.raises(rl.HistoricalInputUnavailable):
        archive(f, execution)
    acknowledgement = gap_acknowledgment(f, execution, missing, ancestor)
    receipt = archive(f, execution, replay_gap=acknowledgement)
    assert receipt["schemaVersion"] == 3
    assert receipt["historicalMatchingReplayAvailable"] is False
    assert receipt["historicalInputClosureVerified"] is False
    assert len(receipt["missingBindings"]) == 1
    assert all(row["sha256"] != missing["sha256"] for row in receipt["relocations"])
    assert not (f["root"] / "history" / execution["documentHash"] / "blobs" / missing["sha256"]).exists()
    chain = rl.build_lineage(root=f["root"], target=f["runtime"].target, evidence_key=EKEY, operator_key=OKEY)
    assert chain["schemaVersion"] == 2 and chain["historicalMatchingReplayAvailable"] is False
    assert chain["missingBindings"][0]["executionHash"] == execution["documentHash"]
    chain_path = f["root"] / "qualified-chain.json"
    bp.write(chain_path, chain)
    plan, artifacts, manifest = next_plan(f, chain_path, monkeypatch)
    assert plan["productionLineage"]["schemaVersion"] == 2
    assert plan["productionLineage"]["historicalMatchingReplayAvailable"] is False
    assert plan["productionLineage"]["historicalReplayQualificationHash"] == chain["historicalReplayQualificationHash"]
    preparation, next_authorization = second_preparation(f, plan, artifacts, manifest)
    assert preparation["historicalMatchingReplayAvailable"] is False
    assert preparation["missingHistoricalBindings"] == chain["missingBindings"]
    review_path = f["root"] / "review-2.json"
    original_review = bp.load(review_path)
    review = bp.unseal(original_review, EKEY, bp.KIND + "-review")
    for key in bp.QUALIFIED_REVIEW_BINDINGS:
        review.pop(key)
    common.save(review_path, bp.seal(review, EKEY))
    with pytest.raises(bp.PromotionError, match="explicitly acknowledge"):
        bp.prepare(root=f["root"], request=preparation["request"], runtime=f["runtime"],
                   evidence_key=EKEY, operator_key=OKEY)
    common.save(review_path, original_review)
    next_execution = bp.execute(
        root=f["root"], document=preparation, authorization=next_authorization,
        supplied_preparation_id=preparation["documentHash"], runtime=f["runtime"],
        evidence_key=EKEY, operator_key=OKEY,
    )
    assert next_execution["phase"] == "completed"
    assert next_execution["preparation"]["historicalMatchingReplayAvailable"] is False
    with pytest.raises(rl.HistoricalInputUnavailable):
        archive(f, next_execution)  # Qualification never becomes an implicit global skip.
    next_ack = gap_acknowledgment(f, next_execution, missing, ancestor)
    archive(f, next_execution, replay_gap=next_ack)
    continued = rl.build_lineage(root=f["root"], target=f["runtime"].target,
                                 evidence_key=EKEY, operator_key=OKEY)
    assert len(continued["steps"]) == 2 and continued["historicalMatchingReplayAvailable"] is False


@pytest.mark.parametrize("change", ["execution", "target", "ancestor", "input-hash", "role", "scope", "signature"])
def test_gap_acknowledgment_cannot_cover_a_different_binding(batch, change):
    f = batch
    missing, ancestor = ancestor_overlap_gap(f)
    execution = common.execute(f)
    acknowledgement = gap_acknowledgment(f, execution, missing, ancestor)
    body = bp.unseal(acknowledgement, OKEY, rl.REPLAY_GAP_KIND)
    if change == "execution":
        body["executionHash"] = "0" * 64
    elif change == "target":
        body["targetHash"] = "0" * 64
    elif change == "ancestor":
        body["missingBindings"][0]["ancestorPlan"]["sha256"] = "0" * 64
    elif change == "input-hash":
        body["missingBindings"][0]["sha256"] = "0" * 64
    elif change == "role":
        body["missingBindings"][0]["bindingRole"] = "schema3-staging-application-plan.evidence.snapshot"
    elif change == "scope":
        body["verificationScope"] = "skip-any-missing-input"
    acknowledgement = bp.seal(body, OKEY)
    if change == "signature":
        acknowledgement["signature"] = "0" * 64
    stops = f["runtime"].stops
    with pytest.raises(bp.PromotionError):
        archive(f, execution, replay_gap=acknowledgement)
    assert f["runtime"].stops == stops
    assert rl.paths(f["root"], f["runtime"].target)["original"].exists()


@pytest.mark.parametrize("role", ["snapshot", "reviewedPlan", "rebuildAccountMap", "metadataRemediations"])
def test_money_or_noncanonical_ancestor_roles_cannot_be_qualified(batch, role):
    f = batch
    missing, ancestor = ancestor_overlap_gap(f, binding_key=role)
    execution = common.execute(f)
    # Even claiming the permissible role does not turn the actual material role into it.
    acknowledgement = gap_acknowledgment(f, execution, missing, ancestor)
    with pytest.raises(rl.HistoricalInputUnavailable):
        archive(f, execution, replay_gap=acknowledgement)


def test_direct_application_binding_remains_mandatory_even_if_ancestor_also_uses_it(batch):
    f = batch
    missing, ancestor = ancestor_overlap_gap(f, direct=True)
    execution = common.execute(f)
    acknowledgement = gap_acknowledgment(f, execution, missing, ancestor)
    with pytest.raises(rl.HistoricalInputUnavailable):
        archive(f, execution, replay_gap=acknowledgement)


@pytest.mark.parametrize("material", [
    "normalized/canonical/transactions.csv", "original.db", "candidate.db",
    "receipt.json", "recovery.json", "synthetic-ofx-source.ofx",
])
def test_gap_cannot_excuse_missing_current_canonical_or_financial_backup(batch, material):
    f = batch
    missing, ancestor = ancestor_overlap_gap(f)
    execution = common.execute(f)
    acknowledgement = gap_acknowledgment(f, execution, missing, ancestor)
    (f["root"] / material).unlink()
    with pytest.raises(rl.HistoricalInputUnavailable):
        archive(f, execution, replay_gap=acknowledgement)
    assert rl.paths(f["root"], f["runtime"].target)["journal"].exists()


def test_available_ancestor_input_cannot_be_intentionally_skipped(batch):
    f = batch
    missing, ancestor = ancestor_overlap_gap(f, available=True)
    execution = common.execute(f)
    acknowledgement = gap_acknowledgment(f, execution, missing, ancestor)
    with pytest.raises(bp.PromotionError, match="unused, available"):
        archive(f, execution, replay_gap=acknowledgement)


def test_nonstaging_application_mode_cannot_claim_an_auxiliary_staging_gap(batch):
    f = batch
    missing, ancestor = ancestor_overlap_gap(f, ancestor_mode="production-promotion-plan")
    execution = common.execute(f)
    acknowledgment = gap_acknowledgment(f, execution, missing, ancestor)
    with pytest.raises(rl.HistoricalInputUnavailable):
        archive(f, execution, replay_gap=acknowledgment)
