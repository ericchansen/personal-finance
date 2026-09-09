"""Interrupted private receipt publication must not strand durable journal state."""

import json
import os

import pytest

from finance_store import incremental_inputs as inputs
from importers.rebuild import immutable_metadata


class Interrupted(BaseException):
    pass


def test_interrupted_receipt_write_never_exposes_partial_json(tmp_path, monkeypatch):
    path = tmp_path / "incremental" / "receipts" / "synthetic.json"
    document = {"state": "applied", "runHash": "a" * 64}
    original = immutable_metadata._write_block

    def partial(stream, block):
        stream.write(block[:5])
        stream.flush()
        os.fsync(stream.fileno())
        raise Interrupted()

    monkeypatch.setattr(immutable_metadata, "_write_block", partial)
    with pytest.raises(Interrupted):
        inputs.write_receipt(tmp_path, str(path.relative_to(tmp_path)), document)
    assert not path.exists()
    monkeypatch.setattr(immutable_metadata, "_write_block", original)
    inputs.write_receipt(tmp_path, str(path.relative_to(tmp_path)), document)
    assert json.loads(path.read_text()) == document
    assert list(path.parent.iterdir()) == [path]
    path.chmod(0o600)


def test_receipt_replay_is_idempotent_but_different_content_is_held(tmp_path):
    document = {"state": "applied"}
    path = inputs.write_receipt(tmp_path, "receipt.json", document)
    before = path.read_bytes()
    assert inputs.write_receipt(tmp_path, "receipt.json", document) == path
    with pytest.raises(inputs.IncrementalHold, match="immutable-receipt-conflict"):
        inputs.write_receipt(tmp_path, "receipt.json", {"state": "pending"})
    assert path.read_bytes() == before
    path.chmod(0o600)
