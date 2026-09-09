"""Build and verify a PII-free canonical identity report from private evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from importers.analytics.publication import (
    ensure_durable_directory,
    fsync_directory,
)
from importers.audit import forensic
from importers.rebuild.safety import validate_private_output

from . import canonical_identity
from .domain import content_hash, normalize_money
from .identity import (
    IdentityObservation,
    IdentityPolicy,
    IdentityResolution,
    ProviderTokenScope,
    apply_declarations,
    provider_id_kind,
    resolve_identity,
    source_account_scope,
)
from .identity_declarations import (
    DeclarationError,
    DeclaredIdentityInputs,
    load_declarations,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_RELATIVE = Path("postgres-shadow") / "reports" / "canonical-identity"

# The published canonical scope is the only input that carries source
# provenance.  The Wealthfolio projection rows in the forensic publication are
# post-import application state, so they can never reconstruct which source
# family observed a transaction.
CANONICAL_SCOPE_SOURCE = "canonical-publication-scope"
FORENSIC_DIAGNOSTIC_SOURCE = "forensic-projection-diagnostic"
PREPUBLICATION_BLOCKER = "canonical-publication-scope-required"


class IdentityShadowError(RuntimeError):
    """The private identity report cannot be safely built or verified."""


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_root(data_dir: str | Path) -> Path:
    root = Path(data_dir).resolve(strict=True)
    if not root.is_dir():
        raise IdentityShadowError("private data root is not a directory")
    try:
        root.relative_to(REPOSITORY_ROOT)
    except ValueError:
        return root
    raise IdentityShadowError(
        "identity reports must be generated outside the public repository"
    )


def _find_named(value: Any, names: set[str]) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if normalized in names and child not in (None, ""):
                found.append(str(child))
            found.extend(_find_named(child, names))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_named(child, names))
    return found


def _source_day(value: Any) -> tuple[date, str]:
    raw = str(value or "").strip()
    if not raw:
        raise IdentityShadowError("forensic activity has no source day")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (InvalidOperation, ValueError) as exc:
        raise IdentityShadowError(
            "forensic activity has an invalid source day"
        ) from exc
    signature = f"T{parsed.hour:02d}" if "T" in raw else ""
    return parsed.date(), signature


def _provider_identity(
    activity: dict[str, Any],
    *,
    has_connection_scope: bool,
) -> tuple[str | None, str]:
    source_family = str(activity.get("sourceFamily") or "unknown")
    source_identity = str(activity.get("sourceIdentity") or "")
    raw = activity.get("raw")
    fitids = _find_named(raw, {"fitid"})
    if fitids:
        return fitids[0], "ofx-fitid"
    if not source_identity or source_family == "unknown":
        return None, "none"
    return source_identity, provider_id_kind(
        source_family,
        source_identity,
        has_connection_scope=has_connection_scope,
    )


def _sealed_forensic_publication(
    root: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    publication, pointer = forensic._current(root)
    try:
        manifest = json.loads(
            (publication / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityShadowError("sealed forensic manifest is unavailable") from exc
    files = manifest.get("files")
    if (
        manifest.get("schemaVersion") != forensic.SCHEMA_VERSION
        or manifest.get("private") is not True
        or manifest.get("readOnly") is not True
        or not isinstance(files, dict)
        or "private-audit.json" not in files
    ):
        raise IdentityShadowError("sealed forensic manifest is invalid")
    expected_names = {"manifest.json", *files}
    if {path.name for path in publication.iterdir()} != expected_names:
        raise IdentityShadowError("sealed forensic publication is incomplete")
    for name, reference in files.items():
        path = publication / name
        if (
            not isinstance(reference, dict)
            or not path.is_file()
            or _sha256(path) != reference.get("sha256")
            or path.stat().st_size != reference.get("size")
        ):
            raise IdentityShadowError(
                "sealed forensic publication failed integrity checks"
            )
    return publication, pointer, manifest


def _forensic_evidence(
    root: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], str, list[str]]:
    """Verify the sealed forensic publication and report how strong that was."""

    try:
        verified = forensic.verify(root, repo_root=REPOSITORY_ROOT)
        publication, pointer, _manifest = _sealed_forensic_publication(root)
        return publication, pointer, verified, "current-evidence-verified", []
    except forensic.ForensicAuditError as exc:
        if str(exc) != "baseline evidence manifest changed or is incomplete":
            raise IdentityShadowError(
                "verified forensic evidence is unavailable"
            ) from exc
    publication, pointer, manifest = _sealed_forensic_publication(root)
    verified = {"publication": pointer, "counts": manifest.get("counts", {})}
    return (
        publication,
        pointer,
        verified,
        "sealed-publication-integrity",
        ["current-evidence-baseline-refresh-required"],
    )


def observations_from_forensic(
    data_dir: str | Path,
) -> tuple[tuple[IdentityObservation, ...], dict[str, Any]]:
    root = _private_root(data_dir)
    publication, pointer, verified, verification_mode, blockers = _forensic_evidence(
        root
    )
    try:
        detail = json.loads(
            (publication / "private-audit.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityShadowError("verified forensic evidence is unavailable") from exc
    activities = detail.get("activities")
    if not isinstance(activities, list):
        raise IdentityShadowError("forensic activities are malformed")

    observations = []
    skipped_non_cash = 0
    for activity in activities:
        if not isinstance(activity, dict):
            raise IdentityShadowError("forensic activity is malformed")
        if activity.get("signedEffect") is None:
            skipped_non_cash += 1
            continue
        observation_id = str(activity.get("activityRef") or "")
        if not observation_id:
            raise IdentityShadowError("forensic activity identity is missing")
        source_family = str(activity.get("sourceFamily") or "unknown")
        source_day, writer_signature = _source_day(
            activity.get("sourceAtUtc") or activity.get("sourceDateUtc")
        )
        raw = activity.get("raw")
        connections = _find_named(
            raw,
            {
                "connectionid",
                "providerconnectionid",
                "sourceconnectionid",
            },
        )
        provider_id, provider_kind = _provider_identity(
            activity,
            has_connection_scope=bool(connections),
        )
        attributes = {}
        if writer_signature:
            attributes["writer_timestamp_signature"] = writer_signature
        for output_name, source_names in (
            ("correction_of", {"correctionof", "correctsof"}),
            ("counterpart_id", {"counterpartid", "transfercounterpartid"}),
            ("pending_of", {"pendingof", "replacespendingid"}),
            ("provider_error_of", {"providererrorof", "mirrorof"}),
            ("reversal_of", {"reversalof", "reverses"}),
        ):
            values = _find_named(raw, source_names)
            if values:
                attributes[output_name] = values[0]
        categories = _find_named(raw, {"category", "categoryname"})
        pending_values = _find_named(raw, {"pending", "ispending"})
        pending = any(
            value.casefold() in {"1", "true", "yes"} for value in pending_values
        )
        canonical_account = str(activity.get("canonicalAccountId") or "")
        source_account_id, connection_scope = source_account_scope(
            source_family,
            str(activity.get("sourceIdentity") or ""),
            canonical_account_id=canonical_account,
            declared_source_account_id=str(activity.get("sourceAccountId") or ""),
            explicit_connection_id=connections[0] if connections else "",
        )
        account_status = (
            "excluded" if activity.get("scopeReason") not in (None, "") else "active"
        )
        try:
            amount = normalize_money(Decimal(str(activity.get("signedEffect"))))
        except ValueError as exc:
            raise IdentityShadowError("forensic activity amount is invalid") from exc
        observations.append(
            IdentityObservation(
                observation_id=observation_id,
                source_family=source_family,
                source_connection_id=connection_scope,
                source_account_id=source_account_id,
                canonical_account_id=canonical_account,
                provider_transaction_id=provider_id,
                provider_id_kind=provider_kind,
                source_hash=content_hash(activity),
                source_day=source_day,
                observed_at=datetime.combine(source_day, time.min, tzinfo=timezone.utc),
                signed_amount=amount,
                currency=str(activity.get("currency") or "USD").upper(),
                description=str(activity.get("description") or ""),
                status="pending" if pending else "posted",
                category=categories[0] if categories else "",
                source_group_id=str(activity.get("transferGroup") or ""),
                import_lineage_hash=content_hash(
                    {
                        "lineage": activity.get("lineage"),
                        "dependentState": activity.get("dependentState"),
                    }
                ),
                account_status=account_status,
                attributes=tuple(sorted(attributes.items())),
            )
        )
    metadata = {
        "identitySource": FORENSIC_DIAGNOSTIC_SOURCE,
        "forensicPublicationId": pointer["publicationId"],
        "forensicSummaryHash": content_hash(verified),
        "verificationMode": verification_mode,
        "blockers": blockers,
        "skippedNonCashObservations": skipped_non_cash,
    }
    return tuple(observations), metadata


def declared_identity_inputs(data_dir: str | Path) -> DeclaredIdentityInputs:
    """The same durable declarations the canonical projection reads.

    Canonical projection and this shadow are two producers of one canonical
    identity.  If the shadow resolved with ``DEFAULT_POLICY`` and no
    declarations, it would keep reporting duplicate groups that canonical has
    already resolved, and PostgreSQL shadow evidence would never match canonical
    evidence.  Both therefore read through ``load_declarations``.
    """

    try:
        return load_declarations(_private_root(data_dir))
    except DeclarationError as exc:
        raise IdentityShadowError(
            f"private identity declaration is unusable: {exc.code}"
        ) from exc


def canonical_publication_evidence(data_dir: str | Path) -> dict[str, Any]:
    """Bind the published canonical scope to the currently verified evidence.

    The canonical publication states which forensic and baseline publications it
    was built from.  Replaying its scope is only honest while those bindings are
    the ones current evidence still verifies; otherwise the publication is
    describing a household state that has moved on.
    """

    root = _private_root(data_dir)
    canonical_root = root / "normalized" / "canonical"
    try:
        manifest = json.loads(
            (canonical_root / "manifest.json").read_text(encoding="utf-8")
        )
        binding = manifest["lineageReview"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
        raise canonical_identity.CanonicalIdentityError(
            "canonical-publication-missing"
        ) from exc
    if not isinstance(binding, dict):
        raise canonical_identity.CanonicalIdentityError(
            "canonical-lineage-binding-missing"
        )
    publication, pointer, _verified, mode, blockers = _forensic_evidence(root)
    if mode != "current-evidence-verified" or blockers:
        raise canonical_identity.CanonicalIdentityError(
            "canonical-publication-evidence-unverified"
        )
    if binding.get("forensicPublicationId") != pointer["publicationId"]:
        raise canonical_identity.CanonicalIdentityError(
            "canonical-publication-forensic-binding-stale"
        )
    try:
        forensic_manifest = json.loads(
            (publication / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise canonical_identity.CanonicalIdentityError(
            "canonical-publication-evidence-unverified"
        ) from exc
    baseline = forensic_manifest.get("baselinePublicationSha256")
    declared_baseline = binding.get("baselinePublicationId")
    if declared_baseline is not None and declared_baseline != baseline:
        raise canonical_identity.CanonicalIdentityError(
            "canonical-publication-baseline-binding-stale"
        )
    return {
        "forensicPublicationId": pointer["publicationId"],
        "baselinePublicationId": baseline,
        "queuePublicationId": binding.get("queuePublicationId"),
        "decisionPublicationId": binding.get("decisionPublicationId"),
    }


def _canonical_scope_report(
    root: Path, declared: DeclaredIdentityInputs
) -> tuple[dict[str, Any] | None, IdentityResolution | None, list[str]]:
    try:
        bindings = canonical_publication_evidence(root)
        verified = canonical_identity.verified_resolution(root)
    except canonical_identity.CanonicalIdentityError as exc:
        return None, None, [exc.code]
    report = {
        **verified.resolution.report_document(),
        "evidence": {
            "identitySource": CANONICAL_SCOPE_SOURCE,
            "verificationMode": CANONICAL_SCOPE_SOURCE,
            "blockers": [],
            "canonicalPublication": {**bindings, **verified.proof},
            "forensicPublicationId": bindings["forensicPublicationId"],
            "declarations": declared.evidence(),
        },
    }
    return report, verified.resolution, []


def build_report_document(
    data_dir: str | Path,
    *,
    policy: IdentityPolicy | None = None,
    duplicate_summaries: Iterable[Any] | None = None,
    token_scopes: Iterable[ProviderTokenScope] | None = None,
) -> tuple[dict[str, Any], IdentityResolution]:
    """Report canonical identity from the strongest evidence available.

    A current canonical v5 publication carries the exact scope its resolver saw,
    with provenance for every source family.  The Wealthfolio projection rows in
    the forensic publication do not: they are what the application kept after
    import, so a QFX statement row and the SimpleFIN posting of the same
    purchase arrive indistinguishable.  Resolving them cannot reconstruct source
    coverage, and inventing a source family for them would be a fabrication.

    So when the publication exists and its bindings match current verified
    evidence, this replays the published scope and emits that exact generation.
    Otherwise it falls back to the forensic projection as a *diagnostic* and
    blocks authority apply.
    """

    root = _private_root(data_dir)
    declared = declared_identity_inputs(root)
    if policy is None:
        policy = declared.policy
    if duplicate_summaries is None:
        duplicate_summaries = declared.duplicate_summaries
    if token_scopes is None:
        token_scopes = declared.token_scopes
    token_scopes = tuple(token_scopes)
    report, resolution, unavailable = _canonical_scope_report(root, declared)
    if report is not None and resolution is not None:
        report["reportHash"] = content_hash(report)
        return report, resolution
    observations, metadata = observations_from_forensic(root)
    observations = apply_declarations(
        observations,
        duplicate_summaries=duplicate_summaries,
        token_scopes=token_scopes,
    )
    resolution = resolve_identity(
        observations, policy=policy, token_scopes=token_scopes
    )
    report = {
        **resolution.report_document(),
        "evidence": {
            **metadata,
            "blockers": [
                PREPUBLICATION_BLOCKER,
                *unavailable,
                *metadata["blockers"],
            ],
            "declarations": declared.evidence(),
        },
    }
    report["reportHash"] = content_hash(report)
    return report, resolution


def _write_file(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def publish_report(data_dir: str | Path) -> tuple[dict[str, Any], Path]:
    root = _private_root(data_dir)
    output = root / OUTPUT_RELATIVE
    validate_private_output(output, root, REPOSITORY_ROOT)
    report, _resolution = build_report_document(root)
    report_content = _json_bytes(report)
    manifest = {
        "schemaVersion": 1,
        "kind": "canonical-identity-shadow-publication",
        "private": True,
        "readOnly": True,
        "reportHash": report["reportHash"],
        "files": {
            "report.json": {
                "sha256": _sha256_bytes(report_content),
                "size": len(report_content),
            }
        },
    }
    manifest_content = _json_bytes(manifest)
    publication_id = _sha256_bytes(manifest_content)
    publications = output / "publications"
    publication = publications / publication_id
    ensure_durable_directory(publications)
    if publication.exists():
        if (
            _sha256(publication / "manifest.json") != publication_id
            or (publication / "report.json").read_bytes() != report_content
        ):
            raise IdentityShadowError(
                "content-addressed identity publication is inconsistent"
            )
    else:
        staging = output / f".identity-staging-{uuid.uuid4().hex}"
        ensure_durable_directory(staging)
        try:
            _write_file(staging / "report.json", report_content)
            _write_file(staging / "manifest.json", manifest_content)
            os.replace(staging, publication)
            fsync_directory(publications)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    pointer = {
        "schemaVersion": 1,
        "kind": "canonical-identity-shadow-pointer",
        "private": True,
        "publicationId": publication_id,
        "reportHash": report["reportHash"],
    }
    ensure_durable_directory(output)
    _write_file(output / "current.json", _json_bytes(pointer))
    return report, publication


def verify_report(data_dir: str | Path) -> dict[str, Any]:
    root = _private_root(data_dir)
    output = root / OUTPUT_RELATIVE
    try:
        pointer = json.loads((output / "current.json").read_text(encoding="utf-8"))
        publication_id = str(pointer["publicationId"])
        publication = output / "publications" / publication_id
        manifest_path = publication / "manifest.json"
        report_path = publication / "report.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise IdentityShadowError("identity shadow publication is unavailable") from exc
    if (
        pointer.get("kind") != "canonical-identity-shadow-pointer"
        or pointer.get("private") is not True
        or _sha256(manifest_path) != publication_id
        or manifest.get("kind") != "canonical-identity-shadow-publication"
        or manifest.get("private") is not True
        or manifest.get("readOnly") is not True
        or manifest.get("reportHash") != pointer.get("reportHash")
        or manifest.get("files", {}).get("report.json", {}).get("sha256")
        != _sha256(report_path)
        or manifest.get("files", {}).get("report.json", {}).get("size")
        != report_path.stat().st_size
        or report.get("reportHash") != pointer.get("reportHash")
    ):
        raise IdentityShadowError("identity shadow publication failed integrity checks")
    expected, _resolution = build_report_document(root)
    if expected != report:
        raise IdentityShadowError(
            "identity shadow publication does not match current evidence"
        )
    return {
        "verified": True,
        "publicationId": publication_id,
        "reportHash": report["reportHash"],
        "policyVersion": report["policyVersion"],
        "policyHash": report["policyHash"],
        "generationHash": report["generationHash"],
        "canonicalStateHash": report["canonicalStateHash"],
        "sourceAuthority": report["sourceAuthority"],
        "declarations": report["evidence"]["declarations"],
        "counts": report["counts"],
        "automaticByClass": report["automaticByClass"],
        "automaticByConfidence": report["automaticByConfidence"],
        "automaticBySourceFamily": report["automaticBySourceFamily"],
        "evidenceVerificationMode": report["evidence"]["verificationMode"],
        "identitySource": report["evidence"]["identitySource"],
        "canonicalPublication": report["evidence"].get("canonicalPublication"),
        "blockers": report["evidence"]["blockers"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="Existing private data directory outside this repository",
    )
    result.add_argument("command", choices=("run", "verify"))
    return result


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "run":
            publish_report(args.data_dir)
        summary = verify_report(args.data_dir)
    except (
        IdentityShadowError,
        forensic.ForensicAuditError,
        OSError,
        ValueError,
    ) as exc:
        raise SystemExit(f"identity shadow operation refused: {exc}") from exc
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
