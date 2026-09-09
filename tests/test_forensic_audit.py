import copy
import hashlib
import inspect
import json
import shutil
from pathlib import Path

import pytest

from importers.audit import forensic
from importers.audit.forensic import ForensicAuditError
from importers.rebuild.safety import plan_fingerprint

BASELINE_ENVIRONMENT = "e" * 64
FOREIGN_ENVIRONMENT = "f" * 64


def activity(
    activity_id,
    account_id,
    effect,
    source,
    description,
    *,
    on="2026-08-26T12:00:00Z",
    group=None,
    metadata=None,
):
    amount = abs(effect)
    return {
        "id": activity_id,
        "accountId": account_id,
        "activityType": "DEPOSIT" if effect > 0 else "WITHDRAWAL",
        "date": on,
        "amount": str(amount),
        "currency": "USD",
        "comment": description,
        "idempotencyKey": source,
        "sourceGroupId": group,
        "metadata": metadata,
    }


def normalized(rows, accounts=None):
    return forensic._normalize_activities(
        rows,
        accounts or [{"id": "acct-1", "institution": "synthetic"}],
        "a" * 64,
    )


def reasons(rows, accounts=None):
    edges = forensic._detect_edges(normalized(rows, accounts))
    return {reason for edge in edges for reason in edge.reasons}, edges


def test_screenshot_pattern_and_monarch_extract_ambiguity_require_review():
    rows = [
        activity(
            "monarch",
            "acct-1",
            -20,
            "monarch:m-1",
            "Synthetic Market",
            on="2026-08-26T00:00:00Z",
        ),
        activity(
            "extract",
            "acct-1",
            -20,
            "extract:stable:e-1",
            "Synthetic Market",
        ),
        activity(
            "simplefin",
            "acct-1",
            -20,
            "simplefin:acct-1:s-1",
            "Synthetic Market",
        ),
    ]

    activities = normalized(rows)
    edges = forensic._detect_edges(activities)
    groups, _ = forensic._candidate_groups(
        edges, {item.ref: item for item in activities}
    )

    assert {item.source_date for item in activities} == {"2026-08-26"}
    assert len(edges) == 3
    assert {edge.classification for edge in edges} == {"review-required"}
    assert {edge.reasons for edge in edges} == {("exact-cross-source",)}
    assert groups[0]["ambiguityCardinality"] == 3
    assert groups[0]["cardinality"] == "one-to-many"


def test_only_exact_account_scoped_source_identity_is_automatic():
    rows = [
        activity("left", "acct-1", -11, "monarch:same", "Synthetic A"),
        activity("right", "acct-1", -11, "monarch:same", "Synthetic A"),
        activity("other-account", "acct-2", -11, "monarch:same", "Synthetic A"),
    ]
    accounts = [
        {"id": "acct-1", "institution": "synthetic"},
        {"id": "acct-2", "institution": "synthetic"},
    ]

    _reason_codes, edges = reasons(rows, accounts)

    automatic = [edge for edge in edges if edge.classification == "automatic-duplicate"]
    assert len(automatic) == 1
    assert automatic[0].reasons == ("exact-source-identity",)


def test_legitimate_same_source_repeats_are_not_suppressed():
    rows = [
        activity("first", "acct-1", -13, "monarch:first", "Synthetic Repeat"),
        activity("second", "acct-1", -13, "monarch:second", "Synthetic Repeat"),
    ]

    _reason_codes, edges = reasons(rows)

    assert edges == []


def test_opaque_ids_are_provider_scoped_and_unknown_families_are_private():
    left = activity("left", "acct-1", -12, "opaque-1", "Synthetic Repeat")
    left["sourceSystem"] = "monarch"
    right = activity("right", "acct-1", -12, "opaque-1", "Synthetic Repeat")
    right["sourceSystem"] = "simplefin"
    hostile = activity(
        "hostile",
        "acct-1",
        -12,
        "PRIVATE PERSON BANK:opaque-1",
        "Synthetic Repeat",
    )

    activities = normalized([left, right, hostile])
    edges = forensic._detect_edges(activities)

    assert not any(edge.classification == "automatic-duplicate" for edge in edges)
    assert {item.source_family for item in activities} == {
        "monarch",
        "simplefin",
        "unknown",
    }


def test_amount_candidates_are_currency_scoped_and_accounts_are_complete():
    rows = [
        activity("usd", "acct-1", -19, "monarch:usd", "Synthetic Same"),
        activity("eur", "acct-1", -19, "simplefin:acct-1:eur", "Synthetic Same"),
    ]
    rows[1]["currency"] = "EUR"

    _reason_codes, edges = reasons(rows)

    assert edges == []
    with pytest.raises(ForensicAuditError, match="unknown account"):
        normalized([activity("missing", "missing", -1, "monarch:x", "Synthetic")])


def test_prefix_and_token_similarity_never_auto_classify():
    rows = [
        activity(
            "long",
            "acct-1",
            -14,
            "monarch:long",
            "Synthetic Provider Merchant Location",
        ),
        activity(
            "short",
            "acct-1",
            -14,
            "simplefin:acct-1:short",
            "Synthetic Provider Merchant",
            on="2026-08-27T12:00:00Z",
        ),
    ]

    reason_codes, edges = reasons(rows)

    assert reason_codes == {
        "bounded-date-equal-amount",
        "provider-description-similar",
    }
    assert edges[0].classification == "review-required"


