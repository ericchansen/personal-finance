from __future__ import annotations

import copy
import hashlib
import inspect
import json
from collections import Counter
from pathlib import Path

import jsonschema
import pytest

from importers.lineage_review import canonical, cli, workflow
from importers.lineage_review.model import ReviewError, decision_id, json_bytes
from importers.audit import baseline


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def synthetic_activity(label: str, family: str, *, dependent: bool = False) -> dict:
    ref = digest(f"activity:{label}")
    return {
        "activityRef": ref,
        "activityId": f"synthetic-{label}",
        "canonicalAccountId": "acct-synthetic",
        "sourceAtUtc": "2026-01-15T12:00:00Z",
        "sourceDateUtc": "2026-01-15",
        "signedEffect": "-10.00",
        "scopeReason": None,
        "sourceFamily": family,
        "sourceIdentity": f"{family}:{label}",
        "description": f"PRIVATE SYNTHETIC DESCRIPTION {label}",
        "normalizedDescription": f"private synthetic description {label}",
        "transferGroup": None,
        "reconciliation": False,
        "observationStatus": "candidate",
        "candidateGroupIds": [],
        "dependentState": {
            "reasonCodes": ["metadata"] if dependent else [],
            "wouldLoseOrCascade": dependent,
            "transferCounterpart": None,
            "categoryAssignments": [],
            "splits": [],
            "spendingEventLinks": [],
            "userNote": None,
            "metadata": {"synthetic": True} if dependent else None,
            "projectionImportRunIds": [],
        },
        "lineage": {
            "status": "receipt-proven",
            "applicationPlanSha256": digest("plan-evidence"),
            "receiptSha256": digest("receipt-evidence"),
            "receiptProvenReconciliation": False,
            "reconciliation": None,
        },
        "raw": {
            "id": f"synthetic-{label}",
            "accountId": "acct-synthetic",
            "idempotencyKey": f"{family}:{label}",
            "amount": "10.00",
            "comment": f"PRIVATE SYNTHETIC DESCRIPTION {label}",
        },
    }


def synthetic_source(tmp_path: Path) -> dict:
    specs = [
        ("extract-left", "extract", False),
        ("simplefin-right", "simplefin", True),
        ("other-left", "monarch", False),
        ("other-right", "simplefin", False),
        ("link-left", "monarch", True),
        ("link-right", "monarch", False),
        ("one-left", "extract", False),
        ("one-right-a", "simplefin", False),
        ("one-right-b", "simplefin", False),
        ("many-left-a", "extract", False),
        ("many-left-b", "extract", False),
        ("many-right-a", "simplefin", False),
        ("many-right-b", "simplefin", False),
        ("binding", "simplefin", False),
    ]
    activities = {
        item["activityRef"]: item
        for item in (
            synthetic_activity(label, family, dependent=dependent)
            for label, family, dependent in specs
        )
    }
    by_label = {
        item["activityId"].removeprefix("synthetic-"): item
        for item in activities.values()
    }
    groups = []
    edges = []

    def add_group(
        name: str,
        labels: list[str],
        cardinality: str,
        reasons: list[str],
    ) -> None:
        refs = sorted(by_label[label]["activityRef"] for label in labels)
        group_id = digest(f"group:{name}")
        families = Counter(by_label[label]["sourceFamily"] for label in labels)
        groups.append(
            {
                "groupId": group_id,
                "classification": "review-required",
                "cardinality": cardinality,
                "ambiguityCardinality": len(refs),
                "activityRefs": refs,
                "sourceFamilyCounts": dict(sorted(families.items())),
                "reasonCodes": sorted(reasons),
                "edgeCount": len(refs) - 1,
            }
        )
        for index, right in enumerate(refs[1:], 1):
            left = refs[0]
            pair = sorted(
                (
                    activities[left]["sourceFamily"],
                    activities[right]["sourceFamily"],
                )
            )
            edges.append(
                {
                    "candidateId": digest(f"edge:{name}:{index}"),
                    "groupId": group_id,
                    "leftActivityRef": left,
                    "rightActivityRef": right,
                    "tier": 1,
                    "classification": "review-required",
                    "reasonCodes": sorted(reasons),
                    "sourceFamilyPair": "-".join(pair),
                    "ambiguityCardinality": len(refs),
                }
            )
        for ref in refs:
            activities[ref]["candidateGroupIds"] = [group_id]

    add_group(
        "extract-simplefin",
        ["extract-left", "simplefin-right"],
        "one-to-one",
        ["exact-cross-source"],
    )
    add_group(
        "other",
        ["other-left", "other-right"],
        "one-to-one",
        ["provider-description-similar"],
    )
    add_group(
        "link",
        ["link-left", "link-right"],
        "one-to-one",
        ["linked-transfer", "transfer-candidate"],
    )
    add_group(
        "one-many",
        ["one-left", "one-right-a", "one-right-b"],
        "one-to-many",
        ["bounded-date-equal-amount"],
    )
    add_group(
        "many",
        ["many-left-a", "many-left-b", "many-right-a", "many-right-b"],
        "many-to-many",
        ["bounded-date-equal-amount"],
    )

    binding = by_label["binding"]
    binding["lineage"]["status"] = "ambiguous"
    binding["lineage"]["applicationPlanSha256"] = None
    binding["lineage"]["receiptSha256"] = None
    reconciliation = {
        "activityId": "synthetic-gap",
        "rollbackPayload": {"amount": "10.00", "type": "synthetic"},
    }
    plan_hash = digest("ambiguous-plan")
    plan_value = {
        "environmentFingerprint": digest("environment"),
        "planFingerprint": digest("plan-fingerprint"),
        "intentFingerprint": digest("intent"),
        "ledgerFingerprint": digest("pre-ledger"),
        "expectedPostLedgerFingerprint": digest("post-ledger"),
        "decisionEvidence": None,
        "operations": {
            "creates": [
                {
                    "accountId": "acct-synthetic",
                    "idempotencyKey": binding["sourceIdentity"],
                }
            ],
            "updates": [],
            "deleteIds": [],
            "metadataFinalizations": [],
        },
        "reconciliations": [reconciliation],
    }
    plan = {"sha256": plan_hash, "value": plan_value}

    def receipt(label: str) -> dict:
        return {
            "sha256": digest(label),
            "value": {
                "applicationPlanSha256": plan_hash,
                "planFingerprint": plan_value["planFingerprint"],
                "intentFingerprint": plan_value["intentFingerprint"],
                "environmentFingerprint": plan_value["environmentFingerprint"],
                "preLedgerFingerprint": plan_value["ledgerFingerprint"],
                "postLedgerFingerprint": (
                    digest("conflicting-post-ledger")
                    if label == "receipt-b"
                    else plan_value["expectedPostLedgerFingerprint"]
                ),
                "decisionEvidence": None,
                "operations": {
                    key: len(value)
                    for key, value in plan_value["operations"].items()
                },
                "reconciliations": [reconciliation],
            },
        }

    inputs = [
        {
            "path": "synthetic/plan.json",
            "kind": "plan",
            "size": 10,
            "sha256": plan_hash,
        },
        {
            "path": "synthetic/receipt-a.json",
            "kind": "receipt",
            "size": 10,
            "sha256": digest("receipt-a"),
        },
        {
            "path": "synthetic/receipt-b.json",
            "kind": "receipt",
            "size": 10,
            "sha256": digest("receipt-b"),
        },
        {
            "path": "synthetic/rollback.json",
            "kind": "backup-manifest",
            "size": 10,
            "sha256": digest("rollback-evidence"),
            "coversCandidateGroupIds": [
                group["groupId"] for group in groups
            ],
        },
    ]
    root = tmp_path / "private"
    repo = tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    return {
        "root": root,
        "repoRoot": repo,
        "publicationPath": root / "audit" / "duplicates" / "synthetic",
        "baselinePublicationPath": root / "audit" / "baselines" / "synthetic",
        "forensicPublicationId": digest("forensic-publication"),
        "baselinePublicationId": digest("baseline-publication"),
        "environmentFingerprint": digest("environment"),
        "forensicCandidateGraphHash": digest("forensic-graph"),
        "detail": {
            "activities": list(reversed(list(activities.values()))),
            "candidateGroups": list(reversed(groups)),
            "candidateEdges": list(reversed(edges)),
        },
        "summary": {"candidateGraphSha256": digest("forensic-graph")},
        "inputs": inputs,
        "plans": [plan],
        "receipts": [receipt("receipt-b"), receipt("receipt-a")],
    }


