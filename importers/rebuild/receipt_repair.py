"""Build and execute a bounded, receipt-proven duplicate projection repair.

The workflow adopts an already-present authoritative Wealthfolio activity,
removes only lower-priority rows suppressed by canonical source authority, and
adjusts the receipt-bound reconciliation row by the removed economic effect.
It never mutates production directly; production delivery is an atomic database
swap of a separately verified clone.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import stat
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlsplit

from finance_store.domain import content_hash
from finance_store.identity import DEFAULT_POLICY, DEFAULT_SOURCE_AUTHORITY_POLICY
from importers.audit import forensic
from importers.lineage_review import canonical
from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.simplefin.application import (
    _activity_fingerprint,
    _signed,
    _update_payload,
    activity_payload,
    activity_semantic_fingerprint,
    ledger_fingerprint,
)
from importers.simplefin.spending_adapter import SpendingAdapter

from .cutover import download_backup, verify_backup_file
from .projector import (
    inspect_rebuild_target,
    require_unique_backup,
    wait_for_recalculation,
)
from .safety import plan_fingerprint, validate_apply_target
from .immutable_metadata import publish_json


SCHEMA_VERSION = 2
KIND = "wealthfolio-receipt-bound-duplicate-repair-v1"
SOURCE_FAMILY = "simplefin"
AUTHORITY_RATIONALE = "authoritative-source-coverage-suppression"
AUTHORITY_CONFIDENCE = "authoritative-source-coverage"
REPO_ROOT = Path(__file__).resolve().parents[2]


class ReceiptRepairError(RuntimeError):
    """Raised when a bounded repair cannot be proved or replayed safely."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_immutable(path: Path, document: dict[str, Any]) -> None:
    try:
        publish_json(path, document)
    except FileExistsError:
        raise ReceiptRepairError("output path already contains another repair artifact") from None
    path.chmod(stat.S_IREAD)


def _load_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptRepairError(code) from exc
    if not isinstance(value, dict):
        raise ReceiptRepairError(code)
    return value


def _provider_token(source_id: str, family: str) -> str:
    prefix = f"{family}:"
    if not source_id.startswith(prefix):
        raise ReceiptRepairError("repair source identity has an unexpected family")
    parts = source_id.split(":", 2)
    if len(parts) != 3 or not parts[2]:
        raise ReceiptRepairError("repair source identity is not account scoped")
    return parts[2]


def _activity_core(row: Mapping[str, Any]) -> dict[str, Any]:
    value = _activity_fingerprint(dict(row))
    value.pop("id", None)
    return value


def _assignment_snapshot(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "id": row.get("id"),
                "activityId": row.get("activityId"),
                "taxonomyId": row.get("taxonomyId"),
                "categoryId": row.get("categoryId"),
                "source": row.get("source"),
                "weight": row.get("weight"),
                "createdAt": row.get("createdAt"),
                "updatedAt": row.get("updatedAt"),
            }
            for row in rows
        ),
        key=lambda row: json.dumps(row, sort_keys=True, default=str),
    )


def _assignment_map(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        taxonomy = str(row.get("taxonomyId") or "")
        category = str(row.get("categoryId") or "")
        if not taxonomy or not category or taxonomy in result:
            raise ReceiptRepairError(
                "repair assignment state is incomplete or ambiguous"
            )
        result[taxonomy] = row
    return result


def _assignment_rank(row: Mapping[str, Any]) -> int:
    source = str(row.get("source") or "").casefold()
    if source == "manual":
        return 2
    if source == "rule":
        return 1
    return 0


def _assignment_time(row: Mapping[str, Any]) -> datetime:
    value = row.get("updatedAt") or row.get("createdAt")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptRepairError(
            "conflicting manual assignments lack a valid edit time"
        ) from exc
    if parsed.tzinfo is None:
        if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?",
            str(value),
        ) is None:
            raise ReceiptRepairError(
                "conflicting manual assignments lack an unambiguous edit time"
            )
        # The pinned 3.7 API serializes NaiveDateTime after converting the
        # stored RFC3339 value with naive_utc(), not the user's local timezone.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assignment_upserts(
    source_activity_id: str,
    source_rows: Sequence[Mapping[str, Any]],
    survivor_activity_id: str,
    survivor_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    source = _assignment_map(source_rows)
    survivor = _assignment_map(survivor_rows)
    result = []
    for taxonomy_id in sorted(set(source) | set(survivor)):
        source_row = source.get(taxonomy_id)
        survivor_row = survivor.get(taxonomy_id)
        if source_row is None:
            continue
        if (
            survivor_row is not None
            and source_row.get("categoryId") == survivor_row.get("categoryId")
        ):
            if (
                str(source_row.get("source") or "").casefold() == "manual"
                and str(survivor_row.get("source") or "").casefold()
                != "manual"
            ):
                chosen = source_row
                reason = "manual-assignment-provenance"
            else:
                continue
        elif survivor_row is None:
            if str(source_row.get("source") or "").casefold() != "manual":
                raise ReceiptRepairError(
                    "rule-derived source-only assignment cannot be recreated"
                )
            chosen = source_row
            reason = "source-only-manual-assignment"
        else:
            source_rank = _assignment_rank(source_row)
            survivor_rank = _assignment_rank(survivor_row)
            if source_rank > survivor_rank:
                chosen = source_row
                reason = "higher-precedence-manual-assignment"
            elif survivor_rank > source_rank:
                continue
            elif source_rank == 2:
                source_time = _assignment_time(source_row)
                survivor_time = _assignment_time(survivor_row)
                if source_time == survivor_time:
                    raise ReceiptRepairError(
                        "conflicting manual assignments lack a unique latest edit"
                    )
                if survivor_time > source_time:
                    continue
                chosen = source_row
                reason = "newer-manual-assignment"
            else:
                raise ReceiptRepairError(
                    "conflicting non-manual assignments require review"
                )
        if str(chosen.get("source") or "").casefold() != "manual":
            raise ReceiptRepairError(
                "repair cannot recreate non-manual assignment provenance"
            )
        if Decimal(str(chosen.get("weight") or "0")) != Decimal("10000"):
            raise ReceiptRepairError(
                "repair cannot recreate a weighted assignment"
            )
        result.append(
            {
                "sourceActivityId": source_activity_id,
                "activityId": survivor_activity_id,
                "taxonomyId": taxonomy_id,
                "categoryId": str(chosen["categoryId"]),
                "expectedSource": "manual",
                "expectedWeight": "10000",
                "reason": reason,
                "before": (
                    dict(survivor_row) if survivor_row is not None else None
                ),
                "sourceEvidence": dict(chosen),
            }
        )
    return result


def _normalized_date(value: Any) -> str:
    return str(value or "")[:10]


def _create_signed_effect(row: Mapping[str, Any]) -> Decimal:
    return _signed(
        {
            "activityType": row.get("activityType"),
            "amount": row.get("amount"),
        }
    )