def test_many_to_many_graph_cardinality_is_explicit():
    rows = [
        activity("m1", "acct-1", -15, "monarch:m1", "Synthetic One"),
        activity(
            "m2",
            "acct-1",
            -15,
            "monarch:m2",
            "Synthetic Two",
            on="2026-08-27T12:00:00Z",
        ),
        activity("s1", "acct-1", -15, "simplefin:acct-1:s1", "Provider One"),
        activity(
            "s2",
            "acct-1",
            -15,
            "simplefin:acct-1:s2",
            "Provider Two",
            on="2026-08-27T12:00:00Z",
        ),
    ]
    activities = normalized(rows)
    edges = forensic._detect_edges(activities)

    groups, _ = forensic._candidate_groups(
        edges, {item.ref: item for item in activities}
    )

    assert groups[0]["cardinality"] == "many-to-many"
    assert groups[0]["ambiguityCardinality"] == 4


def test_cross_account_mirrors_transfers_and_closed_reissued_overlap():
    accounts = [
        {
            "id": "checking",
            "institution": "synthetic-bank",
            "connectionId": "connection-1",
            "isArchived": True,
        },
        {
            "id": "reissued",
            "institution": "synthetic-bank",
            "connectionId": "connection-1",
        },
        {"id": "savings", "institution": "synthetic-bank"},
    ]
    rows = [
        activity("old", "checking", -16, "monarch:old", "Synthetic Mirror"),
        activity(
            "new",
            "reissued",
            -16,
            "simplefin:reissued:new",
            "Synthetic Mirror",
        ),
        activity(
            "transfer-out",
            "checking",
            -17,
            "monarch:out",
            "Synthetic Transfer",
            group="transfer-group",
        ),
        activity(
            "transfer-in",
            "savings",
            17,
            "monarch:in",
            "Synthetic Transfer",
            group="transfer-group",
        ),
    ]

    reason_codes, edges = reasons(rows, accounts)

    assert "same-connection-cross-account-mirror" in reason_codes
    assert "closed-reissued-overlap" in reason_codes
    assert "linked-transfer" in reason_codes
    assert "transfer-candidate" in reason_codes
    transfer_edges = [
        edge
        for edge in edges
        if {"linked-transfer", "transfer-candidate"} & set(edge.reasons)
    ]
    assert transfer_edges
    assert all(
        edge.classification == "relationship-only"
        for edge in transfer_edges
    )
    assert all(
        edge.classification == "review-required"
        for edge in edges
        if edge not in transfer_edges
    )


def test_source_group_requires_opposite_signs_on_different_accounts():
    accounts = [
        {"id": "acct-1", "institution": "synthetic"},
        {"id": "acct-2", "institution": "synthetic"},
    ]
    for amount in (-17, 0):
        rows = [
            activity(
                "left",
                "acct-1",
                amount,
                "monarch:left",
                "Synthetic Group Conflict",
                group="synthetic-group",
            ),
            activity(
                "right",
                "acct-2",
                amount,
                "monarch:right",
                "Synthetic Group Conflict",
                group="synthetic-group",
            ),
        ]

        reason_codes, edges = reasons(rows, accounts)

        assert "source-group-conflict" in reason_codes
        assert {edge.classification for edge in edges} == {
            "review-required"
        }
        assert "transfer-candidate" not in reason_codes


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = forensic._json_bytes(value)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def domain(publication: Path, name: str, records, *, status="available"):
    path = publication / "domains" / f"{name}.json"
    sha256 = write_json(
        path,
        {
            "schemaVersion": 1,
            "domain": name,
            "status": status,
            **(
                {"recordCount": len(records), "records": records}
                if status == "available"
                else {
                    "gap": {
                        "status": status,
                        "reasonType": "http-status",
                        "method": "GET",
                        "endpoint": "/synthetic",
                    }
                }
            ),
        },
    )
    return {
        f"{name}.json": {
            "path": f"domains/{name}.json",
            "sha256": sha256,
            "recordCount": len(records),
        }
    }


def evidence_entry(root: Path, path: Path, kind: str):
    return {
        "path": path.relative_to(root).as_posix(),
        "kind": kind,
        "size": path.stat().st_size,
        "sha256": forensic._sha256(path),
        "omitted": False,
    }


