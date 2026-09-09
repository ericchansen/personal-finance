"""Clone-first receipt repair promotion; no whole-rebuild or writer activation.

Every persistent artifact is private. Hash seals detect drift; HMAC seals additionally
require a locally held evidence/operator key. Neither establishes economic identity:
that remains the independently reviewed schema-2 repair plan's responsibility.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from finance_store.domain import content_hash
from importers.analytics.publication import fsync_directory
from importers.lineage_review.canonical import validate_identity_scope
from importers.lineage_review.model import stable_hash
from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.simplefin.application import (
    activity_semantic_fingerprint,
    ledger_fingerprint,
)
from importers.simplefin.spending_adapter import SpendingAdapter

from . import receipt_repair
from .cutover import (
    DockerContainerRuntime,
    _copy_verified,
    _write_cutover_state,
    stage_restore_copy,
    verify_backup_file,
    verify_stage_restore_report,
)
from .safety import instance_fingerprint, plan_fingerprint, validate_private_output
from .immutable_metadata import publish_json


REPO_ROOT = Path(__file__).resolve().parents[2]
KIND = "wealthfolio-bounded-repair-promotion"
APPROVAL = "AUTHORIZE_EXACT_BOUNDED_DATABASE_PROMOTION"
EVIDENCE_KEY_ENV = "WEALTHFOLIO_PROMOTION_EVIDENCE_KEY"
OPERATOR_KEY_ENV = "WEALTHFOLIO_PROMOTION_OPERATOR_KEY"
TERMINAL = {"completed", "rolled-back", "aborted"}
EXECUTION_PHASES = TERMINAL | {
    "authorized", "candidate-staged", "stop-intent", "stale-prestate",
    "original-move-intent", "candidate-install-intent", "candidate-installed",
    "rolling-back", "original-restored", "recovery-required", "manual-recovery-required",
}
IDENTITY_KEYS = (
    "containerId", "containerName", "composeProject", "composeService", "imageId"
)
AUTOMATIC_PROVIDERS = frozenset({
    "YAHOO", "ALPHA_VANTAGE", "MARKETDATA_APP", "METAL_PRICE_API", "FINNHUB",
    "US_TREASURY_CALC", "BOERSE_FRANKFURT",
})
TIMESTAMP_FIELDS = {
    "quotes": frozenset({"created_at"}),
    "quote_sync_state": frozenset({"last_synced_at", "updated_at"}),
    "holdings_snapshots": frozenset({"calculated_at"}),
    "daily_account_valuation": frozenset({"calculated_at"}),
}
SYNC_ENTITIES = {
    "activity": "activities",
    "activity_taxonomy_assignment": "activity_taxonomy_assignments",
}
REVIEW_BINDINGS = (
    "planHash", "evidenceHash", "diffHash", "releaseRevision", "targetHash",
    "clonePrestateDiffHash", "restoreDiffHashes", "runtimePolicyHash", "canonicalEvidenceHash",
)
QUALIFIED_REVIEW_BINDINGS = (
    "historicalMatchingReplayAvailable", "missingHistoricalBindings", "historicalReplayQualificationHash",
)


class PromotionError(RuntimeError):
    """Fail-closed evidence, preservation, or execution error."""


def replay_review_fields(plan: dict) -> dict:
    extension = plan.get("productionLineage") or {}
    if extension.get("historicalMatchingReplayAvailable") is not False:
        return {}
    return {
        "historicalMatchingReplayAvailable": False,
        "missingHistoricalBindings": extension["missingBindings"],
        "historicalReplayQualificationHash": extension["historicalReplayQualificationHash"],
    }


def review_bindings(document: dict) -> dict:
    fields = REVIEW_BINDINGS + (
        QUALIFIED_REVIEW_BINDINGS if document.get("historicalMatchingReplayAvailable") is False else ()
    )
    return {key: document[key] for key in fields}


class ManualRecoveryRequired(PromotionError):
    """The old rollback authority cannot establish that current app work is disposable."""

    def __init__(self, boundary: str, observed_state_hash: str | None = None):
        super().__init__("automatic rollback is blocked to preserve current app data; manual recovery required")
        self.boundary = boundary
        self.observed_state_hash = observed_state_hash


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PromotionError(message)


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private(path: str | Path, root: Path) -> Path:
    value = Path(path)
    return validate_private_output(
        value if value.is_absolute() else root / value, root, REPO_ROOT
    )


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), "artifact must be a JSON object")
    return value


def seal(body: dict, key: bytes) -> dict:
    _require(len(key) >= 32, "signing key must contain at least 32 bytes")
    _require(not {"documentHash", "signature"} & body.keys(), "already sealed document")
    digest = plan_fingerprint(body)
    return {
        **body,
        "documentHash": digest,
        "signature": hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest(),
    }


def unseal(document: dict, key: bytes, kind: str) -> dict:
    body = {k: v for k, v in document.items() if k not in {"documentHash", "signature"}}
    expected = seal(body, key)
    _require(
        document.get("kind") == kind
        and document.get("documentHash") == expected["documentHash"]
        and hmac.compare_digest(str(document.get("signature", "")), expected["signature"]),
        f"invalid {kind} signature or content",
    )
    return body


def write(path: Path, document: dict) -> None:
    try:
        publish_json(path, document)
    except FileExistsError as exc:
        raise PromotionError("metadata path already contains different content") from exc


def release_revision() -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args], check=True, text=True,
            capture_output=True, timeout=30,
        ).stdout.strip()
    _require(not git("status", "--porcelain"), "release checkout must be clean")
    revision = git("rev-parse", "HEAD")
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "invalid release revision")
    return revision


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"sqliteBlobBase64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float):
        return {"sqliteFloatHex": value.hex()}
    return value


def sqlite_state(path: Path, *, stopped: bool = False) -> dict:
    """Full logical contents, including unknown tables, columns, blobs and DDL."""
    _require(path.is_file() and not path.is_symlink(), "database is not a regular file")
    if stopped:
        no_sidecars(path)
    query = "?mode=ro&immutable=1" if stopped else "?mode=ro"
    connection = sqlite3.connect(f"{path.resolve().as_uri()}{query}", uri=True, timeout=0)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        _require(
            [r[0] for r in connection.execute("PRAGMA integrity_check")] == ["ok"],
            "SQLite integrity check failed",
        )
        _require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                 "SQLite foreign key check failed")
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        _require(not any("CREATE VIRTUAL TABLE" in str(r[3]).upper() for r in schema),
                 "virtual tables require a dedicated preservation contract")
        tables = {}
        for kind, name, _, _sql in schema:
            if kind != "table":
                continue
            columns = connection.execute(f"PRAGMA table_xinfo({_quote(name)})").fetchall()
            names = [r[1] for r in columns]
            rows = [
                dict(zip(names, map(_cell, row)))
                for row in connection.execute(
                    "SELECT " + ",".join(map(_quote, names)) + " FROM " + _quote(name)
                )
            ]
            primary_key = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
            rowids = {}
            if not re.search(r"\bWITHOUT\s+ROWID\b", str(_sql), re.IGNORECASE):
                alias = next((key for key in ("rowid", "_rowid_", "oid")
                              if key not in {n.lower() for n in names}), None)
                _require(alias is not None, "hidden rowid is shadowed by schema columns")
                for rowid, *values in connection.execute(
                    f"SELECT {alias}," + ",".join(map(_quote, names)) + " FROM " + _quote(name)
                ):
                    row = dict(zip(names, map(_cell, values)))
                    key = plan_fingerprint([row[k] for k in primary_key]) if primary_key else str(rowid)
                    _require(key not in rowids, "ambiguous nullable primary key")
                    rowids[key] = rowid if primary_key else plan_fingerprint(row)
            tables[name] = {
                "columns": columns,
                "primaryKey": primary_key,
                "rowids": rowids,
                "rows": sorted(rows, key=lambda r: json.dumps(r, sort_keys=True)),
            }
        body = {
            "schema": schema,
            "pragmas": {
                name: connection.execute(f"PRAGMA {name}").fetchone()[0]
                for name in ("user_version", "application_id", "encoding")
            },
            "tables": tables,
        }
        if stopped:
            no_sidecars(path)
        return {**body, "stateHash": plan_fingerprint(body)}
    finally:
        connection.close()


def no_sidecars(path: Path) -> None:
    _require(
        not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")),
        "database has SQLite sidecars; checkpoint/close its owner before proceeding",
    )


def checkpoint_stopped_database(runtime: Any, *, production: bool) -> dict:
    """Consolidate committed WAL after shutdown without changing logical rows."""
    runtime.check(production=production, running=False)
    path = Path(runtime.target["database"]).resolve()
    _require(not Path(str(path) + "-journal").exists(),
             "rollback journal requires explicit recovery")
    if not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
        no_sidecars(path)
        return {"checkpointed": False}
    before = sqlite_state(path)
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=0)) as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            _require(result is not None and result[0] == 0,
                     "stopped database WAL is still in use")
    except sqlite3.Error as exc:
        raise PromotionError("stopped database WAL checkpoint failed") from exc
    after = sqlite_state(path, stopped=True)
    _require(after["stateHash"] == before["stateHash"],
             "database changed while checkpointing; preserve it for review")
    runtime.check(production=production, running=False)
    return {"checkpointed": True, "logicalStateHash": after["stateHash"]}


def database_diff(before: dict, after: dict) -> list[dict]:
    _require(before["schema"] == after["schema"] and before["pragmas"] == after["pragmas"],
             "database DDL or persistent pragmas changed")
    changes = []
    for table, old in before["tables"].items():
        new = after["tables"][table]
        if old == new:
            continue
        _require(old["columns"] == new["columns"], "database columns changed")
        pk = old["primaryKey"]
        if not pk:
            _require(Counter(map(plan_fingerprint, old["rows"])) ==
                     Counter(map(plan_fingerprint, new["rows"])) and
                     old["rowids"] == new["rowids"],
                     "changed table without a primary key is unsupported")
            continue
        def indexed(rows: list[dict]) -> dict[str, dict]:
            result = {plan_fingerprint([r[k] for k in pk]): r for r in rows}
            _require(len(result) == len(rows), "ambiguous primary key")
            return result
        left, right = indexed(old["rows"]), indexed(new["rows"])
        for key in sorted(left.keys() | right.keys()):
            if left.get(key) != right.get(key) or old["rowids"].get(key) != new["rowids"].get(key):
                changes.append({"table": table, "key": key,
                                "before": left.get(key), "after": right.get(key),
                                "beforeRowId": old["rowids"].get(key),
                                "afterRowId": new["rowids"].get(key)})
    return changes


def _camel(row: dict) -> dict:
    def name(key: str) -> str:
        head, *tail = key.split("_")
        return head + "".join(part.title() for part in tail)
    return {name(key): value for key, value in row.items()}


def _rows(state: dict, table: str) -> list[dict]:
    _require(table in state["tables"], f"missing required table: {table}")
    return state["tables"][table]["rows"]


def _changed(before: dict, after: dict) -> set[str]:
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


def _timestamp(value: str) -> datetime:
    _require(isinstance(value, str), "timestamp must be an explicit string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Pinned SQLite models store naive UTC as well as RFC3339.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _assignment_timestamp(value: Any) -> str | None:
    """Pinned RFC3339 -> API naive_utc conversion, without losing nanoseconds."""
    if value is None:
        return None
    _require(isinstance(value, str), "assignment timestamp is not a string")
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?"
        r"(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?",
        value,
    )
    _require(match is not None, "assignment timestamp is not pinned UTC/RFC3339 format")
    seconds = datetime.fromisoformat(match[1] + (match[3] or "+00:00").replace("Z", "+00:00"))
    seconds = seconds.astimezone(timezone.utc)
    nanos = (match[2] or "").ljust(9, "0")
    return seconds.strftime("%Y-%m-%dT%H:%M:%S") + "." + nanos + "Z"


def _assignment_api_snapshot(rows: list[dict]) -> list[dict]:
    result = receipt_repair._assignment_snapshot(rows)
    for row in result:
        for field in ("createdAt", "updatedAt"):
            row[field] = _assignment_timestamp(row[field])
    return sorted(result, key=lambda row: json.dumps(row, sort_keys=True))


def _market_change(change: dict, before: dict, plan: dict) -> None:
    table, left, right = change["table"], change["before"], change["after"]
    _require(left is not None and right is not None, "market refresh cannot add or remove rows")
    fields = _changed(left, right)
    if table == "quote_sync_state":
        _require(left.get("data_source") in AUTOMATIC_PROVIDERS and
                 fields <= TIMESTAMP_FIELDS[table],
                 "quote sync state changed outside precise provider timestamps")
        return
    _require(left.get("source") in AUTOMATIC_PROVIDERS and
             fields <= {"created_at", "close", "adjclose", "volume"},
             "manual quote, unknown provider or unapproved quote field changed")
    if fields <= {"created_at"}:
        return
    automatic = [row for row in _rows(before, "quotes")
                 if row.get("asset_id") == left.get("asset_id")
                 and row.get("source") in AUTOMATIC_PROVIDERS]
    latest = max(row["day"] for row in automatic)
    generated = _timestamp(plan["generatedAt"]).date()
    _require(left["day"] == latest and
             generated - timedelta(days=7) <= date.fromisoformat(latest) <= generated,
             "historical/stale automatic quote price cannot change")
    _require(not left.get("notes"), "annotated automatic quote price cannot change")
    for field in fields & {"close", "adjclose", "volume"}:
        value = Decimal(str(right[field]))
        _require(value.is_finite() and value >= 0, "invalid refreshed market value")


def _derived_change(change: dict, plan: dict) -> None:
    table, left, right = change["table"], change["before"], change["after"]
    row = right or left
    currency = plan["operations"]["reconciliationUpdate"]["payload"]["currency"]
    _require(all(r is None or r.get("account_id") == plan["scope"]["ledgerAccountId"]
                 for r in (left, right)), "only scoped calculated account changes are supported")
    if table == "holdings_snapshots":
        _require(all(r is None or (
            r.get("source") == "CALCULATED" and r.get("positions") == "{}"
            and r.get("currency") == currency
        ) for r in (left, right)), "only scoped calculated cash snapshots are supported")
        known = {"id", "account_id", "snapshot_date", "currency", "positions",
                 "cash_balances", "cost_basis", "net_contribution", "calculated_at",
                 "net_contribution_base", "cash_total_account_currency",
                 "cash_total_base_currency", "source"}
        fixed = {"id", "account_id", "snapshot_date", "currency", "source", "positions"}
    else:
        _require(right is not None, "daily account valuations cannot be deleted")
        known = {
            "id", "account_id", "valuation_date", "account_currency", "base_currency",
            "fx_rate_to_base", "cash_balance", "investment_market_value", "total_value",
            "cost_basis", "net_contribution", "cash_balance_base", "investment_market_value_base",
            "total_value_base", "cost_basis_base", "net_contribution_base",
            "external_inflow_base", "external_outflow_base", "external_flow_source",
            "performance_eligible_value_base", "value_status", "basis_status", "calculated_at",
        }
        fixed = {"id", "account_id", "valuation_date", "account_currency", "base_currency"}
        for value in (left, right):
            if value is None:
                continue
            _require(value["account_currency"] == value["base_currency"] == currency and
                     Decimal(value["fx_rate_to_base"]) == 1 and
                     Decimal(value["investment_market_value"]) == 0 and
                     Decimal(value["investment_market_value_base"]) == 0 and
                     Decimal(value["total_value"]) == Decimal(value["cash_balance"]) ==
                     Decimal(value["cash_balance_base"]) == Decimal(value["total_value_base"]),
                     "daily valuation is not a same-currency cash calculation")
    _require(set(row) <= known, "unknown calculated account field")
    if left and right:
        _require(not _changed(left, right) & fixed, "calculated account row identity changed")


def environment_diff(before: dict, after: dict, plan: dict) -> list[dict]:
    """Observed startup effects; never activity, assignment, settings or sync changes."""
    changes = database_diff(before, after)
    for change in changes:
        if change["table"] in {"quotes", "quote_sync_state"}:
            _market_change(change, before, plan)
        elif change["table"] in {"holdings_snapshots", "daily_account_valuation"}:
            _derived_change(change, plan)
        else:
            raise PromotionError("restore/startup changed a preserved non-environment table")
    return changes


def build_timestamp_policy(*diffs: list[dict]) -> dict:
    """Draft a reviewable per-row policy from observed, fully retained diffs."""
    rows: dict[tuple[str, str], set[str]] = {}
    for changes in diffs:
        for change in changes:
            table = change["table"]
            left, right = change["before"], change["after"]
            if table not in TIMESTAMP_FIELDS or not right:
                continue
            fields = (_changed(left, right) if left else set(right)) & TIMESTAMP_FIELDS[table]
            if fields:
                rows.setdefault((table, change["key"]), set()).update(fields)
    return {"schemaVersion": 1, "kind": KIND + "-timestamp-policy", "rows": [
        {"table": table, "key": key, "fields": sorted(fields)}
        for (table, key), fields in sorted(rows.items())
    ]}


def verify_runtime_state(
    actual: dict, references: list[dict], plan: dict, policy: dict, *,
    started_at: datetime, checked_at: datetime,
) -> dict:
    """Financial values must match an exact reviewed reference; only named times vary."""
    _require(policy.get("schemaVersion") == 1 and policy.get("kind") == KIND + "-timestamp-policy",
             "invalid runtime timestamp policy")
    allowed = {(r["table"], r["key"]): set(r["fields"]) for r in policy["rows"]}
    _require(len(allowed) == len(policy["rows"]) and all(
        fields and fields <= TIMESTAMP_FIELDS.get(table, frozenset())
        for (table, _key), fields in allowed.items()
    ), "runtime policy contains an unapproved field or duplicate row")
    for reference in references:
        try:
            changes = environment_diff(reference, actual, plan)
            for change in changes:
                left, right = change["before"], change["after"]
                _require(left is not None and right is not None and
                         change["beforeRowId"] == change["afterRowId"],
                         "runtime refresh changed row presence/identity")
                fields = _changed(left, right)
                _require(fields <= allowed.get((change["table"], change["key"]), set()),
                         "runtime refresh changed data outside reviewed timestamp fields")
                for field in fields:
                    value = _timestamp(right[field])
                    _require(started_at - timedelta(seconds=2) <= value <=
                             checked_at + timedelta(seconds=2),
                             "runtime timestamp is outside this execution window")
                    if left[field] is not None:
                        _require(value >= _timestamp(left[field]), "runtime timestamp moved backwards")
            return {
                "referenceStateHash": reference["stateHash"],
                "actualStateHash": actual["stateHash"],
                "checkedAt": checked_at.isoformat(),
                "timestampDiff": changes, "timestampDiffHash": plan_fingerprint(changes),
            }
        except (PromotionError, ValueError, TypeError):
            continue
    raise PromotionError("post-start database differs from every exact reviewed reference")


def _sync_device_binding(state: dict) -> tuple[int, str | None]:
    rows = [row for row in state["tables"].get("sync_device_config", {}).get("rows", [])
            if row.get("trust_state") == "trusted"]
    def latest(values: list[dict]) -> dict | None:
        if not values:
            return None
        ordered = sorted(values, key=lambda row: row.get("last_bootstrap_at") or "", reverse=True)
        _require(len(ordered) == 1 or ordered[0].get("last_bootstrap_at") !=
                 ordered[1].get("last_bootstrap_at"), "ambiguous local sync device selection")
        return ordered[0]
    device = latest(rows)
    key = latest([row for row in rows if row.get("key_version") is not None])
    return max(1, key["key_version"]) if key else 1, device["device_id"] if device else None


def validate_bounded_diff(before: dict, after: dict, plan: dict) -> list[dict]:
    """No domain-wide exclusions, even for data generated by the application."""
    receipt_repair.validate_plan(plan)
    _require(plan["schemaVersion"] == 2, "legacy repair plans cannot be promoted")
    changes = database_diff(before, after)
    old_rows = [_camel(row) for row in _rows(before, "activities")]
    new_rows = [_camel(row) for row in _rows(after, "activities")]
    for rows, expected in ((old_rows, plan["preconditions"]), (new_rows, plan["expected"])):
        accounts = {plan["scope"]["ledgerAccountId"]}
        # Unrelated investment amounts can lose decimal precision in the REST
        # representation. Do not reimplement that conversion here: cash scope
        # hashes bind this repair, full raw SQLite diffs preserve every other
        # row, and authenticated restore proofs bind the global REST hashes.
        _require(len(rows) == expected["activityCount"] and
                 ledger_fingerprint(rows, accounts) == expected["accountLedgerFingerprint"],
                 "scoped SQLite ledger or complete count differs from the repair plan")
    old_by_id = {r["id"]: r for r in old_rows}
    new_by_id = {r["id"]: r for r in new_rows}
    continuation = plan.get("productionLineage")
    if continuation:
        prior_ids = {row["activityId"] for row in continuation["deleted"]}
        prior_tokens = {receipt_repair._provider_token(row["sourceIdentity"], "simplefin")
                        for row in continuation["deleted"]}
        _require(not prior_ids & (old_by_id.keys() | new_by_id.keys()) and not any(
            row["accountId"] == plan["scope"]["ledgerAccountId"] and
            str(row.get("idempotencyKey") or "").startswith("simplefin:") and
            receipt_repair._provider_token(row["idempotencyKey"], "simplefin") in prior_tokens
            for row in old_rows + new_rows
        ), "prior production source ID or alias reappeared")
    operations = plan["operations"]
    deletions = {r["sourceActivityId"] for r in operations["repairs"]}
    survivors = {r["survivorActivityId"] for r in operations["repairs"]}
    _require(len(deletions) == len(operations["repairs"]) and not deletions & survivors,
             "repair activity identities overlap")
    for repair in operations["repairs"]:
        for role in ("source", "survivor"):
            row = old_by_id.get(repair[f"{role}ActivityId"])
            _require(row is not None and activity_semantic_fingerprint(row) ==
                     repair[f"{role}ActivityFingerprint"], "activity precondition changed")
        source = old_by_id[repair["sourceActivityId"]]
        comment = repair["sourceRollbackPayload"].get("comment")
        _require(comment is None or isinstance(comment, str), "rollback comment is not text")
        _require((source.get("notes") or "") == (comment or "") and
                 not source.get("isUserModified") and
                 not source.get("sourceGroupId"), "deleted activity contains user-owned work")
        _require(activity_semantic_fingerprint(source) ==
                 activity_semantic_fingerprint(repair["sourceRollbackPayload"]),
                 "source rollback payload differs")
        _require(new_by_id.get(repair["survivorActivityId"]) ==
                 old_by_id[repair["survivorActivityId"]], "survivor fields changed")
    for name, table in before["tables"].items():
        if name in {"activities", "activity_taxonomy_assignments"}:
            continue
        if name.startswith("sync_"):
            continue
        _require(not any(row.get("activity_id") in deletions | survivors
                         for row in table["rows"]),
                 "selected activity has a dependent split/event/unknown linked row")

    assignments = {}
    for state, label in ((before, "before"), (after, "after")):
        rows = _rows(state, "activity_taxonomy_assignments")
        assignments[label] = rows
    snapshots = plan["preconditions"]["dependentAssignments"]
    for activity_id, snapshot in snapshots.items():
        actual = [_camel(r) for r in assignments["before"] if r["activity_id"] == activity_id]
        _require(_assignment_api_snapshot(actual) == _assignment_api_snapshot(snapshot),
                 "dependent assignment snapshot differs")
    upserts = {(op["activityId"], op["taxonomyId"]): op
               for op in operations["assignmentUpserts"]}
    _require(len(upserts) == len(operations["assignmentUpserts"]), "duplicate assignment upsert")
    for key, op in upserts.items():
        matching = [r for r in assignments["after"]
                    if (r["activity_id"], r["taxonomy_id"]) == key]
        _require(len(matching) == 1, "assignment upsert is missing or duplicated")
        row = matching[0]
        _require(row["category_id"] == op["categoryId"] and
                 row["source"] == op["expectedSource"] and
                 str(row["weight"]) == str(op["expectedWeight"]),
                 "assignment category, provenance or weight differs")

    recon = operations["reconciliationUpdate"]
    _require(activity_semantic_fingerprint(old_by_id[recon["activityId"]]) ==
             recon["beforeFingerprint"] and
             activity_semantic_fingerprint(new_by_id[recon["activityId"]]) ==
             activity_semantic_fingerprint(recon["payload"]), "compensation binding differs")
    effects = [
        sum((receipt_repair._signed(row) for row in rows
             if row["accountId"] == plan["scope"]["ledgerAccountId"]), Decimal())
        for rows in (old_rows, new_rows)
    ]
    _require(effects[0] == effects[1], "SQLite cash ledger effect is not conserved")
    if continuation:
        _require(effects[0] == Decimal(continuation["accountValue"]),
                 "continuation changed its receipt-proven cash value")
    activity_columns = {
        "id", "account_id", "asset_id", "activity_type", "activity_type_override", "source_type",
        "subtype", "status", "activity_date", "settlement_date", "quantity", "unit_price",
        "amount", "fee", "tax", "currency", "fx_rate", "notes", "metadata", "source_system",
        "source_record_id", "source_group_id", "idempotency_key", "import_run_id",
        "is_user_modified", "needs_review", "created_at", "updated_at",
    }
    assignment_columns = {
        "id", "activity_id", "taxonomy_id", "category_id", "weight", "source",
        "created_at", "updated_at",
    }
    journal_targets = {("activities", row): "delete" for row in deletions}
    journal_targets[("activities", recon["activityId"])] = "update"
    for change in changes:
        table, left, right = change["table"], change["before"], change["after"]
        row = right or left
        if table == "activities":
            if left and not right and left["id"] in deletions:
                _require(not any(v is not None for k, v in left.items()
                                 if k not in activity_columns),
                         "deleted activity has an unknown populated field")
                continue
            _require(left is not None and right is not None and
                     row["id"] == recon["activityId"] and
                     _changed(left, right) <= {"amount", "activity_type", "updated_at"},
                     "unplanned activity field change")
        elif table == "activity_taxonomy_assignments":
            if left and not right and left["activity_id"] in deletions:
                _require(set(left) <= assignment_columns,
                         "deleted assignment has unknown fields")
                journal_targets[(table, left["id"])] = "delete"
                continue
            _require(right is not None and
                     (right["activity_id"], right["taxonomy_id"]) in upserts,
                     "unplanned assignment loss or edit")
            if left:
                _require(_changed(left, right) <=
                         {"category_id", "weight", "source", "updated_at"},
                         "assignment identity or unknown field changed")
            else:
                _require(set(right) <= assignment_columns, "unknown new assignment field")
            journal_targets[(table, right["id"])] = "update" if left else "create"
        elif table in {"holdings_snapshots", "daily_account_valuation"}:
            _derived_change(change, plan)
        elif table in {"quotes", "quote_sync_state"}:
            _market_change(change, before, plan)
        elif table in {"sync_outbox", "sync_entity_metadata"}:
            # Validated after assignment targets have been collected.
            continue
        else:
            raise PromotionError(f"unrelated or unsupported table changed: {table}")

    new_events = {}
    for change in changes:
        if change["table"] != "sync_outbox":
            continue
        row = change["after"]
        _require(change["before"] is None and row is not None,
                 "existing synchronization history must be preserved")
        target = (SYNC_ENTITIES.get(row.get("entity")), row.get("entity_id"))
        event_key = (row.get("entity"), row.get("entity_id"))
        key_version, device_id = _sync_device_binding(before)
        _require(set(row) <= {
            "event_id", "entity", "entity_id", "op", "client_timestamp", "payload",
            "payload_key_version", "sent", "status", "retry_count", "next_retry_at",
            "last_error", "last_error_code", "device_id", "created_at",
        }, "unknown synchronization event field")
        _require(target in journal_targets and
                 row.get("op") == journal_targets[target] and
                 row.get("sent") == 0 and row.get("status") == "pending" and
                 row.get("retry_count") == 0 and
                 row.get("payload_key_version") == key_version and
                 row.get("device_id") == device_id and
                 all(row.get(k) is None for k in ("next_retry_at", "last_error", "last_error_code"))
                 and event_key not in new_events,
                 "sync journal event does not match a planned operation")
        new_events[event_key] = row
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError, KeyError) as exc:
            raise PromotionError("encrypted/unknown sync payload requires a dedicated contract") from exc
        if journal_targets[target] == "delete":
            _require(payload == {"id": target[1]}, "sync delete payload differs")
        else:
            # This receipt path emits native, normalized snake-case storage rows.
            # Do not substitute another writer's camelCase or UPDATE-on-insert contract.
            matching = [r for r in _rows(after, target[0]) if r["id"] == target[1]]
            _require(len(matching) == 1 and payload == matching[0], "sync row payload differs")
    metadata_changes = [c for c in changes if c["table"] == "sync_entity_metadata"]
    _require(len(metadata_changes) == len(new_events), "sync events lack exact metadata counterparts")
    for change in metadata_changes:
        left, right = change["before"], change["after"]
        _require(right is not None and set(right) == {
            "entity", "entity_id", "last_event_id", "last_client_timestamp", "last_op", "last_seq",
        }, "sync metadata shape or preservation differs")
        event = new_events.get((right["entity"], right["entity_id"]))
        _require(event is not None and right["last_event_id"] == event["event_id"] and
                 right["last_client_timestamp"] == event["client_timestamp"] and
                 right["last_op"] == event["op"] and
                 right["last_seq"] == (left["last_seq"] if left else 0) and
                 (not left or _changed(left, right) <=
                  {"last_event_id", "last_client_timestamp", "last_op"}),
                 "sync metadata is not the exact matching append event")
    return changes


def _origin(value: str, *, production: bool) -> str:
    parsed = urlparse(value)
    _require(
        parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
        and parsed.port is not None and not parsed.username and not parsed.password
        and not parsed.query and not parsed.fragment and parsed.path in {"", "/"},
        "target must be an explicit authenticated IPv4 loopback HTTP origin",
    )
    _require((parsed.port == 8088) == production, "incorrect production/restore port")
    return f"http://127.0.0.1:{parsed.port}"


def _host_mount(source: str) -> Path:
    match = re.fullmatch(r"/(?:host_mnt|run/desktop/mnt/host)/([a-zA-Z])/(.*)", source)
    if match:
        return Path(match[1] + ":\\" + match[2].replace("/", "\\")).resolve()
    return Path(source).resolve()


class GuardedDockerRuntime(DockerContainerRuntime):
    """Actual adapter: pinned container, loopback port, bind mount and no peer writer."""

    def __init__(self, target: dict, root: Path, password: str):
        super().__init__(project=target["composeProject"], service=target["composeService"],
                         container=target["containerName"])
        self.target, self.root, self.password = target, root, password

    def identity(self) -> dict:
        override = os.environ.get("DOCKER_HOST")
        if override:
            _require(override.startswith("npipe:////./pipe/"), "remote Docker host is prohibited")
        contexts = json.loads(self._run("context", "inspect"))
        _require(len(contexts) == 1 and str(
            contexts[0].get("Endpoints", {}).get("docker", {}).get("Host", "")
        ).startswith("npipe:////./pipe/"), "Docker context must address a local Windows pipe")
        return super().identity()

    def check(self, *, production: bool, running: bool | None = None) -> dict:
        target = self.target
        _origin(target["origin"], production=production)
        identity = self._require_same(target)
        if running is not None:
            _require(identity["running"] is running, "unexpected target runtime state")
        rows = json.loads(self._run("inspect", target["containerId"]))
        _require(len(rows) == 1 and rows[0].get("Id") == target["containerId"],
                 "Docker inspection target differs")
        row = rows[0]
        network_mode = str((row.get("HostConfig") or {}).get("NetworkMode") or "")
        _require(network_mode != "host" and not network_mode.startswith("container:"),
                 "shared container/host networking cannot bind the target origin")
        environment = (row.get("Config") or {}).get("Env") or []
        _require(any(isinstance(value, str) and
                     value.startswith("WF_AUTH_PASSWORD_HASH=") and
                     value.removeprefix("WF_AUTH_PASSWORD_HASH=") not in {"", "replace-me"}
                     for value in environment), "Docker authentication is not configured")
        _require(not (row.get("HostConfig") or {}).get("AutoRemove"),
                 "auto-remove containers cannot be promoted")
        _require(not (row.get("State") or {}).get("Restarting"), "container is restarting")
        db = PurePosixPath(target["containerDatabase"])
        _require(db.is_absolute() and ".." not in db.parts, "invalid container database path")
        live = private(target["database"], self.root)
        mounts = []
        for mount in row.get("Mounts", []):
            dest = PurePosixPath(mount["Destination"])
            if db == dest or dest in db.parents:
                mounts.append((mount, dest))
        _require(len(mounts) == 1, "database must have one unambiguous mount")
        mount, dest = mounts[0]
        _require(mount["Type"] == "bind" and mount.get("RW") is True and db != dest,
                 "promotion requires a writable directory bind mount")
        mapped = _host_mount(mount["Source"]).joinpath(*db.relative_to(dest).parts)
        _require(mapped.resolve() == live and not live.is_symlink(),
                 "Docker mount does not identify the authorized database path")
        bindings = (row.get("HostConfig") or {}).get("PortBindings") or {}
        expected_port = str(urlparse(target["origin"]).port)
        _require(bindings == {
            target["containerPort"]: [{"HostIp": "127.0.0.1", "HostPort": expected_port}]
        }, "Docker loopback port binding differs")
        peer_ids = self._run("ps", "-q").split()
        if peer_ids:
            for peer in json.loads(self._run("inspect", *peer_ids)):
                if peer["Id"] == target["containerId"]:
                    continue
                for peer_mount in peer.get("Mounts", []):
                    if peer_mount.get("Type") != "bind" or not peer_mount.get("RW"):
                        continue
                    source = _host_mount(peer_mount["Source"])
                    _require(source != live and source not in live.parents,
                             "another running container can write the database")
        return {key: identity[key] for key in IDENTITY_KEYS}

    def client(self) -> WealthfolioClient:
        client = WealthfolioClient(self.target["origin"], timeout=20, writer_data_dir=self.root)
        client.login(self.password)
        info = client.get("/app/info")
        _require(info.get("dbPath") == self.target["containerDatabase"],
                 "authenticated app database path differs")
        _require(instance_fingerprint(client, client.base) == self.target["instanceId"],
                 "authenticated app instance differs")
        _require(client.health(), "authenticated app is unhealthy")
        return client

    def verify(self, plan: dict, *, expected: str) -> dict:
        client = self.client()
        status, rows, _ = receipt_repair._target_status(client, SpendingAdapter(client), plan)
        _require(status == expected, "authenticated repair postcondition differs")
        return {"instanceId": self.target["instanceId"], "status": status,
                "ledgerHash": ledger_fingerprint(rows, {r["accountId"] for r in rows})}

    def wait(self, plan: dict, *, expected: str, timeout: int = 90) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.verify(plan, expected=expected)
                receipt_repair.wait_for_recalculation(
                    self.client(), [plan["scope"]["ledgerAccountId"]]
                )
                return self.verify(plan, expected=expected)
            except Exception:
                if time.monotonic() >= deadline:
                    raise PromotionError("authenticated startup/postcondition verification failed") from None
                time.sleep(2)

    def writer_state(self, tasks: list[dict]) -> dict:
        _require(bool(tasks) and all(set(t) == {"taskPath", "taskName"} for t in tasks),
                 "explicit legacy writer scheduled-task inventory is required")
        # Task identifiers are data on stdin, never interpolated PowerShell source.
        script = """
