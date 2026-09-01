"""Deterministic, private spending categorization plans for Wealthfolio.

The plan/rehearse/promote machinery below started as SimpleFIN-only and is now
source-agnostic: ``build_category_plan`` accepts a resolver that joins any live
activity carrying a stable ``idempotencyKey`` to canonical evidence, and the
staging and production appliers rebuild a portable identity for every supported
source. Passing no resolver keeps the original SimpleFIN behaviour byte for
byte, so plans sealed before this change still rehearse and promote unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import stat
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from importers.categorize.identity import (
    ACCOUNT_SCOPED_SOURCES,
    STRUCTURAL_SOURCES,
    SourceResolution,
    parse_source_identity,
)
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint, validate_private_output
from importers.simplefin.category_rules import (
    RuleContext,
    RuleEngine,
    parse_rule_engine,
    run_categorize as _run_rule_categorize,
    run_decorate as _run_rule_decorate,
    run_normalize_and_classify as _run_rule_normalize_and_classify,
)
from importers.simplefin.pipeline import normalize_description
from importers.simplefin.spending_adapter import (
    INCOME_TAXONOMY,
    SPENDING_TAXONOMY,
    CapabilityStatus,
    SpendingAdapter,
    SpendingCapabilityBlocked,
    UNCATEGORIZED_CATEGORY_IDS,
)

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

#: The only source enumerated when a caller does not ask for more. Keeping this
#: as the default is what makes every previously sealed plan, rehearsal receipt
#: and promotion receipt continue to validate untouched.
DEFAULT_SOURCE_SYSTEMS: tuple[str, ...] = ("simplefin",)

#: Enumerate every activity that carries a stable source identity.
ALL_SOURCE_SYSTEMS = "*"

#: Canonical ``transaction_kind`` values that move money or restate a balance
#: rather than buy something. They are decided *before* any category lookup, so
#: a transfer, card payment or reconciliation can never become a purchase.
STRUCTURAL_CANONICAL_KINDS: dict[str, str] = {
    "internal_transfer": "confirmed-internal-transfer",
    "cc_payment": "card-payment",
    "loan_payment": "loan-payment",
    "saving": "saving-transfer",
    "investment": "investment-transfer",
    "reconciliation": "canonical-reconciliation",
    "excluded": "canonically-excluded",
}

#: Canonical kinds whose direction is a credit back to the payer. A credit-card
#: credit already reports as negative spending; a cash-account credit reports as
#: income and needs an income-side category or an explicit abstention.
CREDIT_CANONICAL_KINDS = frozenset({"refund", "reimbursement"})

#: Evidence kinds produced by Wealthfolio's own categorized live history.
LIVE_HISTORY_EVIDENCE_KINDS = frozenset({
    "live-account-history",
    "live-global-history",
})

#: Evidence kind produced by the local Ollama categorization agent. It is the
#: weakest evidence in the system by construction: it is consulted only after
#: every deterministic source has declined, and only a decision at or above an
#: explicit confidence threshold may enter a sealed plan.
AGENT_EVIDENCE_KIND = "ollama-agent"
AGENT_EVIDENCE_KINDS = frozenset({AGENT_EVIDENCE_KIND})

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


def spending_report_snapshot(
    report: dict[str, Any], uncategorized_count: int
) -> dict[str, Any]:
    """Return the exact, privacy-safe Spending values category plans verify."""
    if not isinstance(report, dict) or not isinstance(report.get("current"), dict):
        raise DecisionError("Wealthfolio Spending report has an invalid current summary")
    if not isinstance(uncategorized_count, int) or uncategorized_count < 0:
        raise DecisionError("Wealthfolio uncategorized count is invalid")
    current = report["current"]
    try:
        income = Decimal(str(current.get("income") or 0))
        spending = Decimal(str(current.get("outflow") or 0))
        raw_net = current.get("net")
        net = Decimal(str(income - spending if raw_net is None else raw_net))
        activity_count = int(current.get("count") or 0)
    except (ValueError, TypeError, ArithmeticError):
        raise DecisionError("Wealthfolio Spending summary contains invalid values") from None
    totals: dict[tuple[str, str], tuple[Decimal, int]] = {}
    for key in ("spendingBreakdown", "incomeBreakdown"):
        rows = report.get(key, [])
        if not isinstance(rows, list):
            raise DecisionError(f"Wealthfolio Spending report has invalid {key}")
        for row in rows:
            if not isinstance(row, dict):
                raise DecisionError(f"Wealthfolio Spending report has invalid {key} row")
            taxonomy_id = str(row.get("taxonomyId") or "")
            category_id = str(row.get("categoryId") or "")
            if not taxonomy_id or (
                not category_id and category_id not in UNCATEGORIZED_CATEGORY_IDS
            ):
                raise DecisionError("Wealthfolio Spending report category identity is incomplete")
            identity = (taxonomy_id, category_id)
            amount, count = totals.get(identity, (Decimal(), 0))
            try:
                totals[identity] = (
                    amount + Decimal(str(row.get("amount") or 0)),
                    count + int(row.get("count") or 0),
                )
            except (ValueError, TypeError, ArithmeticError):
                raise DecisionError("Wealthfolio Spending category total is invalid") from None
    return {
        "income": _money(income),
        "spending": _money(spending),
        "net": _money(net),
        "activityCount": activity_count,
        "uncategorizedCount": uncategorized_count,
        "categoryTotals": [
            {
                "taxonomyId": taxonomy_id,
                "categoryId": category_id,
                "amount": _money(amount),
                "count": count,
            }
            for (taxonomy_id, category_id), (amount, count) in sorted(totals.items())
            if count
        ],
    }


def _expected_spending_snapshot(
    before: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    expected = {
        **before,
        "uncategorizedCount": before["uncategorizedCount"] - len(candidates),
    }
    if expected["uncategorizedCount"] < 0:
        raise DecisionError("category candidates exceed the live uncategorized count")
    totals = {
        (row["taxonomyId"], row["categoryId"]): [
            Decimal(row["amount"]),
            int(row["count"]),
        ]
        for row in before["categoryTotals"]
    }
    for candidate in candidates:
        amount = Decimal(candidate["reportAmount"])
        uncategorized_matches = [
            (candidate["taxonomyId"], category_id)
            for category_id in UNCATEGORIZED_CATEGORY_IDS
            if (candidate["taxonomyId"], category_id) in totals
        ]
        if len(uncategorized_matches) > 1:
            raise DecisionError(
                "Wealthfolio Spending report has ambiguous uncategorized totals"
            )
        if uncategorized_matches:
            uncategorized = uncategorized_matches[0]
            totals[uncategorized][0] -= amount
            totals[uncategorized][1] -= 1
            if totals[uncategorized][1] < 0:
                raise DecisionError(
                    "category candidates exceed uncategorized category totals"
                )
        identity = (candidate["taxonomyId"], candidate["categoryId"])
        current = totals.setdefault(identity, [Decimal(), 0])
        current[0] += amount
        current[1] += 1
    expected["categoryTotals"] = [
        {
            "taxonomyId": taxonomy_id,
            "categoryId": category_id,
            "amount": _money(amount),
            "count": count,
        }
        for (taxonomy_id, category_id), (amount, count) in sorted(totals.items())
        if count
    ]
    return expected


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


@dataclass(frozen=True)
class MerchantEvidence:
    """Provenance for one (scope, merchant, category) observation set."""

    evidence_count: int
    source_systems: tuple[str, ...]
    first_seen: str
    last_seen: str


@dataclass
class HistoryIndex:
    by_account: dict[tuple[str, str], dict[str, set[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )
    global_merchants: dict[str, dict[str, set[tuple[str, str]]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )
    #: ``(scopeAccountId or "", normalizedMerchant, category)`` -> provenance.
    #: An empty scope account is the global (cross-account) observation set.
    provenance: dict[tuple[str, str, str], dict[str, Any]] = field(
        default_factory=dict
    )

    def add(
        self,
        account_id: str,
        description: str,
        category: str,
        source_id: str,
        *,
        source_system: str = "",
        when: str = "",
    ) -> None:
        normalized = normalize_description(description)
        if not normalized or not category:
            return
        if category.casefold() in NON_CONSUMPTION_CATEGORIES:
            return
        self.by_account[(account_id, normalized)][category].add(source_id)
        self.global_merchants[normalized][category].add((account_id, source_id))
        parsed = parse_source_identity(source_id)
        system = str(source_system or "").strip() or (
            parsed.source_system if parsed else ""
        )
        day = str(when or "")[:10]
        # Two observation sets are tracked per row: one scoped to the account
        # and one global. The global set counts (account, source) pairs, exactly
        # like ``global_merchants``, so the same source id seen in two accounts
        # is two independent observations there and one in each account.
        for scope, observation in (
            (account_id, source_id),
            ("", f"{account_id}|{source_id}"),
        ):
            record = self.provenance.setdefault(
                (scope, normalized, category),
                {"sourceIds": set(), "sourceSystems": set(), "first": "", "last": ""},
            )
            record["sourceIds"].add(observation)
            if system:
                record["sourceSystems"].add(system)
            if day:
                record["first"] = min(record["first"] or day, day)
                record["last"] = max(record["last"], day)

    def evidence(
        self, account_id: str, normalized_merchant: str, category: str
    ) -> MerchantEvidence:
        """Provenance for one observation set; account id ``""`` is global."""
        record = self.provenance.get(
            (account_id, normalized_merchant, category),
            {"sourceIds": set(), "sourceSystems": set(), "first": "", "last": ""},
        )
        return MerchantEvidence(
            evidence_count=len(record["sourceIds"]),
            source_systems=tuple(sorted(record["sourceSystems"])),
            first_seen=record["first"],
            last_seen=record["last"],
        )


def load_history(canonical_path: Path, monarch_path: Path | None) -> HistoryIndex:
    """Load canonical categories, using Monarch statement text only as an alias.

    Every canonical source contributes -- Monarch, mapped extracts, SimpleFIN --
    because the merchant consensus a plan relies on should be the union of every
    reviewed decision, not one importer's slice of it.
    """
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
                when=str(row.get("date") or ""),
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
                        when=str(canonical.get("date") or ""),
                    )
    return index


def load_category_decisions(path: Path | None) -> dict[str, Any]:
    """Load a private category-decisions.json, schemaVersion 1 or 2.

    SchemaVersion 1 keeps its exact historical shape and validation so
    existing private decision files continue to produce equivalent plans.
    SchemaVersion 2 compiles a staged rule engine (see category_rules.py)
    under the ``ruleEngine`` key; ``merchantOverrides``/``activityOverrides``
    are superseded by rules and always empty for v2. ``categoryAliases`` is
    generic vocabulary translation shared by both schema versions.
    """
    if path is None or not path.exists():
        return {
            "schemaVersion": 1,
            "categoryAliases": {},
            "merchantOverrides": [],
            "activityOverrides": [],
            "ruleEngine": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionError(f"cannot read private category decisions: {exc}") from None
    schema_version = payload.get("schemaVersion")
    if schema_version == 1:
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
            "ruleEngine": None,
        }
    if schema_version == 2:
        if not isinstance(payload.get("categoryAliases", {}), dict):
            raise DecisionError("category-decisions.json categoryAliases has invalid shape")
        return {
            "schemaVersion": 2,
            "categoryAliases": payload.get("categoryAliases", {}),
            "merchantOverrides": [],
            "activityOverrides": [],
            "ruleEngine": parse_rule_engine(payload),
        }
    raise DecisionError("category-decisions.json must have schemaVersion 1 or 2")


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


def normalize_source_systems(
    source_systems: Sequence[str] | None,
) -> tuple[str, ...]:
    """Resolve the requested source scope to a stable, sorted tuple."""
    if source_systems is None:
        return DEFAULT_SOURCE_SYSTEMS
    values = tuple(
        sorted({str(system or "").strip().casefold() for system in source_systems})
    )
    if not values:
        raise DecisionError("a category plan needs at least one source system")
    if ALL_SOURCE_SYSTEMS in values:
        return (ALL_SOURCE_SYSTEMS,)
    if any(system in STRUCTURAL_SOURCES for system in values):
        raise DecisionError(
            "reconciliation sources are reported separately and cannot be categorized"
        )
    return values


def in_source_scope(
    activity: dict[str, Any], source_systems: Sequence[str] | None
) -> bool:
    """Is this activity inside the requested source scope?

    ``gap:`` activities are always excluded here: they are balance
    reconciliation and are enumerated by their own section of the plan, never
    as something that could carry a spending category.
    """
    key = str(activity.get("idempotencyKey") or "")
    if not key:
        return False
    systems = normalize_source_systems(source_systems)
    identity = parse_source_identity(key)
    if identity is None or identity.source_system in STRUCTURAL_SOURCES:
        return False
    if systems == (ALL_SOURCE_SYSTEMS,):
        return True
    return identity.source_system in systems


def _reviewed_plan_resolver(
    identities: dict[tuple[str, str], dict[str, str]]
) -> Callable[[dict[str, Any]], SourceResolution]:
    """The original SimpleFIN resolver: a reviewed source plan and nothing else."""

    def resolve(activity: dict[str, Any]) -> SourceResolution:
        source_key = _source_id(activity)
        identity = identities.get(
            (str(activity.get("accountId") or ""), source_key)
        )
        return SourceResolution(
            source_system="simplefin",
            source_key=source_key,
            identity=identity,
            match_kind="reviewed-source-plan" if identity else "",
            reason="" if identity else "missing-reviewed-source-identity",
        )

    return resolve


def _plan_source_key(activity: dict[str, Any], source_systems: tuple[str, ...]) -> str:
    """The per-transaction key recorded in a plan's pre-category state.

    The legacy SimpleFIN-only scope keeps ``_source_id`` exactly, so a plan
    sealed before this module became source-agnostic still fingerprints
    identically. A wider scope uses the portable parse instead.
    """
    if source_systems == DEFAULT_SOURCE_SYSTEMS:
        return _source_id(activity)
    identity = parse_source_identity(activity.get("idempotencyKey"))
    return identity.source_key if identity else ""


def staging_idempotency_key(candidate: dict[str, Any], stage_account_id: str) -> str:
    """Rebuild a candidate's idempotency key against a staging account.

    Candidates sealed before this module became source-agnostic carry no
    ``sourceSystem``, and SimpleFIN is the only source they could have come
    from, so that is the default.
    """
    system = str(candidate.get("sourceSystem") or "simplefin")
    source_key = str(candidate.get("sourceId") or "")
    if system in ACCOUNT_SCOPED_SOURCES:
        return f"{system}:{stage_account_id}:{source_key}"
    return f"{system}:{source_key}"


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


def _resolve_rule_category(
    payload: dict[str, str],
    catalogs: dict[str, list[dict[str, Any]]],
    bucket: str,
) -> dict[str, str]:
    """Validate a v2 categorize-stage setCategory action against catalogs.

    The rule's own structural fields (non-empty rationale, valid taxonomy
    vocabulary, direction-vs-taxonomy consistency) are already fail-closed
    validated at parse time by category_rules.parse_rule_engine; this only
    resolves the category against the live catalog and the activity's
    actual cash-flow bucket, matching _validate_override_category exactly.
    """
    taxonomy_id = payload["taxonomyId"]
    category_id = payload["categoryId"]
    category = _category_by_id(catalogs).get((taxonomy_id, category_id))
    if not category:
        raise DecisionError("private category rule references an unknown category")
    expected_taxonomy = INCOME_TAXONOMY if bucket == "income" else SPENDING_TAXONOMY
    if taxonomy_id != expected_taxonomy:
        raise DecisionError("private category rule conflicts with cash-flow direction")
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
    rule_context: "RuleContext | None" = None,
) -> tuple[dict[str, str] | None, str] | None:
    """Resolve a v1 override or, for v2 decisions, a categorize-stage rule.

    Returns ``None`` when nothing matches. Returns ``(None, "unreviewed-rule:<id>")``
    when a categorize rule matched but is not yet ``reviewed``; callers must
    treat that as manual, never automatic. Otherwise returns
    ``(category, sourceKind)`` exactly like the v1 override path, with
    ``sourceKind`` set to ``"rule:<id>"`` for a matched, reviewed rule.
    """
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
    if matches:
        kind = "activity-override" if activity_matches else "merchant-override"
        return _validate_override_category(matches[0], catalogs, bucket), kind

    engine: RuleEngine | None = decisions.get("ruleEngine")
    if engine is None or rule_context is None:
        return None
    payload, rule_id, reviewed, _trace = _run_rule_categorize(engine, rule_context)
    if payload is None:
        return None
    if not reviewed:
        return None, f"unreviewed-rule:{rule_id}"
    return _resolve_rule_category(payload, catalogs, bucket), f"rule:{rule_id}"


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


# Public, source-agnostic names for helpers the categorization package needs.
# They are aliases rather than renames so every existing caller and test that
# imports the underscored name keeps working unchanged.
activity_metadata = _metadata
assignment_category = _assignment_category
cash_amount = _cash_amount
cash_bucket = _cash_bucket
history_choice = _history_choice
is_external_flow = _is_external_flow
money = _money
portable_activity = _portable_activity
resolve_category = _resolve_category


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
    canonical_review: dict[str, Any] | None = None,
    resolver: Callable[[dict[str, Any]], SourceResolution] | None = None,
    source_systems: Sequence[str] | None = None,
    live_history: Any | None = None,
    agent_suggestions: Any | None = None,
    allow_merchant_identity: bool | None = None,
) -> dict[str, Any]:
    """Build a sealed category-only plan without exposing merchant text.

    ``resolver`` joins one live activity to canonical evidence and returns the
    portable identity the plan, the staging rehearsal and the production
    promotion all key on. Passing ``None`` keeps the original behaviour: only
    ``simplefin:`` activities are enumerated and only a reviewed SimpleFIN
    source plan can supply an identity.

    ``source_systems`` widens the enumeration to other importers -- pass
    ``["*"]`` for every activity that carries a stable source identity. The
    resolved scope is sealed into the plan so the production applier scopes
    itself the same way months later.

    ``live_history`` is a read-only index of Wealthfolio's *own* existing
    category assignments (see ``importers.categorize.live_history``). It is
    consulted only for activities that are still uncategorized, only after
    private rules and reviewed canonical carryover have declined, and only
    where its consensus is unanimous at the scope it is read from. The scope
    and the exact evidence it relied on are sealed into the plan so a later
    staging rehearsal or production promotion detects drift.

    ``allow_merchant_identity`` permits merchant-history matching for an
    activity whose *canonical transaction* could not be resolved, using the
    weaker identity the resolver derives from the live key and account bridge.
    It defaults to on exactly when ``live_history`` is supplied, because that
    is the case it exists to serve; reviewed canonical carryover still requires
    a real canonical row and is unaffected either way.

    ``agent_suggestions`` is a completed local-model pass (see
    ``importers.categorize.agent``). It is the *last* source consulted, after
    live history has also declined, and only a suggestion at or above the
    pass's explicit confidence threshold is offered at all. Its direction and
    category id are re-checked here against the live catalog, so a model can
    never widen the taxonomy or flip a debit into income, and the exact model
    digest, prompt/schema fingerprint and per-cluster evidence hash are sealed
    into the plan alongside the decision.
    """
    if report_end < report_start:
        raise DecisionError("category report end date precedes its start date")
    systems = normalize_source_systems(source_systems)
    generated_at = generated_at or datetime.now(timezone.utc)
    known_artifact_activity_ids = known_artifact_activity_ids or set()
    canonical_review = canonical_review or {}
    raw_transfer_review = canonical_review.get("transferReview") or {}
    raw_split_review = canonical_review.get("splitReview") or {}
    if not isinstance(raw_transfer_review, dict) or not isinstance(raw_split_review, dict):
        raise DecisionError("canonical transfer/split review summary has invalid shape")
    if any(
        not isinstance(raw_transfer_review.get(key, []), list)
        for key in ("proposals", "confirmed", "rejected", "staleDecisions")
    ) or not isinstance(raw_split_review.get("groups", []), list):
        raise DecisionError("canonical transfer/split review summary has invalid rows")

    def transfer_in_window(row: dict[str, Any]) -> bool:
        dates = [
            str((row.get(leg) or {}).get("date") or "")
            for leg in ("outflow", "inflow")
            if isinstance(row.get(leg), dict)
        ]
        return not dates or any(report_start <= value <= report_end for value in dates)

    def safe_transfer(row: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: row[key]
            for key in (
                "candidateId",
                "evidenceHash",
                "currency",
                "dayDistance",
                "ambiguous",
                "status",
                "priorDecisionId",
                "decisionId",
                "transferGroup",
            )
            if key in row
        }
        for leg in ("outflow", "inflow"):
            if isinstance(row.get(leg), dict):
                result[leg] = {
                    key: row[leg][key]
                    for key in ("accountId", "sourceId", "date", "amount")
                    if key in row[leg]
                }
        return result

    def safe_split(row: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: row[key]
            for key in (
                "groupId",
                "decisionId",
                "accountId",
                "parentSourceId",
                "date",
                "parentAmount",
                "childTotal",
            )
            if key in row
        }
        result["children"] = [
            {
                key: child[key]
                for key in ("sourceId", "amount", "categoryId")
                if key in child
            }
            for child in row.get("children", [])
            if isinstance(child, dict)
        ]
        return result

    transfer_review = {
        "windowDays": raw_transfer_review.get("windowDays"),
        "proposals": [
            safe_transfer(row) for row in raw_transfer_review.get("proposals", [])
            if isinstance(row, dict) and transfer_in_window(row)
        ],
        "confirmed": [
            safe_transfer(row) for row in raw_transfer_review.get("confirmed", [])
            if isinstance(row, dict) and transfer_in_window(row)
        ],
        "rejected": [
            safe_transfer(row) for row in raw_transfer_review.get("rejected", [])
            if isinstance(row, dict) and transfer_in_window(row)
        ],
        "staleDecisions": [
            safe_transfer(row) for row in raw_transfer_review.get("staleDecisions", [])
            if isinstance(row, dict)
        ],
    }
    proposed_transfer_source_ids = {
        str(leg.get("sourceId") or "")
        for proposal in transfer_review["proposals"]
        for leg_name in ("outflow", "inflow")
        for leg in [proposal.get(leg_name)]
        if isinstance(leg, dict) and leg.get("sourceId")
    }
    split_review = {
        "groups": [
            safe_split(row) for row in raw_split_review.get("groups", [])
            if isinstance(row, dict)
            and report_start <= str(row.get("date") or "") <= report_end
        ]
    }
    by_account = {str(row["id"]): row for row in accounts}
    identities = _reviewed_identities(reviewed)
    resolve_source = resolver or _reviewed_plan_resolver(identities)
    scoped = [
        row
        for row in activities
        if in_source_scope(row, systems)
        and report_start <= str(row.get("date") or "")[:10] <= report_end
    ]
    scoped_source_counts = Counter(
        (
            parsed.source_system
            if (parsed := parse_source_identity(row.get("idempotencyKey")))
            else ""
        )
        for row in scoped
    )
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
    if allow_merchant_identity is None:
        allow_merchant_identity = live_history is not None
    auto: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    transfers: list[dict[str, Any]] = []
    category_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    uncategorized_totals: dict[str, Decimal] = defaultdict(Decimal)
    cash_totals: dict[str, Decimal] = defaultdict(Decimal)
    eligible_amount = Decimal()
    auto_amount = Decimal()
    pre_state: list[dict[str, Any]] = []
    category_by_id = _category_by_id(catalogs)
    used_live_evidence: dict[str, Any] = {}
    used_agent_evidence: dict[str, Any] = {}
    live_abstentions: Counter[str] = Counter()
    agent_abstentions: Counter[str] = Counter()

    def live_rescue(
        account_id: str, merchant_digest: str, bucket_value: str
    ) -> tuple[Any, dict[str, str]] | None:
        """Resolve one still-uncategorized activity from Wealthfolio's history.

        This runs *after* private rules and reviewed canonical carryover have
        declined and *before* the activity is written off as manual, so it can
        only ever add coverage, never overrule a stronger decision. A conflict
        or a below-threshold consensus returns ``None`` and is counted, so the
        review report shows why coverage stopped where it did.
        """
        if live_history is None or not merchant_digest:
            return None
        taxonomy_id = INCOME_TAXONOMY if bucket_value == "income" else SPENDING_TAXONOMY
        lookup = live_history.lookup(account_id, merchant_digest, taxonomy_id)
        if not lookup.matched:
            live_abstentions[lookup.reason or "no-live-history"] += 1
            return None
        consensus = lookup.consensus
        category = category_by_id.get(
            (consensus.taxonomy_id, consensus.category_id)
        )
        if not category:
            live_abstentions["live-history-unknown-category"] += 1
            return None
        used_live_evidence.setdefault(consensus.evidence_hash, consensus)
        return consensus, {
            "taxonomyId": consensus.taxonomy_id,
            "categoryId": consensus.category_id,
            "categoryName": str(category["name"]),
        }

    def agent_rescue(
        activity_id: str, bucket_value: str
    ) -> tuple[Any, dict[str, str]] | None:
        """Resolve from a completed local-model pass, or decline and say why.

        The suggestion set has already applied its own confidence threshold.
        Everything re-checked here is a *structural* property the model is not
        permitted to decide: the taxonomy must match the direction this activity
        actually has, and the category must exist in the live catalog right now.
        """
        if agent_suggestions is None:
            return None
        suggestion = agent_suggestions.suggestion_for(activity_id)
        if suggestion is None:
            agent_abstentions["no-agent-suggestion"] += 1
            return None
        taxonomy_id = INCOME_TAXONOMY if bucket_value == "income" else SPENDING_TAXONOMY
        if suggestion.taxonomy_id != taxonomy_id:
            agent_abstentions["agent-direction-mismatch"] += 1
            return None
        category = category_by_id.get(
            (suggestion.taxonomy_id, suggestion.category_id)
        )
        if not category:
            agent_abstentions["agent-unknown-category"] += 1
            return None
        used_agent_evidence.setdefault(suggestion.evidence_hash, suggestion)
        return suggestion, {
            "taxonomyId": suggestion.taxonomy_id,
            "categoryId": suggestion.category_id,
            "categoryName": str(category["name"]),
        }

    def rescue_uncategorized(
        account_id: str,
        merchant_digest: str,
        bucket_value: str,
        activity_id: str,
    ) -> tuple[Any, dict[str, str]] | None:
        """The full last-resort chain, in strict precedence order.

        Wealthfolio's own reviewed history always outranks a model opinion, so
        the model is asked only where history has nothing to say. Both sources
        expose the same ``evidence_kind``/``confidence``/``evidence_count``
        surface and their own ``as_candidate_fields``, which is what lets one
        call site seal two entirely different kinds of provenance.
        """
        return live_rescue(account_id, merchant_digest, bucket_value) or agent_rescue(
            activity_id, bucket_value
        )

    for activity in scoped:
        activity_id = str(activity.get("id") or "")
        account_id = str(activity.get("accountId") or "")
        resolution = resolve_source(activity)
        source_id = resolution.source_key or _plan_source_key(activity, systems)
        activity_type = str(activity.get("activityType") or "")
        amount = abs(Decimal(str(activity.get("amount") or 0)))
        description = str(
            activity.get("comment")
            or activity.get("notes")
            or activity.get("description")
            or ""
        )
        current_category = _assignment_category(assignments.get(activity_id, []))
        if activity_id in assignments:
            pre_state.append({
                "activityId": activity_id,
                "accountId": account_id,
                "sourceId": _plan_source_key(activity, systems),
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
        # Structural canonical kinds are decided before any category lookup, so
        # a confirmed internal transfer, a card payment, a saving or investment
        # movement, or a reconciliation row can never be read as a purchase.
        structural = STRUCTURAL_CANONICAL_KINDS.get(resolution.transaction_kind)
        if structural:
            transfers.append({
                "activityId": activity_id,
                "sourceId": source_id,
                "date": str(activity.get("date") or "")[:10],
                "amount": _money(amount),
                "activityType": activity_type,
                "classification": structural,
                "categoryAction": "none",
                "sourceSystem": resolution.source_system,
                "canonicalTransactionKind": resolution.transaction_kind,
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
        identity = resolution.identity
        merchant_identity = (
            resolution.merchant_identity if allow_merchant_identity else None
        )
        identity_source = "canonical"
        rescue_evidence: Any | None = None
        base = {
            "activityId": activity_id,
            "sourceId": source_id,
            "date": activity_date,
            "amount": _money(amount),
            "merchantHash": merchant_hash(description, merchant_hash_key),
        }
        if resolution.source_system and systems != DEFAULT_SOURCE_SYSTEMS:
            base["sourceSystem"] = resolution.source_system
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
        if source_id in proposed_transfer_source_ids:
            manual.append({**base, "reason": "transfer-candidate-review"})
            continue
        if not identity:
            # A missing canonical row is not a reason to abandon the activity
            # when merchant history can still say what it is. The weaker
            # identity names where the decision is written and verified; it can
            # never carry a canonical category, because there is no canonical
            # row to carry one from.
            if not merchant_identity:
                manual.append({
                    **base,
                    "reason": resolution.reason or "missing-reviewed-source-identity",
                })
                continue
            identity = merchant_identity
            identity_source = "merchant-history"
        portable = _portable_activity(activity, identity, merchant_hash_key)
        normalized_description = normalize_description(description)
        if not normalized_description:
            manual.append({**base, **identity, "reason": "missing-description"})
            continue

        # Schema v2 staged rule engine: normalize/classify/decorate run as a
        # single non-recursive forward pass before category resolution.
        # Decorate always runs, even when classify excludes the transaction,
        # since tag/event annotations are orthogonal to categorization.
        engine: RuleEngine | None = decisions.get("ruleEngine")
        rule_context: RuleContext | None = None
        rule_trace: dict[str, Any] | None = None
        if engine is not None:
            cash_bucket_value = bucket or "unclassified"
            direction = (
                "credit" if bucket == "income"
                else "debit" if bucket == "spending"
                else None
            )
            rule_context = RuleContext(
                payee=normalized_description,
                payee_hash=base["merchantHash"],
                account_id=identity["canonicalAccountId"],
                activity_identity={
                    "sourceAccountId": identity["sourceAccountId"],
                    "sourceId": identity["sourceId"],
                },
                amount=amount,
                direction=direction,
                cash_bucket=cash_bucket_value,
                date=activity_date,
                pending=bool(activity.get("pending")),
            )
            stage_trace = _run_rule_normalize_and_classify(engine, rule_context)
            decorate_trace = _run_rule_decorate(engine, rule_context)
            rule_trace = {
                **stage_trace,
                "decorate": decorate_trace,
                "tags": list(rule_context.tags),
                "event": rule_context.event,
            }
            if rule_context.excluded:
                manual.append({
                    **base,
                    **identity,
                    "reason": "rule-excluded",
                    "ruleId": rule_context.excluded_rule_id,
                    "ruleTrace": rule_trace,
                })
                continue

        base_traced = {**base, "ruleTrace": rule_trace} if rule_trace is not None else base

        override = _find_override(
            identity,
            base["merchantHash"],
            decisions,
            catalogs,
            bucket or "spending",
            rule_context,
        )
        if override and override[0] is not None:
            category, source = override
            confidence = "1.00"
            evidence_count = 1
            historical_category = None
        elif override:
            _, unreviewed_source = override
            rule_id = (
                unreviewed_source.split(":", 1)[1]
                if ":" in unreviewed_source
                else unreviewed_source
            )
            manual.append({
                **base_traced,
                **identity,
                "reason": "unreviewed-rule-match",
                "ruleId": rule_id,
            })
            continue
        elif resolution.canonical_category:
            # This exact transaction already carries a reviewed canonical
            # category. That is stronger evidence than any merchant consensus,
            # so it is applied first -- this is what carries a mortgage,
            # housing or utility decision from a legacy Monarch or mapped
            # extract row onto the live activity built from the same row.
            historical_category = resolution.canonical_category
            source = f"canonical-category:{resolution.match_kind}"
            evidence_count = 1
            confidence = "0.99"
            category = _resolve_category(
                historical_category,
                bucket or "spending",
                catalogs,
                decisions["categoryAliases"],
            )
            if not category:
                rescue = rescue_uncategorized(
                    account_id, base["merchantHash"], bucket or "spending", activity_id
                )
                if rescue is None:
                    manual.append({
                        **base_traced,
                        **identity,
                        "reason": (
                            "credit-direction-review"
                            if resolution.transaction_kind in CREDIT_CANONICAL_KINDS
                            and bucket == "income"
                            else "unmapped-category"
                        ),
                        "historicalCategory": historical_category,
                        "canonicalTransactionKind": resolution.transaction_kind,
                        "evidenceCount": evidence_count,
                    })
                    continue
                rescue_evidence, category = rescue
                source = rescue_evidence.evidence_kind
                confidence = rescue_evidence.confidence
                evidence_count = rescue_evidence.evidence_count
                historical_category = None
        else:
            choice = _history_choice(
                history,
                identity["canonicalAccountId"],
                normalize_description(description),
            )
            if not choice:
                rescue = rescue_uncategorized(
                    account_id, base["merchantHash"], bucket or "spending", activity_id
                )
                if rescue is None:
                    manual.append({**base_traced, **identity, "reason": "no-history"})
                    continue
                rescue_evidence, category = rescue
                source = rescue_evidence.evidence_kind
                confidence = rescue_evidence.confidence
                evidence_count = rescue_evidence.evidence_count
                historical_category = None
            else:
                source, categories = choice
                if source == "conflicting-history":
                    # Reviewed canonical evidence that disagrees with itself is
                    # exactly where a confident guess does damage, so live
                    # history is not consulted to break the tie.
                    manual.append({
                        **base_traced,
                        **identity,
                        "reason": source,
                        "candidateCategoryCount": len(categories),
                        "evidenceHash": plan_fingerprint({
                            key: sorted(value)
                            for key, value in sorted(categories.items())
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
                confidence = "0.98" if source == "account-history" else "0.95"
                insufficient = (
                    bool(category) and evidence_count < MIN_HISTORY_COUNT
                )
                if not category or insufficient:
                    rescue = rescue_uncategorized(
                        account_id,
                        base["merchantHash"],
                        bucket or "spending",
                        activity_id,
                    )
                    if rescue is None:
                        if insufficient:
                            manual.append({
                                **base_traced,
                                **identity,
                                "reason": "insufficient-history",
                                "historicalCategory": historical_category,
                                "evidenceCount": evidence_count,
                                "candidate": category,
                                "confidence": confidence,
                            })
                            continue
                        manual.append({
                            **base_traced,
                            **identity,
                            "reason": (
                                "credit-direction-review"
                                if resolution.transaction_kind in CREDIT_CANONICAL_KINDS
                                and bucket == "income"
                                else "unmapped-category"
                            ),
                            "historicalCategory": historical_category,
                            "evidenceCount": evidence_count,
                        })
                        continue
                    rescue_evidence, category = rescue
                    source = rescue_evidence.evidence_kind
                    confidence = rescue_evidence.confidence
                    evidence_count = rescue_evidence.evidence_count
                    historical_category = None

        candidate = {
            **base_traced,
            **identity,
            "portableActivity": portable,
            "taxonomyId": category["taxonomyId"],
            "categoryId": category["categoryId"],
            "categoryName": category["categoryName"],
            "reportAmount": _money(
                _cash_amount(account_type, activity_type, amount)
            ),
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
        if systems != DEFAULT_SOURCE_SYSTEMS:
            candidate["sourceSystem"] = resolution.source_system
            candidate["matchKind"] = resolution.match_kind or (
                "merchant-history-identity"
                if identity_source == "merchant-history"
                else ""
            )
            if resolution.canonical_source_id:
                candidate["canonicalSourceId"] = resolution.canonical_source_id
            if resolution.transaction_kind:
                candidate["canonicalTransactionKind"] = resolution.transaction_kind
        if identity_source != "canonical":
            candidate["identitySource"] = identity_source
        if rescue_evidence is not None:
            # Each rescue source seals its own provenance: live history names
            # the assignments it read, the local model names its digest, prompt
            # fingerprint and evidence hash. Neither can masquerade as the other.
            candidate.update(rescue_evidence.as_candidate_fields())
        auto.append(candidate)
        auto_amount += amount
        on = base["date"]
        if report_start <= on <= report_end:
            category_totals[
                (category["taxonomyId"], category["categoryId"], category["categoryName"])
            ] += _cash_amount(account_type, activity_type, amount)

    auto_ids = {row["activityId"] for row in auto}
    uncategorized_reasons = {
        "ambiguous-canonical-claim",
        "ambiguous-fallback-match",
        "ambiguous-source-identity",
        "conflicting-history",
        "credit-direction-review",
        "insufficient-history",
        "missing-description",
        "missing-reviewed-source-identity",
        "no-history",
        "unknown-source-identity",
        "unmapped-account",
        "unmapped-category",
        "unresolved-source-identity",
        "transfer-candidate-review",
        "rule-excluded",
        "unreviewed-rule-match",
    }
    for row in manual:
        if row["reason"] not in uncategorized_reasons:
            continue
        if report_start <= row["date"] <= report_end:
            activity = next(
                item for item in scoped if str(item.get("id")) == row["activityId"]
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
    pre_spending = spending_report_snapshot(
        current_report or {"current": {}},
        int(current_uncategorized_count or 0),
    )
    expected_spending = _expected_spending_snapshot(pre_spending, auto)
    evidence_kind_counts = Counter(
        str(row.get("evidenceKind") or "") for row in auto
    )
    manual_reason_counts = Counter(str(row.get("reason") or "") for row in manual)
    live_seal = (
        live_history.seal(used_live_evidence.values())
        if live_history is not None
        else None
    )
    agent_seal = (
        agent_suggestions.seal(used_agent_evidence.values())
        if agent_suggestions is not None
        else None
    )
    document = {
        "schemaVersion": 1,
        "mode": "category-plan-only",
        "generatedAt": generated_at.isoformat(),
        "productionMutated": False,
        "environmentFingerprint": environment_fingerprint,
        "evidence": evidence,
        "reviewWindow": {"startDate": report_start, "endDate": report_end},
        "sourceSystems": list(systems),
        "preCategoryFingerprint": plan_fingerprint(
            sorted(pre_state, key=lambda row: row["activityId"])
        ),
        "preCategoryActivityIds": sorted(
            row["activityId"] for row in pre_state
        ),
        "preWealthfolioWindow": pre_spending,
        "expectedWealthfolioWindow": expected_spending,
        "autoCandidates": sorted(auto, key=lambda row: row["activityId"]),
        "manualItems": sorted(manual, key=lambda row: row["activityId"]),
        "transfersAndReconciliation": sorted(
            transfers, key=lambda row: row["activityId"]
        ),
        "balanceGapReconciliation": sorted(
            reconciliation, key=lambda row: row["activityId"]
        ),
        "transferReview": transfer_review,
        "splitReview": split_review,
        "metrics": {
            # Retained under its original name so existing readers keep working;
            # ``scopedActivities`` is the accurate name once the scope is wider
            # than SimpleFIN.
            "simplefinActivities": len(scoped),
            "scopedActivities": len(scoped),
            "sourceSystemCounts": dict(sorted(scoped_source_counts.items())),
            "eligibleNonTransfers": len(scoped) - len(transfers),
            "eligibleAmount": _money(eligible_amount),
            "autoCount": len(auto),
            "autoAmount": _money(auto_amount),
            "manualCount": len(manual),
            "manualAmount": _money(eligible_amount - auto_amount),
            "canonicalCarryoverCount": sum(
                str(row.get("evidenceKind") or "").startswith("canonical-category")
                for row in auto
            ),
            # Coverage, by exactly which kind of evidence produced it. Every
            # value is a count or a hash: no merchant text, no amount tied to a
            # name, no account number.
            "evidenceKindCounts": dict(sorted(evidence_kind_counts.items())),
            "manualReasonCounts": dict(sorted(manual_reason_counts.items())),
            "liveHistoryCount": sum(
                str(row.get("evidenceKind") or "") in LIVE_HISTORY_EVIDENCE_KINDS
                for row in auto
            ),
            "liveHistoryAccountCount": evidence_kind_counts.get(
                "live-account-history", 0
            ),
            "liveHistoryGlobalCount": evidence_kind_counts.get(
                "live-global-history", 0
            ),
            "liveHistoryAbstentionCounts": dict(sorted(live_abstentions.items())),
            "agentCount": sum(
                str(row.get("evidenceKind") or "") in AGENT_EVIDENCE_KINDS
                for row in auto
            ),
            "agentClusterCount": len(used_agent_evidence),
            "agentAbstentionCounts": dict(sorted(agent_abstentions.items())),
            "merchantIdentityCount": sum(
                row.get("identitySource") == "merchant-history" for row in auto
            ),
            "structuralKindCount": sum(
                bool(row.get("canonicalTransactionKind")) for row in transfers
            ),
            "unresolvedIdentityCount": sum(
                row["reason"]
                in {
                    "ambiguous-canonical-claim",
                    "ambiguous-fallback-match",
                    "ambiguous-source-identity",
                    "unknown-source-identity",
                    "unmapped-account",
                    "unresolved-source-identity",
                    "missing-reviewed-source-identity",
                }
                for row in manual
            ),
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
            "ruleExcludedCount": sum(
                row["reason"] == "rule-excluded" for row in manual
            ),
            "unreviewedRuleMatchCount": sum(
                row["reason"] == "unreviewed-rule-match" for row in manual
            ),
            "transferCandidateCount": len(transfer_review["proposals"]),
            "confirmedTransferPairCount": len(transfer_review["confirmed"]),
            "rejectedTransferPairCount": len(transfer_review["rejected"]),
            "splitGroupCount": len(split_review["groups"]),
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
    if live_seal is not None:
        # Sealed *inside* the plan, so it is covered by the plan fingerprint the
        # operator quotes, the staging rehearsal re-validates and the production
        # promotion re-checks against a fresh read of the same assignments.
        document["liveHistory"] = live_seal
    if agent_seal is not None:
        document["ollamaAgent"] = agent_seal
    if auto_ids & {row["activityId"] for row in manual}:
        raise DecisionError("an activity cannot be both automatic and manual")
    _validate_live_history_seal(document)
    _validate_agent_seal(document)
    document["planFingerprint"] = plan_fingerprint(document)
    return document


def _validate_live_history_seal(plan: dict[str, Any]) -> None:
    """Require a plan's live-history seal to explain its own candidates.

    A plan that never consulted Wealthfolio's own history carries no seal, and
    that stays valid forever -- every plan written before this existed is in
    that shape. When a seal *is* present it must be internally complete: its
    fingerprint must cover its content, and every candidate that claims a
    live-history decision must name evidence the seal actually contains. That
    is what makes the sealed evidence re-checkable months later instead of a
    decorative summary.
    """
    seal = plan.get("liveHistory")
    live_candidates = [
        candidate
        for candidate in plan.get("autoCandidates", [])
        if isinstance(candidate, dict)
        and str(candidate.get("evidenceKind") or "") in LIVE_HISTORY_EVIDENCE_KINDS
    ]
    if seal is None:
        if live_candidates:
            raise DecisionError(
                "category plan uses live category history without sealing it"
            )
        return
    if not isinstance(seal, dict):
        raise DecisionError("category plan live history seal has invalid shape")
    scope = seal.get("scope")
    metrics = seal.get("metrics")
    rows = seal.get("evidence")
    if (
        seal.get("schemaVersion") != 1
        or not isinstance(scope, dict)
        or not isinstance(metrics, dict)
        or not isinstance(rows, list)
    ):
        raise DecisionError("category plan live history seal has invalid shape")
    start = str(scope.get("startDate") or "")
    end = str(scope.get("endDate") or "")
    if not start or not end or end < start:
        raise DecisionError("category plan live history scope has an invalid window")
    if not isinstance(scope.get("accountIds"), list) or not isinstance(
        scope.get("sourceSystems"), list
    ):
        raise DecisionError("category plan live history scope has invalid shape")
    sealed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DecisionError("category plan live history evidence has invalid shape")
        required = {
            "evidenceHash",
            "scope",
            "accountId",
            "merchantHash",
            "taxonomyId",
            "categoryId",
            "evidenceCount",
            "sampleActivityIds",
        }
        if not required <= set(row):
            raise DecisionError("category plan live history evidence is incomplete")
        if not isinstance(row.get("sampleActivityIds"), list) or not row.get(
            "sampleActivityIds"
        ):
            raise DecisionError(
                "category plan live history evidence names no contributing activities"
            )
        if sealed.setdefault(str(row["evidenceHash"]), row) is not row:
            raise DecisionError("category plan live history evidence is duplicated")
    expected = plan_fingerprint({
        key: value for key, value in seal.items() if key != "indexFingerprint"
    })
    if seal.get("indexFingerprint") != expected:
        raise DecisionError("category plan live history fingerprint is invalid")
    for candidate in live_candidates:
        row = sealed.get(str(candidate.get("liveHistoryEvidenceHash") or ""))
        if row is None:
            raise DecisionError(
                "category candidate cites unsealed live category history"
            )
        if (
            row["taxonomyId"] != candidate.get("taxonomyId")
            or row["categoryId"] != candidate.get("categoryId")
            or row["merchantHash"] != candidate.get("merchantHash")
            or row["evidenceCount"] != candidate.get("evidenceCount")
            or row["scope"] != candidate.get("liveHistoryScope")
        ):
            raise DecisionError(
                "category candidate disagrees with its sealed live category history"
            )


def _validate_agent_seal(plan: dict[str, Any]) -> None:
    """Require a plan's local-model seal to justify its own candidates.

    A plan that never consulted a model carries no seal and stays valid forever.
    When a seal *is* present the bar is deliberately higher than for any other
    evidence source, because this is the only one that is not a deterministic
    consequence of private data:

    * the endpoint must be recorded as loopback with web research off, so a plan
      produced against a remote or web-augmented model cannot be promoted;
    * the exact model digest and prompt/schema fingerprint must be named, so the
      decision is reproducible against the same weights and the same question;
    * every candidate must cite a sealed suggestion that agrees with it field by
      field, and that suggestion's confidence must meet the threshold the pass
      declared. A confidence bar quoted in a report and not enforced in the seal
      would be decoration.
    """
    seal = plan.get("ollamaAgent")
    agent_candidates = [
        candidate
        for candidate in plan.get("autoCandidates", [])
        if isinstance(candidate, dict)
        and str(candidate.get("evidenceKind") or "") in AGENT_EVIDENCE_KINDS
    ]
    if seal is None:
        if agent_candidates:
            raise DecisionError(
                "category plan uses local model decisions without sealing them"
            )
        return
    if not isinstance(seal, dict):
        raise DecisionError("category plan agent seal has invalid shape")
    model = seal.get("model")
    endpoint = seal.get("endpoint")
    metrics = seal.get("metrics")
    rows = seal.get("suggestions")
    if (
        seal.get("schemaVersion") != 1
        or not isinstance(model, dict)
        or not isinstance(endpoint, dict)
        or not isinstance(metrics, dict)
        or not isinstance(rows, list)
        or not str(seal.get("promptSchemaFingerprint") or "")
    ):
        raise DecisionError("category plan agent seal has invalid shape")
    if endpoint.get("loopback") is not True:
        raise DecisionError("category plan agent seal names a non-loopback endpoint")
    if endpoint.get("webResearchEnabled") is not False:
        raise DecisionError(
            "category plan agent seal permits web research; refusing to trust it"
        )
    if not str(model.get("model") or "") or not str(model.get("digest") or ""):
        raise DecisionError("category plan agent seal does not identify its model")
    try:
        threshold = Decimal(str(seal.get("minConfidence")))
    except (ArithmeticError, ValueError, TypeError):
        raise DecisionError("category plan agent seal has no confidence threshold") from None
    if not threshold.is_finite() or not (Decimal(0) < threshold <= Decimal(1)):
        raise DecisionError("category plan agent confidence threshold is invalid")
    sealed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DecisionError("category plan agent evidence has invalid shape")
        required = {
            "evidenceHash",
            "clusterId",
            "merchantHash",
            "taxonomyId",
            "categoryId",
            "confidence",
            "activityCount",
            "sampleActivityIds",
        }
        if not required <= set(row):
            raise DecisionError("category plan agent evidence is incomplete")
        if not isinstance(row.get("sampleActivityIds"), list) or not row.get(
            "sampleActivityIds"
        ):
            raise DecisionError(
                "category plan agent evidence names no contributing activities"
            )
        if sealed.setdefault(str(row["evidenceHash"]), row) is not row:
            raise DecisionError("category plan agent evidence is duplicated")
    expected = plan_fingerprint({
        key: value for key, value in seal.items() if key != "sealFingerprint"
    })
    if seal.get("sealFingerprint") != expected:
        raise DecisionError("category plan agent seal fingerprint is invalid")
    for candidate in agent_candidates:
        row = sealed.get(str(candidate.get("agentEvidenceHash") or ""))
        if row is None:
            raise DecisionError("category candidate cites unsealed model evidence")
        if (
            row["taxonomyId"] != candidate.get("taxonomyId")
            or row["categoryId"] != candidate.get("categoryId")
            or row["merchantHash"] != candidate.get("merchantHash")
            or row["clusterId"] != candidate.get("agentClusterId")
            or str(row["confidence"]) != str(candidate.get("confidence"))
        ):
            raise DecisionError(
                "category candidate disagrees with its sealed model evidence"
            )
        if str(candidate.get("agentModelDigest") or "") != str(model.get("digest")):
            raise DecisionError("category candidate names a different model digest")
        try:
            confidence = Decimal(str(row["confidence"]))
        except (ArithmeticError, ValueError, TypeError):
            raise DecisionError("sealed model confidence is not a number") from None
        if not confidence.is_finite() or confidence < threshold:
            raise DecisionError(
                "category candidate applies a model decision below its threshold"
            )


def live_history_evidence_activity_ids(plan: dict[str, Any]) -> list[str]:
    """Every activity whose assignment a sealed live-history decision rests on."""
    seal = plan.get("liveHistory")
    if not isinstance(seal, dict):
        return []
    cited = {
        str(candidate.get("liveHistoryEvidenceHash") or "")
        for candidate in plan.get("autoCandidates", [])
        if isinstance(candidate, dict)
    }
    return sorted({
        str(activity_id)
        for row in seal.get("evidence", [])
        if isinstance(row, dict) and str(row.get("evidenceHash") or "") in cited
        for activity_id in row.get("sampleActivityIds", [])
        if str(activity_id or "")
    })


def live_history_drift(
    plan: dict[str, Any], assignments: dict[str, tuple[str, str] | None]
) -> list[dict[str, str]]:
    """Report every sealed live-history observation that no longer holds.

    ``assignments`` maps activity id to its *current* ``(taxonomyId,
    categoryId)`` assignment, or ``None`` when the activity is now
    uncategorized or gone. A category learned from history that has since been
    re-decided by a human is exactly the drift a promotion must refuse: the
    plan would otherwise write a decision the operator has already reversed.
    """
    seal = plan.get("liveHistory")
    if not isinstance(seal, dict):
        return []
    cited = {
        str(candidate.get("liveHistoryEvidenceHash") or "")
        for candidate in plan.get("autoCandidates", [])
        if isinstance(candidate, dict)
    }
    drift: list[dict[str, str]] = []
    for row in seal.get("evidence", []):
        if not isinstance(row, dict) or str(row.get("evidenceHash") or "") not in cited:
            continue
        expected = (str(row.get("taxonomyId")), str(row.get("categoryId")))
        for activity_id in row.get("sampleActivityIds", []):
            key = str(activity_id or "")
            if not key:
                continue
            current = assignments.get(key, "missing")
            if current == "missing":
                drift.append({"evidenceHash": row["evidenceHash"], "activityId": key,
                              "reason": "evidence-activity-missing"})
            elif current is None:
                drift.append({"evidenceHash": row["evidenceHash"], "activityId": key,
                              "reason": "evidence-activity-uncategorized"})
            elif tuple(current) != expected:
                drift.append({"evidenceHash": row["evidenceHash"], "activityId": key,
                              "reason": "evidence-activity-recategorized"})
    return sorted(drift, key=lambda row: (row["evidenceHash"], row["activityId"]))


def build_blocked_category_plan(
    blocked: list[CapabilityStatus],
    environment_fingerprint: str,
    *,
    report_start: str,
    report_end: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build an explicit, actionable artifact when a required read capability
    the production plan needs is unsupported, incompatible, or broken.

    This is never a mutation and never a fallback to a private database;
    it is a sealed record of exactly which Wealthfolio Spending capability
    blocked plan generation, so the gap is documented rather than silent.
    """
    if not blocked:
        raise DecisionError("a blocked category plan requires at least one blocked capability")
    generated_at = generated_at or datetime.now(timezone.utc)
    document = {
        "schemaVersion": 1,
        "mode": "category-plan-blocked",
        "generatedAt": generated_at.isoformat(),
        "productionMutation": False,
        "environmentFingerprint": environment_fingerprint,
        "reviewWindow": {"startDate": report_start, "endDate": report_end},
        "blockedCapabilities": [
            {
                "capability": status.capability,
                "status": status.status,
                "endpoint": status.endpoint,
                "detail": status.explanation,
            }
            for status in blocked
        ],
        "policy": {
            "unsupportedInterfaceAction": "block-plan-generation",
            "databaseFallbackPermitted": False,
        },
    }
    document["planFingerprint"] = plan_fingerprint(document)
    return document