def private_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    include_receipt=True,
    receipt_kind=None,
    include_non_cash=False,
    include_foreign_environment=False,
    stale_matching_binding=False,
    stale_foreign_binding=False,
    plan_environment=BASELINE_ENVIRONMENT,
    unavailable_domain=None,
    receipt_schema_version=5,
    plan_schema_version=3,
):
    root = tmp_path / "private"
    root.mkdir()
    snapshot_path = root / "raw" / "simplefin" / "snapshot.json"
    snapshot_sha = write_json(snapshot_path, {"accounts": [], "errors": []})
    reviewed_path = root / "normalized" / "simplefin" / "plan.json"
    reviewed_sha = write_json(
        reviewed_path,
        {
            "schemaVersion": 1,
            "mode": "plan-only",
            "accounts": [],
            "institutionErrors": [],
        },
    )
    reconciliation = {
        "accountId": "acct-1",
        "activityId": "gap-row",
        "action": "update",
        "before": {"id": "gap-row", "amount": "100"},
        "beforeEffect": "100",
        "transactionEffect": "-20",
        "afterEffect": "120",
        "after": {"id": "gap-row", "amount": "120"},
        "rollbackPayload": {"id": "gap-row", "amount": "100"},
    }
    plan = {
        "schemaVersion": plan_schema_version,
        "mode": "staging-apply-plan",
        "generatedAt": "2026-08-26T12:00:00+00:00",
        "evidence": {
            "reviewedPlan": {"path": str(reviewed_path), "sha256": reviewed_sha},
            "snapshot": {"path": str(snapshot_path), "sha256": snapshot_sha},
        },
        "ledgerFingerprint": "before-ledger",
        "intentFingerprint": "intent",
        "decisionEvidence": None,
        "operations": {
            "creates": [
                {
                    "accountId": "acct-1",
                    "idempotencyKey": "simplefin:acct-1:s-1",
                    "activityType": "WITHDRAWAL",
                    "amount": "20",
                }
            ],
            "updates": [{"id": "gap-row"}],
            "deleteIds": [],
            "metadataFinalizations": [],
        },
        "links": [],
        "reconciliations": [reconciliation],
        "assertions": [
            {
                "accountId": "acct-1",
                "canonicalAccountId": "canonical-synthetic-account",
            }
        ],
        "expectedPostLedgerFingerprint": "after-ledger",
    }
    if plan_schema_version == 1:
        plan.update(
            {
                "portableIntent": {},
                "ledgerAccountIds": ["acct-1"],
                "manual": [],
                "monitors": [],
                "impact": {},
                "spendingWindow": {},
            }
        )
    if plan_environment is not None:
        plan["environmentFingerprint"] = plan_environment
    if stale_matching_binding:
        plan["evidence"]["snapshot"]["sha256"] = "9" * 64
    if include_non_cash:
        plan["operations"]["creates"].append(
            {
                "accountId": "acct-1",
                "idempotencyKey": "simplefin:acct-1:investment-sell",
                "activityType": "SELL",
                "amount": "999",
                "quantity": "3.25",
            }
        )
    plan["planFingerprint"] = plan_fingerprint(plan)
    plan_path = root / "normalized" / "simplefin" / "apply-plan.json"
    plan_sha = write_json(plan_path, plan)
    receipt = {
        "schemaVersion": receipt_schema_version,
        "status": "applied",
        "applicationPlan": str(plan_path),
        "applicationPlanSha256": plan_sha,
        "planFingerprint": plan["planFingerprint"],
        "intentFingerprint": "intent",
        "decisionEvidence": None,
        "preLedgerFingerprint": "before-ledger",
        "postLedgerFingerprint": "after-ledger",
        "operations": {
            key: len(value) for key, value in plan["operations"].items()
        },
        "reconciliations": [reconciliation],
    }
    if plan_environment is not None:
        receipt["environmentFingerprint"] = plan_environment
    receipt_path = (
        root
        / "normalized"
        / "simplefin"
        / "apply-report-2026-08-26-120000-000000.json"
    )
    if include_receipt:
        write_json(receipt_path, receipt)
    foreign_paths = []
    if include_foreign_environment:
        foreign_plan = json.loads(json.dumps(plan))
        foreign_plan["environmentFingerprint"] = FOREIGN_ENVIRONMENT
        foreign_plan["assertions"][0][
            "canonicalAccountId"
        ] = "foreign-canonical-account"
        if stale_foreign_binding:
            foreign_plan["evidence"]["snapshot"]["sha256"] = "8" * 64
        foreign_plan.pop("planFingerprint")
        foreign_plan["planFingerprint"] = plan_fingerprint(foreign_plan)
        foreign_plan_path = (
            root / "normalized" / "simplefin" / "apply-plan-foreign.json"
        )
        foreign_plan_sha = write_json(foreign_plan_path, foreign_plan)
        foreign_receipt = json.loads(json.dumps(receipt))
        foreign_receipt["environmentFingerprint"] = FOREIGN_ENVIRONMENT
        foreign_receipt["applicationPlan"] = str(foreign_plan_path)
        foreign_receipt["applicationPlanSha256"] = foreign_plan_sha
        foreign_receipt["planFingerprint"] = foreign_plan["planFingerprint"]
        foreign_receipt_path = (
            root / "normalized" / "simplefin" / "apply-report-foreign.json"
        )
        write_json(foreign_receipt_path, foreign_receipt)
        foreign_paths = [foreign_plan_path, foreign_receipt_path]

    rows = [
        activity(
            "monarch-row",
            "acct-1",
            -20,
            "monarch:m-1",
            "PRIVATE SYNTHETIC MERCHANT",
            metadata=json.dumps({"projectionRunId": "private-projection-run"}),
        ),
        activity(
            "simplefin-row",
            "acct-1",
            -20,
            "simplefin:acct-1:s-1",
            "PRIVATE SYNTHETIC MERCHANT",
        ),
        activity(
            "legitimate-repeat",
            "acct-1",
            -21,
            "monarch:repeat",
            "PRIVATE LEGITIMATE REPEAT",
        ),
        activity(
            "exact-first",
            "acct-1",
            -22,
            "monarch:exact-identity",
            "PRIVATE EXACT DUPLICATE",
        ),
        activity(
            "exact-second",
            "acct-1",
            -22,
            "monarch:exact-identity",
            "PRIVATE EXACT DUPLICATE",
        ),
    ]
    rows[0]["userNote"] = "PRIVATE SYNTHETIC USER NOTE"
    if include_non_cash:
        buy = activity(
            "investment-buy",
            "acct-1",
            -999,
            "monarch:investment-buy",
            "PRIVATE SYNTHETIC INVESTMENT",
        )
        buy.update({"activityType": "BUY", "quantity": "3.25"})
        sell = activity(
            "investment-sell",
            "acct-1",
            -999,
            "simplefin:acct-1:investment-sell",
            "PRIVATE SYNTHETIC INVESTMENT",
        )
        sell.update({"activityType": "SELL", "quantity": "3.25"})
        rows.extend((buy, sell))
    source_files = [
        evidence_entry(root, snapshot_path, "raw-snapshot"),
        evidence_entry(root, reviewed_path, "plan"),
        evidence_entry(root, plan_path, "plan"),
    ]
    if include_receipt:
        classified_kind = forensic.baseline._artifact_kind(
            receipt_path.relative_to(root)
        )
        source_files.append(
            evidence_entry(
                root,
                receipt_path,
                receipt_kind or classified_kind,
            )
        )
    source_files.extend(
        evidence_entry(
            root,
            path,
            forensic.baseline._artifact_kind(path.relative_to(root)),
        )
        for path in foreign_paths
    )
    publication = root / "audit" / "baselines" / "staging"
    (publication / "domains").mkdir(parents=True)
    accounts = [{"id": "acct-1", "institution": "synthetic"}]
    domain_files = {}
    domain_files.update(domain(publication, "activities", rows))
    domain_files.update(domain(publication, "accounts", accounts))
    domain_files.update(domain(
        publication,
        "assignments",
        {"monarch-row": [{"categoryId": "private-category"}]},
        status="unavailable" if unavailable_domain == "assignments" else "available",
    ))
    domain_files.update(
        domain(publication, "splits", {"monarch-row": [{"amount": "10"}]})
    )
    domain_files.update(domain(
        publication,
        "transfer-groups",
        {"monarch-row": {"pair": {"id": "private-counterpart"}}},
    ))
    domain_files.update(domain(
        publication,
        "spending-activities",
        [
            {
                "id": "monarch-row",
                "eventId": "private-event",
                "metadata": json.dumps(
                    {"importRunId": "private-import-run"}
                ),
            }
        ],
    ))
    baseline_manifest = {
        "schemaVersion": 1,
        "private": True,
        "readOnly": True,
        "generatedAt": "2026-08-26T13:00:00+00:00",
        "environmentFingerprint": BASELINE_ENVIRONMENT,
        "sourceFiles": source_files,
        "domainFiles": domain_files,
    }
    manifest_content = forensic._json_bytes(baseline_manifest)
    baseline_id = hashlib.sha256(manifest_content).hexdigest()
    final_publication = (
        root / "audit" / "baselines" / "publications" / baseline_id
    )
    final_publication.parent.mkdir(parents=True)
    publication.rename(final_publication)
    publication = final_publication
    (publication / "manifest.json").write_bytes(manifest_content)
    write_json(
        root / "audit" / "baselines" / "current.json",
        {
            "schemaVersion": 1,
            "publicationId": baseline_id,
            "manifestSha256": baseline_id,
        },
    )
    monkeypatch.setattr(
        forensic.baseline,
        "verify",
        lambda *_args, **_kwargs: {"verified": True},
    )
    return root


