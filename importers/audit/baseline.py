"""Capture and verify private evidence without mutating Wealthfolio."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from importers.analytics.publication import ensure_durable_directory, fsync_directory
from importers.monarch.wealthfolio_client import WealthfolioError
from importers.rebuild.safety import plan_fingerprint, validate_private_output

SCHEMA_VERSION = 1
POINTER_SCHEMA_VERSION = 1
OUTPUT_RELATIVE = Path("audit") / "baselines"
DOWNSTREAM_OUTPUTS = frozenset(
    {
        Path("audit") / "duplicates",
        Path("audit") / ".lineage-state",
        Path("audit") / "lineage-review",
        Path("normalized") / "analytics",
        Path("normalized") / "analytics-diagnostics",
        Path("normalized") / "canonical",
        Path("postgres-shadow"),
        Path("wealthfolio-rebuild"),
        Path("automation"),
        Path("incremental"),
    }
)
PAGE_SIZE = 500
READ_CONCURRENCY = 8
READ_ONLY_POSTS = frozenset({"/activities/search"})
GAP_STATUSES = frozenset({404, 405, 501})
AF_UNIX_REPARSE_TAG = 0x80000023
REDACTED = "<redacted>"
LIVE_DATABASE_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
NOT_TRANSFER_PAIR_MESSAGE = (
    "Activity error: Invalid data: Activity is not a valid internal transfer pair"
)
NOT_SPENDING_ACTIVITY_MESSAGE = (
    "Invalid input: Activity account is not opted into spending tracking"
)
IMPORT_CONTEXT_KINDS = ("CSV_ACTIVITY", "CSV_HOLDINGS", "BROKER_ACTIVITY")
HASHED_PATH_FIELDS = frozenset({"dbpath", "logsdir"})
SECRET_PATHS = frozenset(
    {
        "simplefin/access-url.txt",
        "wealthfolio/admin-password.txt",
    }
)
REQUIRED_DOMAINS = frozenset(
    {
        "accounts",
        "activities",
        "allocation-target-constraints",
        "allocation-target-weights",
        "allocation-targets",
        "alternative-holdings",
        "app-info",
        "asset-profiles",
        "asset-assignments",
        "assets",
        "assignments",
        "backup-inventory",
        "budget-period-inventory",
        "exchange-rate-history",
        "exchange-rates",
        "goals",
        "health",
        "holdings",
        "import-metadata",
        "market-data-provider-settings",
        "portfolios",
        "quote-history",
        "settings",
        "spending-activities",
        "spending-budget",
        "spending-events",
        "spending-rules",
        "spending-settings",
        "splits",
        "taxonomies",
        "transfer-groups",
    }
)


class BaselineError(RuntimeError):
    """A complete, trustworthy baseline could not be produced or verified."""


@dataclass(frozen=True)
class Domain:
    name: str
    method: str
    endpoint: str
    expected: type
    read: Callable[[], Any]


class ReadOnlyClient:
    """Make mutation routes physically unreachable during a baseline capture."""

    def __init__(self, client: Any):
        self._client = client

    @staticmethod
    def _route(path: str) -> str:
        return path.partition("?")[0]

    def get(self, path: str) -> Any:
        return self._client.get(path)

    def post(self, path: str, payload: Any) -> Any:
        if self._route(path) not in READ_ONLY_POSTS:
            raise BaselineError(f"read-only baseline refused POST {self._route(path)}")
        return self._client.post(path, payload)

    def put(self, path: str, payload: Any) -> Any:
        raise BaselineError(f"read-only baseline refused PUT {self._route(path)}")

    def delete(self, path: str, payload: Any = None) -> Any:
        raise BaselineError(f"read-only baseline refused DELETE {self._route(path)}")

    def backup_database(self) -> Any:
        raise BaselineError("read-only baseline refused database backup creation")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return (
        "apikey" in normalized
        or "secret" in normalized
        or "password" in normalized
        or "credential" in normalized
        or "authorization" in normalized
        or "accessurl" in normalized
        or "accessuri" in normalized
        or "cookie" in normalized
        or normalized.startswith(("authheader", "oauth"))
        or normalized
        in {
            "auth",
            "authentication",
            "privatekey",
            "sessionid",
            "sessionkey",
            "sessiontoken",
        }
        or normalized.endswith(
            ("token", "tokens", "privatekey", "clientkey", "sessionkey")
        )
    )


def _credential_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    query_keys = [key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)]
    fragment_keys = [
        key for key, _value in parse_qsl(parsed.fragment, keep_blank_values=True)
    ]
    return any(_sensitive_key(key) for key in (*query_keys, *fragment_keys))


def _sensitive_value(value: str, key: str) -> bool:
    stripped = value.strip()
    if stripped.casefold().startswith(("bearer ", "basic ")):
        return True
    if "BEGIN " in stripped and "PRIVATE KEY" in stripped:
        return True
    return _credential_url(value)


def _named_sensitive_value(value: dict[str, Any]) -> bool:
    for field in ("name", "key", "headerName"):
        candidate = value.get(field)
        if isinstance(candidate, str) and _sensitive_key(candidate):
            return True
    return False


def sanitize_api_payload(value: Any, *, key: str = "") -> Any:
    """Remove credentials and host-local paths before API state is serialized."""
    normalized = _normalized_key(key)
    if normalized in HASHED_PATH_FIELDS:
        if not isinstance(value, str):
            raise BaselineError(f"{key} must be a string before sanitization")
        return f"sha256:{_sha256_bytes(value.encode('utf-8'))}"
    if _sensitive_key(key) and value is not None and not isinstance(value, bool):
        return REDACTED
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        named_sensitive = _named_sensitive_value(value)
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise BaselineError("API payload contains a non-string object key")
            if (
                named_sensitive
                and _normalized_key(child_key) in {"value", "headervalue"}
                and child is not None
            ):
                sanitized[child_key] = REDACTED
            else:
                sanitized[child_key] = sanitize_api_payload(child, key=child_key)
        return sanitized
    if isinstance(value, list):
        return [sanitize_api_payload(child, key=key) for child in value]
    if isinstance(value, str) and _sensitive_value(value, key):
        return REDACTED
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise BaselineError("API payload contains a non-JSON value")


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise BaselineError("evidence path leaves --data-dir") from None


def _is_secret(path: Path) -> bool:
    name = path.name.casefold()
    parts = {part.casefold() for part in path.parts}
    relative = path.as_posix().casefold()
    secret_words = ("password", "credential", "secret", "token", "cookie", "auth")
    return (
        relative in SECRET_PATHS
        or name in {".env", ".netrc"}
        or name.startswith(".env.")
        or path.suffix.casefold() in {".key", ".pem", ".p12", ".pfx"}
        or any(word in name for word in secret_words)
        or bool(parts & {"secrets", "credentials"})
    )


def _artifact_kind(path: Path) -> str:
    text = path.as_posix().casefold()
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    if _is_secret(path):
        return "credential-material"
    if (
        "receipt" in name
        or "receipts" in parts
        or name.startswith(("apply-report-", "apply-report."))
    ):
        return "receipt"
    if "decision" in name or "decisions" in parts:
        return "decision"
    if "mapping" in name or "mappings" in parts or name.endswith("-map.json"):
        return "mapping"
    if "plan" in name or "plans" in parts:
        return "plan"
    if "facts" in parts or "fact" in name:
        return "fact"
    if _is_canonical_output_path(path):
        return "canonical-publication"
    if "normalized/analytics" in text:
        return "analytics-publication"
    if parts & {"raw", "recordings", "snapshots"}:
        return "raw-snapshot"
    if parts & {"extracts", "legacy", "imports"}:
        return "source-extract"
    if parts & {"deploy", "deployment"} or name in {
        "compose.yml",
        "compose.yaml",
        "docker-compose.yml",
    }:
        return "deployment-metadata"
    if "backup" in name or "backups" in parts:
        return "database-backup"
    return "other-evidence"


def _is_canonical_output_path(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    return len(parts) >= 2 and parts[:2] == ("normalized", "canonical")


def _is_downstream_output_path(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    if path.is_absolute() or ".." in parts:
        return False
    if (
        len(parts) >= 2
        and parts[0] == "normalized"
        and (
            parts[1].startswith(".canonical-staging-")
            or parts[1].startswith(".canonical-backup-")
        )
    ):
        return True
    return any(
        parts[: len(candidate.parts)]
        == tuple(part.casefold() for part in candidate.parts)
        for candidate in DOWNSTREAM_OUTPUTS
    )


def _entry_metadata(path: Path, relative: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise BaselineError(
            f"cannot inspect evidence entry: {relative.as_posix()}"
        ) from None


def _is_regular_file(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and getattr(metadata, "st_reparse_tag", None) != AF_UNIX_REPARSE_TAG
    )


def _is_live_database(path: Path) -> bool:
    return path.name.casefold().endswith(LIVE_DATABASE_SUFFIXES)


def _stable_inventory(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for entry in entries:
        if entry.get("kind") == "live-database-state":
            continue
        path = Path(str(entry.get("path") or ""))
        if _is_downstream_output_path(path):
            continue
        normalized = dict(entry)
        if entry.get("kind") == "canonical-publication":
            normalized["kind"] = _artifact_kind(path)
        result.append(normalized)
    return result


def inventory_evidence(data_dir: Path) -> list[dict[str, Any]]:
    """Hash the evidence tree while omitting credentials and baseline outputs."""
    root = data_dir.resolve()
    if not root.is_dir():
        raise BaselineError("--data-dir must be an existing directory")
    output = (root / OUTPUT_RELATIVE).resolve()
    entries: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        try:
            children = sorted(
                directory.iterdir(), key=lambda item: item.name.casefold()
            )
        except FileNotFoundError:
            if directory == root:
                raise BaselineError("--data-dir must be an existing directory") from None
            return
        except OSError:
            relative_directory = directory.relative_to(root).as_posix() or "."
            raise BaselineError(
                f"cannot enumerate evidence directory: {relative_directory}"
            ) from None
        for path in children:
            relative = path.relative_to(root)
            if (
                relative == OUTPUT_RELATIVE
                or OUTPUT_RELATIVE in relative.parents
                or _is_downstream_output_path(relative)
            ):
                continue
            kind = _artifact_kind(relative)
            if kind != "database-backup" and _is_live_database(path):
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "kind": "live-database-state",
                        "omitted": True,
                        "reason": "live database contents and metadata are excluded",
                    }
                )
                continue
            metadata = _entry_metadata(path, relative)
            if metadata is None:
                continue
            try:
                resolved = path.resolve()
            except FileNotFoundError:
                continue
            except OSError:
                raise BaselineError(
                    f"cannot resolve evidence entry: {relative.as_posix()}"
                ) from None
            try:
                resolved_relative = resolved.relative_to(root)
            except ValueError:
                raise BaselineError(
                    f"evidence symlink escapes --data-dir: {relative.as_posix()}"
                ) from None
            if (
                resolved == output
                or output in resolved.parents
                or _is_downstream_output_path(resolved_relative)
            ):
                continue
            if stat.S_ISLNK(metadata.st_mode):
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "kind": "symlink",
                        "omitted": True,
                        "reason": "symlinks are not followed",
                    }
                )
                continue
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
                continue
            if not _is_regular_file(metadata):
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "kind": "special-file",
                        "omitted": True,
                        "reason": "non-regular files are not read",
                    }
                )
                continue
            if kind == "credential-material":
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "kind": kind,
                        "omitted": True,
                        "reason": "credential contents and metadata are excluded",
                    }
                )
                continue
            try:
                digest = _sha256(path)
            except FileNotFoundError:
                continue
            except OSError:
                raise BaselineError(
                    f"cannot read evidence file: {relative.as_posix()}"
                ) from None
            entries.append(
                {
                    "path": relative.as_posix(),
                    "kind": kind,
                    "size": metadata.st_size,
                    "sha256": digest,
                    "omitted": False,
                }
            )

    visit(root)
    return sorted(entries, key=lambda entry: entry["path"])


def _all_activities(client: ReadOnlyClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 0
    expected_total: int | None = None
    while True:
        result = client.post(
            "/activities/search", {"page": page, "pageSize": PAGE_SIZE}
        )
        meta = result.get("meta") if isinstance(result, dict) else None
        total = meta.get("totalRowCount") if isinstance(meta, dict) else None
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("data"), list)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
        ):
            raise TypeError("activity search response is not a paginated object")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise BaselineError("activity count changed during baseline pagination")
        batch = result["data"]
        if any(not isinstance(row, dict) for row in batch):
            raise TypeError("activity search contains a non-object")
        rows.extend(batch)
        if len({_activity_id(row) for row in rows}) != len(rows):
            raise BaselineError("activity pagination returned duplicate records")
        if len(rows) == expected_total:
            return rows
        if len(rows) > expected_total:
            raise BaselineError("activity pagination exceeded its declared total")
        if not batch or len(batch) < PAGE_SIZE:
            raise BaselineError("activity pagination ended before its declared total")
        page += 1


def _activity_id(row: dict[str, Any]) -> str:
    value = row.get("id")
    if not isinstance(value, str) or not value:
        raise TypeError("activities contains a record without an id")
    return value


def _ids(rows: list[dict[str, Any]], domain: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        value = row.get("id")
        if not isinstance(value, str) or not value:
            raise TypeError(f"{domain} contains a record without an id")
        values.append(value)
    return values


def _child_get(client: ReadOnlyClient, path: str, domain: str) -> Any:
    try:
        return client.get(path)
    except WealthfolioError as exc:
        if exc.status in GAP_STATUSES:
            raise BaselineError(
                f"{domain} changed or became unavailable during baseline capture"
            ) from None
        raise


def _matches_typed_error(
    exc: WealthfolioError, path: str, message: str
) -> bool:
    try:
        error = json.loads(exc.body)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        exc.status == 400
        and exc.path == path
        and isinstance(error, dict)
        and error.get("code") == 400
        and error.get("message") == message
    )


def _taxonomies(client: ReadOnlyClient) -> dict[str, Any]:
    rows = client.get("/taxonomies")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TypeError("taxonomy list is invalid")
    details = {
        taxonomy_id: _child_get(
            client,
            f"/taxonomies/{quote(taxonomy_id, safe='')}",
            "taxonomy state",
        )
        for taxonomy_id in _ids(rows, "taxonomies")
    }
    return {"taxonomies": rows, "details": details}


def _read_indexed(
    keys: list[str], read: Callable[[str], Any]
) -> dict[str, Any]:
    result = {}
    with ThreadPoolExecutor(max_workers=READ_CONCURRENCY) as executor:
        # Bound both in-flight requests and queued work. A failed batch must
        # not continue issuing reads for the rest of a large household ledger.
        for offset in range(0, len(keys), READ_CONCURRENCY):
            batch = keys[offset:offset + READ_CONCURRENCY]
            result.update(zip(batch, executor.map(read, batch), strict=True))
    return result


def _activity_children(
    client: ReadOnlyClient, activity_ids: list[str], suffix: str
) -> dict[str, Any]:
    def read(activity_id: str) -> Any:
        path = f"/spending/activities/{quote(activity_id, safe='')}/{suffix}"
        try:
            return _child_get(client, path, f"activity {suffix}")
        except WealthfolioError as exc:
            if _matches_typed_error(exc, path, NOT_SPENDING_ACTIVITY_MESSAGE):
                return []
            raise
    return _read_indexed(activity_ids, read)


def _transfer_pairs(
    client: ReadOnlyClient, activity_ids: list[str]
) -> dict[str, Any]:
    def read(activity_id: str) -> Any:
        path = f"/activities/{quote(activity_id, safe='')}/transfer-pair"
        try:
            return _child_get(client, path, "transfer state")
        except WealthfolioError as exc:
            if _matches_typed_error(exc, path, NOT_TRANSFER_PAIR_MESSAGE):
                return None
            raise
    return _read_indexed(activity_ids, read)


def _goals(client: ReadOnlyClient) -> dict[str, Any]:
    rows = client.get("/goals")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TypeError("goal list is invalid")
    goal_ids = _ids(rows, "goals")
    return {
        "goals": rows,
        "funding": {
            goal_id: _child_get(
                client,
                f"/goals/{quote(goal_id, safe='')}/funding",
                "goal funding",
            )
            for goal_id in goal_ids
        },
        "plans": {
            goal_id: _child_get(
                client,
                f"/goals/{quote(goal_id, safe='')}/plan",
                "goal plan",
            )
            for goal_id in goal_ids
        },
    }


def _holdings(client: ReadOnlyClient, accounts: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for account_id in _ids(accounts, "accounts"):
        query = urlencode({"accountId": account_id, "includeClosed": "true"})
        snapshots = client.get(f"/snapshots?{urlencode({'accountId': account_id})}")
        if not isinstance(snapshots, list):
            raise TypeError("snapshot list is invalid")
        snapshot_dates = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise TypeError("snapshot list contains a non-object")
            snapshot_date = snapshot.get("snapshotDate")
            if not isinstance(snapshot_date, str) or not snapshot_date:
                raise TypeError("snapshot metadata has no snapshotDate")
            snapshot_dates.append(snapshot_date)

        def read_snapshot(snapshot_date: str) -> Any:
            detail_query = urlencode({"accountId": account_id, "date": snapshot_date})
            return _child_get(
                client,
                f"/snapshots/holdings?{detail_query}",
                "holdings snapshot state",
            )
        snapshot_rows = _read_indexed(snapshot_dates, read_snapshot)
        result[account_id] = {
            "current": client.get(f"/holdings?{query}"),
            "snapshots": snapshots,
            "snapshotHoldings": snapshot_rows,
        }
    return result


def _import_metadata(
    client: ReadOnlyClient, accounts: list[dict[str, Any]]
) -> dict[str, Any]:
    mappings = {
        account_id: {
            context_kind: _child_get(
                client,
                "/activities/import/mapping?"
                + urlencode(
                    {
                        "accountId": account_id,
                        "contextKind": context_kind,
                    }
                ),
                "activity import mapping",
            )
            for context_kind in IMPORT_CONTEXT_KINDS
        }
        for account_id in _ids(accounts, "accounts")
    }
    return {
        "accountMappings": mappings,
        "templates": client.get("/activities/import/templates"),
    }


def _asset_profiles(
    client: ReadOnlyClient, assets: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        asset_id: _child_get(
            client,
            f"/assets/profile?{urlencode({'assetId': asset_id})}",
            "asset profile state",
        )
        for asset_id in _ids(assets, "assets")
    }


def _asset_assignments(
    client: ReadOnlyClient, assets: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        asset_id: _child_get(
            client,
            f"/taxonomies/assignments/asset/{quote(asset_id, safe='')}",
            "asset taxonomy assignment state",
        )
        for asset_id in _ids(assets, "assets")
    }


def _quote_history(
    client: ReadOnlyClient, assets: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        asset_id: _child_get(
            client,
            f"/market-data/quotes/history?{urlencode({'symbol': asset_id})}",
            "quote history",
        )
        for asset_id in _ids(assets, "assets")
    }


def _allocation_child_state(
    client: ReadOnlyClient, targets: list[dict[str, Any]], suffix: str
) -> dict[str, Any]:
    return {
        target_id: _child_get(
            client,
            f"/allocation-targets/{quote(target_id, safe='')}/{suffix}",
            f"allocation target {suffix}",
        )
        for target_id in _ids(targets, "allocation targets")
    }


def _record_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return sum(_record_count(child) for child in value.values()) or 1
    return 1


def _capture(domain: Domain) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = domain.read()
    except WealthfolioError as exc:
        if exc.status not in GAP_STATUSES:
            raise BaselineError(
                f"{domain.name} read failed with HTTP {exc.status}"
            ) from None
        capability = {
            "status": "unavailable",
            "reasonType": "http-status",
            "httpStatus": exc.status,
            "method": domain.method,
            "endpoint": exc.path,
        }
        return capability, {
            "schemaVersion": SCHEMA_VERSION,
            "domain": domain.name,
            "status": "unavailable",
            "gap": capability,
        }
    except TypeError as exc:
        capability = {
            "status": "incompatible",
            "reasonType": "response-shape",
            "method": domain.method,
            "endpoint": domain.endpoint,
        }
        return capability, {
            "schemaVersion": SCHEMA_VERSION,
            "domain": domain.name,
            "status": "incompatible",
            "gap": {**capability, "detail": str(exc)},
        }
    if not isinstance(payload, domain.expected):
        capability = {
            "status": "incompatible",
            "reasonType": "response-shape",
            "method": domain.method,
            "endpoint": domain.endpoint,
        }
        return capability, {
            "schemaVersion": SCHEMA_VERSION,
            "domain": domain.name,
            "status": "incompatible",
            "gap": capability,
        }
    payload = sanitize_api_payload(payload)
    count = _record_count(payload)
    capability = {
        "status": "available",
        "method": domain.method,
        "endpoint": domain.endpoint,
        "recordCount": count,
    }
    return capability, {
        "schemaVersion": SCHEMA_VERSION,
        "domain": domain.name,
        "status": "available",
        "recordCount": count,
        "records": payload,
    }


def capture_domains(
    client: Any,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, str]]:
    guarded = ReadOnlyClient(client)
    app_info = guarded.get("/app/info")
    if not isinstance(app_info, dict) or not app_info.get("version") or not app_info.get(
        "dbPath"
    ):
        raise BaselineError("Wealthfolio app identity is incomplete")
    accounts = guarded.get("/accounts?includeArchived=true")
    if not isinstance(accounts, list) or any(
        not isinstance(account, dict) for account in accounts
    ):
        raise BaselineError("Wealthfolio accounts response is invalid")
    activities = _all_activities(guarded)
    activity_ids = _ids(activities, "activities")
    assets = guarded.get("/assets")
    if not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
        raise BaselineError("Wealthfolio assets response is invalid")
    allocation_targets = guarded.get("/allocation-targets")
    if not isinstance(allocation_targets, list) or any(
        not isinstance(target, dict) for target in allocation_targets
    ):
        raise BaselineError("Wealthfolio allocation targets response is invalid")

    domains = (
        Domain("app-info", "GET", "/app/info", dict, lambda: app_info),
        Domain(
            "accounts",
            "GET",
            "/accounts?includeArchived=true",
            list,
            lambda: accounts,
        ),
        Domain(
            "activities",
            "POST",
            "/activities/search",
            list,
            lambda: activities,
        ),
        Domain(
            "transfer-groups",
            "GET",
            "/activities/{id}/transfer-pair",
            dict,
            lambda: _transfer_pairs(guarded, activity_ids),
        ),
        Domain("taxonomies", "GET", "/taxonomies", dict, lambda: _taxonomies(guarded)),
        Domain(
            "assignments",
            "GET",
            "/spending/activities/{id}/assignments",
            dict,
            lambda: _activity_children(guarded, activity_ids, "assignments"),
        ),
        Domain(
            "splits",
            "GET",
            "/spending/activities/{id}/splits",
            dict,
            lambda: _activity_children(guarded, activity_ids, "splits"),
        ),
        Domain(
            "spending-events",
            "GET",
            "/spending/events",
            dict,
            lambda: {
                "eventTypes": guarded.get("/spending/event-types"),
                "events": guarded.get("/spending/events"),
            },
        ),
        Domain(
            "spending-activities",
            "GET",
            "/spending/cash-activities",
            list,
            lambda: guarded.get("/spending/cash-activities"),
        ),
        Domain(
            "spending-rules",
            "GET",
            "/spending/rules",
            list,
            lambda: guarded.get("/spending/rules"),
        ),
        Domain(
            "spending-budget",
            "GET",
            "/spending/budget",
            dict,
            lambda: guarded.get("/spending/budget"),
        ),
        Domain(
            "spending-settings",
            "GET",
            "/spending/settings",
            dict,
            lambda: guarded.get("/spending/settings"),
        ),
        Domain("settings", "GET", "/settings", dict, lambda: guarded.get("/settings")),
        Domain("goals", "GET", "/goals", dict, lambda: _goals(guarded)),
        Domain("assets", "GET", "/assets", list, lambda: assets),
        Domain(
            "asset-profiles",
            "GET",
            "/assets/profile?assetId={id}",
            dict,
            lambda: _asset_profiles(guarded, assets),
        ),
        Domain(
            "asset-assignments",
            "GET",
            "/taxonomies/assignments/asset/{assetId}",
            dict,
            lambda: _asset_assignments(guarded, assets),
        ),
        Domain(
            "quote-history",
            "GET",
            "/market-data/quotes/history?symbol={assetId}",
            dict,
            lambda: _quote_history(guarded, assets),
        ),
        Domain(
            "exchange-rates",
            "GET",
            "/exchange-rates/latest",
            list,
            lambda: guarded.get("/exchange-rates/latest"),
        ),
        Domain(
            "market-data-provider-settings",
            "GET",
            "/providers/settings",
            list,
            lambda: guarded.get("/providers/settings"),
        ),
        Domain(
            "holdings",
            "GET",
            "/holdings and /snapshots",
            dict,
            lambda: _holdings(guarded, accounts),
        ),
        Domain(
            "alternative-holdings",
            "GET",
            "/alternative-holdings",
            list,
            lambda: guarded.get("/alternative-holdings"),
        ),
        Domain(
            "portfolios",
            "GET",
            "/portfolios",
            list,
            lambda: guarded.get("/portfolios"),
        ),
        Domain(
            "allocation-targets",
            "GET",
            "/allocation-targets",
            list,
            lambda: allocation_targets,
        ),
        Domain(
            "allocation-target-weights",
            "GET",
            "/allocation-targets/{id}/weights",
            dict,
            lambda: _allocation_child_state(
                guarded, allocation_targets, "weights"
            ),
        ),
        Domain(
            "allocation-target-constraints",
            "GET",
            "/allocation-targets/{id}/constraints",
            dict,
            lambda: _allocation_child_state(
                guarded, allocation_targets, "constraints"
            ),
        ),
        Domain(
            "health",
            "GET",
            "/healthz and /health/status",
            dict,
            lambda: {
                "liveness": guarded.get("/healthz"),
                "status": guarded.get("/health/status"),
            },
        ),
        Domain(
            "backup-inventory",
            "GET",
            "/utilities/database/backups",
            list,
            lambda: guarded.get("/utilities/database/backups"),
        ),
        Domain(
            "import-metadata",
            "GET",
            "/activities/import/mapping and /activities/import/templates",
            dict,
            lambda: _import_metadata(guarded, accounts),
        ),
    )
    documents: dict[str, bytes] = {}
    capabilities: dict[str, Any] = {}
    for domain in domains:
        capability, snapshot = _capture(domain)
        capabilities[domain.name] = capability
        documents[f"{domain.name}.json"] = _json_bytes(snapshot)
    known_gap = {
        "status": "unavailable",
        "reasonType": "known-api-gap",
        "method": "(no route)",
        "endpoint": "(no route)",
    }
    capabilities["budget-period-inventory"] = known_gap
    documents["budget-period-inventory.json"] = _json_bytes(
        {
            "schemaVersion": SCHEMA_VERSION,
            "domain": "budget-period-inventory",
            "status": "unavailable",
            "gap": {
                **known_gap,
                "detail": (
                    "Wealthfolio 3.7.0 can read one known period but has no API "
                    "that enumerates every configured budget period."
                ),
            },
        }
    )
    capabilities["exchange-rate-history"] = known_gap
    documents["exchange-rate-history.json"] = _json_bytes(
        {
            "schemaVersion": SCHEMA_VERSION,
            "domain": "exchange-rate-history",
            "status": "unavailable",
            "gap": {
                **known_gap,
                "detail": (
                    "Wealthfolio 3.7.0 exposes latest exchange-rate state but "
                    "has no authenticated read API for historical exchange rates."
                ),
            },
        }
    )
    if {name.removesuffix(".json") for name in documents} != REQUIRED_DOMAINS:
        raise BaselineError("baseline domain implementation is incomplete")
    identity = {"version": str(app_info["version"]), "dbPath": str(app_info["dbPath"])}
    return documents, capabilities, identity


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("xb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def _origin(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BaselineError("--base-url must be an HTTP(S) origin")
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or default_port}"


def build(
    data_dir: str | Path,
    client: Any,
    *,
    base_url: str,
    repo_root: str | Path,
    now: datetime | None = None,
    before_pointer: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    output = root / OUTPUT_RELATIVE
    validate_private_output(output, root, Path(repo_root))
    source_files = inventory_evidence(root)
    documents, capabilities, app_identity = capture_domains(client)
    generated = now or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        raise BaselineError("baseline timestamp must include a timezone")
    record_counts = {
        name: result.get("recordCount", 0)
        for name, result in sorted(capabilities.items())
    }
    domain_files = {
        name: {
            "path": f"domains/{name}",
            "sha256": _sha256_bytes(content),
            "recordCount": record_counts[name.removesuffix(".json")],
        }
        for name, content in sorted(documents.items())
    }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "private": True,
        "readOnly": True,
        "generatedAt": generated.astimezone(timezone.utc).isoformat(),
        "environmentFingerprint": plan_fingerprint(
            {
                "origin": _origin(base_url),
                "version": app_identity["version"],
                "dbPath": app_identity["dbPath"],
            }
        ),
        "sourceFiles": source_files,
        "capabilityResults": capabilities,
        "recordCounts": record_counts,
        "domainFiles": domain_files,
    }
    manifest_content = _json_bytes(manifest)
    publication_id = _sha256_bytes(manifest_content)
    publications = output / "publications"
    ensure_durable_directory(publications, fsync_directory)
    publication = publications / publication_id
    staging = publications / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        domains_dir = staging / "domains"
        domains_dir.mkdir()
        for name, content in documents.items():
            _write_synced(domains_dir / name, content)
        _write_synced(staging / "manifest.json", manifest_content)
        fsync_directory(domains_dir)
        fsync_directory(staging)
        if publication.exists():
            if (publication / "manifest.json").read_bytes() != manifest_content:
                raise BaselineError("baseline publication identifier collision")
            existing_domains = publication / "domains"
            if (
                not existing_domains.is_dir()
                or {path.name for path in existing_domains.iterdir()} != set(documents)
                or any(not path.is_file() for path in existing_domains.iterdir())
                or {path.name for path in publication.iterdir()}
                != {"manifest.json", "domains"}
            ):
                raise BaselineError("existing baseline publication is incomplete")
            for name, content in documents.items():
                if (existing_domains / name).read_bytes() != content:
                    raise BaselineError(
                        f"existing baseline publication is corrupt: {name}"
                    )
        else:
            os.replace(staging, publication)
            fsync_directory(publications)
        if before_pointer:
            before_pointer(publication)
        pointer = {
            "schemaVersion": POINTER_SCHEMA_VERSION,
            "publicationId": publication_id,
            "manifestSha256": publication_id,
        }
        _atomic_write(output / "current.json", _json_bytes(pointer))
        return {
            "publication": pointer,
            "sourceFileCount": len(source_files),
            "domainCount": len(documents),
            "recordCounts": record_counts,
            "outputPath": str(output),
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _current(root: Path) -> tuple[Path, dict[str, Any]]:
    output = root / OUTPUT_RELATIVE
    pointer_path = output / "current.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError("baseline current pointer is missing or invalid") from exc
    publication_id = pointer.get("publicationId")
    if (
        pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION
        or not isinstance(publication_id, str)
        or len(publication_id) != 64
        or pointer.get("manifestSha256") != publication_id
    ):
        raise BaselineError("baseline current pointer schema is invalid")
    publication = output / "publications" / publication_id
    manifest_path = publication / "manifest.json"
    if not manifest_path.is_file() or _sha256(manifest_path) != publication_id:
        raise BaselineError("baseline current manifest hash mismatch")
    return publication, pointer


def verify(
    data_dir: str | Path, *, repo_root: str | Path
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    validate_private_output(root / OUTPUT_RELATIVE, root, Path(repo_root))
    publication, pointer = _current(root)
    try:
        manifest = json.loads((publication / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError("baseline manifest is missing or invalid") from exc
    if (
        manifest.get("schemaVersion") != SCHEMA_VERSION
        or manifest.get("private") is not True
        or manifest.get("readOnly") is not True
    ):
        raise BaselineError("baseline manifest schema is invalid")
    source_files = manifest.get("sourceFiles")
    if not isinstance(source_files, list) or _stable_inventory(
        source_files
    ) != _stable_inventory(inventory_evidence(root)):
        raise BaselineError("baseline evidence manifest changed or is incomplete")
    for entry in source_files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise BaselineError("baseline evidence entry is invalid")
        relative = Path(entry["path"])
        if _is_downstream_output_path(relative):
            continue
        path = (root / relative).resolve()
        _safe_relative(path, root)
        if entry.get("omitted") is True:
            continue
        try:
            metadata = path.stat()
            digest = _sha256(path)
        except OSError:
            raise BaselineError(
                f"baseline evidence became unavailable: {entry['path']}"
            ) from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != entry.get("size")
            or digest != entry.get("sha256")
        ):
            raise BaselineError(f"baseline evidence hash mismatch: {entry['path']}")
    capabilities = manifest.get("capabilityResults")
    record_counts = manifest.get("recordCounts")
    domain_files = manifest.get("domainFiles")
    if not all(
        isinstance(value, dict)
        for value in (capabilities, record_counts, domain_files)
    ):
        raise BaselineError("baseline domain manifest is invalid")
    if set(capabilities) != set(record_counts) or {
        f"{name}.json" for name in capabilities
    } != set(domain_files):
        raise BaselineError("baseline domain manifest is incomplete")
    if set(capabilities) != REQUIRED_DOMAINS:
        raise BaselineError("baseline required domain inventory is incomplete")
    if (
        {path.name for path in publication.iterdir()} != {"manifest.json", "domains"}
        or not (publication / "domains").is_dir()
        or {path.name for path in (publication / "domains").iterdir()}
        != set(domain_files)
    ):
        raise BaselineError("baseline publication contains an incomplete file set")
    for name, capability in capabilities.items():
        status = capability.get("status")
        if (
            status not in {"available", "unavailable", "incompatible"}
            or not isinstance(capability.get("method"), str)
            or not isinstance(capability.get("endpoint"), str)
        ):
            raise BaselineError(f"baseline capability schema mismatch: {name}")
        if status == "available":
            if (
                not isinstance(capability.get("recordCount"), int)
                or isinstance(capability.get("recordCount"), bool)
                or capability["recordCount"] < 0
                or capability["recordCount"] != record_counts[name]
            ):
                raise BaselineError(f"baseline capability count mismatch: {name}")
        elif (
            not isinstance(capability.get("reasonType"), str)
            or record_counts[name] != 0
        ):
            raise BaselineError(f"baseline capability gap mismatch: {name}")
    for name, reference in domain_files.items():
        if not isinstance(reference, dict) or reference.get("path") != f"domains/{name}":
            raise BaselineError("baseline domain reference is invalid")
        path = publication / "domains" / name
        if not path.is_file() or _sha256(path) != reference.get("sha256"):
            raise BaselineError(f"baseline domain hash mismatch: {name}")
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BaselineError(f"baseline domain JSON is invalid: {name}") from exc
        domain_name = name.removesuffix(".json")
        if (
            snapshot.get("schemaVersion") != SCHEMA_VERSION
            or snapshot.get("domain") != domain_name
            or snapshot.get("status") != capabilities[domain_name].get("status")
        ):
            raise BaselineError(f"baseline domain schema mismatch: {name}")
        actual_count = (
            _record_count(snapshot.get("records"))
            if snapshot.get("status") == "available"
            else 0
        )
        if (
            actual_count != record_counts[domain_name]
            or actual_count != reference.get("recordCount")
            or (
                snapshot.get("status") == "available"
                and snapshot.get("recordCount") != actual_count
            )
        ):
            raise BaselineError(f"baseline domain count mismatch: {name}")
    return {
        "verified": True,
        "publication": pointer,
        "sourceFileCount": len(source_files),
        "domainCount": len(domain_files),
        "recordCounts": record_counts,
        "outputPath": str(root / OUTPUT_RELATIVE),
    }
