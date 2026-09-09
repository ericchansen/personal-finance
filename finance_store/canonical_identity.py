"""Rebuild the published canonical identity generation for durable storage.

PostgreSQL may only become the durable authority for canonical identity if the
rows it stores are provably the same generation the canonical publication
already declared.  This module is the single bridge: it reads the published
identity scope, replays it through the *same* canonical builder the publication
used, and refuses to hand the result to a writer unless every hash and count
matches the published ``identityPolicy`` exactly.

Nothing here guesses.  A publication that does not carry an identity scope, or
whose replay disagrees by one hash, produces a blocker instead of a weaker
comparison.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from importers.lineage_review.canonical import (
    ReviewError,
    applied_identity_decisions_by_event,
    declared_identity_inputs,
    resolve_canonical_identity,
    validate_identity_scope,
)
from importers.normalized import builder as normalized

from .identity import IdentityResolution

__all__ = [
    "CanonicalIdentityError",
    "VerifiedCanonicalIdentity",
    "plan_binding",
    "published_artifact_hashes",
    "verified_resolution",
]

# Counts that must agree between the published policy block and the replay.
BOUND_COUNTS = (
    "automaticScopeRows",
    "appliedAutomaticDecisions",
    "safeAutomaticResolutions",
    "unresolvedDuplicateGroups",
    "sourceSuppressedClaims",
    "authorityCoveredClaims",
    "authorityAmbiguousGroups",
)

# Counts that must be zero before a generation is durable.
ZERO_COUNTS = ("unresolvedDuplicateGroups", "authorityAmbiguousGroups")


class CanonicalIdentityError(RuntimeError):
    """A safe, code-only failure. The message never carries private values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VerifiedCanonicalIdentity:
    """A replayed generation proven equal to the published identity policy."""

    resolution: IdentityResolution
    published: dict[str, Any]
    proof: dict[str, Any]


def _canonical_dir(root: Path) -> Path:
    return Path(root) / "normalized" / "canonical"


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CanonicalIdentityError("canonical-publication-missing") from exc
    except json.JSONDecodeError as exc:
        raise CanonicalIdentityError("canonical-publication-unreadable") from exc


def _published_policy(root: Path) -> dict[str, Any]:
    manifest = _read(_canonical_dir(root) / "manifest.json")
    if not isinstance(manifest, dict):
        raise CanonicalIdentityError("canonical-publication-unreadable")
    if manifest.get("schemaVersion") != normalized.SCHEMA_VERSION:
        raise CanonicalIdentityError("canonical-lineage-publication-required")
    binding = manifest.get("lineageReview")
    if not isinstance(binding, dict):
        raise CanonicalIdentityError("canonical-lineage-binding-missing")
    policy = binding.get("identityPolicy")
    if not isinstance(policy, dict):
        raise CanonicalIdentityError("canonical-identity-policy-missing")
    return policy


def _published_scope(root: Path) -> tuple[dict[str, Any], ...]:
    observations = _read(_canonical_dir(root) / "transaction-observations.json")
    if not isinstance(observations, dict):
        raise CanonicalIdentityError("canonical-publication-unreadable")
    scope = observations.get("identityScope")
    if scope is None:
        raise CanonicalIdentityError("canonical-identity-scope-missing")
    try:
        validate_identity_scope(scope)
    except ReviewError as exc:
        raise CanonicalIdentityError("canonical-identity-scope-invalid") from exc
    return tuple(dict(row) for row in scope["rows"])


def published_artifact_hashes(root: Path) -> dict[str, str] | None:
    """Replay the artifact binding used by the canonical producer, when present."""
    manifest = _read(_canonical_dir(root) / "manifest.json")
    sources = manifest.get("sourceFiles")
    if sources is None:
        return None
    if not isinstance(sources, list):
        raise CanonicalIdentityError("canonical-source-artifacts-invalid")
    result = {}
    for item in sources:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or item["path"] in result
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in item["sha256"])
        ):
            raise CanonicalIdentityError("canonical-source-artifacts-invalid")
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise CanonicalIdentityError("canonical-source-artifact-unavailable")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise CanonicalIdentityError("canonical-source-artifact-drift")
        result[item["path"]] = item["sha256"]
    return result or None


def verified_resolution(
    root: Path, *, require_resolved: bool = True
) -> VerifiedCanonicalIdentity:
    """Replay the published identity scope and prove the generation matches."""

    published = _published_policy(root)
    scope = _published_scope(root)
    try:
        declared = declared_identity_inputs(Path(root))
    except ReviewError as exc:
        raise CanonicalIdentityError("canonical-identity-declarations-invalid") from exc
    generation = resolve_canonical_identity(
        scope,
        duplicate_summaries=declared.duplicate_summaries,
        token_scopes=declared.token_scopes,
        policy=declared.policy,
        source_artifact_hashes=published_artifact_hashes(root),
    )
    resolution = generation.resolution
    report = resolution.report_document()
    counts = report["counts"]
    applied_by_event = applied_identity_decisions_by_event(resolution)
    replayed = {
        "policyVersion": resolution.policy.version,
        "policyHash": resolution.policy.policy_hash,
        "generationHash": resolution.generation_hash,
        "canonicalStateHash": resolution.canonical_state_hash,
        "automaticScopeRows": len(scope),
        "appliedAutomaticDecisions": sum(
            event.canonical_event_id in applied_by_event
            and len(event.member_observation_ids) >= 2
            for event in resolution.canonical_events
        ),
        "safeAutomaticResolutions": counts["safeAutomaticResolutions"],
        "unresolvedDuplicateGroups": counts["unresolvedDuplicateGroups"],
        "sourceSuppressedClaims": counts["sourceSuppressedClaims"],
        "authorityCoveredClaims": counts["authorityCoveredClaims"],
        "authorityAmbiguousGroups": counts["authorityAmbiguousGroups"],
    }
    for name in (
        "policyVersion",
        "policyHash",
        "generationHash",
        "canonicalStateHash",
    ):
        if published.get(name) != replayed[name]:
            raise CanonicalIdentityError("canonical-identity-generation-drift")
    for name in BOUND_COUNTS:
        if published.get(name) != replayed[name]:
            raise CanonicalIdentityError("canonical-identity-count-drift")
    if published.get("sourceAuthority") != report["sourceAuthority"]:
        raise CanonicalIdentityError("canonical-identity-authority-drift")
    if published.get("residualByClass") != report["residualByClass"]:
        raise CanonicalIdentityError("canonical-identity-residual-drift")
    if require_resolved:
        for name in ZERO_COUNTS:
            if replayed[name]:
                raise CanonicalIdentityError("canonical-identity-unresolved-residual")
    proof = {
        **replayed,
        "identityScopeHash": generation.scope_document()["scopeHash"],
        "claims": len(resolution.claims),
        "canonicalEvents": len(resolution.canonical_events),
        "decisions": len(resolution.decisions),
        "authorityIntervals": len(resolution.policy.source_authority.intervals),
    }
    return VerifiedCanonicalIdentity(
        resolution=resolution, published=published, proof=proof
    )


def plan_binding(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Return the plan's identity binding, or the blockers that prevent one."""

    try:
        verified = verified_resolution(root)
    except CanonicalIdentityError as exc:
        return None, [exc.code]
    return verified.proof, []