def current_audit(root: Path):
    publication, pointer = forensic._current(root)
    detail = json.loads(
        (publication / "private-audit.json").read_text(encoding="utf-8")
    )
    summary = json.loads((publication / "summary.json").read_text(encoding="utf-8"))
    return publication, pointer, detail, summary


def test_build_inventory_lineage_privacy_and_determinism(tmp_path, monkeypatch):
    root = private_fixture(tmp_path, monkeypatch)

    first = forensic.build(root, repo_root=tmp_path / "repo")
    second = forensic.build(root, repo_root=tmp_path / "repo")

    publication, pointer, detail, summary = current_audit(root)
    assert first["publication"] == second["publication"] == pointer
    assert len(detail["activities"]) == 5
    assert {row["observationStatus"] for row in detail["activities"]} == {
        "candidate",
        "no-candidate",
    }
    victim = next(
        row for row in detail["activities"] if row["activityId"] == "monarch-row"
    )
    assert set(victim["dependentState"]["reasonCodes"]) >= {
        "category-assignment",
        "split",
        "spending-event-link",
        "user-note",
        "metadata",
        "projection-import-run",
        "transfer-counterpart",
    }
    assert set(victim["dependentState"]["projectionImportRunIds"]) == {
        "private-import-run",
        "private-projection-run",
    }
    provider_comment = next(
        row
        for row in detail["activities"]
        if row["activityId"] == "legitimate-repeat"
    )
    assert "user-note" not in provider_comment["dependentState"]["reasonCodes"]
    created = next(
        row for row in detail["activities"] if row["activityId"] == "simplefin-row"
    )
    assert created["lineage"]["status"] == "receipt-proven"
    assert created["lineage"]["receiptProvenReconciliation"] is True
    assert created["lineage"]["reconciliation"]["beforeFingerprint"]
    assert created["lineage"]["reconciliation"]["afterFingerprint"]
    assert len(detail["duplicateEconomicEffects"]) == 1
    assert len(detail["receiptProvenReconciliationEffects"]) == 1
    assert all(
        row["canonicalAccountId"] == "canonical-synthetic-account"
        for row in detail["activities"]
    )
    assert all(row["netted"] is False for row in detail["unresolvedEffects"])
    assert summary["counts"]["baseline-activities"] == 5
    assert summary["counts"]["accounted-activities"] == 5
    assert summary["counts"]["duplicate-economic-effects"] == 1
    assert summary["counts"]["receipt-proven-reconciliation-effects"] == 1
    assert summary["counts"]["unresolved-effect-observations"] == 2
    assert "PRIVATE" not in (publication / "summary.md").read_text(encoding="utf-8")
    assert "private-category" not in json.dumps(summary)
    assert "acct-1" not in json.dumps(summary)
    assert "simplefin-row" not in json.dumps(summary)
    assert (
        json.loads(
            (publication / "review-decisions.json").read_text(encoding="utf-8")
        )["decisions"][0]["status"]
        == "pending"
    )