$ErrorActionPreference='Stop'
$items=[Console]::In.ReadToEnd() | ConvertFrom-Json
$result=@(foreach($item in $items) {
 $task=Get-ScheduledTask -TaskPath $item.taskPath -TaskName $item.taskName
 if($task.State -ne 'Disabled' -or $task.Settings.Enabled) { throw 'writer enabled' }
 [ordered]@{taskPath=$task.TaskPath;taskName=$task.TaskName;
  definition=(Export-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName)}
})
ConvertTo-Json -InputObject $result -Depth 10 -Compress
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=json.dumps(tasks), text=True, capture_output=True, timeout=30, check=True,
        )
        values = json.loads(result.stdout)
        _require(len(values) == len(tasks), "legacy task inventory is incomplete")
        return {"tasks": tasks, "definitionHash": plan_fingerprint(values)}


@contextmanager
def exclusive_database(path: Path):
    """Windows mandatory file sharing denial retained through the original rename."""
    _require(os.name == "nt", "production file fencing currently requires Windows")
    no_sidecars(path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    # DELETE sharing permits our rename; denying READ/WRITE rejects SQLite writers.
    handle = create(str(path), 0x80000000, 0x00000004, None, 3, 0x80, None)
    _require(handle != ctypes.c_void_p(-1).value,
             "database has another reader/writer or cannot be exclusively fenced")
    try:
        no_sidecars(path)
        read = kernel.ReadFile
        read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        read.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(1024 * 1024)
        count = ctypes.c_uint32()
        digest = hashlib.sha256()
        while True:
            _require(bool(read(handle, buffer, len(buffer), ctypes.byref(count), None)),
                     "cannot read fenced database")
            if not count.value:
                break
            digest.update(buffer.raw[:count.value])
        yield digest.hexdigest()
    finally:
        close(handle)


def backup_evidence(path: Path, root: Path) -> dict:
    no_sidecars(path)
    return verify_backup_file(path, data_dir=root, repo_root=REPO_ROOT)


def file_evidence(path: Path, root: Path) -> dict:
    path = private(path, root)
    return {"path": str(path.relative_to(root)), "sha256": _hash(path)}


def check_file(evidence: dict, root: Path) -> Path:
    path = private(evidence["path"], root)
    _require(_hash(path) == evidence["sha256"], "bound artifact bytes changed")
    return path


def required_review_hashes(plan: dict, canonical_evidence: dict | None = None) -> set[str]:
    """File-byte namespace only; operation sourceHashes also contain normalized-row hashes."""
    required = {
        plan["evidence"][name] for name in (
            "canonicalManifestSha256", "sourceApplicationPlanSha256",
            "sourceApplicationReceiptSha256",
        )
    }
    selected_intervals = set()
    for operation in plan["operations"]["repairs"]:
        hashes = operation.get("sourceHashes")
        _require(isinstance(hashes, list) and bool(hashes) and
                 all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                     for value in hashes), "repair lacks exact source evidence hashes")
        features = operation["featureVector"]
        selected_intervals.update(
            features[name] for name in ("authoritativeIntervalId", "suppressedIntervalId")
        )
    found = set()
    authority = plan["evidence"]["identityPolicyDocument"]["sourceAuthority"]
    for interval in authority["intervals"]:
        interval_id = content_hash(interval)
        if interval_id not in selected_intervals:
            continue
        found.add(interval_id)
        hashes = interval["evidence"].get("sourceHashes")
        _require(isinstance(hashes, list) and bool(hashes),
                 "selected authority interval lacks raw source evidence")
        required.update(hashes)
    _require(found == selected_intervals, "selected authority interval evidence is missing")
    if canonical_evidence is not None:
        required.update(canonical_evidence["rawArtifactHashes"])
    return required


