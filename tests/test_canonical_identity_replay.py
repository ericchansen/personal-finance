import json
from pathlib import Path

import pytest

from finance_store.canonical_identity import (
    CanonicalIdentityError, published_artifact_hashes, verified_resolution,
)
from importers.normalized.builder import build
from tests.test_normalized import make_estate


def test_real_producer_artifact_binding_replays_even_with_local_residuals(tmp_path):
    root = make_estate(tmp_path)
    manifest = build(root)
    published = manifest["lineageReview"]["identityPolicy"]
    verified = verified_resolution(root, require_resolved=False)
    assert verified.resolution.generation_hash == published["generationHash"]
    assert verified.resolution.canonical_state_hash == published["canonicalStateHash"]
    assert all(
        dict(item.attributes).get("sourceArtifactSha256")
        for item in verified.resolution.observations
    )
    assert published["unresolvedDuplicateGroups"] > 0
    with pytest.raises(CanonicalIdentityError, match="unresolved-residual"):
        verified_resolution(root)


def test_allowing_residuals_does_not_allow_changed_artifacts(tmp_path):
    root = make_estate(tmp_path)
    build(root)
    artifact = root / "extracts" / "example" / "activity.qfx"
    artifact.write_text(artifact.read_text() + "\nchanged source bytes")
    with pytest.raises(CanonicalIdentityError, match="artifact-drift"):
        verified_resolution(root, require_resolved=False)


def test_artifact_manifest_cannot_escape_the_private_root(tmp_path):
    root = make_estate(tmp_path)
    build(root)
    path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["sourceFiles"][0]["path"] = str(Path("..") / "outside.json")
    path.write_text(json.dumps(manifest))
    with pytest.raises(CanonicalIdentityError, match="artifact-unavailable"):
        published_artifact_hashes(root)