def test_forensic_publication_is_independent_of_canonical_output(
    tmp_path, monkeypatch
):
    root = private_fixture(tmp_path, monkeypatch)
    canonical = root / "normalized" / "canonical"
    canonical.mkdir(parents=True)
    (canonical / "manifest.json").write_text(
        '{"schemaVersion":"corrupt"}\n', encoding="utf-8"
    )

    forensic.build(root, repo_root=tmp_path / "repo")

    assert forensic.verify(root, repo_root=tmp_path / "repo")["verified"] is True


def test_unproven_reconciliation_is_never_inferred_from_amount(tmp_path, monkeypatch):
    root = private_fixture(tmp_path, monkeypatch, include_receipt=False)

    forensic.build(root, repo_root=tmp_path / "repo")

    _publication, _pointer, detail, summary = current_audit(root)
    created = next(
        row for row in detail["activities"] if row["activityId"] == "simplefin-row"
    )
    assert created["lineage"]["status"] == "plan-only"
    assert created["lineage"]["receiptProvenReconciliation"] is False
    assert created["lineage"]["reconciliation"] is None
    assert summary["counts"]["receipt-proven-reconciliation-effects"] == 0
    assert detail["lineageEvidenceCounts"]["plans-missing-receipt"] == 1
    assert summary["lineageEvidenceCounts"]["plans-missing-receipt"] == 1


def test_identical_plan_and_receipt_copies_do_not_create_false_ambiguity():
    item = normalized([
        activity(
            "simplefin-row",
            "acct-1",
            -20,
            "simplefin:acct-1:s-1",
            "Synthetic evidence",
        )
    ])[0]
    plan = {
        "planFingerprint": "plan-fingerprint",
        "intentFingerprint": "intent",
        "environmentFingerprint": BASELINE_ENVIRONMENT,
        "ledgerFingerprint": "before",
        "expectedPostLedgerFingerprint": "after",
        "decisionEvidence": None,
        "operations": {
            "creates": [{
                "accountId": "acct-1",
                "idempotencyKey": "simplefin:acct-1:s-1",
                "activityType": "WITHDRAWAL",
                "amount": "20",
            }],
            "updates": [],
        },
        "reconciliations": [],
    }
    plan_document = {"sha256": "a" * 64, "value": plan}
    receipt = {
        "applicationPlanSha256": plan_document["sha256"],
        "planFingerprint": plan["planFingerprint"],
        "intentFingerprint": plan["intentFingerprint"],
        "environmentFingerprint": plan["environmentFingerprint"],
        "preLedgerFingerprint": plan["ledgerFingerprint"],
        "postLedgerFingerprint": plan["expectedPostLedgerFingerprint"],
        "decisionEvidence": None,
        "operations": {"creates": 1, "updates": 0},
        "reconciliations": [],
    }
    receipt_document = {"sha256": "b" * 64, "value": receipt}

    lineage = forensic._lineage(
        item,
        [plan_document, copy.deepcopy(plan_document)],
        [receipt_document, copy.deepcopy(receipt_document)],
    )

    assert lineage["status"] == "receipt-proven"
    assert len(
        forensic._matching_receipts(
            plan_document,
            [receipt_document, copy.deepcopy(receipt_document)],
        )
    ) == 1


def test_unique_receipt_proven_plan_outweighs_unapplied_competing_plans():
    item = normalized([
        activity(
            "simplefin-row",
            "acct-1",
            -20,
            "simplefin:acct-1:s-1",
            "Synthetic evidence",
        )
    ])[0]
    applied_plan = {
        "planFingerprint": "applied-plan",
        "intentFingerprint": "intent",
        "environmentFingerprint": BASELINE_ENVIRONMENT,
        "ledgerFingerprint": "before",
        "expectedPostLedgerFingerprint": "after",
        "decisionEvidence": None,
        "operations": {
            "creates": [{
                "accountId": "acct-1",
                "idempotencyKey": "simplefin:acct-1:s-1",
                "activityType": "WITHDRAWAL",
                "amount": "20",
            }],
            "updates": [],
        },
        "reconciliations": [],
    }
    competing_plan = copy.deepcopy(applied_plan)
    competing_plan["planFingerprint"] = "unapplied-plan"
    applied_document = {"sha256": "a" * 64, "value": applied_plan}
    receipt_document = {
        "sha256": "b" * 64,
        "value": {
            "status": "applied",
            "applicationPlanSha256": applied_document["sha256"],
            "planFingerprint": applied_plan["planFingerprint"],
            "intentFingerprint": applied_plan["intentFingerprint"],
            "environmentFingerprint": applied_plan["environmentFingerprint"],
            "preLedgerFingerprint": applied_plan["ledgerFingerprint"],
            "postLedgerFingerprint": applied_plan["expectedPostLedgerFingerprint"],
            "decisionEvidence": None,
            "operations": {"creates": 1, "updates": 0},
            "reconciliations": [],
        },
    }

    lineage = forensic._lineage(
        item,
        [
            applied_document,
            {"sha256": "c" * 64, "value": competing_plan},
        ],
        [receipt_document],
    )

    assert lineage["status"] == "receipt-proven"
    assert lineage["applicationPlanSha256"] == applied_document["sha256"]
    assert lineage["receiptSha256"] == receipt_document["sha256"]


