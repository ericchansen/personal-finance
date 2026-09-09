"""Bound, source-only inputs for a single incremental cash account.

This module never collects data. Files are read only after the external root and
their content bindings have been checked; it delegates financial parsing and
identity interpretation to the existing adapters.
"""

from __future__ import annotations

import hashlib
import csv
import ipaddress
import json
import re
import stat
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit

from .canonical_identity import verified_resolution
from .domain import content_hash, normalize_money, require_hash, stable_id, utc
from .identity import IdentityObservation
from .identity_declarations import load_declarations
from .simplefin import SimpleFinAdapter, detect_protocol_version
from .source_admission import (
    partition_snapshot, partition_connection_errors, evaluate_connection, organization_scope,
)
from importers.facts.schema import AccountFact, extract_fact_objects, parse_fact
from importers.normalized.builder import verify_publication
from importers.rebuild.immutable_metadata import publish_json
from importers.simplefin.pipeline import load_mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
PERSISTENT_SOURCE_BLOCKERS = frozenset({
    "linked-source-transition-unsupported", "security-source-activity-unsupported",
})


class IncrementalHold(ValueError):
    """A stable reason code, deliberately not a dump of private source values."""


def require(condition: object, reason: str) -> None:
    if not condition:
        raise IncrementalHold(reason)


def timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(int(value), timezone.utc)
    return utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def money(value: Any) -> Decimal:
    require(value is not None and not isinstance(value, bool), "explicit-amount-required")
    return normalize_money(Decimal(str(value)))


def currency(value: Any) -> str:
    require(isinstance(value, str) and len(value) == 3
            and value.isascii() and value.isalpha() and value.isupper(),
            "explicit-currency-required")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def private(root: Path, relative: str | Path) -> Path:
    root = root.resolve()
    require(root != REPO_ROOT and REPO_ROOT not in root.parents, "external-data-root-required")
    path = (root / relative).absolute()
    require(not path.is_symlink(), "symlink-evidence-refused")
    resolved = path.resolve()
    require(resolved == root or root in resolved.parents, "path-outside-data-root")
    return resolved


def document(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(result, dict), "object-document-required")
    return result


def bound_file(root: Path, spec: dict) -> Path:
    path = private(root, spec["path"])
    require(path.is_file() and digest(path) == require_hash(spec["sha256"]), "evidence-hash-drift")
    return path


def write_receipt(root: Path, relative: str, body: dict) -> Path:
    path = private(root, relative)
    try:
        publish_json(path, body)
    except FileExistsError as error:
        raise IncrementalHold("immutable-receipt-conflict") from error
    path.chmod(stat.S_IREAD)
    return path


def observation_document(observation: IdentityObservation) -> dict:
    body = asdict(observation)
    for key in ("source_day", "observed_at", "trust_cutoff_day"):
        body[key] = body[key].isoformat() if body[key] is not None else None
    body["signed_amount"] = str(observation.signed_amount)
    body["attributes"] = [list(pair) for pair in observation.attributes]
    return body


def observation_from_document(body: dict) -> IdentityObservation:
    values = dict(body)
    values["source_day"] = date.fromisoformat(values["source_day"])
    values["observed_at"] = timestamp(values["observed_at"])
    values["trust_cutoff_day"] = (
        date.fromisoformat(values["trust_cutoff_day"]) if values["trust_cutoff_day"] else None
    )
    values["signed_amount"] = money(values["signed_amount"])
    values["attributes"] = tuple(tuple(pair) for pair in values["attributes"])
    return IdentityObservation(**values)


def economic_key(observation: IdentityObservation) -> str:
    return content_hash({
        "sourceId": observation.provider_transaction_id,
        "date": observation.source_day.isoformat(), "amount": str(observation.signed_amount),
        "currency": observation.currency, "status": observation.status,
        "description": observation.description,
    })


@dataclass(frozen=True)
class Scope:
    root: Path
    config: dict
    config_hash: str
    scope_id: str
    fact: AccountFact
    baseline_root: Path
    baseline: Any
    declarations: Any
    zone: ZoneInfo
    rows_by_observation: dict
    config_path: Path
    baseline_currency: str
    published_canonical_ids: frozenset[str]

    @property
    def account_id(self):
        return self.config["wealthfolioAccountId"]

    @property
    def canonical_id(self):
        return self.config["canonicalAccountId"]


