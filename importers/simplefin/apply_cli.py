"""Plan or apply SimpleFIN transactions only to an isolated rebuild instance."""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import time
import urllib.error
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.rebuild.accounts import (
    build_account_plan,
    build_aliases,
    load_canonical_accounts,
)
from importers.rebuild.current import account_values
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import (
    instance_fingerprint,
    plan_fingerprint,
    validate_apply_target,
    validate_private_output,
)

from .application import (
    activity_semantic_fingerprint,
    build_application_plan,
    canonicalize_metadata,
    expected_post_fingerprint,
    ledger_fingerprint,
    link_group_fingerprint,
    load_manual_decisions,
    manual_decision_evidence_binding,
    metadata_with_external_flow,
    normalize_subtype,
    sha256_file,
    validate_plan_seal,
    write_application_plan,
)

TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    ConnectionError,
)
CENT = Decimal("0.01")
HEALTH_MIN_POLLS = 4
HEALTH_MAX_POLLS = 8


def _password() -> str:
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    if not password:
        raise DecisionError("WEALTHFOLIO_PASSWORD is required for rebuild staging")
    return password


def _load_metadata_remediations(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionError(f"invalid metadata remediation evidence: {exc}") from None
    if document.get("schemaVersion") != 1 or not isinstance(
        document.get("activities"), list
    ):
        raise DecisionError("metadata remediation evidence schema is invalid")
    remediations = document["activities"]
    activity_ids = [str(row.get("activityId") or "") for row in remediations]
    if (
        not remediations
        or any(not activity_id for activity_id in activity_ids)
        or len(set(activity_ids)) != len(activity_ids)
    ):
        raise DecisionError("metadata remediation activity identities are invalid")
    for row in remediations:
        fingerprint = str(row.get("originalFingerprint") or "")
        if len(fingerprint) != 64:
            raise DecisionError("metadata remediation fingerprint is invalid")
        desired = row.get("desiredMetadata")
        if not isinstance(desired, dict):
            raise DecisionError("metadata remediation metadata must be an object")
        canonicalize_metadata(desired)
    return remediations


def _current_values(client, accounts: list[dict]) -> dict[str, Decimal]:
    return account_values(client, accounts)


def _portfolio_values(client, accounts: list[dict]) -> dict[str, Any]:
    account_values_by_id = _current_values(client, accounts)
    alternative_values: dict[str, Decimal] = {}
    for holding in client.get("/alternative-holdings") or []:
        holding_id = str(holding.get("id") or "")
        if not holding_id or holding_id in alternative_values:
            raise DecisionError("alternative holding identity is missing or duplicated")
        value = Decimal(str(holding.get("marketValue") or 0))
        alternative_values[holding_id] = (
            -value if holding.get("kind") == "liability" else value
        )
    return {
        "accountValues": account_values_by_id,
        "alternativeValues": alternative_values,
        "globalTotal": (
            sum(account_values_by_id.values(), Decimal())
            + sum(alternative_values.values(), Decimal())
        ),
    }


def _portfolio_value_report(
    before: dict[str, Any],
    after: dict[str, Any],
    touched_account_ids: set[str],
) -> dict[str, Decimal]:
    before_accounts = before["accountValues"]
    after_accounts = after["accountValues"]
    if any(
        account_id not in before_accounts or account_id not in after_accounts
        for account_id in touched_account_ids
    ):
        raise DecisionError("touched account is missing from portfolio valuation")
    touched_deltas = {
        account_id: after_accounts[account_id] - before_accounts[account_id]
        for account_id in touched_account_ids
    }
    if any(abs(delta) > CENT for delta in touched_deltas.values()):
        raise DecisionError("SimpleFIN apply changed a touched account value")

    before_alternatives = before["alternativeValues"]
    after_alternatives = after["alternativeValues"]
    if before_alternatives.keys() != after_alternatives.keys():
        raise DecisionError("alternative holding set changed during SimpleFIN apply")
    alternative_deltas = {
        holding_id: after_alternatives[holding_id] - before_alternatives[holding_id]
        for holding_id in before_alternatives
    }
    if any(abs(delta) > CENT for delta in alternative_deltas.values()):
        raise DecisionError("alternative holding value changed during SimpleFIN apply")

    all_account_ids = before_accounts.keys() | after_accounts.keys()
    untouched_delta = sum(
        (
            after_accounts.get(account_id, Decimal())
            - before_accounts.get(account_id, Decimal())
            for account_id in all_account_ids - touched_account_ids
        ),
        Decimal(),
    )
    touched_delta = sum(touched_deltas.values(), Decimal())
    alternative_delta = sum(alternative_deltas.values(), Decimal())
    global_delta = after["globalTotal"] - before["globalTotal"]
    unexplained = global_delta - untouched_delta
    if abs(unexplained) > CENT:
        raise DecisionError(
            f"SimpleFIN apply has unexplained net-worth delta {unexplained}"
        )
    return {
        "globalBefore": before["globalTotal"],
        "globalAfter": after["globalTotal"],
        "globalDifference": global_delta,
        "touchedAccountDifference": touched_delta,
        "untouchedAccountCacheDifference": untouched_delta,
        "alternativeHoldingsDifference": alternative_delta,
        "unexplainedResidual": unexplained,
    }


def _spending_totals(client, plan: dict) -> dict[str, Decimal] | None:
    window = plan.get("spendingWindow")
    if not window:
        return None
    settings = client.get("/spending/settings") or {}
    if not settings.get("enabled") or not settings.get("accountIds"):
        return None
    report = client.post("/spending/report", window)
    current = report["current"]
    return {
        "income": Decimal(str(current.get("income") or 0)),
        "spending": Decimal(str(current.get("outflow") or 0)),
    }


def _core_matches(row: dict, payload: dict) -> bool:
    return (
        row.get("accountId") == payload.get("accountId")
        and row.get("activityType") == payload.get("activityType")
        and str(row.get("date") or "")[:10]
        == str(payload.get("activityDate") or "")[:10]
        and Decimal(str(row.get("amount") or 0))
        == Decimal(str(payload.get("amount") or 0))
        and row.get("idempotencyKey") == payload.get("idempotencyKey")
        and normalize_subtype(row.get("subtype"))
        == normalize_subtype(payload.get("subtype"))
        and canonicalize_metadata(row.get("metadata"))
        == canonicalize_metadata(payload.get("metadata"))
    )


def _finalization_activity(rows: list[dict], operation: dict) -> dict | None:
    activity_id = operation["activityId"]
    idempotency_key = operation.get("idempotencyKey")
    return next(
        (
            row
            for row in rows
            if str(row.get("id")) == activity_id
            or idempotency_key
            and row.get("idempotencyKey") == idempotency_key
        ),
        None,
    )


def _put_metadata_finalization(
    client,
    operation: dict,
    *,
    payload_key: str = "payload",
    expected_key: str = "expectedFingerprint",
    allowed_fingerprints: set[str] | None = None,
) -> None:
    payload = operation[payload_key]
    expected = operation[expected_key]
    allowed = set(allowed_fingerprints or ())
    allowed.add(expected)
    last_error = None
    for _ in range(4):
        rows = _read_activities_for_rollback(client)
        current = _finalization_activity(rows, operation)
        if current is None:
            raise DecisionError("metadata finalization activity is missing")
        current_fingerprint = activity_semantic_fingerprint(current)
        if current_fingerprint == expected:
            return
        if current_fingerprint not in allowed:
            raise DecisionError(
                "metadata finalization activity differs from its sealed state"
            )
        try:
            response = client.put("/activities", payload)
        except TRANSPORT_ERRORS as exc:
            last_error = exc
            continue
        if isinstance(response, dict) and response.get("id"):
            if activity_semantic_fingerprint(response) != expected:
                raise DecisionError(
                    "metadata finalization response differs from sealed semantics"
                )
        rows = _read_activities_for_rollback(client)
        persisted = _finalization_activity(rows, operation)
        if (
            persisted is not None
            and activity_semantic_fingerprint(persisted) == expected
        ):
            return
    raise DecisionError(
        "metadata finalization could not confirm persisted semantics"
    ) from last_error


def _already_applied(plan: dict, rows: list[dict]) -> bool:
    by_id = {row["id"]: row for row in rows}
    by_key = {
        row.get("idempotencyKey"): row
        for row in rows
        if row.get("idempotencyKey")
    }
    operations = plan["operations"]
    linked_keys = {
        link[field]
        for link in plan.get("links", [])
        for field in ("leftKey", "rightKey")
        if link.get(field)
    }
    linked_ids = {
        link[field]
        for link in plan.get("links", [])
        for field in ("leftActivityId", "rightActivityId")
        if link.get(field)
    }

    def persisted_payload(payload: dict) -> dict:
        if (
            payload.get("idempotencyKey") not in linked_keys
            and payload.get("id") not in linked_ids
        ):
            return payload
        expected = dict(payload)
        expected["metadata"] = metadata_with_external_flow(
            payload.get("metadata"), False
        )
        return expected

    linked_group_fingerprints: dict[str, str] = {}
    links_match = True
    for link in plan.get("links", []):
        left = (
            by_key.get(link.get("leftKey"))
            or by_id.get(link.get("leftActivityId"))
        )
        right = (
            by_key.get(link.get("rightKey"))
            or by_id.get(link.get("rightActivityId"))
        )
        if not (
            left
            and right
            and left["id"] != right["id"]
            and left.get("sourceGroupId")
            and left.get("sourceGroupId") == right.get("sourceGroupId")
        ):
            links_match = False
            break
        group_id = str(left["sourceGroupId"])
        actual_members = {
            str(row["id"])
            for row in rows
            if str(row.get("sourceGroupId") or "") == group_id
        }
        expected_members = {str(left["id"]), str(right["id"])}
        if actual_members != expected_members:
            links_match = False
            break
        group_fingerprint = link_group_fingerprint(link)
        if any(
            activity_id in linked_group_fingerprints
            and linked_group_fingerprints[activity_id] != group_fingerprint
            for activity_id in expected_members
        ):
            links_match = False
            break
        linked_group_fingerprints.update({
            activity_id: group_fingerprint
            for activity_id in expected_members
        })

    def finalization_matches(finalization: dict) -> bool:
        persisted = _finalization_activity(rows, finalization)
        if persisted is None:
            return False
        normalized = dict(persisted)
        actual_link_group = linked_group_fingerprints.get(str(persisted["id"]))
        if (
            actual_link_group
            != finalization.get("expectedLinkGroupFingerprint")
        ):
            return False
        if actual_link_group:
            normalized["sourceGroupId"] = actual_link_group
        return (
            activity_semantic_fingerprint(normalized)
            == finalization["expectedPostLinkFingerprint"]
        )

    operations_match = (
        all(
            payload["idempotencyKey"] in by_key
            and _core_matches(
                by_key[payload["idempotencyKey"]], persisted_payload(payload)
            )
            for payload in operations["creates"]
        )
        and all(
            payload["id"] in by_id
            and _core_matches(by_id[payload["id"]], persisted_payload(payload))
            for payload in operations["updates"]
        )
        and all(activity_id not in by_id for activity_id in operations["deleteIds"])
        and all(
            finalization_matches(finalization)
            for finalization in operations["metadataFinalizations"]
        )
    )
    actual_post = expected_post_fingerprint(
        rows,
        set(plan["ledgerAccountIds"]),
        {"creates": [], "updates": [], "deleteIds": []},
        plan.get("links", []),
    )
    return (
        operations_match
        and links_match
        and actual_post == plan["expectedPostLedgerFingerprint"]
    )


def _reject_delete_operations(plan: dict) -> None:
    delete_ids = plan.get("operations", {}).get("deleteIds") or []
    if delete_ids:
        raise DecisionError(
            "SimpleFIN application plans with deletes are prohibited until "
            "database-restore rollback is implemented and tested"
        )


def _link_members(rows: list[dict], link: dict) -> tuple[dict | None, dict | None]:
    by_id = {row["id"]: row for row in rows}
    by_key = {
        row.get("idempotencyKey"): row
        for row in rows
        if row.get("idempotencyKey")
    }
    return (
        by_key.get(link.get("leftKey")) or by_id.get(link.get("leftActivityId")),
        by_key.get(link.get("rightKey")) or by_id.get(link.get("rightActivityId")),
    )


def _members_are_linked(left: dict | None, right: dict | None) -> bool:
    return bool(
        left
        and right
        and left.get("sourceGroupId")
        and left.get("sourceGroupId") == right.get("sourceGroupId")
    )


def _read_activities_for_rollback(client, attempts: int = 4) -> list[dict]:
    last_error = None
    for _ in range(attempts):
        try:
            return list(client.iter_activities())
        except TRANSPORT_ERRORS as exc:
            last_error = exc
    raise DecisionError(
        "automatic rollback could not read live activity state"
    ) from last_error


def _unlink_for_rollback(client, link: dict, rows: list[dict]) -> list[dict]:
    current = rows
    last_error = None
    for attempt in range(4):
        if attempt:
            current = _read_activities_for_rollback(client)
        left, right = _link_members(current, link)
        if not _members_are_linked(left, right):
            return current
        try:
            client.post(
                "/activities/unlink",
                {"activityAId": left["id"], "activityBId": right["id"]},
            )
        except TRANSPORT_ERRORS as exc:
            last_error = exc
        current = _read_activities_for_rollback(client)
        left, right = _link_members(current, link)
        if not _members_are_linked(left, right):
            return current
    raise DecisionError(
        "automatic rollback could not confirm transfer unlink"
    ) from last_error


def _rollback(client, plan: dict) -> None:
    rows = _read_activities_for_rollback(client)
    by_id = {row["id"]: row for row in rows}
    by_key = {
        row.get("idempotencyKey"): row
        for row in rows
        if row.get("idempotencyKey")
    }
    created_keys = {
        payload["idempotencyKey"] for payload in plan["operations"]["creates"]
    }
    for link in plan.get("links", []):
        rows = _unlink_for_rollback(client, link, rows)
    by_id = {row["id"]: row for row in rows}
    by_key = {
        row.get("idempotencyKey"): row
        for row in rows
        if row.get("idempotencyKey")
    }
    delete_ids = [by_key[key]["id"] for key in created_keys if key in by_key]
    updates = []
    creates = []
    reversible_updates = plan["reconciliations"] + plan.get("linkRetypes", [])
    for change in reversible_updates:
        original = dict(change["rollbackPayload"])
        if change["activityId"] in by_id:
            updates.append(original)
        else:
            original.pop("id", None)
            creates.append(original)
    result = None
    try:
        result = client.save_activities(
            creates=creates, updates=updates, delete_ids=delete_ids
        )
    except TRANSPORT_ERRORS:
        pass
    if result is not None:
        if result.get("errors"):
            raise DecisionError(str(result["errors"][0]))
    for finalization in plan["operations"]["metadataFinalizations"]:
        _put_metadata_finalization(
            client,
            finalization,
            payload_key="rollbackPayload",
            expected_key="originalFingerprint",
            allowed_fingerprints={
                finalization["expectedFingerprint"],
                finalization["bulkFingerprint"],
                finalization["rollbackBulkFingerprint"],
            },
        )
    try:
        client.post("/portfolio/recalculate", {})
    except TRANSPORT_ERRORS:
        pass
    restored = _read_activities_for_rollback(client)
    restored_by_id = {row["id"]: row for row in restored}
    restored_by_key = {
        row.get("idempotencyKey"): row
        for row in restored
        if row.get("idempotencyKey")
    }
    if any(key in restored_by_key for key in created_keys):
        raise DecisionError(
            "automatic rollback left SimpleFIN activities; recreate staging"
        )
    for change in reversible_updates:
        expected = change["rollbackPayload"]
        actual = (
            restored_by_id.get(change["activityId"])
            or restored_by_key.get(expected.get("idempotencyKey"))
        )
        if actual is None or not _core_matches(actual, expected):
            raise DecisionError(
                "automatic rollback did not restore reconciliation; recreate staging"
            )
    restored_fingerprint = ledger_fingerprint(
        restored, set(plan["ledgerAccountIds"])
    )
    if restored_fingerprint != plan["ledgerFingerprint"]:
        raise DecisionError(
            "automatic rollback did not restore the sealed pre-ledger state"
        )


def _validate_plan_environment(client, base_url: str, plan: dict) -> None:
    if instance_fingerprint(client, base_url) != plan["environmentFingerprint"]:
        raise DecisionError("sealed plan belongs to a different staging instance")


def _health_summary(client, *, refresh: bool = False) -> dict:
    status = (
        client.post("/health/check", {})
        if refresh
        else client.get("/health/status")
    ) or {}
    issues = status.get("issues") or []
    non_info = []
    non_info_keys = []
    non_info_issues = []
    non_info_affected: dict[str, set[str]] = {}
    non_info_activities: dict[str, set[str]] = {}
    for issue in issues:
        severity = str(
            issue.get("severity")
            or issue.get("level")
            or issue.get("status")
            or ""
        ).upper()
        if severity not in {"INFO", "INFORMATIONAL"}:
            code = str(issue.get("code") or "unknown")
            affected_ids = sorted(
                str(item["id"])
                for item in issue.get("affectedItems") or []
                if isinstance(item, dict) and item.get("id")
            )
            activity_ids = sorted({
                str(entity.get("id")).removeprefix("ungrouped:")
                for diagnostic in issue.get("diagnostics") or []
                for entity in diagnostic.get("entities") or []
                if entity.get("kind") == "transferGroup"
                and str(entity.get("id") or "").startswith("ungrouped:")
            })
            data_hash = str(issue.get("dataHash") or "")
            identity = (
                f"{code}:{data_hash}"
                if data_hash
                else f"{code}:{plan_fingerprint({'affectedIds': affected_ids})}"
            )
            non_info.append(code)
            non_info_affected.setdefault(code, set()).update(affected_ids)
            non_info_activities.setdefault(code, set()).update(activity_ids)
            non_info_keys.append(identity)
            non_info_issues.append({
                "identity": identity,
                "code": code,
                "affectedIds": affected_ids,
                "activityIds": activity_ids,
            })
    return {
        "issueCount": len(issues),
        "nonInfoCount": len(non_info),
        "nonInfoCodes": sorted(non_info),
        "nonInfoCodeCounts": dict(sorted(Counter(non_info).items())),
        "nonInfoAffectedIds": {
            code: sorted(activity_ids)
            for code, activity_ids in sorted(non_info_affected.items())
        },
        "nonInfoActivityIds": {
            code: sorted(activity_ids)
            for code, activity_ids in sorted(non_info_activities.items())
        },
        "nonInfoKeys": sorted(non_info_keys),
        "nonInfoIssues": sorted(
            non_info_issues,
            key=lambda issue: (
                issue["identity"],
                issue["affectedIds"],
                issue["activityIds"],
            ),
        ),
    }


def _health_issue_multiset(summary: dict) -> Counter:
    return Counter(
        (
            issue["identity"],
            tuple(issue.get("affectedIds", [])),
            tuple(issue.get("activityIds", [])),
        )
        for issue in summary.get("nonInfoIssues", [])
    )


def _stable_health_summary(client) -> dict:
    summary = {}
    previous_signature = None
    stable_reads = 0
    for attempt in range(HEALTH_MAX_POLLS):
        summary = _health_summary(client, refresh=True)
        signature = tuple(sorted(_health_issue_multiset(summary).items()))
        stable_reads = stable_reads + 1 if signature == previous_signature else 1
        previous_signature = signature
        if attempt + 1 >= HEALTH_MIN_POLLS and stable_reads >= 2:
            return summary
        if attempt + 1 < HEALTH_MAX_POLLS:
            time.sleep(1)
    raise DecisionError("Wealthfolio health did not settle")


def _plan_link_repair_activity_ids(client, plan: dict) -> set[str]:
    if not plan.get("links"):
        return set()
    rows = list(client.iter_activities())
    by_key = {
        row.get("idempotencyKey"): row["id"]
        for row in rows
        if row.get("idempotencyKey")
    }
    touched = set()
    for link in plan.get("links", []):
        for id_key, key_key in (
            ("leftActivityId", "leftKey"),
            ("rightActivityId", "rightKey"),
        ):
            activity_id = link.get(id_key) or by_key.get(link.get(key_key))
            if activity_id:
                touched.add(str(activity_id))
    return touched


def _assert_health_not_degraded(client, before: dict, plan: dict) -> dict:
    after = _stable_health_summary(client)
    new_issues = _health_issue_multiset(after) - _health_issue_multiset(before)
    new_non_info_count = sum(new_issues.values())
    if new_non_info_count:
        raise DecisionError(
            f"Wealthfolio health gained {new_non_info_count} non-INFO issue(s)"
        )
    repair_ids = _plan_link_repair_activity_ids(client, plan) | {
        finalization["activityId"]
        for finalization in plan["operations"].get("metadataFinalizations", [])
    }
    remaining_repairs = repair_ids & {
        activity_id
        for activity_ids in after.get("nonInfoActivityIds", {}).values()
        for activity_id in activity_ids
    }
    if remaining_repairs:
        raise DecisionError("planned repair activities remain unhealthy")
    if repair_ids and "transfer_incomplete" in after.get("nonInfoCodes", []):
        raise DecisionError("planned transfer health repairs did not clear")
    return {
        "before": before,
        "after": after,
        "newNonInfoCount": new_non_info_count,
        "newNonInfoIssues": [],
    }


def _assert_health_repairable_by_links(client, plan: dict) -> dict:
    summary = _stable_health_summary(client)
    unsupported_codes = set(summary.get("nonInfoCodes", [])) - {
        "transfer_incomplete"
    }
    if unsupported_codes:
        raise DecisionError(
            "production health has unrelated non-INFO issue(s): "
            + ", ".join(sorted(unsupported_codes))
        )
    transfer_issues = [
        issue
        for issue in summary.get("nonInfoIssues", [])
        if issue["code"] == "transfer_incomplete"
    ]
    repair_ids = _plan_link_repair_activity_ids(client, plan) | {
        finalization["activityId"]
        for finalization in plan["operations"].get("metadataFinalizations", [])
    }
    if any(not issue.get("activityIds") for issue in transfer_issues):
        raise DecisionError(
            "production transfer health issue has no affected activity identity"
        )
    incomplete_ids = {
        activity_id
        for issue in transfer_issues
        for activity_id in issue["activityIds"]
    }
    if not incomplete_ids.issubset(repair_ids):
        raise DecisionError("production health has issues this plan cannot repair")
    return summary


def _validate_staging_receipt(
    staging_plan_path: Path, receipt_path: Path
) -> tuple[dict, dict]:
    staging_plan = json.loads(staging_plan_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    validate_plan_seal(staging_plan)
    if staging_plan.get("mode") != "staging-apply-plan":
        raise DecisionError("promotion requires a sealed staging application plan")
    _reject_delete_operations(staging_plan)
    if receipt.get("schemaVersion") != 5 or receipt.get("status") != "applied":
        raise DecisionError("promotion requires a successful schemaVersion 5 receipt")
    if (
        receipt.get("applicationPlanSha256") != sha256_file(staging_plan_path)
        or receipt.get("planFingerprint") != staging_plan["planFingerprint"]
        or receipt.get("intentFingerprint") != staging_plan["intentFingerprint"]
        or receipt.get("environmentFingerprint")
        != staging_plan["environmentFingerprint"]
        or receipt.get("preLedgerFingerprint") != staging_plan["ledgerFingerprint"]
        or receipt.get("postLedgerFingerprint")
        != staging_plan["expectedPostLedgerFingerprint"]
        or receipt.get("decisionEvidence") != staging_plan.get("decisionEvidence")
    ):
        raise DecisionError("staging receipt does not exactly seal the staging plan")
    net_worth = receipt.get("netWorth") or {}
    required_net_worth = {
        "globalBefore",
        "globalAfter",
        "globalDifference",
        "touchedAccountDifference",
        "untouchedAccountCacheDifference",
        "alternativeHoldingsDifference",
        "unexplainedResidual",
    }
    if not required_net_worth.issubset(net_worth) or (
        abs(Decimal(str(net_worth["unexplainedResidual"]))) > CENT
    ):
        raise DecisionError("staging receipt did not conserve source-safe net worth")
    values = {
        key: Decimal(str(net_worth[key])) for key in required_net_worth
    }
    if (
        values["globalAfter"] - values["globalBefore"]
        != values["globalDifference"]
        or values["globalDifference"]
        != values["touchedAccountDifference"]
        + values["untouchedAccountCacheDifference"]
        + values["alternativeHoldingsDifference"]
        or values["unexplainedResidual"]
        != values["globalDifference"]
        - values["untouchedAccountCacheDifference"]
        or abs(values["touchedAccountDifference"]) > CENT
        or abs(values["alternativeHoldingsDifference"]) > CENT
    ):
        raise DecisionError("staging receipt net-worth attribution is inconsistent")
    if receipt.get("health", {}).get("newNonInfoCount") != 0:
        raise DecisionError("staging receipt introduced non-INFO health issues")
    expected_counts = {
        key: len(value) for key, value in staging_plan["operations"].items()
    }
    if receipt.get("operations") != expected_counts or not receipt.get("backup"):
        raise DecisionError("staging receipt lacks exact operations or backup")
    spending = receipt.get("spendingReport")
    if spending is None or (
        abs(
            Decimal(str(spending["incomeDelta"]))
            - Decimal(staging_plan["impact"]["incomeDelta"])
        )
        > Decimal("0.01")
        or abs(
            Decimal(str(spending["spendingDelta"]))
            - Decimal(staging_plan["impact"]["spendingDelta"])
        )
        > Decimal("0.01")
    ):
        raise DecisionError("staging receipt spending assertions are incomplete")
    if any(
        abs(Decimal(str(assertion["afterDrift"]))) > Decimal("0.01")
        for assertion in receipt.get("assertions", [])
    ):
        raise DecisionError("staging receipt contains a failed balance assertion")
    if {
        assertion.get("canonicalAccountId")
        for assertion in receipt.get("assertions", [])
    } != {
        assertion.get("canonicalAccountId")
        for assertion in staging_plan["assertions"]
    }:
        raise DecisionError("staging receipt balance assertions are incomplete")
    return staging_plan, receipt


def _revalidate_production_decisions(
    plan: dict,
    staging_plan: dict,
    staging_receipt: dict,
    data_dir: Path,
) -> dict:
    evidence = plan.get("evidence", {}).get("manualDecisions")
    if not evidence:
        raise DecisionError("production plan has no sealed manual decisions")
    decision_path = validate_private_output(
        Path(evidence["path"]), data_dir, Path.cwd()
    )
    binding = manual_decision_evidence_binding(decision_path, data_dir)
    expected = plan.get("decisionEvidence")
    if (
        binding != expected
        or binding.get("manualDecisionsSha256") != evidence.get("sha256")
        or plan.get("portableIntent", {}).get("decisionEvidence") != expected
        or staging_plan.get("decisionEvidence") != expected
        or staging_plan.get("portableIntent", {}).get("decisionEvidence") != expected
        or staging_receipt.get("decisionEvidence") != expected
    ):
        raise DecisionError(
            "manual decision file or nested evidence differs from staging"
        )
    return binding


def _validate_promotion_intent(production_plan: dict, staging_plan: dict) -> None:
    for label, plan in (
        ("production", production_plan),
        ("staging", staging_plan),
    ):
        portable = plan.get("portableIntent")
        if not isinstance(portable, dict) or plan.get(
            "intentFingerprint"
        ) != plan_fingerprint(portable):
            raise DecisionError(f"{label} portable intent fingerprint is invalid")
    if (
        production_plan["portableIntent"] != staging_plan["portableIntent"]
        or production_plan["intentFingerprint"]
        != staging_plan["intentFingerprint"]
    ):
        raise DecisionError(
            "production intent differs from the exactly rehearsed staging intent"
        )


def _validate_production_authorization(args, plan_path: Path, plan: dict) -> None:
    target = urlparse(args.base_url)
    if (
        target.scheme not in {"http", "https"}
        or target.hostname not in {"127.0.0.1", "localhost", "::1"}
        or target.port != 8088
    ):
        raise DecisionError("production promotion is restricted to loopback port 8088")
    if not args.allow_production:
        raise DecisionError("--allow-production is required")
    if not args.expected_plan_sha:
        raise DecisionError("--expected-plan-sha is required")
    if sha256_file(plan_path) != args.expected_plan_sha:
        raise DecisionError("operator-supplied production plan SHA is not exact")
    if plan.get("mode") != "production-promotion-plan":
        raise DecisionError("production promotion requires a production plan")
    if any(row.get("decisionStatus") != "ruled" for row in plan.get("manual", [])):
        raise DecisionError("production plan has unruled manual transactions")


def _validate_current_snapshot(plan: dict, data_dir: Path) -> None:
    latest = max(
        (data_dir / "raw" / "simplefin").rglob("simplefin-*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        default=None,
    )
    planned = Path(plan["evidence"]["snapshot"]["path"]).resolve()
    if latest is None or planned != latest.resolve():
        raise DecisionError(
            "production plan does not use the latest private SimpleFIN snapshot"
        )


def _write_live_account_map(client, data_dir: Path) -> tuple[dict[str, str], Path]:
    canonical = load_canonical_accounts(
        data_dir / "normalized" / "canonical" / "accounts.csv"
    )
    aliases = build_aliases(data_dir, canonical)
    account_plan = build_account_plan(client.list_accounts(), canonical, aliases)
    if account_plan.creates or account_plan.updates:
        raise DecisionError(
            "production accounts differ from canonical identities; promotion is read-only"
        )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-%f")
    path = (
        data_dir
        / "normalized"
        / "simplefin"
        / f"production-account-map-{stamp}.json"
    )
    path.write_text(
        json.dumps(account_plan.canonical_to_existing, indent=2),
        encoding="utf-8",
    )
    return account_plan.canonical_to_existing, path


def _durable_transfers(
    data_dir: Path, reviewed: dict[str, Any]
) -> dict[tuple[str, str], str]:
    canonical_groups: dict[str, str] = {}
    canonical_path = data_dir / "normalized" / "canonical" / "transactions.csv"
    if canonical_path.exists():
        with canonical_path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                if row.get("transfer_group"):
                    source_id = str(row.get("source_id") or "")
                    group = str(row["transfer_group"])
                    previous = canonical_groups.setdefault(source_id, group)
                    if previous != group:
                        raise DecisionError(
                            "canonical transfer identity has conflicting groups"
                        )
    result: dict[tuple[str, str], str] = {}
    for account in reviewed.get("accounts", []):
        source_account_id = str(account.get("sourceAccountId") or "")
        for transaction in account.get("transactions", []):
            source_id = str(transaction.get("sourceId") or "")
            canonical_id = f"simplefin:{source_account_id}:{source_id}"
            if canonical_id in canonical_groups:
                result[(source_account_id, source_id)] = canonical_groups[canonical_id]
    return result


def _apply_operations(client, plan: dict) -> None:
    _reject_delete_operations(plan)
    operations = plan["operations"]
    pre_rows = list(client.iter_activities())
    by_key_id = {
        row.get("idempotencyKey"): row["id"]
        for row in pre_rows
        if row.get("idempotencyKey")
    }
    try:
        result = client.save_activities(
            creates=operations["creates"],
            updates=operations["updates"],
            delete_ids=operations["deleteIds"],
        )
        if result.get("errors"):
            raise DecisionError(f"SimpleFIN apply failed: {result['errors'][0]}")
        if len(result.get("created") or []) != len(operations["creates"]):
            raise DecisionError("SimpleFIN apply created an unexpected activity count")
        if len(result.get("updated") or []) != len(operations["updates"]):
            raise DecisionError("SimpleFIN apply updated an unexpected activity count")
        if len(result.get("deleted") or []) != len(operations["deleteIds"]):
            raise DecisionError("SimpleFIN apply deleted an unexpected activity count")
        created_by_key = {
            row.get("idempotencyKey"): row["id"]
            for row in result.get("created") or []
        }
        for finalization in operations["metadataFinalizations"]:
            _put_metadata_finalization(
                client,
                finalization,
                allowed_fingerprints=set(finalization["forwardFingerprints"]),
            )
        response_groups: dict[str, str] = {}
        for link in plan.get("links", []):
            left_id = (
                created_by_key.get(link.get("leftKey"))
                or link.get("leftActivityId")
                or by_key_id.get(link.get("leftKey"))
            )
            right_id = (
                created_by_key.get(link.get("rightKey"))
                or link.get("rightActivityId")
                or by_key_id.get(link.get("rightKey"))
            )
            if not left_id or not right_id:
                raise DecisionError("linked transfer create IDs were not returned")
            link_result = client.post(
                "/activities/link",
                {
                    "activityAId": left_id,
                    "activityBId": right_id,
                },
            )
            if isinstance(link_result, str):
                if not link_result.strip():
                    raise DecisionError("transfer link endpoint returned invalid data")
                response_groups[link["leftKey"]] = link_result
            elif isinstance(link_result, list):
                serialized = json.dumps(link_result)
                if (
                    not link_result
                    or left_id not in serialized
                    or right_id not in serialized
                ):
                    raise DecisionError("transfer link endpoint returned invalid data")
            elif isinstance(link_result, bool):
                if not link_result:
                    raise DecisionError("transfer link endpoint reported failure")
            elif link_result is not None:
                if not isinstance(link_result, dict):
                    raise DecisionError("transfer link endpoint returned invalid data")
                if link_result.get("errors") or link_result.get("success") is False:
                    raise DecisionError("transfer link endpoint reported failure")
                group_id = (
                    link_result.get("sourceGroupId")
                    or link_result.get("source_group_id")
                    or link_result.get("groupId")
                )
                if group_id:
                    response_groups[str(
                        link.get("leftKey") or link.get("leftActivityId")
                    )] = str(group_id)
        client.post("/portfolio/recalculate", {})
        post_rows = list(client.iter_activities())
        if not _already_applied(plan, post_rows):
            raise DecisionError(
                "live activities do not match the sealed post-apply ledger state"
            )
        post_by_key = {
            row.get("idempotencyKey"): row
            for row in post_rows
            if row.get("idempotencyKey")
        }
        post_by_id = {row["id"]: row for row in post_rows}
        for member, response_group in response_groups.items():
            persisted = post_by_key.get(member) or post_by_id.get(member)
            if (
                persisted is None
                or str(persisted.get("sourceGroupId")) != response_group
            ):
                raise DecisionError(
                    "transfer link response group differs from persisted activities"
                )
    except Exception:
        _rollback(client, plan)
        raise


def cmd_plan(args, client) -> int:
    reviewed_path = args.reviewed_plan.resolve()
    data_dir = args.data_dir.resolve()
    validate_private_output(
        data_dir / "normalized" / "simplefin" / "apply-plan.json",
        data_dir,
        Path.cwd(),
    )
    validate_private_output(reviewed_path, data_dir, Path.cwd())
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    snapshot_path = Path(reviewed["snapshot"]).resolve()
    validate_private_output(snapshot_path, data_dir, Path.cwd())
    stage_map_path = args.rebuild_map.resolve()
    validate_private_output(stage_map_path, args.staging_data_dir, Path.cwd())
    stage_map = json.loads(stage_map_path.read_text(encoding="utf-8"))
    decisions = None
    decisions_path = None
    if args.manual_decisions:
        decisions_path = validate_private_output(
            args.manual_decisions, data_dir, Path.cwd()
        )
        decisions = load_manual_decisions(decisions_path, data_dir)
    metadata_remediations = None
    metadata_remediations_path = None
    if args.metadata_remediations:
        metadata_remediations_path = validate_private_output(
            args.metadata_remediations, data_dir, Path.cwd()
        )
        metadata_remediations = _load_metadata_remediations(
            metadata_remediations_path
        )
    accounts = client.list_accounts()
    activities = list(client.iter_activities())
    values = _current_values(client, accounts)
    environment = instance_fingerprint(client, args.base_url)
    durable_transfers = _durable_transfers(data_dir, reviewed)
    plan = build_application_plan(
        reviewed,
        reviewed_path,
        snapshot_path,
        stage_map,
        activities,
        accounts,
        values,
        environment,
        stage_map_path=stage_map_path,
        durable_transfer_groups=durable_transfers,
        manual_decisions=decisions,
        manual_decisions_path=decisions_path,
        metadata_remediations=metadata_remediations,
        metadata_remediations_path=metadata_remediations_path,
    )
    _reject_delete_operations(plan)
    path = write_application_plan(data_dir, plan)
    counts = plan["operations"]
    print(f"plan={path}")
    print(
        f"create={len(counts['creates'])} update={len(counts['updates'])} "
        f"metadata-finalize={len(counts['metadataFinalizations'])} "
        f"delete={len(counts['deleteIds'])} manual={len(plan['manual'])} "
        f"fingerprint={plan['planFingerprint']} environment={environment}"
    )
    return 0


def cmd_promote_plan(args, client) -> int:
    data_dir = args.data_dir.resolve()
    target = urlparse(args.base_url)
    if (
        target.scheme not in {"http", "https"}
        or target.hostname not in {"127.0.0.1", "localhost", "::1"}
        or target.port != 8088
    ):
        raise DecisionError(
            "production planning is restricted to loopback port 8088"
        )
    reviewed_path = validate_private_output(
        args.reviewed_plan, data_dir, Path.cwd()
    )
    decisions_path = validate_private_output(
        args.manual_decisions, data_dir, Path.cwd()
    )
    staging_plan_path = validate_private_output(
        args.staging_plan, data_dir, Path.cwd()
    )
    staging_receipt_path = validate_private_output(
        args.staging_receipt, data_dir, Path.cwd()
    )
    staging_plan, staging_receipt = _validate_staging_receipt(
        staging_plan_path, staging_receipt_path
    )
    decisions = load_manual_decisions(decisions_path, data_dir)
    metadata_remediations = None
    metadata_remediations_path = None
    if args.metadata_remediations:
        metadata_remediations_path = validate_private_output(
            args.metadata_remediations, data_dir, Path.cwd()
        )
        metadata_remediations = _load_metadata_remediations(
            metadata_remediations_path
        )
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    snapshot_path = Path(reviewed["snapshot"]).resolve()
    validate_private_output(snapshot_path, data_dir, Path.cwd())
    production_map, production_map_path = _write_live_account_map(
        client, data_dir
    )
    accounts = client.list_accounts()
    activities = list(client.iter_activities())
    plan = build_application_plan(
        reviewed,
        reviewed_path,
        snapshot_path,
        production_map,
        activities,
        accounts,
        _current_values(client, accounts),
        instance_fingerprint(client, args.base_url),
        stage_map_path=production_map_path,
        durable_transfer_groups=_durable_transfers(data_dir, reviewed),
        manual_decisions=decisions,
        manual_decisions_path=decisions_path,
        metadata_remediations=metadata_remediations,
        metadata_remediations_path=metadata_remediations_path,
        mode="production-promotion-plan",
    )
    _reject_delete_operations(plan)
    _validate_promotion_intent(plan, staging_plan)
    if (
        plan.get("decisionEvidence") != staging_plan.get("decisionEvidence")
        or staging_receipt.get("decisionEvidence") != plan.get("decisionEvidence")
    ):
        raise DecisionError(
            "production manual decision evidence differs from staging"
        )
    if plan["environmentFingerprint"] == staging_plan["environmentFingerprint"]:
        raise DecisionError(
            "production and staging must be distinct application environments"
        )
    if any(row.get("decisionStatus") != "ruled" for row in plan["manual"]):
        raise DecisionError("production plan has unruled manual transactions")
    plan["evidence"].update({
        "stagingApplicationPlan": {
            "path": str(staging_plan_path),
            "sha256": sha256_file(staging_plan_path),
        },
        "stagingReceipt": {
            "path": str(staging_receipt_path),
            "sha256": sha256_file(staging_receipt_path),
        },
    })
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    _validate_current_snapshot(plan, data_dir)
    health = _assert_health_repairable_by_links(client, plan)
    if _spending_totals(client, plan) is None:
        raise DecisionError("production spending endpoint is not configured")
    plan_path = write_application_plan(data_dir, plan)
    plan_sha = sha256_file(plan_path)
    transfer_count = sum(
        payload["activityType"] in {"TRANSFER_IN", "TRANSFER_OUT"}
        for payload in plan["operations"]["creates"]
    )
    report = {
        "schemaVersion": 3,
        "status": "production-ready",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "productionMutated": False,
        "applicationPlan": str(plan_path),
        "applicationPlanSha256": plan_sha,
        "planFingerprint": plan["planFingerprint"],
        "intentFingerprint": plan["intentFingerprint"],
        "decisionEvidence": plan["decisionEvidence"],
        "preLedgerFingerprint": plan["ledgerFingerprint"],
        "expectedPostLedgerFingerprint": plan[
            "expectedPostLedgerFingerprint"
        ],
        "stagingReceipt": str(staging_receipt_path),
        "stagingReceiptStatus": staging_receipt["status"],
        "operations": {
            key: len(value) for key, value in plan["operations"].items()
        },
        "links": len(plan["links"]),
        "remainingManual": plan["manual"],
        "cashFlow": plan["impact"],
        "categoryImpact": {
            "categorizedCreates": 0,
            "uncategorizedCreates": (
                len(plan["operations"]["creates"]) - transfer_count
            ),
            "transferCreates": transfer_count,
            "expectedUncategorizedIncomeDelta": plan["impact"]["incomeDelta"],
            "expectedUncategorizedSpendingDelta": plan["impact"][
                "spendingDelta"
            ],
        },
        "preHealth": health,
        "expectedPostHealth": {"newNonInfoCount": 0},
    }
    report_path = (
        data_dir
        / "normalized"
        / "simplefin"
        / f"production-ready-{datetime.now(timezone.utc):%Y-%m-%d-%H%M%S-%f}.json"
    )
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"status=production-ready create={report['operations']['creates']} "
        f"manual={len(plan['manual'])} plan-sha256={plan_sha} "
        f"report={report_path}"
    )
    return 0


def cmd_apply(args, client) -> int:
    plan_path = validate_private_output(
        args.application_plan, args.data_dir, Path.cwd()
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_plan_seal(plan)
    _reject_delete_operations(plan)
    production = plan.get("mode") == "production-promotion-plan"
    staging_plan = None
    staging_receipt = None
    if production:
        if args.command != "promote-apply":
            raise DecisionError(
                "production plans require the explicit promote-apply command"
            )
        _validate_production_authorization(args, plan_path, plan)
        _validate_current_snapshot(plan, args.data_dir.resolve())
        staging_plan, staging_receipt = _validate_staging_receipt(
            Path(plan["evidence"]["stagingApplicationPlan"]["path"]),
            Path(plan["evidence"]["stagingReceipt"]["path"]),
        )
        _validate_promotion_intent(plan, staging_plan)
        if staging_receipt["intentFingerprint"] != plan["intentFingerprint"]:
            raise DecisionError(
                "production intent differs from the sealed staging rehearsal"
            )
        _revalidate_production_decisions(
            plan, staging_plan, staging_receipt, args.data_dir.resolve()
        )
    else:
        if args.command != "apply":
            raise DecisionError("promote-apply requires a production plan")
        validate_apply_target(
            client,
            args.base_url,
            plan["planFingerprint"],
            args.plan_fingerprint,
        )
    _validate_plan_environment(client, args.base_url, plan)
    rows = list(client.iter_activities())
    account_ids = {row["accountId"] for row in plan["assertions"]}
    ledger_account_ids = set(plan["ledgerAccountIds"])
    already = _already_applied(plan, rows)
    if (
        not already
        and ledger_fingerprint(rows, ledger_account_ids) != plan["ledgerFingerprint"]
    ):
        raise DecisionError("staging ledger changed after the SimpleFIN plan was sealed")

    accounts = client.list_accounts()
    selected_accounts = [row for row in accounts if row["id"] in account_ids]
    touched_account_ids = {
        str(payload["accountId"])
        for operation in ("creates", "updates")
        for payload in plan["operations"][operation]
    }
    touched_account_ids.update(
        str(finalization["payload"]["accountId"])
        for finalization in plan["operations"]["metadataFinalizations"]
    )
    touched_activity_ids = {
        str(link[field])
        for link in plan.get("links", [])
        for field in ("leftActivityId", "rightActivityId")
        if link.get(field)
    }
    touched_account_ids.update(
        str(row["accountId"])
        for row in rows
        if str(row.get("id")) in touched_activity_ids
    )
    touched_account_ids.update(
        str(snapshot["accountId"])
        for retype in plan.get("linkRetypes", [])
        for snapshot in (retype.get("before"), retype.get("after"))
        if snapshot and snapshot.get("accountId")
    )
    before_portfolio = _portfolio_values(client, accounts)
    before_values = {
        account_id: before_portfolio["accountValues"][account_id]
        for account_id in account_ids
    }
    expected_balances = {
        row["accountId"]: Decimal(row["sourceBalance"])
        for row in plan["assertions"]
    }
    before_spending = _spending_totals(client, plan)
    if production and before_spending is None:
        raise DecisionError("production spending endpoint is not configured")
    if not production:
        client.post("/portfolio/recalculate", {})
    pre_health = (
        _assert_health_repairable_by_links(client, plan)
        if production
        else _stable_health_summary(client)
    )
    health = {
        "before": pre_health,
        "after": pre_health,
        "newNonInfoCount": 0,
    }
    backup = None
    if not already:
        backup = client.backup_database()
        if production and not backup:
            raise DecisionError("production backup was not confirmed")
        if production:
            validate_plan_seal(plan)
            _revalidate_production_decisions(
                plan,
                staging_plan,
                staging_receipt,
                args.data_dir.resolve(),
            )
        mutation_succeeded = False
        try:
            _apply_operations(client, plan)
            mutation_succeeded = True

            deadline = time.monotonic() + args.wait_seconds
            while True:
                after_values = _current_values(client, selected_accounts)
                if all(
                    account_id in after_values
                    and abs(after_values[account_id] - value) <= Decimal("0.01")
                    for account_id, value in expected_balances.items()
                ):
                    break
                if time.monotonic() >= deadline:
                    raise DecisionError(
                        "SimpleFIN balance assertions did not settle within $0.01"
                    )
                time.sleep(2)
            after_portfolio = _portfolio_values(client, accounts)
            after_values = {
                account_id: after_portfolio["accountValues"][account_id]
                for account_id in account_ids
            }
            if any(
                abs(after_values[account_id] - value) > CENT
                for account_id, value in expected_balances.items()
            ):
                raise DecisionError(
                    "SimpleFIN balance assertion drifted during portfolio verification"
                )
            net_worth = _portfolio_value_report(
                before_portfolio, after_portfolio, touched_account_ids
            )
            after_spending = _spending_totals(client, plan)
            if before_spending is not None:
                if after_spending is None:
                    raise DecisionError(
                        "Wealthfolio spending endpoint became unavailable"
                    )
                income_delta = after_spending["income"] - before_spending["income"]
                spending_delta = (
                    after_spending["spending"] - before_spending["spending"]
                )
                if (
                    abs(income_delta - Decimal(plan["impact"]["incomeDelta"]))
                    > Decimal("0.01")
                    or abs(
                        spending_delta - Decimal(plan["impact"]["spendingDelta"])
                    )
                    > Decimal("0.01")
                ):
                    raise DecisionError(
                        "Wealthfolio spending report differs from sealed cash-flow "
                        f"impact: income={income_delta} "
                        f"spending={spending_delta}"
                    )
            health = _assert_health_not_degraded(client, pre_health, plan)
            post_rows = list(client.iter_activities())
            post_fingerprint = expected_post_fingerprint(
                post_rows,
                ledger_account_ids,
                {"creates": [], "updates": [], "deleteIds": []},
                plan.get("links", []),
            )
            if post_fingerprint != plan["expectedPostLedgerFingerprint"]:
                raise DecisionError(
                    "post-apply ledger fingerprint changed after verification"
                )
        except Exception:
            if mutation_succeeded:
                _rollback(client, plan)
            raise
    else:
        after_portfolio = _portfolio_values(client, accounts)
        after_values = {
            account_id: after_portfolio["accountValues"][account_id]
            for account_id in account_ids
        }
        net_worth = _portfolio_value_report(
            before_portfolio, after_portfolio, touched_account_ids
        )
        after_spending = _spending_totals(client, plan)
        if any(
            account_id not in after_values
            or abs(after_values[account_id] - value) > Decimal("0.01")
            for account_id, value in expected_balances.items()
        ):
            raise DecisionError(
                "already-applied SimpleFIN balance assertion drifted beyond $0.01"
            )
        health = _assert_health_not_degraded(client, pre_health, plan)
        post_rows = list(client.iter_activities())
        post_fingerprint = expected_post_fingerprint(
            post_rows,
            ledger_account_ids,
            {"creates": [], "updates": [], "deleteIds": []},
            plan.get("links", []),
        )
        if post_fingerprint != plan["expectedPostLedgerFingerprint"]:
            raise DecisionError(
                "already-applied ledger differs from the sealed post-state"
            )
    report = {
        "schemaVersion": 5,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": "already-applied" if already else "applied",
        "applicationPlan": str(plan_path),
        "applicationPlanSha256": sha256_file(plan_path),
        "planFingerprint": plan["planFingerprint"],
        "intentFingerprint": plan["intentFingerprint"],
        "decisionEvidence": plan.get("decisionEvidence"),
        "environmentFingerprint": plan["environmentFingerprint"],
        "preLedgerFingerprint": plan["ledgerFingerprint"],
        "postLedgerFingerprint": post_fingerprint,
        "backup": backup,
        "preHealth": pre_health,
        "health": health,
        "operations": {
            key: len(value) for key, value in plan["operations"].items()
        },
        "manual": plan["manual"],
        "monitors": plan["monitors"],
        "reconciliations": plan["reconciliations"],
        "linkRetypes": plan.get("linkRetypes", []),
        "cashFlow": plan["impact"],
        "netWorth": {
            key: format(value, "f")
            for key, value in net_worth.items()
        },
        "spendingReport": (
            {
                "beforeIncome": format(before_spending["income"], "f"),
                "afterIncome": format(after_spending["income"], "f"),
                "incomeDelta": format(
                    after_spending["income"] - before_spending["income"], "f"
                ),
                "beforeSpending": format(before_spending["spending"], "f"),
                "afterSpending": format(after_spending["spending"], "f"),
                "spendingDelta": format(
                    after_spending["spending"] - before_spending["spending"], "f"
                ),
            }
            if before_spending is not None and after_spending is not None
            else None
        ),
        "assertions": [
            {
                "canonicalAccountId": row["canonicalAccountId"],
                "sourceBalance": row["sourceBalance"],
                "beforeBalance": format(before_values[row["accountId"]], "f"),
                "afterBalance": format(after_values[row["accountId"]], "f"),
                "afterDrift": format(
                    after_values[row["accountId"]]
                    - Decimal(row["sourceBalance"]),
                    "f",
                ),
            }
            for row in plan["assertions"]
        ],
    }
    output = (
        args.data_dir
        / "normalized"
        / "simplefin"
        / f"apply-report-{datetime.now(timezone.utc):%Y-%m-%d-%H%M%S-%f}.json"
    )
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"status={report['status']} "
        f"net-worth-delta={net_worth['globalDifference']} "
        f"cache-delta={net_worth['untouchedAccountCacheDifference']} "
        f"unexplained={net_worth['unexplainedResidual']} report={output}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("plan", "apply", "promote-plan", "promote-apply"),
        default="plan",
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--reviewed-plan", type=Path)
    parser.add_argument("--rebuild-map", type=Path)
    parser.add_argument("--staging-data-dir", type=Path)
    parser.add_argument("--application-plan", type=Path)
    parser.add_argument("--manual-decisions", type=Path)
    parser.add_argument("--metadata-remediations", type=Path)
    parser.add_argument("--staging-plan", type=Path)
    parser.add_argument("--staging-receipt", type=Path)
    parser.add_argument("--plan-fingerprint")
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--expected-plan-sha")
    parser.add_argument("--wait-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    client = WealthfolioClient(args.base_url)
    try:
        client.login(_password())
        if args.command == "plan":
            if not args.reviewed_plan or not args.rebuild_map or not args.staging_data_dir:
                parser.error(
                    "plan requires --reviewed-plan, --rebuild-map, and --staging-data-dir"
                )
            return cmd_plan(args, client)
        if args.command == "promote-plan":
            if not all((
                args.reviewed_plan,
                args.manual_decisions,
                args.staging_plan,
                args.staging_receipt,
            )):
                parser.error(
                    "promote-plan requires --reviewed-plan, --manual-decisions, "
                    "--staging-plan, and --staging-receipt"
                )
            return cmd_promote_plan(args, client)
        if not args.application_plan:
            parser.error(f"{args.command} requires --application-plan")
        return cmd_apply(args, client)
    except DecisionError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