def test_multiple_receipt_proven_competing_plans_remain_ambiguous():
    item = normalized([
        activity(
            "simplefin-row",
            "acct-1",
            -20,
            "simplefin:acct-1:s-1",
            "Synthetic evidence",
        )
    ])[0]
    plans = []
    receipts = []
    for plan_sha, receipt_sha, fingerprint in (
        ("a" * 64, "b" * 64, "first-plan"),
        ("c" * 64, "d" * 64, "second-plan"),
    ):
        plan = {
            "planFingerprint": fingerprint,
            "intentFingerprint": "intent",
            "environmentFingerprint": BASELINE_ENVIRONMENT,
            "ledgerFingerprint": "before",
            "expectedPostLedgerFingerprint": "after",
            "decisionEvidence": None,
            "operations": {
                "creates": [{
                    "accountId": "acct-1",
                    "idempotencyKey": "simplefin:acct-1:s-1",
                    "activityType": "WITHDRAWAL",
                    "amount": "20",
                }],
                "updates": [],
            },
            "reconciliations": [],
        }
        plans.append({"sha256": plan_sha, "value": plan})
        receipts.append({
            "sha256": receipt_sha,
            "value": {
                "status": "applied",
                "applicationPlanSha256": plan_sha,
                "planFingerprint": fingerprint,
                "intentFingerprint": plan["intentFingerprint"],
                "environmentFingerprint": plan["environmentFingerprint"],
                "preLedgerFingerprint": plan["ledgerFingerprint"],
                "postLedgerFingerprint": plan["expectedPostLedgerFingerprint"],
                "decisionEvidence": None,
                "operations": {"creates": 1, "updates": 0},
                "reconciliations": [],
            },
        })

    lineage = forensic._lineage(item, plans, receipts)

    assert lineage["status"] == "ambiguous"
    assert lineage["applicationPlanSha256"] is None
    assert lineage["receiptSha256"] is None


def test_schema_three_applied_receipt_is_valid_historical_lineage(
    tmp_path, monkeypatch
):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        receipt_schema_version=3,
    )

    forensic.build(root, repo_root=tmp_path / "repo")

    _publication, _pointer, detail, summary = current_audit(root)
    created = next(
        row for row in detail["activities"] if row["activityId"] == "simplefin-row"
    )
    assert created["lineage"]["status"] == "receipt-proven"
    assert summary["lineageEvidenceCounts"]["receipts"] == 1


def test_schema_one_plan_and_schema_three_receipt_restore_historical_lineage(
    tmp_path, monkeypatch
):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        plan_schema_version=1,
        receipt_schema_version=3,
    )

    forensic.build(root, repo_root=tmp_path / "repo")

    _publication, _pointer, detail, summary = current_audit(root)
    created = next(
        row for row in detail["activities"] if row["activityId"] == "simplefin-row"
    )
    assert created["lineage"]["status"] == "receipt-proven"
    assert summary["lineageEvidenceCounts"]["plans-proved"] == 1


def test_applied_and_already_applied_receipts_corroborate_one_transition():
    item = normalized([
        activity(
            "simplefin-row",
            "acct-1",
            -20,
            "simplefin:acct-1:s-1",
            "Synthetic evidence",
        )
    ])[0]
    plan = {
        "planFingerprint": "plan-fingerprint",
        "intentFingerprint": "intent",
        "environmentFingerprint": BASELINE_ENVIRONMENT,
        "ledgerFingerprint": "before",
        "expectedPostLedgerFingerprint": "after",
        "decisionEvidence": None,
        "operations": {
            "creates": [
                {
                    "accountId": "acct-1",
                    "idempotencyKey": "simplefin:acct-1:s-1",
                    "activityType": "WITHDRAWAL",
                    "amount": "20",
                }
            ],
            "updates": [],
        },
        "reconciliations": [],
    }
    plan_document = {"sha256": "a" * 64, "value": plan}
    binding = {
        "schemaVersion": 3,
        "applicationPlanSha256": plan_document["sha256"],
        "planFingerprint": plan["planFingerprint"],
        "intentFingerprint": plan["intentFingerprint"],
        "environmentFingerprint": plan["environmentFingerprint"],
        "preLedgerFingerprint": plan["ledgerFingerprint"],
        "postLedgerFingerprint": plan["expectedPostLedgerFingerprint"],
        "decisionEvidence": None,
        "operations": {"creates": 1, "updates": 0},
        "reconciliations": [],
    }
    receipts = [
        {
            "sha256": "b" * 64,
            "value": {**binding, "status": "applied"},
        },
        {
            "sha256": "c" * 64,
            "value": {**binding, "status": "already-applied"},
        },
    ]

    lineage = forensic._lineage(item, [plan_document], receipts)

    assert lineage["status"] == "receipt-proven"
    matching = forensic._matching_receipts(plan_document, receipts)
    assert len(matching) == 1
    assert matching[0]["value"]["status"] == "applied"


