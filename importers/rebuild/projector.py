"""Shared target and backup safeguards for bounded repair and incremental writes."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from .safety import instance_fingerprint


WEALTHFOLIO_VERSION = "3.7.0"
REBUILD_MARKER = "wealthfolio-clean-rebuild-v1"
REBUILD_MARKER_ENV = "WEALTHFOLIO_REBUILD_CANDIDATE"
REBUILD_FINGERPRINT_ENV = "WEALTHFOLIO_REBUILD_INSTANCE_ID"
REBUILD_DB_PATH = "/data/wealthfolio-rebuild.db"
_BACKUP_LOCK = threading.Lock()


class ProjectionError(RuntimeError):
    """A target, backup, or recalculation safeguard rejected an operation."""


@dataclass(frozen=True, slots=True)
class RebuildIdentity:
    base_url: str
    version: str
    db_path: str
    fingerprint: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_rebuild_boundary(base_url: str, marker: str | None) -> None:
    """Reject a production-like endpoint before credentials or transport are used."""
    target = urlparse(base_url)
    if target.scheme not in {"http", "https"} or not _is_loopback(target.hostname):
        raise ProjectionError("rebuild operations require a loopback URL")
    if target.port is None:
        raise ProjectionError("rebuild operations require an explicit candidate port")
    if target.port == 8088:
        raise ProjectionError("rebuild operations refuse production port 8088")
    if marker != REBUILD_MARKER:
        raise ProjectionError(
            f"{REBUILD_MARKER_ENV} must equal the clean-rebuild candidate marker"
        )


def inspect_rebuild_target(
    client: Any, base_url: str, marker: str | None
) -> RebuildIdentity:
    validate_rebuild_boundary(base_url, marker)
    info = client.get("/app/info") or {}
    version = str(info.get("version") or "")
    db_path = str(info.get("dbPath") or "")
    if version != WEALTHFOLIO_VERSION:
        raise ProjectionError(
            f"candidate version must be {WEALTHFOLIO_VERSION}"
        )
    if db_path != REBUILD_DB_PATH:
        raise ProjectionError("candidate database path is not the rebuild path")
    return RebuildIdentity(
        base_url.rstrip("/"),
        version,
        db_path,
        instance_fingerprint(client, base_url),
    )


def require_rebuild_target(
    client: Any,
    base_url: str,
    marker: str | None,
    expected_fingerprint: str | None,
) -> RebuildIdentity:
    identity = inspect_rebuild_target(client, base_url, marker)
    if not expected_fingerprint:
        raise ProjectionError(f"{REBUILD_FINGERPRINT_ENV} is required")
    if identity.fingerprint != expected_fingerprint:
        raise ProjectionError("candidate environment fingerprint does not match")
    return identity


def _backup_inventory(client: Any) -> tuple[list[dict[str, Any]], set[str]]:
    inventory = client.list_backups()
    if not isinstance(inventory, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("filename"), str)
        or not row["filename"]
        for row in inventory
    ):
        raise ProjectionError("candidate backup inventory is invalid")
    names = [str(row["filename"]) for row in inventory]
    if len(names) != len(set(names)):
        raise ProjectionError("candidate backup inventory contains duplicate filenames")
    return inventory, set(names)


def _require_backup(
    client: Any,
    *,
    timeout_seconds: float = 5,
    interval_seconds: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    with _BACKUP_LOCK:
        deadline = clock() + timeout_seconds
        while True:
            before, before_names = _backup_inventory(client)
            current_name = (
                "wealthfolio_backup_"
                f"{utcnow().strftime('%Y%m%d_%H%M%S')}.db"
            )
            if current_name not in before_names:
                break
            if clock() >= deadline:
                raise ProjectionError(
                    "candidate backup timestamp did not become unique"
                )
            sleeper(interval_seconds)
        backup = client.backup_database()
        filename = (
            str(backup.get("filename") or "")
            if isinstance(backup, dict)
            else ""
        )
        if not re.fullmatch(r"wealthfolio_backup_\d{8}_\d{6}\.db", filename):
            raise ProjectionError("candidate mutation requires a confirmed backup")
        if filename in before_names:
            raise ProjectionError(
                "candidate backup would overwrite a same-second filename"
            )
        while True:
            inventory, after_names = _backup_inventory(client)
            added = after_names - before_names
            removed = before_names - after_names
            matching = [
                row for row in inventory if str(row["filename"]) == filename
            ]
            if not removed and added == {filename} and len(matching) == 1:
                if len(inventory) != len(before) + 1:
                    raise ProjectionError(
                        "candidate backup inventory count is ambiguous"
                    )
                return matching[0]
            if removed or len(added) > 1 or (
                added and added != {filename}
            ):
                raise ProjectionError(
                    "candidate backup inventory changed ambiguously"
                )
            if clock() >= deadline:
                raise ProjectionError(
                    "candidate backup did not appear uniquely in inventory"
                )
            sleeper(interval_seconds)


def require_unique_backup(client: Any) -> dict[str, Any]:
    """Create one collision-safe backup and return its unique inventory record."""
    return _require_backup(client)


def wait_for_recalculation(
    client: Any,
    account_ids: Iterable[str],
    *,
    timeout_seconds: float = 60,
    interval_seconds: float = 0.5,
    stable_reads: int = 3,
    minimum_wait_seconds: float = 2,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    ids = sorted(set(account_ids))
    started = clock()
    deadline = started + timeout_seconds
    previous_hash: str | None = None
    stable = 0
    latest: list[dict[str, Any]] = []
    while clock() <= deadline:
        result = client.post("/performance/accounts/simple", {"accountIds": ids})
        if not isinstance(result, list):
            raise ProjectionError("performance recalculation returned an invalid response")
        latest = sorted(
            result, key=lambda row: str(row.get("accountId") or "")
        )
        current_hash = semantic_hash(latest)
        stable = stable + 1 if current_hash == previous_hash else 1
        if (
            stable >= stable_reads
            and clock() - started >= minimum_wait_seconds
        ):
            return latest
        previous_hash = current_hash
        sleeper(interval_seconds)
    raise ProjectionError("Wealthfolio recalculation did not settle before timeout")
