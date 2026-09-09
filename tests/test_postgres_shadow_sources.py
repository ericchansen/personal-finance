import csv
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest

import finance_store.shadow as shadow_store
import finance_store.sources as sources
from finance_store.memory import MemoryRepository
from finance_store.reconcile import IngestionService
from finance_store.shadow import (
    MUTATION_INTERLOCK_VALUE,
    ShadowSafetyError,
    apply_plan,
    create_drift_report,
    create_plan,
    status_document,
)
from finance_store.sources import (
    SourceLoadError,
    _baseline_capability_blockers,
    _canonical_identity_supersedes_review_queue,
    _lineage_readiness,
    load_source_catalog,
)
from importers.audit import baseline, forensic
from importers.lineage_review import workflow as lineage_workflow
from importers.normalized import builder as normalized


NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def test_known_empty_baseline_api_gaps_do_not_block_authority():
    manifest = {
        "capabilityResults": {
            "accounts": {"status": "available"},
            "budget-period-inventory": {
                "status": "unavailable",
                "reasonType": "known-api-gap",
            },
            "exchange-rate-history": {
                "status": "unavailable",
                "reasonType": "known-api-gap",
            },
        },
        "recordCounts": {
            "accounts": 1,
            "budget-period-inventory": 0,
            "exchange-rate-history": 0,
        },
    }

    assert _baseline_capability_blockers(manifest) == []
    manifest["recordCounts"]["exchange-rate-history"] = 1
    assert _baseline_capability_blockers(manifest) == [
        "baseline-capability-unavailable:exchange-rate-history"
    ]


def test_only_zero_residual_canonical_identity_supersedes_review_queue():
    binding = {
        "identityPolicy": {
            "policyVersion": "canonical-identity-v2",
            "unresolvedDuplicateGroups": 0,
            "authorityAmbiguousGroups": 0,
            "residualByClass": {"unresolved": 0},
        }
    }

    assert _canonical_identity_supersedes_review_queue(binding)
    binding["identityPolicy"]["unresolvedDuplicateGroups"] = 1
    assert not _canonical_identity_supersedes_review_queue(binding)