def reviewed_decision(
    queue: dict,
    group: dict,
    decision_type: str,
) -> dict:
    value = workflow._decision_template(group, queue)
    value.update(
        {
            "decisionType": decision_type,
            "reviewer": "synthetic-reviewer",
            "decidedAt": "2026-01-16T10:30:00Z",
            "rationale": "Synthetic evidence was reviewed for this exact candidate graph.",
            "evidenceHashes": [queue["evidenceCatalog"][0]["sha256"]],
        }
    )
    if decision_type in {
        "same-economic-event",
        "account-handoff-reissue",
        "source-error-mirror",
    }:
        value["survivorActivityRef"] = group["activityRefs"][0]
        value["rollbackEvidenceHashes"] = [digest("rollback-evidence")]
        value["evidenceHashes"] = sorted(
            {*value["evidenceHashes"], digest("rollback-evidence")}
        )
    if decision_type == "linked-transfer":
        value["linkedActivityRefs"] = group["activityRefs"]
    if decision_type == "receipt-binding-resolution":
        candidates = group["receiptBindingCandidates"]
        selected = candidates[0]
        value["receiptBinding"] = {
            "applicationPlanSha256": selected["applicationPlanSha256"],
            "receiptSha256": selected["receiptSha256"],
        }
        value["evidenceHashes"] = sorted(value["receiptBinding"].values())
        if selected["reconciliationFingerprints"]:
            value["reconciliationBinding"] = {
                "fingerprints": selected["reconciliationFingerprints"]
            }
    elif group["reconciliationRequired"]:
        value["reconciliationBinding"] = {
            "fingerprints": group["reconciliationFingerprints"]
        }
    return value


def decision_import_document(queue: dict, decisions: list[dict]) -> dict:
    return {
        "schemaVersion": 1,
        "kind": "lineage-review-decisions",
        "environmentFingerprint": queue["environmentFingerprint"],
        "baselinePublicationId": queue["baselinePublicationId"],
        "forensicPublicationId": queue["forensicPublicationId"],
        "candidateGraphHash": queue["candidateGraphHash"],
        "queueInputHash": queue["queueInputHash"],
        "evidenceFiles": [],
        "decisions": decisions,
    }


def decision_input_path(root: Path, name: str) -> Path:
    path = root / workflow.DECISION_INPUT_RELATIVE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_decision_input_must_use_baseline_excluded_inbox(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    path = root / "review-input.json"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ReviewError, match="decision-input-location-invalid"):
        workflow._load_decision_input(root, tmp_path / "repo", path)


def mock_verified_source(monkeypatch, source: dict) -> None:
    monkeypatch.setattr(
        workflow, "_load_verified_forensic", lambda *_args: source
    )
    monkeypatch.setattr(
        workflow, "_assert_source_unchanged", lambda _source: None
    )


