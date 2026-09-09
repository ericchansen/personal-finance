"""Crash-atomic publication of new immutable metadata without replacing a path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from importers.analytics.publication import fsync_directory


def json_bytes(document: dict) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def _write_block(stream, block: bytes) -> int:
    return stream.write(block)


def _existing(path: Path, expected: bytes) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
        raise FileExistsError("metadata destination contains different or unverifiable bytes")
    fsync_directory(path.parent)
    return True


def _clean_stages(path: Path) -> None:
    prefix = f".{path.name}.publish-"
    for candidate in path.parent.iterdir():
        if candidate.name.startswith(prefix) and re.fullmatch(
            r"[0-9a-f]{64}-[0-9a-f]{32}\.stage", candidate.name[len(prefix):]
        ):
            try:
                candidate.unlink()
            except OSError:
                # A concurrent publisher can still own an open staging handle.
                # Its eventual no-overwrite publication must verify this final.
                pass


def publish_bytes(path: Path, content: bytes, *, allow_existing: bool = True) -> None:
    """Link a fully written/fsynced staging inode into place; never publish partial bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _existing(path, content):
        if not allow_existing:
            raise FileExistsError("metadata destination already exists")
        _clean_stages(path)
        return
    digest = hashlib.sha256(content).hexdigest()
    stage = path.parent / f".{path.name}.publish-{digest}-{uuid.uuid4().hex}.stage"
    # Unique staging ownership prevents one publisher from syncing another
    # publisher's still-incomplete inode. Failed writes leave only unpublished stages.
    with stage.open("x+b") as stream:
        position = 0
        while position < len(content):
            written = _write_block(stream, content[position:position + 64 * 1024])
            if not isinstance(written, int) or written <= 0:
                raise OSError("metadata staging write made no progress")
            position += written
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise OSError("metadata staging bytes failed verification")
    try:
        os.link(stage, path)  # Atomic creation, unlike replace/rename with overwrite.
    except OSError:
        if not _existing(path, content):
            raise
        if not allow_existing:
            raise FileExistsError("metadata destination already exists") from None
    fsync_directory(path.parent)
    if not _existing(path, content):
        raise OSError("published metadata disappeared")
    _clean_stages(path)


def publish_json(path: Path, document: dict, *, allow_existing: bool = True) -> None:
    publish_bytes(path, json_bytes(document), allow_existing=allow_existing)
