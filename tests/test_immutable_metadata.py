from __future__ import annotations

import json
import os

import pytest

from importers.rebuild import immutable_metadata as metadata
from importers.rebuild import receipt_repair_cli


class PowerLoss(BaseException):
    pass


@pytest.mark.parametrize("failed_block", [1, 2])
def test_interruption_during_bytes_write_never_exposes_partial_final(tmp_path, monkeypatch, failed_block):
    path = tmp_path / "metadata.json"
    document = {"kind": "synthetic", "values": ["synthetic evidence"] * 10000}
    calls = 0
    write_block = metadata._write_block
    def interrupted(stream, block):
        nonlocal calls
        calls += 1
        if calls == failed_block:
            stream.write(block[:max(1, len(block) // 2)])
            stream.flush()
            os.fsync(stream.fileno())
            raise PowerLoss()
        return write_block(stream, block)
    monkeypatch.setattr(metadata, "_write_block", interrupted)
    with pytest.raises(PowerLoss):
        metadata.publish_json(path, document)
    assert not path.exists()
    assert any(child.suffix == ".stage" for child in tmp_path.iterdir())
    monkeypatch.setattr(metadata, "_write_block", write_block)
    metadata.publish_json(path, document)
    assert json.loads(path.read_text()) == document
    assert list(tmp_path.iterdir()) == [path]


def test_existing_matching_metadata_is_verified_and_different_metadata_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "head.json"
    metadata.publish_json(path, {"generation": "synthetic-first"})
    before = path.read_bytes()
    monkeypatch.setattr(metadata, "_write_block", lambda *_: pytest.fail("rewrote existing immutable metadata"))
    metadata.publish_json(path, {"generation": "synthetic-first"})
    with pytest.raises(FileExistsError):
        metadata.publish_json(path, {"generation": "synthetic-fork"})
    assert path.read_bytes() == before


def test_no_overwrite_publication_loses_race_without_replacing_winner(tmp_path, monkeypatch):
    path = tmp_path / "head.json"
    link = os.link
    def winner_first(source, destination):
        winner = tmp_path / "winner"
        with winner.open("xb") as stream:
            stream.write(metadata.json_bytes({"generation": "other-writer"}))
            stream.flush()
            os.fsync(stream.fileno())
        link(winner, destination)
        link(source, destination)
    monkeypatch.setattr(metadata.os, "link", winner_first)
    with pytest.raises(FileExistsError):
        metadata.publish_json(path, {"generation": "losing-writer"})
    assert json.loads(path.read_text()) == {"generation": "other-writer"}


def test_interruption_after_link_leaves_complete_metadata_and_retry_finishes_publication(tmp_path, monkeypatch):
    path = tmp_path / "origin.json"
    document = {"origin": "synthetic"}
    link = os.link
    def linked_then_interrupted(source, destination):
        link(source, destination)
        raise PowerLoss()
    monkeypatch.setattr(metadata.os, "link", linked_then_interrupted)
    with pytest.raises(PowerLoss):
        metadata.publish_json(path, document)
    assert json.loads(path.read_text()) == document
    monkeypatch.setattr(metadata.os, "link", link)
    metadata.publish_json(path, document)
    assert list(tmp_path.iterdir()) == [path]


def test_receipt_reservation_is_not_an_empty_json_document(tmp_path, monkeypatch):
    path = tmp_path / "recovery.json"
    receipt_repair_cli._reserve_output(path)
    assert not path.exists()
    write_block = metadata._write_block
    def partial(stream, block):
        stream.write(block[:5])
        stream.flush()
        os.fsync(stream.fileno())
        raise PowerLoss()
    monkeypatch.setattr(metadata, "_write_block", partial)
    with pytest.raises(PowerLoss):
        receipt_repair_cli._write_reserved(path, {"kind": "synthetic-recovery"})
    assert not path.exists()
    monkeypatch.setattr(metadata, "_write_block", write_block)
    receipt_repair_cli._write_reserved(path, {"kind": "synthetic-recovery"})
    assert json.loads(path.read_text()) == {"kind": "synthetic-recovery"}
    assert not receipt_repair_cli._reservation(path).exists()