def canonical_evidence_binding(
    *, root: Path, plan: dict, manifest_path: str | Path | None = None,
) -> dict:
    """Bind mixed evidence hashes to their original, explicit publication namespaces."""
    manifest_path = private(manifest_path or Path("normalized") / "canonical" / "manifest.json", root)
    _require(_hash(manifest_path) == plan["evidence"]["canonicalManifestSha256"],
             "original canonical manifest bytes differ")
    manifest = load(manifest_path)
    _require(manifest.get("schemaVersion") == 5 and isinstance(manifest.get("dataFiles"), dict)
             and isinstance(manifest.get("sourceFiles"), list),
             "canonical manifest lacks data/artifact namespaces")
    data_files = {}
    for name, digest in manifest["dataFiles"].items():
        _require(isinstance(name, str) and Path(name).name == name,
                 "canonical data file name escapes its publication")
        evidence = {"path": str(private(manifest_path.parent / name, root).relative_to(root)),
                    "sha256": digest}
        check_file(evidence, root)
        data_files[name] = evidence
    _require({"transaction-observations.json", "transaction-lineage.json"} <= data_files.keys(),
             "canonical observation and lineage data are required")
    observations = load(check_file(data_files["transaction-observations.json"], root))
    lineage = load(check_file(data_files["transaction-lineage.json"], root))
    scope = observations.get("identityScope")
    _require(scope is not None, "canonical identity scope is missing")
    validate_identity_scope(scope)
    identity = lineage.get("identityPolicy") or {}
    _require(
        identity.get("policyDocument") == plan["evidence"]["identityPolicyDocument"] and
        identity.get("policyHash") == plan["evidence"]["identityPolicyHash"] and
        identity.get("generationHash") == plan["evidence"]["canonicalGenerationHash"] and
        identity.get("canonicalStateHash") == plan["evidence"]["canonicalStateHash"] and
        lineage.get("baselinePublicationId") == plan["evidence"]["baselinePublicationId"] and
        lineage.get("forensicPublicationId") == plan["evidence"]["forensicPublicationId"],
        "original canonical generation/policy binding differs",
    )
    sources = {}
    for source in manifest["sourceFiles"]:
        _require(isinstance(source, dict) and isinstance(source.get("path"), str) and
                 isinstance(source.get("sha256"), str), "invalid canonical source artifact declaration")
        path = private(source["path"], root)
        _require(path not in sources or sources[path]["sha256"] == source["sha256"],
                 "conflicting canonical source artifact declarations")
        sources[path] = {"path": str(path.relative_to(root)), "sha256": source["sha256"]}
    publication_files = {
        plan["evidence"][name] for name in (
            "canonicalManifestSha256", "sourceApplicationPlanSha256", "sourceApplicationReceiptSha256"
        )
    }
    declared_raw = {row["sha256"] for row in sources.values()} | (
        required_review_hashes(plan) - publication_files
    )
    row_index: dict[str, list[int]] = {}
    for index, row in enumerate(scope["rows"]):
        row_index.setdefault(content_hash(row), []).append(index)
    def index_records(records: list[dict], field: str) -> dict:
        indexed = {row[field]: row for row in records}
        _require(len(indexed) == len(records), "ambiguous canonical evidence identity")
        return indexed
    record_index = index_records(observations["observations"], "observationId")
    decision_index = index_records(lineage["decisionProjections"], "decisionId")
    transaction_index = index_records(lineage["canonicalTransactions"], "canonicalTransactionId")
    normalized, artifacts, bindings = set(), set(), []
    for operation in plan["operations"]["repairs"]:
        decision = decision_index.get(operation["decisionId"])
        transaction = transaction_index.get(operation["canonicalTransactionId"])
        _require(decision is not None and transaction is not None and
                 decision.get("decisionHash") == operation["decisionHash"] and
                 decision.get("sourceHashes") == operation["sourceHashes"] and
                 decision.get("featureVector") == operation["featureVector"] and
                 decision.get("competingCandidateProof") == operation["competingCandidateProof"] and
                 decision.get("sourceAuthorityPolicyHash") == operation["authorityPolicyHash"] and
                 operation["canonicalTransactionId"] in decision.get("canonicalTransactionIds", []) and
                 transaction.get("activeObservationId") == operation["survivorObservationId"],
                 "repair operation differs from its original canonical decision")
        for role in ("source", "survivor"):
            observation_id = operation[role + "ObservationId"]
            record = record_index.get(observation_id)
            _require(record is not None and
                     observation_id in transaction.get("memberObservationIds", []) and
                     record.get("canonicalTransactionId") == operation["canonicalTransactionId"] and
                     record.get("observationFingerprint") == stable_hash(record.get("transaction")),
                     "repair observation differs from the canonical publication")
        for digest in operation["sourceHashes"]:
            _require(digest in row_index or digest in declared_raw,
                     "operation source hash has no declared raw or normalized-row namespace")
            if digest in row_index:
                normalized.add(digest)
            if digest in declared_raw:
                artifacts.add(digest)
        bindings.append({
            "decisionId": operation["decisionId"], "decisionHash": operation["decisionHash"],
            "sourceObservationId": operation["sourceObservationId"],
            "survivorObservationId": operation["survivorObservationId"],
        })
    selected_rows, selected_files = [], {}
    for digest in sorted(normalized):
        indexes = row_index[digest]
        row = scope["rows"][indexes[0]]
        _require(bool(row.get("source_file")), "normalized evidence row lacks an artifact path")
        artifact = sources.get(private(row["source_file"], root))
        _require(artifact is not None, "normalized evidence artifact is not declared by its manifest")
        if artifact["path"] not in selected_files:
            check_file(artifact, root)
        artifacts.add(artifact["sha256"])
        selected_files[artifact["path"]] = artifact
        selected_rows.append({"rowHash": digest, "scopeIndexes": indexes, "row": row})
    return {
        "kind": KIND + "-canonical-evidence", "manifest": file_evidence(manifest_path, root),
        "dataFiles": data_files, "scopeHash": scope["scopeHash"], "scopeRowCount": scope["rowCount"],
        "normalizedRowHashes": sorted(normalized), "normalizedRows": selected_rows,
        "rawArtifactHashes": sorted(artifacts),
        "rowArtifacts": [selected_files[name] for name in sorted(selected_files)],
        "operationBindings": bindings,
    }