@pytest.mark.parametrize(
    ("decision_type", "priority"),
    [
        ("same-economic-event", "extract-simplefin-one-to-one"),
        ("distinct-events", "other-one-to-one"),
        ("linked-transfer", "linked-transfer"),
        ("account-handoff-reissue", "one-to-many"),
        ("defer-insufficient-evidence", "many-to-many"),
        ("source-error-mirror", "other-one-to-one"),
        (
            "receipt-binding-resolution",
            "ambiguous-receipt-reconciliation",
        ),
    ],
)
def test_every_decision_type_validates(tmp_path, decision_type, priority):
    queue = workflow._prepare_queue(synthetic_source(tmp_path), 2)
    group = next(item for item in queue["groups"] if item["priorityCode"] == priority)
    decision = reviewed_decision(queue, group, decision_type)

    assert workflow._validate_decision_set(queue, [decision], []) == [decision]


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("environmentFingerprint", digest("foreign"), "foreign-environment"),
        ("baselinePublicationId", digest("stale-baseline"), "baseline-publication-stale"),
        ("forensicPublicationId", digest("stale-forensic"), "forensic-publication-stale"),
        ("candidateGraphHash", digest("stale-graph"), "candidate-graph-stale"),
        ("candidateGroupHash", digest("stale-group"), "candidate-group-stale"),
        ("cardinality", "many-to-many", "candidate-cardinality-changed"),
        ("reasonCodes", ["bounded-date-equal-amount"], "candidate-reasons-changed"),
        ("sourceFamilyCounts", {"unknown": 2}, "candidate-source-families-changed"),
        ("memberActivityFingerprints", [], "candidate-members-changed"),
        (
            "expectedDependentStateFingerprints",
            [],
            "dependent-state-drift",
        ),
    ],
)
def test_stale_or_tampered_decision_fails_closed(
    tmp_path, field, replacement, message
):
    queue = workflow._prepare_queue(synthetic_source(tmp_path), 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    decision = reviewed_decision(queue, group, "same-economic-event")
    decision[field] = replacement

    with pytest.raises(ReviewError, match=message):
        workflow._validate_decision_set(queue, [decision], [])


def test_survivor_linked_members_and_review_metadata_fail_closed(tmp_path):
    queue = workflow._prepare_queue(synthetic_source(tmp_path), 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    same = reviewed_decision(queue, group, "same-economic-event")
    same["survivorActivityRef"] = digest("outsider")
    with pytest.raises(ReviewError, match="survivor-outside-group"):
        workflow._validate_decision_set(queue, [same], [])

    linked_group = next(
        item for item in queue["groups"] if item["priorityCode"] == "linked-transfer"
    )
    linked = reviewed_decision(queue, linked_group, "linked-transfer")
    linked["linkedActivityRefs"] = linked_group["activityRefs"][:1]
    with pytest.raises(ReviewError, match="linked-member-omission"):
        workflow._validate_decision_set(queue, [linked], [])

    incomplete = reviewed_decision(queue, group, "distinct-events")
    incomplete["reviewer"] = "TBD"
    incomplete["rationale"] = ""
    with pytest.raises(ReviewError, match="review-metadata-incomplete"):
        workflow._validate_decision_set(queue, [incomplete], [])


def test_duplicate_and_conflicting_claims_fail_closed(tmp_path):
    queue = workflow._prepare_queue(synthetic_source(tmp_path), 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    first = reviewed_decision(queue, group, "distinct-events")
    second = reviewed_decision(queue, group, "same-economic-event")
    with pytest.raises(ReviewError, match="conflicting-decisions"):
        workflow._validate_decision_set(queue, [first, second], [])

    clone = copy.deepcopy(group)
    clone["groupId"] = digest("overlapping-group")
    clone["decisionId"] = decision_id(clone["groupId"])
    clone["candidateGroupHash"] = digest("overlapping-group-snapshot")
    overlapping_queue = {**queue, "groups": [group, clone]}
    clone_decision = reviewed_decision(
        overlapping_queue, clone, "distinct-events"
    )
    with pytest.raises(ReviewError, match="duplicate-member-claim"):
        workflow._validate_decision_set(
            overlapping_queue, [first, clone_decision], []
        )


def test_queue_batches_are_deterministic_and_priority_partitioned(tmp_path):
    source = synthetic_source(tmp_path)
    first = workflow._prepare_queue(source, 2)
    source["detail"]["activities"].reverse()
    source["detail"]["candidateGroups"].reverse()
    source["detail"]["candidateEdges"].reverse()
    source["receipts"].reverse()
    second = workflow._prepare_queue(source, 2)

    assert first == second
    assert Counter(item["priorityCode"] for item in first["groups"]) == {
        "extract-simplefin-one-to-one": 1,
        "other-one-to-one": 1,
        "linked-transfer": 1,
        "one-to-many": 1,
        "many-to-many": 1,
        "ambiguous-receipt-reconciliation": 1,
    }
    assert all(
        len(
            {
                next(
                    group["priorityCode"]
                    for group in first["groups"]
                    if group["groupId"] == group_id
                )
                for group_id in batch["groupIds"]
            }
        )
        == 1
        for batch in first["batches"]
    )


def test_exact_source_identity_is_still_queued_for_review(tmp_path):
    source = synthetic_source(tmp_path)
    group = source["detail"]["candidateGroups"][0]
    group["classification"] = "automatic-duplicate"
    group["reasonCodes"] = ["exact-source-identity"]
    for edge in source["detail"]["candidateEdges"]:
        if edge["groupId"] == group["groupId"]:
            edge["classification"] = "automatic-duplicate"
            edge["reasonCodes"] = ["exact-source-identity"]

    queue = workflow._prepare_queue(source, 2)
    queued = next(item for item in queue["groups"] if item["groupId"] == group["groupId"])

    assert queued["forensicClassification"] == "automatic-duplicate"
    assert queued["reasonCodes"] == ["exact-source-identity"]


def test_transfer_only_groups_are_not_duplicate_review_work(tmp_path):
    source = synthetic_source(tmp_path)
    group = next(
        item
        for item in source["detail"]["candidateGroups"]
        if "linked-transfer" in item["reasonCodes"]
    )
    group["classification"] = "relationship-only"
    for edge in source["detail"]["candidateEdges"]:
        if edge["groupId"] == group["groupId"]:
            edge["classification"] = "relationship-only"

    queue = workflow._prepare_queue(source, 2)

    assert group["groupId"] not in {
        item["groupId"] for item in queue["groups"]
    }


def test_multiple_matching_plans_create_one_resolvable_binding_group(tmp_path):
    source = synthetic_source(tmp_path)
    first_plan = source["plans"][0]
    first_receipt = source["receipts"][0]
    second_plan = copy.deepcopy(first_plan)
    second_plan["sha256"] = digest("second-ambiguous-plan")
    second_receipt = copy.deepcopy(first_receipt)
    second_receipt["sha256"] = digest("second-plan-receipt")
    second_receipt["value"]["applicationPlanSha256"] = second_plan["sha256"]
    source["plans"] = [first_plan, second_plan]
    source["receipts"] = [first_receipt, second_receipt]
    source["inputs"].extend(
        [
            {
                "path": "synthetic/plan-2.json",
                "kind": "plan",
                "size": 10,
                "sha256": second_plan["sha256"],
            },
            {
                "path": "synthetic/receipt-2.json",
                "kind": "receipt",
                "size": 10,
                "sha256": second_receipt["sha256"],
            },
        ]
    )

    queue = workflow._prepare_queue(source, 2)
    bindings = [
        item for item in queue["groups"] if item["reviewKind"] == "receipt-binding"
    ]

    assert len(bindings) == 1
    assert {
        item["applicationPlanSha256"]
        for item in bindings[0]["receiptBindingCandidates"]
    } == {first_plan["sha256"], second_plan["sha256"]}


def test_multiple_plan_ambiguity_with_one_receipt_can_be_resolved(tmp_path):
    source = synthetic_source(tmp_path)
    first_plan = source["plans"][0]
    first_receipt = source["receipts"][0]
    second_plan = copy.deepcopy(first_plan)
    second_plan["sha256"] = digest("unproved-second-plan")
    source["plans"] = [first_plan, second_plan]
    source["receipts"] = [first_receipt]
    source["inputs"].append(
        {
            "path": "synthetic/unproved-plan.json",
            "kind": "plan",
            "size": 10,
            "sha256": second_plan["sha256"],
        }
    )

    queue = workflow._prepare_queue(source, 2)
    binding = next(
        item for item in queue["groups"] if item["reviewKind"] == "receipt-binding"
    )
    decision = reviewed_decision(
        queue, binding, "receipt-binding-resolution"
    )

    assert len(binding["applicationPlanCandidates"]) == 2
    assert len(binding["receiptBindingCandidates"]) == 1
    assert workflow._validate_decision_set(queue, [decision], []) == [decision]


def test_duplicate_plan_and_receipt_files_do_not_duplicate_candidates(tmp_path):
    source = synthetic_source(tmp_path)
    source["plans"].append(copy.deepcopy(source["plans"][0]))
    source["receipts"].append(copy.deepcopy(source["receipts"][0]))

    queue = workflow._prepare_queue(source, 2)
    binding = next(
        item for item in queue["groups"] if item["reviewKind"] == "receipt-binding"
    )

    assert len(binding["receiptBindingCandidates"]) == 2
    decision = reviewed_decision(
        queue, binding, "receipt-binding-resolution"
    )
    assert workflow._validate_decision_set(queue, [decision], []) == [decision]


def test_single_conflicting_receipt_still_creates_a_binding_group(tmp_path):
    source = synthetic_source(tmp_path)
    source["receipts"] = [source["receipts"][0]]

    queue = workflow._prepare_queue(source, 2)
    binding = next(
        item for item in queue["groups"] if item["reviewKind"] == "receipt-binding"
    )

    assert len(binding["applicationPlanCandidates"]) == 1
    assert len(binding["receiptBindingCandidates"]) == 1


def test_private_packets_and_shareable_output_respect_privacy(
    tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path)
    sentinel = source["root"] / "sealed-input.json"
    sentinel.write_text('{"synthetic":"unchanged"}\n', encoding="utf-8")
    sentinel_hash = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    mock_verified_source(monkeypatch, source)
    result = workflow.build(
        source["root"], repo_root=source["repoRoot"], batch_size=2
    )
    publication = (
        source["root"]
        / "audit"
        / "lineage-review"
        / "publications"
        / result["queuePublicationId"]
    )
    packet_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in publication.rglob("*")
        if path.is_file() and path.parent.name == "packets"
    )
    summary_text = (publication / "summary.json").read_text(encoding="utf-8")
    console = cli._summary(result)

    assert "PRIVATE SYNTHETIC DESCRIPTION" in packet_text
    assert "PRIVATE SYNTHETIC DESCRIPTION" not in summary_text
    assert "PRIVATE SYNTHETIC DESCRIPTION" not in console
    assert str(source["root"]) not in console
    assert not (source["repoRoot"] / "audit").exists()
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == sentinel_hash


def test_lineage_publications_are_excluded_from_future_baselines(tmp_path):
    root = tmp_path / "private"
    source = root / "synthetic-source.json"
    lineage = root / "audit" / "lineage-review" / "current.json"
    lock = root / "audit" / ".lineage-state" / ".import.lock"
    canonical_output = root / "normalized" / "canonical" / "manifest.json"
    canonical_sibling = (
        root / "normalized" / "canonical-archive" / "manifest.json"
    )
    source.parent.mkdir(parents=True)
    source.write_text('{"synthetic":true}\n', encoding="utf-8")
    lineage.parent.mkdir(parents=True)
    lineage.write_text('{"private":true}\n', encoding="utf-8")
    lock.parent.mkdir(parents=True)
    lock.write_text("synthetic lock\n", encoding="utf-8")
    canonical_output.parent.mkdir(parents=True)
    canonical_output.write_text('{"schemaVersion":5}\n', encoding="utf-8")
    canonical_sibling.parent.mkdir(parents=True)
    canonical_sibling.write_text('{"synthetic":"archive"}\n', encoding="utf-8")

    inventory = baseline.inventory_evidence(root)

    assert any(item["path"] == "synthetic-source.json" for item in inventory)
    assert not any(
        item["path"].startswith("audit/lineage-review")
        for item in inventory
    )
    assert not any(
        item["path"].startswith("audit/.lineage-state")
        for item in inventory
    )
    assert not any(
        item["path"].startswith("normalized/canonical")
        for item in inventory
        if not item["path"].startswith("normalized/canonical-archive")
    )
    sibling = next(
        item
        for item in inventory
        if item["path"] == "normalized/canonical-archive/manifest.json"
    )
    assert sibling["kind"] == "other-evidence"


def test_sealed_legacy_canonical_reference_does_not_create_dependency_cycle(
    tmp_path,
):
    root = tmp_path / "private"
    root.mkdir()
    entry = {
        "path": "normalized/canonical/manifest.json",
        "kind": "canonical-publication",
        "size": 12,
        "sha256": digest("sealed-predecessor"),
        "omitted": False,
    }

    inputs, plans, receipts = workflow.forensic._read_evidence(
        root, [entry], digest("environment")
    )

    assert inputs == [
        {key: entry[key] for key in ("path", "kind", "size", "sha256")}
    ]
    assert plans == []
    assert receipts == []


def test_sealed_legacy_shadow_plan_does_not_create_dependency_cycle(tmp_path):
    root = tmp_path / "private"
    path = root / "postgres-shadow" / "plans" / "synthetic.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"schemaVersion":3,"mode":"production-promotion-plan"}\n',
        encoding="utf-8",
    )
    entry = {
        "path": path.relative_to(root).as_posix(),
        "kind": "plan",
        "size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "omitted": False,
    }

    inputs, plans, receipts = workflow.forensic._read_evidence(
        root, [entry], digest("environment")
    )

    assert inputs == [
        {key: entry[key] for key in ("path", "kind", "size", "sha256")}
    ]
    assert plans == []
    assert receipts == []


def test_legacy_shadow_hash_cannot_satisfy_decision_readiness(tmp_path):
    source = synthetic_source(tmp_path)
    shadow_hash = digest("nonexistent-shadow-plan")
    source["inputs"].append(
        {
            "path": "postgres-shadow/plans/synthetic.json",
            "kind": "plan",
            "size": 999,
            "sha256": shadow_hash,
        }
    )

    queue = workflow._prepare_queue(source, 2)

    assert shadow_hash not in {
        item["sha256"] for item in queue["evidenceCatalog"]
    }


def test_legacy_canonical_hash_cannot_satisfy_decision_readiness(tmp_path):
    source = synthetic_source(tmp_path)
    canonical_hash = digest("nonexistent-canonical-predecessor")
    source["inputs"].append(
        {
            "path": "normalized/canonical/manifest.json",
            "kind": "canonical-publication",
            "size": 999,
            "sha256": canonical_hash,
        }
    )
    queue = workflow._prepare_queue(source, 2)
    assert canonical_hash not in {
        item["sha256"] for item in queue["evidenceCatalog"]
    }
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "linked-transfer"
    )
    decision = reviewed_decision(queue, group, "linked-transfer")
    decision["evidenceHashes"] = [canonical_hash]

    with pytest.raises(ReviewError, match="decision-evidence-invalid"):
        workflow._validate_decision_set(queue, [decision], [])
    readiness, _gaps = workflow._readiness(queue, [])
    assert readiness["surgical-eligible-groups"] == 0
    assert readiness["rebuild-eligible-groups"] == 0

    independent_hash = next(
        item["sha256"]
        for item in queue["evidenceCatalog"]
        if item["kind"] in {"plan", "receipt"}
    )
    decision["evidenceHashes"] = [independent_hash]
    validated = workflow._validate_decision_set(queue, [decision], [])
    readiness, _gaps = workflow._readiness(queue, validated)
    assert readiness["ready-groups"] == 1
    assert readiness["surgical-eligible-groups"] == 1
    assert readiness["rebuild-eligible-groups"] == 1