def _validate_selection(selection: Mapping[str, Any]) -> None:
    required = {
        "schemaVersion",
        "sourceApplicationPlanSha256",
        "ledgerAccountId",
        "effectiveFrom",
        "effectiveThrough",
        "expectedSourceRows",
        "expectedSuppressions",
    }
    if (
        not required <= set(selection)
        or set(selection) - required - {"sourceActivityIds"}
        or selection.get("schemaVersion") != 1
    ):
        raise ReceiptRepairError("repair selection shape is invalid")
    for key in ("sourceApplicationPlanSha256",):
        value = str(selection.get(key) or "")
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ReceiptRepairError("repair selection hash is invalid")
    try:
        start = date.fromisoformat(str(selection["effectiveFrom"]))
        end = date.fromisoformat(str(selection["effectiveThrough"]))
    except ValueError as exc:
        raise ReceiptRepairError("repair selection interval is invalid") from exc
    if start > end:
        raise ReceiptRepairError("repair selection interval is inverted")
    if not str(selection.get("ledgerAccountId") or ""):
        raise ReceiptRepairError("repair selection account is missing")
    for key in ("expectedSourceRows", "expectedSuppressions"):
        if not isinstance(selection.get(key), int) or selection[key] < 1:
            raise ReceiptRepairError("repair selection count is invalid")
    selected_ids = selection.get("sourceActivityIds")
    if "sourceActivityIds" in selection and (
        not isinstance(selected_ids, list)
        or any(not isinstance(value, str) or not value for value in selected_ids)
        or len(set(selected_ids)) != len(selected_ids)
        or len(selected_ids) != selection["expectedSuppressions"]
    ):
        raise ReceiptRepairError("repair selected activity inventory is invalid")


def _canonical_documents(
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]], str]:
    output = root / "normalized" / "canonical"
    manifest_path = output / "manifest.json"
    manifest = _load_json(manifest_path, "canonical manifest is unavailable")
    data_files = manifest.get("dataFiles")
    if manifest.get("schemaVersion") != 5 or not isinstance(data_files, dict):
        raise ReceiptRepairError("canonical manifest is incompatible")
    for name, expected in data_files.items():
        path = output / str(name)
        if not path.is_file() or _sha256(path) != expected:
            raise ReceiptRepairError("canonical publication hash mismatch")
    observations = _load_json(
        output / "transaction-observations.json",
        "canonical observations are unavailable",
    )
    lineage = _load_json(
        output / "transaction-lineage.json",
        "canonical lineage is unavailable",
    )
    try:
        with (output / "transactions.csv").open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            rows = list(csv.DictReader(stream))
        with (output / "accounts.csv").open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            accounts = list(csv.DictReader(stream))
        canonical.validate_documents(observations, lineage, rows)
    except (OSError, canonical.ReviewError) as exc:
        raise ReceiptRepairError("canonical publication verification failed") from exc
    return observations, lineage, accounts, _sha256(manifest_path)


def _repair_currency(transaction: Mapping[str, Any], account_currency: str) -> str:
    currency = transaction.get("currency")
    if currency is None:
        # Canonical v5 carries currency on accounts; its identity producer only
        # supports USD when a transaction does not declare its own currency.
        if account_currency != "USD":
            raise ReceiptRepairError(
                "canonical identity lacks explicit non-USD transaction currency"
            )
        return account_currency
    currency = str(currency).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ReceiptRepairError("canonical transaction currency is invalid")
    return currency


def _validate_repair_policy(identity: Mapping[str, Any]) -> None:
    policy_document = identity.get("policyDocument")
    source_authority = identity.get("sourceAuthority")
    if (
        identity.get("policyVersion") != "canonical-identity-v5"
        or not isinstance(policy_document, Mapping)
        or not isinstance(source_authority, Mapping)
        or source_authority.get("policyVersion")
        != "canonical-source-authority-v3"
    ):
        raise ReceiptRepairError("repair requires canonical identity v5")
    rules = policy_document.get("rules")
    authority_document = policy_document.get("sourceAuthority")
    authority_policy = (
        authority_document.get("policy")
        if isinstance(authority_document, Mapping)
        else None
    )
    unique_cross_source = (
        rules.get("uniqueCrossSource") if isinstance(rules, Mapping) else None
    )
    if (
        not isinstance(unique_cross_source, Mapping)
        or unique_cross_source.get("automatic") is not False
        or unique_cross_source.get(
            "requiresSourceAuthorityOrScopedProviderIdentity"
        )
        is not True
        or not isinstance(authority_policy, Mapping)
        or authority_policy.get("descriptionParticipates")
        != "exact-only"
    ):
        raise ReceiptRepairError("repair identity policy semantics are unsafe")
    if authority_policy != DEFAULT_SOURCE_AUTHORITY_POLICY.document() or {
        key: value for key, value in policy_document.items() if key != "sourceAuthority"
    } != {
        key: value for key, value in DEFAULT_POLICY.document().items()
        if key != "sourceAuthority"
    }:
        raise ReceiptRepairError("repair identity policy semantics are unsafe")


def _current_evidence(root: Path) -> dict[str, Any]:
    baseline_publication, baseline_pointer, baseline_manifest = (
        forensic._baseline_state(root, REPO_ROOT)
    )
    forensic.verify(root, repo_root=REPO_ROOT)
    forensic_publication, forensic_pointer = forensic._current(root)
    detail = _load_json(
        forensic_publication / "private-audit.json",
        "forensic detail is unavailable",
    )
    inputs, plans, receipts = forensic._read_evidence(
        root,
        baseline_manifest["sourceFiles"],
        baseline_manifest["environmentFingerprint"],
    )
    return {
        "baselinePublication": baseline_publication,
        "baselinePointer": baseline_pointer,
        "baselineManifest": baseline_manifest,
        "forensicPointer": forensic_pointer,
        "forensicDetail": detail,
        "inputs": inputs,
        "plans": [
            plan
            for plan in plans
            if plan["value"]["environmentFingerprint"]
            == baseline_manifest["environmentFingerprint"]
        ],
        "receipts": [
            receipt
            for receipt in receipts
            if receipt["value"]["environmentFingerprint"]
            == baseline_manifest["environmentFingerprint"]
        ],
    }