def restore_proof(
    *, root: Path, backup: Path, plan: dict, runtime: GuardedDockerRuntime,
    evidence_key: bytes, expected: str,
) -> dict:
    """Really restore/start/authenticate/stop an isolated, initially empty clone."""
    _require(expected in {"ready", "applied"}, "invalid restore expected state")
    receipt_repair.validate_plan(plan)
    _require(plan["schemaVersion"] == 2, "legacy plan cannot create a promotion restore proof")
    runtime.check(production=False, running=False)
    target = runtime.target
    destination = private(target["database"], root)
    _require(not destination.exists(), "restore clone must use a fresh empty database slot")
    evidence = backup_evidence(backup, root)
    original_state = sqlite_state(backup, stopped=True)
    stage = stage_restore_copy(backup, destination, data_dir=root, repo_root=REPO_ROOT)
    try:
        runtime.start(target)
        api = runtime.wait(plan, expected=expected)
    finally:
        runtime.stop(target)
    runtime.check(production=False, running=False)
    checkpoint = checkpoint_stopped_database(runtime, production=False)
    restored_state = sqlite_state(destination, stopped=True)
    refresh = environment_diff(original_state, restored_state, plan)
    return seal({
        "kind": KIND + "-restore", "schemaVersion": 1,
        "planHash": plan["planHash"], "backup": evidence, "target": target,
        "stage": stage, "stateHash": restored_state["stateHash"], "api": api,
        "backupStateHash": original_state["stateHash"],
        "environmentDiff": refresh, "environmentDiffHash": plan_fingerprint(refresh),
        "checkpoint": checkpoint,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
    }, evidence_key)