def load_scope(root: Path, path: str | Path) -> Scope:
    root = private(root, ".")
    config = document(private(root, path))
    require(config.get("schemaVersion") == 1 and config.get("kind") == "incremental-cash-scope",
            "scope-schema-invalid")
    require(Path(config["dataRoot"]).resolve() == root, "scope-data-root-mismatch")
    for name in ("canonicalAccountId", "sourceAccountId", "sourceConnectionId", "rawConnectionId",
                 "wealthfolioAccountId", "origin", "timezone", "writerEnvironmentId", "instanceId"):
        require(isinstance(config.get(name), str) and config[name], "scope-binding-missing")
    require_hash(config["writerEnvironmentId"])
    require_hash(config["instanceId"])
    database = config.get("postgres")
    require(isinstance(database, dict) and all(isinstance(database.get(key), str) and database[key]
            for key in ("database", "instanceId", "environment")), "postgres-authority-binding-required")
    uuid.UUID(database["instanceId"])
    origin = urlsplit(config["origin"])
    try:
        loopback = origin.hostname == "localhost" or ipaddress.ip_address(origin.hostname).is_loopback
    except ValueError:
        loopback = False
    require(origin.scheme in {"http", "https"} and loopback and origin.port is not None
            and not origin.username and not origin.password and not origin.query
            and not origin.fragment and origin.path == "", "scoped-loopback-app-origin-required")
    for key in ("maxSnapshotAgeSeconds", "maxBalanceAgeSeconds"):
        require(type(config.get(key)) is int and 0 < config[key] <= 172800,
                "explicit-bounded-freshness-required")
    try:
        zone = ZoneInfo(config["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise IncrementalHold("timezone-data-unavailable") from exc
    mapping_path = bound_file(root, config["accountMap"])
    require(mapping_path == private(root, "simplefin/account-map.json"), "mapping-path-mismatch")
    mapping = load_mapping(root).get(config["sourceAccountId"])
    require(isinstance(mapping, dict) and mapping.get("action") == "import",
            "source-account-not-admitted")
    require(mapping.get("assertionAccountId") == config["canonicalAccountId"]
            and mapping.get("wealthfolioAccountId") == config["wealthfolioAccountId"],
            "source-account-routing-mismatch")
    fact_path = bound_file(root, config["accountFact"])
    facts = []
    for item in extract_fact_objects(json.loads(fact_path.read_text(encoding="utf-8"))):
        parsed, errors = parse_fact(item, fact_path)
        if item.get("id") == config["canonicalAccountId"]:
            require(not errors and parsed is not None, "account-fact-invalid")
            facts.append(parsed.fact)
    require(len(facts) == 1 and isinstance(facts[0], AccountFact), "account-fact-not-unique")
    fact = facts[0]
    require(not fact.excluded and fact.closed is None
            and fact.kind in {"CASH", "CREDIT_CARD"}
            and fact.tracking_mode == "TRANSACTIONS", "account-not-active-cash-card")
    baseline_root = private(root, config["baselineRoot"])
    manifest = private(baseline_root, "normalized/canonical/manifest.json")
    require(digest(manifest) == require_hash(config["baselineManifestSha256"]), "baseline-manifest-drift")
    verify_publication(baseline_root)
    published_facts = {
        item["path"].replace("\\", "/"): item["sha256"] for item in document(manifest)["sourceFiles"]
        if item["path"].replace("\\", "/").startswith("facts/")
    }
    live_facts = {
        path.relative_to(root).as_posix(): digest(path)
        for path in private(root, "facts").rglob("*.json")
    }
    require(published_facts and published_facts == live_facts, "facts-catalog-drift")
    require(fact_path.relative_to(root).as_posix() in live_facts, "account-fact-outside-facts-catalog")
    with private(baseline_root, "normalized/canonical/accounts.csv").open(encoding="utf-8-sig", newline="") as stream:
        accounts = [row for row in csv.DictReader(stream) if row["account_id"] == config["canonicalAccountId"]]
    require(len(accounts) == 1, "canonical-baseline-account-not-unique")
    account = accounts[0]
    require(account["kind"] == fact.kind and account["name"] == fact.display_name
            and not account["closed"] and account["excluded"] == "false"
            and account["tracking_mode"] == "TRANSACTIONS", "canonical-baseline-account-ineligible")
    baseline_currency = currency(account["currency"])
    with private(baseline_root, "normalized/canonical/positions.csv").open(encoding="utf-8-sig", newline="") as stream:
        require(not any(row["account_id"] == config["canonicalAccountId"] for row in csv.DictReader(stream)),
                "baseline-investments-unsupported")
    declarations = load_declarations(baseline_root)
    verified = verified_resolution(baseline_root, require_resolved=False)
    require(verified.published.get("policyDocument") == verified.resolution.policy.document(),
            "baseline-policy-document-drift")
    scope_rows = document(private(
        baseline_root, "normalized/canonical/transaction-observations.json"
    ))["identityScope"]["rows"]
    # source_hash is the verified row fingerprint. Identical duplicate rows can
    # share that fingerprint without replacing their distinct observation IDs.
    observations = verified.resolution.observations
    require(Counter(item.source_hash for item in observations)
            == Counter(content_hash(row) for row in scope_rows), "baseline-observation-row-binding-drift")
    rows_by_hash = {content_hash(row): row for row in scope_rows}
    rows_by_observation = {
        observation.observation_id: rows_by_hash[observation.source_hash]
        for observation in observations
    }
    require(set(rows_by_observation) == {item.observation_id for item in verified.resolution.observations},
            "baseline-observation-row-binding-drift")
    scope_id = stable_id(
        "incremental_scope", content_hash(str(root)), config["canonicalAccountId"],
        config["rawConnectionId"], config["sourceAccountId"],
        config["origin"], config["wealthfolioAccountId"],
    )
    if config.get("bootstrap") is not None:
        bound_file(root, config["bootstrap"])
    if config.get("bootstrapSourceAnchor") is not None:
        require(config.get("bootstrap") is None, "source-anchor-cannot-use-date-window-bootstrap")
        bound_file(root, config["bootstrapSourceAnchor"])
    return Scope(root, config, content_hash(config), scope_id, fact, baseline_root,
                 verified.resolution, declarations, zone, rows_by_observation, private(root, path), baseline_currency,
                 frozenset(item["canonicalTransactionId"] for item in document(private(
                     baseline_root, "normalized/canonical/transaction-lineage.json"
                 ))["canonicalTransactions"]))


def baseline_currency_proof(scope: Scope, observation: IdentityObservation) -> dict | None:
    row = scope.rows_by_observation[observation.observation_id]
    if not row.get("source_file"):
        return None
    path = private(scope.baseline_root, row["source_file"])
    raw = path.read_bytes()
    if observation.source_family == "simplefin":
        payload = json.loads(raw)
        body = payload.get("data", payload)
        accounts = [item for item in body.get("accounts", [])
                    if str(item.get("id") or item.get("account_id")) == observation.source_account_id]
        require(len(accounts) == 1, "baseline-source-account-not-unique")
        account = accounts[0]
        unit = currency(account.get("currency"))
        prefix = f"simplefin:{observation.source_account_id}:"
        source_id = observation.provider_transaction_id or ""
        require(source_id.startswith(prefix) and row["source_id"] == source_id,
                "baseline-source-identity-mismatch")
        raw_id = source_id[len(prefix):]
        matching = [item for item in account.get("transactions", [])
                    if str(item.get("id") or item.get("transaction_id") or "") == raw_id]
        require(raw_id and len(matching) == 1, "baseline-source-transaction-not-unique")
        transaction = matching[0]
        has_date = validate_raw_transaction(transaction, unit, allow_dateless_pending=True)
        raw_blocker = raw_projection_blocker(transaction, observation.status)
        version = detect_protocol_version(payload)
        raw_connection = organization_scope(account, version)
        if observation.source_account_id == scope.config["sourceAccountId"]:
            require(raw_connection == scope.config["rawConnectionId"], "baseline-source-connection-mismatch")
        if not has_date:
            require(observation.status == "pending" and money(transaction["amount"]) == observation.signed_amount
                    and observation.currency == unit, "baseline-source-transaction-mismatch")
            return {"currency": unit, "artifactSha256": hashlib.sha256(raw).hexdigest(),
                    "sourceFamily": "simplefin", "rawAccountId": observation.source_account_id,
                    "rawTransactionId": raw_id, "rawConnectionId": raw_connection,
                    "transactionSha256": content_hash(transaction), "dateStatus": "unavailable-pending",
                    **({"rawProjectionBlocker": raw_blocker} if raw_blocker else {})}
        parsed = SimpleFinAdapter(raw_connection).parse(
            json.dumps({"version": version, "accounts": [{**account, "transactions": [transaction]}]}).encode(),
            raw_locator=f"external://baseline/{hashlib.sha256(raw).hexdigest()}",
            observed_at=observation.observed_at, processed_at=observation.observed_at,
            protocol_version=version,
        ).transactions
        require(len(parsed) == 1 and parsed[0].source_transaction_id == raw_id
                and parsed[0].amount == observation.signed_amount
                and parsed[0].status == observation.status
                and parsed[0].currency == observation.currency, "baseline-source-transaction-mismatch")
        proof = {"currency": unit, "artifactSha256": hashlib.sha256(raw).hexdigest(),
                 "sourceFamily": "simplefin", "rawAccountId": observation.source_account_id,
                 "rawTransactionId": raw_id, "rawConnectionId": raw_connection,
                 "transactionSha256": content_hash(transaction),
                 **({"rawProjectionBlocker": raw_blocker} if raw_blocker else {})}
        if parsed[0].effective_at.date() != observation.source_day:
            require(observation.status == "pending", "baseline-source-transaction-mismatch")
            proof.update(dateStatus="unverified-normalized-pending-date",
                         rawSourceDay=parsed[0].effective_at.date().isoformat())
        return proof
    elif observation.source_family in {"ofx", "qfx"}:
        units = {value.decode("ascii") for value in re.findall(rb"<CURDEF>\s*([A-Z]{3})(?=<|\s|$)", raw)}
        if len(units) != 1:
            return None
        unit = currency(next(iter(units)))
    else:
        return None
    require(unit == observation.currency, "baseline-source-currency-mismatch")
    return {"currency": unit, "artifactSha256": hashlib.sha256(raw).hexdigest(),
            "sourceFamily": observation.source_family}


def validate_raw_transaction(row: dict, account_currency: str, *, allow_dateless_pending=False) -> bool:
    """Validate explicit inputs without changing the existing parser's lifecycle.

    SimpleFIN pending is optional: a nonzero posted timestamp without the flag
    is posted under the existing parser. Zero posted time denotes pending and
    still needs a real transacted_at instead of the parser's clock fallback.
    """
    pending = row.get("pending")
    status = row.get("status")
    require("pending" not in row or isinstance(pending, bool), "source-pending-state-invalid")
    require(status is None or isinstance(status, str)
            and status.lower() in {"pending", "posted", "reversed"}, "source-lifecycle-invalid")
    posted = row.get("posted") if "posted" in row else row.get("posted_at")
    parser_pending = bool(pending) or posted in {0, "0"}
    if status is not None:
        require(pending is None or (status.lower() == "pending") == pending, "source-lifecycle-conflict")
        require(posted not in {0, "0"} or status.lower() == "pending", "source-lifecycle-conflict")
    require(posted is not None or status is not None or pending is True, "source-lifecycle-not-explicit")
    effective = row.get("transacted_at") if parser_pending and posted in {None, 0, "0"} else posted or row.get("transacted_at")
    require(currency(row.get("currency", account_currency)) == account_currency,
            "source-transaction-currency-mismatch")
    money(row.get("amount"))
    final_status = status.lower() if status is not None else "pending" if parser_pending else "posted"
    if effective in {None, 0, "0", ""}:
        require(allow_dateless_pending and final_status == "pending", "source-effective-time-not-explicit")
        return False
    timestamp(effective)
    return True


def raw_transaction_state(row: dict, account_currency: str, source_account_id: str) -> dict:
    """Exact source state; a pending epoch-zero record has no posting date."""
    has_date = validate_raw_transaction(row, account_currency, allow_dateless_pending=True)
    raw_id = str(row.get("id") or row.get("transaction_id") or "")
    require(raw_id, "source-transaction-id-required")
    posted = row.get("posted") if "posted" in row else row.get("posted_at")
    pending = bool(row.get("pending")) or posted in {0, "0"}
    status = str(row["status"]).lower() if row.get("status") is not None else "pending" if pending else "posted"
    effective = row.get("transacted_at") if pending and posted in {None, 0, "0"} else posted or row.get("transacted_at")
    return {
        "sourceId": f"simplefin:{source_account_id}:{raw_id}", "rawTransactionId": raw_id,
        "status": status, "sourceDay": timestamp(effective).date().isoformat() if has_date else None,
        "amount": str(money(row["amount"])), "currency": currency(row.get("currency", account_currency)),
        "description": str(row.get("description") or row.get("payee") or ""),
        "rawTransactionHash": content_hash(row),
    }


def raw_projection_blocker(row: dict, status: str) -> str:
    """Current raw restrictions apply even when the financial version is unchanged."""
    if status not in {"pending", "posted"}:
        return "source-lifecycle-unsupported"
    if any(row.get(key) for key in ("assetId", "asset_id", "security_id", "symbol", "shares")):
        return "security-source-activity-unsupported"
    if any(row.get(key) for key in ("correction_of", "reversal_of", "pending_of", "counterpart_id")):
        return "linked-source-transition-unsupported"
    return ""


@dataclass(frozen=True)
class Source:
    receipt: dict
    receipt_hash: str
    snapshot_hash: str
    observed_at: datetime
    balance_at: datetime
    balance: Decimal
    currency: str
    transactions: tuple
    raw_transactions: dict
    snapshot_path: Path
    admission: dict
    states: dict
    pending_without_date: tuple


def load_source(scope: Scope, now: datetime) -> Source:
    root, config = scope.root, scope.config
    folder = private(root, "automation/source-collection")
    current = document(folder / "current.json")
    require(current.get("status") == "collected", "latest-collector-run-failed")
    pointer = document(folder / "latest-success.json")
    require(current.get("receiptHash") == pointer.get("receiptHash"), "collector-pointer-transition")
    require(pointer.get("kind") == "simplefin-source-collection-pointer"
            and pointer.get("schemaVersion") == 1 and pointer.get("status") == "collected",
            "collector-success-pointer-invalid")
    receipt_hash = require_hash(pointer["receiptHash"])
    receipt = document(folder / "runs" / f"{receipt_hash}.json")
    require(receipt.get("receiptHash") == receipt_hash
            and receipt.get("schemaVersion") == 1 and receipt.get("kind") == "simplefin-source-collection-run"
            and content_hash({k: v for k, v in receipt.items() if k != "receiptHash"}) == receipt_hash
            and pointer["inputSetHash"] == receipt["inputSetHash"]
            and pointer.get("releaseCommit") == receipt.get("releaseCommit")
            and receipt.get("status") == "collected", "collector-receipt-invalid")
    observed = timestamp(receipt["observedAt"])
    require(0 <= (utc(now) - observed).total_seconds() <= config["maxSnapshotAgeSeconds"],
            "source-snapshot-stale")
    snapshot_hash = require_hash(receipt["inputManifest"]["snapshotSha256"])
    require(receipt["inputSetHash"] == content_hash({
        "inputManifest": receipt["inputManifest"],
        "expectedInventoryHash": receipt["inventory"]["expectedInventoryHash"],
    }), "collector-input-set-drift")
    candidates = [
        path for path in private(root, f"raw/simplefin/{observed.date().isoformat()}").glob("simplefin-*.json")
        if not path.is_symlink() and digest(path) == snapshot_hash
    ]
    require(len(candidates) == 1, "immutable-snapshot-not-unique")
    snapshot = private(root, candidates[0])
    request = snapshot.with_name(f"request-{snapshot.stem.removeprefix('simplefin-')}.json")
    require(digest(request) == receipt["inputManifest"]["requestMetadataSha256"],
            "snapshot-request-binding-drift")
    metadata = document(request)
    raw = document(snapshot)
    version = detect_protocol_version(raw)
    require(str(metadata.get("protocolVersion")) == version == str(receipt["protocolVersion"]),
            "source-protocol-mismatch")
    body = raw.get("data", raw) if version == "2" else raw
    accounts = body.get("accounts")
    require(isinstance(accounts, list), "source-account-inventory-invalid")
    requested_from = date.fromisoformat(metadata["requestedStart"])
    requested_through = date.fromisoformat(metadata["requestedEnd"])
    require(1 <= (requested_through - requested_from).days + 1 <= 90
            and requested_through <= observed.date()
            and receipt["requestWindowDays"] == (requested_through - requested_from).days + 1,
            "source-request-window-invalid")
    require(metadata["requestedStart"] == receipt["inputManifest"]["requestedStart"]
            and metadata["requestedEnd"] == receipt["inputManifest"]["requestedEnd"],
            "source-request-window-drift")
    partition = partition_snapshot(
        snapshot_sha256=snapshot_hash, observed_at=observed, version=version,
        accounts=accounts, errors=body.get("errlist" if version == "2" else "errors", []),
        requested_start=requested_from, requested_end=requested_through,
    )
    advisories, actionable = partition_connection_errors(partition.unscopable_errors)
    require(not actionable, "unscopable-source-errors")
    evidence = [item for item in partition.evidence if item.connection_id == config["sourceConnectionId"]]
    require(len(evidence) == 1, "source-connection-mismatch")
    admission = evaluate_connection(evidence, None, as_of=utc(now))
    require(admission.admitted and admission.fresh and not admission.stale,
            "source-connection-unhealthy")
    require(config["sourceAccountId"] in partition.accounts_by_scope.get(config["sourceConnectionId"], ()),
            "source-account-connection-mismatch")
    selected = [item for item in accounts
                if str(item.get("id") or item.get("account_id")) == config["sourceAccountId"]]
    require(len(selected) == 1, "source-account-missing-or-duplicated")
    account = selected[0]
    require(organization_scope(account, version) == config["rawConnectionId"],
            "raw-source-connection-mismatch")
    raw_status = account.get("status", account.get("account_status"))
    require(raw_status is None or str(raw_status).lower() == "active", "source-account-inactive")
    raw_kind = account.get("account_type", account.get("type"))
    if raw_kind is not None:
        kinds = ({"cash", "checking", "savings", "depository"} if scope.fact.kind == "CASH"
                 else {"credit", "card", "credit_card", "credit-card", "creditcard"})
        require(str(raw_kind).casefold() in kinds, "source-account-type-mismatch")
    require(not account.get("holdings") and not account.get("positions"), "source-investments-unsupported")
    explicit_currency = currency(account.get("currency"))
    raw_txns = account.get("transactions")
    require(isinstance(raw_txns, list), "source-transactions-not-explicit")
    indexed = {}
    states = {}
    for row in raw_txns:
        key = str(row.get("id") or row.get("transaction_id") or "")
        require(key and key not in indexed, "source-transaction-id-not-unique")
        state = raw_transaction_state(row, explicit_currency, config["sourceAccountId"])
        states[state["sourceId"]] = state
        indexed[key] = row
    balance_at = timestamp(account.get("balance-date") or account.get("balance_date") or account.get("balance_at"))
    require(0 <= (utc(now) - balance_at).total_seconds() <= config["maxBalanceAgeSeconds"],
            "source-balance-stale")
    require(balance_at <= observed, "source-balance-after-snapshot")
    # Parse just this validated account, so an unrelated failed institution does
    # not contaminate a healthy scope. These are parser inputs, not new artifacts.
    dated = [row for row in raw_txns
             if states[f"simplefin:{config['sourceAccountId']}:{str(row.get('id') or row.get('transaction_id') or '')}"]["sourceDay"] is not None]
    filtered = {"version": version, "accounts": [{**account, "transactions": dated}]}
    parsed = SimpleFinAdapter(config["sourceConnectionId"]).parse(
        json.dumps(filtered).encode(), raw_locator=f"external://snapshot/{snapshot_hash}",
        observed_at=observed, processed_at=utc(now), protocol_version=version,
    )
    require(len(parsed.balances) == 1, "source-current-balance-required")
    balance = parsed.balances[0]
    require(balance.currency == explicit_currency, "source-balance-currency-mismatch")
    return Source(receipt, receipt_hash, snapshot_hash, observed, balance_at,
                  balance.amount, explicit_currency, parsed.transactions,
                  indexed, snapshot, {**evidence[0].document(), "rawAccountStatus": raw_status,
                                      "unscopedAdvisories": list(advisories)},
                  states, tuple(row for row in states.values() if row["sourceDay"] is None))