def test_conflicting_receipt_transition_remains_ambiguous():
    plan = {
        "planFingerprint": "plan-fingerprint",
        "intentFingerprint": "intent",
        "environmentFingerprint": BASELINE_ENVIRONMENT,
        "ledgerFingerprint": "before",
        "expectedPostLedgerFingerprint": "after",
        "decisionEvidence": None,
        "operations": {"creates": [{}], "updates": []},
        "reconciliations": [],
    }
    plan_document = {"sha256": "a" * 64, "value": plan}
    receipt = {
        "schemaVersion": 3,
        "status": "applied",
        "applicationPlanSha256": plan_document["sha256"],
        "planFingerprint": plan["planFingerprint"],
        "intentFingerprint": plan["intentFingerprint"],
        "environmentFingerprint": plan["environmentFingerprint"],
        "preLedgerFingerprint": plan["ledgerFingerprint"],
        "postLedgerFingerprint": plan["expectedPostLedgerFingerprint"],
        "decisionEvidence": None,
        "operations": {"creates": 1, "updates": 0},
        "reconciliations": [],
    }
    conflicting = {
        **receipt,
        "status": "already-applied",
        "postLedgerFingerprint": "different-transition",
    }

    status, selected = forensic._receipt_for_plan(
        plan_document,
        [
            {"sha256": "b" * 64, "value": receipt},
            {"sha256": "c" * 64, "value": conflicting},
        ],
    )

    assert status == "ambiguous"
    assert selected is None


def test_other_evidence_apply_report_is_detected_by_validated_shape(
    tmp_path, monkeypatch
):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        receipt_kind="other-evidence",
    )

    forensic.build(root, repo_root=tmp_path / "repo")

    _publication, _pointer, detail, summary = current_audit(root)
    created = next(
        row for row in detail["activities"] if row["activityId"] == "simplefin-row"
    )
    assert created["lineage"]["status"] == "receipt-proven"
    assert summary["lineageEvidenceCounts"]["receipts"] == 1


def test_only_exact_baseline_environment_can_bind_lineage_and_aliases(
    tmp_path, monkeypatch
):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        include_foreign_environment=True,
    )

    forensic.build(root, repo_root=tmp_path / "repo")

    _publication, _pointer, detail, summary = current_audit(root)
    created = next(
        row for row in detail["activities"] if row["activityId"] == "simplefin-row"
    )
    assert created["canonicalAccountId"] == "canonical-synthetic-account"
    assert created["lineage"]["status"] == "receipt-proven"
    assert detail["lineageEvidenceCounts"] == {
        "application-plans": 1,
        "receipts": 1,
        "plans-proved": 1,
        "plans-missing-receipt": 0,
        "plans-ambiguous-receipt": 0,
        "ignored-other-environment-application-plans": 1,
        "ignored-other-environment-receipts": 1,
    }
    assert summary["lineageEvidenceCounts"] == detail["lineageEvidenceCounts"]


def test_other_environment_plan_does_not_bind_nested_evidence(tmp_path, monkeypatch):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        include_foreign_environment=True,
        stale_foreign_binding=True,
    )

    forensic.build(root, repo_root=tmp_path / "repo")

    _publication, _pointer, detail, _summary = current_audit(root)
    assert detail["lineageEvidenceCounts"][
        "ignored-other-environment-application-plans"
    ] == 1
    created = next(
        row for row in detail["activities"] if row["activityId"] == "simplefin-row"
    )
    assert created["lineage"]["status"] == "receipt-proven"


def test_matching_environment_plan_requires_nested_evidence(tmp_path, monkeypatch):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        stale_matching_binding=True,
    )

    with pytest.raises(
        ForensicAuditError,
        match="SimpleFIN plan evidence hash is unproved",
    ):
        forensic.build(root, repo_root=tmp_path / "repo")


def test_application_plan_without_environment_fails_closed(tmp_path, monkeypatch):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        plan_environment=None,
    )

    with pytest.raises(
        ForensicAuditError,
        match="application plan lacks a valid environment fingerprint",
    ):
        forensic.build(root, repo_root=tmp_path / "repo")


def test_investment_activities_are_accounted_for_but_not_cash_matched(
    tmp_path, monkeypatch
):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        include_non_cash=True,
    )

    forensic.build(root, repo_root=tmp_path / "repo")

    _publication, _pointer, detail, summary = current_audit(root)
    investment_rows = [
        row
        for row in detail["activities"]
        if row["activityId"].startswith("investment-")
    ]
    assert len(investment_rows) == 2
    assert all(row["signedEffect"] is None for row in investment_rows)
    assert all(row["candidateGroupIds"] == [] for row in investment_rows)
    assert all(row["observationStatus"] == "out-of-scope" for row in investment_rows)
    assert {
        row["scopeReason"] for row in investment_rows
    } == {"non-cash-investment-activity"}
    planned_sell = next(
        row for row in investment_rows if row["activityId"] == "investment-sell"
    )
    assert planned_sell["lineage"]["status"] == "receipt-proven"
    assert planned_sell["lineage"]["reconciliationCreateBatchCardinality"] == 1
    assert summary["counts"]["baseline-activities"] == 7
    assert summary["counts"]["accounted-activities"] == 7
    assert summary["counts"]["out-of-scope-activities"] == 2
    assert summary["outOfScopeReasonCounts"] == {
        "non-cash-investment-activity": 2
    }
    assert detail["analysisScope"]["cashDuplicateMatching"] == {
        "status": "available",
        "includedActivityCount": 5,
        "outOfScopeActivityCount": 2,
        "outOfScopeReasonCounts": {"non-cash-investment-activity": 2},
    }