def test_artifact_connection_identity_changes_with_parser_contract(tmp_path):
    first_parser = tmp_path / "parser-one.py"
    second_parser = tmp_path / "parser-two.py"
    first_parser.write_text("VERSION = 1\n", encoding="utf-8")
    second_parser.write_text("VERSION = 2\n", encoding="utf-8")
    source = write_json(tmp_path / "private" / "source.json", {"value": 1})

    first, _ = sources._artifact_batch(
        source.parent,
        source,
        source_kind="synthetic-artifact",
        source_version="v1",
        parser_paths=(first_parser,),
        records=(),
        observed_at=NOW,
    )
    replay, _ = sources._artifact_batch(
        source.parent,
        source,
        source_kind="synthetic-artifact",
        source_version="v1",
        parser_paths=(first_parser,),
        records=(),
        observed_at=NOW,
    )
    upgraded, _ = sources._artifact_batch(
        source.parent,
        source,
        source_kind="synthetic-artifact",
        source_version="v1",
        parser_paths=(second_parser,),
        records=(),
        observed_at=NOW,
    )

    assert first.connection == replay.connection
    assert first.connection.id != upgraded.connection.id
    assert first.connection.connection_key != upgraded.connection.connection_key


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def write_csv(path: Path, columns, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def fixture(tmp_path: Path, *, include_forensic_group: bool = True) -> Path:
    root = tmp_path / "private"
    root.mkdir()
    write_json(
        root / "facts" / "accounts.json",
        {
            "type": "account",
            "id": "acct-synthetic",
            "institution": "Example Bank",
            "displayName": "Synthetic Checking",
            "maskedNumber": None,
            "kind": "CASH",
            "opened": "2020-01-01",
            "closed": None,
            "excluded": False,
            "reason": None,
            "source": "synthetic fixture",
            "sourcePath": None,
            "notes": "synthetic",
        },
    )
    write_json(
        root / "simplefin" / "account-map.json",
        {
            "version": 1,
            "accounts": {
                "source-synthetic": {
                    "action": "import",
                    "assertionAccountId": "acct-synthetic",
                }
            },
        },
    )
    snapshot_path = write_json(
        root
        / "raw"
        / "simplefin"
        / "2026-09-01"
        / "simplefin-120000-000001.json",
        {
            "accounts": [
                {
                    "id": "source-synthetic",
                    "name": "Synthetic Checking",
                    "org": {
                        "name": "Example Bank",
                        "domain": "example.invalid",
                    },
                    "currency": "USD",
                    "balance": "100.00",
                    "balance-date": 1788220800,
                    "transactions": [
                        {
                            "id": "txn-synthetic",
                            "posted": 1788220800,
                            "amount": "-4.25",
                            "description": "Synthetic Merchant",
                            "pending": False,
                        }
                    ],
                }
            ],
            "errors": [],
        },
    )
    write_json(
        snapshot_path.with_name("request-120000-000001.json"),
        {
            "schemaVersion": 1,
            "protocolVersion": 1,
            "snapshotSha256": hashlib.sha256(
                snapshot_path.read_bytes()
            ).hexdigest(),
            "requestedStart": "2026-08-01",
            "requestedEnd": "2026-09-01",
            "pendingIncluded": True,
        },
    )
    extract = root / "extracts" / "example" / "activity.qfx"
    extract.parent.mkdir(parents=True)
    extract.write_text(
        "OFXHEADER:100\n<OFX><BANKACCTFROM><ACCTID>SYNTHETIC</BANKACCTFROM>"
        "<STMTTRN><DTPOSTED>20260901<TRNAMT>-4.25<FITID>qfx-synthetic"
        "<NAME>Synthetic Merchant</STMTTRN></OFX>",
        encoding="utf-8",
    )
    write_json(
        root / "extracts" / "mapping.json",
        {
            "files": [
                {
                    "file": "example/activity.qfx",
                    "account": "Synthetic Checking",
                }
            ]
        },
    )
    write_json(
        root / "normalized" / "monarch-account-map.json",
        {"Synthetic Checking": "acct-synthetic"},
    )
    monarch = root / "legacy" / "monarch"
    monarch.mkdir(parents=True)
    (monarch / "Transactions_2026.csv").write_text(
        "Date,Merchant,Category,Account,Original Statement,Notes,Amount,"
        "Tags,Owner,Reviewed,Id\n"
        "2026-09-01,Synthetic Merchant,Shopping,Synthetic Checking,,,"
        "-4.25,,,,monarch-synthetic\n",
        encoding="utf-8",
    )
    (monarch / "Balances_2026.csv").write_text(
        "Date,Account,Balance\n"
        "2026-09-01,Synthetic Checking,100.00\n",
        encoding="utf-8",
    )

    canonical = root / "normalized" / "canonical"
    account_row = {column: "" for column in normalized.ACCOUNT_COLUMNS}
    account_row.update(
        {
            "account_id": "acct-synthetic",
            "institution": "Example Bank",
            "name": "Synthetic Checking",
            "kind": "CASH",
            "currency": "USD",
            "opened": "2020-01-01",
            "excluded": "false",
            "tracking_mode": "TRANSACTIONS",
        }
    )
    transaction_row = {column: "" for column in normalized.TRANSACTION_COLUMNS}
    transaction_row.update(
        {
            "date": "2026-09-01",
            "account_id": "acct-synthetic",
            "amount": "-4.25",
            "description": "Synthetic Merchant",
            "source_id": "simplefin:source-synthetic:txn-synthetic",
            "source_file": "synthetic",
            "external_flow": "false",
            "excluded": "false",
            "transaction_kind": "expense",
        }
    )
    paths = {
        "accounts.csv": write_csv(
            canonical / "accounts.csv", normalized.ACCOUNT_COLUMNS, [account_row]
        ),
        "transactions.csv": write_csv(
            canonical / "transactions.csv",
            normalized.TRANSACTION_COLUMNS,
            [transaction_row],
        ),
        "positions.csv": write_csv(
            canonical / "positions.csv", normalized.POSITION_COLUMNS, []
        ),
        "valuations.csv": write_csv(
            canonical / "valuations.csv", normalized.VALUATION_COLUMNS, []
        ),
    }
    write_json(
        canonical / "manifest.json",
        {
            "schemaVersion": 4,
            "buildTimestamp": NOW.isoformat(),
            "sourceFiles": [],
            "dataFiles": {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in paths.items()
            },
            "rowCounts": {
                "accounts": 1,
                "transactions": 1,
                "positions": 0,
                "valuations": 0,
            },
            "warnings": [],
        },
    )

    baseline_id = "a" * 64
    baseline_publication = (
        root / "audit" / "baselines" / "publications" / baseline_id
    )
    baseline_domain = write_json(
        baseline_publication / "domains" / "activities.json",
        {
            "schemaVersion": 1,
            "domain": "activities",
            "status": "available",
            "recordCount": 0,
            "records": [],
        },
    )
    baseline_manifest = write_json(
        baseline_publication / "manifest.json",
        {
            "schemaVersion": 1,
            "generatedAt": NOW.isoformat(),
            "capabilityResults": {
                "activities": {"status": "available"}
            },
            "domainFiles": {
                "activities.json": {
                    "sha256": hashlib.sha256(
                        baseline_domain.read_bytes()
                    ).hexdigest()
                }
            },
        },
    )
    baseline_publication_id = hashlib.sha256(
        baseline_manifest.read_bytes()
    ).hexdigest()
    published_baseline = (
        baseline_publication.parent / baseline_publication_id
    )
    baseline_publication.rename(published_baseline)
    write_json(
        root / "audit" / "baselines" / "current.json",
        {
            "schemaVersion": 1,
            "publicationId": baseline_publication_id,
            "manifestSha256": baseline_publication_id,
        },
    )

    forensic_id = "b" * 64
    forensic_publication = (
        root / "audit" / "duplicates" / "publications" / forensic_id
    )
    group = {
        "groupId": "group-synthetic",
        "classification": "review-required",
        "ambiguityCardinality": 2,
        "activityRefs": ["activity-a", "activity-b"],
    }
    forensic_detail = write_json(
        forensic_publication / "private-audit.json",
        {"candidateGroups": [group] if include_forensic_group else []},
    )
    forensic_review = write_json(
        forensic_publication / "review-decisions.json",
        {
            "schemaVersion": 1,
            "auditGraphSha256": "c" * 64,
            "decisions": (
                [
                    {
                        "candidateGroupId": "group-synthetic",
                        "candidateHash": "d" * 64,
                        "status": "pending",
                        "evidenceHashes": ["e" * 64],
                    }
                ]
                if include_forensic_group
                else []
            ),
        },
    )
    forensic_manifest = write_json(
        forensic_publication / "manifest.json",
        {
            "schemaVersion": 1,
            "generatedAt": NOW.isoformat(),
            "files": {
                "private-audit.json": {
                    "sha256": hashlib.sha256(
                        forensic_detail.read_bytes()
                    ).hexdigest(),
                    "size": forensic_detail.stat().st_size,
                },
                "review-decisions.json": {
                    "sha256": hashlib.sha256(
                        forensic_review.read_bytes()
                    ).hexdigest(),
                    "size": forensic_review.stat().st_size,
                },
            },
        },
    )
    forensic_publication_id = hashlib.sha256(
        forensic_manifest.read_bytes()
    ).hexdigest()
    published_forensic = (
        forensic_publication.parent / forensic_publication_id
    )
    forensic_publication.rename(published_forensic)
    write_json(
        root / "audit" / "duplicates" / "current.json",
        {
            "schemaVersion": 1,
            "publicationId": forensic_publication_id,
            "manifestSha256": forensic_publication_id,
        },
    )
    return root


def configure_verified_v5_lineage(
    root: Path,
    monkeypatch,
    *,
    queue_groups: int,
    reviewed_decisions: int,
) -> None:
    queue_id = "1" * 64
    decision_id = "2" * 64 if reviewed_decisions else None
    queue_output = root / "audit" / "lineage-review"
    queue_output.mkdir(parents=True)
    write_json(queue_output / "current.json", {"publicationId": queue_id})
    canonical = root / "normalized" / "canonical"
    observation_path = write_json(
        canonical / "transaction-observations.json",
        {
            "schemaVersion": 1,
            "kind": "canonical-transaction-observations",
            "private": True,
            "observationCount": 0,
            "observations": [],
        },
    )
    lineage_path = write_json(
        canonical / "transaction-lineage.json",
        {
            "schemaVersion": 1,
            "kind": "canonical-transaction-lineage",
            "private": True,
            "canonicalTransactions": [],
            "decisionProjections": [],
        },
    )
    manifest_path = canonical / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schemaVersion"] = normalized.SCHEMA_VERSION
    manifest["lineageReview"] = {
        "queuePublicationId": queue_id,
        "decisionPublicationId": decision_id,
    }
    manifest["dataFiles"].update(
        {
            observation_path.name: hashlib.sha256(
                observation_path.read_bytes()
            ).hexdigest(),
            lineage_path.name: hashlib.sha256(lineage_path.read_bytes()).hexdigest(),
        }
    )
    write_json(manifest_path, manifest)
    monkeypatch.setattr(
        normalized,
        "verify",
        lambda _root: {
            "verified": True,
            "schemaVersion": normalized.SCHEMA_VERSION,
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        lineage_workflow,
        "verify",
        lambda *_args, **_kwargs: {
            "queuePublicationId": queue_id,
            "decisionPublicationId": decision_id,
            "counts": {
                "queue-groups": queue_groups,
                "queue-batches": 1 if queue_groups else 0,
                "reviewed-decisions": reviewed_decisions,
                "resolved-groups": reviewed_decisions,
                "unresolved-groups": queue_groups - reviewed_decisions,
            },
            "readinessCounts": {
                "ready-groups": reviewed_decisions,
                "restore-eligible-groups": 0,
                "surgical-eligible-groups": 0,
                "rebuild-eligible-groups": reviewed_decisions,
            },
            "evidenceGapCounts": {},
        },
    )


def database_state_manifest(dsn: str) -> str:
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT finance.backup_state_manifest()::text"
        ).fetchone()[0]


def test_catalog_reads_every_selected_source_without_mutating_them(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    before = {
        path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {"verified": True, "warnings": []},
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})

    catalog = load_source_catalog(root, generated_at=NOW)

    after = {
        path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert {
        "simplefin-snapshot",
        "simplefin-request-metadata",
        "normalized-ofx",
        "monarch-legacy-transactions",
        "monarch-legacy-balances",
        "fact",
        "canonical-transactions",
        "wealthfolio-baseline-manifest",
        "forensic-publication",
    } <= set(catalog.source_counts)
    assert len(catalog.lineage_groups) == 1
    assert catalog.lineage_decisions == ()
    assert "lineage-review-decision-missing" in catalog.gaps

    repository = MemoryRepository()
    service = IngestionService(repository, projection_target=None)
    for batch in catalog.batches:
        service.ingest(batch)
    state = repository.state()
    assert state.artifact_observations
    assert len(state.transaction_observations) == 1
    assert state.projection_records == ()
    assert any(
        issue.details.get("policy")
        == "retain_all_until_evidence_bound_decision"
        for issue in state.issues
    )


def test_catalog_ingests_canonical_lineage_documents(tmp_path, monkeypatch):
    root = fixture(tmp_path)
    canonical = root / "normalized" / "canonical"
    observations = write_json(
        canonical / "transaction-observations.json",
        {
            "schemaVersion": 1,
            "kind": "canonical-transaction-observations",
            "private": True,
            "observationCount": 1,
            "observations": [
                {
                    "observationId": "synthetic-observation",
                    "transaction": {"description": "Synthetic Merchant"},
                }
            ],
        },
    )
    lineage = write_json(
        canonical / "transaction-lineage.json",
        {
            "schemaVersion": 1,
            "kind": "canonical-transaction-lineage",
            "private": True,
            "canonicalTransactions": [
                {"canonicalTransactionId": "synthetic-transaction"}
            ],
            "decisionProjections": [],
        },
    )
    manifest_path = canonical / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schemaVersion"] = normalized.SCHEMA_VERSION
    manifest["dataFiles"].update(
        {
            observations.name: hashlib.sha256(observations.read_bytes()).hexdigest(),
            lineage.name: hashlib.sha256(lineage.read_bytes()).hexdigest(),
        }
    )
    write_json(manifest_path, manifest)
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {
            "verified": True,
            "schemaVersion": normalized.SCHEMA_VERSION,
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        normalized,
        "verify",
        lambda _root: {
            "verified": True,
            "schemaVersion": normalized.SCHEMA_VERSION,
            "warnings": [],
        },
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})

    catalog = load_source_catalog(root, generated_at=NOW)

    assert {
        "canonical-transaction-observations",
        "canonical-transaction-lineage",
    } <= set(catalog.source_counts)
    canonical_artifacts = {
        artifact.observation_kind: artifact.payload
        for batch in catalog.batches
        for artifact in batch.artifacts
        if artifact.observation_kind.startswith("canonical-transaction-")
    }
    assert canonical_artifacts["canonical-transaction-observations"][
        "observationCount"
    ] == 1
    assert canonical_artifacts["canonical-transaction-lineage"][
        "canonicalTransactions"
    ] == [{"canonicalTransactionId": "synthetic-transaction"}]


def test_verified_incomplete_lineage_readiness_blocks_shadow_apply(
    tmp_path, monkeypatch
):
    output = tmp_path / "audit" / "lineage-review"
    output.mkdir(parents=True)
    (output / "current.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        lineage_workflow,
        "verify",
        lambda *_args, **_kwargs: {
            "queuePublicationId": "a" * 64,
            "decisionPublicationId": "b" * 64,
            "counts": {
                "queue-groups": 3,
                "queue-batches": 2,
                "reviewed-decisions": 1,
                "resolved-groups": 1,
                "unresolved-groups": 2,
            },
            "readinessCounts": {
                "ready-groups": 1,
                "restore-eligible-groups": 0,
                "surgical-eligible-groups": 1,
                "rebuild-eligible-groups": 1,
            },
            "evidenceGapCounts": {"missing-decision": 2},
        },
    )

    counts, readiness, gaps, blockers, status_gaps = _lineage_readiness(
        tmp_path,
        {
            "queuePublicationId": "a" * 64,
            "decisionPublicationId": "b" * 64,
        },
    )

    assert counts["reviewed-decisions"] == 1
    assert readiness["ready-groups"] == 1
    assert gaps == {"missing-decision": 2}
    assert blockers == [
        "lineage-remediation-readiness-incomplete",
        "lineage-v5-persistence-not-implemented",
    ]
    assert status_gaps == ["lineage-readiness-missing-decision-count:2"]


def test_missing_lineage_publication_blocks_shadow_apply(tmp_path):
    counts, readiness, gaps, blockers, status_gaps = _lineage_readiness(
        tmp_path, None
    )

    assert counts == readiness == gaps == {}
    assert blockers == ["lineage-review-publication-missing"]
    assert status_gaps == []


def test_complete_v5_review_remains_blocked_until_shadow_persistence_exists(
    tmp_path, monkeypatch
):
    output = tmp_path / "audit" / "lineage-review"
    output.mkdir(parents=True)
    (output / "current.json").write_text("{}\n", encoding="utf-8")
    queue_id = "a" * 64
    decision_id = "b" * 64
    monkeypatch.setattr(
        lineage_workflow,
        "verify",
        lambda *_args, **_kwargs: {
            "queuePublicationId": queue_id,
            "decisionPublicationId": decision_id,
            "counts": {
                "queue-groups": 1,
                "queue-batches": 1,
                "reviewed-decisions": 1,
                "resolved-groups": 1,
                "unresolved-groups": 0,
            },
            "readinessCounts": {
                "ready-groups": 1,
                "restore-eligible-groups": 1,
                "surgical-eligible-groups": 1,
                "rebuild-eligible-groups": 1,
            },
            "evidenceGapCounts": {},
        },
    )

    _counts, _readiness, _gaps, blockers, _status_gaps = _lineage_readiness(
        tmp_path,
        {
            "queuePublicationId": queue_id,
            "decisionPublicationId": decision_id,
        },
    )

    assert blockers == ["lineage-v5-persistence-not-implemented"]


def test_evidence_bound_lineage_decision_is_versioned_and_ingested(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    write_json(
        root / "decisions" / "lineage-review.json",
        {
            "schemaVersion": 1,
            "auditGraphSha256": "c" * 64,
            "decisions": [
                {
                    "candidateGroupId": "group-synthetic",
                    "candidateHash": "d" * 64,
                    "decisionVersion": 1,
                    "outcome": "distinct-economic-events",
                    "rationale": "Synthetic records are independently evidenced.",
                    "evidenceHashes": ["e" * 64],
                    "decidedAt": NOW.isoformat(),
                }
            ],
        },
    )
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {"verified": True, "warnings": []},
    )
    monkeypatch.setattr(
        normalized,
        "verify",
        lambda _root: {
            "verified": True,
            "schemaVersion": normalized.SCHEMA_VERSION,
            "warnings": [],
        },
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})

    catalog = load_source_catalog(root, generated_at=NOW)

    assert len(catalog.lineage_decisions) == 1
    assert catalog.lineage_decisions[0].decision_version == 1
    assert "lineage-review-decision-missing" not in catalog.gaps
    assert catalog.source_counts["lineage-review-decisions"] == 1
    assert any(
        decision.subject_type == "lineage_group"
        for batch in catalog.batches
        for decision in batch.source_decisions
    )


