"""Pure ownership-contract checks; native staged requests live in test_incremental."""

import json
import uuid

import pytest

from importers.monarch.mutation_guard import (
    INCREMENTAL_WRITER_MODE_VALUE, MutationInterlockError, WRITER_MARKER_RELATIVE,
    current_incremental_release, incremental_writer_context,
    require_wealthfolio_mutations, validate_writer_ownership_marker, writer_marker_hash,
)
from tests.test_mutation_interlock import _activate_writer_marker


def marker(root):
    body = {
        "schemaVersion": 2, "mode": "incremental-projector-only",
        "writerModeToken": INCREMENTAL_WRITER_MODE_VALUE, "environmentId": "e" * 64,
        "origin": "http://127.0.0.1:18091", "instanceId": "a" * 64,
        "release": current_incremental_release(),
        "scopes": [{"scopeId": str(uuid.uuid4()), "configurationHash": "b" * 64}],
        "activatedAt": "2026-01-01T00:00:00+00:00",
    }
    body["markerHash"] = writer_marker_hash(body)
    path = root / WRITER_MARKER_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return body


def test_schema1_fixed_production_origin_is_not_relaxed(tmp_path):
    original = _activate_writer_marker(tmp_path / "legacy.json")
    assert validate_writer_ownership_marker(original)["schemaVersion"] == 1
    changed = {**original, "origin": "http://127.0.0.1:18091"}
    changed["markerHash"] = writer_marker_hash(changed)
    with pytest.raises(MutationInterlockError, match="invalid"):
        validate_writer_ownership_marker(changed)


@pytest.mark.parametrize("invalid", ["https://example.org:443", "http://127.0.0.1:1234/path",
                                   "http://name:secret@127.0.0.1:1234", "http://127.0.0.1"])
def test_incremental_origin_must_be_a_bare_loopback_origin(tmp_path, invalid):
    body = marker(tmp_path)
    body["origin"] = invalid
    body["markerHash"] = writer_marker_hash(body)
    with pytest.raises(MutationInterlockError, match="invalid"):
        validate_writer_ownership_marker(body)


def test_incremental_contract_never_accepts_fictional_whole_rebuild_fields(tmp_path):
    body = marker(tmp_path)
    assert validate_writer_ownership_marker(body)["schemaVersion"] == 2
    body["preparationId"] = "c" * 64
    body["markerHash"] = writer_marker_hash(body)
    with pytest.raises(MutationInterlockError, match="invalid"):
        validate_writer_ownership_marker(body)


def test_incremental_request_context_is_removed_on_failure(tmp_path, monkeypatch):
    body = marker(tmp_path)
    monkeypatch.setenv("WEALTHFOLIO_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("WEALTHFOLIO_WRITER_MODE", INCREMENTAL_WRITER_MODE_VALUE)
    monkeypatch.setenv("WEALTHFOLIO_WRITER_ENVIRONMENT_ID", body["environmentId"])
    with pytest.raises(RuntimeError, match="synthetic"):
        with incremental_writer_context(
            base_url=body["origin"], data_dir=tmp_path, instance_id=body["instanceId"],
            environment_id=body["environmentId"], scope_id=body["scopes"][0]["scopeId"],
            configuration_hash=body["scopes"][0]["configurationHash"],
        ):
            require_wealthfolio_mutations("POST", "/activities/bulk", base_url=body["origin"], data_dir=tmp_path)
            raise RuntimeError("synthetic request interruption")
    with pytest.raises(MutationInterlockError, match="scoped request context"):
        require_wealthfolio_mutations("POST", "/activities/bulk", base_url=body["origin"], data_dir=tmp_path)