def build_plan(
    data_dir: str | Path,
    selection: Mapping[str, Any],
    *,
    generated_at: datetime | None = None,
    production_lineage: str | Path | None = None,
    lineage_evidence_key: bytes | None = None,
    lineage_operator_key: bytes | None = None,
) -> dict[str, Any]:
    """Build one immutable repair plan from current verified private evidence."""

    _validate_selection(selection)
    root = Path(data_dir).resolve()
    evidence = _current_evidence(root)
    observations_document, lineage, canonical_accounts, canonical_manifest_sha256 = (
        _canonical_documents(root)
    )
    baseline_id = evidence["baselinePointer"]["publicationId"]
    forensic_id = evidence["forensicPointer"]["publicationId"]
    if (
        lineage.get("baselinePublicationId") != baseline_id
        or lineage.get("forensicPublicationId") != forensic_id
    ):
        raise ReceiptRepairError(
            "canonical publication is not bound to current forensic evidence"
        )
    identity = lineage.get("identityPolicy") or {}
    _validate_repair_policy(identity)
    interval_proofs = {
        str(row.get("intervalId") or ""): row
        for row in (identity.get("sourceAuthority") or {}).get("intervals") or []
        if isinstance(row, Mapping)
    }
    if (
        (identity.get("policyDocument") or {}).get("sourceAuthority", {}).get(
            "policyHash"
        )
        != (identity.get("sourceAuthority") or {}).get("policyHash")
    ):
        raise ReceiptRepairError("repair source-authority policy binding changed")

    source_plan_sha256 = str(selection["sourceApplicationPlanSha256"])
    source_plans = [
        item for item in evidence["plans"] if item["sha256"] == source_plan_sha256
    ]
    if len(source_plans) != 1:
        raise ReceiptRepairError("source application plan is not uniquely sealed")
    source_plan_document = source_plans[0]
    receipt_status, receipt_document = forensic._receipt_for_plan(
        source_plan_document,
        evidence["receipts"],
    )
    if receipt_status != "proved" or receipt_document is None:
        raise ReceiptRepairError(
            "source application plan lacks one exact applied receipt"
        )
    source_plan = source_plan_document["value"]
    account_id = str(selection["ledgerAccountId"])
    chain = None
    continuation = None
    prior_tokens = set()
    if production_lineage is not None:
        from . import repair_lineage
        keys = repair_lineage.keys(lineage_evidence_key, lineage_operator_key)
        chain, continuation = repair_lineage.load_lineage(
            Path(production_lineage), root=root, evidence_key=keys[0], operator_key=keys[1],
        )
        if (chain["accountId"] != account_id
                or chain["sourcePlan"]["sha256"] != source_plan_sha256
                or chain["sourceReceipt"]["sha256"] != receipt_document["sha256"]):
            raise ReceiptRepairError("production continuation source application/account differs")
        prior_tokens = {
            _provider_token(row["sourceIdentity"], SOURCE_FAMILY) for row in chain["deleted"]
        }
    assertions = [
        row
        for row in source_plan.get("assertions") or []
        if str(row.get("accountId") or "") == account_id
    ]
    if len(assertions) != 1:
        raise ReceiptRepairError("repair account assertion is not unique")
    canonical_account_id = str(assertions[0].get("canonicalAccountId") or "")
    if not canonical_account_id:
        raise ReceiptRepairError("repair account has no canonical identity")
    canonical_account_rows = [
        row for row in canonical_accounts
        if row.get("account_id") == canonical_account_id
    ]
    if len(canonical_account_rows) != 1:
        raise ReceiptRepairError("canonical repair account is not unique")
    account_currency = str(canonical_account_rows[0].get("currency") or "").upper()
    if len(account_currency) != 3 or not account_currency.isalpha():
        raise ReceiptRepairError("canonical repair account currency is unavailable")

    start = date.fromisoformat(str(selection["effectiveFrom"]))
    end = date.fromisoformat(str(selection["effectiveThrough"]))
    source_creates = [
        row
        for row in (source_plan.get("operations") or {}).get("creates") or []
        if str(row.get("accountId") or "") == account_id
        and start <= date.fromisoformat(_normalized_date(row.get("activityDate"))) <= end
    ]
    if len(source_creates) != selection["expectedSourceRows"]:
        raise ReceiptRepairError("repair source-row count changed")
    create_by_token: dict[str, dict[str, Any]] = {}
    for row in source_creates:
        token = _provider_token(str(row.get("idempotencyKey") or ""), SOURCE_FAMILY)
        if token in create_by_token:
            raise ReceiptRepairError("repair source provider identity is not unique")
        create_by_token[token] = row
    previously_repaired = len(set(create_by_token) & prior_tokens)
    create_by_token = {token: row for token, row in create_by_token.items() if token not in prior_tokens}

    observations = observations_document.get("observations")
    canonical_events = lineage.get("canonicalTransactions")
    decisions = lineage.get("decisionProjections")
    if not all(
        isinstance(value, list)
        for value in (observations, canonical_events, decisions)
    ):
        raise ReceiptRepairError("canonical lineage shape is invalid")
    observation_by_id = {
        str(row.get("observationId") or ""): row for row in observations
    }
    event_by_id = {
        str(row.get("canonicalTransactionId") or ""): row
        for row in canonical_events
    }
    decision_by_id = {
        str(row.get("decisionId") or ""): row for row in decisions
    }
    source_observation_by_token: dict[str, dict[str, Any]] = {}
    for row in observations:
        transaction = row.get("transaction") or {}
        source_id = str(transaction.get("source_id") or "")
        if (
            str(transaction.get("account_id") or "") == canonical_account_id
            and source_id.startswith(f"{SOURCE_FAMILY}:")
        ):
            token = _provider_token(source_id, SOURCE_FAMILY)
            if token in create_by_token:
                if token in source_observation_by_token:
                    raise ReceiptRepairError(
                        "canonical repair source observation is not unique"
                    )
                source_observation_by_token[token] = row
    if set(source_observation_by_token) != set(create_by_token):
        raise ReceiptRepairError(
            "canonical publication does not cover every selected source row"
        )

    activities = evidence["forensicDetail"].get("activities")
    accounts = forensic._domain(
        evidence["baselinePublication"],
        evidence["baselineManifest"],
        "accounts",
    )
    if not isinstance(activities, list) or not isinstance(accounts, list):
        raise ReceiptRepairError("baseline activity inventory is unavailable")
    account_rows = [
        row for row in accounts if str(row.get("id") or "") == account_id
    ]
    if len(account_rows) != 1:
        raise ReceiptRepairError("repair account is absent from baseline")
    if str(account_rows[0].get("currency") or "").upper() != account_currency:
        raise ReceiptRepairError("canonical and ledger account currencies differ")
    account_type = str(account_rows[0].get("accountType") or "")
    raw_activities = [dict(row.get("raw") or {}) for row in activities]
    by_activity_id = {
        str(row.get("activityId") or ""): row for row in activities
    }
    by_source_identity: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_source_token: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in activities:
        ledger_account = str((row.get("raw") or {}).get("accountId") or "")
        source_identity = str(row.get("sourceIdentity") or "")
        by_source_identity[(ledger_account, source_identity)].append(row)
        if source_identity.count(":") >= 2:
            family = source_identity.split(":", 1)[0]
            by_source_token[
                (ledger_account, family, source_identity.split(":", 2)[2])
            ].append(row)
    if chain is not None:
        prior_ids = {row["activityId"] for row in chain["deleted"]}
        if prior_ids & set(by_activity_id) or any(
            by_source_token.get((account_id, SOURCE_FAMILY, token)) for token in prior_tokens
        ):
            raise ReceiptRepairError("previous production source ID or alias reappeared")
        if set(selection.get("sourceActivityIds", [])) & prior_ids:
            raise ReceiptRepairError("selection repeats a previous production deletion")
        if sum((_signed(row) for row in raw_activities if row["accountId"] == account_id), Decimal()) != \
                Decimal(chain["accountValue"]):
            raise ReceiptRepairError("continuation cash value differs from its receipt-proven anchor")
        if any(len(by_source_identity.get((account_id, row["idempotencyKey"]), [])) != 1
               for row in create_by_token.values()):
            raise ReceiptRepairError("remaining source row is missing or ambiguous")

    repairs = []
    assignment_upserts = []
    dependent_assignments = {}
    preserved = 0
    requested_ids = (
        set(selection["sourceActivityIds"]) if "sourceActivityIds" in selection else None
    )
    for token, create in sorted(create_by_token.items()):
        observation = source_observation_by_token[token]
        if requested_ids is not None and not any(
            row["activityId"] in requested_ids
            for row in by_source_identity.get(
                (account_id, str(create.get("idempotencyKey") or "")), []
            )
        ):
            preserved += 1
            continue
        if observation.get("disposition") != "suppressed":
            preserved += 1
            continue
        if observation.get("decisionType") != "automatic-identity":
            raise ReceiptRepairError("suppressed repair row is not machine-proven")
        decision = decision_by_id.get(str(observation.get("decisionId") or ""))
        if (
            decision is None
            or decision.get("outcome") != "source-suppressed"
            or decision.get("rationaleCode") != AUTHORITY_RATIONALE
            or decision.get("confidenceTier") != AUTHORITY_CONFIDENCE
            or decision.get("policyVersion") != "canonical-identity-v5"
        ):
            raise ReceiptRepairError(
                "suppressed repair row lacks authoritative coverage proof"
            )
        decision_features = dict(decision.get("featureVector") or {})
        decision_intervals = [
            interval_proofs.get(
                str(decision_features.get(key) or "")
            )
            for key in (
                "authoritativeIntervalId",
                "suppressedIntervalId",
            )
        ]
        if any(
            interval is None
            or interval.get("proven") is not True
            or interval.get("reconciled") is not True
            or (interval.get("authorityProof") or {}).get(
                "sourceArtifactBound"
            )
            != "true"
            for interval in decision_intervals
        ):
            raise ReceiptRepairError(
                "repair authority interval lacks exact artifact proof"
            )
        event = event_by_id.get(
            str(observation.get("canonicalTransactionId") or "")
        )
        if event is None:
            raise ReceiptRepairError("suppressed repair row has no canonical event")
        active = observation_by_id.get(str(event.get("activeObservationId") or ""))
        if active is None:
            raise ReceiptRepairError("canonical repair survivor is unavailable")
        active_transaction = active.get("transaction") or {}
        active_currency = _repair_currency(active_transaction, account_currency)
        active_source_id = str(active_transaction.get("source_id") or "")
        active_family = str(
            dict(decision.get("featureVector") or {}).get(
                "authoritativeSourceFamily"
            )
            or ""
        )
        if active_family not in {"ofx", "qfx"}:
            raise ReceiptRepairError(
                "repair survivor is not an authoritative OFX/QFX observation"
            )
        source_matches = by_source_identity.get(
            (account_id, str(create.get("idempotencyKey") or "")),
            [],
        )
        survivor_matches = by_source_token.get(
            (
                account_id,
                "extract",
                _provider_token(active_source_id, "extract"),
            ),
            [],
        )
        if len(source_matches) != 1 or len(survivor_matches) != 1:
            raise ReceiptRepairError(
                "repair source or survivor is not unique in the baseline"
            )
        source = source_matches[0]
        survivor = survivor_matches[0]
        source_raw = dict(source["raw"])
        survivor_raw = dict(survivor["raw"])
        if (
            _activity_core(source_raw) != _activity_core(create)
            or source["lineage"].get("status") != "receipt-proven"
            or source["lineage"].get("applicationPlanSha256")
            != source_plan_sha256
            or source_raw.get("sourceGroupId")
            or survivor_raw.get("sourceGroupId")
            or set(source["dependentState"].get("reasonCodes") or [])
            - {"category-assignment"}
            or set(survivor["dependentState"].get("reasonCodes") or [])
            - {"category-assignment"}
        ):
            raise ReceiptRepairError("repair activity graph is not safely bounded")
        if (
            _normalized_date(source_raw.get("date"))
            != _normalized_date(active_transaction.get("date"))
            or _signed(source_raw)
            != Decimal(str(active_transaction.get("amount") or "0"))
            or str(source_raw.get("currency") or "").upper()
            != active_currency
            or _normalized_date(survivor_raw.get("date"))
            != _normalized_date(active_transaction.get("date"))
            or _signed(survivor_raw)
            != Decimal(str(active_transaction.get("amount") or "0"))
            or str(survivor_raw.get("currency") or "").upper()
            != active_currency
        ):
            raise ReceiptRepairError(
                "repair activity economics differ from canonical authority"
            )
        pair_assignments = _assignment_upserts(
            str(source["activityId"]),
            source["dependentState"].get("categoryAssignments") or [],
            str(survivor["activityId"]),
            survivor["dependentState"].get("categoryAssignments") or [],
        )
        assignment_upserts.extend(pair_assignments)
        for activity in (source, survivor):
            dependent_assignments[str(activity["activityId"])] = _assignment_snapshot(
                activity["dependentState"].get("categoryAssignments") or []
            )
        repairs.append(
            {
                "canonicalTransactionId": observation["canonicalTransactionId"],
                "decisionId": observation["decisionId"],
                "decisionHash": decision["decisionHash"],
                "sourceObservationId": observation["observationId"],
                "sourceActivityId": source["activityId"],
                "sourceActivityFingerprint": activity_semantic_fingerprint(
                    source_raw
                ),
                "sourceRollbackPayload": activity_payload(source_raw),
                "survivorObservationId": active["observationId"],
                "survivorActivityId": survivor["activityId"],
                "survivorActivityFingerprint": activity_semantic_fingerprint(
                    survivor_raw
                ),
                "sourceHashes": decision["sourceHashes"],
                "authorityPolicyHash": decision["sourceAuthorityPolicyHash"],
                "featureVector": decision["featureVector"],
                "competingCandidateProof": decision[
                    "competingCandidateProof"
                ],
            }
        )

    if len(repairs) != selection["expectedSuppressions"]:
        raise ReceiptRepairError("repair suppression count changed")
    if requested_ids is not None and {
        row["sourceActivityId"] for row in repairs
    } != requested_ids:
        raise ReceiptRepairError("repair selected activities are not all proved")
    if len({row["sourceActivityId"] for row in repairs}) != len(repairs):
        raise ReceiptRepairError("repair would delete an activity twice")
    if len({row["survivorActivityId"] for row in repairs}) != len(repairs):
        raise ReceiptRepairError("repair would reuse an authoritative occurrence")

    reconciliation_rows = [
        row
        for row in source_plan.get("reconciliations") or []
        if str(row.get("accountId") or "") == account_id
        and row.get("action") == "update"
    ]
    if len(reconciliation_rows) != 1:
        raise ReceiptRepairError(
            "repair account lacks one receipt-bound reconciliation"
        )
    source_reconciliation = reconciliation_rows[0]
    reconciliation = by_activity_id.get(
        str(source_reconciliation.get("activityId") or "")
    )
    if reconciliation is None:
        raise ReceiptRepairError("repair reconciliation activity is missing")
    reconciliation_raw = dict(reconciliation["raw"])
    compensation_prestate = chain["compensationPayload"] if chain is not None else source_reconciliation["after"]
    if chain is not None and chain["reconciliationId"] != source_reconciliation["activityId"]:
        raise ReceiptRepairError("production continuation reconciliation identity differs")
    if (
        activity_semantic_fingerprint(reconciliation_raw)
        != activity_semantic_fingerprint(compensation_prestate)
        or reconciliation_raw.get("sourceGroupId")
    ):
        raise ReceiptRepairError("repair reconciliation state has drifted")
    removed_effect = sum(
        (
            _signed(by_activity_id[row["sourceActivityId"]]["raw"])
            for row in repairs
        ),
        Decimal(),
    )
    current_reconciliation_effect = _signed(reconciliation_raw)
    repaired_reconciliation_effect = current_reconciliation_effect + removed_effect
    if abs(repaired_reconciliation_effect) < Decimal("0.005"):
        raise ReceiptRepairError(
            "repair would require deleting the reconciliation activity"
        )
    reconciliation_update = _update_payload(
        reconciliation_raw,
        repaired_reconciliation_effect,
        account_type,
    )

    account_ids = {
        str(row.get("accountId") or "") for row in raw_activities
    }
    expected_rows = [
        row
        for row in raw_activities
        if str(row.get("id") or "")
        not in {item["sourceActivityId"] for item in repairs}
        and str(row.get("id") or "")
        != str(reconciliation["activityId"])
    ]
    expected_rows.append(reconciliation_update)
    plan = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "generatedAt": (
            generated_at or datetime.now(timezone.utc)
        ).isoformat(),
        "selectionHash": plan_fingerprint(dict(selection)),
        "scope": dict(selection),
        "evidence": {
            "baselinePublicationId": baseline_id,
            "forensicPublicationId": forensic_id,
            "canonicalManifestSha256": canonical_manifest_sha256,
            "canonicalGenerationHash": identity["generationHash"],
            "canonicalStateHash": identity["canonicalStateHash"],
            "identityPolicyHash": identity["policyHash"],
            "identityPolicyDocument": identity["policyDocument"],
            "sourceAuthorityHash": (
                identity.get("sourceAuthority") or {}
            ).get("authorityHash"),
            "sourceApplicationPlanSha256": source_plan_sha256,
            "sourceApplicationReceiptSha256": receipt_document["sha256"],
        },
        "counts": {
            "selectedSourceRows": len(source_creates),
            "sourceSuppressedRows": len(repairs),
            "preservedSourceRows": preserved,
            "assignmentUpserts": len(assignment_upserts),
            "reconciliationUpdates": 1,
        },
        "preconditions": {
            "activityCount": len(raw_activities),
            "dependentAssignments": dependent_assignments,
            "accountLedgerFingerprint": ledger_fingerprint(
                raw_activities, {account_id}
            ),
            "globalLedgerFingerprint": ledger_fingerprint(
                raw_activities, account_ids
            ),
        },
        "operations": {
            "repairs": sorted(
                repairs, key=lambda row: row["sourceActivityId"]
            ),
            "assignmentUpserts": sorted(
                assignment_upserts,
                key=lambda row: (row["activityId"], row["taxonomyId"]),
            ),
            "reconciliationUpdate": {
                "activityId": reconciliation["activityId"],
                "beforeFingerprint": activity_semantic_fingerprint(
                    reconciliation_raw
                ),
                "beforeEffect": format(current_reconciliation_effect, "f"),
                "removedActivityEffect": format(removed_effect, "f"),
                "afterEffect": format(repaired_reconciliation_effect, "f"),
                "payload": reconciliation_update,
                "rollbackPayload": activity_payload(reconciliation_raw),
            },
        },
        "expected": {
            "activityCount": len(expected_rows),
            "accountLedgerFingerprint": ledger_fingerprint(
                expected_rows, {account_id}
            ),
            "globalLedgerFingerprint": ledger_fingerprint(
                expected_rows, account_ids
            ),
            "accountEffectConserved": True,
            "selectedDuplicatePairsRemaining": 0,
        },
    }
    if continuation is not None:
        plan["productionLineage"] = continuation
        plan["counts"].update(
            previouslyRepairedSourceRows=previously_repaired,
            liveSourceRowsBefore=len(create_by_token),
            remainingLiveSourceRows=preserved,
        )
    plan["planHash"] = plan_fingerprint(plan)
    return plan


