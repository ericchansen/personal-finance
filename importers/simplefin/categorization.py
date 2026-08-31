"""Deterministic, private SimpleFIN spending categorization plans."""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint, validate_private_output
from importers.simplefin.pipeline import normalize_description

SPENDING_TAXONOMY = "spending_categories"
INCOME_TAXONOMY = "income_sources"
REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSFER_TYPES = frozenset({"TRANSFER_IN", "TRANSFER_OUT"})
INCOME_TYPES = frozenset({"DEPOSIT", "CREDIT", "INTEREST", "DIVIDEND"})
OUTFLOW_TYPES = frozenset({"WITHDRAWAL", "FEE", "TAX", "EXPENSE"})
NON_CONSUMPTION_CATEGORIES = frozenset({
    "buy",
    "credit",
    "debit",
    "deposit",
    "inflow",
    "outflow",
    "sell",
    "transfer",
    "transfer_in",
    "transfer_out",
    "uncategorized",
    "withdrawal",
    "xfer",
})
MIN_HISTORY_COUNT = 2

# Generic vocabulary translation only. User-specific judgments belong in the
# private category-decisions.json file.
DEFAULT_CATEGORY_ALIASES = {
    "alcohol": {"spending": "Bars & Alcohol"},
    "business auto expenses": {"spending": "Transportation"},
    "charity": {"spending": "Gifts & Donations"},
    "entertainment recreation": {"spending": "Entertainment"},
    "financial fees": {"spending": "Fees & Charges"},
    "fitness": {"spending": "Gym & Fitness"},
    "gas": {"spending": "Gas & Fuel"},
    "gas electric": {"spending": "Utilities"},
    "gifts": {
        "income": "Gifts Received",
        "spending": "Gifts & Donations",
    },
    "home costs": {"spending": "Housing"},
    "interest": {
        "income": "Interest",
        "spending": "Interest Charges",
    },
    "landlord income": {"income": "Rental Income"},
    "maintenance": {"spending": "Maintenance & Repairs"},
    "miscellaneous": {"spending": "Other Expenses"},
    "mortgage": {"spending": "Rent/Mortgage"},
    "other government fees": {"spending": "Fees & Charges"},
    "paychecks": {"income": "Salary"},
    "public transportation": {"spending": "Public Transit"},
    "rent": {"spending": "Rent/Mortgage"},
    "taxi ride sharing": {"spending": "Rideshare & Taxi"},
    "travel vacation": {"spending": "Travel"},
    "tv internet": {"spending": "Internet"},
    "water": {"spending": "Utilities"},
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merchant_hash(description: str, hash_key: bytes) -> str:
    normalized = normalize_description(description)
    return hmac.new(hash_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_data_dir(data_dir: Path) -> Path:
    """Resolve and reject private data roots contained by this public repository."""
    resolved = data_dir.resolve()
    validate_private_output(resolved, resolved, REPO_ROOT)
    return resolved


def load_or_create_hash_key(data_dir: Path) -> tuple[Path, bytes]:
    """Load the stable private HMAC key used to redact merchant descriptions."""
    data_dir = validate_data_dir(data_dir)
    path = data_dir / "simplefin" / "category-hash-key.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        key = os.urandom(32).hex()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(key + "\n")
            output.flush()
            os.fsync(output.fileno())
    try:
        encoded = path.read_text(encoding="ascii").strip()
        key_bytes = bytes.fromhex(encoded)
    except (OSError, ValueError) as exc:
        raise DecisionError(f"cannot read private category hash key: {exc}") from None
    if len(key_bytes) != 32:
        raise DecisionError("category-hash-key.txt must contain 32 random bytes")
    return path, key_bytes


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _assignment_category(rows: Any) -> tuple[str, str] | None:
    if not isinstance(rows, list):
        raise DecisionError("activity category assignments must be a list")
    categories = [
        (str(row.get("taxonomyId") or ""), str(row.get("categoryId") or ""))
        for row in rows
        if isinstance(row, dict)
        and row.get("taxonomyId") in {SPENDING_TAXONOMY, INCOME_TAXONOMY}
    ]
    categories = [item for item in categories if all(item)]
    if len(categories) > 1:
        raise DecisionError("activity has multiple cash-flow category assignments")
    return categories[0] if categories else None


@dataclass
class HistoryIndex:
    by_account: dict[tuple[str, str], dict[str, set[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )
    global_merchants: dict[str, dict[str, set[tuple[str, str]]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )

    def add(
        self,
        account_id: str,
        description: str,
        category: str,
        source_id: str,
    ) -> None:
        normalized = normalize_description(description)
        if not normalized or not category:
            return
        if category.casefold() in NON_CONSUMPTION_CATEGORIES:
            return
        self.by_account[(account_id, normalized)][category].add(source_id)
        self.global_merchants[normalized][category].add((account_id, source_id))


def load_history(canonical_path: Path, monarch_path: Path | None) -> HistoryIndex:
    """Load canonical categories, using Monarch statement text only as an alias."""
    index = HistoryIndex()
    canonical_by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    with canonical_path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            source_id = str(row.get("source_id") or "")
            canonical_by_source[source_id].append(row)
            if (
                str(row.get("excluded") or "").casefold() == "true"
                or row.get("transfer_group")
            ):
                continue
            index.add(
                str(row.get("account_id") or ""),
                str(row.get("description") or ""),
                str(row.get("category") or "").strip(),
                source_id,
            )

    if monarch_path is None:
        return index
    with monarch_path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            matches = canonical_by_source.get(
                f"monarch:{str(row.get('Id') or '').strip()}", []
            )
            for canonical in matches:
                category = str(canonical.get("category") or "").strip()
                if (
                    str(canonical.get("excluded") or "").casefold() == "true"
                    or canonical.get("transfer_group")
                ):
                    continue
                source_id = str(canonical.get("source_id") or "")
                aliases = {
                    str(row.get("Merchant") or ""),
                    str(row.get("Original Statement") or ""),
                }
                for alias in aliases:
                    index.add(
                        str(canonical.get("account_id") or ""),
                        alias,
                        category,
                        source_id,
                    )
    return index


def load_category_decisions(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {
            "schemaVersion": 1,
            "categoryAliases": {},
            "merchantOverrides": [],
            "activityOverrides": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionError(f"cannot read private category decisions: {exc}") from None
    if payload.get("schemaVersion") != 1:
        raise DecisionError("category-decisions.json must have schemaVersion 1")
    for key, expected in (
        ("categoryAliases", dict),
        ("merchantOverrides", list),
        ("activityOverrides", list),
    ):
        if not isinstance(payload.get(key, expected()), expected):
            raise DecisionError(f"category-decisions.json {key} has invalid shape")
    return {
        "schemaVersion": 1,
        "categoryAliases": payload.get("categoryAliases", {}),
        "merchantOverrides": payload.get("merchantOverrides", []),
        "activityOverrides": payload.get("activityOverrides", []),
    }


def evidence_binding(paths: Iterable[Path]) -> list[dict[str, str]]:
    unique = sorted({path.resolve() for path in paths}, key=str)
    return [{"path": str(path), "sha256": sha256_file(path)} for path in unique]


def _catalog_index(catalogs: dict[str, list[dict[str, Any]]]) -> dict[
    tuple[str, str], dict[str, Any]
]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for taxonomy_id, categories in catalogs.items():
        for category in categories:
            category_id = str(category.get("id") or "")
            name = str(category.get("name") or "")
            if category_id and name:
                result[(taxonomy_id, normalize_description(name))] = category
    return result


def _category_by_id(
    catalogs: dict[str, list[dict[str, Any]]]
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (taxonomy_id, str(category["id"])): category
        for taxonomy_id, categories in catalogs.items()
        for category in categories
        if category.get("id")
    }


def _resolve_category(
    historical_category: str,
    bucket: str,
    catalogs: dict[str, list[dict[str, Any]]],
    private_aliases: dict[str, Any],
) -> dict[str, str] | None:
    taxonomy_id = INCOME_TAXONOMY if bucket == "income" else SPENDING_TAXONOMY
    index = _catalog_index(catalogs)
    normalized = normalize_description(historical_category)
    exact = index.get((taxonomy_id, normalized))
    if exact:
        return {
            "taxonomyId": taxonomy_id,
            "categoryId": str(exact["id"]),
            "categoryName": str(exact["name"]),
        }
    aliases = {
        **DEFAULT_CATEGORY_ALIASES,
        **{
            normalize_description(str(key)): value
            for key, value in private_aliases.items()
        },
    }
    target = aliases.get(normalized, {})
    if isinstance(target, str):
        target = {bucket: target}
    if not isinstance(target, dict):
        raise DecisionError("private category alias must be a string or object")
    target_name = target.get(bucket)
    if not target_name:
        return None
    category = index.get((taxonomy_id, normalize_description(str(target_name))))
    if not category:
        raise DecisionError(
            f"category alias target does not exist in {taxonomy_id}: {target_name}"
        )
    return {
        "taxonomyId": taxonomy_id,
        "categoryId": str(category["id"]),
        "categoryName": str(category["name"]),
    }


def _reviewed_identities(reviewed: dict[str, Any]) -> dict[
    tuple[str, str], dict[str, str]
]:
    result: dict[tuple[str, str], dict[str, str]] = {}
    for account in reviewed.get("accounts", []):
        target = str(account.get("wealthfolioAccountId") or "")
        canonical = str(account.get("assertionAccountId") or "")
        source_account = str(account.get("sourceAccountId") or "")
        if not all((target, canonical, source_account)):
            continue
        for transaction in account.get("transactions", []):
            source_id = str(transaction.get("sourceId") or "")
            if source_id:
                result[(target, source_id)] = {
                    "canonicalAccountId": canonical,
                    "sourceAccountId": source_account,
                    "sourceId": source_id,
                }
    return result


def _source_id(activity: dict[str, Any]) -> str:
    account_id = str(activity.get("accountId") or "")
    prefix = f"simplefin:{account_id}:"
    key = str(activity.get("idempotencyKey") or "")
    return key[len(prefix):] if key.startswith(prefix) else ""


def _cash_bucket(account_type: str, activity_type: str) -> str | None:
    if activity_type in TRANSFER_TYPES:
        return None
    if account_type == "CASH" and activity_type in INCOME_TYPES:
        return "income"
    if account_type in {"CASH", "CREDIT_CARD"} and (
        activity_type in OUTFLOW_TYPES
        or account_type == "CREDIT_CARD" and activity_type == "CREDIT"
    ):
        return "spending"
    return None


def _cash_amount(account_type: str, activity_type: str, amount: Decimal) -> Decimal:
    if account_type == "CREDIT_CARD" and activity_type == "CREDIT":
        return -abs(amount)
    return abs(amount)


def _portable_activity(
    activity: dict[str, Any],
    identity: dict[str, str],
    hash_key: bytes,
) -> dict[str, Any]:
    description = str(
        activity.get("comment")
        or activity.get("notes")
        or activity.get("description")
        or ""
    )
    return {
        **identity,
        "activityType": str(activity.get("activityType") or ""),
        "date": str(activity.get("date") or "")[:10],
        "amount": _money(abs(Decimal(str(activity.get("amount") or 0)))),
        "currency": str(activity.get("currency") or ""),
        "merchantHash": merchant_hash(description, hash_key),
    }


def _validate_override_category(
    override: dict[str, Any],
    catalogs: dict[str, list[dict[str, Any]]],
    bucket: str,
) -> dict[str, str]:
    taxonomy_id = str(override.get("taxonomyId") or "")
    category_id = str(override.get("categoryId") or "")
    category = _category_by_id(catalogs).get((taxonomy_id, category_id))
    if taxonomy_id not in {SPENDING_TAXONOMY, INCOME_TAXONOMY} or not category:
        raise DecisionError("private category override references an unknown category")
    expected_taxonomy = (
        INCOME_TAXONOMY if bucket == "income" else SPENDING_TAXONOMY
    )
    if taxonomy_id != expected_taxonomy:
        raise DecisionError("private category override conflicts with cash-flow direction")
    rationale = str(override.get("rationale") or "").strip()
    if not rationale:
        raise DecisionError("private category override requires a rationale")
    return {
        "taxonomyId": taxonomy_id,
        "categoryId": category_id,
        "categoryName": str(category["name"]),
    }


def _find_override(
    identity: dict[str, str],
    hashed_merchant: str,
    decisions: dict[str, Any],
    catalogs: dict[str, list[dict[str, Any]]],
    bucket: str,
) -> tuple[dict[str, str], str] | None:
    activity_matches = [
        row
        for row in decisions["activityOverrides"]
        if str(row.get("sourceAccountId") or "") == identity["sourceAccountId"]
        and str(row.get("sourceId") or "") == identity["sourceId"]
    ]
    merchant_matches = [
        row
        for row in decisions["merchantOverrides"]
        if str(row.get("merchantHash") or "") == hashed_merchant
        and (
            not row.get("canonicalAccountId")
            or str(row["canonicalAccountId"]) == identity["canonicalAccountId"]
        )
    ]
    matches = activity_matches + merchant_matches
    if len(matches) > 1:
        raise DecisionError("multiple private category overrides match one activity")
    if not matches:
        return None
    kind = "activity-override" if activity_matches else "merchant-override"
    return _validate_override_category(matches[0], catalogs, bucket), kind


def _history_choice(
    index: HistoryIndex,
    canonical_account_id: str,
    normalized_merchant: str,
) -> tuple[str, dict[str, Any]] | None:
    global_evidence = index.global_merchants.get(normalized_merchant, {})
    if len(global_evidence) > 1:
        return "conflicting-history", global_evidence
    account_evidence = index.by_account.get(
        (canonical_account_id, normalized_merchant), {}
    )
    if len(account_evidence) > 1:
        return "conflicting-history", account_evidence
    if account_evidence:
        return "account-history", account_evidence
    if global_evidence:
        return "global-history", global_evidence
    return None


def _is_external_flow(activity: dict[str, Any]) -> bool:
    metadata = _metadata(activity.get("metadata"))
    flow = metadata.get("flow")
    return (
        str(activity.get("subtype") or "").strip().casefold()
        == "external_transfer"
        or isinstance(flow, dict)
        and flow.get("is_external") is True
    )


def _transfer_kind(activity: dict[str, Any]) -> str:
    if _is_external_flow(activity):
        return "external-reconciliation"
    if activity.get("sourceGroupId"):
        return "paired-transfer"
    return "unpaired-transfer"


def build_category_plan(
    activities: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    assignments: dict[str, list[dict[str, Any]]],
    catalogs: dict[str, list[dict[str, Any]]],
    reviewed: dict[str, Any],
    history: HistoryIndex,
    decisions: dict[str, Any],
    evidence: list[dict[str, str]],
    environment_fingerprint: str,
    merchant_hash_key: bytes,
    *,
    report_start: str,
    report_end: str,
    spending_account_ids: set[str] | None = None,
    current_report: dict[str, Any] | None = None,
    current_uncategorized_count: int | None = None,
    known_artifact_activity_ids: set[str] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a sealed category-only plan without exposing merchant text."""
    if report_end < report_start:
        raise DecisionError("category report end date precedes its start date")
    generated_at = generated_at or datetime.now(timezone.utc)
    known_artifact_activity_ids = known_artifact_activity_ids or set()
    by_account = {str(row["id"]): row for row in accounts}
    identities = _reviewed_identities(reviewed)
    simplefin = [
        row
        for row in activities
        if str(row.get("idempotencyKey") or "").startswith("simplefin:")
        and report_start <= str(row.get("date") or "")[:10] <= report_end
    ]
    reconciliation = [
        {
            "activityId": str(row.get("id") or ""),
            "date": str(row.get("date") or "")[:10],
            "amount": _money(abs(Decimal(str(row.get("amount") or 0)))),
            "activityType": str(row.get("activityType") or ""),
            "classification": "balance-gap-reconciliation",
            "knownSpendingArtifact": str(row.get("id") or "")
            in known_artifact_activity_ids,
            "categoryAction": "none",
        }
        for row in activities
        if str(row.get("idempotencyKey") or "").startswith("gap:")
        and report_start <= str(row.get("date") or "")[:10] <= report_end
    ]
    if spending_account_ids is None:
        spending_account_ids = set(by_account)
    auto: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    transfers: list[dict[str, Any]] = []
    category_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    uncategorized_totals: dict[str, Decimal] = defaultdict(Decimal)
    cash_totals: dict[str, Decimal] = defaultdict(Decimal)
    eligible_amount = Decimal()
    auto_amount = Decimal()
    pre_state: list[dict[str, Any]] = []

    for activity in simplefin:
        activity_id = str(activity.get("id") or "")
        account_id = str(activity.get("accountId") or "")
        source_id = _source_id(activity)
        activity_type = str(activity.get("activityType") or "")
        amount = abs(Decimal(str(activity.get("amount") or 0)))
        description = str(
            activity.get("comment")
            or activity.get("notes")
            or activity.get("description")
            or ""
        )
        current_category = _assignment_category(assignments.get(activity_id, []))
        pre_state.append({
            "activityId": activity_id,
            "accountId": account_id,
            "sourceId": source_id,
            "activityType": activity_type,
            "date": str(activity.get("date") or "")[:10],
            "amount": _money(amount),
            "assignment": list(current_category) if current_category else None,
        })
        if activity_type in TRANSFER_TYPES or _is_external_flow(activity):
            transfers.append({
                "activityId": activity_id,
                "sourceId": source_id,
                "date": str(activity.get("date") or "")[:10],
                "amount": _money(amount),
                "activityType": activity_type,
                "classification": _transfer_kind(activity),
                "categoryAction": "none",
            })
            continue

        eligible_amount += amount
        account = by_account.get(account_id, {})
        account_type = str(account.get("accountType") or "")
        bucket = _cash_bucket(account_type, activity_type)
        activity_date = str(activity.get("date") or "")[:10]
        if (
            bucket
            and account_id in spending_account_ids
            and report_start <= activity_date <= report_end
        ):
            cash_totals[bucket] += _cash_amount(account_type, activity_type, amount)
        identity = identities.get((account_id, source_id))
        base = {
            "activityId": activity_id,
            "sourceId": source_id,
            "date": activity_date,
            "amount": _money(amount),
            "merchantHash": merchant_hash(description, merchant_hash_key),
        }
        if current_category:
            manual.append({
                **base,
                "reason": "already-categorized",
                "currentCategory": {
                    "taxonomyId": current_category[0],
                    "categoryId": current_category[1],
                },
            })
            continue
        if account_id not in spending_account_ids:
            manual.append({**base, "reason": "account-not-spending-enabled"})
            continue
        if not identity:
            manual.append({**base, "reason": "missing-reviewed-source-identity"})
            continue
        portable = _portable_activity(activity, identity, merchant_hash_key)
        if not normalize_description(description):
            manual.append({**base, **identity, "reason": "missing-description"})
            continue

        override = _find_override(
            identity,
            base["merchantHash"],
            decisions,
            catalogs,
            bucket or "spending",
        )
        if override:
            category, source = override
            confidence = "1.00"
            evidence_count = 1
            historical_category = None
        else:
            choice = _history_choice(
                history,
                identity["canonicalAccountId"],
                normalize_description(description),
            )
            if not choice:
                manual.append({**base, **identity, "reason": "no-history"})
                continue
            source, categories = choice
            if source == "conflicting-history":
                manual.append({
                    **base,
                    **identity,
                    "reason": source,
                    "candidateCategoryCount": len(categories),
                    "evidenceHash": plan_fingerprint({
                        key: sorted(value) for key, value in sorted(categories.items())
                    }),
                })
                continue
            historical_category, sources = next(iter(categories.items()))
            evidence_count = len(sources)
            category = _resolve_category(
                historical_category,
                bucket or "spending",
                catalogs,
                decisions["categoryAliases"],
            )
            if not category:
                manual.append({
                    **base,
                    **identity,
                    "reason": "unmapped-category",
                    "historicalCategory": historical_category,
                    "evidenceCount": evidence_count,
                })
                continue
            confidence = "0.98" if source == "account-history" else "0.95"
            if evidence_count < MIN_HISTORY_COUNT:
                manual.append({
                    **base,
                    **identity,
                    "reason": "insufficient-history",
                    "historicalCategory": historical_category,
                    "evidenceCount": evidence_count,
                    "candidate": category,
                    "confidence": confidence,
                })
                continue

        candidate = {
            **base,
            **identity,
            "portableActivity": portable,
            "taxonomyId": category["taxonomyId"],
            "categoryId": category["categoryId"],
            "categoryName": category["categoryName"],
            "confidence": confidence,
            "evidenceKind": source,
            "evidenceCount": evidence_count,
            "evidenceHash": plan_fingerprint({
                "merchantHash": base["merchantHash"],
                "canonicalAccountId": identity["canonicalAccountId"],
                "historicalCategory": historical_category,
                "evidenceKind": source,
                "evidenceCount": evidence_count,
            }),
        }
        auto.append(candidate)
        auto_amount += amount
        on = base["date"]
        if report_start <= on <= report_end:
            category_totals[
                (category["taxonomyId"], category["categoryId"], category["categoryName"])
            ] += _cash_amount(account_type, activity_type, amount)

    auto_ids = {row["activityId"] for row in auto}
    uncategorized_reasons = {
        "conflicting-history",
        "insufficient-history",
        "missing-description",
        "missing-reviewed-source-identity",
        "no-history",
        "unmapped-category",
    }
    for row in manual:
        if row["reason"] not in uncategorized_reasons:
            continue
        if report_start <= row["date"] <= report_end:
            activity = next(
                item for item in simplefin if str(item.get("id")) == row["activityId"]
            )
            account_type = str(
                by_account.get(str(activity.get("accountId") or ""), {}).get(
                    "accountType"
                )
                or ""
            )
            bucket = _cash_bucket(account_type, str(activity.get("activityType") or ""))
            if bucket:
                uncategorized_totals[bucket] += _cash_amount(
                    account_type,
                    str(activity.get("activityType") or ""),
                    abs(Decimal(str(activity.get("amount") or 0))),
                )

    current = (current_report or {}).get("current") or {}
    current_income = Decimal(str(current.get("income") or 0))
    current_spending = Decimal(str(current.get("outflow") or 0))
    known_gap_amount = sum(
        (
            Decimal(row["amount"])
            for row in reconciliation
            if row["knownSpendingArtifact"]
        ),
        Decimal(),
    )
    adjusted_current_spending = current_spending - known_gap_amount
    document = {
        "schemaVersion": 1,
        "mode": "category-plan-only",
        "generatedAt": generated_at.isoformat(),
        "productionMutated": False,
        "environmentFingerprint": environment_fingerprint,
        "evidence": evidence,
        "reviewWindow": {"startDate": report_start, "endDate": report_end},
        "preCategoryFingerprint": plan_fingerprint(
            sorted(pre_state, key=lambda row: row["activityId"])
        ),
        "autoCandidates": sorted(auto, key=lambda row: row["activityId"]),
        "manualItems": sorted(manual, key=lambda row: row["activityId"]),
        "transfersAndReconciliation": sorted(
            transfers, key=lambda row: row["activityId"]
        ),
        "balanceGapReconciliation": sorted(
            reconciliation, key=lambda row: row["activityId"]
        ),
        "metrics": {
            "simplefinActivities": len(simplefin),
            "eligibleNonTransfers": len(simplefin) - len(transfers),
            "eligibleAmount": _money(eligible_amount),
            "autoCount": len(auto),
            "autoAmount": _money(auto_amount),
            "manualCount": len(manual),
            "manualAmount": _money(eligible_amount - auto_amount),
            "transferCount": len(transfers),
            "transferAmount": _money(
                sum((Decimal(row["amount"]) for row in transfers), Decimal())
            ),
            "externalReconciliationCount": sum(
                row["classification"] == "external-reconciliation"
                for row in transfers
            ),
            "externalReconciliationAmount": _money(
                sum(
                    (
                        Decimal(row["amount"])
                        for row in transfers
                        if row["classification"] == "external-reconciliation"
                    ),
                    Decimal(),
                )
            ),
            "balanceGapCount": len(reconciliation),
            "balanceGapAmount": _money(
                sum((Decimal(row["amount"]) for row in reconciliation), Decimal())
            ),
            "knownGapArtifactCount": sum(
                row["knownSpendingArtifact"] for row in reconciliation
            ),
            "knownGapArtifactAmount": _money(
                known_gap_amount
            ),
        },
        "predictedCategoryTotals": [
            {
                "taxonomyId": taxonomy_id,
                "categoryId": category_id,
                "categoryName": name,
                "amount": _money(amount),
            }
            for (taxonomy_id, category_id, name), amount in sorted(
                category_totals.items()
            )
        ],
        "uncategorizedWindowTotals": {
            key: _money(value) for key, value in sorted(uncategorized_totals.items())
        },
        "cashFlowExcludingTransfersAndReconciliation": {
            "income": _money(cash_totals["income"]),
            "spending": _money(cash_totals["spending"]),
            "net": _money(cash_totals["income"] - cash_totals["spending"]),
        },
        "currentWealthfolioWindow": {
            "income": _money(current_income),
            "spending": _money(current_spending),
            "net": _money(Decimal(str(current.get("net") or 0))),
            "uncategorizedCount": current_uncategorized_count,
        },
        "comparison": {
            "knownGapArtifactAdjustment": _money(-known_gap_amount),
            "wealthfolioSpendingExcludingKnownGapArtifact": _money(
                adjusted_current_spending
            ),
            "wealthfolioNetExcludingKnownGapArtifact": _money(
                current_income - adjusted_current_spending
            ),
            "simplefinScopeSpending": _money(cash_totals["spending"]),
            "note": (
                "Current Wealthfolio totals cover the full review window; the "
                "SimpleFIN scope is a subset. The known unpaired balance-gap "
                "transfer is removed only in the adjusted comparison."
            ),
        },
    }
    if auto_ids & {row["activityId"] for row in manual}:
        raise DecisionError("an activity cannot be both automatic and manual")
    document["planFingerprint"] = plan_fingerprint(document)
    return document


def validate_category_plan(plan: dict[str, Any]) -> None:
    expected = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    if plan.get("planFingerprint") != expected:
        raise DecisionError("category plan fingerprint is invalid")
    if plan.get("mode") != "category-plan-only":
        raise DecisionError("category rehearsal requires a category-only plan")
    window = plan.get("reviewWindow") or {}
    start = str(window.get("startDate") or "")
    end = str(window.get("endDate") or "")
    if not start or not end or end < start:
        raise DecisionError("category plan has an invalid review window")
    for section in (
        "autoCandidates",
        "manualItems",
        "transfersAndReconciliation",
        "balanceGapReconciliation",
    ):
        rows = plan.get(section)
        if not isinstance(rows, list):
            raise DecisionError(f"category plan has invalid {section}")
        if any(
            not isinstance(row, dict)
            or not start <= str(row.get("date") or "") <= end
            for row in rows
        ):
            raise DecisionError(f"category plan {section} escapes its review window")
    evidence_rows = plan.get("evidence")
    if not isinstance(evidence_rows, list) or not evidence_rows:
        raise DecisionError("category plan has no sealed evidence")
    for evidence in evidence_rows:
        if not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}:
            raise DecisionError("category plan evidence has invalid shape")
        path = Path(str(evidence.get("path") or ""))
        if not path.is_file() or sha256_file(path) != evidence.get("sha256"):
            raise DecisionError(f"category evidence hash changed: {path.name}")


def write_category_plan(data_dir: Path, plan: dict[str, Any]) -> Path:
    data_dir = validate_data_dir(data_dir)
    folder = data_dir / "normalized" / "simplefin"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(plan["generatedAt"]).strftime(
        "%Y-%m-%d-%H%M%S-%f"
    )
    path = folder / f"category-plan-{stamp}.json"
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


def write_category_review(data_dir: Path, plan: dict[str, Any]) -> Path:
    """Write a private, merchant-redacted Markdown review companion."""
    data_dir = validate_data_dir(data_dir)
    folder = data_dir / "normalized" / "simplefin"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(plan["generatedAt"]).strftime(
        "%Y-%m-%d-%H%M%S-%f"
    )
    path = folder / f"category-review-{stamp}.md"
    metrics = plan["metrics"]
    cash = plan["cashFlowExcludingTransfersAndReconciliation"]
    current = plan["currentWealthfolioWindow"]
    comparison = plan["comparison"]
    lines = [
        "# SimpleFIN category review",
        "",
        f"- Plan fingerprint: `{plan['planFingerprint']}`",
        f"- High-confidence candidates: {metrics['autoCount']} "
        f"({_money(Decimal(metrics['autoAmount']))})",
        f"- Manual/ambiguous: {metrics['manualCount']} "
        f"({_money(Decimal(metrics['manualAmount']))})",
        f"- SimpleFIN transfers: {metrics['transferCount']} "
        f"({_money(Decimal(metrics['transferAmount']))})",
        f"- Balance-gap/reconciliation rows: {metrics['balanceGapCount']} "
        f"({_money(Decimal(metrics['balanceGapAmount']))})",
        f"- Known unavoidable spending artifact: {metrics['knownGapArtifactCount']} "
        f"({_money(Decimal(metrics['knownGapArtifactAmount']))})",
        "",
        "## August cash flow excluding transfers and reconciliation",
        "",
        f"- Income: {cash['income']}",
        f"- Spending: {cash['spending']}",
        f"- Net: {cash['net']}",
        f"- Current Wealthfolio income: {current['income']}",
        f"- Current Wealthfolio spending: {current['spending']}",
        f"- Wealthfolio spending excluding known gap artifact: "
        f"{comparison['wealthfolioSpendingExcludingKnownGapArtifact']}",
        f"- Wealthfolio net excluding known gap artifact: "
        f"{comparison['wealthfolioNetExcludingKnownGapArtifact']}",
        f"- Current Wealthfolio uncategorized count: "
        f"{current['uncategorizedCount']}",
        "",
        "## Predicted category totals",
        "",
        "| Category | Taxonomy | Amount |",
        "|---|---|---:|",
    ]
    lines.extend(
        f"| {row['categoryName']} | {row['taxonomyId']} | {row['amount']} |"
        for row in plan["predictedCategoryTotals"]
    )
    lines.extend([
        "",
        "## High-confidence candidates",
        "",
        "| Activity ID | Merchant evidence | Category | Confidence | History | Amount |",
        "|---|---|---|---:|---:|---:|",
    ])
    lines.extend(
        f"| `{row['activityId']}` | `{row['merchantHash'][:16]}` | "
        f"{row['categoryName']} | {row['confidence']} | "
        f"{row['evidenceCount']} | {row['amount']} |"
        for row in plan["autoCandidates"]
    )
    lines.extend([
        "",
        "## Manual and ambiguous items",
        "",
        "| Activity ID | Merchant evidence | Reason | Amount |",
        "|---|---|---|---:|",
    ])
    lines.extend(
        f"| `{row['activityId']}` | `{row['merchantHash'][:16]}` | "
        f"{row['reason']} | {row['amount']} |"
        for row in plan["manualItems"]
    )
    lines.extend([
        "",
        "## Balance gaps and transfers",
        "",
        "| Activity ID | Classification | Type | Amount | Known artifact |",
        "|---|---|---|---:|---|",
    ])
    lines.extend(
        f"| `{row['activityId']}` | {row['classification']} | "
        f"{row['activityType']} | {row['amount']} | "
        f"{'yes' if row.get('knownSpendingArtifact') else 'no'} |"
        for row in (
            plan["balanceGapReconciliation"]
            + plan["transfersAndReconciliation"]
        )
    )
    lines.extend([
        "",
        "No merchant descriptions are included. All activity IDs, amounts, and "
        "category decisions in this report remain private.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _stage_rows(
    client: Any,
    plan: dict[str, Any],
    stage_account_map: dict[str, str],
    hash_key: bytes,
) -> list[tuple[dict[str, Any], dict[str, Any], tuple[str, str] | None]]:
    rows = list(client.iter_activities())
    by_key = {
        str(row.get("idempotencyKey")): row
        for row in rows
        if row.get("idempotencyKey")
    }
    result = []
    for candidate in plan["autoCandidates"]:
        stage_account = stage_account_map.get(candidate["canonicalAccountId"])
        if not stage_account:
            raise DecisionError("category candidate has no staging account mapping")
        key = f"simplefin:{stage_account}:{candidate['sourceId']}"
        row = by_key.get(key)
        if not row:
            raise DecisionError("category candidate is missing from staging")
        portable = _portable_activity(
            row,
            {
                "canonicalAccountId": candidate["canonicalAccountId"],
                "sourceAccountId": candidate["sourceAccountId"],
                "sourceId": candidate["sourceId"],
            },
            hash_key,
        )
        if portable != candidate["portableActivity"]:
            raise DecisionError("staging activity differs from reviewed category evidence")
        current = _assignment_category(
            client.get(f"/spending/activities/{row['id']}/assignments")
        )
        result.append((candidate, row, current))
    return result


def rehearse_category_plan(
    client: Any,
    plan: dict[str, Any],
    stage_account_map: dict[str, str],
    hash_key: bytes,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Apply a sealed category plan only after the caller validates staging."""
    validate_category_plan(plan)
    generated_at = generated_at or datetime.now(timezone.utc)
    resolved = _stage_rows(client, plan, stage_account_map, hash_key)
    if all(
        current == (candidate["taxonomyId"], candidate["categoryId"])
        for candidate, _row, current in resolved
    ):
        status = "already-applied"
        backup = None
        applied: list[tuple[dict[str, Any], dict[str, Any]]] = []
    else:
        conflicting = [
            current
            for candidate, _row, current in resolved
            if current
            and current != (candidate["taxonomyId"], candidate["categoryId"])
        ]
        if conflicting:
            raise DecisionError("staging contains conflicting category assignments")
        backup = client.backup_database()
        if not backup:
            raise DecisionError("staging backup was not confirmed")
        applied = []
        try:
            for candidate, row, current in resolved:
                if current:
                    continue
                client.put(
                    f"/spending/activities/{row['id']}/assignments",
                    {
                        "taxonomyId": candidate["taxonomyId"],
                        "categoryId": candidate["categoryId"],
                    },
                )
                applied.append((candidate, row))
            verified = _stage_rows(client, plan, stage_account_map, hash_key)
            if any(
                current != (candidate["taxonomyId"], candidate["categoryId"])
                for candidate, _row, current in verified
            ):
                raise DecisionError("staging category verification failed")
            status = "applied"
        except Exception:
            for candidate, row, before in reversed(resolved):
                if before is not None:
                    continue
                current = _assignment_category(
                    client.get(f"/spending/activities/{row['id']}/assignments")
                )
                desired_category = (
                    candidate["taxonomyId"],
                    candidate["categoryId"],
                )
                if current == desired_category:
                    client.delete(
                        f"/spending/activities/{row['id']}/assignments/"
                        f"{candidate['taxonomyId']}"
                    )
                elif current is not None:
                    raise DecisionError(
                        "category rollback found an unexpected staging assignment"
                    )
            rolled_back = _stage_rows(client, plan, stage_account_map, hash_key)
            if any(
                current is not None
                for (_candidate, _row, current), (
                    _before_candidate,
                    _before_row,
                    before,
                ) in zip(rolled_back, resolved, strict=True)
                if before is None
            ):
                raise DecisionError("category rollback did not restore staging")
            raise

    final = _stage_rows(client, plan, stage_account_map, hash_key)
    state = [
        {
            "canonicalAccountId": candidate["canonicalAccountId"],
            "sourceAccountId": candidate["sourceAccountId"],
            "sourceId": candidate["sourceId"],
            "taxonomyId": current[0] if current else None,
            "categoryId": current[1] if current else None,
        }
        for candidate, _row, current in final
    ]
    receipt = {
        "schemaVersion": 1,
        "mode": "category-staging-rehearsal",
        "generatedAt": generated_at.isoformat(),
        "productionMutated": False,
        "status": status,
        "categoryPlanFingerprint": plan["planFingerprint"],
        "categoryPlanSha256": None,
        "backup": backup,
        "appliedCount": len(applied),
        "targetCount": len(resolved),
        "postCategoryFingerprint": plan_fingerprint(
            sorted(
                state,
                key=lambda row: (
                    row["canonicalAccountId"],
                    row["sourceAccountId"],
                    row["sourceId"],
                ),
            )
        ),
    }
    receipt["receiptFingerprint"] = plan_fingerprint(receipt)
    return receipt


def write_rehearsal_receipt(
    data_dir: Path, receipt: dict[str, Any], plan_path: Path
) -> Path:
    data_dir = validate_data_dir(data_dir)
    receipt["categoryPlanSha256"] = sha256_file(plan_path)
    receipt["receiptFingerprint"] = plan_fingerprint({
        key: value for key, value in receipt.items() if key != "receiptFingerprint"
    })
    folder = data_dir / "normalized" / "simplefin"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(receipt["generatedAt"]).strftime(
        "%Y-%m-%d-%H%M%S-%f"
    )
    path = folder / f"category-rehearsal-{stamp}.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path