def test_conflicting_lineage_decision_version_fails_closed(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    common = {
        "candidateGroupId": "group-synthetic",
        "candidateHash": "d" * 64,
        "decisionVersion": 1,
        "rationale": "Synthetic reviewed evidence.",
        "evidenceHashes": ["e" * 64],
        "decidedAt": NOW.isoformat(),
    }
    for name, outcome in (("first", "distinct-economic-events"), ("second", "transfer")):
        write_json(
            root / "decisions" / f"{name}.json",
            {
                "schemaVersion": 1,
                "auditGraphSha256": "c" * 64,
                "decisions": [{**common, "outcome": outcome}],
            },
        )
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {"verified": True, "warnings": []},
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})

    with pytest.raises(SourceLoadError, match="version-conflict"):
        load_source_catalog(root, generated_at=NOW)


def test_official_v2_catalog_uses_connection_scope_and_admission_hash(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    snapshot = next((root / "raw" / "simplefin").glob("**/simplefin-*.json"))
    write_json(
        snapshot,
        {
            "errlist": [],
            "connections": [{"conn_id": "connection-synthetic"}],
            "accounts": [
                {
                    "id": "source-synthetic",
                    "conn_id": "connection-synthetic",
                    "name": "Synthetic Checking",
                    "currency": "USD",
                    "balance": "100.00",
                    "balance-date": 1788220800,
                    "transactions": [
                        {
                            "id": "txn-synthetic",
                            "posted": 0,
                            "transacted_at": 1788220800,
                            "amount": "-4.25",
                            "description": "Synthetic Pending",
                            "pending": True,
                        }
                    ],
                }
            ],
        },
    )
    request = snapshot.with_name("request-120000-000001.json")
    metadata = json.loads(request.read_text(encoding="utf-8"))
    metadata["protocolVersion"] = 2
    metadata["snapshotSha256"] = hashlib.sha256(
        snapshot.read_bytes()
    ).hexdigest()
    write_json(request, metadata)
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {"verified": True, "warnings": []},
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})

    first = load_source_catalog(root, generated_at=NOW)
    first_batch = next(
        batch
        for batch in first.batches
        if batch.blob.source_kind == "simplefin-snapshot"
    )
    assert first_batch.run.source_version == "2"
    assert first_batch.run.admission_hash
    assert ":" in first_batch.accounts[0].external_id
    assert first_batch.transactions[0].status == "pending"

    mapping_path = root / "simplefin" / "account-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["accounts"]["source-synthetic"]["action"] = "exclude"
    write_json(mapping_path, mapping)
    second = load_source_catalog(root, generated_at=NOW)
    second_batch = next(
        batch
        for batch in second.batches
        if batch.blob.source_kind == "simplefin-snapshot"
    )
    assert second_batch.run.id != first_batch.run.id
    assert second_batch.run.admission_hash != first_batch.run.admission_hash


