from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from finance_store import identity_shadow
from finance_store.identity_shadow import IdentityShadowError


def _activity(
    reference: str,
    *,
    family: str,
    source_identity: str,
    description: str,
    hour: int,
) -> dict:
    return {
        "activityRef": reference,
        "activityId": f"SYN-ACTIVITY-{reference}",
        "canonicalAccountId": "SYN-ACCOUNT",
        "sourceAtUtc": f"2026-01-15T{hour:02d}:00:00Z",
        "sourceDateUtc": "2026-01-15",
        "signedEffect": "-12.34",
        "currency": "USD",
        "scopeReason": None,
        "sourceFamily": family,
        "sourceIdentity": source_identity,
        "description": description,
        "normalizedDescription": description.casefold(),
        "transferGroup": "",
        "observationStatus": "candidate",
        "candidateGroupIds": ["SYN-LEGACY-GROUP"],
        "dependentState": {},
        "lineage": {"status": "synthetic"},
        "raw": {
            "id": f"SYN-RAW-{reference}",
            "connectionId": f"SYN-CONNECTION-{family}",
            "pending": False,
            "category": "Synthetic Category",
        },
    }


def _private_forensic_publication(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "private"
    publication = root / "audit" / "duplicates" / "publications" / ("a" * 64)
    publication.mkdir(parents=True)
    detail = {
        "activities": [
            _activity(
                "b" * 64,
                family="monarch",
                source_identity="monarch:SYN-ONE",
                description="Synthetic Merchant",
                hour=0,
            ),
            _activity(
                "c" * 64,
                family="simplefin",
                source_identity="simplefin:SYN-ACCOUNT:SYN-TWO",
                description="Synthetic Merchant Detail",
                hour=12,
            ),
        ]
    }
    detail_content = json.dumps(detail).encode()
    (publication / "private-audit.json").write_bytes(detail_content)
    (publication / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": identity_shadow.forensic.SCHEMA_VERSION,
                "private": True,
                "readOnly": True,
                "counts": {"accounted-activities": 2},
                "files": {
                    "private-audit.json": {
                        "sha256": hashlib.sha256(detail_content).hexdigest(),
                        "size": len(detail_content),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        identity_shadow.forensic,
        "verify",
        lambda *_args, **_kwargs: {"verified": True},
    )
    monkeypatch.setattr(
        identity_shadow.forensic,
        "_current",
        lambda *_args, **_kwargs: (
            publication,
            {"publicationId": "a" * 64},
        ),
    )
    return root


def test_private_shadow_report_is_content_addressed_and_reproducible(
    tmp_path, monkeypatch
):
    root = _private_forensic_publication(tmp_path, monkeypatch)

    first, first_path = identity_shadow.publish_report(root)
    verified = identity_shadow.verify_report(root)
    second, second_path = identity_shadow.publish_report(root)

    assert first == second
    assert first_path == second_path
    assert verified["verified"] is True
    assert verified["counts"]["safeAutomaticResolutions"] == 0
    assert verified["counts"]["unresolvedDuplicateGroups"] == 1
    assert verified["counts"]["canonicalEventsBefore"] == 2
    assert verified["counts"]["canonicalEventsAfter"] == 2
    assert first["automaticDecisions"] == []


def test_private_shadow_verification_rejects_tampering(tmp_path, monkeypatch):
    root = _private_forensic_publication(tmp_path, monkeypatch)
    _report, publication = identity_shadow.publish_report(root)
    report_path = publication / "report.json"
    report_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(IdentityShadowError, match="failed integrity checks"):
        identity_shadow.verify_report(root)


def test_private_shadow_can_use_an_intact_sealed_forensic_publication(
    tmp_path, monkeypatch
):
    root = _private_forensic_publication(tmp_path, monkeypatch)
    monkeypatch.setattr(
        identity_shadow.forensic,
        "verify",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            identity_shadow.forensic.ForensicAuditError(
                "baseline evidence manifest changed or is incomplete"
            )
        ),
    )

    report, _resolution = identity_shadow.build_report_document(root)

    assert report["evidence"]["verificationMode"] == ("sealed-publication-integrity")
    assert report["evidence"]["identitySource"] == (
        identity_shadow.FORENSIC_DIAGNOSTIC_SOURCE
    )
    assert report["evidence"]["blockers"] == [
        identity_shadow.PREPUBLICATION_BLOCKER,
        "canonical-publication-missing",
        "current-evidence-baseline-refresh-required",
    ]


def test_private_shadow_stdout_contains_only_aggregate_hashes(
    tmp_path, monkeypatch, capsys
):
    root = _private_forensic_publication(tmp_path, monkeypatch)

    assert identity_shadow.main(["--data-dir", str(root), "run"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["verified"] is True
    assert "automaticDecisions" not in output
    assert str(root) not in json.dumps(output)
    assert "Synthetic Merchant" not in json.dumps(output)


def test_unscoped_shadow_provider_identity_is_not_exact():
    activity = _activity(
        "d" * 64,
        family="monarch",
        source_identity="monarch:SYN-REUSED",
        description="Synthetic Merchant",
        hour=0,
    )
    activity["raw"].pop("connectionId")

    provider_id, provider_kind = identity_shadow._provider_identity(
        activity,
        has_connection_scope=False,
    )

    assert provider_id == "monarch:SYN-REUSED"
    assert provider_kind == "none"
