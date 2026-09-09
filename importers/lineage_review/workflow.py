"""Build, import, verify, and summarize private lineage review publications."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from importers.analytics.publication import ensure_durable_directory, fsync_directory
from importers.audit import forensic
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import validate_private_output

from .model import (
    DECISION_TYPES,
    HEX_64,
    POINTER_SCHEMA_VERSION,
    PRIORITIES,
    PRIORITY_RANK,
    ROLLBACK_EVIDENCE_KINDS,
    SAFE_CODE,
    SCHEMA_VERSION,
    SUPPRESSION_TYPES,
    ReviewError,
    decision_id,
    json_bytes,
    parse_decided_at,
    require_hash,
    sha256_bytes,
    stable_hash,
)

OUTPUT_RELATIVE = Path("audit") / "lineage-review"
DECISION_INPUT_RELATIVE = Path("audit") / ".lineage-state" / "decision-imports"
DECISION_REQUIRED_FIELDS = frozenset(
    {
        "schemaVersion",
        "decisionId",
        "candidateGroupId",
        "decisionType",
        "reviewer",
        "decidedAt",
        "rationale",
        "environmentFingerprint",
        "baselinePublicationId",
        "forensicPublicationId",
        "candidateGraphHash",
        "candidateGroupHash",
        "cardinality",
        "reasonCodes",
        "sourceFamilyCounts",
        "memberActivityFingerprints",
        "evidenceHashes",
        "expectedDependentStateFingerprints",
        "rollbackEvidenceHashes",
    }
)
DECISION_OPTIONAL_FIELDS = frozenset(
    {
        "survivorActivityRef",
        "linkedActivityRefs",
        "receiptBinding",
        "reconciliationBinding",
    }
)
TOP_LEVEL_DECISION_FIELDS = frozenset(
    {
        "schemaVersion",
        "kind",
        "environmentFingerprint",
        "baselinePublicationId",
        "forensicPublicationId",
        "candidateGraphHash",
        "queueInputHash",
        "evidenceFiles",
        "decisions",
    }
)
EVIDENCE_FILE_KINDS = frozenset(
    {
        "backup-manifest",
        "review-evidence",
        "rollback-plan",
        "rollback-receipt",
    }
)
PLACEHOLDER_REVIEW = frozenset({"", "pending", "tbd", "todo", "unknown", "n/a"})
_NO_CAS = object()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ReviewError(code) from None


def _hashed_json(
    path: Path,
    reference: dict[str, Any],
    code: str,
) -> Any:
    if not isinstance(reference, dict):
        raise ReviewError(code)
    try:
        content = path.read_bytes()
    except OSError:
        raise ReviewError(code) from None
    if (
        not path.is_file()
        or len(content) != reference.get("size")
        or sha256_bytes(content) != reference.get("sha256")
    ):
        raise ReviewError(code)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        raise ReviewError(code) from None


def _safe_private_path(root: Path, value: str, repo_root: Path) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError:
        raise ReviewError("private-path-outside-data-dir") from None
    if repo_root.resolve() == resolved or repo_root.resolve() in resolved.parents:
        raise ReviewError("private-path-inside-repository")
    if not str(relative):
        raise ReviewError("private-path-invalid")
    return resolved


def _selected_receipt_candidate(
    group: dict[str, Any],
    decision: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if (
        group.get("reviewKind") != "receipt-binding"
        or decision is None
        or decision.get("decisionType") != "receipt-binding-resolution"
    ):
        return None
    binding = decision.get("receiptBinding")
    matches = [
        item
        for item in group.get("receiptBindingCandidates", [])
        if {
            "applicationPlanSha256": item.get("applicationPlanSha256"),
            "receiptSha256": item.get("receiptSha256"),
        }
        == binding
    ]
    return matches[0] if len(matches) == 1 else None


def _load_verified_forensic(
    data_dir: str | Path, repo_root: str | Path
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    checkout = Path(repo_root).resolve()
    try:
        verified = forensic.verify(root, repo_root=checkout)
        publication, pointer = forensic._current(root)
        if pointer != verified.get("publication"):
            raise ReviewError("forensic-publication-changed")
        manifest_content = (publication / "manifest.json").read_bytes()
        if sha256_bytes(manifest_content) != pointer["publicationId"]:
            raise ReviewError("forensic-publication-changed")
        forensic_manifest = json.loads(manifest_content)
        forensic_files = forensic_manifest.get("files")
        if not isinstance(forensic_files, dict):
            raise ReviewError("forensic-publication-invalid")
        detail = _hashed_json(
            publication / "private-audit.json",
            forensic_files.get("private-audit.json", {}),
            "forensic-detail-invalid",
        )
        summary = _hashed_json(
            publication / "summary.json",
            forensic_files.get("summary.json", {}),
            "forensic-summary-invalid",
        )
        baseline_pointer = detail.get("baselinePublication")
        baseline_id = require_hash(
            (
                baseline_pointer.get("publicationId")
                if isinstance(baseline_pointer, dict)
                else None
            ),
            "baseline-publication-stale",
        )
        if (
            baseline_pointer.get("manifestSha256") != baseline_id
            or baseline_pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION
        ):
            raise ReviewError("baseline-publication-stale")
        current_baseline = _safe_json(
            root / forensic.baseline.OUTPUT_RELATIVE / "current.json",
            "baseline-publication-stale",
        )
        if current_baseline != baseline_pointer:
            raise ReviewError("baseline-publication-changed")
        baseline_publication = (
            root
            / forensic.baseline.OUTPUT_RELATIVE
            / "publications"
            / baseline_id
        )
        baseline_manifest_path = baseline_publication / "manifest.json"
        if (
            not baseline_manifest_path.is_file()
            or _sha256(baseline_manifest_path) != baseline_id
        ):
            raise ReviewError("baseline-publication-stale")
        baseline_content = baseline_manifest_path.read_bytes()
        if sha256_bytes(baseline_content) != baseline_id:
            raise ReviewError("baseline-publication-stale")
        baseline_manifest = json.loads(baseline_content)
        inputs, plans, receipts = forensic._read_evidence(
            root,
            baseline_manifest["sourceFiles"],
            baseline_manifest["environmentFingerprint"],
        )
        if (
            _safe_json(
                root / forensic.OUTPUT_RELATIVE / "current.json",
                "forensic-publication-changed",
            )
            != pointer
            or _safe_json(
                root / forensic.baseline.OUTPUT_RELATIVE / "current.json",
                "baseline-publication-changed",
            )
            != baseline_pointer
        ):
            raise ReviewError("forensic-publication-changed")
    except (
        forensic.ForensicAuditError,
        DecisionError,
        KeyError,
        OSError,
        TypeError,
    ):
        raise ReviewError("forensic-publication-invalid") from None
    environment = require_hash(
        baseline_manifest.get("environmentFingerprint"),
        "foreign-environment",
    )
    plans = [
        item
        for item in plans
        if item["value"].get("environmentFingerprint") == environment
    ]
    receipts = [
        item
        for item in receipts
        if item["value"].get("environmentFingerprint") == environment
    ]
    return {
        "root": root,
        "repoRoot": checkout,
        "publicationPath": publication,
        "baselinePublicationPath": baseline_publication,
        "forensicManifest": forensic_manifest,
        "baselineManifest": baseline_manifest,
        "forensicPublicationId": require_hash(
            pointer.get("publicationId"), "forensic-publication-invalid"
        ),
        "baselinePublicationId": require_hash(
            baseline_pointer.get("publicationId"), "baseline-publication-stale"
        ),
        "environmentFingerprint": environment,
        "forensicCandidateGraphHash": require_hash(
            summary.get("candidateGraphSha256"), "candidate-graph-stale"
        ),
        "detail": detail,
        "summary": summary,
        "inputs": inputs,
        "plans": plans,
        "receipts": receipts,
    }


def _assert_source_unchanged(source: dict[str, Any]) -> None:
    root = source["root"]
    try:
        _publication, forensic_pointer = forensic._current(root)
        baseline_pointer = _safe_json(
            root / forensic.baseline.OUTPUT_RELATIVE / "current.json",
            "baseline-publication-changed",
        )
        if (
            forensic_pointer.get("publicationId")
            != source["forensicPublicationId"]
            or not isinstance(baseline_pointer, dict)
            or baseline_pointer.get("publicationId")
            != source["baselinePublicationId"]
        ):
            raise ReviewError("forensic-publication-changed")
        baseline_publication = source["baselinePublicationPath"]
        baseline_manifest_path = baseline_publication / "manifest.json"
        if (
            not baseline_manifest_path.is_file()
            or _sha256(baseline_manifest_path)
            != source["baselinePublicationId"]
        ):
            raise ReviewError("baseline-publication-changed")
        for name, reference in source["baselineManifest"].get(
            "domainFiles", {}
        ).items():
            path = baseline_publication / "domains" / name
            if (
                not isinstance(reference, dict)
                or not path.is_file()
                or _sha256(path) != reference.get("sha256")
            ):
                raise ReviewError("baseline-publication-changed")
        for item in source["baselineManifest"].get("sourceFiles", []):
            if item.get("omitted") or forensic.baseline._is_downstream_output_path(
                Path(str(item.get("path") or ""))
            ):
                continue
            path = forensic._safe_evidence_path(root, str(item.get("path") or ""))
            metadata = forensic.baseline._entry_metadata(
                path, Path(str(item.get("path") or ""))
            )
            if (
                metadata is None
                or not forensic.baseline._is_regular_file(metadata)
                or metadata.st_size != item.get("size")
                or _sha256(path) != item.get("sha256")
            ):
                raise ReviewError("forensic-input-changed")
        for name in ("private-audit.json", "summary.json"):
            reference = source["forensicManifest"]["files"][name]
            path = source["publicationPath"] / name
            if (
                path.stat().st_size != reference.get("size")
                or _sha256(path) != reference.get("sha256")
            ):
                raise ReviewError("forensic-publication-changed")
    except (forensic.baseline.BaselineError, KeyError, OSError, TypeError):
        raise ReviewError("forensic-publication-changed") from None


def _evidence_catalog(source: dict[str, Any]) -> list[dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for item in source["inputs"]:
        if forensic.baseline._is_downstream_output_path(
            Path(str(item.get("path") or ""))
        ):
            continue
        digest = require_hash(item.get("sha256"), "evidence-hash-invalid")
        catalog[digest] = {
            "sha256": digest,
            "kind": str(item.get("kind") or "review-evidence"),
            "path": str(item.get("path") or ""),
            "size": int(item.get("size") or 0),
            "coversCandidateGroupIds": sorted(
                str(group_id)
                for group_id in item.get("coversCandidateGroupIds", [])
            ),
        }
    for plan in source["plans"]:
        for reconciliation in plan["value"].get("reconciliations") or []:
            if not isinstance(reconciliation, dict) or "rollbackPayload" not in reconciliation:
                continue
            digest = stable_hash(reconciliation["rollbackPayload"])
            catalog.setdefault(
                digest,
                {
                    "sha256": digest,
                    "kind": "embedded-rollback",
                    "sourceSha256": plan["sha256"],
                    "coversCandidateGroupIds": [],
                },
            )
    return [catalog[key] for key in sorted(catalog)]


def _member_fingerprints(
    refs: Iterable[str], activities: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    result = []
    for ref in sorted(refs):
        activity = activities.get(ref)
        if activity is None:
            raise ReviewError("candidate-members-changed")
        result.append({"activityRef": ref, "fingerprint": stable_hash(activity)})
    return result


def _dependent_fingerprints(
    refs: Iterable[str], activities: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    return [
        {
            "activityRef": ref,
            "fingerprint": stable_hash(activities[ref].get("dependentState")),
        }
        for ref in sorted(refs)
    ]


def _reconciliation_fingerprints(
    refs: Iterable[str], activities: dict[str, dict[str, Any]]
) -> list[str]:
    return sorted(
        {
            stable_hash(reconciliation)
            for ref in refs
            for reconciliation in [activities[ref].get("lineage", {}).get("reconciliation")]
            if isinstance(reconciliation, dict)
        }
    )


def _rollback_hashes(
    refs: Iterable[str], activities: dict[str, dict[str, Any]]
) -> list[str]:
    return sorted(
        {
            stable_hash(reconciliation["rollbackPayload"])
            for ref in refs
            for reconciliation in [activities[ref].get("lineage", {}).get("reconciliation")]
            if isinstance(reconciliation, dict)
            and "rollbackPayload" in reconciliation
        }
    )


def _priority(
    cardinality: str, reasons: list[str], source_families: dict[str, int]
) -> tuple[str, int]:
    family_set = {name for name, count in source_families.items() if count}
    if cardinality == "one-to-one" and family_set == {"extract", "simplefin"}:
        code = "extract-simplefin-one-to-one"
    elif {"linked-transfer", "transfer-candidate"}.intersection(reasons):
        code = "linked-transfer"
    elif cardinality == "one-to-many":
        code = "one-to-many"
    elif cardinality == "many-to-many":
        code = "many-to-many"
    else:
        code = "other-one-to-one"
    return code, PRIORITY_RANK[code]


def _risk_score(
    cardinality: str,
    refs: list[str],
    reasons: list[str],
    activities: dict[str, dict[str, Any]],
    reconciliation_required: bool,
) -> int:
    cardinality_score = {
        "one-to-one": 10,
        "one-to-many": 30,
        "many-to-many": 50,
        "receipt-binding": 60,
    }[cardinality]
    dependent = sum(
        len(activities[ref].get("dependentState", {}).get("reasonCodes") or [])
        for ref in refs
    )
    return (
        cardinality_score
        + len(refs)
        + dependent * 20
        + (20 if {"linked-transfer", "transfer-candidate"}.intersection(reasons) else 0)
        + (50 if reconciliation_required else 0)
    )


def _evidence_strength(reasons: list[str]) -> int:
    weights = {
        "exact-source-identity": 100,
        "exact-cross-source": 90,
        "linked-transfer": 80,
        "transfer-candidate": 70,
        "same-connection-cross-account-mirror": 60,
        "closed-reissued-overlap": 55,
        "provider-description-similar": 40,
        "bounded-date-equal-amount": 30,
        "ambiguous-receipt-binding": 20,
    }
    return max((weights.get(reason, 10) for reason in reasons), default=0)


def _candidate_review_groups(source: dict[str, Any]) -> list[dict[str, Any]]:
    detail = source["detail"]
    raw_activities = detail.get("activities")
    raw_groups = detail.get("candidateGroups")
    raw_edges = detail.get("candidateEdges")
    if not all(isinstance(value, list) for value in (raw_activities, raw_groups, raw_edges)):
        raise ReviewError("forensic-detail-invalid")
    activities = {
        str(item.get("activityRef") or ""): item
        for item in raw_activities
        if isinstance(item, dict)
    }
    if "" in activities or len(activities) != len(raw_activities):
        raise ReviewError("forensic-detail-invalid")
    edges_by_group: dict[str, list[dict[str, Any]]] = {}
    for edge in raw_edges:
        if not isinstance(edge, dict):
            raise ReviewError("candidate-graph-stale")
        edges_by_group.setdefault(str(edge.get("groupId") or ""), []).append(edge)
    groups: list[dict[str, Any]] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ReviewError("candidate-group-shape-changed")
        if raw.get("classification") not in {
            "automatic-duplicate",
            "relationship-only",
            "review-required",
        }:
            raise ReviewError("candidate-group-shape-changed")
        refs = raw.get("activityRefs")
        reasons = raw.get("reasonCodes")
        families = raw.get("sourceFamilyCounts")
        cardinality = raw.get("cardinality")
        if (
            not isinstance(refs, list)
            or not refs
            or len(set(refs)) != len(refs)
            or not isinstance(reasons, list)
            or not isinstance(families, dict)
            or cardinality not in {"one-to-one", "one-to-many", "many-to-many"}
        ):
            raise ReviewError("candidate-group-shape-changed")
        member_fingerprints = _member_fingerprints(refs, activities)
        dependent_fingerprints = _dependent_fingerprints(refs, activities)
        reconciliation_fingerprints = _reconciliation_fingerprints(refs, activities)
        rollback_hashes = _rollback_hashes(refs, activities)
        if raw["classification"] == "relationship-only":
            continue
        edge_snapshot = sorted(
            edges_by_group.get(str(raw.get("groupId") or ""), []),
            key=lambda item: str(item.get("candidateId") or ""),
        )
        group_hash = stable_hash(
            {
                "group": raw,
                "edges": edge_snapshot,
                "members": member_fingerprints,
                "dependentState": dependent_fingerprints,
            }
        )
        priority_code, priority_rank = _priority(cardinality, reasons, families)
        lineage_statuses = [
            str(activities[ref].get("lineage", {}).get("status") or "missing")
            for ref in refs
        ]
        groups.append(
            {
                "groupId": str(raw["groupId"]),
                "decisionId": decision_id(str(raw["groupId"])),
                "authorityDomain": "economic-event",
                "reviewKind": "candidate",
                "forensicClassification": str(raw["classification"]),
                "candidateGroupHash": group_hash,
                "cardinality": cardinality,
                "reasonCodes": sorted(str(reason) for reason in reasons),
                "sourceFamilyCounts": {
                    str(key): int(value) for key, value in sorted(families.items())
                },
                "activityRefs": sorted(str(ref) for ref in refs),
                "memberActivityFingerprints": member_fingerprints,
                "expectedDependentStateFingerprints": dependent_fingerprints,
                "lineageStatuses": lineage_statuses,
                "reconciliationFingerprints": reconciliation_fingerprints,
                "reconciliationRequired": bool(reconciliation_fingerprints),
                "dependentStatePresent": any(
                    activities[ref]
                    .get("dependentState", {})
                    .get("wouldLoseOrCascade")
                    is True
                    for ref in refs
                ),
                "availableRollbackEvidenceHashes": rollback_hashes,
                "recommendedEvidenceHashes": sorted(
                    {
                        value
                        for ref in refs
                        for value in activities[ref].get("lineage", {}).values()
                        if isinstance(value, str) and HEX_64.fullmatch(value)
                    }
                ),
                "priorityCode": priority_code,
                "priorityRank": priority_rank,
                "riskScore": _risk_score(
                    cardinality,
                    refs,
                    reasons,
                    activities,
                    bool(reconciliation_fingerprints),
                ),
                "evidenceStrengthScore": _evidence_strength(reasons),
            }
        )
    return groups


def _receipt_binding_groups(source: dict[str, Any]) -> list[dict[str, Any]]:
    activities = {
        str(item["activityRef"]): item for item in source["detail"]["activities"]
    }
    plans: list[dict[str, Any]] = []
    seen_plan_hashes: set[str] = set()
    for plan in source["plans"]:
        plan_hash = str(plan["sha256"])
        if plan_hash in seen_plan_hashes:
            continue
        seen_plan_hashes.add(plan_hash)
        matches = forensic._matching_receipts(plan, source["receipts"])
        receipt_status, _selected_receipt = forensic._receipt_for_plan(
            plan, source["receipts"]
        )
        creates = {
            (
                str(item.get("accountId") or ""),
                str(item.get("idempotencyKey") or ""),
            )
            for item in (plan["value"].get("operations") or {}).get("creates") or []
            if isinstance(item, dict)
        }
        refs = {
            ref
            for ref, activity in activities.items()
            if (
                str(activity.get("raw", {}).get("accountId") or ""),
                str(activity.get("sourceIdentity") or ""),
            )
            in creates
        }
        reconciliation_rows = [
            item
            for item in plan["value"].get("reconciliations") or []
            if isinstance(item, dict)
        ]
        reconciliation_fingerprints = sorted(
            stable_hash(item) for item in reconciliation_rows
        )
        rollback_hashes = sorted(
            stable_hash(item["rollbackPayload"])
            for item in reconciliation_rows
            if "rollbackPayload" in item
        )
        candidate_by_pair = {
            (plan_hash, str(receipt["sha256"])): {
                "applicationPlanSha256": plan_hash,
                "receiptSha256": str(receipt["sha256"]),
                "reconciliationFingerprints": sorted(
                    stable_hash(item)
                    for item in receipt["value"].get("reconciliations") or []
                    if isinstance(item, dict)
                ),
                "rollbackEvidenceHashes": rollback_hashes,
            }
            for receipt in matches
        }
        plans.append(
            {
                "applicationPlanSha256": plan_hash,
                "activityRefs": refs,
                "candidates": [
                    candidate_by_pair[key] for key in sorted(candidate_by_pair)
                ],
                "receiptCount": len(candidate_by_pair),
                "receiptStatus": receipt_status,
            }
        )

    grouped: dict[str, dict[str, Any]] = {}
    for ref in sorted(activities):
        matching_plans = [plan for plan in plans if ref in plan["activityRefs"]]
        if not matching_plans:
            continue
        plan_hashes = sorted(
            plan["applicationPlanSha256"] for plan in matching_plans
        )
        candidates = sorted(
            (
                candidate
                for plan in matching_plans
                for candidate in plan["candidates"]
            ),
            key=lambda item: (
                item["applicationPlanSha256"],
                item["receiptSha256"],
            ),
        )
        if (
            len(matching_plans) == 1
            and matching_plans[0]["receiptCount"] <= 1
            and matching_plans[0]["receiptStatus"] != "ambiguous"
        ):
            continue
        key = stable_hash({"plans": plan_hashes, "candidates": candidates})
        entry = grouped.setdefault(
            key,
            {
                "activityRefs": [],
                "applicationPlanCandidates": plan_hashes,
                "candidates": candidates,
            },
        )
        entry["activityRefs"].append(ref)

    covered_plans = {
        candidate["applicationPlanSha256"]
        for entry in grouped.values()
        for candidate in entry["candidates"]
    }
    for plan in plans:
        if (
            (
                plan["receiptCount"] <= 1
                and plan["receiptStatus"] != "ambiguous"
            )
            or plan["applicationPlanSha256"] in covered_plans
        ):
            continue
        candidates = sorted(
            plan["candidates"],
            key=lambda item: item["receiptSha256"],
        )
        grouped[stable_hash(candidates)] = {
            "activityRefs": sorted(plan["activityRefs"]),
            "applicationPlanCandidates": [
                plan["applicationPlanSha256"]
            ],
            "candidates": candidates,
        }

    groups = []
    for entry in grouped.values():
        refs = sorted(entry["activityRefs"])
        plan_hashes = entry["applicationPlanCandidates"]
        candidates = entry["candidates"]
        reconciliation_fingerprints = sorted(
            {
                fingerprint
                for candidate in candidates
                for fingerprint in candidate["reconciliationFingerprints"]
            }
        )
        rollback_hashes = sorted(
            {
                digest
                for candidate in candidates
                for digest in candidate["rollbackEvidenceHashes"]
            }
        )
        binding_material = {
            "applicationPlanCandidates": plan_hashes,
            "candidates": candidates,
            "activityRefs": refs,
        }
        group_id = stable_hash(["receipt-binding-group", binding_material])
        member_fingerprints = _member_fingerprints(refs, activities)
        dependent_fingerprints = _dependent_fingerprints(refs, activities)
        families = Counter(
            str(activities[ref].get("sourceFamily") or "unknown") for ref in refs
        )
        reasons = ["ambiguous-receipt-binding"]
        groups.append(
            {
                "groupId": group_id,
                "decisionId": decision_id(group_id),
                "authorityDomain": "receipt-binding",
                "reviewKind": "receipt-binding",
                "candidateGroupHash": stable_hash(
                    {
                        "binding": binding_material,
                        "members": member_fingerprints,
                        "dependentState": dependent_fingerprints,
                    }
                ),
                "cardinality": "receipt-binding",
                "reasonCodes": reasons,
                "sourceFamilyCounts": dict(sorted(families.items())),
                "activityRefs": refs,
                "memberActivityFingerprints": member_fingerprints,
                "expectedDependentStateFingerprints": dependent_fingerprints,
                "lineageStatuses": [
                    str(activities[ref].get("lineage", {}).get("status") or "ambiguous")
                    for ref in refs
                ],
                "reconciliationFingerprints": reconciliation_fingerprints,
                "reconciliationRequired": bool(reconciliation_fingerprints),
                "dependentStatePresent": any(
                    activities[ref]
                    .get("dependentState", {})
                    .get("wouldLoseOrCascade")
                    is True
                    for ref in refs
                ),
                "availableRollbackEvidenceHashes": rollback_hashes,
                "recommendedEvidenceHashes": [
                    *plan_hashes,
                    *sorted(
                        {
                            candidate["receiptSha256"]
                            for candidate in candidates
                        }
                    ),
                ],
                "applicationPlanCandidates": plan_hashes,
                "receiptBindingCandidates": candidates,
                "priorityCode": "ambiguous-receipt-reconciliation",
                "priorityRank": PRIORITY_RANK[
                    "ambiguous-receipt-reconciliation"
                ],
                "riskScore": _risk_score(
                    "receipt-binding",
                    refs,
                    reasons,
                    activities,
                    bool(reconciliation_fingerprints),
                ),
                "evidenceStrengthScore": _evidence_strength(reasons),
            }
        )
    return groups


def _decision_template(group: dict[str, Any], queue: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "decisionId": group["decisionId"],
        "candidateGroupId": group["groupId"],
        "decisionType": None,
        "reviewer": None,
        "decidedAt": None,
        "rationale": None,
        "environmentFingerprint": queue["environmentFingerprint"],
        "baselinePublicationId": queue["baselinePublicationId"],
        "forensicPublicationId": queue["forensicPublicationId"],
        "candidateGraphHash": queue["candidateGraphHash"],
        "candidateGroupHash": group["candidateGroupHash"],
        "cardinality": group["cardinality"],
        "reasonCodes": group["reasonCodes"],
        "sourceFamilyCounts": group["sourceFamilyCounts"],
        "memberActivityFingerprints": group["memberActivityFingerprints"],
        "evidenceHashes": [],
        "expectedDependentStateFingerprints": group[
            "expectedDependentStateFingerprints"
        ],
        "rollbackEvidenceHashes": [],
        "survivorActivityRef": None,
        "linkedActivityRefs": [],
        "receiptBinding": None,
        "reconciliationBinding": None,
    }


def _review_graph_hash(groups: list[dict[str, Any]]) -> str:
    return stable_hash(
        [
            {
                "groupId": item["groupId"],
                "authorityDomain": item["authorityDomain"],
                "candidateGroupHash": item["candidateGroupHash"],
                "activityRefs": item["activityRefs"],
            }
            for item in sorted(groups, key=lambda value: value["groupId"])
        ]
    )


def _prepare_queue(source: dict[str, Any], batch_size: int) -> dict[str, Any]:
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size <= 0
        or batch_size > 500
    ):
        raise ReviewError("batch-size-invalid")
    groups = _candidate_review_groups(source) + _receipt_binding_groups(source)
    groups.sort(
        key=lambda item: (
            item["priorityRank"],
            -item["riskScore"],
            -item["evidenceStrengthScore"],
            item["groupId"],
        )
    )
    graph_hash = _review_graph_hash(groups)
    queue_input_hash = stable_hash(
        {
            "baselinePublicationId": source["baselinePublicationId"],
            "forensicPublicationId": source["forensicPublicationId"],
            "forensicCandidateGraphHash": source["forensicCandidateGraphHash"],
            "environmentFingerprint": source["environmentFingerprint"],
            "inputHashes": sorted(item["sha256"] for item in source["inputs"]),
        }
    )
    batches = []
    for priority_code, priority_rank in PRIORITIES:
        members = [
            item for item in groups if item["priorityCode"] == priority_code
        ]
        for offset in range(0, len(members), batch_size):
            chunk = members[offset : offset + batch_size]
            batch_id = "review-batch-" + stable_hash(
                {
                    "queueInputHash": queue_input_hash,
                    "candidateGraphHash": graph_hash,
                    "priorityCode": priority_code,
                    "groupIds": [item["groupId"] for item in chunk],
                }
            )[:24]
            batches.append(
                {
                    "batchId": batch_id,
                    "priorityCode": priority_code,
                    "priorityRank": priority_rank,
                    "groupIds": [item["groupId"] for item in chunk],
                }
            )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "lineage-review-queue",
        "private": True,
        "baselinePublicationId": source["baselinePublicationId"],
        "forensicPublicationId": source["forensicPublicationId"],
        "environmentFingerprint": source["environmentFingerprint"],
        "forensicCandidateGraphHash": source["forensicCandidateGraphHash"],
        "candidateGraphHash": graph_hash,
        "queueInputHash": queue_input_hash,
        "batchSize": batch_size,
        "evidenceCatalog": _evidence_catalog(source),
        "groups": groups,
        "batches": batches,
    }


def _packet(
    queue: dict[str, Any],
    batch: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    by_group = {item["groupId"]: item for item in queue["groups"]}
    activities = {
        str(item["activityRef"]): item for item in source["detail"]["activities"]
    }
    edges_by_group: dict[str, list[dict[str, Any]]] = {}
    for edge in source["detail"]["candidateEdges"]:
        edges_by_group.setdefault(str(edge["groupId"]), []).append(edge)
    packet_groups = []
    for group_id in batch["groupIds"]:
        group = by_group[group_id]
        packet_groups.append(
            {
                "reviewGroup": group,
                "activities": [
                    activities[ref]
                    for ref in group["activityRefs"]
                    if ref in activities
                ],
                "candidateEdges": sorted(
                    edges_by_group.get(group_id, []),
                    key=lambda item: str(item["candidateId"]),
                ),
                "decisionTemplate": _decision_template(group, queue),
            }
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "private-lineage-review-packet",
        "private": True,
        "baselinePublicationId": queue["baselinePublicationId"],
        "forensicPublicationId": queue["forensicPublicationId"],
        "candidateGraphHash": queue["candidateGraphHash"],
        "queueInputHash": queue["queueInputHash"],
        "batch": batch,
        "evidenceCatalog": queue["evidenceCatalog"],
        "groups": packet_groups,
    }


def _packet_markdown(packet: dict[str, Any]) -> bytes:
    lines = [
        "# Private lineage review packet",
        "",
        "> PRIVATE: keep this packet under the external data directory.",
        "",
        f"- Baseline publication: `{packet['baselinePublicationId']}`",
        f"- Forensic publication: `{packet['forensicPublicationId']}`",
        f"- Candidate graph: `{packet['candidateGraphHash']}`",
        f"- Queue input: `{packet['queueInputHash']}`",
        f"- Batch: `{packet['batch']['batchId']}`",
        f"- Priority: `{packet['batch']['priorityCode']}`",
        "",
        "## Evidence catalog",
        "",
        "```json",
        json.dumps(
            packet["evidenceCatalog"],
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        ),
        "```",
        "",
    ]
    for item in packet["groups"]:
        group = item["reviewGroup"]
        lines.extend(
            [
                f"## Decision `{group['decisionId']}`",
                "",
                f"- Group: `{group['groupId']}`",
                f"- Cardinality: `{group['cardinality']}`",
                f"- Reasons: `{', '.join(group['reasonCodes'])}`",
                f"- Dependent state present: `{str(group['dependentStatePresent']).lower()}`",
                f"- Reconciliation attribution required: `{str(group['reconciliationRequired']).lower()}`",
                "",
                "### Candidate evidence",
                "",
                "```json",
                json.dumps(
                    {
                        "activities": item["activities"],
                        "candidateEdges": item["candidateEdges"],
                    },
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=True,
                ),
                "```",
                "",
                "### Decision template",
                "",
                "```json",
                json.dumps(
                    item["decisionTemplate"],
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=True,
                ),
                "```",
                "",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _readiness(
    queue: dict[str, Any], decisions: list[dict[str, Any]]
) -> tuple[dict[str, int], dict[str, int]]:
    by_group = {item["candidateGroupId"]: item for item in decisions}
    binding_by_ref: dict[str, list[tuple[dict[str, Any], dict[str, Any] | None]]] = {}
    for binding_group in queue["groups"]:
        if binding_group["reviewKind"] != "receipt-binding":
            continue
        binding_decision = by_group.get(binding_group["groupId"])
        for ref in binding_group["activityRefs"]:
            binding_by_ref.setdefault(ref, []).append(
                (binding_group, binding_decision)
            )

    def source_lineage_complete(group: dict[str, Any]) -> bool:
        if group["reviewKind"] == "receipt-binding":
            binding_decision = by_group.get(group["groupId"])
            return bool(
                binding_decision and binding_decision.get("receiptBinding")
            )
        return all(
            status == "receipt-proven"
            or any(
                decision and decision.get("receiptBinding")
                for _binding, decision in binding_by_ref.get(ref, [])
            )
            for ref, status in zip(
                group["activityRefs"], group["lineageStatuses"], strict=True
            )
        )

    def expected_reconciliation(
        group: dict[str, Any], decision: dict[str, Any] | None
    ) -> list[str]:
        expected = group["reconciliationFingerprints"]
        if group["reviewKind"] != "receipt-binding" or decision is None:
            return expected
        selected = _selected_receipt_candidate(group, decision)
        return selected["reconciliationFingerprints"] if selected else []

    def reconciliation_complete(
        group: dict[str, Any], decision: dict[str, Any]
    ) -> bool:
        expected = expected_reconciliation(group, decision)
        if expected and not decision.get("reconciliationBinding"):
            return False
        if group["reviewKind"] == "receipt-binding":
            return True
        return all(
            not expected_reconciliation(binding, binding_decision)
            or bool(
                binding_decision
                and binding_decision.get("reconciliationBinding")
            )
            for ref in group["activityRefs"]
            for binding, binding_decision in binding_by_ref.get(ref, [])
        )

    eligibility = Counter(
        {
            "ready-groups": 0,
            "restore-eligible-groups": 0,
            "surgical-eligible-groups": 0,
            "rebuild-eligible-groups": 0,
        }
    )
    gaps: Counter[str] = Counter()
    for group in queue["groups"]:
        decision = by_group.get(group["groupId"])
        if decision is None:
            gaps["missing-decision"] += 1
            if not source_lineage_complete(group):
                gaps["missing-source-lineage"] += 1
            continue
        decision_type = decision["decisionType"]
        if decision_type == "defer-insufficient-evidence":
            gaps["deferred-insufficient-evidence"] += 1
            continue
        lineage_complete = source_lineage_complete(group)
        if not lineage_complete:
            gaps["missing-source-lineage"] += 1
        survivor_complete = (
            decision_type not in SUPPRESSION_TYPES
            or bool(decision.get("survivorActivityRef"))
        )
        if not survivor_complete:
            gaps["missing-survivor"] += 1
        reconciliation_is_complete = reconciliation_complete(group, decision)
        if not reconciliation_is_complete:
            gaps["missing-reconciliation-attribution"] += 1
        graph_complete = (
            decision["expectedDependentStateFingerprints"]
            == group["expectedDependentStateFingerprints"]
            and (
                decision_type != "linked-transfer"
                or decision.get("linkedActivityRefs") == group["activityRefs"]
            )
        )
        if not graph_complete:
            gaps["incomplete-dependent-graph"] += 1
        rollback_complete = (
            decision_type not in SUPPRESSION_TYPES
            or bool(decision["rollbackEvidenceHashes"])
        )
        if not rollback_complete:
            gaps["missing-rollback-evidence"] += 1
        ready = all(
            (
                lineage_complete,
                survivor_complete,
                reconciliation_is_complete,
                graph_complete,
                rollback_complete,
            )
        )
        if not ready:
            continue
        eligibility["ready-groups"] += 1
        eligibility["rebuild-eligible-groups"] += 1
        if decision_type in SUPPRESSION_TYPES:
            eligibility["restore-eligible-groups"] += 1
            eligibility["surgical-eligible-groups"] += 1
        elif decision_type == "linked-transfer":
            eligibility["surgical-eligible-groups"] += 1
    return dict(sorted(eligibility.items())), dict(sorted(gaps.items()))


def _summary(
    queue: dict[str, Any],
    decisions: list[dict[str, Any]],
    *,
    decision_publication_id: str | None = None,
) -> dict[str, Any]:
    readiness, gaps = _readiness(queue, decisions)
    decided_groups = {item["candidateGroupId"] for item in decisions}
    priorities = Counter(item["priorityCode"] for item in queue["groups"])
    cardinalities = Counter(item["cardinality"] for item in queue["groups"])
    priority_counts = {code: priorities[code] for code, _rank in PRIORITIES}
    counts = {
        "queue-groups": len(queue["groups"]),
        "queue-batches": len(queue["batches"]),
        "reviewed-decisions": len(decisions),
        "resolved-groups": len(decided_groups),
        "unresolved-groups": len(queue["groups"]) - len(decided_groups),
    }
    status_value = (
        "ready"
        if counts["unresolved-groups"] == 0
        and readiness["ready-groups"] == counts["queue-groups"]
        else "review-required"
    )
    summary = {
        "schemaVersion": SCHEMA_VERSION,
        "privateDetailExcluded": True,
        "status": status_value,
        "baselinePublicationId": queue["baselinePublicationId"],
        "forensicPublicationId": queue["forensicPublicationId"],
        "candidateGraphHash": queue["candidateGraphHash"],
        "queueInputHash": queue["queueInputHash"],
        "decisionPublicationId": decision_publication_id,
        "counts": counts,
        "priorityCounts": priority_counts,
        "cardinalityCounts": dict(sorted(cardinalities.items())),
        "readinessCounts": readiness,
        "evidenceGapCounts": gaps,
    }
    _validate_shareable(summary)
    return summary


def _validate_shareable(summary: dict[str, Any]) -> None:
    if set(summary) != {
        "schemaVersion",
        "privateDetailExcluded",
        "status",
        "baselinePublicationId",
        "forensicPublicationId",
        "candidateGraphHash",
        "queueInputHash",
        "decisionPublicationId",
        "counts",
        "priorityCounts",
        "cardinalityCounts",
        "readinessCounts",
        "evidenceGapCounts",
    }:
        raise ReviewError("shareable-summary-unsafe")
    hashes = [
        summary["baselinePublicationId"],
        summary["forensicPublicationId"],
        summary["candidateGraphHash"],
        summary["queueInputHash"],
    ]
    if summary["decisionPublicationId"] is not None:
        hashes.append(summary["decisionPublicationId"])
    if not all(isinstance(value, str) and HEX_64.fullmatch(value) for value in hashes):
        raise ReviewError("shareable-summary-unsafe")
    if summary["status"] not in {"review-required", "ready"}:
        raise ReviewError("shareable-summary-unsafe")
    for name in (
        "counts",
        "priorityCounts",
        "cardinalityCounts",
        "readinessCounts",
        "evidenceGapCounts",
    ):
        values = summary[name]
        if not isinstance(values, dict) or not all(
            SAFE_CODE.fullmatch(str(key))
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            for key, value in values.items()
        ):
            raise ReviewError("shareable-summary-unsafe")


def _summary_markdown(summary: dict[str, Any]) -> bytes:
    lines = [
        "# Lineage review status",
        "",
        f"- Status: `{summary['status']}`",
        f"- Baseline publication: `{summary['baselinePublicationId']}`",
        f"- Forensic publication: `{summary['forensicPublicationId']}`",
        f"- Candidate graph: `{summary['candidateGraphHash']}`",
        f"- Queue input: `{summary['queueInputHash']}`",
        "",
    ]
    for title, key in (
        ("Counts", "counts"),
        ("Priority counts", "priorityCounts"),
        ("Cardinality counts", "cardinalityCounts"),
        ("Readiness", "readinessCounts"),
        ("Evidence gaps", "evidenceGapCounts"),
    ):
        lines.extend([f"## {title}", "", "| Status | Count |", "|---|---:|"])
        values = summary[key]
        lines.extend(
            f"| `{name}` | {count} |" for name, count in sorted(values.items())
        )
        if not values:
            lines.append("| `none` | 0 |")
        lines.append("")
    lines.append(
        "Raw account, activity, amount, date, description, and evidence details are intentionally excluded."
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _queue_documents(
    queue: dict[str, Any], source: dict[str, Any]
) -> dict[str, bytes]:
    documents = {"queue.json": json_bytes(queue)}
    for index, batch in enumerate(queue["batches"], 1):
        packet = _packet(queue, batch, source)
        stem = (
            f"{batch['priorityRank']:02d}-{index:04d}-"
            f"{batch['batchId'].removeprefix('review-batch-')[:12]}"
        )
        documents[f"packets/{stem}.json"] = json_bytes(packet)
        documents[f"packets/{stem}.md"] = _packet_markdown(packet)
    summary = _summary(queue, [])
    documents["summary.json"] = json_bytes(summary)
    documents["summary.md"] = _summary_markdown(summary)
    return documents


def _relative_files(path: Path) -> set[str]:
    return {
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_file()
    }


def private_publications_exist(output: Path) -> bool:
    publications = output / "publications"
    return publications.is_dir() and any(
        item.is_dir() and HEX_64.fullmatch(item.name)
        for item in publications.iterdir()
    )


def _current_id(output: Path) -> str | None:
    current_path = output / "current.json"
    if not current_path.is_file():
        return None
    pointer = _safe_json(current_path, "private-current-pointer-invalid")
    if (
        not isinstance(pointer, dict)
        or pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION
        or pointer.get("manifestSha256") != pointer.get("publicationId")
    ):
        raise ReviewError("private-current-pointer-invalid")
    return require_hash(
        pointer.get("publicationId"), "private-current-pointer-invalid"
    )


def _write_synced(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_synced(temporary, content)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _remove_synced(path: Path) -> None:
    if path.exists():
        path.unlink()
        fsync_directory(path.parent)


def _recover_pending(output: Path, kind: str) -> None:
    pending_path = output / ".pending.json"
    if not pending_path.is_file():
        return
    pending = _safe_json(pending_path, "pending-publication-invalid")
    if not isinstance(pending, dict) or set(pending) != {
        "schemaVersion",
        "kind",
        "expectedCurrentPublicationId",
        "publicationId",
    }:
        raise ReviewError("pending-publication-invalid")
    publication_id = require_hash(
        pending.get("publicationId"), "pending-publication-invalid"
    )
    expected_current = pending.get("expectedCurrentPublicationId")
    if expected_current is not None:
        require_hash(expected_current, "pending-publication-invalid")
    if (
        pending.get("schemaVersion") != POINTER_SCHEMA_VERSION
        or pending.get("kind") != kind
    ):
        raise ReviewError("pending-publication-invalid")
    actual_current = _current_id(output)
    if actual_current == publication_id:
        _remove_synced(pending_path)
        return
    if actual_current != expected_current:
        raise ReviewError("pending-publication-conflict")
    publication = output / "publications" / publication_id
    if not publication.is_dir():
        _remove_synced(pending_path)
        return
    manifest_path = publication / "manifest.json"
    if not manifest_path.is_file() or _sha256(manifest_path) != publication_id:
        raise ReviewError("pending-publication-invalid")
    manifest = _safe_json(manifest_path, "pending-publication-invalid")
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("kind") != kind
        or not isinstance(files, dict)
        or _relative_files(publication) != {"manifest.json", *files}
        or any(
            not isinstance(name, str)
            or not isinstance(reference, dict)
            or not (publication / Path(name)).is_file()
            or (publication / Path(name)).stat().st_size
            != reference.get("size")
            or _sha256(publication / Path(name))
            != reference.get("sha256")
            for name, reference in files.items()
        )
    ):
        raise ReviewError("pending-publication-invalid")
    if (
        kind == "lineage-decision-publication"
        and manifest.get("parentDecisionPublicationId")
        != expected_current
    ):
        raise ReviewError("pending-publication-invalid")
    _atomic_write(
        output / "current.json",
        json_bytes(
            {
                "schemaVersion": POINTER_SCHEMA_VERSION,
                "publicationId": publication_id,
                "manifestSha256": publication_id,
            }
        ),
    )
    _remove_synced(pending_path)


def _publish(
    output: Path,
    documents: dict[str, bytes],
    manifest: dict[str, Any],
    *,
    expected_current_id: str | None | object = _NO_CAS,
    before_pointer: Any | None = None,
) -> tuple[str, Path]:
    manifest_content = json_bytes(manifest)
    publication_id = sha256_bytes(manifest_content)
    publications = output / "publications"
    ensure_durable_directory(publications, fsync_directory)
    publication = publications / publication_id
    staging = publications / f".staging-{uuid.uuid4().hex}"
    pending_path = output / ".pending.json"
    publication_ready = False
    staging.mkdir()
    try:
        for name, content in documents.items():
            _write_synced(staging / Path(name), content)
        _write_synced(staging / "manifest.json", manifest_content)
        for directory in sorted(
            {item.parent for item in staging.rglob("*") if item.is_file()},
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            fsync_directory(directory)
        if before_pointer is not None:
            before_pointer()
        if expected_current_id is not _NO_CAS:
            actual_current_id = _current_id(output)
            if actual_current_id != expected_current_id:
                raise ReviewError("concurrent-decision-import")
            _atomic_write(
                pending_path,
                json_bytes(
                    {
                        "schemaVersion": POINTER_SCHEMA_VERSION,
                        "kind": manifest["kind"],
                        "expectedCurrentPublicationId": expected_current_id,
                        "publicationId": publication_id,
                    }
                ),
            )
        if publication.exists():
            expected = {"manifest.json", *documents}
            if _relative_files(publication) != expected or any(
                (publication / name).read_bytes() != content
                for name, content in documents.items()
            ) or (publication / "manifest.json").read_bytes() != manifest_content:
                raise ReviewError("existing-private-publication-corrupt")
            publication_ready = True
        else:
            os.replace(staging, publication)
            fsync_directory(publications)
            publication_ready = True
        pointer = {
            "schemaVersion": POINTER_SCHEMA_VERSION,
            "publicationId": publication_id,
            "manifestSha256": publication_id,
        }
        _atomic_write(output / "current.json", json_bytes(pointer))
        _remove_synced(pending_path)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if not publication_ready:
            _remove_synced(pending_path)
    return publication_id, publication


def _queue_manifest(
    queue: dict[str, Any], documents: dict[str, bytes]
) -> dict[str, Any]:
    summary = _safe_json_bytes(documents["summary.json"])
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "lineage-review-queue-publication",
        "private": True,
        "readOnly": True,
        "baselinePublicationId": queue["baselinePublicationId"],
        "forensicPublicationId": queue["forensicPublicationId"],
        "environmentFingerprint": queue["environmentFingerprint"],
        "forensicCandidateGraphHash": queue["forensicCandidateGraphHash"],
        "candidateGraphHash": queue["candidateGraphHash"],
        "queueInputHash": queue["queueInputHash"],
        "batchSize": queue["batchSize"],
        "counts": summary["counts"],
        "priorityCounts": summary["priorityCounts"],
        "files": {
            name: {"sha256": sha256_bytes(content), "size": len(content)}
            for name, content in sorted(documents.items())
        },
    }


def _safe_json_bytes(content: bytes) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        raise ReviewError("private-publication-invalid") from None


def _current_publication(output: Path, kind: str) -> tuple[Path, str, dict[str, Any]]:
    pointer = _safe_json(output / "current.json", "private-current-pointer-invalid")
    if not isinstance(pointer, dict):
        raise ReviewError("private-current-pointer-invalid")
    publication_id = require_hash(
        pointer.get("publicationId"),
        "private-current-pointer-invalid",
    )
    if (
        pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION
        or pointer.get("manifestSha256") != publication_id
    ):
        raise ReviewError("private-current-pointer-invalid")
    publication = output / "publications" / publication_id
    manifest_path = publication / "manifest.json"
    if not manifest_path.is_file() or _sha256(manifest_path) != publication_id:
        raise ReviewError("private-manifest-hash-mismatch")
    manifest = _safe_json(manifest_path, "private-manifest-invalid")
    if not isinstance(manifest, dict) or manifest.get("kind") != kind:
        raise ReviewError("private-manifest-invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or _relative_files(publication) != {
        "manifest.json",
        *files,
    }:
        raise ReviewError("private-manifest-invalid")
    for name, reference in files.items():
        if not isinstance(name, str) or not isinstance(reference, dict):
            raise ReviewError("private-manifest-invalid")
        path = publication / Path(name)
        if (
            not path.is_file()
            or path.stat().st_size != reference.get("size")
            or _sha256(path) != reference.get("sha256")
        ):
            raise ReviewError("private-output-hash-mismatch")
    return publication, publication_id, manifest


def build(
    data_dir: str | Path,
    *,
    repo_root: str | Path,
    batch_size: int = 50,
) -> dict[str, Any]:
    source = _load_verified_forensic(data_dir, repo_root)
    output = validate_private_output(
        source["root"] / OUTPUT_RELATIVE,
        source["root"],
        source["repoRoot"],
    )
    queue = _prepare_queue(source, batch_size)
    documents = _queue_documents(queue, source)
    manifest = _queue_manifest(queue, documents)
    expected_publication_id = sha256_bytes(json_bytes(manifest))
    decision_output = output / "decisions"
    with decision_state_lock(source["root"], source["repoRoot"]):
        existing_queue_id = _current_id(output)
        decision_history_exists = (
            (decision_output / "current.json").is_file()
            or private_publications_exist(decision_output)
        )
        if (
            decision_history_exists
            and existing_queue_id != expected_publication_id
        ):
            raise ReviewError("queue-change-with-decisions")
        publication_id, _publication = _publish(
            output,
            documents,
            manifest,
            expected_current_id=existing_queue_id,
            before_pointer=lambda: _assert_source_unchanged(source),
        )
    verified_queue, verified_id, _paths, _source = _verify_queue(
        source["root"], source["repoRoot"], source=source
    )
    if verified_id != publication_id:
        raise ReviewError("private-publication-verification-failed")
    return {
        **_summary(verified_queue, []),
        "queuePublicationId": publication_id,
    }


def _verify_queue(
    root: Path,
    repo_root: Path,
    *,
    source: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, tuple[Path, ...], dict[str, Any]]:
    output = validate_private_output(
        root / OUTPUT_RELATIVE, root, repo_root
    )
    publication, publication_id, manifest = _current_publication(
        output, "lineage-review-queue-publication"
    )
    source = source or _load_verified_forensic(root, repo_root)
    queue = _prepare_queue(source, manifest.get("batchSize"))
    documents = _queue_documents(queue, source)
    expected_manifest = _queue_manifest(queue, documents)
    if (
        manifest != expected_manifest
        or set(manifest["files"]) != set(documents)
        or any(
            (publication / name).read_bytes() != content
            for name, content in documents.items()
        )
    ):
        raise ReviewError("queue-semantic-verification-failed")
    stored_queue = _safe_json(publication / "queue.json", "queue-document-invalid")
    if stored_queue != queue:
        raise ReviewError("queue-semantic-verification-failed")
    paths = (
        output / "current.json",
        publication / "manifest.json",
        publication / "queue.json",
    )
    return queue, publication_id, paths, source


def _evidence_files(
    root: Path,
    repo_root: Path,
    raw: Any,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ReviewError("decision-evidence-files-invalid")
    result: list[dict[str, Any]] = []
    paths: set[str] = set()
    hashes: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "kind",
            "size",
            "coversCandidateGroupIds",
        }:
            raise ReviewError("decision-evidence-files-invalid")
        kind = str(item.get("kind") or "")
        if kind not in EVIDENCE_FILE_KINDS:
            raise ReviewError("decision-evidence-files-invalid")
        path_value = str(item.get("path") or "")
        coverage = item.get("coversCandidateGroupIds")
        if (
            not isinstance(coverage, list)
            or any(
                not isinstance(group_id, str)
                or not HEX_64.fullmatch(group_id)
                for group_id in coverage
            )
            or len(set(coverage)) != len(coverage)
            or (kind in ROLLBACK_EVIDENCE_KINDS and not coverage)
        ):
            raise ReviewError("decision-evidence-files-invalid")
        path = _safe_private_path(root, path_value, repo_root)
        relative_path = path.relative_to(root)
        parts = relative_path.parts
        if forensic.baseline._is_downstream_output_path(relative_path) or (
            len(parts) >= 2
            and parts[0].casefold() == "normalized"
            and (
                parts[1].casefold() == "canonical"
                or parts[1].casefold().startswith(".canonical-staging-")
                or parts[1].casefold().startswith(".canonical-backup-")
            )
        ):
            raise ReviewError("decision-evidence-files-invalid")
        review_output = (root / OUTPUT_RELATIVE).resolve()
        if review_output == path or review_output in path.parents:
            raise ReviewError("decision-evidence-files-invalid")
        digest = require_hash(item.get("sha256"), "decision-evidence-files-invalid")
        if (
            not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item["size"] < 0
            or not path.is_file()
            or path.stat().st_size != item.get("size")
            or _sha256(path) != digest
        ):
            raise ReviewError("decision-evidence-file-changed")
        relative = path.relative_to(root).as_posix()
        if relative in paths or digest in hashes:
            raise ReviewError("decision-evidence-files-invalid")
        paths.add(relative)
        hashes.add(digest)
        result.append(
            {
                "path": relative,
                "sha256": digest,
                "kind": kind,
                "size": path.stat().st_size,
                "coversCandidateGroupIds": sorted(coverage),
            }
        )
    return sorted(result, key=lambda item: item["path"])


def _validate_binding_hashes(value: Any, code: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ReviewError(code)
    return sorted(require_hash(item, code) for item in value)


def _validate_decision(
    raw: Any,
    group: dict[str, Any],
    queue: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReviewError("decision-shape-invalid")
    if not DECISION_REQUIRED_FIELDS.issubset(raw) or not set(raw).issubset(
        DECISION_REQUIRED_FIELDS | DECISION_OPTIONAL_FIELDS
    ):
        raise ReviewError("decision-shape-invalid")
    if raw.get("schemaVersion") != SCHEMA_VERSION:
        raise ReviewError("decision-schema-version-unsupported")
    if raw.get("decisionId") != group["decisionId"] or raw.get(
        "candidateGroupId"
    ) != group["groupId"]:
        raise ReviewError("decision-identity-conflict")
    decision_type = str(raw.get("decisionType") or "")
    if decision_type not in DECISION_TYPES:
        raise ReviewError("decision-type-invalid")
    if (
        group["reviewKind"] == "receipt-binding"
        and decision_type
        not in {
            "defer-insufficient-evidence",
            "receipt-binding-resolution",
        }
    ) or (
        group["reviewKind"] != "receipt-binding"
        and decision_type == "receipt-binding-resolution"
    ):
        raise ReviewError("decision-type-conflict")
    reviewer_value = raw.get("reviewer")
    rationale_value = raw.get("rationale")
    if not isinstance(reviewer_value, str) or not isinstance(
        rationale_value, str
    ):
        raise ReviewError("review-metadata-incomplete")
    reviewer = reviewer_value.strip()
    rationale = rationale_value.strip()
    if (
        reviewer.casefold() in PLACEHOLDER_REVIEW
        or rationale.casefold() in PLACEHOLDER_REVIEW
        or len(reviewer) < 3
        or len(rationale) < 20
    ):
        raise ReviewError("review-metadata-incomplete")
    parse_decided_at(raw.get("decidedAt"))
    for key, code in (
        ("environmentFingerprint", "foreign-environment"),
        ("baselinePublicationId", "baseline-publication-stale"),
        ("forensicPublicationId", "forensic-publication-stale"),
        ("candidateGraphHash", "candidate-graph-stale"),
        ("candidateGroupHash", "candidate-group-stale"),
    ):
        require_hash(raw.get(key), code)
    if raw["environmentFingerprint"] != queue["environmentFingerprint"]:
        raise ReviewError("foreign-environment")
    if raw["baselinePublicationId"] != queue["baselinePublicationId"]:
        raise ReviewError("baseline-publication-stale")
    if raw["forensicPublicationId"] != queue["forensicPublicationId"]:
        raise ReviewError("forensic-publication-stale")
    if raw["candidateGraphHash"] != queue["candidateGraphHash"]:
        raise ReviewError("candidate-graph-stale")
    if raw["candidateGroupHash"] != group["candidateGroupHash"]:
        raise ReviewError("candidate-group-stale")
    if raw.get("cardinality") != group["cardinality"]:
        raise ReviewError("candidate-cardinality-changed")
    if raw.get("reasonCodes") != group["reasonCodes"]:
        raise ReviewError("candidate-reasons-changed")
    if raw.get("sourceFamilyCounts") != group["sourceFamilyCounts"]:
        raise ReviewError("candidate-source-families-changed")
    if raw.get("memberActivityFingerprints") != group["memberActivityFingerprints"]:
        raise ReviewError("candidate-members-changed")
    if raw.get("expectedDependentStateFingerprints") != group[
        "expectedDependentStateFingerprints"
    ]:
        raise ReviewError("dependent-state-drift")
    evidence_hashes = _validate_binding_hashes(
        raw.get("evidenceHashes"), "decision-evidence-invalid"
    )
    if not evidence_hashes or any(item not in evidence for item in evidence_hashes):
        raise ReviewError("decision-evidence-invalid")
    rollback_hashes = _validate_binding_hashes(
        raw.get("rollbackEvidenceHashes"), "rollback-evidence-invalid"
    )
    related_rollback_hashes = {
        digest
        for candidate_group in queue["groups"]
        if candidate_group["reviewKind"] == "receipt-binding"
        and set(candidate_group["activityRefs"]).intersection(
            group["activityRefs"]
        )
        for digest in candidate_group["availableRollbackEvidenceHashes"]
    }
    allowed_embedded_rollback = {
        *group["availableRollbackEvidenceHashes"],
        *related_rollback_hashes,
    }
    if any(
        item not in evidence
        or evidence[item]["kind"] not in ROLLBACK_EVIDENCE_KINDS
        or (
            evidence[item]["kind"] == "embedded-rollback"
            and item not in allowed_embedded_rollback
        )
        or (
            evidence[item]["kind"] != "embedded-rollback"
            and group["groupId"]
            not in evidence[item].get("coversCandidateGroupIds", [])
        )
        for item in rollback_hashes
    ) or not set(rollback_hashes).issubset(evidence_hashes):
        raise ReviewError("rollback-evidence-invalid")
    refs = group["activityRefs"]
    survivor = raw.get("survivorActivityRef")
    if decision_type in SUPPRESSION_TYPES:
        if survivor not in refs:
            raise ReviewError("survivor-outside-group")
    elif survivor not in (None, ""):
        raise ReviewError("survivor-not-allowed")
    linked = raw.get("linkedActivityRefs", [])
    if decision_type == "linked-transfer":
        if linked != refs:
            raise ReviewError("linked-member-omission")
    elif linked not in (None, []):
        raise ReviewError("linked-members-not-allowed")
    receipt_binding = raw.get("receiptBinding")
    expected_reconciliation_fingerprints = group[
        "reconciliationFingerprints"
    ]
    if group["reviewKind"] == "receipt-binding":
        candidates = group["receiptBindingCandidates"]
        if decision_type == "defer-insufficient-evidence":
            if receipt_binding is not None:
                raise ReviewError("receipt-binding-invalid")
            candidates = []
            selected = []
        else:
            if not candidates:
                raise ReviewError("receipt-binding-invalid")
            if not isinstance(receipt_binding, dict) or set(receipt_binding) != {
                "applicationPlanSha256",
                "receiptSha256",
            }:
                raise ReviewError("receipt-binding-invalid")
            selected = [
                item
                for item in candidates
                if {
                    "applicationPlanSha256": item["applicationPlanSha256"],
                    "receiptSha256": item["receiptSha256"],
                }
                == receipt_binding
            ]
        candidate_pairs = {
            (item["applicationPlanSha256"], item["receiptSha256"])
            for item in candidates
        }
        if decision_type != "defer-insufficient-evidence" and (
            len(selected) != 1 or len(candidate_pairs) != len(candidates)
        ):
            raise ReviewError("receipt-binding-invalid")
        if selected:
            expected_reconciliation_fingerprints = selected[0][
                "reconciliationFingerprints"
            ]
            if not {
                receipt_binding["applicationPlanSha256"],
                receipt_binding["receiptSha256"],
            }.issubset(evidence_hashes):
                raise ReviewError("receipt-binding-invalid")
    elif receipt_binding is not None:
        if not isinstance(receipt_binding, dict) or set(receipt_binding) != {
            "applicationPlanSha256",
            "receiptSha256",
        }:
            raise ReviewError("receipt-binding-invalid")
        if any(
            require_hash(receipt_binding.get(key), "receipt-binding-invalid")
            not in evidence
            for key in ("applicationPlanSha256", "receiptSha256")
        ):
            raise ReviewError("receipt-binding-invalid")
        if not set(receipt_binding.values()).issubset(evidence_hashes):
            raise ReviewError("receipt-binding-invalid")
    reconciliation_binding = raw.get("reconciliationBinding")
    if reconciliation_binding is not None:
        if not isinstance(reconciliation_binding, dict) or set(
            reconciliation_binding
        ) != {"fingerprints"}:
            raise ReviewError("reconciliation-binding-invalid")
        if _validate_binding_hashes(
            reconciliation_binding["fingerprints"],
            "reconciliation-binding-invalid",
        ) != expected_reconciliation_fingerprints:
            raise ReviewError("reconciliation-binding-invalid")
    normalized = {
        key: raw[key] for key in sorted(DECISION_REQUIRED_FIELDS)
    }
    normalized.update(
        {
            "survivorActivityRef": survivor or None,
            "linkedActivityRefs": linked or [],
            "receiptBinding": receipt_binding,
            "reconciliationBinding": reconciliation_binding,
            "evidenceHashes": evidence_hashes,
            "rollbackEvidenceHashes": rollback_hashes,
        }
    )
    return normalized


def _validate_decision_set(
    queue: dict[str, Any],
    raw_decisions: Any,
    evidence_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw_decisions, list):
        raise ReviewError("decision-set-invalid")
    groups = {item["groupId"]: item for item in queue["groups"]}
    evidence = {item["sha256"]: item for item in queue["evidenceCatalog"]}
    for item in evidence_files:
        prior = evidence.get(item["sha256"])
        if prior is not None and prior.get("kind") != item.get("kind"):
            raise ReviewError("decision-evidence-files-invalid")
        evidence[item["sha256"]] = item
    result = []
    claimed_groups: set[str] = set()
    claims: set[tuple[str, str]] = set()
    for raw in raw_decisions:
        group_id = str(raw.get("candidateGroupId") or "") if isinstance(raw, dict) else ""
        group = groups.get(group_id)
        if group is None:
            raise ReviewError("candidate-group-missing")
        if group_id in claimed_groups:
            raise ReviewError("conflicting-decisions")
        decision = _validate_decision(raw, group, queue, evidence)
        claimed_groups.add(group_id)
        for ref in group["activityRefs"]:
            claim = (group["authorityDomain"], ref)
            if claim in claims:
                raise ReviewError("duplicate-member-claim")
            claims.add(claim)
        result.append(decision)
    result = sorted(result, key=lambda item: item["decisionId"])
    by_group = {item["candidateGroupId"]: item for item in result}
    evidence_by_hash = {
        item["sha256"]: item
        for item in [*queue["evidenceCatalog"], *evidence_files]
    }
    for decision in result:
        group = groups[decision["candidateGroupId"]]
        selected_rollback_hashes = {
            digest
            for binding_group in queue["groups"]
            if binding_group["reviewKind"] == "receipt-binding"
            and set(binding_group["activityRefs"]).intersection(
                group["activityRefs"]
            )
            for selected in [
                _selected_receipt_candidate(
                    binding_group,
                    by_group.get(binding_group["groupId"]),
                )
            ]
            if selected is not None
            for digest in selected["rollbackEvidenceHashes"]
        }
        has_selected_binding = any(
            _selected_receipt_candidate(
                binding_group,
                by_group.get(binding_group["groupId"]),
            )
            is not None
            for binding_group in queue["groups"]
            if binding_group["reviewKind"] == "receipt-binding"
            and set(binding_group["activityRefs"]).intersection(
                group["activityRefs"]
            )
        )
        if has_selected_binding and any(
            evidence_by_hash[digest]["kind"] == "embedded-rollback"
            and digest not in {
                *group["availableRollbackEvidenceHashes"],
                *selected_rollback_hashes,
            }
            for digest in decision["rollbackEvidenceHashes"]
        ):
            raise ReviewError("rollback-evidence-invalid")
    return result


def _decision_summary(
    queue: dict[str, Any],
    decisions: list[dict[str, Any]],
    publication_id: str | None,
) -> dict[str, Any]:
    return _summary(
        queue, decisions, decision_publication_id=publication_id
    )


def _decision_documents(
    queue: dict[str, Any],
    decisions: list[dict[str, Any]],
    evidence_files: list[dict[str, Any]],
) -> dict[str, bytes]:
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "verified-lineage-decisions",
        "private": True,
        "environmentFingerprint": queue["environmentFingerprint"],
        "baselinePublicationId": queue["baselinePublicationId"],
        "forensicPublicationId": queue["forensicPublicationId"],
        "candidateGraphHash": queue["candidateGraphHash"],
        "queueInputHash": queue["queueInputHash"],
        "evidenceFiles": evidence_files,
        "decisions": decisions,
    }
    summary = _decision_summary(queue, decisions, None)
    return {
        "decisions.json": json_bytes(document),
        "summary.json": json_bytes(summary),
        "summary.md": _summary_markdown(summary),
    }


def _decision_manifest(
    queue: dict[str, Any],
    queue_publication_id: str,
    decisions: list[dict[str, Any]],
    evidence_files: list[dict[str, Any]],
    documents: dict[str, bytes],
    *,
    parent_publication_id: str | None,
    sequence: int,
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "lineage-decision-publication",
        "private": True,
        "readOnly": True,
        "parentDecisionPublicationId": parent_publication_id,
        "sequence": sequence,
        "queuePublicationId": queue_publication_id,
        "baselinePublicationId": queue["baselinePublicationId"],
        "forensicPublicationId": queue["forensicPublicationId"],
        "environmentFingerprint": queue["environmentFingerprint"],
        "candidateGraphHash": queue["candidateGraphHash"],
        "queueInputHash": queue["queueInputHash"],
        "decisionCount": len(decisions),
        "evidenceFiles": evidence_files,
        "files": {
            name: {"sha256": sha256_bytes(content), "size": len(content)}
            for name, content in sorted(documents.items())
        },
    }


def _decision_history(
    output: Path,
    queue_publication_id: str,
    current_publication_id: str,
) -> None:
    publications = output / "publications"
    scoped: dict[str, dict[str, Any]] = {}
    decision_sets: dict[str, dict[str, dict[str, Any]]] = {}
    if not publications.is_dir():
        raise ReviewError("decision-history-corrupt")
    for publication in publications.iterdir():
        if not publication.is_dir() or not HEX_64.fullmatch(publication.name):
            continue
        manifest_path = publication / "manifest.json"
        if not manifest_path.is_file() or _sha256(manifest_path) != publication.name:
            raise ReviewError("decision-history-corrupt")
        manifest = _safe_json(manifest_path, "decision-history-corrupt")
        if not isinstance(manifest, dict):
            raise ReviewError("decision-history-corrupt")
        if manifest.get("kind") != "lineage-decision-publication":
            continue
        if manifest.get("queuePublicationId") == queue_publication_id:
            scoped[publication.name] = manifest
    if current_publication_id not in scoped:
        raise ReviewError("decision-history-corrupt")
    for publication_id, manifest in scoped.items():
        files = manifest.get("files")
        publication = publications / publication_id
        if (
            not isinstance(files, dict)
            or _relative_files(publication) != {"manifest.json", *files}
        ):
            raise ReviewError("decision-history-corrupt")
        for name, reference in files.items():
            path = publication / Path(name)
            if (
                not isinstance(name, str)
                or not isinstance(reference, dict)
                or not path.is_file()
                or path.stat().st_size != reference.get("size")
                or _sha256(path) != reference.get("sha256")
            ):
                raise ReviewError("decision-history-corrupt")
        reference = files.get("decisions.json")
        if not isinstance(reference, dict):
            raise ReviewError("decision-history-corrupt")
        document = _hashed_json(
            publications / publication_id / "decisions.json",
            reference,
            "decision-history-corrupt",
        )
        raw_decisions = document.get("decisions") if isinstance(document, dict) else None
        if not isinstance(raw_decisions, list) or any(
            not isinstance(item, dict) for item in raw_decisions
        ):
            raise ReviewError("decision-history-corrupt")
        by_group = {
            str(item.get("candidateGroupId") or ""): item
            for item in raw_decisions
        }
        if "" in by_group or len(by_group) != len(raw_decisions):
            raise ReviewError("decision-history-corrupt")
        decision_sets[publication_id] = by_group
    children: dict[str, list[str]] = {}
    for publication_id, manifest in scoped.items():
        parent = manifest.get("parentDecisionPublicationId")
        sequence = manifest.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            raise ReviewError("decision-history-corrupt")
        if parent is None:
            if sequence != 1:
                raise ReviewError("decision-history-corrupt")
            continue
        if (
            not isinstance(parent, str)
            or not HEX_64.fullmatch(parent)
            or parent not in scoped
            or scoped[parent].get("sequence") != sequence - 1
        ):
            raise ReviewError("decision-history-corrupt")
        parent_decisions = decision_sets[parent]
        child_decisions = decision_sets[publication_id]
        parent_evidence_rows = scoped[parent].get("evidenceFiles")
        child_evidence_rows = manifest.get("evidenceFiles")
        if not isinstance(parent_evidence_rows, list) or not isinstance(
            child_evidence_rows, list
        ):
            raise ReviewError("decision-history-corrupt")
        parent_evidence = {
            item.get("sha256"): item
            for item in parent_evidence_rows
            if isinstance(item, dict)
        }
        child_evidence = {
            item.get("sha256"): item
            for item in child_evidence_rows
            if isinstance(item, dict)
        }
        if len(parent_evidence) != len(parent_evidence_rows) or len(
            child_evidence
        ) != len(child_evidence_rows):
            raise ReviewError("decision-history-corrupt")
        if not set(parent_decisions).issubset(child_decisions) or any(
            prior != child_decisions[group_id]
            and not _is_evidence_enrichment(
                prior, child_decisions[group_id]
            )
            for group_id, prior in parent_decisions.items()
        ):
            raise ReviewError("decision-history-regression")
        if not set(parent_evidence).issubset(child_evidence) or any(
            item != child_evidence[digest]
            for digest, item in parent_evidence.items()
        ):
            raise ReviewError("decision-history-regression")
        children.setdefault(parent, []).append(publication_id)
    if any(len(items) != 1 for items in children.values()):
        raise ReviewError("decision-history-fork")
    heads = sorted(set(scoped) - set(children))
    if heads != [current_publication_id]:
        raise ReviewError("decision-history-rollback")
    visited: set[str] = set()
    current: str | None = current_publication_id
    while current is not None:
        if current in visited:
            raise ReviewError("decision-history-corrupt")
        visited.add(current)
        current = scoped[current].get("parentDecisionPublicationId")
    if visited != set(scoped):
        raise ReviewError("decision-history-fork")


@contextmanager
def _decision_import_lock(output: Path) -> Iterable[None]:
    ensure_durable_directory(output, fsync_directory)
    lock_path = output / ".import.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        except OSError:
            raise ReviewError("decision-import-in-progress") from None
        fsync_directory(output)
        yield
    finally:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


@contextmanager
def decision_state_lock(
    data_dir: str | Path, repo_root: str | Path
) -> Iterable[None]:
    root = Path(data_dir).resolve()
    checkout = Path(repo_root).resolve()
    output = validate_private_output(
        root / "audit" / ".lineage-state", root, checkout
    )
    with _decision_import_lock(output):
        _recover_pending(
            root / OUTPUT_RELATIVE,
            "lineage-review-queue-publication",
        )
        _recover_pending(
            root / OUTPUT_RELATIVE / "decisions",
            "lineage-decision-publication",
        )
        yield


def _load_decision_input(
    root: Path, repo_root: Path, input_path: str | Path
) -> dict[str, Any]:
    path = _safe_private_path(root, str(input_path), repo_root)
    inbox = (root / DECISION_INPUT_RELATIVE).resolve()
    if inbox not in path.parents:
        raise ReviewError("decision-input-location-invalid")
    document = _safe_json(path, "decision-input-invalid")
    if not isinstance(document, dict) or set(document) != TOP_LEVEL_DECISION_FIELDS:
        raise ReviewError("decision-input-invalid")
    return document


def _validate_top_level_decision_input(
    document: dict[str, Any], queue: dict[str, Any]
) -> None:
    if document.get("schemaVersion") != SCHEMA_VERSION or document.get(
        "kind"
    ) != "lineage-review-decisions":
        raise ReviewError("decision-input-invalid")
    for field, code in (
        ("environmentFingerprint", "foreign-environment"),
        ("baselinePublicationId", "baseline-publication-stale"),
        ("forensicPublicationId", "forensic-publication-stale"),
        ("candidateGraphHash", "candidate-graph-stale"),
        ("queueInputHash", "queue-input-stale"),
    ):
        require_hash(document.get(field), code)
        if document[field] != queue[field]:
            raise ReviewError(code)


def _load_current_decisions(
    root: Path,
    repo_root: Path,
    queue: dict[str, Any],
    queue_publication_id: str,
    *,
    required: bool,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    str | None,
    tuple[Path, ...],
    int,
]:
    output = root / OUTPUT_RELATIVE / "decisions"
    if not (output / "current.json").is_file():
        if private_publications_exist(output):
            raise ReviewError("decision-history-rollback")
        if required:
            raise ReviewError("decision-publication-missing")
        return [], [], None, (), 0
    publication, publication_id, manifest = _current_publication(
        output, "lineage-decision-publication"
    )
    _decision_history(output, queue_publication_id, publication_id)
    parent_publication_id = manifest.get("parentDecisionPublicationId")
    sequence = manifest.get("sequence")
    document = _safe_json(
        publication / "decisions.json", "decision-publication-invalid"
    )
    if not isinstance(document, dict) or document.get("kind") != "verified-lineage-decisions":
        raise ReviewError("decision-publication-invalid")
    evidence_files = _evidence_files(
        root, repo_root, document.get("evidenceFiles")
    )
    decisions = _validate_decision_set(
        queue, document.get("decisions"), evidence_files
    )
    expected_documents = _decision_documents(queue, decisions, evidence_files)
    expected_manifest = _decision_manifest(
        queue,
        queue_publication_id,
        decisions,
        evidence_files,
        expected_documents,
        parent_publication_id=parent_publication_id,
        sequence=sequence,
    )
    if manifest != expected_manifest or any(
        (publication / name).read_bytes() != content
        for name, content in expected_documents.items()
    ):
        raise ReviewError("decision-semantic-verification-failed")
    paths = (
        output / "current.json",
        publication / "manifest.json",
        publication / "decisions.json",
        *(root / item["path"] for item in evidence_files),
    )
    return decisions, evidence_files, publication_id, paths, sequence


def _is_evidence_enrichment(
    prior: dict[str, Any], replacement: dict[str, Any]
) -> bool:
    extensible = {
        "evidenceHashes",
        "rollbackEvidenceHashes",
        "reconciliationBinding",
    }
    if any(
        prior.get(key) != replacement.get(key)
        for key in set(prior) | set(replacement)
        if key not in extensible
    ):
        return False
    if not set(prior["evidenceHashes"]).issubset(replacement["evidenceHashes"]):
        return False
    if not set(prior["rollbackEvidenceHashes"]).issubset(
        replacement["rollbackEvidenceHashes"]
    ):
        return False
    prior_reconciliation = prior.get("reconciliationBinding")
    replacement_reconciliation = replacement.get("reconciliationBinding")
    return prior_reconciliation is None or (
        prior_reconciliation == replacement_reconciliation
    )


def import_decisions(
    data_dir: str | Path,
    input_path: str | Path,
    *,
    repo_root: str | Path,
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    checkout = Path(repo_root).resolve()
    output = validate_private_output(
        root / OUTPUT_RELATIVE / "decisions", root, checkout
    )
    with decision_state_lock(root, checkout):
        queue, queue_publication_id, _queue_paths, _source = _verify_queue(
            root, checkout
        )
        document = _load_decision_input(root, checkout, input_path)
        _validate_top_level_decision_input(document, queue)
        incoming_evidence = _evidence_files(
            root, checkout, document.get("evidenceFiles")
        )
        incoming = _validate_decision_set(
            queue, document.get("decisions"), incoming_evidence
        )
        if not incoming:
            raise ReviewError("decision-input-empty")
        (
            existing,
            existing_evidence,
            existing_id,
            _paths,
            existing_sequence,
        ) = _load_current_decisions(
            root,
            checkout,
            queue,
            queue_publication_id,
            required=False,
        )
        by_group = {item["candidateGroupId"]: item for item in existing}
        for item in incoming:
            prior = by_group.get(item["candidateGroupId"])
            if (
                prior is not None
                and prior != item
                and not _is_evidence_enrichment(prior, item)
            ):
                raise ReviewError("conflicting-decisions")
            by_group[item["candidateGroupId"]] = item
        evidence_by_hash = {
            item["sha256"]: item for item in existing_evidence
        }
        for item in incoming_evidence:
            prior = evidence_by_hash.get(item["sha256"])
            if prior is not None and prior != item:
                raise ReviewError("decision-evidence-files-invalid")
            evidence_by_hash[item["sha256"]] = item
        path_claims: dict[str, str] = {}
        for item in evidence_by_hash.values():
            previous = path_claims.setdefault(item["path"], item["sha256"])
            if previous != item["sha256"]:
                raise ReviewError("decision-evidence-files-invalid")
        decisions = _validate_decision_set(
            queue,
            list(by_group.values()),
            list(evidence_by_hash.values()),
        )
        evidence_files = sorted(
            evidence_by_hash.values(), key=lambda item: item["path"]
        )
        if (
            existing_id is not None
            and decisions == existing
            and evidence_files == existing_evidence
        ):
            return {
                **_decision_summary(queue, existing, existing_id),
                "queuePublicationId": queue_publication_id,
                "priorDecisionPublicationId": existing_id,
            }
        documents = _decision_documents(queue, decisions, evidence_files)
        sequence = existing_sequence + 1
        manifest = _decision_manifest(
            queue,
            queue_publication_id,
            decisions,
            evidence_files,
            documents,
            parent_publication_id=existing_id,
            sequence=sequence,
        )

        def verify_before_pointer() -> None:
            _assert_source_unchanged(_source)
            _evidence_files(root, checkout, evidence_files)
            if _current_id(root / OUTPUT_RELATIVE) != queue_publication_id:
                raise ReviewError("queue-publication-changed")

        publication_id, _publication = _publish(
            output,
            documents,
            manifest,
            expected_current_id=existing_id,
            before_pointer=verify_before_pointer,
        )
        (
            verified,
            _evidence,
            verified_id,
            _paths,
            _verified_sequence,
        ) = _load_current_decisions(
            root,
            checkout,
            queue,
            queue_publication_id,
            required=True,
        )
        if verified_id != publication_id:
            raise ReviewError("private-publication-verification-failed")
        return {
            **_decision_summary(queue, verified, verified_id),
            "queuePublicationId": queue_publication_id,
            "priorDecisionPublicationId": existing_id,
        }


def verified_state(
    data_dir: str | Path,
    *,
    repo_root: str | Path,
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    checkout = Path(repo_root).resolve()
    queue, queue_id, queue_paths, source = _verify_queue(root, checkout)
    (
        decisions,
        evidence,
        decision_id_value,
        decision_paths,
        _decision_sequence,
    ) = _load_current_decisions(
        root,
        checkout,
        queue,
        queue_id,
        required=False,
    )
    return {
        "queue": queue,
        "queuePublicationId": queue_id,
        "decisions": decisions,
        "decisionPublicationId": decision_id_value,
        "evidenceFiles": evidence,
        "activities": {
            str(item["activityRef"]): item
            for item in source["detail"]["activities"]
        },
        "sourcePaths": (*queue_paths, *decision_paths),
    }


def verify(
    data_dir: str | Path,
    *,
    repo_root: str | Path,
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    checkout = Path(repo_root).resolve()
    with decision_state_lock(root, checkout):
        state = verified_state(root, repo_root=checkout)
        return {
            **_decision_summary(
                state["queue"],
                state["decisions"],
                state["decisionPublicationId"],
            ),
            "queuePublicationId": state["queuePublicationId"],
        }


def status(
    data_dir: str | Path,
    *,
    repo_root: str | Path,
) -> dict[str, Any]:
    return verify(data_dir, repo_root=repo_root)