def test_identical_artifact_occurrences_keep_distinct_ids(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    transactions = root / "normalized" / "canonical" / "transactions.csv"
    with transactions.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    write_csv(
        transactions,
        normalized.TRANSACTION_COLUMNS,
        [rows[0], rows[0]],
    )
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {"verified": True, "warnings": []},
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})

    catalog = load_source_catalog(root, generated_at=NOW)
    batch = next(
        item
        for item in catalog.batches
        if item.blob.source_kind == "canonical-transactions"
    )
    assert len(batch.artifacts) == 2
    assert len({item.id for item in batch.artifacts}) == 2


def test_invalid_simplefin_mapping_action_is_a_blocker(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    mapping_path = root / "simplefin" / "account-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["accounts"]["source-synthetic"]["action"] = "surprise"
    write_json(mapping_path, mapping)
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {"verified": True, "warnings": []},
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})

    catalog = load_source_catalog(root, generated_at=NOW)

    assert "simplefin-mapping-action-invalid" in catalog.blockers
    batch = next(
        item
        for item in catalog.batches
        if item.blob.source_kind == "simplefin-snapshot"
    )
    assert batch.transactions == ()


@pytest.mark.skipif(
    not os.environ.get("FINANCE_POSTGRES_SHADOW_TEST_DSN"),
    reason="requires the disposable PostgreSQL integration profile",
)
def test_reviewed_v5_decisions_block_apply_before_backup_or_writes(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {"verified": True, "warnings": []},
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})
    configure_verified_v5_lineage(
        root,
        monkeypatch,
        queue_groups=1,
        reviewed_decisions=1,
    )
    dsn = os.environ["FINANCE_POSTGRES_SHADOW_TEST_DSN"]

    plan, plan_path = create_plan(
        root,
        dsn,
        environment="synthetic-shadow-integration",
        generated_at=NOW,
    )
    assert not plan["ready"]
    assert plan["blockers"] == ["lineage-v5-persistence-not-implemented"]
    assert plan["counts"]["lineageReviewedDecisions"] == 1
    monkeypatch.setenv(
        "FINANCE_SHADOW_ENVIRONMENT", "synthetic-shadow-integration"
    )
    monkeypatch.setenv(
        "FINANCE_SHADOW_MUTATIONS_ENABLED", MUTATION_INTERLOCK_VALUE
    )
    backup = Mock(side_effect=AssertionError("backup must not run"))
    monkeypatch.setattr(shadow_store, "verify_backup", backup)
    before = database_state_manifest(dsn)

    with pytest.raises(ShadowSafetyError, match="sealed plan has blockers"):
        apply_plan(
            root,
            dsn,
            plan_path=plan_path.relative_to(root),
            supplied_hash=plan["planHash"],
        )

    backup.assert_not_called()
    assert database_state_manifest(dsn) == before


