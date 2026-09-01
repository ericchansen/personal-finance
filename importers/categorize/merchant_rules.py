"""Transparent, reviewable exact-merchant rules learned from reviewed history.

Actual Budget's payee rules are the model here: a rule is an *exact* normalized
merchant string mapped to one category, with nothing hidden behind a model or a
remote service. Everything in this module is local, deterministic, and
explainable -- a rule exists only because a stated number of already-reviewed
canonical transactions agree unanimously on a category, and the rule carries
that evidence with it.

Two scopes are learned, matching the resolution order the category plan already
applies:

* **account** -- the merchant means one thing on one canonical account.
* **global**  -- the merchant means the same thing on every account that has
  seen it.

An account rule outranks a global rule. A merchant whose category conflicts
*anywhere* produces no rule at either scope and is instead reported as a
conflict for a human to resolve, because a conflicting merchant is exactly the
case where guessing does damage.

The emitted document is merchant-redacted: merchants appear only as the keyed
HMAC-SHA256 digest the sealed plan already uses, so a rule set can be reviewed,
diffed, and archived without ever writing a payee string down.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from importers.rebuild.safety import plan_fingerprint
from importers.simplefin.categorization import (
    MIN_HISTORY_COUNT,
    HistoryIndex,
    merchant_hash,
    validate_data_dir,
)

#: Confidence the category plan records for each scope, repeated here so a rule
#: set states the same number the plan will.
ACCOUNT_SCOPE_CONFIDENCE = "0.98"
GLOBAL_SCOPE_CONFIDENCE = "0.95"


def _rule_id(scope: str, merchant_digest: str, account_id: str) -> str:
    return "merchant-{scope}-{digest}".format(
        scope=scope,
        digest=plan_fingerprint(
            {"scope": scope, "merchantHash": merchant_digest, "account": account_id}
        )[:16],
    )


def _rule(
    history: HistoryIndex,
    *,
    scope: str,
    account_id: str,
    normalized_merchant: str,
    category: str,
    merchant_digest: str,
) -> dict[str, Any]:
    evidence = history.evidence(account_id, normalized_merchant, category)
    return {
        "ruleId": _rule_id(scope, merchant_digest, account_id),
        "scope": scope,
        "canonicalAccountId": account_id,
        "merchantHash": merchant_digest,
        "category": category,
        "confidence": (
            ACCOUNT_SCOPE_CONFIDENCE if scope == "account" else GLOBAL_SCOPE_CONFIDENCE
        ),
        "evidenceCount": evidence.evidence_count,
        "sourceSystems": list(evidence.source_systems),
        "firstSeen": evidence.first_seen,
        "lastSeen": evidence.last_seen,
    }


def build_merchant_rules(
    history: HistoryIndex,
    merchant_hash_key: bytes,
    *,
    min_evidence: int = MIN_HISTORY_COUNT,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Derive an auditable rule set from unanimous reviewed canonical history.

    The unanimity and precedence tests mirror the category plan's own history
    resolution exactly, so a rule in this document is a faithful statement of
    what the plan would decide -- not a second, divergent opinion.
    """
    if min_evidence < 1:
        raise ValueError("min_evidence must be at least 1")
    generated_at = generated_at or datetime.now(timezone.utc)

    digests: dict[str, str] = {}

    def digest(normalized_merchant: str) -> str:
        if normalized_merchant not in digests:
            digests[normalized_merchant] = merchant_hash(
                normalized_merchant, merchant_hash_key
            )
        return digests[normalized_merchant]

    conflicts: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []

    conflicted_merchants: set[str] = set()
    for normalized_merchant, categories in history.global_merchants.items():
        if len(categories) <= 1:
            continue
        conflicted_merchants.add(normalized_merchant)
        conflicts.append({
            "scope": "global",
            "merchantHash": digest(normalized_merchant),
            "categoryCount": len(categories),
            "evidenceCount": sum(len(sources) for sources in categories.values()),
        })

    for (account_id, normalized_merchant), categories in history.by_account.items():
        if normalized_merchant in conflicted_merchants:
            continue
        if len(categories) > 1:
            conflicted_merchants.add(normalized_merchant)
            conflicts.append({
                "scope": "account",
                "merchantHash": digest(normalized_merchant),
                "categoryCount": len(categories),
                "evidenceCount": sum(len(sources) for sources in categories.values()),
            })
            continue
        category, sources = next(iter(categories.items()))
        row = _rule(
            history,
            scope="account",
            account_id=account_id,
            normalized_merchant=normalized_merchant,
            category=category,
            merchant_digest=digest(normalized_merchant),
        )
        (rules if len(sources) >= min_evidence else pending).append(row)

    for normalized_merchant, categories in history.global_merchants.items():
        if normalized_merchant in conflicted_merchants or len(categories) != 1:
            continue
        category, sources = next(iter(categories.items()))
        row = _rule(
            history,
            scope="global",
            account_id="",
            normalized_merchant=normalized_merchant,
            category=category,
            merchant_digest=digest(normalized_merchant),
        )
        (rules if len(sources) >= min_evidence else pending).append(row)

    def order(row: dict[str, Any]) -> tuple[str, str, str]:
        return (row["scope"], row["merchantHash"], row["canonicalAccountId"])

    document = {
        "schemaVersion": 1,
        "kind": "merchant-rule-set",
        "generatedAt": generated_at.isoformat(),
        "minEvidenceCount": min_evidence,
        "precedence": ["account", "global"],
        "rules": sorted(rules, key=order),
        "pendingRules": sorted(pending, key=order),
        "conflicts": sorted(
            conflicts, key=lambda row: (row["scope"], row["merchantHash"])
        ),
        "metrics": {
            "ruleCount": len(rules),
            "accountRuleCount": sum(row["scope"] == "account" for row in rules),
            "globalRuleCount": sum(row["scope"] == "global" for row in rules),
            "pendingRuleCount": len(pending),
            "conflictCount": len(conflicts),
            "sourceSystems": sorted(
                {
                    system
                    for row in rules + pending
                    for system in row["sourceSystems"]
                }
            ),
        },
    }
    document["ruleSetFingerprint"] = plan_fingerprint(document)
    return document


