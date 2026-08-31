from pathlib import Path

import pytest

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import (
    plan_fingerprint,
    validate_apply_target,
    validate_private_output,
)


class FakeClient:
    def __init__(self, instance_id="staging-123"):
        self.instance_id = instance_id

    def get(self, path):
        assert path == "/app/info"
        return {"version": "3.7.0", "dbPath": "/data/wealthfolio.db"}


def test_apply_guard_requires_staging_and_matching_fingerprints():
    client = FakeClient()
    fingerprint = plan_fingerprint({"create": ["one"]})
    from importers.rebuild.safety import instance_fingerprint
    staging_id = instance_fingerprint(client, "http://127.0.0.1:18088")

    with pytest.raises(DecisionError, match="production port"):
        validate_apply_target(
            client, "http://127.0.0.1:8088", fingerprint, fingerprint, "staging-123"
        )
    with pytest.raises(DecisionError, match="plan-fingerprint"):
        validate_apply_target(
            client, "http://127.0.0.1:18088", fingerprint, None, staging_id
        )
    with pytest.raises(DecisionError, match="identity"):
        validate_apply_target(
            client, "http://127.0.0.1:18088", fingerprint, fingerprint, "other"
        )

    validate_apply_target(
        client, "http://127.0.0.1:18088", fingerprint, fingerprint, staging_id
    )


def test_vanguard_output_must_be_private_and_outside_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    private = tmp_path / "private"
    checkout.mkdir()
    private.mkdir()

    with pytest.raises(DecisionError, match="repository"):
        validate_private_output(checkout / "plan.json", private, checkout)
    with pytest.raises(DecisionError, match="under --data-dir"):
        validate_private_output(tmp_path / "other" / "plan.json", private, checkout)

    assert validate_private_output(
        private / "plan.json", private, checkout
    ) == (private / "plan.json").resolve()