def build_review_inputs(*, root: Path, request: dict, evidence_key: bytes,
                        operator_key: bytes | None = None) -> dict:
    """Offline review material, not an approval; no live file, Docker or scheduler reads."""
    plan = receipt_repair.load_plan(private(request["plan"], root))
    _require(plan["schemaVersion"] == 2, "legacy plan cannot be reviewed for promotion")
    if plan.get("productionLineage"):
        from .repair_lineage import verify_plan_lineage
        verify_plan_lineage(plan, root, evidence_key=evidence_key, operator_key=operator_key)
    canonical = canonical_evidence_binding(
        root=root, plan=plan, manifest_path=request.get("canonicalManifest")
    )
    live = private(request["target"]["database"], root)
    states, proofs, refreshes = {}, {}, {}
    for name in ("original", "candidate"):
        backup_path = private(request[name + "Backup"], root)
        _require(backup_path != live, "offline review cannot read the live database")
        backup = backup_evidence(backup_path, root)
        states[name] = sqlite_state(backup_path, stopped=True)
        proof = unseal(load(private(request[name + "RestoreProof"], root)),
                       evidence_key, KIND + "-restore")
        restored_path = private(proof["target"]["database"], root)
        _require(restored_path != live, "offline review cannot read a live restore reference")
        restored = sqlite_state(restored_path, stopped=True)
        _require(proof["planHash"] == plan["planHash"] and proof["backup"] == backup and
                 proof["backupStateHash"] == states[name]["stateHash"] and
                 proof["stateHash"] == restored["stateHash"], "review restore reference differs")
        verify_stage_restore_report(
            proof["stage"], backup=backup, restored_database=restored_path,
            data_dir=root, repo_root=REPO_ROOT,
        )
        refreshes[name] = environment_diff(states[name], restored, plan)
        _require(refreshes[name] == proof["environmentDiff"] and
                 plan_fingerprint(refreshes[name]) == proof["environmentDiffHash"],
                 "review restore diff differs")
        proofs[name] = proof
    recovery = load(private(request["recovery"], root))
    _require(private(recovery["backup"]["path"], root) != live,
             "offline review cannot read the live recovery database")
    receipt_repair._validate_applied_receipt(
        load(private(request["receipt"], root)), plan,
        expected_instance_id=request["appliedCloneInstanceId"], data_dir=root, recovery=recovery,
    )
    prestate_diff = environment_diff(
        states["original"], sqlite_state(private(recovery["backup"]["path"], root), stopped=True), plan
    )
    diff = validate_bounded_diff(states["original"], states["candidate"], plan)
    policy = build_timestamp_policy(diff, prestate_diff, *refreshes.values())
    return {
        "kind": KIND + "-review-inputs", "schemaVersion": 1,
        "planHash": plan["planHash"], "evidenceHash": plan_fingerprint(plan["evidence"]),
        "diffHash": plan_fingerprint(diff), "diff": diff,
        "clonePrestateDiffHash": plan_fingerprint(prestate_diff), "clonePrestateDiff": prestate_diff,
        "restoreDiffHashes": {name: proof["environmentDiffHash"] for name, proof in proofs.items()},
        "restoreDiffs": refreshes, "runtimeTimestampPolicy": policy,
        "runtimePolicyHash": plan_fingerprint(policy), "releaseRevision": request["releaseRevision"],
        "targetHash": plan_fingerprint(request["target"]),
        "canonicalEvidence": canonical, "canonicalEvidenceHash": plan_fingerprint(canonical),
        "normalizedRowHashes": canonical["normalizedRowHashes"],
        "requiredEvidenceSha256": sorted(required_review_hashes(plan, canonical)),
        **replay_review_fields(plan),
    }