def write_blocked_category_plan(data_dir: Path, plan: dict[str, Any]) -> Path:
    data_dir = validate_data_dir(data_dir)
    folder = data_dir / "normalized" / "simplefin"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(plan["generatedAt"]).strftime("%Y-%m-%d-%H%M%S-%f")
    path = folder / f"category-plan-blocked-{stamp}.json"
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


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
    pre_category_ids = plan.get("preCategoryActivityIds")
    if (
        not isinstance(pre_category_ids, list)
        or any(not isinstance(activity_id, str) or not activity_id for activity_id in pre_category_ids)
        or pre_category_ids != sorted(set(pre_category_ids))
    ):
        raise DecisionError("category plan has invalid pre-category activity scope")
    if not {
        str(candidate.get("activityId") or "") for candidate in plan["autoCandidates"]
    } <= set(pre_category_ids):
        raise DecisionError("category candidates leave the pre-category activity scope")
    evidence_rows = plan.get("evidence")
    if not isinstance(evidence_rows, list) or not evidence_rows:
        raise DecisionError("category plan has no sealed evidence")
    for evidence in evidence_rows:
        if not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}:
            raise DecisionError("category plan evidence has invalid shape")
        path = Path(str(evidence.get("path") or ""))
        if not path.is_file() or sha256_file(path) != evidence.get("sha256"):
            raise DecisionError(f"category evidence hash changed: {path.name}")
    transfer_review = plan.get("transferReview") or {
        "proposals": [],
        "confirmed": [],
        "rejected": [],
        "staleDecisions": [],
    }
    split_review = plan.get("splitReview") or {"groups": []}
    if (
        not isinstance(transfer_review, dict)
        or any(
            not isinstance(transfer_review.get(key), list)
            for key in ("proposals", "confirmed", "rejected", "staleDecisions")
        )
        or not isinstance(split_review, dict)
        or not isinstance(split_review.get("groups"), list)
    ):
        raise DecisionError("category plan has invalid transfer/split review summaries")
    _validate_live_history_seal(plan)
    _validate_agent_seal(plan)


