"""Guarded SimpleFIN transaction application planning for rebuild staging."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint

from .pipeline import (
    INFLOW,
    OUTFLOW,
    ExistingTransaction,
    existing_from_activities,
    normalize_description,
    overlap_key,
    read_snapshot,
)

CENT = Decimal("0.01")
ZERO = Decimal("0")
RECONCILIATION_PREFIXES = (
    "gap:",
    "rebuild:assertion:",
    "fidelity:reconcile:",
    "monarch:opening:",
)
MANUAL_RULINGS = {"hold", "hold-non-household", "pair-existing"}
TransferKey = tuple[str, str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manual_decisions(
    path: Path, private_root: Path | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load and verify private, source-backed rulings for held transactions."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionError(f"cannot read SimpleFIN manual decisions: {exc}") from exc
    decisions = document.get("decisions")
    if document.get("schemaVersion") != 1 or not isinstance(decisions, list):
        raise DecisionError(
            "SimpleFIN manual decisions must have schemaVersion 1 and decisions"
        )
    result: dict[tuple[str, str], dict[str, Any]] = {}
    decision_ids: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            raise DecisionError("SimpleFIN manual decision must be an object")
        decision_id = str(decision.get("decisionId") or "").strip()
        if not decision_id:
            raise DecisionError("SimpleFIN manual decision has no decisionId")
        if decision_id in decision_ids:
            raise DecisionError("SimpleFIN manual decisionId is duplicated")
        decision_ids.add(decision_id)
        source_account_id = str(decision.get("sourceAccountId") or "")
        source_id = str(decision.get("sourceId") or "")
        key = (source_account_id, source_id)
        if not all(key) or key in result:
            raise DecisionError("SimpleFIN manual decision IDs are missing or duplicated")
        if decision.get("ruling") not in MANUAL_RULINGS:
            raise DecisionError(f"unsupported SimpleFIN ruling for {source_id}")
        if not str(decision.get("rationale") or "").strip():
            raise DecisionError(f"SimpleFIN ruling has no rationale for {source_id}")
        evidence = decision.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise DecisionError(f"SimpleFIN ruling has no evidence for {source_id}")
        for source in evidence:
            if not isinstance(source, dict):
                raise DecisionError(
                    f"SimpleFIN ruling evidence is invalid for {source_id}"
                )
            source_path = Path(str(source.get("path") or "")).resolve()
            expected = str(source.get("sha256") or "")
            if private_root is not None:
                root = private_root.resolve()
                if source_path != root and root not in source_path.parents:
                    raise DecisionError(
                        f"SimpleFIN ruling evidence is outside private data for {source_id}"
                    )
            if not source_path.is_file() or sha256_file(source_path) != expected:
                raise DecisionError(
                    f"SimpleFIN ruling evidence changed for {source_id}"
                )
        if decision["ruling"] == "pair-existing" and not (
            decision.get("counterpartSourceAccountId")
            and decision.get("counterpartSourceId")
        ):
            raise DecisionError(
                f"pair-existing ruling has no exact counterpart for {source_id}"
            )
        result[key] = decision
    return result


def manual_decision_evidence_binding(
    path: Path, private_root: Path | None = None
) -> dict[str, Any]:
    """Return the exact decision-file and nested-evidence hashes."""
    decisions = load_manual_decisions(path, private_root)
    nested = set()
    for decision in decisions.values():
        for source in decision["evidence"]:
            source_path = Path(str(source["path"])).resolve()
            actual_hash = sha256_file(source_path)
            if actual_hash != str(source["sha256"]):
                raise DecisionError(
                    "SimpleFIN ruling evidence changed while it was being sealed"
                )
            nested.add((str(source_path), actual_hash))
    binding = {
        "manualDecisionsSha256": sha256_file(path),
        "nestedEvidence": [
            {"path": evidence_path, "sha256": evidence_hash}
            for evidence_path, evidence_hash in sorted(nested)
        ],
    }
    binding["fingerprint"] = plan_fingerprint(binding)
    return binding


def _signed(activity: dict[str, Any]) -> Decimal:
    amount = abs(Decimal(str(activity.get("amount") or 0)))
    kind = activity.get("activityType")
    if kind in INFLOW:
        return amount
    if kind in OUTFLOW:
        return -amount
    raise DecisionError(f"unsupported cash activity type: {kind}")


def normalize_subtype(value: Any) -> str | None:
    normalized = str(value).strip().casefold() if value is not None else ""
    return normalized or None


def canonicalize_metadata(value: Any) -> str:
    parsed = {} if value is None else value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
    return json.dumps(
        parsed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def metadata_with_external_flow(value: Any, is_external: bool) -> str:
    canonical = canonicalize_metadata(value)
    parsed = json.loads(canonical)
    if not isinstance(parsed, dict):
        raise DecisionError("activity metadata must be a JSON object")
    flow = parsed.get("flow")
    if not isinstance(flow, dict):
        flow = {}
    parsed["flow"] = {**flow, "is_external": is_external}
    return canonicalize_metadata(parsed)


def _activity_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    raw_date = str(row.get("date") or row.get("activityDate") or "")
    try:
        normalized_date = datetime.fromisoformat(
            raw_date.replace("Z", "+00:00")
        ).isoformat()
    except ValueError:
        normalized_date = raw_date
    return {
        "id": row.get("id"),
        "accountId": row.get("accountId"),
        "activityType": row.get("activityType"),
        "date": normalized_date,
        "amount": format(Decimal(str(row.get("amount") or 0)).normalize(), "f"),
        "currency": row.get("currency"),
        "idempotencyKey": row.get("idempotencyKey"),
        "sourceGroupId": row.get("sourceGroupId"),
        "assetId": row.get("assetId") or None,
        "subtype": normalize_subtype(row.get("subtype")),
        "metadata": canonicalize_metadata(row.get("metadata")),
    }


def activity_semantic_fingerprint(row: dict[str, Any]) -> str:
    return plan_fingerprint(_activity_fingerprint(row))


def ledger_fingerprint(rows: list[dict[str, Any]], account_ids: set[str]) -> str:
    relevant = [
        _activity_fingerprint(row)
        for row in rows
        if str(row.get("accountId")) in account_ids
    ]
    relevant.sort(key=lambda row: str(row["id"]))
    return plan_fingerprint(relevant)


def _cash_flow(
    account_type: str, activity_type: str, amount: Decimal
) -> tuple[Decimal, Decimal]:
    if account_type not in {"CASH", "CREDIT_CARD"}:
        return ZERO, ZERO
    if account_type == "CREDIT_CARD":
        if activity_type == "CREDIT":
            return ZERO, -amount
        if activity_type in {"WITHDRAWAL", "FEE", "TAX", "EXPENSE"}:
            return ZERO, amount
        return ZERO, ZERO
    if activity_type in {"DEPOSIT", "CREDIT", "INTEREST", "DIVIDEND"}:
        return amount, ZERO
    if activity_type in {"WITHDRAWAL", "FEE", "TAX", "EXPENSE"}:
        return ZERO, amount
    return ZERO, ZERO


def _activity_cash_flow(
    account_type: str, activity: dict[str, Any]
) -> tuple[Decimal, Decimal]:
    metadata = activity.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    if (
        normalize_subtype(activity.get("subtype")) == "external_transfer"
        or isinstance(metadata, dict)
        and metadata.get("flow", {}).get("is_external") is True
    ):
        return ZERO, ZERO
    return _cash_flow(
        account_type,
        str(activity["activityType"]),
        abs(Decimal(str(activity.get("amount") or 0))),
    )


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _has_transfer_evidence(description: str) -> bool:
    value = normalize_description(description)
    if "interest" in value:
        return "transfer" in value
    if any(word in value for word in ("transfer", "autopay", "pymt", "thank you")):
        return True
    return "payment" in value and any(
        marker in value
        for marker in ("bank", "card", "p2p", "online payment", "payment received")
    )


def _transfer_key(item: dict[str, Any]) -> TransferKey:
    key = (
        str(item.get("sourceAccountId") or ""),
        str(item["transaction"].get("sourceId") or ""),
    )
    if not all(key):
        raise DecisionError("SimpleFIN transfer identity is incomplete")
    return key


def _pair_pending_transfers(
    pending: list[dict[str, Any]],
    durable_groups: dict[TransferKey, str],
    *,
    max_days: int = 3,
) -> tuple[list[tuple[dict, dict]], set[TransferKey]]:
    pairs: list[tuple[dict, dict]] = []
    ambiguous: set[TransferKey] = set()
    remaining: dict[TransferKey, dict[str, Any]] = {}
    for item in pending:
        key = _transfer_key(item)
        if key in remaining:
            raise DecisionError("SimpleFIN transfer identity is duplicated")
        remaining[key] = item
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in pending:
        group = durable_groups.get(_transfer_key(item))
        if group:
            grouped[group].append(item)
    for items in grouped.values():
        for item in items:
            remaining.pop(_transfer_key(item), None)
        card_payment = (
            len(items) == 2
            and {items[0]["accountType"], items[1]["accountType"]}
            == {"CASH", "CREDIT_CARD"}
        )
        valid_card_direction = True
        if card_payment:
            cash_item = next(item for item in items if item["accountType"] == "CASH")
            card_item = next(
                item for item in items if item["accountType"] == "CREDIT_CARD"
            )
            valid_card_direction = (
                Decimal(cash_item["transaction"]["amount"]) < 0
                and Decimal(card_item["transaction"]["amount"]) > 0
            )
        if (
            len(items) == 2
            and items[0]["target"] != items[1]["target"]
            and Decimal(items[0]["transaction"]["amount"])
            == -Decimal(items[1]["transaction"]["amount"])
            and valid_card_direction
        ):
            pairs.append((items[0], items[1]))
        else:
            ambiguous.update(_transfer_key(item) for item in items)

    candidates: dict[TransferKey, list[TransferKey]] = defaultdict(list)
    items = list(remaining.values())
    for index, left in enumerate(items):
        left_txn = left["transaction"]
        left_date = datetime.fromisoformat(left_txn["date"]).date()
        left_description = normalize_description(left_txn["description"])
        for right in items[index + 1:]:
            right_txn = right["transaction"]
            if left["target"] == right["target"]:
                continue
            if Decimal(left_txn["amount"]) != -Decimal(right_txn["amount"]):
                continue
            right_date = datetime.fromisoformat(right_txn["date"]).date()
            if abs((left_date - right_date).days) > max_days:
                continue
            right_description = normalize_description(right_txn["description"])
            card_payment = {
                left["accountType"], right["accountType"]
            } == {"CASH", "CREDIT_CARD"}
            if card_payment:
                cash_item = left if left["accountType"] == "CASH" else right
                card_item = right if right["accountType"] == "CREDIT_CARD" else left
                if (
                    Decimal(cash_item["transaction"]["amount"]) >= 0
                    or Decimal(card_item["transaction"]["amount"]) <= 0
                ):
                    continue
            left_evidence = _has_transfer_evidence(left_description)
            right_evidence = _has_transfer_evidence(right_description)
            if not (left_evidence or right_evidence):
                continue
            if not (
                (card_payment and left_evidence and right_evidence)
                or (
                    "transfer" in left_description
                    and "transfer" in right_description
                )
            ):
                continue
            left_key = _transfer_key(left)
            right_key = _transfer_key(right)
            candidates[left_key].append(right_key)
            candidates[right_key].append(left_key)
    claimed: set[TransferKey] = set()
    for key, matches in candidates.items():
        if key in claimed:
            continue
        if len(matches) != 1 or len(candidates[matches[0]]) != 1:
            ambiguous.add(key)
            ambiguous.update(matches)
            continue
        other_key = matches[0]
        pairs.append((remaining[key], remaining[other_key]))
        claimed.update((key, other_key))
    return pairs, ambiguous


def _as_row(payload: dict[str, Any]) -> dict[str, Any]:
    row = deepcopy(payload)
    row["date"] = row.pop("activityDate")
    return row


def link_group_fingerprint(link: dict[str, Any]) -> str:
    members = sorted(
        str(value)
        for value in (
            link.get("leftKey"),
            link.get("rightKey"),
            link.get("leftActivityId"),
            link.get("rightActivityId"),
        )
        if value
    )
    return "link:" + "|".join(members)


def _link_for_activity(
    links: list[dict[str, Any]],
    activity_id: str,
    idempotency_key: str | None,
) -> dict[str, Any] | None:
    matches = [
        link
        for link in links
        if activity_id
        in {
            str(link.get("leftActivityId") or ""),
            str(link.get("rightActivityId") or ""),
        }
        or idempotency_key
        and idempotency_key
        in {
            link.get("leftKey"),
            link.get("rightKey"),
        }
    ]
    if len(matches) > 1:
        raise DecisionError("metadata finalization belongs to multiple transfer links")
    return matches[0] if matches else None


def _post_link_finalization_payload(
    payload: dict[str, Any],
    link: dict[str, Any] | None,
) -> dict[str, Any]:
    expected = dict(payload)
    if link:
        expected["sourceGroupId"] = link_group_fingerprint(link)
        expected["metadata"] = metadata_with_external_flow(
            payload.get("metadata"), False
        )
    return expected


def expected_post_fingerprint(
    activities: list[dict[str, Any]],
    account_ids: set[str],
    operations: dict[str, list],
    links: list[dict[str, str]],
) -> str:
    by_id = {
        row["id"]: deepcopy(row)
        for row in activities
        if str(row.get("accountId")) in account_ids
    }
    for activity_id in operations["deleteIds"]:
        by_id.pop(activity_id, None)
    for payload in operations["updates"]:
        by_id[payload["id"]] = {"id": payload["id"], **_as_row(payload)}
    for finalization in operations.get("metadataFinalizations", []):
        payload = finalization["payload"]
        by_id[finalization["activityId"]] = {
            "id": finalization["activityId"],
            **_as_row(payload),
        }
    for payload in operations["creates"]:
        key = payload["idempotencyKey"]
        by_id[f"key:{key}"] = {"id": f"key:{key}", **_as_row(payload)}
    linked_rows: dict[str, str] = {}
    for link in links:
        group = link_group_fingerprint(link)
        for field in ("leftKey", "rightKey"):
            if link.get(field):
                linked_rows[f"key:{link[field]}"] = group
        for field in ("leftActivityId", "rightActivityId"):
            if link.get(field):
                linked_rows[f"id:{link[field]}"] = group
    normalized = []
    for row in by_id.values():
        fingerprint = _activity_fingerprint(row)
        key = str(row.get("idempotencyKey") or "")
        if key.startswith("simplefin:"):
            fingerprint["id"] = f"key:{key}"
        link_group = linked_rows.get(f"key:{key}") or linked_rows.get(
            f"id:{row.get('id')}"
        )
        if link_group:
            fingerprint["sourceGroupId"] = link_group
            fingerprint["metadata"] = metadata_with_external_flow(
                row.get("metadata"), False
            )
        normalized.append(fingerprint)
    normalized.sort(key=lambda row: str(row["id"]))
    return plan_fingerprint(normalized)


def _reconciliation(
    rows: list[dict[str, Any]], account_id: str
) -> dict[str, Any] | None:
    candidates = [
        row
        for row in rows
        if row.get("accountId") == account_id
        and any(
            str(row.get("idempotencyKey") or "").startswith(prefix)
            for prefix in RECONCILIATION_PREFIXES
        )
    ]
    for prefix in RECONCILIATION_PREFIXES:
        preferred = [
            row
            for row in candidates
            if str(row.get("idempotencyKey") or "").startswith(prefix)
        ]
        if len(preferred) > 1:
            raise DecisionError(
                f"account {account_id} has multiple {prefix} reconciliation activities"
            )
        if preferred:
            return preferred[0]
    return None


def _type_for_effect(effect: Decimal, account_type: str, original_type: str) -> str:
    if account_type == "CREDIT_CARD":
        return "TRANSFER_IN" if effect > 0 else "WITHDRAWAL"
    return "TRANSFER_IN" if effect > 0 else "TRANSFER_OUT"


def _external_transfer_fields() -> dict[str, str]:
    return {
        "subtype": "external_transfer",
        "metadata": canonicalize_metadata({"flow": {"is_external": True}}),
    }


def activity_payload(row: dict[str, Any], *, include_id: bool = True) -> dict[str, Any]:
    payload = {
        "accountId": row["accountId"],
        "activityType": row["activityType"],
        "activityDate": row["date"],
        "amount": float(abs(Decimal(str(row.get("amount") or 0)))),
        "currency": row["currency"],
        "isDraft": bool(row.get("isDraft", False)),
        "comment": row.get("comment"),
        "idempotencyKey": row.get("idempotencyKey"),
        "subtype": normalize_subtype(row.get("subtype")),
        "metadata": canonicalize_metadata(row.get("metadata")),
    }
    if include_id:
        payload["id"] = row["id"]
    if row.get("assetId") is not None:
        payload["assetId"] = row["assetId"]
    return payload


def _update_payload(
    row: dict[str, Any], effect: Decimal, account_type: str
) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "accountId": row["accountId"],
        "activityType": _type_for_effect(
            effect, account_type, str(row["activityType"])
        ),
        "activityDate": row["date"],
        "amount": float(abs(effect)),
        "currency": row["currency"],
        "isDraft": False,
        "comment": row.get("comment"),
        "idempotencyKey": row.get("idempotencyKey"),
    }
    if row.get("assetId") is not None:
        payload["assetId"] = row["assetId"]
    payload.update(_external_transfer_fields())
    return payload


def _create_payload(account_id: str, account_type: str, transaction: dict) -> dict:
    amount = Decimal(transaction["amount"])
    if amount > 0:
        kind = "CREDIT" if account_type == "CREDIT_CARD" else "DEPOSIT"
    else:
        kind = "WITHDRAWAL"
    return {
        "accountId": account_id,
        "activityType": kind,
        "activityDate": f"{transaction['date']}T12:00:00Z",
        "amount": float(abs(amount)),
        "currency": "USD",
        "isDraft": False,
        "comment": transaction["description"],
        "idempotencyKey": f"simplefin:{account_id}:{transaction['sourceId']}",
    }


def _validate_reviewed_snapshot(reviewed: dict, snapshot_path: Path) -> None:
    source_accounts, errors = read_snapshot(snapshot_path)
    if list(reviewed.get("institutionErrors") or []) != list(errors):
        raise DecisionError("reviewed plan institution errors differ from its snapshot")
    mapped_ids = {
        account["sourceAccountId"]
        for account in reviewed["accounts"]
        if account.get("status") == "mapped"
    }
    actual = {
        (account.id, txn.id): (
            txn.posted.isoformat(),
            format(txn.amount, "f"),
            normalize_description(txn.description),
        )
        for account in source_accounts
        if account.id in mapped_ids
        for txn in account.transactions
    }
    planned = {
        (account["sourceAccountId"], txn.get("sourceId")): (
            txn["date"],
            txn["amount"],
            txn["normalizedDescription"],
        )
        for account in reviewed["accounts"]
        if account.get("status") == "mapped"
        for txn in account.get("transactions", [])
    }
    if actual != planned:
        raise DecisionError("reviewed plan transaction evidence differs from its snapshot")


def build_application_plan(
    reviewed: dict[str, Any],
    reviewed_path: Path,
    snapshot_path: Path,
    stage_account_map: dict[str, str],
    activities: list[dict[str, Any]],
    app_accounts: list[dict[str, Any]],
    current_values: dict[str, Decimal],
    environment_fingerprint: str,
    *,
    stage_map_path: Path | None = None,
    durable_transfer_groups: dict[TransferKey, str] | None = None,
    manual_decisions: dict[tuple[str, str], dict[str, Any]] | None = None,
    manual_decisions_path: Path | None = None,
    metadata_remediations: list[dict[str, Any]] | None = None,
    metadata_remediations_path: Path | None = None,
    mode: str = "staging-apply-plan",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a sealed plan whose transaction and reconciliation effects net to zero."""
    _validate_reviewed_snapshot(reviewed, snapshot_path)
    generated_at = generated_at or datetime.now(timezone.utc)
    by_account = {str(row["id"]): row for row in app_accounts}
    existing = existing_from_activities(activities)
    by_source: dict[tuple[str, str], list[ExistingTransaction]] = defaultdict(list)
    by_overlap: dict[tuple[str, object, Decimal, str], list[ExistingTransaction]] = (
        defaultdict(list)
    )
    for old in existing:
        if old.source_id:
            by_source[(old.account_id, old.source_id)].append(old)
        by_overlap[
            overlap_key(old.account_id, old.posted, old.amount, old.description)
        ].append(old)

    creates: list[dict] = []
    updates: list[dict] = []
    reconciliation_changes: list[dict] = []
    link_retypes: list[dict] = []
    assertions: list[dict] = []
    manual: list[dict] = []
    monitors: list[dict] = []
    target_ids: set[str] = set()
    cash_flow = ZERO
    reconciliation_effect = ZERO
    income_delta = ZERO
    spending_delta = ZERO
    timed_cash_flow_deltas: list[tuple[str, Decimal, Decimal]] = []
    eligible_accounts: list[dict[str, Any]] = []
    decisions = manual_decisions or {}
    used_decisions: set[tuple[str, str]] = set()

    def held(entry: dict[str, Any]) -> None:
        key = (
            str(entry.get("sourceAccountId") or ""),
            str(entry.get("sourceId") or ""),
        )
        decision = decisions.get(key)
        if decision:
            used_decisions.add(key)
            if decision.get("detectedReason") != entry["reason"]:
                raise DecisionError(
                    f"SimpleFIN ruling reason changed for {entry.get('sourceId')}"
                )
            entry = {
                **entry,
                "decisionStatus": "ruled",
                "ruling": decision["ruling"],
                "rationale": decision["rationale"],
                "decisionEvidence": decision["evidence"],
            }
        else:
            entry = {**entry, "decisionStatus": "unruled"}
        manual.append(entry)

    for account in reviewed["accounts"]:
        status = account.get("status")
        if status in {"monitored", "observed", "excluded"}:
            monitors.append({
                "sourceAccountId": account["sourceAccountId"],
                "status": status,
                "assertionAccountId": account.get("assertionAccountId"),
                "sourceBalance": account.get("sourceBalance"),
                "balanceDate": account.get("balanceDate"),
                "balanceAction": account.get("balanceAction"),
            })
            continue
        if status != "mapped":
            held({
                "sourceAccountId": account["sourceAccountId"],
                "reason": f"reviewed-account-{status}",
            })
            continue
        canonical_id = account.get("assertionAccountId")
        target = stage_account_map.get(str(canonical_id))
        if not target or target not in by_account:
            raise DecisionError(
                f"mapped account has no rebuild identity: {account['sourceAccountId']}"
            )
        target_ids.add(target)
        source_balance = Decimal(account["sourceBalance"])
        current = current_values.get(target)
        for transaction in account.get("transactions", []):
            if transaction["status"] == "review":
                held({
                    "sourceAccountId": account["sourceAccountId"],
                    "sourceId": transaction.get("sourceId"),
                    "reason": transaction.get("reason") or "review",
                })
        pending: list[dict] = []
        for transaction in account.get("transactions", []):
            if transaction["status"] == "review":
                continue
            if transaction["status"] != "planned":
                continue
            source_id = transaction.get("sourceId")
            if by_source.get((target, source_id)):
                decision_key = (str(account["sourceAccountId"]), str(source_id))
                decision = decisions.get(decision_key)
                if decision and decision["ruling"] == "pair-existing":
                    idempotency_key = f"simplefin:{target}:{source_id}"
                    persisted = next(
                        (
                            row
                            for row in activities
                            if row.get("idempotencyKey") == idempotency_key
                        ),
                        None,
                    )
                    group_id = persisted and persisted.get("sourceGroupId")
                    if not group_id or sum(
                        row.get("sourceGroupId") == group_id for row in activities
                    ) != 2:
                        raise DecisionError(
                            "applied pair-existing decision is not durably linked"
                        )
                    used_decisions.add(decision_key)
                continue
            key = overlap_key(
                target,
                datetime.fromisoformat(transaction["date"]).date(),
                Decimal(transaction["amount"]),
                transaction["description"],
            )
            overlaps = by_overlap.get(key, [])
            if overlaps:
                if len(overlaps) > 1:
                    held({
                        "sourceAccountId": account["sourceAccountId"],
                        "sourceId": source_id,
                        "reason": "late-ambiguous-overlap",
                    })
                continue
            pending.append(transaction)

        if not pending:
            continue
        drift = (
            current - source_balance
            if current is not None
            else None
        )
        balance_ok = drift is not None and abs(drift) <= CENT
        if balance_ok:
            assertions.append({
                "accountId": target,
                "canonicalAccountId": canonical_id,
                "sourceBalance": format(source_balance, "f"),
                "beforeBalance": format(current, "f"),
            })
        eligible_accounts.append({
            "sourceAccountId": account["sourceAccountId"],
            "canonicalAccountId": canonical_id,
            "target": target,
            "accountType": str(by_account[target]["accountType"]),
            "pending": pending,
            "balanceDrift": format(drift, "f") if drift is not None else None,
            "balanceOk": balance_ok,
        })

    all_pending = [
        {
            "sourceAccountId": entry["sourceAccountId"],
            "target": entry["target"],
            "accountType": entry["accountType"],
            "transaction": transaction,
            "balanceDrift": entry["balanceDrift"],
            "balanceOk": entry["balanceOk"],
        }
        for entry in eligible_accounts
        for transaction in entry["pending"]
    ]
    transfer_pairs, ambiguous_transfers = _pair_pending_transfers(
        all_pending, durable_transfer_groups or {}
    )
    reviewed_transactions = {
        (str(account["sourceAccountId"]), str(transaction.get("sourceId"))): (
            account,
            transaction,
        )
        for account in reviewed["accounts"]
        for transaction in account.get("transactions", [])
    }
    existing_links: dict[TransferKey, dict[str, Any]] = {}
    claimed_members = {
        _transfer_key(item)
        for pair in transfer_pairs
        for item in pair
    }
    claimed_activity_ids: set[str] = set()
    for item in all_pending:
        transaction = item["transaction"]
        item_key = _transfer_key(item)
        decision = decisions.get(
            item_key
        )
        if not decision or decision.get("ruling") != "pair-existing":
            continue
        used_decisions.add(
            (str(item["sourceAccountId"]), str(transaction["sourceId"]))
        )
        if decision.get("detectedReason") != "ambiguous-transfer-match":
            raise DecisionError(
                f"pair-existing ruling reason changed for {transaction['sourceId']}"
            )
        counterpart_key = (
            str(decision["counterpartSourceAccountId"]),
            str(decision["counterpartSourceId"]),
        )
        if item_key in claimed_members or counterpart_key in claimed_members:
            raise DecisionError("SimpleFIN transfer member is claimed more than once")
        counterpart = reviewed_transactions.get(counterpart_key)
        if counterpart is None or counterpart[1].get("reason") != "duplicate-overlap":
            raise DecisionError(
                f"pair-existing counterpart is not a reviewed duplicate for "
                f"{transaction['sourceId']}"
            )
        counterpart_account, counterpart_transaction = counterpart
        source_date = datetime.fromisoformat(transaction["date"]).date()
        counterpart_date = datetime.fromisoformat(
            counterpart_transaction["date"]
        ).date()
        if (
            counterpart_transaction.get("status") != "skipped"
            or Decimal(transaction["amount"])
            != -Decimal(counterpart_transaction["amount"])
            or abs((source_date - counterpart_date).days) > 3
            or not _has_transfer_evidence(transaction["description"])
            or not _has_transfer_evidence(counterpart_transaction["description"])
        ):
            raise DecisionError(
                f"pair-existing counterpart evidence is incompatible for "
                f"{transaction['sourceId']}"
            )
        counterpart_target = stage_account_map.get(
            str(counterpart_account.get("assertionAccountId"))
        )
        if not counterpart_target or counterpart_target == item["target"]:
            raise DecisionError(
                f"pair-existing counterpart account is invalid for "
                f"{transaction['sourceId']}"
            )
        candidates = [
            row
            for row in activities
            if row.get("activityType") in INFLOW | OUTFLOW
            if overlap_key(
                str(row.get("accountId")),
                datetime.fromisoformat(str(row.get("date"))).date(),
                _signed(row),
                str(row.get("comment") or ""),
            )
            == overlap_key(
                str(counterpart_target),
                datetime.fromisoformat(counterpart_transaction["date"]).date(),
                Decimal(counterpart_transaction["amount"]),
                counterpart_transaction["description"],
            )
        ]
        if len(candidates) != 1 or candidates[0].get("sourceGroupId"):
            raise DecisionError(
                f"pair-existing counterpart is not unique and unlinked for "
                f"{transaction['sourceId']}"
            )
        activity_id = str(candidates[0].get("id") or "")
        if activity_id in claimed_activity_ids:
            raise DecisionError(
                "pair-existing counterpart or activity is claimed more than once"
            )
        claimed_members.update((item_key, counterpart_key))
        claimed_activity_ids.add(activity_id)
        existing_links[item_key] = {
            "activity": candidates[0],
            "accountType": str(by_account[counterpart_target]["accountType"]),
        }
        ambiguous_transfers.discard(item_key)
    paired_candidate_ids = {
        _transfer_key(item)
        for pair in transfer_pairs
        for item in pair
    }
    ambiguous_transfers.update(
        _transfer_key(item)
        for item in all_pending
        if _transfer_key(item) not in paired_candidate_ids
        and _transfer_key(item) not in existing_links
        and _has_transfer_evidence(item["transaction"]["description"])
    )
    transfer_ids = {
        _transfer_key(item)
        for pair in transfer_pairs
        for item in pair
    }
    transfer_ids.update(existing_links)
    blocked_pair_ids = {
        _transfer_key(item)
        for pair in transfer_pairs
        if not all(member["balanceOk"] for member in pair)
        for item in pair
    }
    transfer_ids -= blocked_pair_ids
    links = []
    for left, right in transfer_pairs:
        if _transfer_key(left) in blocked_pair_ids:
            continue
        links.append({
            "leftKey": (
                f"simplefin:{left['target']}:{left['transaction']['sourceId']}"
            ),
            "rightKey": (
                f"simplefin:{right['target']}:{right['transaction']['sourceId']}"
            ),
            "leftSourceAccountId": left["sourceAccountId"],
            "leftSourceId": left["transaction"]["sourceId"],
            "rightSourceAccountId": right["sourceAccountId"],
            "rightSourceId": right["transaction"]["sourceId"],
        })
    for item in all_pending:
        source_id = item["transaction"]["sourceId"]
        item_key = _transfer_key(item)
        if item_key not in existing_links:
            continue
        decision = decisions[item_key]
        links.append({
            "leftKey": f"simplefin:{item['target']}:{source_id}",
            "rightActivityId": existing_links[item_key]["activity"]["id"],
            "leftSourceAccountId": item["sourceAccountId"],
            "leftSourceId": source_id,
            "rightSourceAccountId": decision["counterpartSourceAccountId"],
            "rightSourceId": decision["counterpartSourceId"],
        })
    for existing in existing_links.values():
        row = existing["activity"]
        effect = _signed(row)
        update = activity_payload(row)
        update["activityType"] = (
            "TRANSFER_IN" if effect > 0 else "TRANSFER_OUT"
        )
        updates.append(update)
        before_income, before_spending = _cash_flow(
            existing["accountType"],
            str(row["activityType"]),
            abs(effect),
        )
        after_income, after_spending = _cash_flow(
            existing["accountType"],
            update["activityType"],
            abs(effect),
        )
        timed_cash_flow_deltas.append((
            str(row["date"])[:10],
            after_income - before_income,
            after_spending - before_spending,
        ))
        link_retypes.append({
            "activityId": row["id"],
            "before": _activity_fingerprint(row),
            "after": _activity_fingerprint(_as_row(update)),
            "rollbackPayload": activity_payload(row),
        })
    for entry in eligible_accounts:
        def entry_key(transaction: dict[str, Any]) -> TransferKey:
            return (
                str(entry["sourceAccountId"]),
                str(transaction["sourceId"]),
            )

        pending = [
            transaction
            for transaction in entry["pending"]
            if entry_key(transaction) not in ambiguous_transfers
            and entry_key(transaction) not in blocked_pair_ids
            and entry["balanceOk"]
        ]
        for transaction in entry["pending"]:
            key = entry_key(transaction)
            if key in ambiguous_transfers:
                held({
                    "sourceAccountId": entry["sourceAccountId"],
                    "sourceId": transaction["sourceId"],
                    "reason": "ambiguous-transfer-match",
                })
            elif (
                key in blocked_pair_ids
                or (
                    not entry["balanceOk"]
                    and key not in existing_links
                )
            ):
                held({
                    "sourceAccountId": entry["sourceAccountId"],
                    "sourceId": transaction["sourceId"],
                    "reason": (
                        "balance-assertion-precondition"
                        if not entry["balanceOk"]
                        else "paired-account-precondition"
                    ),
                    "drift": entry["balanceDrift"],
                })
        if not pending:
            continue
        target = entry["target"]
        net = sum((Decimal(txn["amount"]) for txn in pending), ZERO)
        if net:
            reconciliation = _reconciliation(activities, target)
            if reconciliation is None:
                for txn in pending:
                    held({
                        "sourceAccountId": entry["sourceAccountId"],
                        "sourceId": txn.get("sourceId"),
                        "reason": "missing-reconciliation",
                    })
                continue
            if reconciliation.get("sourceGroupId"):
                raise DecisionError(
                    f"reconciliation activity is linked: {reconciliation['id']}"
                )
            before = _signed(reconciliation)
            after = before - net
            change = {
                "accountId": target,
                "activityId": reconciliation["id"],
                "before": _activity_fingerprint(reconciliation),
                "beforeEffect": format(before, "f"),
                "transactionEffect": format(net, "f"),
                "afterEffect": format(after, "f"),
                "rollbackPayload": activity_payload(reconciliation),
            }
            if abs(after) < Decimal("0.005"):
                for txn in pending:
                    held({
                        "sourceAccountId": entry["sourceAccountId"],
                        "sourceId": txn.get("sourceId"),
                        "reason": "unsafe-reconciliation-delete",
                    })
                continue
            update = _update_payload(
                reconciliation, after, entry["accountType"]
            )
            updates.append(update)
            change["action"] = "update"
            change["after"] = update
            after_income, after_spending = _cash_flow(
                entry["accountType"], update["activityType"], abs(after)
            )
            before_income, before_spending = _activity_cash_flow(
                entry["accountType"], reconciliation
            )
            timed_cash_flow_deltas.append((
                str(reconciliation["date"])[:10],
                after_income - before_income,
                after_spending - before_spending,
            ))
            reconciliation_changes.append(change)
            reconciliation_effect -= net
        account_creates = [
            _create_payload(
                target, entry["accountType"], transaction
            )
            for transaction in pending
        ]
        for payload, transaction in zip(account_creates, pending):
            if entry_key(transaction) in transfer_ids:
                amount = Decimal(transaction["amount"])
                payload["activityType"] = (
                    "TRANSFER_IN" if amount > 0 else "TRANSFER_OUT"
                )
                payload.update(_external_transfer_fields())
            income, spending = _cash_flow(
                entry["accountType"],
                payload["activityType"],
                Decimal(str(payload["amount"])),
            )
            income_delta += income
            spending_delta += spending
        creates.extend(account_creates)
        cash_flow += net

    original_by_id = {str(row["id"]): row for row in activities}
    metadata_finalizations: dict[str, dict[str, Any]] = {}

    def add_metadata_finalization(
        original: dict[str, Any],
        desired_payload: dict[str, Any],
        *,
        kind: str,
    ) -> None:
        activity_id = str(original["id"])
        payload = dict(desired_payload)
        payload["id"] = activity_id
        payload["metadata"] = canonicalize_metadata(payload.get("metadata"))
        rollback_payload = activity_payload(original)
        bulk_payload = dict(payload)
        bulk_payload["metadata"] = None
        rollback_bulk_payload = dict(rollback_payload)
        rollback_bulk_payload["metadata"] = None
        original_fingerprint = activity_semantic_fingerprint(original)
        bulk_fingerprint = activity_semantic_fingerprint(bulk_payload)
        linked = _link_for_activity(
            links, activity_id, payload.get("idempotencyKey")
        )
        post_link_payload = _post_link_finalization_payload(payload, linked)
        forward_fingerprints = {bulk_fingerprint}
        if kind == "remediation":
            forward_fingerprints.add(original_fingerprint)
        metadata_finalizations[activity_id] = {
            "activityId": activity_id,
            "idempotencyKey": payload.get("idempotencyKey"),
            "kind": kind,
            "originalFingerprint": original_fingerprint,
            "bulkFingerprint": bulk_fingerprint,
            "rollbackBulkFingerprint": activity_semantic_fingerprint(
                rollback_bulk_payload
            ),
            "expectedFingerprint": activity_semantic_fingerprint(payload),
            "expectedLinkGroupFingerprint": (
                link_group_fingerprint(linked) if linked else None
            ),
            "expectedPostLinkFingerprint": activity_semantic_fingerprint(
                post_link_payload
            ),
            "forwardFingerprints": sorted(forward_fingerprints),
            "payload": payload,
            "rollbackPayload": rollback_payload,
        }

    for payload in updates:
        if canonicalize_metadata(payload.get("metadata")) == "{}":
            continue
        original = original_by_id.get(str(payload["id"]))
        if original is None:
            raise DecisionError("metadata finalization update has no original activity")
        add_metadata_finalization(original, payload, kind="bulk-update")

    for remediation in metadata_remediations or []:
        activity_id = str(remediation.get("activityId") or "")
        row = original_by_id.get(activity_id)
        if row is None:
            raise DecisionError("metadata remediation activity is missing")
        if activity_semantic_fingerprint(row) != remediation.get(
            "originalFingerprint"
        ):
            raise DecisionError(
                "metadata remediation activity differs from reviewed evidence"
            )
        payload = activity_payload(row)
        payload["metadata"] = canonicalize_metadata(
            remediation.get("desiredMetadata")
        )
        if canonicalize_metadata(row.get("metadata")) == payload["metadata"]:
            raise DecisionError("metadata remediation is already applied")
        add_metadata_finalization(row, payload, kind="remediation")
        target_ids.add(str(row["accountId"]))

    operations = {
        "creates": creates,
        "updates": updates,
        "deleteIds": [],
        "metadataFinalizations": sorted(
            metadata_finalizations.values(),
            key=lambda row: row["activityId"],
        ),
    }
    target_to_canonical = {
        target: canonical_id for canonical_id, target in stage_account_map.items()
    }
    missing_finalization_accounts = {
        finalization["payload"]["accountId"]
        for finalization in operations["metadataFinalizations"]
        if finalization["payload"]["accountId"] not in target_to_canonical
    }
    if missing_finalization_accounts:
        raise DecisionError(
            "metadata finalization account lacks a canonical mapping"
        )
    decision_evidence = (
        manual_decision_evidence_binding(manual_decisions_path)
        if manual_decisions_path
        else None
    )
    operation_by_key = {
        payload["idempotencyKey"]: payload
        for payload in creates + updates
        if payload.get("idempotencyKey")
    }
    operation_by_id = {
        payload["id"]: payload for payload in updates if payload.get("id")
    }

    def link_semantics(link: dict[str, Any], side: str) -> dict[str, Any]:
        payload = (
            operation_by_key.get(link.get(f"{side}Key"))
            or operation_by_id.get(link.get(f"{side}ActivityId"))
        )
        if payload is None:
            raise DecisionError("portable transfer intent has no operation payload")
        return {
            "subtype": normalize_subtype(payload.get("subtype")),
            "metadata": metadata_with_external_flow(
                payload.get("metadata"), False
            ),
        }

    reconciliation_ids = {
        change["activityId"] for change in reconciliation_changes
    }
    link_retype_ids = {change["activityId"] for change in link_retypes}
    portable_intent = {
        "creates": sorted(
            (
                {
                    "canonicalAccountId": target_to_canonical[payload["accountId"]],
                    "activityType": payload["activityType"],
                    "activityDate": payload["activityDate"],
                    "amount": str(payload["amount"]),
                    "subtype": normalize_subtype(payload.get("subtype")),
                    "metadata": canonicalize_metadata(payload.get("metadata")),
                    "descriptionHash": plan_fingerprint(
                        normalize_description(str(payload.get("comment") or ""))
                    ),
                    "sourceId": next(
                        transaction["sourceId"]
                        for entry in eligible_accounts
                        for transaction in entry["pending"]
                        if payload["idempotencyKey"]
                        == f"simplefin:{entry['target']}:{transaction['sourceId']}"
                    ),
                }
                for payload in creates
            ),
            key=lambda row: (
                str(row["canonicalAccountId"]),
                str(row["sourceId"]),
            ),
        ),
        "updates": sorted(
            (
                {
                    "canonicalAccountId": target_to_canonical[payload["accountId"]],
                    "role": (
                        "reconciliation"
                        if payload["id"] in reconciliation_ids
                        else "link-retype"
                        if payload["id"] in link_retype_ids
                        else "other"
                    ),
                    "activityType": payload["activityType"],
                    "activityDate": payload["activityDate"],
                    "amount": str(payload["amount"]),
                    "subtype": normalize_subtype(payload.get("subtype")),
                    "metadata": canonicalize_metadata(payload.get("metadata")),
                }
                for payload in updates
            ),
            key=lambda row: (
                str(row["canonicalAccountId"]),
                str(row["role"]),
                str(row["activityDate"]),
                str(row["activityType"]),
                str(row["amount"]),
                str(row["subtype"]),
                str(row["metadata"]),
            ),
        ),
        "metadataFinalizations": sorted(
            (
                {
                    "canonicalAccountId": target_to_canonical[
                        finalization["payload"]["accountId"]
                    ],
                    "idempotencyKey": finalization.get("idempotencyKey"),
                    "activityType": finalization["payload"]["activityType"],
                    "activityDate": finalization["payload"]["activityDate"],
                    "amount": str(finalization["payload"]["amount"]),
                    "subtype": normalize_subtype(
                        finalization["payload"].get("subtype")
                    ),
                    "metadata": canonicalize_metadata(
                        finalization["payload"].get("metadata")
                    ),
                }
                for finalization in operations["metadataFinalizations"]
            ),
            key=lambda row: (
                str(row["canonicalAccountId"]),
                str(row["idempotencyKey"]),
                str(row["activityDate"]),
                str(row["activityType"]),
                str(row["amount"]),
                str(row["subtype"]),
                str(row["metadata"]),
            ),
        ),
        "links": sorted(
            (
                {
                    "leftSourceAccountId": link["leftSourceAccountId"],
                    "leftSourceId": link["leftSourceId"],
                    "rightSourceAccountId": link["rightSourceAccountId"],
                    "rightSourceId": link["rightSourceId"],
                    "left": link_semantics(link, "left"),
                    "right": link_semantics(link, "right"),
                }
                for link in links
            ),
            key=lambda row: (
                str(row["leftSourceAccountId"]),
                str(row["leftSourceId"]),
                str(row["rightSourceAccountId"]),
                str(row["rightSourceId"]),
            ),
        ),
        "reconciliations": sorted(
            (
                {
                    "canonicalAccountId": target_to_canonical[change["accountId"]],
                    "action": change["action"],
                    "beforeEffect": change["beforeEffect"],
                    "transactionEffect": change["transactionEffect"],
                    "afterEffect": change["afterEffect"],
                }
                for change in reconciliation_changes
            ),
            key=lambda row: str(row["canonicalAccountId"]),
        ),
        "manual": sorted(
            (
                {
                    key: row.get(key)
                    for key in (
                        "sourceAccountId",
                        "sourceId",
                        "reason",
                        "decisionStatus",
                        "ruling",
                    )
                }
                for row in manual
            ),
            key=lambda row: (
                str(row.get("sourceAccountId")),
                str(row.get("sourceId")),
            ),
        ),
        "metadataRemediationSha256": (
            sha256_file(metadata_remediations_path)
            if metadata_remediations_path
            else None
        ),
        "decisionEvidence": decision_evidence,
    }
    unused_decisions = set(decisions) - used_decisions
    if unused_decisions:
        raise DecisionError(
            f"{len(unused_decisions)} SimpleFIN manual decision(s) did not match "
            "the current reviewed evidence"
        )
    activity_dates = sorted(
        str(payload["activityDate"])[:10] for payload in creates
    )
    if not activity_dates:
        activity_dates = sorted(
            str(finalization["payload"]["activityDate"])[:10]
            for finalization in operations["metadataFinalizations"]
        )
    if activity_dates:
        for on, income, spending in timed_cash_flow_deltas:
            if activity_dates[0] <= on <= activity_dates[-1]:
                income_delta += income
                spending_delta += spending
    document = {
        "schemaVersion": 3,
        "mode": mode,
        "generatedAt": generated_at.isoformat(),
        "environmentFingerprint": environment_fingerprint,
        "evidence": {
            "reviewedPlan": {
                "path": str(reviewed_path.resolve()),
                "sha256": sha256_file(reviewed_path),
            },
            "snapshot": {
                "path": str(snapshot_path.resolve()),
                "sha256": sha256_file(snapshot_path),
            },
            **({
                "rebuildAccountMap": {
                    "path": str(stage_map_path.resolve()),
                    "sha256": sha256_file(stage_map_path),
                }
            } if stage_map_path else {}),
            **({
                "manualDecisions": {
                    "path": str(manual_decisions_path.resolve()),
                    "sha256": sha256_file(manual_decisions_path),
                }
            } if manual_decisions_path else {}),
            **({
                "metadataRemediations": {
                    "path": str(metadata_remediations_path.resolve()),
                    "sha256": sha256_file(metadata_remediations_path),
                }
            } if metadata_remediations_path else {}),
        },
        "ledgerFingerprint": ledger_fingerprint(activities, target_ids),
        "ledgerAccountIds": sorted(target_ids),
        "operations": operations,
        "decisionEvidence": decision_evidence,
        "portableIntent": portable_intent,
        "intentFingerprint": plan_fingerprint(portable_intent),
        "links": links,
        "spendingWindow": (
            {
                "startDate": f"{activity_dates[0]}T00:00:00Z",
                "endDate": f"{activity_dates[-1]}T23:59:59Z",
            }
            if activity_dates
            else None
        ),
        "reconciliations": reconciliation_changes,
        "linkRetypes": link_retypes,
        "assertions": assertions,
        "monitors": monitors,
        "manual": manual,
        "impact": {
            "transactionCashFlow": format(cash_flow, "f"),
            "reconciliationCashFlow": "0",
            "reconciliationBalanceEffect": format(reconciliation_effect, "f"),
            "expectedCashFlowDelta": _money(income_delta - spending_delta),
            "incomeDelta": _money(income_delta),
            "spendingDelta": _money(spending_delta),
            "expectedNetWorthDelta": "0",
        },
    }
    document["expectedPostLedgerFingerprint"] = expected_post_fingerprint(
        activities, target_ids, operations, links
    )
    document["planFingerprint"] = plan_fingerprint(document)
    return document