def prepare(
    *, root: Path, request: dict, runtime: GuardedDockerRuntime, evidence_key: bytes,
    operator_key: bytes | None = None,
) -> dict:
    """Read-only production preparation. Never stop production or create a live backup."""
    plan_path = private(request["plan"], root)
    plan = receipt_repair.load_plan(plan_path)
    _require(plan["schemaVersion"] == 2, "legacy plan cannot be promoted")
    from .repair_lineage import require_current_head, verify_plan_lineage
    chain = verify_plan_lineage(plan, root, evidence_key=evidence_key, operator_key=operator_key)
    require_current_head(plan, root, request["target"], evidence_key)
    if chain:
        _require(chain["target"] == request["target"], "continuation production target changed")
    canonical = canonical_evidence_binding(
        root=root, plan=plan, manifest_path=request.get("canonicalManifest")
    )
    _require(request["releaseRevision"] == release_revision(), "release revision changed")
    target = request["target"]
    _require(runtime.target == target, "runtime target differs from request")
    runtime.check(production=True, running=True)
    api = runtime.verify(plan, expected="ready")
    original = private(request["originalBackup"], root)
    candidate = private(request["candidateBackup"], root)
    live = private(target["database"], root)
    _require(len({original, candidate, live}) == 3, "backup and live paths must be distinct")
    backups = {name: backup_evidence(path, root)
               for name, path in (("original", original), ("candidate", candidate))}
    before, after = sqlite_state(original, stopped=True), sqlite_state(candidate, stopped=True)
    _require(sqlite_state(live)["stateHash"] == before["stateHash"], "production prestate drifted")
    diff = validate_bounded_diff(before, after, plan)
    receipt_path = private(request["receipt"], root)
    recovery_path = private(request["recovery"], root)
    receipt, recovery = load(receipt_path), load(recovery_path)
    receipt_repair._validate_applied_receipt(
        receipt, plan, expected_instance_id=request["appliedCloneInstanceId"],
        data_dir=root, recovery=recovery,
    )
    original_effect = sum((
        receipt_repair._signed(_camel(row))
        for row in _rows(before, "activities")
        if row["account_id"] == plan["scope"]["ledgerAccountId"]
    ), Decimal())
    _require(receipt.get("preLedgerFingerprint") ==
             plan["preconditions"]["globalLedgerFingerprint"] and
             receipt["accountValueBasis"] == "cash-ledger" and
             Decimal(receipt["accountValueBefore"]) == original_effect,
             "receipt account value/prestate is not backed by the original cash ledger")
    recovery_backup = private(recovery["backup"]["path"], root)
    prestate_diff = environment_diff(before, sqlite_state(recovery_backup, stopped=True), plan)
    proofs, files = {}, {}
    for name in ("original", "candidate"):
        proof_path = private(request[name + "RestoreProof"], root)
        document = load(proof_path)
        proof = unseal(document, evidence_key, KIND + "-restore")
        restored = private(proof["target"]["database"], root)
        expected_state = before if name == "original" else after
        _require(proof["backup"] == backups[name] and proof["planHash"] == plan["planHash"]
                 and proof["backupStateHash"] == expected_state["stateHash"]
                 and proof["target"]["imageId"] == target["imageId"]
                 and proof["target"]["containerId"] != target["containerId"]
                 and proof["target"]["instanceId"] != target["instanceId"]
                 and restored not in {live, original, candidate}
                 and proof["api"]["status"] == ("ready" if name == "original" else "applied")
                 and proof["api"]["instanceId"] == proof["target"]["instanceId"]
                 and proof["api"]["ledgerHash"] ==
                 plan["preconditions" if name == "original" else "expected"]["globalLedgerFingerprint"],
                 "restore proof is not bound to this backup, plan and image")
        _origin(proof["target"]["origin"], production=False)
        verify_stage_restore_report(
            proof["stage"], backup=backups[name], restored_database=restored,
            data_dir=root, repo_root=REPO_ROOT,
        )
        restored_state = sqlite_state(restored, stopped=True)
        _require(restored_state["stateHash"] == proof["stateHash"],
                 "restored proof database changed or is not stopped")
        actual_refresh = environment_diff(expected_state, restored_state, plan)
        _require(actual_refresh == proof["environmentDiff"] and
                 plan_fingerprint(actual_refresh) == proof["environmentDiffHash"],
                 "restore's exact environmental diff is missing or changed")
        proofs[name] = document
        files[name + "RestoreProof"] = file_evidence(proof_path, root)
    review_path = private(request["review"], root)
    review = unseal(load(review_path), evidence_key, KIND + "-review")
    timestamp_policy = build_timestamp_policy(
        diff, prestate_diff, *(proof["environmentDiff"] for proof in proofs.values())
    )
    restore_hashes = {name: proof["environmentDiffHash"] for name, proof in proofs.items()}
    _require(review["planHash"] == plan["planHash"] and
             review["diffHash"] == plan_fingerprint(diff) and
             review["evidenceHash"] == plan_fingerprint(plan["evidence"]) and
             review["releaseRevision"] == request["releaseRevision"] and
             review["targetHash"] == plan_fingerprint(target) and
             review.get("clonePrestateDiffHash") == plan_fingerprint(prestate_diff) and
             review.get("restoreDiffHashes") == restore_hashes and
             review.get("runtimePolicyHash") == plan_fingerprint(timestamp_policy) and
             review.get("canonicalEvidenceHash") == plan_fingerprint(canonical) and
             review.get("independentEvidence"), "independent review binding differs")
    _require(all(review.get(key) == value for key, value in replay_review_fields(plan).items()),
             "review does not explicitly acknowledge the historical matching replay gap")
    for evidence in review["independentEvidence"]:
        check_file(evidence, root)
    required_sources = required_review_hashes(plan, canonical)
    _require(required_sources <= {e["sha256"] for e in review["independentEvidence"]},
             "review must bind the canonical manifest, source plan/receipt and selected raw sources")
    writers = runtime.writer_state(review["legacyWriterTasks"])
    _require(writers == review["legacyWriterState"], "legacy writer disabled evidence changed")
    for name, path in (("plan", plan_path), ("receipt", receipt_path),
                       ("recovery", recovery_path), ("review", review_path)):
        files[name] = file_evidence(path, root)
    if plan.get("productionLineage"):
        files["productionLineage"] = plan["productionLineage"]["file"]
    return seal({
        "kind": KIND + "-preparation", "schemaVersion": 1,
        "request": request, "planHash": plan["planHash"],
        "evidenceHash": plan_fingerprint(plan["evidence"]),
        "releaseRevision": request["releaseRevision"], "target": target,
        "targetHash": plan_fingerprint(target), "backups": backups,
        "originalStateHash": before["stateHash"], "candidateStateHash": after["stateHash"],
        "diff": diff, "diffHash": plan_fingerprint(diff), "files": files,
        "clonePrestateDiff": prestate_diff, "clonePrestateDiffHash": plan_fingerprint(prestate_diff),
        "runtimeTimestampPolicy": timestamp_policy,
        "canonicalEvidence": canonical, "canonicalEvidenceHash": plan_fingerprint(canonical),
        "restoreProofs": proofs, "legacyWriterState": writers, "api": api,
        "preservation": "all-tables-all-columns-ddl-no-domain-exclusions",
        "writerActivation": False,
        **replay_review_fields(plan),
    }, evidence_key)