def validate_plan(document: object) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") not in {1, SCHEMA_VERSION}
        or document.get("kind") != KIND
        or not isinstance(document.get("operations"), dict)
        or not isinstance(document.get("counts"), dict)
        or not isinstance(document.get("preconditions"), dict)
        or not isinstance(document.get("expected"), dict)
        or not isinstance(document.get("evidence"), dict)
    ):
        raise ReceiptRepairError("repair plan shape is invalid")
    expected = plan_fingerprint(
        {key: value for key, value in document.items() if key != "planHash"}
    )
    if document.get("planHash") != expected:
        raise ReceiptRepairError("repair plan fingerprint is invalid")
    scope = document.get("scope")
    if not isinstance(scope, Mapping):
        raise ReceiptRepairError("repair plan scope is invalid")
    _validate_selection(scope)
    evidence = document["evidence"]
    required_evidence = {
        "baselinePublicationId",
        "forensicPublicationId",
        "canonicalManifestSha256",
        "canonicalGenerationHash",
        "canonicalStateHash",
        "identityPolicyHash",
        "sourceAuthorityHash",
        "sourceApplicationPlanSha256",
        "sourceApplicationReceiptSha256",
    }
    policy_fields = (
        {"identityPolicyDocument"} if document["schemaVersion"] == SCHEMA_VERSION else set()
    )
    if set(evidence) != required_evidence | policy_fields or any(
        not isinstance(evidence[key], str)
        or len(evidence[key]) != 64
        or any(character not in "0123456789abcdef" for character in evidence[key])
        for key in required_evidence
    ):
        raise ReceiptRepairError("repair plan evidence binding is invalid")
    if document["schemaVersion"] == SCHEMA_VERSION:
        policy_document = evidence["identityPolicyDocument"]
        if (
            not isinstance(policy_document, dict)
            or content_hash(policy_document) != evidence["identityPolicyHash"]
        ):
            raise ReceiptRepairError("repair plan identity policy hash is invalid")
        authority = policy_document.get("sourceAuthority")
        policy = authority.get("policy") if isinstance(authority, dict) else None
        if not isinstance(policy, dict):
            raise ReceiptRepairError("repair plan source authority is invalid")
        _validate_repair_policy({
            "policyVersion": policy_document.get("version"),
            "policyDocument": policy_document,
            "sourceAuthority": {"policyVersion": policy.get("version")},
        })
    repairs = document["operations"].get("repairs")
    assignments = document["operations"].get("assignmentUpserts")
    reconciliation = document["operations"].get("reconciliationUpdate")
    if (
        not isinstance(repairs, list)
        or not repairs
        or not isinstance(assignments, list)
        or not isinstance(reconciliation, dict)
        or document["counts"].get("sourceSuppressedRows") != len(repairs)
        or document["counts"].get("assignmentUpserts") != len(assignments)
        or document["expected"].get("activityCount")
        != document["preconditions"].get("activityCount") - len(repairs)
    ):
        raise ReceiptRepairError("repair plan operation counts are invalid")
    if any(
        not isinstance(row, Mapping)
        or any(
            not isinstance(row.get(key), str) or not row[key]
            for key in (
                "canonicalTransactionId",
                "decisionId",
                "decisionHash",
                "sourceObservationId",
                "sourceActivityId",
                "sourceActivityFingerprint",
                "survivorObservationId",
                "survivorActivityId",
                "survivorActivityFingerprint",
                "authorityPolicyHash",
            )
        )
        for row in repairs
    ):
        raise ReceiptRepairError("repair activity binding is invalid")
    if document["schemaVersion"] == SCHEMA_VERSION:
        authority = evidence["identityPolicyDocument"]["sourceAuthority"]
        authority_hash = content_hash(authority["policy"])
        intervals = {
            content_hash(interval) for interval in authority.get("intervals", [])
        }
        if (
            authority.get("policyHash") != authority_hash
            or evidence["sourceAuthorityHash"] != content_hash(authority)
        ):
            raise ReceiptRepairError("repair source-authority hash binding is invalid")
        for repair in repairs:
            features = repair.get("featureVector")
            if (
                not isinstance(features, dict)
                or repair["authorityPolicyHash"] != authority_hash
                or features.get("authorityPolicyHash") != authority_hash
                or features.get("authorityPolicyVersion") != authority["policy"]["version"]
                or features.get("descriptionRelation") != "exact"
                or features.get("authoritativeSourceFamily") not in {"ofx", "qfx"}
                or features.get("suppressedSourceFamily") != SOURCE_FAMILY
                or any(
                    features.get(name) != "true"
                    for name in (
                        "sameCanonicalAccount", "sameSourceDay",
                        "sameSignedAmountAndCurrency", "distinctSourceScope",
                        "intervalsOverlap",
                    )
                )
                or any(
                    features.get(name) not in intervals
                    for name in ("authoritativeIntervalId", "suppressedIntervalId")
                )
            ):
                raise ReceiptRepairError("repair operation contradicts the bound identity policy")
    snapshots = document["preconditions"].get("dependentAssignments")
    activity_ids = {
        str(row[key])
        for row in repairs
        for key in ("sourceActivityId", "survivorActivityId")
    }
    if (
        not isinstance(snapshots, dict)
        or set(snapshots) != activity_ids
        or any(not isinstance(rows, list) for rows in snapshots.values())
    ):
        raise ReceiptRepairError("repair dependent-state binding is invalid")
    extension = document.get("productionLineage")
    if extension is not None:
        from .repair_lineage import EXTENSION_KIND, qualification_fields
        if document["schemaVersion"] != SCHEMA_VERSION or not isinstance(extension, dict) or \
                extension.get("schemaVersion") not in {1, 2} or extension.get("kind") != EXTENSION_KIND or \
                not isinstance(extension.get("file"), dict) or \
                not isinstance(extension.get("deleted"), list):
            raise ReceiptRepairError("production continuation extension is invalid")
        if extension["schemaVersion"] == 2:
            if (extension.get("historicalMatchingReplayAvailable") is not False or
                    qualification_fields(extension).get("historicalReplayQualificationHash") !=
                    extension.get("historicalReplayQualificationHash")):
                raise ReceiptRepairError("production continuation replay qualification is invalid")
        elif any(key in extension for key in (
            "historicalMatchingReplayAvailable", "missingBindings", "gapAcknowledgments",
            "historicalReplayQualificationHash",
        )):
            raise ReceiptRepairError("unqualified continuation cannot hide a replay-gap extension")
        for name in ("chainHash", "headExecutionHash", "targetHash", "sourcePlanSha256",
                     "sourceReceiptSha256", "compensationFingerprint"):
            value = extension.get(name)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ReceiptRepairError("production continuation hash is invalid")
        prior_ids = [row.get("activityId") for row in extension["deleted"]]
        prior_aliases = [row.get("sourceIdentity") for row in extension["deleted"]]
        counts = document["counts"]
        if (len(prior_ids) != len(set(prior_ids)) or not all(prior_ids) or
                len(prior_aliases) != len(set(prior_aliases)) or not all(prior_aliases) or
                set(prior_ids) & {row["sourceActivityId"] for row in repairs} or
                extension.get("accountId") != scope["ledgerAccountId"] or
                extension.get("sourcePlanSha256") != evidence["sourceApplicationPlanSha256"] or
                extension.get("sourceReceiptSha256") != evidence["sourceApplicationReceiptSha256"] or
                extension.get("reconciliationId") != reconciliation["activityId"] or
                extension.get("compensationFingerprint") != reconciliation["beforeFingerprint"] or
                any(not isinstance(counts.get(k), int) or counts[k] < 0 for k in (
                    "previouslyRepairedSourceRows", "liveSourceRowsBefore", "remainingLiveSourceRows")) or
                counts["selectedSourceRows"] != counts["previouslyRepairedSourceRows"] + counts["liveSourceRowsBefore"] or
                counts["liveSourceRowsBefore"] != len(repairs) + counts["remainingLiveSourceRows"] or
                counts["preservedSourceRows"] != counts["remainingLiveSourceRows"]):
            raise ReceiptRepairError("production continuation scope/counts differ")
    return document