def validate_plan_seal(plan: dict[str, Any]) -> None:
    if plan.get("schemaVersion") != 3:
        raise DecisionError("SimpleFIN application plan schemaVersion 3 is required")
    operations = plan.get("operations") or {}
    if not isinstance(operations.get("metadataFinalizations"), list):
        raise DecisionError("SimpleFIN plan lacks explicit metadata finalizations")
    for finalization in operations["metadataFinalizations"]:
        kind = finalization.get("kind")
        if kind not in {"bulk-update", "remediation"}:
            raise DecisionError("metadata finalization kind is invalid")
        original = finalization.get("originalFingerprint")
        bulk = finalization.get("bulkFingerprint")
        rollback_bulk = finalization.get("rollbackBulkFingerprint")
        expected = finalization.get("expectedFingerprint")
        if (
            original != activity_semantic_fingerprint(finalization["rollbackPayload"])
            or bulk
            != activity_semantic_fingerprint({
                **finalization["payload"],
                "metadata": None,
            })
            or rollback_bulk
            != activity_semantic_fingerprint({
                **finalization["rollbackPayload"],
                "metadata": None,
            })
            or expected
            != activity_semantic_fingerprint(finalization["payload"])
        ):
            raise DecisionError("metadata finalization fingerprints are invalid")
        expected_forward = {bulk}
        if kind == "remediation":
            expected_forward.add(original)
        if set(finalization.get("forwardFingerprints") or []) != expected_forward:
            raise DecisionError("metadata finalization forward states are invalid")
        link = _link_for_activity(
            plan.get("links", []),
            str(finalization["activityId"]),
            finalization.get("idempotencyKey"),
        )
        link_group = link_group_fingerprint(link) if link else None
        if finalization.get("expectedLinkGroupFingerprint") != link_group:
            raise DecisionError("metadata finalization link binding is invalid")
        post_link = _post_link_finalization_payload(
            finalization["payload"], link
        )
        if (
            finalization.get("expectedPostLinkFingerprint")
            != activity_semantic_fingerprint(post_link)
        ):
            raise DecisionError("metadata finalization post-link state is invalid")
    expected = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    if plan.get("planFingerprint") != expected:
        raise DecisionError("SimpleFIN application plan fingerprint is invalid")
    for evidence in plan["evidence"].values():
        path = Path(evidence["path"])
        if sha256_file(path) != evidence["sha256"]:
            raise DecisionError(f"SimpleFIN evidence hash changed: {path.name}")


def write_application_plan(data_dir: Path, plan: dict[str, Any]) -> Path:
    stamp = datetime.fromisoformat(plan["generatedAt"]).strftime(
        "%Y-%m-%d-%H%M%S-%f"
    )
    path = data_dir / "normalized" / "simplefin" / f"apply-plan-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path