#: Public name for the local-model seal check, so a caller can audit a plan's
#: model provenance on its own without re-validating the whole document.
validate_agent_seal = _validate_agent_seal


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
        f"- Transfer candidates awaiting review: {metrics['transferCandidateCount']}",
        f"- Confirmed transfer pairs: {metrics['confirmedTransferPairCount']}",
        f"- Rejected transfer pairs suppressed: {metrics['rejectedTransferPairCount']}",
        f"- Exact category split groups: {metrics['splitGroupCount']}",
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
        "| Activity ID | Merchant evidence | Category | Confidence | Evidence | History | Amount |",
        "|---|---|---|---:|---|---:|---:|",
    ])
    lines.extend(
        f"| `{row['activityId']}` | `{row['merchantHash'][:16]}` | "
        f"{row['categoryName']} | {row['confidence']} | "
        f"{row['evidenceKind']} | {row['evidenceCount']} | {row['amount']} |"
        for row in plan["autoCandidates"]
    )
    lines.extend([
        "",
        "## Manual and ambiguous items",
        "",
        "| Activity ID | Merchant evidence | Reason | Rule | Amount |",
        "|---|---|---|---|---:|",
    ])
    lines.extend(
        f"| `{row['activityId']}` | `{row['merchantHash'][:16]}` | "
        f"{row['reason']} | {row.get('ruleId') or ''} | {row['amount']} |"
        for row in plan["manualItems"]
    )
    lines.extend([
        "",
        "## Coverage by evidence kind",
        "",
        "| Evidence kind | Candidates |",
        "|---|---:|",
    ])
    lines.extend(
        f"| {kind or 'none'} | {count} |"
        for kind, count in (metrics.get("evidenceKindCounts") or {}).items()
    )
    lines.extend([
        "",
        "## Abstentions by reason",
        "",
        "| Reason | Items |",
        "|---|---:|",
    ])
    lines.extend(
        f"| {reason or 'none'} | {count} |"
        for reason, count in (metrics.get("manualReasonCounts") or {}).items()
    )
    live = plan.get("liveHistory")
    if isinstance(live, dict):
        live_scope = live["scope"]
        live_metrics = live["metrics"]
        lines.extend([
            "",
            "## Wealthfolio's own category history",
            "",
            f"- Index fingerprint: `{live['indexFingerprint']}`",
            f"- Lookback: {live_scope['lookbackMonths']} months "
            f"({live_scope['startDate']} to {live_scope['endDate']})",
            f"- Spending-enabled accounts read: {len(live_scope['accountIds'])}",
            f"- Sources: {', '.join(live_scope['sourceSystems'])}",
            f"- Minimum observations per decision: "
            f"{live_scope['minEvidenceCount']}",
            f"- Already-categorized activities observed: "
            f"{live_metrics['observedActivityCount']}",
            f"- Distinct merchants: {live_metrics['merchantCount']} "
            f"(conflicting: {live_metrics['conflictingMerchantCount']})",
            f"- Applied: {metrics.get('liveHistoryCount', 0)} "
            f"(account {metrics.get('liveHistoryAccountCount', 0)}, "
            f"global {metrics.get('liveHistoryGlobalCount', 0)})",
            f"- Candidates reached only by merchant identity: "
            f"{metrics.get('merchantIdentityCount', 0)}",
            "",
            "### Training exclusions",
            "",
            "| Excluded because | Activities |",
            "|---|---:|",
        ])
        lines.extend(
            f"| {reason} | {count} |"
            for reason, count in live_metrics["excludedCounts"].items()
        )
        lines.extend([
            "",
            "### Sealed live-history evidence",
            "",
            "| Evidence | Scope | Merchant evidence | Category | Observations | "
            "First | Last | Assignment provenance |",
            "|---|---|---|---|---:|---|---|---|",
        ])
        lines.extend(
            f"| `{row['evidenceHash'][:16]}` | {row['scope']} | "
            f"`{row['merchantHash'][:16]}` | {row['categoryId']} | "
            f"{row['evidenceCount']} | {row.get('firstSeen', '')} | "
            f"{row.get('lastSeen', '')} | "
            f"{', '.join(row.get('assignmentProvenance', []))} |"
            for row in live["evidence"]
        )
    agent = plan.get("ollamaAgent")
    if isinstance(agent, dict):
        agent_model = agent.get("model") or {}
        agent_endpoint = agent.get("endpoint") or {}
        agent_metrics = agent.get("metrics") or {}
        lines.extend([
            "",
            "## Local model (Ollama) decisions",
            "",
            f"- Seal fingerprint: `{agent.get('sealFingerprint', '')}`",
            f"- Model: `{agent_model.get('model', '')}` "
            f"(digest `{str(agent_model.get('digest', ''))[:16]}`)",
            f"- Endpoint: `{agent_endpoint.get('baseUrl', '')}` "
            f"(loopback: {'yes' if agent_endpoint.get('loopback') else 'no'}, "
            f"web research: "
            f"{'yes' if agent_endpoint.get('webResearchEnabled') else 'no'})",
            f"- Prompt/schema fingerprint: "
            f"`{agent.get('promptSchemaFingerprint', '')}` "
            f"(template v{agent.get('promptTemplateVersion', '')})",
            f"- Confidence threshold: {agent.get('minConfidence', '')}",
            f"- Applied: {metrics.get('agentCount', 0)} activities from "
            f"{metrics.get('agentClusterCount', 0)} merchant cluster(s)",
            f"- Left for manual review below threshold: "
            f"{agent_metrics.get('belowThresholdCount', 0)}",
            f"- Model abstentions: {agent_metrics.get('abstainedCount', 0)}",
            "",
            "### Sealed model evidence",
            "",
            "| Evidence | Cluster | Merchant evidence | Category | Confidence | "
            "Activities | Flags | Rationale |",
            "|---|---|---|---|---:|---:|---|---|",
        ])
        lines.extend(
            f"| `{row['evidenceHash'][:16]}` | `{row['clusterId']}` | "
            f"`{row['merchantHash'][:16]}` | {row['categoryId']} | "
            f"{row['confidence']} | {row['activityCount']} | "
            f"{', '.join(row.get('uncertaintyFlags', [])) or 'none'} | "
            f"{row.get('rationale', '')} |"
            for row in agent.get("suggestions", [])
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
        "## Transfer candidate review memory",
        "",
        "| Candidate | Status | Evidence | Currency | Days | Ambiguous |",
        "|---|---|---|---|---:|---|",
    ])
    lines.extend(
        f"| `{row['candidateId']}` | {row.get('status', 'proposed')} | "
        f"`{str(row['evidenceHash'])[:16]}` | {row.get('currency', '')} | "
        f"{row.get('dayDistance', '')} | {'yes' if row.get('ambiguous') else 'no'} |"
        for row in plan["transferReview"]["proposals"]
    )
    lines.extend(
        f"| `{row['candidateId']}` | rejected/suppressed | "
        f"`{str(row['evidenceHash'])[:16]}` |  |  |  |"
        for row in plan["transferReview"]["rejected"]
    )
    lines.extend(
        f"| `{row['candidateId']}` | confirmed | "
        f"`{str(row['evidenceHash'])[:16]}` |  |  |  |"
        for row in plan["transferReview"]["confirmed"]
    )
    lines.extend([
        "",
        "## Exact category splits",
        "",
        "| Split group | Decision | Parent source | Children | Parent amount |",
        "|---|---|---|---:|---:|",
    ])
    lines.extend(
        f"| `{row['groupId']}` | `{row['decisionId']}` | "
        f"`{row['parentSourceId']}` | {len(row.get('children', []))} | "
        f"{row['parentAmount']} |"
        for row in plan["splitReview"]["groups"]
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
    adapter: SpendingAdapter,
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
        key = staging_idempotency_key(candidate, stage_account)
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
        current = _assignment_category(adapter.assignment_rows(row["id"]))
        result.append((candidate, row, current))
    return result


def _rollback_assignments(
    client: Any,
    adapter: SpendingAdapter,
    plan: dict[str, Any],
    resolved: list[tuple[dict[str, Any], dict[str, Any], tuple[str, str] | None]],
    stage_account_map: dict[str, str],
    hash_key: bytes,
) -> None:
    """Undo every assignment this run applied, restoring the pre-run state."""
    for candidate, row, before in reversed(resolved):
        current = _assignment_category(adapter.assignment_rows(row["id"]))
        if current == before:
            continue
        if before is None:
            if current is not None:
                adapter.unassign(row["id"], current[0])
        else:
            adapter.assign(row["id"], before[0], before[1])
    rolled_back = _stage_rows(client, adapter, plan, stage_account_map, hash_key)
    if any(
        current != before
        for (_candidate, _row, current), (
            _before_candidate,
            _before_row,
            before,
        ) in zip(rolled_back, resolved, strict=True)
    ):
        raise DecisionError("category rollback did not restore staging")


def _report_window(plan: dict[str, Any]) -> dict[str, str]:
    return {
        "startDate": f"{plan['reviewWindow']['startDate']}T00:00:00Z",
        "endDate": f"{plan['reviewWindow']['endDate']}T23:59:59Z",
    }


def _read_spending_snapshot(
    adapter: SpendingAdapter, plan: dict[str, Any]
) -> dict[str, Any]:
    window = _report_window(plan)
    return spending_report_snapshot(
        adapter.report(window), adapter.uncategorized_count(window)
    )


def _desired_assignment_state(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "canonicalAccountId": candidate["canonicalAccountId"],
                "sourceAccountId": candidate["sourceAccountId"],
                "sourceId": candidate["sourceId"],
                "taxonomyId": candidate["taxonomyId"],
                "categoryId": candidate["categoryId"],
            }
            for candidate in plan["autoCandidates"]
        ],
        key=lambda row: (
            row["canonicalAccountId"],
            row["sourceAccountId"],
            row["sourceId"],
        ),
    )


