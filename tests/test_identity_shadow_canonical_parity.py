"""Canonical projection and the private shadow must resolve identity alike.

Two producers derive one canonical identity from the same household evidence:
``importers.lineage_review.canonical`` reads canonical transaction rows, and
``finance_store.identity_shadow`` reads sealed forensic activities for the
PostgreSQL shadow.  If only one of them read the durable private declarations --
coverage-authority intervals, duplicate-summary account decisions, scoped
provider-token lineage -- the shadow would keep reporting duplicate groups
canonical had already resolved and shadow evidence would never match canonical
evidence.

Every fixture in this module is invented.  No real institution, account,
balance, merchant, or transaction appears anywhere in this file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from finance_store import identity_shadow
from finance_store.identity import (
    DEFAULT_POLICY,
    apply_declarations,
    observations_from_transaction_rows,
    resolve_identity,
)
from finance_store.identity_shadow import IdentityShadowError
from importers.lineage_review import canonical

ACCOUNT = "SYN-ACCOUNT"
DETAIL = "SYN-SF-DETAIL"
SUMMARY = "SYN-SF-SUMMARY"
DECIDED_AT = "2026-02-20"
QFX_FILE = "extracts/synthetic/statement.qfx"

# One economic story, told twice: once as canonical rows and once as forensic
# activities.  Both producers must reach the same decisions about it.
#   * two SimpleFIN rows that a declared duplicate-summary mapping collapses
#   * a QFX row and a SimpleFIN row sharing a declared scoped provider token
#   * a QFX row and a legacy Monarch row two days apart, inside a declared
#     coverage-authority interval that ranks QFX above Monarch
SPEC = (
    ("SF-DETAIL-1", "simplefin", f"simplefin:{DETAIL}:SF-1", "2026-02-05", "-11.00",
     "Synthetic Grocer", ""),
    ("SF-SUMMARY-1", "simplefin", f"simplefin:{SUMMARY}:SF-2", "2026-02-05", "-11.00",
     "Synthetic Grocer", ""),
    ("QFX-TOKEN-1", "qfx", "extract:stable:TOKEN-9", "2026-02-10", "-22.00",
     "Synthetic Hardware", ""),
    ("SF-TOKEN-1", "simplefin", f"simplefin:{DETAIL}:TOKEN-9", "2026-02-10", "-22.00",
     "Synthetic Hardware", ""),
    ("QFX-AUTH-1", "qfx", "extract:stable:QFX-AUTH-1", "2026-01-15", "-42.00",
     "Synthetic Merchant", ""),
    ("MONARCH-AUTH-1", "monarch", "monarch:MON-AUTH-1", "2026-01-17", "-42.00",
     "Synthetic Merchant", "Household Supplies"),
)


# ---------------------------------------------------------------------------
# The same story, in each producer's own input shape
# ---------------------------------------------------------------------------


def canonical_rows() -> list[dict[str, object]]:
    rows = []
    for _ref, family, source_id, day, amount, description, category in SPEC:
        rows.append(
            {
                "account_id": ACCOUNT,
                "date": day,
                "amount": amount,
                "currency": "USD",
                "description": description,
                "category": category,
                "source_id": source_id,
                "source_file": QFX_FILE if family == "qfx" else "",
            }
        )
    return rows


def forensic_activities() -> list[dict[str, object]]:
    activities = []
    for ref, family, source_id, day, amount, description, category in SPEC:
        activities.append(
            {
                "activityRef": hashlib.sha256(ref.encode()).hexdigest(),
                "canonicalAccountId": ACCOUNT,
                # A bare day, matching the canonical row's own date precision:
                # a writer timestamp neither producer has must not appear in one.
                "sourceAtUtc": None,
                "sourceDateUtc": day,
                "signedEffect": amount,
                "currency": "USD",
                "scopeReason": None,
                "sourceFamily": family,
                "sourceIdentity": source_id,
                "description": description,
                "transferGroup": "",
                "dependentState": {},
                "lineage": {"status": "synthetic"},
                "raw": {"pending": False, "category": category},
            }
        )
    return activities


# ---------------------------------------------------------------------------
# The durable private declarations
# ---------------------------------------------------------------------------


def coverage_interval(
    *,
    family: str,
    strength: str,
    tolerance: int = 0,
) -> dict[str, object]:
    return {
        "canonical_account_id": ACCOUNT,
        "effective_from": "2026-01-01",
        "effective_through": "2026-01-31",
        "source_family": family,
        "source_connection_id": (
            "unscoped:monarch" if family == "monarch" else f"{family}-account-scoped"
        ),
        "source_account_id": ACCOUNT,
        "format_strength": strength,
        "stable_id_support": family != "monarch",
        "replay_stable_ids": True,
        "extraction_requested_from": "2026-01-01",
        "extraction_requested_through": "2026-01-31",
        "extracted_at": "2026-02-10T00:00:00+00:00",
        "freshness_as_of": "2026-02-10T00:00:00+00:00",
        "completeness": "complete",
        "source_transaction_count": 1,
        "source_hashes": [hashlib.sha256(f"SYN-{family}".encode()).hexdigest()],
        "trust_cutoff_day": None,
        "posting_date_tolerance_days": tolerance,
    }


def write_declarations(root: Path) -> None:
    (root / "identity").mkdir(parents=True, exist_ok=True)
    (root / "simplefin").mkdir(parents=True, exist_ok=True)
    (root / "identity" / "source-authority.json").write_bytes(
        json.dumps(
            {
                "coverageIntervals": [
                    coverage_interval(
                        family="qfx", strength="stable-provider-id", tolerance=2
                    ),
                    coverage_interval(family="monarch", strength="legacy-export"),
                ]
            }
        ).encode("utf-8")
    )
    (root / "identity" / "provider-token-scopes.json").write_bytes(
        json.dumps(
            {
                "version": 1,
                "providerTokenScopes": [
                    {
                        "canonicalAccountId": ACCOUNT,
                        "decision": "shared-provider-token-namespace",
                        "decidedAt": DECIDED_AT,
                        "maxDaySkew": 1,
                        "namespaces": [
                            {
                                "sourceFamily": "qfx",
                                "sourceAccountId": ACCOUNT,
                                "providerIdKind": "ofx-fitid",
                                "tokenPrefix": "extract:stable:",
                            },
                            {
                                "sourceFamily": "simplefin",
                                "sourceAccountId": DETAIL,
                                "providerIdKind": "simplefin-id",
                                "tokenPrefix": f"simplefin:{DETAIL}:",
                            },
                        ],
                    }
                ],
            }
        ).encode("utf-8")
    )
    (root / "simplefin" / "account-map.json").write_bytes(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    DETAIL: {"action": "import", "wealthfolioAccountId": "SYN-WF"},
                    SUMMARY: {
                        "action": "exclude",
                        "decision": "aggregator-account-summary",
                        "duplicateOfSourceAccountId": DETAIL,
                    },
                },
            }
        ).encode("utf-8")
    )


def private_root(tmp_path: Path, monkeypatch, *, declared: bool = True) -> Path:
    root = tmp_path / "private"
    publication = root / "audit" / "duplicates" / "publications" / ("a" * 64)
    publication.mkdir(parents=True)
    detail = json.dumps({"activities": forensic_activities()}).encode()
    (publication / "private-audit.json").write_bytes(detail)
    (publication / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": identity_shadow.forensic.SCHEMA_VERSION,
                "private": True,
                "readOnly": True,
                "counts": {"accounted-activities": len(SPEC)},
                "files": {
                    "private-audit.json": {
                        "sha256": hashlib.sha256(detail).hexdigest(),
                        "size": len(detail),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        identity_shadow.forensic, "verify", lambda *_a, **_k: {"verified": True}
    )
    monkeypatch.setattr(
        identity_shadow.forensic,
        "_current",
        lambda *_a, **_k: (publication, {"publicationId": "a" * 64}),
    )
    if declared:
        write_declarations(root)
    return root


def canonical_identity(root: Path) -> dict[str, object]:
    declared = canonical.declared_identity_inputs(root)
    projection = canonical._automatic_identity_projection(
        canonical_rows(),
        set(),
        declared.duplicate_summaries,
        declared.token_scopes,
        declared.policy,
    )
    return projection["identity"]


def canonical_resolution(root: Path):
    declared = canonical.declared_identity_inputs(root)
    return resolve_identity(
        observations_from_transaction_rows(
            canonical_rows(),
            duplicate_summaries=declared.duplicate_summaries,
            token_scopes=declared.token_scopes,
        ),
        policy=declared.policy,
        token_scopes=declared.token_scopes,
    )


def economics(resolution) -> list[tuple[str, str, str, str, str, str]]:
    return sorted(
        (
            event.canonical_account_hash,
            event.source_day.isoformat(),
            str(event.signed_amount),
            event.currency,
            event.description,
            event.category,
        )
        for event in resolution.canonical_events
    )


def rationales(resolution) -> list[str]:
    return sorted(item.rationale_code for item in resolution.decisions)


# ---------------------------------------------------------------------------
# The shadow reads exactly the canonical declarations
# ---------------------------------------------------------------------------


def test_the_shadow_resolves_with_the_canonically_declared_inputs(
    tmp_path, monkeypatch
):
    """The regression: same loader, same policy, same generation, same state."""

    root = private_root(tmp_path, monkeypatch)
    declared = canonical.declared_identity_inputs(root)
    observations, _metadata = identity_shadow.observations_from_forensic(root)
    reference = resolve_identity(
        apply_declarations(
            observations,
            duplicate_summaries=declared.duplicate_summaries,
            token_scopes=declared.token_scopes,
        ),
        policy=declared.policy,
        token_scopes=declared.token_scopes,
    )

    report, resolution = identity_shadow.build_report_document(root)

    assert report["policyVersion"] == declared.policy.version
    assert report["policyHash"] == declared.policy.policy_hash
    assert resolution.generation_hash == reference.generation_hash
    assert resolution.canonical_state_hash == reference.canonical_state_hash
    assert report["sourceAuthority"] == reference.source_authority_document()


def test_the_shadow_no_longer_resolves_with_the_default_policy(tmp_path, monkeypatch):
    """Proof the declarations are load-bearing, not decoration."""

    root = private_root(tmp_path, monkeypatch)
    report, _resolution = identity_shadow.build_report_document(root)

    assert report["policyHash"] != DEFAULT_POLICY.policy_hash
    assert report["sourceAuthority"]["intervalCount"] == 2


def test_a_shadow_without_declarations_keeps_the_conservative_default(
    tmp_path, monkeypatch
):
    root = private_root(tmp_path, monkeypatch, declared=False)
    report, _resolution = identity_shadow.build_report_document(root)

    assert report["policyHash"] == DEFAULT_POLICY.policy_hash
    assert report["evidence"]["declarations"]["duplicateSummaryCount"] == 0
    assert report["evidence"]["declarations"]["providerTokenScopeCount"] == 0
    assert report["evidence"]["declarations"]["authorityIntervalCount"] == 0


# ---------------------------------------------------------------------------
# The two producers agree about the same evidence
# ---------------------------------------------------------------------------


def test_the_two_producers_bind_the_same_policy_and_authority(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    report, _resolution = identity_shadow.build_report_document(root)
    identity = canonical_identity(root)

    assert report["policyVersion"] == identity["policyVersion"]
    assert report["policyHash"] == identity["policyHash"]
    assert report["policyDocument"] == identity["policyDocument"]
    assert report["sourceAuthority"] == identity["sourceAuthority"]


def test_the_two_producers_reach_the_same_canonical_state(tmp_path, monkeypatch):
    """Different input shapes, identical economics, identical decisions.

    A row's ``sourceHash`` and a forensic activity's ``sourceHash`` cover
    different bytes, so the two ``generationHash`` values are legitimately
    distinct; pretending otherwise would only prove the fixtures were rigged.
    What must agree is everything the household actually sees: which events
    survive, and why each collapse happened.
    """

    root = private_root(tmp_path, monkeypatch)
    _report, resolution = identity_shadow.build_report_document(root)
    reference = canonical_resolution(root)

    assert economics(resolution) == economics(reference)
    assert rationales(resolution) == rationales(reference)
    assert len(resolution.canonical_events) == len(reference.canonical_events) == 3


def test_the_two_producers_agree_about_every_residual(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    report, _resolution = identity_shadow.build_report_document(root)
    identity = canonical_identity(root)

    assert report["residualByClass"] == identity["residualByClass"]
    assert (
        report["counts"]["unresolvedDuplicateGroups"]
        == identity["unresolvedDuplicateGroups"]
        == 0
    )
    assert (
        report["counts"]["safeAutomaticResolutions"]
        == identity["safeAutomaticResolutions"]
    )
    assert (
        report["counts"]["sourceSuppressedClaims"]
        == identity["sourceSuppressedClaims"]
    )
    assert (
        report["counts"]["authorityCoveredClaims"]
        == identity["authorityCoveredClaims"]
    )


def test_every_declared_mechanism_actually_fires(tmp_path, monkeypatch):
    """Each of the three declarations resolves its own group, in the shadow."""

    root = private_root(tmp_path, monkeypatch)
    report, _resolution = identity_shadow.build_report_document(root)

    assert report["automaticByClass"]["declared-duplicate-summary-suppression"] == 1
    assert report["automaticByClass"]["scoped-shared-provider-token-lineage"] == 1
    assert report["automaticByClass"]["authoritative-source-coverage-suppression"] == 1
    assert report["counts"]["declaredDuplicateSuppressions"] == 1
    assert report["counts"]["scopedSharedTokenLinks"] == 1
    assert report["counts"]["authorityPostingWindowSuppressions"] == 1
    assert report["counts"]["canonicalEventsBefore"] == len(SPEC)
    assert report["counts"]["canonicalEventsAfter"] == 3


def test_without_declarations_the_shadow_still_reports_the_open_groups(
    tmp_path, monkeypatch
):
    """The conservative baseline this wiring replaces, kept provable.

    Undeclared, the engine leaves cross-source economic tuples open rather than
    treating description similarity as source lineage.
    """

    root = private_root(tmp_path, monkeypatch, declared=False)
    report, _resolution = identity_shadow.build_report_document(root)

    assert report["automaticByClass"] == {}
    assert report["counts"]["canonicalEventsAfter"] == len(SPEC)
    assert report["counts"]["unresolvedDuplicateGroups"] == 2
    assert report["counts"]["sourceSuppressedClaims"] == 0
    assert report["counts"]["declaredDuplicateSuppressions"] == 0
    assert report["counts"]["scopedSharedTokenLinks"] == 0


# ---------------------------------------------------------------------------
# Safety of the wiring itself
# ---------------------------------------------------------------------------


def test_the_published_declaration_evidence_is_hashes_and_counts_only(
    tmp_path, monkeypatch
):
    root = private_root(tmp_path, monkeypatch)
    report, _resolution = identity_shadow.build_report_document(root)
    evidence = report["evidence"]["declarations"]

    assert set(evidence) == {
        "policyVersion",
        "policyHash",
        "authorityHash",
        "authorityIntervalCount",
        "duplicateSummaryCount",
        "duplicateSummaryMapHashes",
        "providerTokenScopeCount",
        "providerTokenScopeMapHashes",
    }
    serialized = json.dumps(evidence)
    for secret in (ACCOUNT, DETAIL, SUMMARY, "Synthetic Grocer", "Synthetic Merchant"):
        assert secret not in serialized


def test_the_declaration_map_hashes_bind_the_exact_private_bytes(
    tmp_path, monkeypatch
):
    root = private_root(tmp_path, monkeypatch)
    report, _resolution = identity_shadow.build_report_document(root)
    evidence = report["evidence"]["declarations"]

    assert evidence["duplicateSummaryMapHashes"] == [
        hashlib.sha256(
            (root / "simplefin" / "account-map.json").read_bytes()
        ).hexdigest()
    ]
    assert evidence["providerTokenScopeMapHashes"] == [
        hashlib.sha256(
            (root / "identity" / "provider-token-scopes.json").read_bytes()
        ).hexdigest()
    ]


def test_an_unreadable_declaration_blocks_the_shadow_rather_than_defaulting(
    tmp_path, monkeypatch
):
    root = private_root(tmp_path, monkeypatch)
    (root / "identity" / "source-authority.json").write_bytes(b"{not json")

    with pytest.raises(IdentityShadowError, match="source-authority-map-unreadable"):
        identity_shadow.build_report_document(root)


def test_the_published_shadow_report_replays_byte_for_byte(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)

    first, first_path = identity_shadow.publish_report(root)
    verified = identity_shadow.verify_report(root)
    second, second_path = identity_shadow.publish_report(root)

    assert first == second
    assert first_path == second_path
    assert verified["verified"] is True
    assert verified["declarations"] == first["evidence"]["declarations"]
    assert verified["sourceAuthority"] == first["sourceAuthority"]