def test_decision_import_verify_status_and_tamper_detection(tmp_path, monkeypatch):
    source = synthetic_source(tmp_path)
    mock_verified_source(monkeypatch, source)
    built = workflow.build(
        source["root"], repo_root=source["repoRoot"], batch_size=2
    )
    queue = workflow._prepare_queue(source, 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    decision = reviewed_decision(queue, group, "same-economic-event")
    input_path = decision_input_path(source["root"], "review-input.json")
    input_path.write_bytes(
        json_bytes(decision_import_document(queue, [decision]))
    )

    imported = workflow.import_decisions(
        source["root"], input_path, repo_root=source["repoRoot"]
    )
    verified = workflow.verify(source["root"], repo_root=source["repoRoot"])
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (source["root"] / "audit" / "lineage-review").rglob("*")
        if path.is_file()
    }
    reported = workflow.status(source["root"], repo_root=source["repoRoot"])
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in before
    }

    assert imported["counts"]["reviewed-decisions"] == 1
    assert imported["counts"]["unresolved-groups"] == 5
    assert verified == reported
    assert before == after
    assert built["queuePublicationId"] == imported["queuePublicationId"]

    queue_publication = (
        source["root"]
        / "audit"
        / "lineage-review"
        / "publications"
        / built["queuePublicationId"]
    )
    packet = next((queue_publication / "packets").glob("*.json"))
    packet.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ReviewError, match="private-output-hash-mismatch"):
        workflow.verify(source["root"], repo_root=source["repoRoot"])