def test_unavailable_dependent_state_fails_closed(tmp_path, monkeypatch):
    root = private_fixture(
        tmp_path,
        monkeypatch,
        unavailable_domain="assignments",
    )

    with pytest.raises(
        ForensicAuditError,
        match="dependent-state baseline domain unavailable: assignments",
    ):
        forensic.build(root, repo_root=tmp_path / "repo")


@pytest.mark.parametrize("failure", ["changed", "missing"])
def test_changed_or_missing_evidence_fails_closed(tmp_path, failure):
    root = tmp_path / "private"
    path = root / "raw" / "simplefin" / "snapshot.json"
    write_json(path, {"accounts": [], "errors": []})
    entries = [evidence_entry(root, path, "raw-snapshot")]
    if failure == "changed":
        path.write_text('{"tampered":true}\n', encoding="utf-8")
    else:
        path.unlink()

    with pytest.raises(ForensicAuditError, match="evidence hash changed"):
        forensic._read_evidence(root, entries, BASELINE_ENVIRONMENT)


def test_output_tampering_is_detected(tmp_path, monkeypatch):
    root = private_fixture(tmp_path, monkeypatch)
    forensic.build(root, repo_root=tmp_path / "repo")
    publication, _pointer = forensic._current(root)
    (publication / "summary.json").write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(ForensicAuditError, match="output hash mismatch"):
        forensic.verify(root, repo_root=tmp_path / "repo")


def test_semantic_tampering_with_rehashed_publication_is_detected(
    tmp_path, monkeypatch
):
    root = private_fixture(tmp_path, monkeypatch)
    forensic.build(root, repo_root=tmp_path / "repo")
    publication, _pointer = forensic._current(root)
    detail = json.loads(
        (publication / "private-audit.json").read_text(encoding="utf-8")
    )
    candidate = next(
        row for row in detail["activities"] if row["observationStatus"] == "candidate"
    )
    candidate["observationStatus"] = "no-candidate"
    candidate["candidateGroupIds"] = []
    private_content = forensic._json_bytes(detail)
    manifest = json.loads(
        (publication / "manifest.json").read_text(encoding="utf-8")
    )
    manifest["files"]["private-audit.json"] = {
        "sha256": hashlib.sha256(private_content).hexdigest(),
        "size": len(private_content),
    }
    manifest_content = forensic._json_bytes(manifest)
    forged_id = hashlib.sha256(manifest_content).hexdigest()
    forged = publication.parent / forged_id
    shutil.copytree(publication, forged)
    (forged / "private-audit.json").write_bytes(private_content)
    (forged / "manifest.json").write_bytes(manifest_content)
    write_json(
        root / "audit" / "duplicates" / "current.json",
        {
            "schemaVersion": 1,
            "publicationId": forged_id,
            "manifestSha256": forged_id,
        },
    )

    with pytest.raises(ForensicAuditError, match="semantic analysis differs"):
        forensic.verify(root, repo_root=tmp_path / "repo")


def test_cli_and_module_have_no_wealthfolio_mutation_transport():
    module_source = inspect.getsource(forensic)
    from importers.audit import forensic_cli

    cli_source = inspect.getsource(forensic_cli)
    signature = inspect.signature(forensic.build)

    assert "client" not in signature.parameters
    assert "WealthfolioClient" not in module_source
    assert "WealthfolioClient" not in cli_source
    assert 'choices=("build", "verify")' in cli_source
    assert "apply" not in {choice for choice in ("build", "verify")}
    output = forensic_cli._summary(
        {
            "publication": {"publicationId": "a" * 64},
            "counts": {
                "accounted-activities": 2,
                "candidate-groups": 1,
                "review-required-groups": 1,
            },
            "summaryPath": r"C:\PRIVATE\summary.md",
        }
    )
    assert "PRIVATE" not in output


def test_forensic_wraps_baseline_inventory_error_without_private_path(
    monkeypatch, tmp_path
):
    private_marker = "\\".join(
        (
            "C:",
            "Users",
            "PRIVATE_PROFILE",
            "documents",
            "finance-data",
            "raw",
            "snapshot.json",
        )
    )
    safe_error = forensic.baseline.BaselineError(
        "cannot inspect evidence entry: raw/snapshot.json"
    )
    publication = tmp_path / "publication"
    pointer = {"publicationId": "a" * 64}
    monkeypatch.setattr(
        forensic.baseline,
        "_current",
        lambda _root: (publication, pointer),
    )
    monkeypatch.setattr(
        forensic.baseline,
        "verify",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(safe_error),
    )

    with pytest.raises(ForensicAuditError) as raised:
        forensic._baseline_state(tmp_path, tmp_path / "repo")

    assert private_marker not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    assert str(raised.value) == "cannot inspect evidence entry: raw/snapshot.json"