def inspect_preparation(document: dict, *, root: Path, evidence_key: bytes) -> dict:
    preparation = unseal(document, evidence_key, KIND + "-preparation")
    _require(preparation["schemaVersion"] == 1 and preparation["writerActivation"] is False,
             "unsupported preparation contract")
    for file in preparation["files"].values():
        check_file(file, root)
    for backup in preparation["backups"].values():
        path = private(backup["path"], root)
        _require(backup_evidence(path, root) == backup, "backup bytes changed")
    _require(preparation["diffHash"] == plan_fingerprint(preparation["diff"]),
             "prepared diff changed")
    plan = receipt_repair.load_plan(check_file(preparation["files"]["plan"], root))
    _require(plan["schemaVersion"] == 2 and plan["planHash"] == preparation["planHash"],
             "prepared plan is legacy or changed")
    canonical = canonical_evidence_binding(
        root=root, plan=plan, manifest_path=preparation["canonicalEvidence"]["manifest"]["path"]
    )
    _require(canonical == preparation["canonicalEvidence"] and
             plan_fingerprint(canonical) == preparation["canonicalEvidenceHash"],
             "canonical evidence namespace binding changed")
    for name, document in preparation["restoreProofs"].items():
        proof = unseal(document, evidence_key, KIND + "-restore")
        _require(sqlite_state(private(proof["target"]["database"], root), stopped=True)["stateHash"]
                 == proof["stateHash"] and proof["backupStateHash"] == preparation[name + "StateHash"],
                 "restored proof database changed")
    review = unseal(load(check_file(preparation["files"]["review"], root)),
                    evidence_key, KIND + "-review")
    for evidence in review["independentEvidence"]:
        check_file(evidence, root)
    return preparation


def validate_authorization(
    document: dict, preparation: dict, preparation_id: str, *, operator_key: bytes,
    now: datetime | None = None, rollback_only: bool = False,
) -> None:
    body = unseal(document, operator_key, KIND + "-authorization")
    expected = {
        "preparationId": preparation_id, "planHash": preparation["planHash"],
        "releaseRevision": preparation["releaseRevision"], "targetHash": preparation["targetHash"],
        "diffHash": preparation["diffHash"], "approval": APPROVAL,
        "writeBoundary": "NO_APP_OR_EXTERNAL_WRITES_UNTIL_TERMINAL_RECEIPT",
    }
    _require(all(body.get(k) == v for k, v in expected.items()),
             "authorization does not bind this exact plan, release, diff and target")
    _require(isinstance(body.get("operator"), str) and bool(body["operator"].strip()),
             "authorization operator is required")
    now = now or datetime.now(timezone.utc)
    issued, expires = (datetime.fromisoformat(body[k]) for k in ("issuedAt", "expiresAt"))
    _require(issued.tzinfo is not None and expires.tzinfo is not None and
             (rollback_only or issued <= now < expires)
             and 0 < (expires - issued).total_seconds() <= 1800,
             "authorization is expired, future-dated or exceeds 30 minutes")


def _rename(source: Path, target: Path) -> None:
    _require(not target.exists(), "exact rename slot already exists")
    os.rename(source, target)
    fsync_directory(source.parent)
    if target.parent != source.parent:
        fsync_directory(target.parent)


def _slots(root: Path, target: dict) -> dict[str, Path]:
    live = private(target["database"], root)
    return {"root": root, "live": live, **{
        key: live.with_name(live.name + ".bounded-" + suffix)
        for key, suffix in (
            ("incoming", "incoming"), ("original", "original"),
            ("rejected", "rejected"), ("journal", "journal.json"),
            ("lock", "lock"),
        )
    }}


def _runtime_references(preparation: dict, root: Path, name: str) -> list[dict]:
    original = sqlite_state(check_file(preparation["backups"][name], root), stopped=True)
    proof = preparation["restoreProofs"][name]
    restored = sqlite_state(private(proof["target"]["database"], root), stopped=True)
    _require(original["stateHash"] == preparation[name + "StateHash"] and
             restored["stateHash"] == proof["stateHash"],
             "runtime reference database changed")
    return [original, restored]


@contextmanager
def _execution_lock(path: Path):
    """OS-released ownership; the lock file is durable and never unlinked on release."""
    _require(os.name == "nt", "production execution currently requires Windows")
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise PromotionError("another promotion/recovery owns this database") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _transition(slots: dict, state: dict, phase: str, key: bytes, **evidence: Any) -> None:
    _require(phase in EXECUTION_PHASES, "unknown execution phase")
    state.update(phase=phase, **evidence)
    state["history"].append({"phase": phase, "at": datetime.now(timezone.utc).isoformat()})
    document = seal(dict(state), key)
    if slots["journal"].exists():
        _write_cutover_state(slots["journal"], document, create=False)
    else:
        write(slots["journal"], document)


def _protect_rollback_state(
    slots: dict, state: dict, plan: dict, *, reference: str, boundary: str, stopped: bool,
) -> tuple[dict, str | None]:
    actual = None
    try:
        live = slots["live"]
        physical_hash = _hash(live) if stopped else None
        actual = sqlite_state(live, stopped=stopped)
        phases = (
            {"candidate-install-intent", "candidate-installed"}
            if reference == "candidate" else {"original-restored"}
        )
        times = [entry["at"] for entry in state["history"] if entry["phase"] in phases]
        _require(bool(times), "rollback reference has no durable installation history")
        verification = verify_runtime_state(
            actual, _runtime_references(state["preparation"], slots["root"], reference),
            plan, state["preparation"]["runtimeTimestampPolicy"],
            started_at=_timestamp(min(times)), checked_at=datetime.now(timezone.utc),
        )
        if stopped:
            _require(_hash(live) == physical_hash, "database changed during rollback protection read")
        return verification, physical_hash
    except Exception as exc:
        raise ManualRecoveryRequired(
            boundary, actual["stateHash"] if actual is not None else None,
        ) from exc


def _record_manual_recovery(slots: dict, state: dict, key: bytes, error: ManualRecoveryRequired) -> None:
    _transition(
        slots, state, "manual-recovery-required", key,
        manualRecovery={
            "boundary": error.boundary, "observedStateHash": error.observed_state_hash,
            "reason": "current-state-not-proven-safe-to-discard",
        },
    )


def _rollback(
    slots: dict, state: dict, runtime: GuardedDockerRuntime, plan: dict, key: bytes,
) -> None:
    target = runtime.target
    prior_restores = [entry["at"] for entry in state["history"]
                      if entry["phase"] == "original-restored"]
    resumed_reference = None
    if state["phase"] in {"stop-intent", "original-move-intent", "stale-prestate"} and \
            not slots["original"].exists() and slots["incoming"].exists():
        state["freshnessRejected"] = True
    _transition(slots, state, "rolling-back", key)
    runtime.check(production=True)
    original, live = slots["original"], slots["live"]
    # Disabling scheduled tasks does not fence interactive clients. Check while
    # still serving, then check again after stop and under the final file fence.
    reference = (
        "candidate" if original.exists() else
        "original" if prior_restores and not state.get("freshnessRejected") else None
    )
    if reference is not None and live.exists():
        verification, _ = _protect_rollback_state(
            slots, state, plan, reference=reference, boundary="before-stop", stopped=False,
        )
        _transition(slots, state, "rolling-back", key, rollbackPrecheck=verification)
    runtime.stop(target)
    runtime.check(production=True, running=False)
    if live.exists():
        try:
            checkpoint = checkpoint_stopped_database(runtime, production=True)
        except (PromotionError, OSError) as exc:
            raise ManualRecoveryRequired("rollback-checkpoint") from exc
        _transition(slots, state, "rolling-back", key, rollbackCheckpoint=checkpoint)
    if original.exists():
        _require(_hash(original) == state["stoppedOriginalSha256"],
                 "rollback original bytes changed; manual recovery required")
        if live.exists():
            verification, physical_hash = _protect_rollback_state(
                slots, state, plan, reference="candidate", boundary="after-stop", stopped=True,
            )
            _transition(slots, state, "rolling-back", key, rollbackStoppedCheck=verification)
            try:
                with exclusive_database(live) as digest:
                    if digest != physical_hash:
                        raise ManualRecoveryRequired("file-fence")
                    _rename(live, slots["rejected"])
            except PromotionError as exc:
                raise ManualRecoveryRequired("file-fence", verification["actualStateHash"]) from exc
        with exclusive_database(original) as digest:
            _require(digest == state["stoppedOriginalSha256"], "fenced rollback bytes changed")
            _rename(original, live)
    else:
        _require(live.is_file(), "original database is missing; manual recovery required")
        # An interrupted rollback may already have moved the original back.
        if state.get("stoppedOriginalSha256") and not state.get("freshnessRejected"):
            if _hash(live) != state["stoppedOriginalSha256"]:
                _require(bool(prior_restores), "cannot identify original database for recovery")
                resumed_reference = sqlite_state(live, stopped=True)
                state["resumedOriginalVerification"] = verify_runtime_state(
                    resumed_reference,
                    _runtime_references(state["preparation"], slots["root"], "original"),
                    plan, state["preparation"]["runtimeTimestampPolicy"],
                    started_at=_timestamp(min(prior_restores)),
                    checked_at=datetime.now(timezone.utc),
                )
        else:
            state["freshnessRejected"] = True
    _transition(slots, state, "original-restored", key)
    started_at = datetime.now(timezone.utc)
    runtime.start(target)
    # Drift detected before the swap is legitimate user work, not the old plan.
    if state.get("freshnessRejected"):
        runtime.client()
    else:
        runtime.wait(plan, expected="ready")
        references = _runtime_references(state["preparation"], slots["root"], "original")
        if resumed_reference is not None:
            references.append(resumed_reference)
        verification = verify_runtime_state(
            sqlite_state(live), references,
            plan, state["preparation"]["runtimeTimestampPolicy"],
            started_at=started_at, checked_at=datetime.now(timezone.utc),
        )
        state["rollbackVerification"] = verification
    _transition(slots, state, "rolled-back", key)