def summarize_merchant_rules(document: dict[str, Any]) -> str:
    """A PII-free one-line summary safe to print to a terminal."""
    metrics = document["metrics"]
    return (
        f"rules={metrics['ruleCount']} "
        f"(account={metrics['accountRuleCount']} global={metrics['globalRuleCount']}) "
        f"pending={metrics['pendingRuleCount']} conflicts={metrics['conflictCount']} "
        f"minEvidence={document['minEvidenceCount']}"
    )


def write_merchant_rules(data_dir: Path, document: dict[str, Any]) -> Path:
    """Write a rule set under the private data directory, never into the repo."""
    data_dir = validate_data_dir(data_dir)
    folder = data_dir / "normalized" / "categorize"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(document["generatedAt"]).strftime(
        "%Y-%m-%d-%H%M%S-%f"
    )
    path = folder / f"merchant-rules-{stamp}.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_merchant_rule_review(data_dir: Path, document: dict[str, Any]) -> Path:
    """Write a merchant-redacted Markdown companion for the rule set."""
    data_dir = validate_data_dir(data_dir)
    folder = data_dir / "normalized" / "categorize"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(document["generatedAt"]).strftime(
        "%Y-%m-%d-%H%M%S-%f"
    )
    path = folder / f"merchant-rules-{stamp}.md"
    lines = [
        "# Learned merchant rules",
        "",
        f"- Rule set fingerprint: `{document['ruleSetFingerprint']}`",
        f"- Minimum reviewed observations per rule: {document['minEvidenceCount']}",
        f"- Precedence: {' before '.join(document['precedence'])}",
        f"- {summarize_merchant_rules(document)}",
        "",
        "## Rules",
        "",
        "| Rule | Scope | Merchant evidence | Category | Evidence | Sources | First | Last |",
        "|---|---|---|---|---:|---|---|---|",
    ]
    lines.extend(
        f"| `{row['ruleId']}` | {row['scope']} | `{row['merchantHash'][:16]}` | "
        f"{row['category']} | {row['evidenceCount']} | "
        f"{', '.join(row['sourceSystems'])} | {row['firstSeen']} | {row['lastSeen']} |"
        for row in document["rules"]
    )
    lines.extend([
        "",
        "## Below the evidence threshold",
        "",
        "| Scope | Merchant evidence | Category | Evidence |",
        "|---|---|---|---:|",
    ])
    lines.extend(
        f"| {row['scope']} | `{row['merchantHash'][:16]}` | {row['category']} | "
        f"{row['evidenceCount']} |"
        for row in document["pendingRules"]
    )
    lines.extend([
        "",
        "## Conflicting merchants (no rule is learned)",
        "",
        "| Scope | Merchant evidence | Categories | Evidence |",
        "|---|---|---:|---:|",
    ])
    lines.extend(
        f"| {row['scope']} | `{row['merchantHash'][:16]}` | {row['categoryCount']} | "
        f"{row['evidenceCount']} |"
        for row in document["conflicts"]
    )
    lines.extend([
        "",
        "No merchant descriptions are included. Merchants appear only as keyed "
        "HMAC-SHA256 digests.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
