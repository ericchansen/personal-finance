"""Stable state export used for replay and candidate comparison."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import FinanceState


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            key: _json_value(item)
            for key, item in dataclasses.asdict(value).items()
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return format(normalized, "f")
    if isinstance(value, dict):
        return {key: _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def export_state(state: FinanceState) -> dict[str, Any]:
    collections = {}
    for field in dataclasses.fields(state):
        values = getattr(state, field.name)
        collections[field.name] = sorted(
            (_json_value(item) for item in values),
            key=lambda item: item.get("id", json.dumps(item, sort_keys=True)),
        )
    body = {
        "schemaVersion": 1,
        "candidate": "python-postgresql",
        "state": collections,
    }
    body["stateHash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return body


def state_digest(state: FinanceState) -> str:
    return export_state(state)["stateHash"]


def write_export(state: FinanceState, path: Path) -> None:
    path.write_text(
        json.dumps(export_state(state), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
