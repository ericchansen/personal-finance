"""The shadow must replay the published canonical scope, not re-derive identity.

The forensic publication holds Wealthfolio projection rows: what the application
kept *after* import.  Those rows have no source provenance -- a statement row and
an aggregator posting of the same purchase arrive indistinguishable -- so
resolving them cannot reconstruct source coverage.  On real household evidence
that gap is not theoretical: the shadow reconciled a small minority of the
declared coverage intervals and reported unresolved duplicate groups that
canonical had already resolved, because it was answering a different question
from a weaker input.

These regressions pin the fix.  When a current canonical publication carries the
exact scope its resolver saw, and its forensic and baseline bindings are the ones
current evidence still verifies, the shadow replays that scope and emits the
canonical generation exactly.  Otherwise it stays in forensic mode as an explicit
pre-publication diagnostic and carries a blocker that stops the authority apply.

Every fixture here is invented.  No real institution, account, balance, merchant,
or transaction appears anywhere in this file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from finance_store import identity_shadow
from importers.lineage_review import canonical
from importers.normalized import builder as normalized

ACCOUNT = "SYN-ACCOUNT"
QFX_FILE = "extracts/synthetic/statement.qfx"
FORENSIC_ID = "a" * 64
BASELINE_ID = "b" * 64

# One authoritative statement row, the legacy export of the same purchase two
# days later, and one unrelated authoritative row.  Canonical sees the source
# family of each; the projection rows below have lost it.
SPEC = (
    ("qfx", "extract:stable:QFX-1", "2026-01-15", "-42.00", "Synthetic Merchant", ""),
    ("monarch", "monarch:MON-1", "2026-01-17", "-42.00", "Synthetic Merchant",
     "Household Supplies"),
    ("qfx", "extract:stable:QFX-2", "2026-01-20", "-7.50", "Synthetic Grocer", ""),
)


def canonical_rows() -> list[dict[str, object]]:
    return [
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
        for family, source_id, day, amount, description, category in SPEC
    ]


def projection_activities() -> list[dict[str, object]]:
    """The same economics as the application kept them: no provenance at all."""

    activities = []
    for index, (_family, _source_id, day, amount, description, category) in enumerate(
        SPEC
    ):
        activities.append(
            {
                "activityRef": hashlib.sha256(f"WF-{index}".encode()).hexdigest(),
                "canonicalAccountId": ACCOUNT,
                "sourceAtUtc": None,
                "sourceDateUtc": day,
                "signedEffect": amount,
                "currency": "USD",
                "scopeReason": None,
                # The application stored its own row identity, not the source's.
                "sourceIdentity": f"wealthfolio:row:{index}",
                "description": description,
                "transferGroup": "",
                "dependentState": {},
                "lineage": {"status": "projected"},
                "raw": {"pending": False, "category": category},
            }
        )
    return activities


def coverage_interval(*, family: str, strength: str, count: int, tolerance: int = 0):
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
        "source_transaction_count": count,
        "source_hashes": [hashlib.sha256(f"SYN-{family}".encode()).hexdigest()],
        "trust_cutoff_day": None,
        "posting_date_tolerance_days": tolerance,
    }


def write_declarations(root: Path) -> None:
    (root / "identity").mkdir(parents=True, exist_ok=True)
    (root / "identity" / "source-authority.json").write_text(
        json.dumps(
            {
                "coverageIntervals": [
                    coverage_interval(
                        family="qfx",
                        strength="stable-provider-id",
                        count=2,
                        tolerance=2,
                    ),
                    coverage_interval(
                        family="monarch", strength="legacy-export", count=1
                    ),
                ]
            }
        ),
        encoding="utf-8",
    )


def private_root(tmp_path: Path, monkeypatch) -> Path:
    """A sealed forensic publication holding only projection rows."""

    root = tmp_path / "private"
    publication = root / "audit" / "duplicates" / "publications" / FORENSIC_ID
    publication.mkdir(parents=True)
    detail = json.dumps({"activities": projection_activities()}).encode()
    (publication / "private-audit.json").write_bytes(detail)
    (publication / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": identity_shadow.forensic.SCHEMA_VERSION,
                "private": True,
                "readOnly": True,
                "baselinePublicationSha256": BASELINE_ID,
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
        lambda *_a, **_k: (publication, {"publicationId": FORENSIC_ID}),
    )
    write_declarations(root)
    return root


def publish(root: Path, **binding) -> dict[str, object]:
    """Write a canonical publication exactly as the producer would."""

    declarations = canonical.declared_identity_inputs(root)
    projection = canonical._automatic_identity_projection(
        canonical_rows(),
        set(),
        declarations.duplicate_summaries,
        declarations.token_scopes,
        declarations.policy,
    )
    directory = root / "normalized" / "canonical"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transaction-observations.json").write_text(
        json.dumps(
            {
                "schemaVersion": normalized.SCHEMA_VERSION,
                "kind": "canonical-transaction-observations",
                "private": True,
                "observationCount": 0,
                "observations": [],
                "identityScope": projection["identityScope"],
            }
        ),
        encoding="utf-8",
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": normalized.SCHEMA_VERSION,
                "lineageReview": {
                    "identityPolicy": projection["identity"],
                    "forensicPublicationId": FORENSIC_ID,
                    "baselinePublicationId": BASELINE_ID,
                    **binding,
                },
            }
        ),
        encoding="utf-8",
    )
    return projection


def rewrite(root: Path, name: str, mutate) -> None:
    path = root / "normalized" / "canonical" / name
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")


# ---------------------------------------------------------------------------
# What the projection rows can and cannot answer
# ---------------------------------------------------------------------------


def test_legacy_projection_rows_cannot_reconstruct_source_coverage(
    tmp_path, monkeypatch
):
    """The real-shape failure: no provenance, so no interval reconciles."""

    root = private_root(tmp_path, monkeypatch)

    report, _resolution = identity_shadow.build_report_document(root)

    assert report["evidence"]["identitySource"] == (
        identity_shadow.FORENSIC_DIAGNOSTIC_SOURCE
    )
    authority = report["sourceAuthority"]
    assert authority["intervalCount"] == 2
    assert authority["reconciledIntervalCount"] == 0
    assert authority["authoritativeIntervalCount"] == 0
    assert report["counts"]["authorityCoveredClaims"] == 0
    assert report["counts"]["sourceSuppressedClaims"] == 0
    # The harm: the legacy export of the statement row survives as its own
    # canonical event, because nothing in a projection row says which source
    # observed it.  Canonical, reading provenance, keeps two.
    assert report["counts"]["canonicalEventsAfter"] == 3


def test_forensic_mode_blocks_authority_apply(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)

    report, _resolution = identity_shadow.build_report_document(root)

    assert report["evidence"]["blockers"] == [
        identity_shadow.PREPUBLICATION_BLOCKER,
        "canonical-publication-missing",
    ]


def test_canonical_scope_replay_emits_the_published_generation(tmp_path, monkeypatch):
    """The fix: replay the published scope and match canonical exactly."""

    root = private_root(tmp_path, monkeypatch)
    projection = publish(root)
    published = projection["identity"]

    report, resolution = identity_shadow.build_report_document(root)

    assert report["evidence"]["identitySource"] == (
        identity_shadow.CANONICAL_SCOPE_SOURCE
    )
    assert report["evidence"]["blockers"] == []
    for key in ("policyVersion", "policyHash", "generationHash", "canonicalStateHash"):
        assert report[key] == published[key]
    assert report["sourceAuthority"] == published["sourceAuthority"]
    counts = report["counts"]
    assert counts["unresolvedDuplicateGroups"] == 0
    assert counts["authorityAmbiguousGroups"] == 0
    assert counts["authorityCoveredClaims"] == published["authorityCoveredClaims"]
    assert counts["sourceSuppressedClaims"] == published["sourceSuppressedClaims"]
    assert len(resolution.canonical_events) == 2


def test_canonical_replay_reconciles_every_declared_interval(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    publish(root)

    report, _resolution = identity_shadow.build_report_document(root)

    authority = report["sourceAuthority"]
    assert authority["intervalCount"] == 2
    assert authority["reconciledIntervalCount"] == 2


def test_canonical_replay_reads_the_scope_not_the_projection_rows(
    tmp_path, monkeypatch
):
    """Scope row count comes from the publication, never from forensic rows."""

    root = private_root(tmp_path, monkeypatch)
    projection = publish(root)
    scope = projection["identityScope"]

    report, _resolution = identity_shadow.build_report_document(root)

    assert len(scope["rows"]) == projection["identity"]["automaticScopeRows"]
    assert report["counts"]["observations"] == len(scope["rows"])
    assert report["evidence"]["canonicalPublication"]["identityScopeHash"] == (
        scope["scopeHash"]
    )
    assert report["evidence"]["canonicalPublication"]["forensicPublicationId"] == (
        FORENSIC_ID
    )
    assert report["evidence"]["canonicalPublication"]["baselinePublicationId"] == (
        BASELINE_ID
    )


def test_canonical_replay_is_stable_across_repeats(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    publish(root)

    first, _one = identity_shadow.build_report_document(root)
    second, _two = identity_shadow.build_report_document(root)

    assert first == second


# ---------------------------------------------------------------------------
# The bindings must be the ones current evidence still verifies
# ---------------------------------------------------------------------------


def test_stale_forensic_binding_falls_back_to_the_diagnostic(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    publish(root, forensicPublicationId="c" * 64)

    report, _resolution = identity_shadow.build_report_document(root)

    assert report["evidence"]["identitySource"] == (
        identity_shadow.FORENSIC_DIAGNOSTIC_SOURCE
    )
    assert "canonical-publication-forensic-binding-stale" in (
        report["evidence"]["blockers"]
    )


def test_stale_baseline_binding_falls_back_to_the_diagnostic(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    publish(root, baselinePublicationId="d" * 64)

    report, _resolution = identity_shadow.build_report_document(root)

    assert "canonical-publication-baseline-binding-stale" in (
        report["evidence"]["blockers"]
    )


def test_unverified_forensic_evidence_refuses_canonical_replay(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    publish(root)
    monkeypatch.setattr(
        identity_shadow.forensic,
        "verify",
        lambda *_a, **_k: (_ for _ in ()).throw(
            identity_shadow.forensic.ForensicAuditError(
                "baseline evidence manifest changed or is incomplete"
            )
        ),
    )

    report, _resolution = identity_shadow.build_report_document(root)

    assert report["evidence"]["blockers"] == [
        identity_shadow.PREPUBLICATION_BLOCKER,
        "canonical-publication-evidence-unverified",
        "current-evidence-baseline-refresh-required",
    ]


def test_tampered_scope_refuses_canonical_replay(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    publish(root)
    rewrite(
        root,
        "transaction-observations.json",
        lambda document: document["identityScope"]["rows"].pop(),
    )

    report, _resolution = identity_shadow.build_report_document(root)

    assert report["evidence"]["identitySource"] == (
        identity_shadow.FORENSIC_DIAGNOSTIC_SOURCE
    )
    assert identity_shadow.PREPUBLICATION_BLOCKER in report["evidence"]["blockers"]


def test_rewritten_published_counts_refuse_canonical_replay(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    publish(root)

    def bump(document):
        policy = document["lineageReview"]["identityPolicy"]
        policy["sourceSuppressedClaims"] = policy["sourceSuppressedClaims"] + 1

    rewrite(root, "manifest.json", bump)

    report, _resolution = identity_shadow.build_report_document(root)

    assert "canonical-identity-count-drift" in report["evidence"]["blockers"]


# ---------------------------------------------------------------------------
# The published shadow report carries the mode it was built in
# ---------------------------------------------------------------------------


def test_verify_report_exposes_the_canonical_identity_source(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)
    projection = publish(root)

    identity_shadow.publish_report(root)
    summary = identity_shadow.verify_report(root)

    assert summary["identitySource"] == identity_shadow.CANONICAL_SCOPE_SOURCE
    assert summary["blockers"] == []
    assert summary["generationHash"] == projection["identity"]["generationHash"]
    assert summary["canonicalPublication"]["forensicPublicationId"] == FORENSIC_ID


def test_verify_report_reports_the_diagnostic_blocker(tmp_path, monkeypatch):
    root = private_root(tmp_path, monkeypatch)

    identity_shadow.publish_report(root)
    summary = identity_shadow.verify_report(root)

    assert summary["identitySource"] == identity_shadow.FORENSIC_DIAGNOSTIC_SOURCE
    assert identity_shadow.PREPUBLICATION_BLOCKER in summary["blockers"]
    assert summary["canonicalPublication"] is None


def test_publishing_before_and_after_canonical_changes_the_report(
    tmp_path, monkeypatch
):
    root = private_root(tmp_path, monkeypatch)
    diagnostic, _one = identity_shadow.build_report_document(root)
    publish(root)
    authoritative, _two = identity_shadow.build_report_document(root)

    assert diagnostic["generationHash"] != authoritative["generationHash"]
    assert diagnostic["counts"]["canonicalEventsAfter"] == 3
    assert authoritative["counts"]["canonicalEventsAfter"] == 2
    assert authoritative["counts"]["unresolvedDuplicateGroups"] == 0


def test_shadow_never_fabricates_provenance_for_projection_rows(
    tmp_path, monkeypatch
):
    """Nothing may invent a source family for an application row."""

    root = private_root(tmp_path, monkeypatch)
    observations, _metadata = identity_shadow.observations_from_forensic(root)

    assert {item.source_family for item in observations} == {"unknown"}


def test_canonical_publication_evidence_refuses_a_missing_binding(
    tmp_path, monkeypatch
):
    root = private_root(tmp_path, monkeypatch)
    publish(root)
    rewrite(root, "manifest.json", lambda document: document.pop("lineageReview"))

    with pytest.raises(Exception) as error:
        identity_shadow.canonical_publication_evidence(root)

    assert error.value.code == "canonical-publication-missing"