def write_plan(path: str | Path, document: dict[str, Any]) -> None:
    _write_immutable(Path(path), validate_plan(document))


def load_plan(path: str | Path) -> dict[str, Any]:
    return validate_plan(_load_json(Path(path), "repair plan is unavailable"))


def _activity_state(
    client: WealthfolioClient,
    plan: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    rows = list(client.iter_activities(page_size=500))
    by_id = {str(row.get("id") or ""): row for row in rows}
    account_ids = {str(row.get("accountId") or "") for row in rows}
    fingerprint = ledger_fingerprint(rows, account_ids)
    return rows, by_id, fingerprint


def _assignments_match(
    adapter: SpendingAdapter,
    operations: Sequence[Mapping[str, Any]],
    *,
    before: bool,
) -> bool:
    by_activity: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for operation in operations:
        by_activity[str(operation["activityId"])].append(operation)
    for activity_id, expected in by_activity.items():
        rows = adapter.assignment_rows(activity_id)
        if before:
            for operation in expected:
                snapshot = _assignment_snapshot(
                    [operation["before"]] if operation["before"] else []
                )
                actual = [
                    row
                    for row in rows
                    if str(row.get("taxonomyId") or "")
                    == str(operation["taxonomyId"])
                ]
                if _assignment_snapshot(actual) != snapshot:
                    return False
        else:
            for operation in expected:
                matching = [
                    row
                    for row in rows
                    if str(row.get("taxonomyId") or "")
                    == str(operation["taxonomyId"])
                    and str(row.get("categoryId") or "")
                    == str(operation["categoryId"])
                ]
                if (
                    len(matching) != 1
                    or str(matching[0].get("source") or "").casefold()
                    != str(operation["expectedSource"]).casefold()
                    or Decimal(str(matching[0].get("weight") or "1"))
                    != Decimal(str(operation["expectedWeight"]))
                ):
                    return False
    return True


def _target_status(
    client: WealthfolioClient,
    adapter: SpendingAdapter,
    plan: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows, by_id, fingerprint = _activity_state(client, plan)
    repairs = plan["operations"]["repairs"]
    reconciliation = plan["operations"]["reconciliationUpdate"]
    delete_ids = {str(row["sourceActivityId"]) for row in repairs}
    survivors = {
        str(row["survivorActivityId"]): str(
            row["survivorActivityFingerprint"]
        )
        for row in repairs
    }
    if fingerprint == plan["preconditions"]["globalLedgerFingerprint"]:
        if not all(
            activity_semantic_fingerprint(by_id.get(activity_id, {}))
            == expected
            for activity_id, expected in survivors.items()
        ):
            raise ReceiptRepairError("repair survivor precondition changed")
        if not _assignments_match(
            adapter, plan["operations"]["assignmentUpserts"], before=True
        ):
            raise ReceiptRepairError("repair assignment precondition changed")
        _verify_dependent_state(client, adapter, plan, before=True)
        return "ready", rows, by_id
    if fingerprint == plan["expected"]["globalLedgerFingerprint"]:
        if delete_ids & set(by_id):
            raise ReceiptRepairError("repair post-state still contains a deleted row")
        if (
            activity_semantic_fingerprint(
                by_id.get(str(reconciliation["activityId"]), {})
            )
            != activity_semantic_fingerprint(reconciliation["payload"])
            or not _assignments_match(
                adapter,
                plan["operations"]["assignmentUpserts"],
                before=False,
            )
        ):
            raise ReceiptRepairError("repair post-state is incomplete")
        _verify_dependent_state(client, adapter, plan, before=False)
        return "applied", rows, by_id
    raise ReceiptRepairError("repair target ledger fingerprint changed")


def _verify_dependent_state(
    client: WealthfolioClient,
    adapter: SpendingAdapter,
    plan: Mapping[str, Any],
    *,
    before: bool,
) -> None:
    snapshots = plan["preconditions"]["dependentAssignments"]
    deleted = {
        str(row["sourceActivityId"]) for row in plan["operations"]["repairs"]
    }
    selected = set(snapshots) if before else set(snapshots) - deleted
    spending = client.get("/spending/cash-activities")
    if not isinstance(spending, list) or any(
        not isinstance(row, dict) for row in spending
    ):
        raise ReceiptRepairError("repair spending graph is unavailable")
    if any(
        str(row.get("id") or row.get("activityId") or row.get("activity_id") or "")
        in selected
        and (row.get("eventId") or row.get("event_id"))
        for row in spending
    ):
        raise ReceiptRepairError("repair spending event link changed")
    for activity_id in sorted(selected):
        splits = client.get(
            f"/spending/activities/{quote(activity_id, safe='')}/splits"
        )
        if splits != []:
            raise ReceiptRepairError("repair split state changed")
        actual = _assignment_snapshot(adapter.assignment_rows(activity_id))
        expected = snapshots[activity_id]
        changed_taxonomies = (
            set() if before else {
                str(operation["taxonomyId"])
                for operation in plan["operations"]["assignmentUpserts"]
                if str(operation["activityId"]) == activity_id
            }
        )
        if [
            row for row in actual
            if str(row["taxonomyId"]) not in changed_taxonomies
        ] != [
            row for row in expected
            if str(row["taxonomyId"]) not in changed_taxonomies
        ]:
            raise ReceiptRepairError("repair dependent assignments changed")


def _account_value(
    client: WealthfolioClient, account_id: str
) -> tuple[Decimal, str]:
    accounts = client.get("/accounts?includeArchived=true")
    if not isinstance(accounts, list):
        raise ReceiptRepairError("repair account inventory is unavailable")
    selected = [row for row in accounts if row.get("id") == account_id]
    if len(selected) != 1:
        raise ReceiptRepairError("repair account identity is unavailable")
    if selected[0].get("accountType") == "CREDIT_CARD":
        # Wealthfolio excludes credit cards from investment performance.
        # Their balance is the signed cash ledger, not a missing/zero valuation.
        currency = selected[0].get("currency")
        activities = [
            row for row in client.iter_activities()
            if row.get("accountId") == account_id
        ]
        if not currency or not activities or any(
            row.get("assetId") or row.get("currency") != currency
            for row in activities
        ):
            raise ReceiptRepairError("repair credit-card cash ledger is invalid")
        value = sum((_signed(row) for row in activities), Decimal())
        if not value.is_finite():
            raise ReceiptRepairError("repair account value is invalid")
        return value, "cash-ledger"
    rows = client.post(
        "/performance/accounts/simple", {"accountIds": [account_id]}
    )
    matches = [
        row
        for row in (rows or [])
        if str(row.get("accountId") or "") == account_id
    ]
    if len(matches) != 1 or matches[0].get("totalValue") is None:
        raise ReceiptRepairError("repair account value is unavailable")
    value = Decimal(str(matches[0]["totalValue"]))
    if not value.is_finite():
        raise ReceiptRepairError("repair account value is invalid")
    return value, "performance"


def _canonical_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ReceiptRepairError("repair target origin is invalid")
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


def _validate_target_binding(
    client: WealthfolioClient,
    *,
    base_url: str,
    expected_instance_id: str,
    actual_plan_hash: str,
    supplied_plan_hash: str,
    marker: str | None,
) -> None:
    client_base = getattr(client, "base", None)
    if not isinstance(client_base, str) or _canonical_origin(
        client_base
    ) != _canonical_origin(base_url):
        raise ReceiptRepairError("repair client origin differs from validated target")
    validate_apply_target(
        client,
        base_url,
        actual_plan_hash,
        supplied_plan_hash,
        expected_instance_id,
    )
    identity = inspect_rebuild_target(client, base_url, marker)
    if identity.fingerprint != expected_instance_id:
        raise ReceiptRepairError("repair clone identity changed")


def _verify_receipt_backup(
    backup: Mapping[str, Any],
    *,
    data_dir: str | Path,
) -> dict[str, Any]:
    if set(backup) != {"path", "size", "sha256", "schemaMigration"}:
        raise ReceiptRepairError("repair receipt backup evidence is incomplete")
    verified = verify_backup_file(
        Path(data_dir).resolve() / str(backup["path"]),
        data_dir=data_dir,
        repo_root=REPO_ROOT,
    )
    if verified != dict(backup):
        raise ReceiptRepairError("repair receipt backup evidence changed")
    return verified


def _validate_applied_receipt(
    receipt: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    expected_instance_id: str,
    data_dir: str | Path,
    recovery: Mapping[str, Any],
) -> dict[str, Any]:
    receipt_hash = str(receipt.get("receiptHash") or "")
    if (
        receipt.get("schemaVersion") != plan["schemaVersion"]
        or receipt.get("kind") != f"{KIND}-receipt"
        or receipt.get("status") != "applied"
        or receipt.get("planHash") != plan["planHash"]
        or receipt.get("instanceId") != expected_instance_id
        or receipt_hash
        != plan_fingerprint(
            {
                key: value
                for key, value in receipt.items()
                if key != "receiptHash"
            }
        )
        or receipt.get("operationCounts")
        != {
            "deleted": len(plan["operations"]["repairs"]),
            "assignmentUpserts": len(
                plan["operations"]["assignmentUpserts"]
            ),
            "reconciliationUpdates": 1,
        }
        or receipt.get("postLedgerFingerprint")
        != plan["expected"]["globalLedgerFingerprint"]
        or receipt.get("accountValueConserved") is not True
        or receipt.get("accountValueBasis") not in {"cash-ledger", "performance"}
        or not isinstance(receipt.get("accountValueBefore"), str)
        or receipt.get("accountValueBefore") != receipt.get("accountValueAfter")
        or not isinstance(receipt.get("backup"), Mapping)
        or receipt.get("recoveryHash") != recovery.get("recoveryHash")
    ):
        raise ReceiptRepairError("repair receipt binding is invalid")
    _validate_recovery(
        recovery,
        plan,
        expected_instance_id=expected_instance_id,
        data_dir=data_dir,
    )
    if dict(receipt["backup"]) != dict(recovery["backup"]):
        raise ReceiptRepairError("repair receipt backup binding changed")
    return dict(receipt)


def _validate_recovery(
    recovery: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    expected_instance_id: str,
    data_dir: str | Path,
) -> dict[str, Any]:
    recovery_hash = str(recovery.get("recoveryHash") or "")
    if (
        recovery.get("schemaVersion") != plan["schemaVersion"]
        or recovery.get("kind") != f"{KIND}-recovery"
        or recovery.get("status") != "prepared"
        or recovery.get("planHash") != plan["planHash"]
        or recovery.get("instanceId") != expected_instance_id
        or recovery.get("preLedgerFingerprint")
        != plan["preconditions"]["globalLedgerFingerprint"]
        or recovery_hash
        != plan_fingerprint(
            {
                key: value
                for key, value in recovery.items()
                if key != "recoveryHash"
            }
        )
        or not isinstance(recovery.get("backup"), Mapping)
    ):
        raise ReceiptRepairError("repair recovery binding is invalid")
    _verify_receipt_backup(recovery["backup"], data_dir=data_dir)
    return dict(recovery)


def apply_plan(
    client: WealthfolioClient,
    plan: Mapping[str, Any],
    *,
    base_url: str,
    expected_instance_id: str,
    supplied_plan_hash: str,
    marker: str | None,
    backup_download: str | Path,
    data_dir: str | Path,
    record_recovery: Callable[[dict[str, Any]], None],
    lineage_evidence_key: bytes | None = None,
    lineage_operator_key: bytes | None = None,
) -> dict[str, Any]:
    validated = validate_plan(dict(plan))
    if validated["schemaVersion"] != SCHEMA_VERSION:
        raise ReceiptRepairError(
            "historical repair plan is verification-only; rebuild with current identity evidence"
        )
    if validated.get("productionLineage") is not None:
        from .repair_lineage import verify_plan_lineage
        verify_plan_lineage(validated, Path(data_dir).resolve(), evidence_key=lineage_evidence_key,
                            operator_key=lineage_operator_key)
    _validate_target_binding(
        client,
        base_url=base_url,
        expected_instance_id=expected_instance_id,
        actual_plan_hash=str(validated["planHash"]),
        supplied_plan_hash=supplied_plan_hash,
        marker=marker,
    )
    adapter = SpendingAdapter(client)
    status, _rows, _by_id = _target_status(client, adapter, validated)
    if status == "applied":
        raise ReceiptRepairError(
            "repair is already applied; verify its original sealed receipt"
        )
    account_id = str(validated["scope"]["ledgerAccountId"])
    before_value, value_basis = _account_value(client, account_id)
    backup = require_unique_backup(client)
    backup_evidence = download_backup(
        client,
        str(backup["filename"]),
        backup_download,
        data_dir=data_dir,
        repo_root=REPO_ROOT,
    )
    recovery = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": f"{KIND}-recovery",
        "status": "prepared",
        "preparedAt": datetime.now(timezone.utc).isoformat(),
        "planHash": validated["planHash"],
        "instanceId": expected_instance_id,
        "preLedgerFingerprint": validated["preconditions"][
            "globalLedgerFingerprint"
        ],
        "backup": backup_evidence,
    }
    recovery["recoveryHash"] = plan_fingerprint(recovery)
    record_recovery(recovery)
    status, _rows, _by_id = _target_status(client, adapter, validated)
    if status != "ready":
        raise ReceiptRepairError("repair target changed while preparing recovery")
    for operation in validated["operations"]["assignmentUpserts"]:
        adapter.assign(
            str(operation["activityId"]),
            str(operation["taxonomyId"]),
            str(operation["categoryId"]),
        )
    result = client.save_activities(
        updates=[validated["operations"]["reconciliationUpdate"]["payload"]],
        delete_ids=[
            str(row["sourceActivityId"])
            for row in validated["operations"]["repairs"]
        ],
    )
    if not isinstance(result, dict) or result.get("errors"):
        raise ReceiptRepairError(
            "repair mutation was rejected; restore the sealed backup"
        )
    # Bulk mutation already emits scoped domain events. The explicit recalculate
    # endpoint instead rebuilds every account and backfills market history.
    wait_for_recalculation(client, [account_id])
    status, _rows, _by_id = _target_status(client, adapter, validated)
    if status != "applied":
        raise ReceiptRepairError(
            "repair postcondition failed; restore the sealed backup"
        )
    after_value, after_basis = _account_value(client, account_id)
    if after_value != before_value or after_basis != value_basis:
        raise ReceiptRepairError(
            "repair changed the account value; restore the sealed backup"
        )
    receipt = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": f"{KIND}-receipt",
        "status": "applied",
        "appliedAt": datetime.now(timezone.utc).isoformat(),
        "planHash": validated["planHash"],
        "instanceId": expected_instance_id,
        "operationCounts": {
            "deleted": len(validated["operations"]["repairs"]),
            "assignmentUpserts": len(
                validated["operations"]["assignmentUpserts"]
            ),
            "reconciliationUpdates": 1,
        },
        "preLedgerFingerprint": validated["preconditions"][
            "globalLedgerFingerprint"
        ],
        "postLedgerFingerprint": validated["expected"][
            "globalLedgerFingerprint"
        ],
        "accountValueConserved": True,
        "accountValueBasis": value_basis,
        "accountValueBefore": format(before_value.normalize(), "f"),
        "accountValueAfter": format(after_value.normalize(), "f"),
        "backup": backup_evidence,
        "recoveryHash": recovery["recoveryHash"],
    }
    receipt["receiptHash"] = plan_fingerprint(receipt)
    return receipt