def test_decision_history_is_idempotent_chained_and_rollback_protected(
    tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path)
    mock_verified_source(monkeypatch, source)
    workflow.build(source["root"], repo_root=source["repoRoot"], batch_size=2)
    queue = workflow._prepare_queue(source, 2)
    first_group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    first_decision = reviewed_decision(
        queue, first_group, "same-economic-event"
    )
    input_path = decision_input_path(source["root"], "first-decisions.json")
    input_path.write_bytes(
        json_bytes(decision_import_document(queue, [first_decision]))
    )
    first = workflow.import_decisions(
        source["root"], input_path, repo_root=source["repoRoot"]
    )
    replay = workflow.import_decisions(
        source["root"], input_path, repo_root=source["repoRoot"]
    )
    assert replay["decisionPublicationId"] == first["decisionPublicationId"]

    second_group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "other-one-to-one"
    )
    second_decision = reviewed_decision(queue, second_group, "distinct-events")
    second_path = decision_input_path(source["root"], "second-decisions.json")
    second_path.write_bytes(
        json_bytes(decision_import_document(queue, [second_decision]))
    )
    second = workflow.import_decisions(
        source["root"], second_path, repo_root=source["repoRoot"]
    )
    assert second["decisionPublicationId"] != first["decisionPublicationId"]
    decision_root = source["root"] / workflow.OUTPUT_RELATIVE / "decisions"
    second_manifest = json.loads(
        (
            decision_root
            / "publications"
            / second["decisionPublicationId"]
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert second_manifest["parentDecisionPublicationId"] == first[
        "decisionPublicationId"
    ]
    assert second_manifest["sequence"] == 2

    first_summary_path = (
        decision_root
        / "publications"
        / first["decisionPublicationId"]
        / "summary.json"
    )
    first_summary = first_summary_path.read_bytes()
    first_summary_path.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ReviewError, match="decision-history-corrupt"):
        workflow.verify(source["root"], repo_root=source["repoRoot"])
    first_summary_path.write_bytes(first_summary)

    current_pointer = decision_root / "current.json"
    current_pointer.unlink()
    with pytest.raises(ReviewError, match="decision-history-rollback"):
        workflow.verify(source["root"], repo_root=source["repoRoot"])

    current_pointer.write_bytes(
        json_bytes(
            {
                "schemaVersion": 1,
                "publicationId": second["decisionPublicationId"],
                "manifestSha256": second["decisionPublicationId"],
            }
        )
    )
    current_pointer.write_bytes(
        json_bytes(
            {
                "schemaVersion": 1,
                "publicationId": first["decisionPublicationId"],
                "manifestSha256": first["decisionPublicationId"],
            }
        )
    )
    with pytest.raises(ReviewError, match="decision-history-rollback"):
        workflow.verify(source["root"], repo_root=source["repoRoot"])


def test_queue_change_is_blocked_after_decisions_exist(tmp_path, monkeypatch):
    source = synthetic_source(tmp_path)
    mock_verified_source(monkeypatch, source)
    workflow.build(source["root"], repo_root=source["repoRoot"], batch_size=2)
    queue = workflow._prepare_queue(source, 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "other-one-to-one"
    )
    input_path = decision_input_path(source["root"], "durable-decision.json")
    input_path.write_bytes(
        json_bytes(
            decision_import_document(
                queue, [reviewed_decision(queue, group, "distinct-events")]
            )
        )
    )
    workflow.import_decisions(
        source["root"], input_path, repo_root=source["repoRoot"]
    )

    with pytest.raises(ReviewError, match="queue-change-with-decisions"):
        workflow.build(
            source["root"], repo_root=source["repoRoot"], batch_size=1
        )


def test_missing_queue_pointer_cannot_bypass_canonical_review(
    tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path)
    mock_verified_source(monkeypatch, source)
    workflow.build(source["root"], repo_root=source["repoRoot"], batch_size=2)
    (source["root"] / workflow.OUTPUT_RELATIVE / "current.json").unlink()
    row = {
        "date": "2026-01-15",
        "account_id": "acct-synthetic",
        "amount": "-10.00",
        "description": "Synthetic row",
        "source_id": "synthetic:row",
        "source_file": "synthetic/source.json",
    }

    with pytest.raises(ReviewError, match="review-history-rollback"):
        canonical.project(
            source["root"],
            [row],
            [row],
            [],
            repo_root=source["repoRoot"],
        )


def test_decision_evidence_can_be_enriched_without_changing_ruling(
    tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path)
    mock_verified_source(monkeypatch, source)
    workflow.build(source["root"], repo_root=source["repoRoot"], batch_size=2)
    queue = workflow._prepare_queue(source, 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    initial = reviewed_decision(queue, group, "same-economic-event")
    initial["rollbackEvidenceHashes"] = []
    initial_path = decision_input_path(source["root"], "initial-decision.json")
    initial_path.write_bytes(
        json_bytes(decision_import_document(queue, [initial]))
    )
    first = workflow.import_decisions(
        source["root"], initial_path, repo_root=source["repoRoot"]
    )

    enriched = copy.deepcopy(initial)
    rollback_hash = digest("rollback-evidence")
    enriched["rollbackEvidenceHashes"] = [rollback_hash]
    enriched["evidenceHashes"] = sorted(
        {*enriched["evidenceHashes"], rollback_hash}
    )
    enriched_path = decision_input_path(source["root"], "enriched-decision.json")
    enriched_path.write_bytes(
        json_bytes(decision_import_document(queue, [enriched]))
    )
    second = workflow.import_decisions(
        source["root"], enriched_path, repo_root=source["repoRoot"]
    )

    assert second["decisionPublicationId"] != first["decisionPublicationId"]
    assert second["readinessCounts"]["ready-groups"] == 1

    changed = copy.deepcopy(enriched)
    changed["rationale"] = "A different synthetic ruling rationale is not enrichment."
    enriched_path.write_bytes(
        json_bytes(decision_import_document(queue, [changed]))
    )
    with pytest.raises(ReviewError, match="conflicting-decisions"):
        workflow.import_decisions(
            source["root"], enriched_path, repo_root=source["repoRoot"]
        )


def test_evidence_hash_cannot_be_rebound_to_a_different_path(
    tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path)
    mock_verified_source(monkeypatch, source)
    workflow.build(source["root"], repo_root=source["repoRoot"], batch_size=2)
    queue = workflow._prepare_queue(source, 2)
    first_group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "other-one-to-one"
    )
    evidence_one = source["root"] / "review-evidence" / "first.json"
    evidence_two = source["root"] / "review-evidence" / "second.json"
    evidence_one.parent.mkdir()
    content = b'{"synthetic":"same-bytes"}\n'
    evidence_one.write_bytes(content)
    evidence_two.write_bytes(content)

    def reference(path: Path) -> dict:
        return {
            "path": path.relative_to(source["root"]).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "kind": "review-evidence",
            "size": len(content),
            "coversCandidateGroupIds": [],
        }

    first_document = decision_import_document(
        queue,
        [reviewed_decision(queue, first_group, "distinct-events")],
    )
    first_document["evidenceFiles"] = [reference(evidence_one)]
    input_path = decision_input_path(
        source["root"], "first-evidence-decision.json"
    )
    input_path.write_bytes(json_bytes(first_document))
    first = workflow.import_decisions(
        source["root"], input_path, repo_root=source["repoRoot"]
    )

    second_group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "one-to-many"
    )
    second_document = decision_import_document(
        queue,
        [reviewed_decision(queue, second_group, "distinct-events")],
    )
    second_document["evidenceFiles"] = [reference(evidence_two)]
    input_path.write_bytes(json_bytes(second_document))
    with pytest.raises(ReviewError, match="decision-evidence-files-invalid"):
        workflow.import_decisions(
            source["root"], input_path, repo_root=source["repoRoot"]
        )
    current = json.loads(
        (
            source["root"]
            / workflow.OUTPUT_RELATIVE
            / "decisions"
            / "current.json"
        ).read_text(encoding="utf-8")
    )
    assert current["publicationId"] == first["decisionPublicationId"]


def test_decision_import_lock_fails_closed(tmp_path, monkeypatch):
    source = synthetic_source(tmp_path)
    mock_verified_source(monkeypatch, source)
    workflow.build(source["root"], repo_root=source["repoRoot"], batch_size=2)
    queue = workflow._prepare_queue(source, 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "other-one-to-one"
    )
    input_path = decision_input_path(source["root"], "locked-decision.json")
    input_path.write_bytes(
        json_bytes(
            decision_import_document(
                queue, [reviewed_decision(queue, group, "distinct-events")]
            )
        )
    )
    lock_root = source["root"] / "audit" / ".lineage-state"
    lock_root.mkdir(parents=True, exist_ok=True)
    with workflow._decision_import_lock(lock_root):
        with pytest.raises(ReviewError, match="decision-import-in-progress"):
            workflow.import_decisions(
                source["root"], input_path, repo_root=source["repoRoot"]
            )


def test_receipt_binding_must_select_exact_candidate(tmp_path):
    queue = workflow._prepare_queue(synthetic_source(tmp_path), 2)
    group = next(
        item
        for item in queue["groups"]
        if item["reviewKind"] == "receipt-binding"
    )
    decision = reviewed_decision(queue, group, "receipt-binding-resolution")
    decision["receiptBinding"]["receiptSha256"] = digest("not-a-candidate")

    with pytest.raises(ReviewError, match="receipt-binding-invalid"):
        workflow._validate_decision_set(queue, [decision], [])


def test_changed_private_decision_evidence_fails_closed(tmp_path):
    source = synthetic_source(tmp_path)
    evidence_path = source["root"] / "review-evidence" / "rollback.json"
    evidence_path.parent.mkdir()
    evidence_path.write_text('{"synthetic":"rollback"}\n', encoding="utf-8")
    evidence = {
        "path": evidence_path.relative_to(source["root"]).as_posix(),
        "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "kind": "rollback-plan",
        "size": evidence_path.stat().st_size,
        "coversCandidateGroupIds": [digest("synthetic-group")],
    }

    assert workflow._evidence_files(
        source["root"], source["repoRoot"], [evidence]
    ) == [evidence]
    evidence_path.write_text('{"synthetic":"changed"}\n', encoding="utf-8")
    with pytest.raises(ReviewError, match="decision-evidence-file-changed"):
        workflow._evidence_files(
            source["root"], source["repoRoot"], [evidence]
        )

    canonical_path = (
        source["root"] / "normalized" / "canonical" / "manifest.json"
    )
    canonical_path.parent.mkdir(parents=True)
    canonical_path.write_text('{"schemaVersion":5}\n', encoding="utf-8")
    canonical_evidence = {
        "path": canonical_path.relative_to(source["root"]).as_posix(),
        "sha256": hashlib.sha256(canonical_path.read_bytes()).hexdigest(),
        "kind": "review-evidence",
        "size": canonical_path.stat().st_size,
        "coversCandidateGroupIds": [],
    }
    with pytest.raises(ReviewError, match="decision-evidence-files-invalid"):
        workflow._evidence_files(
            source["root"], source["repoRoot"], [canonical_evidence]
        )


