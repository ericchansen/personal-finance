"""Shared constants and deterministic identities for lineage review."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1
POINTER_SCHEMA_VERSION = 1
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_CODE = re.compile(r"^[a-z0-9-]+$")

DECISION_TYPES = frozenset(
    {
        "same-economic-event",
        "distinct-events",
        "linked-transfer",
        "account-handoff-reissue",
        "defer-insufficient-evidence",
        "source-error-mirror",
        "receipt-binding-resolution",
    }
)
SUPPRESSION_TYPES = frozenset(
    {
        "same-economic-event",
        "account-handoff-reissue",
        "source-error-mirror",
    }
)
ROLLBACK_EVIDENCE_KINDS = frozenset(
    {
        "backup-manifest",
        "embedded-rollback",
        "rollback-plan",
        "rollback-receipt",
    }
)
PRIORITIES = (
    ("extract-simplefin-one-to-one", 10),
    ("other-one-to-one", 20),
    ("linked-transfer", 30),
    ("one-to-many", 40),
    ("many-to-many", 50),
    ("ambiguous-receipt-reconciliation", 60),
)
PRIORITY_RANK = dict(PRIORITIES)


class ReviewError(RuntimeError):
    """Private review evidence is missing, stale, conflicting, or invalid."""


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def decision_id(group_id: str) -> str:
    return f"lineage-decision-{stable_hash(['lineage-decision', group_id])[:24]}"


def require_hash(value: Any, code: str) -> str:
    if not isinstance(value, str) or not HEX_64.fullmatch(value):
        raise ReviewError(code)
    return value


def parse_decided_at(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError("review-metadata-incomplete")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReviewError("review-metadata-incomplete") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewError("review-metadata-incomplete")
    return value