def verify_target(
    client: WealthfolioClient,
    plan: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    base_url: str,
    expected_instance_id: str,
    supplied_plan_hash: str,
    data_dir: str | Path,
    recovery: Mapping[str, Any],
    marker: str | None,
) -> dict[str, Any]:
    validated = validate_plan(dict(plan))
    _validate_target_binding(
        client,
        base_url=base_url,
        expected_instance_id=expected_instance_id,
        actual_plan_hash=str(validated["planHash"]),
        supplied_plan_hash=supplied_plan_hash,
        marker=marker,
    )
    sealed_receipt = _validate_applied_receipt(
        receipt,
        validated,
        expected_instance_id=expected_instance_id,
        data_dir=data_dir,
        recovery=recovery,
    )
    adapter = SpendingAdapter(client)
    status, rows, _by_id = _target_status(client, adapter, validated)
    if status != "applied":
        raise ReceiptRepairError("repair target is not in the expected post-state")
    account_id = str(validated["scope"]["ledgerAccountId"])
    account_rows = [
        row for row in rows if str(row.get("accountId") or "") == account_id
    ]
    source_keys = {
        str(row["sourceRollbackPayload"].get("idempotencyKey") or "")
        for row in validated["operations"]["repairs"]
    }
    survivor_ids = {
        str(row["survivorActivityId"])
        for row in validated["operations"]["repairs"]
    }
    if any(
        str(row.get("idempotencyKey") or "") in source_keys
        for row in account_rows
    ) or not survivor_ids <= {
        str(row.get("id") or "") for row in account_rows
    }:
        raise ReceiptRepairError("repair duplicate signature remains")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": f"{KIND}-verification",
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "planHash": validated["planHash"],
        "receiptHash": sealed_receipt["receiptHash"],
        "instanceId": expected_instance_id,
        "activityCount": len(rows),
        "deletedCount": len(validated["operations"]["repairs"]),
        "preservedSelectedSourceRows": validated["counts"][
            "preservedSourceRows"
        ],
        "selectedDuplicatePairsRemaining": 0,
        "globalLedgerFingerprint": validated["expected"][
            "globalLedgerFingerprint"
        ],
        "accountEffectConserved": True,
    }


def write_receipt(path: str | Path, document: dict[str, Any]) -> None:
    _write_immutable(Path(path), document)