def _desired_assignment_fingerprint(plan: dict[str, Any]) -> str:
    return plan_fingerprint(_desired_assignment_state(plan))


def _blocked_rehearsal_receipt(
    plan: dict[str, Any],
    status: CapabilityStatus,
    generated_at: datetime,
    *,
    target_count: int,
    backup: Any = None,
    applied_count: int = 0,
) -> dict[str, Any]:
    """An explicit, idempotent receipt when a Spending write capability the
    rehearsal needs is unsupported or broken, instead of a raw HTTP failure.
    """
    receipt = {
        "schemaVersion": 1,
        "mode": "category-staging-rehearsal",
        "generatedAt": generated_at.isoformat(),
        "productionMutated": False,
        "status": "blocked",
        "categoryPlanFingerprint": plan["planFingerprint"],
        "categoryPlanSha256": None,
        "backup": backup,
        "appliedCount": applied_count,
        "targetCount": target_count,
        "postCategoryFingerprint": None,
        "blockedCapability": {
            "capability": status.capability,
            "status": status.status,
            "endpoint": status.endpoint,
            "detail": status.explanation,
        },
    }
    receipt["receiptFingerprint"] = plan_fingerprint(receipt)
    return receipt


def rehearse_category_plan(
    client: Any,
    plan: dict[str, Any],
    stage_account_map: dict[str, str],
    hash_key: bytes,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Apply a sealed category plan only after the caller validates staging.

    Every Spending endpoint this touches (assignment read/write/delete,
    database backup) goes through `SpendingAdapter`. An unsupported or
    broken capability produces an explicit `status: "blocked"` receipt
    instead of a raw HTTP failure or an uninformative crash; any other
    failure still rolls back and re-raises exactly as before.
    """
    validate_category_plan(plan)
    generated_at = generated_at or datetime.now(timezone.utc)
    adapter = SpendingAdapter(client)
    try:
        resolved = _stage_rows(client, adapter, plan, stage_account_map, hash_key)
    except SpendingCapabilityBlocked as exc:
        return _blocked_rehearsal_receipt(
            plan, exc.status, generated_at, target_count=len(plan["autoCandidates"])
        )

    already_applied = all(
        current == (candidate["taxonomyId"], candidate["categoryId"])
        for candidate, _row, current in resolved
    )
    try:
        before_report = _read_spending_snapshot(adapter, plan)
    except SpendingCapabilityBlocked as exc:
        return _blocked_rehearsal_receipt(
            plan, exc.status, generated_at, target_count=len(resolved)
        )
    if already_applied:
        if before_report != plan.get("expectedWealthfolioWindow"):
            raise DecisionError("already-applied staging Spending report differs from plan")
        status = "already-applied"
        backup = None
        applied: list[tuple[dict[str, Any], dict[str, Any]]] = []
    else:
        if before_report != plan.get("preWealthfolioWindow"):
            raise DecisionError("staging Spending report differs from sealed pre-state")
        conflicting = [
            current
            for candidate, _row, current in resolved
            if current
            and current != (candidate["taxonomyId"], candidate["categoryId"])
        ]
        if conflicting:
            raise DecisionError("staging contains conflicting category assignments")
        try:
            backup = adapter.backup()
        except SpendingCapabilityBlocked as exc:
            return _blocked_rehearsal_receipt(
                plan, exc.status, generated_at, target_count=len(resolved)
            )
        if not backup:
            raise DecisionError("staging backup was not confirmed")
        applied = []
        try:
            for candidate, row, current in resolved:
                if current:
                    continue
                adapter.assign(row["id"], candidate["taxonomyId"], candidate["categoryId"])
                applied.append((candidate, row))
            verified = _stage_rows(client, adapter, plan, stage_account_map, hash_key)
            if any(
                current != (candidate["taxonomyId"], candidate["categoryId"])
                for candidate, _row, current in verified
            ):
                raise DecisionError("staging category verification failed")
            after_report = _read_spending_snapshot(adapter, plan)
            if after_report != plan.get("expectedWealthfolioWindow"):
                raise DecisionError("staging Spending report verification failed")
            status = "applied"
        except SpendingCapabilityBlocked as exc:
            _rollback_assignments(client, adapter, plan, resolved, stage_account_map, hash_key)
            return _blocked_rehearsal_receipt(
                plan,
                exc.status,
                generated_at,
                target_count=len(resolved),
                backup=backup,
            )
        except Exception:
            _rollback_assignments(client, adapter, plan, resolved, stage_account_map, hash_key)
            raise

    final = _stage_rows(client, adapter, plan, stage_account_map, hash_key)
    if already_applied:
        after_report = before_report
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
        "verification": {
            "status": "passed",
            "before": before_report,
            "after": after_report,
        },
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


def validate_rehearsal_receipt(
    plan: dict[str, Any], plan_path: Path, receipt_path: Path
) -> dict[str, Any]:
    """Require the exact successful staging rehearsal for this category plan."""
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    fingerprint = plan_fingerprint({
        key: value for key, value in receipt.items() if key != "receiptFingerprint"
    })
    if receipt.get("receiptFingerprint") != fingerprint:
        raise DecisionError("staging category rehearsal receipt fingerprint is invalid")
    if (
        receipt.get("schemaVersion") != 1
        or receipt.get("mode") != "category-staging-rehearsal"
        or receipt.get("status") != "applied"
        or receipt.get("productionMutated") is not False
        or receipt.get("categoryPlanFingerprint") != plan["planFingerprint"]
        or receipt.get("categoryPlanSha256") != sha256_file(plan_path)
        or receipt.get("targetCount") != len(plan["autoCandidates"])
        or receipt.get("appliedCount") != len(plan["autoCandidates"])
        or receipt.get("postCategoryFingerprint")
        != _desired_assignment_fingerprint(plan)
        or receipt.get("verification", {}).get("status") != "passed"
        or receipt.get("verification", {}).get("after")
        != plan.get("expectedWealthfolioWindow")
        or not receipt.get("backup")
    ):
        raise DecisionError(
            "promotion requires the exact successful staging category rehearsal"
        )
    return receipt


def _validate_promotion_blockers(plan: dict[str, Any]) -> None:
    transfer_review = plan.get("transferReview") or {}
    if transfer_review.get("staleDecisions"):
        raise DecisionError("category plan has unresolved blocking transfer decisions")
    if any(
        row.get("categoryAction") != "none"
        for row in plan.get("transfersAndReconciliation", [])
    ):
        raise DecisionError("category plan has invalid transfer decisions")
    if any(
        row.get("classification") != "balance-gap-reconciliation"
        or row.get("categoryAction") != "none"
        for row in plan.get("balanceGapReconciliation", [])
    ):
        raise DecisionError("category plan has invalid reconciliation decisions")


def _production_rows(
    client: Any,
    adapter: SpendingAdapter,
    plan: dict[str, Any],
    hash_key: bytes,
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any], tuple[str, str] | None]],
    str,
]:
    rows = list(client.iter_activities())
    start = plan["reviewWindow"]["startDate"]
    end = plan["reviewWindow"]["endDate"]
    # A plan sealed before this module became source-agnostic has no
    # ``sourceSystems``; SimpleFIN was the only thing it could have enumerated,
    # so it keeps exactly the original scope and per-transaction key derivation.
    systems = normalize_source_systems(plan.get("sourceSystems"))
    scoped = [
        row
        for row in rows
        if in_source_scope(row, systems)
        and start <= str(row.get("date") or "")[:10] <= end
    ]
    pre_category_ids = set(plan["preCategoryActivityIds"])
    assignments = {
        str(row.get("id") or ""): _assignment_category(
            adapter.assignment_rows(str(row.get("id") or ""))
        )
        for row in scoped
        if str(row.get("id") or "") in pre_category_ids
    }
    state = [
        {
            "activityId": str(row.get("id") or ""),
            "accountId": str(row.get("accountId") or ""),
            "sourceId": _plan_source_key(row, systems),
            "activityType": str(row.get("activityType") or ""),
            "date": str(row.get("date") or "")[:10],
            "amount": _money(abs(Decimal(str(row.get("amount") or 0)))),
            "assignment": list(assignments.get(str(row.get("id") or "")))
            if assignments.get(str(row.get("id") or ""))
            else None,
        }
        for row in scoped
        if str(row.get("id") or "") in pre_category_ids
    ]
    if {str(row.get("id") or "") for row in scoped} & pre_category_ids != pre_category_ids:
        raise DecisionError("production pre-category activity scope changed after planning")
    by_id = {str(row.get("id") or ""): row for row in scoped}
    resolved = []
    for candidate in plan["autoCandidates"]:
        row = by_id.get(candidate["activityId"])
        if row is None:
            raise DecisionError("category candidate is missing from production")
        identity = {
            "canonicalAccountId": candidate["canonicalAccountId"],
            "sourceAccountId": candidate["sourceAccountId"],
            "sourceId": candidate["sourceId"],
        }
        if _portable_activity(row, identity, hash_key) != candidate["portableActivity"]:
            raise DecisionError("production activity differs from reviewed category evidence")
        resolved.append((candidate, row, assignments[candidate["activityId"]]))
    return resolved, plan_fingerprint(sorted(state, key=lambda row: row["activityId"]))


def _assignment_receipt_rows(
    resolved: list[tuple[dict[str, Any], dict[str, Any], tuple[str, str] | None]],
    *,
    after: bool,
) -> list[dict[str, Any]]:
    result = []
    for candidate, _row, before in resolved:
        assignment = (
            (candidate["taxonomyId"], candidate["categoryId"]) if after else before
        )
        result.append({
            "activityId": candidate["activityId"],
            "canonicalAccountId": candidate["canonicalAccountId"],
            "sourceAccountId": candidate["sourceAccountId"],
            "sourceId": candidate["sourceId"],
            "assignment": {
                "taxonomyId": assignment[0],
                "categoryId": assignment[1],
            }
            if assignment
            else None,
        })
    return sorted(result, key=lambda row: row["activityId"])


def _restore_production_assignments(
    adapter: SpendingAdapter,
    resolved: list[tuple[dict[str, Any], dict[str, Any], tuple[str, str] | None]],
) -> None:
    for _candidate, row, before in reversed(resolved):
        current = _assignment_category(adapter.assignment_rows(row["id"]))
        if current == before:
            continue
        if before is None:
            if current is not None:
                adapter.unassign(row["id"], current[0])
        else:
            adapter.assign(row["id"], before[0], before[1])
    for _candidate, row, before in resolved:
        if _assignment_category(adapter.assignment_rows(row["id"])) != before:
            raise DecisionError("production category rollback did not restore an assignment")


def fresh_backup(adapter: SpendingAdapter) -> dict[str, Any]:
    before = adapter.backups()
    known = {str(row.get("filename") or "") for row in before if isinstance(row, dict)}
    response = adapter.backup()
    if not isinstance(response, dict) or not str(response.get("filename") or ""):
        raise DecisionError("production category backup was not confirmed")
    filename = str(response["filename"])
    after = adapter.backups()
    matches = [
        row
        for row in after
        if isinstance(row, dict) and str(row.get("filename") or "") == filename
    ]
    if filename in known or len(matches) != 1:
        raise DecisionError("production category backup is not demonstrably fresh")
    metadata = matches[0]
    try:
        size_bytes = int(metadata.get("sizeBytes") or 0)
    except (ValueError, TypeError):
        raise DecisionError("production category backup metadata is incomplete") from None
    if size_bytes <= 0 or not metadata.get("modifiedAt"):
        raise DecisionError("production category backup metadata is incomplete")
    return {
        "filename": filename,
        "sizeBytes": size_bytes,
        "modifiedAt": str(metadata["modifiedAt"]),
    }


def write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        raise DecisionError(f"refusing to overwrite immutable receipt: {path.name}") from None
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        path.chmod(0o444)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _validate_existing_promotion_receipt(
    receipt: dict[str, Any],
    plan: dict[str, Any],
    plan_path: Path,
    environment_fingerprint: str,
) -> None:
    expected = plan_fingerprint({
        key: value for key, value in receipt.items() if key != "receiptFingerprint"
    })
    if (
        receipt.get("receiptFingerprint") != expected
        or receipt.get("schemaVersion") != 1
        or receipt.get("mode") != "category-production-promotion"
        or receipt.get("status") != "applied"
        or receipt.get("categoryPlanFingerprint") != plan["planFingerprint"]
        or receipt.get("categoryPlanSha256") != sha256_file(plan_path)
        or receipt.get("environmentFingerprint") != environment_fingerprint
        or receipt.get("afterReport") != plan.get("expectedWealthfolioWindow")
        or not receipt.get("backup")
    ):
        raise DecisionError("existing production category receipt is invalid")


def _require_no_live_history_drift(
    adapter: SpendingAdapter, plan: dict[str, Any]
) -> None:
    """Refuse to promote when the history a decision was learned from moved.

    The plan sealed the exact assignments each live-history decision rests on.
    Re-reading only those is bounded and precise: if a human has since
    re-categorized or cleared one of them, the consensus that produced the
    candidate no longer exists and the plan is stale, not merely old.
    """
    activity_ids = live_history_evidence_activity_ids(plan)
    if not activity_ids:
        return
    current: dict[str, tuple[str, str] | None] = {}
    for activity_id in activity_ids:
        try:
            current[activity_id] = _assignment_category(
                adapter.assignment_rows(activity_id)
            )
        except SpendingCapabilityBlocked as exc:
            if exc.status.status != "unsupported":
                raise
            # The evidence activity itself is gone. That is drift in the
            # history, not a broken endpoint -- the endpoint answered for every
            # other activity this promotion already read.
            current[activity_id] = None
        except DecisionError:
            current[activity_id] = None
    drift = live_history_drift(plan, current)
    if drift:
        raise DecisionError(
            "live category history changed after planning: "
            f"{len(drift)} sealed observations no longer match"
        )


def _promote_category_plan_locked(
    client: Any,
    plan: dict[str, Any],
    plan_path: Path,
    rehearsal_receipt_path: Path,
    data_dir: Path,
    hash_key: bytes,
    *,
    environment_fingerprint: str,
    supplied_plan_fingerprint: str,
    supplied_environment_fingerprint: str,
    allow_production: bool,
    wait_seconds: float = 10,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], Path, bool]:
    """Promote one exactly rehearsed category plan, or prove it already applied."""
    validate_category_plan(plan)
    if not allow_production:
        raise DecisionError("--allow-production is required")
    if supplied_plan_fingerprint != plan["planFingerprint"]:
        raise DecisionError("operator-supplied category plan fingerprint is not exact")
    if (
        supplied_environment_fingerprint != plan.get("environmentFingerprint")
        or environment_fingerprint != plan.get("environmentFingerprint")
    ):
        raise DecisionError("production environment fingerprint is not exact")
    if not isinstance(plan.get("preWealthfolioWindow"), dict) or not isinstance(
        plan.get("expectedWealthfolioWindow"), dict
    ):
        raise DecisionError("category plan lacks sealed Spending verification values")
    if not plan.get("autoCandidates"):
        raise DecisionError("category plan has no assignments to promote")
    _validate_promotion_blockers(plan)
    validate_rehearsal_receipt(plan, plan_path, rehearsal_receipt_path)
    validated_plan_sha = sha256_file(plan_path)
    adapter = SpendingAdapter(client)
    resolved, current_fingerprint = _production_rows(client, adapter, plan, hash_key)
    desired = [
        (candidate["taxonomyId"], candidate["categoryId"])
        for candidate, _row, _before in resolved
    ]
    currents = [before for _candidate, _row, before in resolved]
    all_applied = currents == desired
    none_applied = all(current is None for current in currents)
    receipt_folder = data_dir / "normalized" / "simplefin"
    receipt_folder.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_folder / (
        f"category-promotion-{validated_plan_sha}.json"
    )
    if receipt_path.exists():
        if receipt_path.stat().st_mode & stat.S_IWRITE:
            raise DecisionError("existing production category receipt is not immutable")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        _validate_existing_promotion_receipt(
            receipt, plan, plan_path, environment_fingerprint
        )
        if not all_applied:
            raise DecisionError("production assignments drifted after category promotion")
        if _read_spending_snapshot(adapter, plan) != plan["expectedWealthfolioWindow"]:
            raise DecisionError("production Spending report drifted after category promotion")
        return receipt, receipt_path, True
    if all_applied:
        raise DecisionError("production categories are applied without an immutable receipt")
    if not none_applied:
        raise DecisionError("production has a partially applied category plan")
    if current_fingerprint != plan["preCategoryFingerprint"]:
        raise DecisionError("production pre-category fingerprint changed after planning")
    _require_no_live_history_drift(adapter, plan)
    before_report = _read_spending_snapshot(adapter, plan)
    if before_report != plan["preWealthfolioWindow"]:
        raise DecisionError("production Spending report changed after planning")
    backup = fresh_backup(adapter)
    if sha256_file(plan_path) != validated_plan_sha:
        raise DecisionError("category plan file changed before production mutation")
    validate_category_plan(plan)
    validate_rehearsal_receipt(plan, plan_path, rehearsal_receipt_path)
    resolved, current_fingerprint = _production_rows(client, adapter, plan, hash_key)
    if (
        current_fingerprint != plan["preCategoryFingerprint"]
        or any(before is not None for _candidate, _row, before in resolved)
        or _read_spending_snapshot(adapter, plan) != before_report
    ):
        raise DecisionError("production category state changed before mutation")
    generated_at = generated_at or datetime.now(timezone.utc)
    try:
        for candidate, row, _before in resolved:
            adapter.assign(row["id"], candidate["taxonomyId"], candidate["categoryId"])
        verified = [
            _assignment_category(adapter.assignment_rows(row["id"]))
            for _candidate, row, _before in resolved
        ]
        if verified != desired:
            raise DecisionError("production category assignment verification failed")
        deadline = time.monotonic() + wait_seconds
        while True:
            after_report = _read_spending_snapshot(adapter, plan)
            if after_report == plan["expectedWealthfolioWindow"]:
                break
            if time.monotonic() >= deadline:
                raise DecisionError(
                    "production category totals, cash flow, or uncategorized count "
                    "differ from the sealed plan"
                )
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        if sha256_file(plan_path) != validated_plan_sha:
            raise DecisionError("category plan file changed during production mutation")
        validate_category_plan(plan)
        validate_rehearsal_receipt(plan, plan_path, rehearsal_receipt_path)
        receipt = {
            "schemaVersion": 1,
            "mode": "category-production-promotion",
            "generatedAt": generated_at.isoformat(),
            "status": "applied",
            "categoryPlan": str(plan_path),
            "categoryPlanSha256": validated_plan_sha,
            "categoryPlanFingerprint": plan["planFingerprint"],
            "stagingReceipt": {
                "path": str(rehearsal_receipt_path),
                "sha256": sha256_file(rehearsal_receipt_path),
            },
            "environmentFingerprint": environment_fingerprint,
            "preCategoryFingerprint": plan["preCategoryFingerprint"],
            "postCategoryFingerprint": _desired_assignment_fingerprint(plan),
            "backup": backup,
            "beforeAssignments": _assignment_receipt_rows(resolved, after=False),
            "afterAssignments": _assignment_receipt_rows(resolved, after=True),
            "beforeReport": before_report,
            "afterReport": after_report,
        }
        receipt["receiptFingerprint"] = plan_fingerprint(receipt)
        write_immutable_json(receipt_path, receipt)
    except Exception as exc:
        try:
            _restore_production_assignments(adapter, resolved)
        except Exception as rollback_exc:
            raise DecisionError(
                f"production category promotion failed and rollback failed: {rollback_exc}"
            ) from exc
        raise
    return receipt, receipt_path, False


def promote_category_plan(
    client: Any,
    plan: dict[str, Any],
    plan_path: Path,
    rehearsal_receipt_path: Path,
    data_dir: Path,
    hash_key: bytes,
    *,
    environment_fingerprint: str,
    supplied_plan_fingerprint: str,
    supplied_environment_fingerprint: str,
    allow_production: bool,
    wait_seconds: float = 10,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], Path, bool]:
    """Serialize promotions so concurrent retries cannot undo a successful run."""
    receipt_folder = data_dir / "normalized" / "simplefin"
    receipt_folder.mkdir(parents=True, exist_ok=True)
    lock_path = receipt_folder / f".category-promotion-{sha256_file(plan_path)}.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        raise DecisionError("another category promotion is already in progress") from None
    try:
        return _promote_category_plan_locked(
            client,
            plan,
            plan_path,
            rehearsal_receipt_path,
            data_dir,
            hash_key,
            environment_fingerprint=environment_fingerprint,
            supplied_plan_fingerprint=supplied_plan_fingerprint,
            supplied_environment_fingerprint=supplied_environment_fingerprint,
            allow_production=allow_production,
            wait_seconds=wait_seconds,
            generated_at=generated_at,
        )
    finally:
        os.close(descriptor)
        lock_path.chmod(0o600)
        lock_path.unlink(missing_ok=True)
