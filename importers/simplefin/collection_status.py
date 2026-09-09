"""Private, content-addressed status records for source-only collection."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from finance_store.domain import content_hash, utc
from finance_store.simplefin import detect_protocol_version
from finance_store.source_admission import (
    DEFAULT_CONNECTION_ID,
    partition_snapshot,
    snapshot_observed_at,
)


OUTPUT_RELATIVE = Path("automation") / "source-collection"
SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _account_rows(payload: Mapping[str, Any], version: str) -> list[dict[str, Any]]:
    body = (
        payload.get("data")
        if version == "2" and isinstance(payload.get("data"), Mapping)
        else payload
    )
    rows = body.get("accounts") if isinstance(body, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("SimpleFIN response account inventory is invalid")
    return rows


def _error_rows(payload: Mapping[str, Any], version: str) -> list[Any]:
    body = (
        payload.get("data")
        if version == "2" and isinstance(payload.get("data"), Mapping)
        else payload
    )
    if not isinstance(body, Mapping):
        raise ValueError("SimpleFIN response body is invalid")
    rows = body.get("errlist") if version == "2" else body.get("errors")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("SimpleFIN response error inventory is invalid")
    return rows


def _expected_account_hashes(data_dir: Path) -> tuple[str, ...]:
    path = data_dir / "simplefin" / "account-map.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("SimpleFIN expected account inventory is unavailable") from exc
    accounts = document.get("accounts") if isinstance(document, dict) else None
    if document.get("version") != 1 or not isinstance(accounts, dict):
        raise ValueError("SimpleFIN expected account inventory is invalid")
    return tuple(sorted(content_hash(str(account_id)) for account_id in accounts))


def build_receipt(
    data_dir: Path,
    snapshot: Path,
    payload: Mapping[str, Any],
    *,
    requested_days: int,
    observed_at: datetime,
    release_commit: str,
) -> dict[str, Any]:
    root = data_dir.resolve()
    snapshot = snapshot.resolve()
    snapshot.relative_to(root)
    version = detect_protocol_version(dict(payload))
    accounts = _account_rows(payload, version)
    errors = _error_rows(payload, version)
    observed_ids = tuple(
        sorted(
            content_hash(str(row.get("id") or row.get("account_id") or ""))
            for row in accounts
            if str(row.get("id") or row.get("account_id") or "")
        )
    )
    if len(set(observed_ids)) != len(observed_ids):
        raise ValueError("SimpleFIN response contains duplicate account identities")
    expected_ids = _expected_account_hashes(root)
    request_path = snapshot.with_name(
        f"request-{snapshot.stem.removeprefix('simplefin-')}.json"
    )
    request_document = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request_document, dict):
        raise ValueError("SimpleFIN request manifest is invalid")
    request_start = str(request_document.get("requestedStart") or "")
    request_end = str(request_document.get("requestedEnd") or "")
    if str(request_document.get("protocolVersion") or "") != version:
        raise ValueError("SimpleFIN request and response protocol versions disagree")
    requested_start = date.fromisoformat(request_start)
    requested_end = date.fromisoformat(request_end)
    if (requested_end - requested_start).days + 1 != requested_days:
        raise ValueError("SimpleFIN request manifest window does not match the run")
    actual_observed_at = snapshot_observed_at(snapshot, observed_at)
    partition = partition_snapshot(
        snapshot_sha256=_sha256(snapshot),
        observed_at=actual_observed_at,
        version=version,
        accounts=accounts,
        errors=errors,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    body = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "simplefin-source-collection-run",
        "status": "collected",
        "releaseCommit": release_commit or "development",
        "observedAt": actual_observed_at.isoformat(),
        "protocolVersion": version,
        "requestWindowDays": requested_days,
        "inputManifest": {
            "snapshotSha256": _sha256(snapshot),
            "requestMetadataSha256": _sha256(request_path),
            "requestedStart": request_start,
            "requestedEnd": request_end,
        },
        "watermark": {
            "snapshotSha256": _sha256(snapshot),
            "observedAt": actual_observed_at.isoformat(),
            "requestedThrough": request_end,
        },
        "inventory": {
            "expectedAccountCount": len(expected_ids),
            "observedAccountCount": len(observed_ids),
            "expectedInventoryHash": content_hash(expected_ids),
            "observedInventoryHash": content_hash(observed_ids),
            "missingExpectedCount": len(set(expected_ids) - set(observed_ids)),
            "newObservedCount": len(set(observed_ids) - set(expected_ids)),
        },
        "connections": [
            {
                "connectionIdHash": content_hash(item.connection_id),
                "accountCount": len(
                    partition.accounts_by_scope.get(item.connection_id, ())
                ),
                "actionableErrorCount": len(item.actionable_errors),
                "advisoryCount": len(item.advisories),
            }
            for item in partition.evidence
            if item.connection_id != DEFAULT_CONNECTION_ID
            or item.errors
            or partition.accounts_by_scope.get(item.connection_id)
        ],
    }
    body["inputSetHash"] = content_hash(
        {
            "inputManifest": body["inputManifest"],
            "expectedInventoryHash": body["inventory"]["expectedInventoryHash"],
        }
    )
    body["receiptHash"] = content_hash(body)
    return body


def build_failure_receipt(
    *,
    requested_days: int,
    observed_at: datetime,
    release_commit: str,
    error_code: str,
    snapshot: Path | None = None,
) -> dict[str, Any]:
    input_manifest = {
        "snapshotSha256": _sha256(snapshot) if snapshot is not None else None,
        "requestWindowDays": requested_days,
    }
    body = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "simplefin-source-collection-run",
        "status": "failed",
        "releaseCommit": release_commit or "development",
        "observedAt": utc(observed_at).isoformat(),
        "errorCode": error_code,
        "inputManifest": input_manifest,
        "inputSetHash": content_hash(input_manifest),
    }
    body["receiptHash"] = content_hash(body)
    return body


def write_receipt(data_dir: Path, receipt: Mapping[str, Any]) -> Path:
    root = data_dir.resolve()
    output = root / OUTPUT_RELATIVE
    runs = output / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    receipt_hash = str(receipt.get("receiptHash") or "")
    if len(receipt_hash) != 64:
        raise ValueError("source collection receipt hash is invalid")
    content = (
        json.dumps(dict(receipt), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode()
    target = runs / f"{receipt_hash}.json"
    if target.exists():
        if target.read_bytes() != content:
            raise ValueError("source collection receipt hash collision")
    else:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o444)
    pointer = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "simplefin-source-collection-pointer",
        "receiptHash": receipt_hash,
        "inputSetHash": receipt["inputSetHash"],
        "releaseCommit": receipt["releaseCommit"],
        "status": receipt["status"],
    }

    def update_pointer(name: str) -> None:
        temporary = output / f".{name}-{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(pointer, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, output / name)

    update_pointer("current.json")
    if receipt["status"] == "collected":
        update_pointer("latest-success.json")
    return target