@pytest.mark.skipif(
    not os.environ.get("FINANCE_POSTGRES_SHADOW_TEST_DSN"),
    reason="requires the disposable PostgreSQL integration profile",
)
def test_sealed_plan_apply_and_stable_drift(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path, include_forensic_group=False)
    monkeypatch.setattr(
        normalized,
        "verify_publication",
        lambda _root: {"verified": True, "warnings": []},
    )
    monkeypatch.setattr(baseline, "verify", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forensic, "verify", lambda *_args, **_kwargs: {})
    configure_verified_v5_lineage(
        root,
        monkeypatch,
        queue_groups=0,
        reviewed_decisions=0,
    )
    dsn = os.environ["FINANCE_POSTGRES_SHADOW_TEST_DSN"]

    plan, plan_path = create_plan(
        root,
        dsn,
        environment="synthetic-shadow-integration",
        generated_at=NOW,
    )
    assert plan["ready"]
    assert plan["blockers"] == []
    assert {
        key: plan["counts"][key]
        for key in (
            "lineageDecisions",
            "lineageGroups",
            "lineageQueueGroups",
            "lineageReadyGroups",
            "lineageReviewedDecisions",
        )
    } == {
        "lineageDecisions": 0,
        "lineageGroups": 0,
        "lineageQueueGroups": 0,
        "lineageReadyGroups": 0,
        "lineageReviewedDecisions": 0,
    }
    assert plan["counts"]["newObservations"] > 0
    monkeypatch.setenv(
        "FINANCE_SHADOW_ENVIRONMENT", "synthetic-shadow-integration"
    )
    monkeypatch.setenv(
        "FINANCE_SHADOW_MUTATIONS_ENABLED", MUTATION_INTERLOCK_VALUE
    )
    result = apply_plan(
        root,
        dsn,
        plan_path=plan_path.relative_to(root),
        supplied_hash=plan["planHash"],
    )
    assert result["stateHash"] == plan["expectedStateHash"]
    assert status_document(
        dsn, "synthetic-shadow-integration"
    )["observationCount"] > 0

    second_plan, second_path = create_plan(
        root,
        dsn,
        environment="synthetic-shadow-integration",
        generated_at=NOW,
    )
    with monkeypatch.context() as apply_patch:
        apply_patch.setattr(
            shadow_store,
            "verify_backup",
            lambda *_args, **_kwargs: {
                "dumpSha256": "f" * 64,
                "observationCount": second_plan["counts"]["observations"],
            },
        )
        replay = apply_plan(
            root,
            dsn,
            plan_path=second_path.relative_to(root),
            supplied_hash=second_plan["planHash"],
        )
    assert replay["stateHash"] == second_plan["expectedStateHash"]
    with psycopg.connect(dsn) as connection:
        lineage_counts = connection.execute(
            """
            SELECT
                (SELECT count(*)
                 FROM finance.lineage_review_group_members),
                (SELECT count(*)
                 FROM finance.lineage_review_decisions)
            """
        ).fetchone()
        current_run = connection.execute(
            """
            SELECT
                source_blob_id, importer_name, importer_version,
                run_status, effective_start, effective_end,
                observed_at, processed_at, records_seen,
                records_accepted, source_protocol, source_version,
                overlap_start, overlap_end, sealed_plan_hash,
                admission_hash
            FROM finance.ingestion_runs
            WHERE parser_hash IS NOT NULL
            ORDER BY processed_at DESC, ingestion_run_id DESC
            LIMIT 1
            """
        ).fetchone()
        connection.execute(
            """
            INSERT INTO finance.ingestion_runs (
                ingestion_run_id, source_blob_id, importer_name,
                importer_version, run_status, effective_start,
                effective_end, observed_at, processed_at, finished_at,
                records_seen, records_accepted, parser_hash,
                source_protocol, source_version, overlap_start,
                overlap_end, sealed_plan_hash, admission_hash
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s - interval '2 days',
                %s - interval '1 day',
                %s - interval '1 day',
                %s + 1, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                uuid.uuid4(),
                current_run[0],
                current_run[1],
                current_run[2],
                current_run[3],
                current_run[4],
                current_run[5],
                current_run[6],
                current_run[7],
                current_run[7],
                current_run[8],
                current_run[9],
                "f" * 64,
                current_run[10],
                current_run[11],
                current_run[12],
                current_run[13],
                current_run[14],
                current_run[15],
            ),
        )
        connection.commit()
    assert lineage_counts == (0, 0)

    first, _ = create_drift_report(
        root,
        dsn,
        environment="synthetic-shadow-integration",
        generated_at=NOW.replace(hour=13),
    )
    second, _ = create_drift_report(
        root,
        dsn,
        environment="synthetic-shadow-integration",
        generated_at=NOW.replace(hour=14),
    )
    assert first["driftHash"] == second["driftHash"]
    assert first["missingInputBlobs"] == 0
    assert first["extraDatabaseBlobs"] == 0

    next_plan, next_path = create_plan(
        root,
        dsn,
        environment="synthetic-shadow-integration",
        generated_at=NOW.replace(hour=15),
    )
    with pytest.raises(ShadowSafetyError, match="backup"):
        apply_plan(
            root,
            dsn,
            plan_path=next_path.relative_to(root),
            supplied_hash=next_plan["planHash"],
        )