def execute(
    *, root: Path, document: dict, authorization: dict, supplied_preparation_id: str,
    runtime: GuardedDockerRuntime, evidence_key: bytes, operator_key: bytes,
) -> dict:
    preparation = inspect_preparation(document, root=root, evidence_key=evidence_key)
    _require(document["documentHash"] == supplied_preparation_id,
             "operator-supplied preparation ID differs")
    validate_authorization(authorization, preparation, supplied_preparation_id,
                           operator_key=operator_key)
    _require(runtime.target == preparation["target"], "runtime target differs")
    _require(release_revision() == preparation["releaseRevision"], "release revision changed")
    # Rebuild all gates from the immutable request before acquiring mutation ownership.
    fresh = prepare(root=root, request=preparation["request"], runtime=runtime,
                    evidence_key=evidence_key, operator_key=operator_key)
    _require(fresh == document, "preparation evidence changed")
    plan = receipt_repair.load_plan(check_file(preparation["files"]["plan"], root))
    slots = _slots(root, runtime.target)
    with _execution_lock(slots["lock"]):
        from .repair_lineage import require_current_head
        require_current_head(plan, root, runtime.target, evidence_key)
        _require(not any(slots[k].exists() for k in ("journal", "incoming", "original", "rejected")),
                 "existing execution state requires inspect/recovery, not re-execution")
        state = {
            "kind": KIND + "-execution", "schemaVersion": 1,
            "preparationId": supplied_preparation_id,
            "preparation": document, "authorization": authorization,
            "target": runtime.target, "history": [],
            "previousExecutionHash": (plan.get("productionLineage") or {}).get("headExecutionHash"),
        }
        _transition(slots, state, "authorized", evidence_key)
        touched = False
        try:
            candidate = check_file(preparation["backups"]["candidate"], root)
            _copy_verified(candidate, slots["incoming"], preparation["backups"]["candidate"]["sha256"])
            _transition(slots, state, "candidate-staged", evidence_key)
            _require(inspect_preparation(document, root=root, evidence_key=evidence_key) == preparation,
                     "bound inputs changed while staging the candidate")
            if plan.get("productionLineage"):
                from .repair_lineage import verify_plan_lineage
                verify_plan_lineage(plan, root, evidence_key=evidence_key, operator_key=operator_key)
            validate_authorization(authorization, preparation, supplied_preparation_id,
                                   operator_key=operator_key)
            runtime.check(production=True, running=True)
            _require(runtime.writer_state(preparation["legacyWriterState"]["tasks"]) ==
                     preparation["legacyWriterState"], "legacy writer state changed")
            # Durable intent BEFORE stopping; recovery never guesses that this succeeded.
            _transition(slots, state, "stop-intent", evidence_key)
            touched = True
            runtime.stop(runtime.target)
            runtime.check(production=True, running=False)
            checkpoint = checkpoint_stopped_database(runtime, production=True)
            _transition(slots, state, "stop-intent", evidence_key, stoppedCheckpoint=checkpoint)
            stopped_sha = _hash(slots["live"])
            stopped = sqlite_state(slots["live"], stopped=True)
            if stopped["stateHash"] != preparation["originalStateHash"] or \
                    _hash(slots["live"]) != stopped_sha:
                _transition(slots, state, "stale-prestate", evidence_key, freshnessRejected=True)
                raise PromotionError("stopped production state changed; stale clone rejected")
            validate_authorization(authorization, preparation, supplied_preparation_id,
                                   operator_key=operator_key)
            _transition(slots, state, "original-move-intent", evidence_key,
                        stoppedOriginalSha256=stopped_sha)
            with exclusive_database(slots["live"]) as original_digest, \
                    exclusive_database(slots["incoming"]) as candidate_digest:
                # Mandatory file fence closes the SQLite-close/rename writer window.
                if original_digest != stopped_sha:
                    _transition(slots, state, "stale-prestate", evidence_key,
                                freshnessRejected=True)
                    raise PromotionError("production changed before file fence")
                _require(candidate_digest == preparation["backups"]["candidate"]["sha256"],
                         "staged candidate bytes changed")
                _rename(slots["live"], slots["original"])
                _transition(slots, state, "candidate-install-intent", evidence_key)
                _rename(slots["incoming"], slots["live"])
            _transition(slots, state, "candidate-installed", evidence_key)
            started_at = datetime.now(timezone.utc)
            runtime.start(runtime.target)
            api = runtime.wait(plan, expected="applied")
            verification = verify_runtime_state(
                sqlite_state(slots["live"]), _runtime_references(preparation, root, "candidate"),
                plan, preparation["runtimeTimestampPolicy"],
                started_at=started_at, checked_at=datetime.now(timezone.utc),
            )
            _require(runtime.writer_state(preparation["legacyWriterState"]["tasks"]) ==
                     preparation["legacyWriterState"], "legacy writer state changed after startup")
            runtime.check(production=True, running=True)
            _transition(slots, state, "completed", evidence_key, postVerification=api,
                        databaseVerification=verification)
        except Exception as exc:
            if touched:
                try:
                    _rollback(slots, state, runtime, plan, evidence_key)
                except ManualRecoveryRequired as protection:
                    _record_manual_recovery(slots, state, evidence_key, protection)
                    raise protection from exc
                except Exception:
                    _transition(slots, state, "recovery-required", evidence_key)
                    raise PromotionError("promotion failed; explicit recovery is required") from exc
            else:
                _transition(slots, state, "aborted", evidence_key)
            raise PromotionError("promotion failed; original preserved; inspect durable journal") from exc
    return load(slots["journal"])


def recover(
    *, root: Path, runtime: GuardedDockerRuntime, evidence_key: bytes,
    operator_key: bytes, expected_preparation_id: str,
) -> dict:
    """Rollback only, never resume forward. Original authorization remains in journal."""
    slots = _slots(root, runtime.target)
    with _execution_lock(slots["lock"]):
        document = load(slots["journal"])
        state = unseal(document, evidence_key, KIND + "-execution")
        _require(state.get("schemaVersion") == 1 and state.get("phase") in EXECUTION_PHASES and
                 isinstance(state.get("history"), list) and bool(state["history"]) and
                 all(isinstance(entry, dict) and entry.get("phase") in EXECUTION_PHASES
                     for entry in state["history"]) and
                 state["history"][-1]["phase"] == state["phase"],
                 "invalid execution journal phase/history")
        _require(state["preparationId"] == expected_preparation_id and
                 state["target"] == runtime.target, "recovery journal target differs")
        _require(state["phase"] not in TERMINAL, "execution is terminal; no automatic recovery")
        _require(state["phase"] != "manual-recovery-required",
                 "manual recovery is latched; the old authorization cannot retry rollback")
        preparation = unseal(state["preparation"], evidence_key, KIND + "-preparation")
        _require(state["preparation"]["documentHash"] == expected_preparation_id and
                 preparation["target"] == runtime.target,
                 "recovery preparation differs from original authorization")
        validate_authorization(
            state["authorization"], preparation, expected_preparation_id,
            operator_key=operator_key, rollback_only=True,
        )
        plan = receipt_repair.load_plan(check_file(preparation["files"]["plan"], root))
        _require(runtime.writer_state(preparation["legacyWriterState"]["tasks"]) ==
                 preparation["legacyWriterState"], "legacy writer state changed")
        if state["phase"] in {"authorized", "candidate-staged"}:
            _transition(slots, state, "aborted", evidence_key)
        else:
            try:
                _rollback(slots, state, runtime, plan, evidence_key)
            except ManualRecoveryRequired as protection:
                _record_manual_recovery(slots, state, evidence_key, protection)
                raise
            except Exception:
                _transition(slots, state, "recovery-required", evidence_key)
                raise PromotionError("recovery incomplete; original/candidate evidence retained") from None
        return load(slots["journal"])