def test_generated_shadow_output_cannot_be_imported_as_rollback_evidence(tmp_path):
    source = synthetic_source(tmp_path)
    evidence_path = (
        source["root"] / "postgres-shadow" / "plans" / "synthetic.json"
    )
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text('{"synthetic":"rollback"}\n', encoding="utf-8")
    evidence = {
        "path": evidence_path.relative_to(source["root"]).as_posix(),
        "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "kind": "rollback-plan",
        "size": evidence_path.stat().st_size,
        "coversCandidateGroupIds": [digest("synthetic-group")],
    }

    with pytest.raises(ReviewError, match="decision-evidence-files-invalid"):
        workflow._evidence_files(
            source["root"], source["repoRoot"], [evidence]
        )


def test_remediation_readiness_requires_lineage_and_rollback(tmp_path):
    queue = workflow._prepare_queue(synthetic_source(tmp_path), 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    decision = reviewed_decision(queue, group, "same-economic-event")
    readiness, gaps = workflow._readiness(queue, [decision])

    assert readiness["ready-groups"] == 1
    assert readiness["restore-eligible-groups"] == 1
    assert readiness["surgical-eligible-groups"] == 1
    assert readiness["rebuild-eligible-groups"] == 1
    assert gaps["missing-decision"] == len(queue["groups"]) - 1

    decision["rollbackEvidenceHashes"] = []
    readiness, gaps = workflow._readiness(queue, [decision])
    assert readiness["ready-groups"] == 0
    assert gaps["missing-rollback-evidence"] == 1

    missing_lineage = copy.deepcopy(queue)
    target = next(
        item
        for item in missing_lineage["groups"]
        if item["groupId"] == group["groupId"]
    )
    target["lineageStatuses"][0] = "missing"
    readiness, gaps = workflow._readiness(missing_lineage, [decision])
    assert readiness["ready-groups"] == 0
    assert gaps["missing-source-lineage"] >= 1

    unrelated_queue = copy.deepcopy(queue)
    rollback = next(
        item
        for item in unrelated_queue["evidenceCatalog"]
        if item["sha256"] == digest("rollback-evidence")
    )
    rollback["coversCandidateGroupIds"] = [
        next(
            item["groupId"]
            for item in unrelated_queue["groups"]
            if item["groupId"] != group["groupId"]
        )
    ]
    with pytest.raises(ReviewError, match="rollback-evidence-invalid"):
        workflow._validate_decision_set(
            unrelated_queue,
            [reviewed_decision(queue, group, "same-economic-event")],
            [],
        )


def test_rollback_must_match_selected_receipt_candidate(tmp_path):
    source = synthetic_source(tmp_path)
    economic_group = next(
        item
        for item in source["detail"]["candidateGroups"]
        if item["sourceFamilyCounts"] == {"extract": 1, "simplefin": 1}
    )
    activities = {
        item["activityRef"]: item for item in source["detail"]["activities"]
    }
    target = activities[economic_group["activityRefs"][0]]
    first_plan = source["plans"][0]
    first_plan["value"]["operations"]["creates"][0].update(
        {
            "accountId": target["raw"]["accountId"],
            "idempotencyKey": target["sourceIdentity"],
        }
    )
    first_plan["value"]["reconciliations"][0]["rollbackPayload"] = {
        "synthetic": "first"
    }
    first_receipt = copy.deepcopy(source["receipts"][0])
    first_receipt["value"]["reconciliations"] = copy.deepcopy(
        first_plan["value"]["reconciliations"]
    )
    second_plan = copy.deepcopy(first_plan)
    second_plan["sha256"] = digest("selected-second-plan")
    second_plan["value"]["reconciliations"][0]["rollbackPayload"] = {
        "synthetic": "second"
    }
    second_receipt = copy.deepcopy(first_receipt)
    second_receipt["sha256"] = digest("selected-second-receipt")
    second_receipt["value"]["applicationPlanSha256"] = second_plan["sha256"]
    second_receipt["value"]["reconciliations"] = copy.deepcopy(
        second_plan["value"]["reconciliations"]
    )
    source["plans"] = [first_plan, second_plan]
    source["receipts"] = [first_receipt, second_receipt]
    source["inputs"].extend(
        [
            {
                "path": "synthetic/selected-plan.json",
                "kind": "plan",
                "size": 10,
                "sha256": second_plan["sha256"],
            },
            {
                "path": "synthetic/selected-receipt.json",
                "kind": "receipt",
                "size": 10,
                "sha256": second_receipt["sha256"],
            },
        ]
    )
    queue = workflow._prepare_queue(source, 2)
    economic = next(
        item
        for item in queue["groups"]
        if item["groupId"] == economic_group["groupId"]
    )
    binding = next(
        item
        for item in queue["groups"]
        if item["reviewKind"] == "receipt-binding"
        and set(item["activityRefs"]).intersection(economic["activityRefs"])
    )
    selected = max(
        binding["receiptBindingCandidates"],
        key=lambda item: item["applicationPlanSha256"],
    )
    unselected = next(
        item
        for item in binding["receiptBindingCandidates"]
        if item != selected
    )
    binding_decision = reviewed_decision(
        queue, binding, "receipt-binding-resolution"
    )
    binding_decision["receiptBinding"] = {
        "applicationPlanSha256": selected["applicationPlanSha256"],
        "receiptSha256": selected["receiptSha256"],
    }
    binding_decision["evidenceHashes"] = sorted(
        binding_decision["receiptBinding"].values()
    )
    binding_decision["reconciliationBinding"] = {
        "fingerprints": selected["reconciliationFingerprints"]
    }
    suppression = reviewed_decision(
        queue, economic, "same-economic-event"
    )
    wrong_rollback = unselected["rollbackEvidenceHashes"][0]
    suppression["rollbackEvidenceHashes"] = [wrong_rollback]
    suppression["evidenceHashes"] = sorted(
        {*suppression["evidenceHashes"], wrong_rollback}
    )

    with pytest.raises(ReviewError, match="rollback-evidence-invalid"):
        workflow._validate_decision_set(
            queue, [suppression, binding_decision], []
        )


def test_canonical_projection_preserves_observations_and_suppresses_explicitly(
    tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path)
    queue = workflow._prepare_queue(source, 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    decision = reviewed_decision(queue, group, "same-economic-event")
    activities = {
        item["activityRef"]: item for item in source["detail"]["activities"]
    }
    state = {
        "queue": queue,
        "queuePublicationId": digest("queue-publication"),
        "decisions": [decision],
        "decisionPublicationId": digest("decision-publication"),
        "activities": activities,
        "sourcePaths": (),
    }
    monkeypatch.setattr(canonical, "verified_state", lambda *_args, **_kwargs: state)
    (source["root"] / workflow.OUTPUT_RELATIVE).mkdir(parents=True)
    (source["root"] / workflow.OUTPUT_RELATIVE / "current.json").write_text("{}")
    rows = [
        {
            "date": activities[ref]["sourceDateUtc"],
            "account_id": activities[ref]["canonicalAccountId"],
            "amount": activities[ref]["signedEffect"],
            "description": activities[ref]["description"],
            "source_id": activities[ref]["sourceIdentity"],
            "source_file": f"synthetic/{index}.json",
            "category": "",
            "transfer_group": "",
            "symbol": "",
            "quantity": "",
            "price": "",
            "external_flow": False,
            "excluded": False,
            "exclusion_reason": "",
            "transaction_kind": "",
            "category_id": "",
            "payee_normalized": "",
            "assignment_source": "",
            "assignment_rule_id": "",
            "assignment_confidence": "",
            "split_group": "",
        }
        for index, ref in enumerate(group["activityRefs"])
    ]
    original = copy.deepcopy(rows)

    result = canonical.project(
        source["root"],
        rows,
        rows,
        [],
        repo_root=source["repoRoot"],
    )

    assert rows == original
    assert len(result["rows"]) == 1
    assert result["rows"][0]["source_id"] == activities[
        decision["survivorActivityRef"]
    ]["sourceIdentity"]
    assert result["observations"]["observationCount"] == 2
    assert Counter(
        item["disposition"] for item in result["observations"]["observations"]
    ) == {"active": 1, "suppressed": 1}
    assert result["lineage"]["counts"]["canonical-transactions"] == 1
    assert result["lineage"]["decisionProjections"][0][
        "suppressedActivityRefs"
    ]

    distinct_group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "other-one-to-one"
    )
    state["decisions"] = [
        reviewed_decision(queue, distinct_group, "distinct-events")
    ]
    distinct_rows = [
        {
            **rows[0],
            "description": activities[ref]["description"],
            "source_id": activities[ref]["sourceIdentity"],
            "source_file": f"synthetic/distinct-{index}.json",
        }
        for index, ref in enumerate(distinct_group["activityRefs"])
    ]
    distinct = canonical.project(
        source["root"],
        distinct_rows,
        distinct_rows,
        [],
        repo_root=source["repoRoot"],
    )
    assert len(distinct["rows"]) == 2
    assert distinct["lineage"]["decisionProjections"][0][
        "suppressedActivityRefs"
    ] == []

    state["decisions"] = [decision]
    transfer_rows = copy.deepcopy(original)
    transfer_rows[0]["transfer_group"] = "existing-reviewed-transfer"
    with pytest.raises(
        ReviewError, match="suppression-conflicts-with-transfer-authority"
    ):
        canonical.project(
            source["root"],
            transfer_rows,
            transfer_rows,
            [],
            repo_root=source["repoRoot"],
        )

    receipt_group = copy.deepcopy(
        next(
            item
            for item in queue["groups"]
            if item["reviewKind"] == "receipt-binding"
        )
    )
    receipt_group["activityRefs"] = group["activityRefs"]
    receipt_group["groupId"] = digest("overlapping-receipt-defer")
    receipt_group["decisionId"] = decision_id(receipt_group["groupId"])
    state["queue"] = {
        **queue,
        "groups": [*queue["groups"], receipt_group],
    }
    receipt_defer = reviewed_decision(
        state["queue"], receipt_group, "defer-insufficient-evidence"
    )
    state["decisions"] = [decision, receipt_defer]
    preserved = canonical.project(
        source["root"],
        rows,
        rows,
        [],
        repo_root=source["repoRoot"],
    )
    economic_records = preserved["observations"]["observations"]
    assert {
        item["decisionId"] for item in economic_records
    } == {decision["decisionId"]}

    tampered_observations = copy.deepcopy(result["observations"])
    tampered_lineage = copy.deepcopy(result["lineage"])
    suppressed_record = next(
        item
        for item in tampered_observations["observations"]
        if item["disposition"] == "suppressed"
    )
    suppressed_record["disposition"] = "active"
    tampered_lineage["counts"]["active-observations"] = 2
    tampered_lineage["counts"].pop("suppressed-observations")
    with pytest.raises(
        ReviewError, match="canonical-lineage-publication-invalid"
    ):
        canonical.validate_documents(
            tampered_observations,
            tampered_lineage,
            result["rows"],
        )


def test_canonical_exact_source_replay_decision_does_not_invent_observations(
    tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path)
    queue = workflow._prepare_queue(source, 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    activities = {
        item["activityRef"]: item for item in source["detail"]["activities"]
    }
    shared_identity = "simplefin:synthetic-shared"
    shared_description = activities[group["activityRefs"][0]]["description"]
    for ref in group["activityRefs"]:
        activities[ref]["sourceIdentity"] = shared_identity
        activities[ref]["description"] = shared_description
    decision = reviewed_decision(queue, group, "same-economic-event")
    state = {
        "queue": queue,
        "queuePublicationId": digest("queue-publication"),
        "decisions": [decision],
        "decisionPublicationId": digest("decision-publication"),
        "activities": activities,
        "sourcePaths": (),
    }
    monkeypatch.setattr(canonical, "verified_state", lambda *_args, **_kwargs: state)
    (source["root"] / workflow.OUTPUT_RELATIVE).mkdir(parents=True)
    (source["root"] / workflow.OUTPUT_RELATIVE / "current.json").write_text("{}")
    row = {
        "date": "2026-01-15",
        "account_id": "acct-synthetic",
        "amount": "-10.00",
        "description": activities[group["activityRefs"][0]]["description"],
        "source_id": shared_identity,
        "source_file": "synthetic/source.json",
    }

    result = canonical.project(
        source["root"],
        [row],
        [row],
        [],
        repo_root=source["repoRoot"],
    )

    assert len(result["rows"]) == 1
    assert result["observations"]["observationCount"] == 1
    assert result["lineage"]["decisionProjections"][0][
        "suppressedActivityRefs"
    ] == [
        ref
        for ref in group["activityRefs"]
        if ref != decision["survivorActivityRef"]
    ]


def test_source_id_collisions_keep_distinct_observation_ownership(tmp_path):
    root = tmp_path / "private"
    repo = tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    source_rows = [
        {
            "date": "2026-01-15",
            "account_id": "acct-synthetic",
            "amount": "-10.00",
            "description": "Synthetic collision",
            "source_id": "synthetic:collision",
            "source_file": "synthetic/source.json",
            "category": category,
        }
        for category in ("Synthetic One", "Synthetic Two")
    ]
    canonical_rows = [
        {
            **row,
            "source_id": (
                f"{row['source_id']}:collision:"
                f"{canonical.source_collision_suffix(row)}"
            ),
        }
        for row in source_rows
    ]

    result = canonical.project(
        root,
        canonical_rows,
        source_rows,
        [],
        repo_root=repo,
    )

    assert len(result["rows"]) == 2
    assert result["observations"]["observationCount"] == 2
    claimed = [
        observation_id
        for item in result["lineage"]["canonicalTransactions"]
        for observation_id in item["memberObservationIds"]
    ]
    assert len(claimed) == len(set(claimed)) == 2

    split_parent = canonical_rows[0]
    split_rows = [
        {
            **split_parent,
            "source_id": f"{split_parent['source_id']}:split:{index}",
            "amount": amount,
            "split_group": "synthetic-split",
        }
        for index, amount in ((1, "-4.00"), (2, "-6.00"))
    ]
    published_rows = [*split_rows, canonical_rows[1]]
    split_lineage = canonical.bind_transaction_rows(
        result["lineage"],
        result["observations"],
        published_rows,
    )
    canonical.validate_documents(
        result["observations"], split_lineage, published_rows
    )

    tampered = copy.deepcopy(result["lineage"])
    tampered_observations = copy.deepcopy(result["observations"])
    tampered_observations["observations"][0][
        "canonicalTransactionId"
    ] = digest("contradictory-owner")
    with pytest.raises(
        ReviewError, match="canonical-lineage-publication-invalid"
    ):
        canonical.validate_documents(
            tampered_observations,
            tampered,
            result["rows"],
        )


def test_transaction_binding_indexes_sources_without_rehashing_every_candidate(monkeypatch):
    parents = [
        {
            "account_id": f"SYN-ACCOUNT-{index % 2}",
            "source_id": f"synthetic:SYN-{index}:split:SYN-PROVIDER",
            "date": "2026-01-15",
            "amount": "-10.00",
            "description": "Synthetic source",
        }
        for index in range(40)
    ]
    observations = {"observations": [
        {"observationId": f"SYN-OBS-{index}", "transaction": parent}
        for index, parent in enumerate(parents)
    ]}
    lineage = {"canonicalTransactions": [
        {"canonicalTransactionId": f"SYN-EVENT-{index}",
         "activeObservationId": f"SYN-OBS-{index}"}
        for index in range(len(parents))
    ]}
    published = []
    expected = []
    for index, parent in enumerate(parents):
        source_id = parent["source_id"]
        if index % 4 in {1, 3}:
            source_id += f":collision:{canonical.source_collision_suffix(parent)}"
        if index % 4 in {2, 3}:
            children = [
                {**parent, "source_id": f"{source_id}:split:{part}",
                 "split_group": "SYN-SPLIT", "amount": "-5.00"}
                for part in range(2)
            ]
        else:
            children = [{**parent, "source_id": source_id}]
        published.extend(children)
        expected.append(sorted(
            canonical.stable_hash(canonical._normalized_transaction(row))
            for row in children
        ))
    original = canonical.source_collision_suffix
    calls = []

    def count_hash(row):
        calls.append(row["source_id"])
        return original(row)

    monkeypatch.setattr(canonical, "source_collision_suffix", count_hash)
    result = canonical.bind_transaction_rows(lineage, observations, reversed(published))
    assert len(calls) == len(parents)
    assert [
        row["publishedTransactionFingerprints"]
        for row in result["canonicalTransactions"]
    ] == expected


@pytest.mark.parametrize("missing_field", ["account_id", "source_id"])
def test_transaction_binding_rejects_missing_source_scope(missing_field):
    row = {"account_id": "", "source_id": "", "amount": "0"}
    observations = {"observations": [
        {"observationId": "SYN-OBS", "transaction": dict(row)}
    ]}
    lineage = {"canonicalTransactions": [
        {"activeObservationId": "SYN-OBS"}
    ]}
    del row[missing_field]
    with pytest.raises(ReviewError, match="publication-unmapped"):
        canonical.bind_transaction_rows(lineage, observations, [row])


def test_pending_publication_is_recovered_under_stable_lock(tmp_path):
    root = tmp_path / "private"
    repo = tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    output = root / workflow.OUTPUT_RELATIVE
    documents = {"queue.json": json_bytes({"synthetic": True})}
    manifest = {
        "schemaVersion": 1,
        "kind": "lineage-review-queue-publication",
        "private": True,
        "readOnly": True,
        "files": {
            name: {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            for name, content in documents.items()
        },
    }
    manifest_content = json_bytes(manifest)
    publication_id = hashlib.sha256(manifest_content).hexdigest()
    publication = output / "publications" / publication_id
    publication.mkdir(parents=True)
    for name, content in documents.items():
        (publication / name).write_bytes(content)
    (publication / "manifest.json").write_bytes(manifest_content)
    output.mkdir(parents=True, exist_ok=True)
    (output / ".pending.json").write_bytes(
        json_bytes(
            {
                "schemaVersion": 1,
                "kind": "lineage-review-queue-publication",
                "expectedCurrentPublicationId": None,
                "publicationId": publication_id,
            }
        )
    )
    lock_path = root / "audit" / ".lineage-state" / ".import.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("stale process marker\n", encoding="utf-8")

    with workflow.decision_state_lock(root, repo):
        pass

    pointer = json.loads(
        (output / "current.json").read_text(encoding="utf-8")
    )
    assert pointer["publicationId"] == publication_id
    assert not (output / ".pending.json").exists()


def test_stable_replays_from_multiple_files_share_canonical_identity(tmp_path):
    root = tmp_path / "private"
    repo = tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    first = {
        "date": "2026-01-15",
        "account_id": "acct-synthetic",
        "amount": "-10.00",
        "description": "Synthetic replay",
        "source_id": "synthetic:stable-id",
        "source_file": "synthetic/first.json",
    }
    second = {**first, "source_file": "synthetic/second.json"}

    result = canonical.project(
        root,
        [first],
        [first, second],
        [],
        repo_root=repo,
    )

    assert result["observations"]["observationCount"] == 2
    assert len(result["lineage"]["canonicalTransactions"]) == 1
    canonical.validate_documents(
        result["observations"], result["lineage"], result["rows"]
    )


def test_canonical_link_conflicts_with_legacy_transfer_authority(
    tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path)
    queue = workflow._prepare_queue(source, 2)
    group = next(
        item for item in queue["groups"] if item["priorityCode"] == "linked-transfer"
    )
    decision = reviewed_decision(queue, group, "linked-transfer")
    activities = {
        item["activityRef"]: item for item in source["detail"]["activities"]
    }
    state = {
        "queue": queue,
        "queuePublicationId": digest("queue-publication"),
        "decisions": [decision],
        "decisionPublicationId": digest("decision-publication"),
        "activities": activities,
        "sourcePaths": (),
    }
    monkeypatch.setattr(canonical, "verified_state", lambda *_args, **_kwargs: state)
    (source["root"] / workflow.OUTPUT_RELATIVE).mkdir(parents=True)
    (source["root"] / workflow.OUTPUT_RELATIVE / "current.json").write_text("{}")
    rows = [
        {
            "date": activities[ref]["sourceDateUtc"],
            "account_id": activities[ref]["canonicalAccountId"],
            "amount": activities[ref]["signedEffect"],
            "description": activities[ref]["description"],
            "source_id": activities[ref]["sourceIdentity"],
            "source_file": f"synthetic/{index}.json",
            "transfer_group": "",
        }
        for index, ref in enumerate(group["activityRefs"])
    ]

    class Fact:
        kind = "transfer"
        affects = ()

    class Parsed:
        fact = Fact()
        data = {"sourceIds": [activities[group["activityRefs"][0]]["sourceIdentity"]]}

    with pytest.raises(ReviewError, match="duplicated-decision-authority"):
        canonical.project(
            source["root"],
            rows,
            rows,
            [Parsed()],
            repo_root=source["repoRoot"],
        )

    rows[0]["transfer_group"] = "existing-reviewed-group"
    with pytest.raises(ReviewError, match="duplicated-decision-authority"):
        canonical.project(
            source["root"],
            rows,
            rows,
            [],
            repo_root=source["repoRoot"],
        )

    rows[0]["transfer_group"] = ""
    legacy_review = {
        "rejected": [
            {
                "outflow": {
                    "accountId": rows[0]["account_id"],
                    "sourceId": rows[0]["source_id"],
                },
                "inflow": {
                    "accountId": rows[1]["account_id"],
                    "sourceId": rows[1]["source_id"],
                },
            }
        ]
    }
    with pytest.raises(ReviewError, match="duplicated-decision-authority"):
        canonical.project(
            source["root"],
            rows,
            rows,
            [],
            repo_root=source["repoRoot"],
            legacy_transfer_review=legacy_review,
        )


def test_schema_accepts_a_complete_synthetic_decision(tmp_path):
    queue = workflow._prepare_queue(synthetic_source(tmp_path), 2)
    group = next(
        item
        for item in queue["groups"]
        if item["priorityCode"] == "extract-simplefin-one-to-one"
    )
    document = decision_import_document(
        queue, [reviewed_decision(queue, group, "same-economic-event")]
    )
    schema_path = (
        Path(__file__).parents[1]
        / "importers"
        / "lineage_review"
        / "decision-schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    jsonschema.Draft202012Validator(schema).validate(document)


def test_cli_and_modules_have_no_wealthfolio_mutation_transport():
    source = "\n".join(
        inspect.getsource(module) for module in (workflow, canonical, cli)
    )

    assert "WealthfolioClient" not in source
    assert "requests." not in source
    assert "httpx." not in source
    assert 'choices=("build", "import", "verify", "status")' in inspect.getsource(
        cli.main
    )
