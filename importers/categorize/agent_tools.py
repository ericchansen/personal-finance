"""Deterministic local evidence for the Ollama categorization agent.

The model is never handed a bare merchant string and asked to guess. Every
question it answers is accompanied by tool output assembled *here*, locally,
from the same private evidence the deterministic planner already trusts:

======================  =====================================================
tool                    what it answers
======================  =====================================================
``live_taxonomy``       which categories exist, and how they nest
``transaction_shape``   direction, activity type, account kind
``recurrence``          how often this merchant recurs and in what amount band
``source_evidence``     which importer produced the rows, and how they joined
``category_history``    what live and canonical history already say
``structural_guard``    whether a structural rule forbids categorizing at all
``merchant_research``   optional, local-only, disabled by default
======================  =====================================================

Two invariants hold everywhere in this module:

1. **Structural exclusions are not negotiable.** ``structural_guard`` runs
   *before* a cluster is ever offered to a model, and a blocked cluster is
   dropped rather than described. A model cannot argue a transfer into being a
   purchase because it never sees one.
2. **The raw payee never leaves memory.** A cluster keeps the normalized payee
   in a private attribute with a redacting ``__repr__``, and the only document
   it can serialize itself into is keyed by the HMAC digest. Nothing in this
   module can write, log, hash or print merchant text by accident.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from importers.categorize.identity import parse_source_identity
from importers.categorize.live_history import (
    structural_exclusion,
    taxonomy_for_bucket,
)
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint
from importers.simplefin.categorization import (
    HistoryIndex,
    cash_bucket,
    history_choice,
    merchant_hash,
)
from importers.simplefin.pipeline import normalize_description
from importers.simplefin.spending_adapter import (
    INCOME_TAXONOMY,
    SPENDING_TAXONOMY,
)

#: Manual reasons the deterministic planner reaches by *running out of
#: evidence*, which is exactly and only where a local model may be consulted.
#: Every other reason is either an identity problem (there is nowhere to write
#: a decision), a structural exclusion (a decision would be wrong), or an
#: explicit deterministic refusal such as ``conflicting-history`` (a decision
#: would paper over a contradiction a human must settle).
AGENT_ELIGIBLE_REASONS = frozenset({
    "no-history",
    "insufficient-history",
    "unmapped-category",
})

#: Amount bands, not amounts, are what a model needs to tell a coffee from a
#: mortgage. Quantized to whole currency units in the prompt.
_CENTS = Decimal("0.01")

_VOCABULARY_TOKEN = re.compile(r"[a-z0-9]+")


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(_CENTS)
    except (ArithmeticError, ValueError):
        raise DecisionError("activity amount is not a decimal value") from None


@dataclass(frozen=True)
class ClusterMember:
    """One uncategorized activity inside a merchant cluster."""

    activity_id: str
    account_id: str
    canonical_account_id: str
    account_type: str
    date: str
    amount: Decimal
    activity_type: str
    source_system: str
    reason: str

    def as_document(self) -> dict[str, Any]:
        return {
            "activityId": self.activity_id,
            "accountType": self.account_type,
            "date": self.date,
            "activityType": self.activity_type,
            "sourceSystem": self.source_system,
            "reason": self.reason,
        }


class MerchantCluster:
    """Every unresolved activity that shares one merchant *and* one direction.

    Deliberately not a dataclass. ``plan_fingerprint`` serializes dataclasses
    via :func:`dataclasses.asdict`, so a dataclass field holding the normalized
    payee could be hashed -- or worse, written -- by any caller that passed the
    cluster itself instead of :meth:`as_evidence_document`. A plain object with
    a private attribute and a redacting ``__repr__`` removes that whole class of
    accident, including leakage into a pytest assertion diff.
    """

    __slots__ = ("_payee", "merchant_hash", "taxonomy_id", "bucket", "members")

    def __init__(
        self,
        *,
        normalized_payee: str,
        merchant_hash: str,
        taxonomy_id: str,
        bucket: str,
        members: Sequence[ClusterMember],
    ) -> None:
        self._payee = normalized_payee
        self.merchant_hash = merchant_hash
        self.taxonomy_id = taxonomy_id
        self.bucket = bucket
        self.members = tuple(members)

    def __repr__(self) -> str:  # pragma: no cover - defensive, not behaviour
        return (
            f"MerchantCluster(merchantHash={self.merchant_hash[:16]!r}, "
            f"taxonomyId={self.taxonomy_id!r}, activities={len(self.members)})"
        )

    @property
    def cluster_id(self) -> str:
        """A stable, merchant-free identifier for this cluster."""
        return f"{self.taxonomy_id}:{self.merchant_hash[:32]}"

    @property
    def prompt_payee(self) -> str:
        """The raw normalized payee. Loopback model prompt only -- never stored."""
        return self._payee

    @property
    def activity_ids(self) -> tuple[str, ...]:
        return tuple(member.activity_id for member in self.members)

    @property
    def amounts(self) -> tuple[Decimal, ...]:
        return tuple(member.amount for member in self.members)

    @property
    def first_seen(self) -> str:
        return min((member.date for member in self.members), default="")

    @property
    def last_seen(self) -> str:
        return max((member.date for member in self.members), default="")

    def as_document(self) -> dict[str, Any]:
        """The merchant-redacted description of this cluster."""
        return {
            "clusterId": self.cluster_id,
            "merchantHash": self.merchant_hash,
            "taxonomyId": self.taxonomy_id,
            "bucket": self.bucket,
            "activityCount": len(self.members),
            "activityIds": sorted(self.activity_ids),
            "accountTypes": sorted({member.account_type for member in self.members}),
            "sourceSystems": sorted(
                {member.source_system for member in self.members if member.source_system}
            ),
            "reasons": sorted({member.reason for member in self.members}),
            "firstSeen": self.first_seen,
            "lastSeen": self.last_seen,
        }


def build_clusters(
    plan: dict[str, Any],
    activities: Iterable[dict[str, Any]],
    accounts: Iterable[dict[str, Any]],
    merchant_hash_key: bytes,
    *,
    eligible_reasons: frozenset[str] = AGENT_ELIGIBLE_REASONS,
) -> tuple[list[MerchantCluster], Counter[str]]:
    """Group a plan's genuinely-unresolved manual items by merchant and direction.

    One decision per ``(merchant, direction)`` covers every recurring variant of
    the same payee, so a subscription billed monthly costs one model call rather
    than twelve, and every instance is decided identically by construction.

    Splitting on direction as well as merchant is not an optimization: a payee
    that appears as both a charge and a refund is two different questions, and
    they resolve against two different taxonomies.
    """
    by_activity = {str(row.get("id") or ""): row for row in activities}
    account_types = {
        str(row.get("id") or ""): str(row.get("accountType") or "")
        for row in accounts
    }
    skipped: Counter[str] = Counter()
    grouped: dict[tuple[str, str], list[ClusterMember]] = defaultdict(list)
    payees: dict[tuple[str, str], str] = {}

    for item in plan.get("manualItems", []):
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "")
        if reason not in eligible_reasons:
            skipped[reason or "unknown-reason"] += 1
            continue
        activity_id = str(item.get("activityId") or "")
        activity = by_activity.get(activity_id)
        if activity is None:
            skipped["activity-not-in-window"] += 1
            continue
        canonical_account_id = str(item.get("canonicalAccountId") or "")
        if not canonical_account_id or not item.get("sourceId"):
            # No portable identity means no place to write a decision back to.
            skipped["no-portable-identity"] += 1
            continue
        blocked = structural_exclusion(
            activity, structural_activity_ids=set(), activity_id=activity_id
        )
        if blocked:
            skipped[f"structural:{blocked}"] += 1
            continue
        account_id = str(activity.get("accountId") or "")
        account_type = account_types.get(account_id, "")
        activity_type = str(activity.get("activityType") or "")
        bucket = cash_bucket(account_type, activity_type)
        if not bucket:
            skipped["not-a-cash-flow"] += 1
            continue
        description = str(
            activity.get("comment")
            or activity.get("notes")
            or activity.get("description")
            or ""
        )
        normalized = normalize_description(description)
        if not normalized:
            skipped["missing-description"] += 1
            continue
        digest = merchant_hash(description, merchant_hash_key)
        if digest != str(item.get("merchantHash") or ""):
            # The plan and this pass disagree about what the merchant is, which
            # means one of them is reading a different activity. Fail closed.
            skipped["merchant-digest-mismatch"] += 1
            continue
        taxonomy_id = taxonomy_for_bucket(bucket)
        key = (digest, taxonomy_id)
        payees.setdefault(key, normalized)
        identity = parse_source_identity(activity.get("idempotencyKey"))
        grouped[key].append(
            ClusterMember(
                activity_id=activity_id,
                account_id=account_id,
                canonical_account_id=canonical_account_id,
                account_type=account_type,
                date=str(item.get("date") or "")[:10],
                amount=_decimal(item.get("amount")),
                activity_type=activity_type,
                source_system=identity.source_system if identity else "",
                reason=reason,
            )
        )

    clusters = [
        MerchantCluster(
            normalized_payee=payees[key],
            merchant_hash=key[0],
            taxonomy_id=key[1],
            bucket="income" if key[1] == INCOME_TAXONOMY else "spending",
            members=sorted(members, key=lambda member: member.activity_id),
        )
        for key, members in grouped.items()
    ]
    clusters.sort(key=lambda cluster: cluster.cluster_id)
    return clusters, skipped


# -- merchant research (local only) ----------------------------------------


class MerchantResearch:
    """Interface for an optional local merchant lookup.

    A research backend receives the normalized payee and returns redacted facts
    the model may use, such as an industry label. **No implementation in this
    repository performs a network request**, and none may: the payee is the one
    piece of transaction data that is genuinely identifying, and a web lookup
    would publish it to a search provider along with the timing of the query.

    A future web-backed implementation would be opt-in, off by default, and
    would have to be documented as sending merchant names off this machine.
    """

    name = "disabled"
    enabled = False
    performs_network_requests = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "performsNetworkRequests": self.performs_network_requests,
        }

    def lookup(self, normalized_payee: str) -> dict[str, Any] | None:
        return None


class LocalFileMerchantResearch(MerchantResearch):
    """Look a merchant up in a private, operator-curated JSON file.

    The file lives under the private data directory and maps a normalized payee
    to redacted facts, e.g. ``{"acme coffee": {"industry": "coffee shop"}}``.
    Reading it is a local file read; there is no client, no socket and no cache.
    """

    name = "local-file"
    enabled = True
    performs_network_requests = False

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DecisionError(f"cannot read merchant research file: {exc}") from None
        if not isinstance(payload, dict):
            raise DecisionError("merchant research file must be a JSON object")
        self._entries = {
            normalize_description(str(key)): value
            for key, value in payload.items()
            if isinstance(value, dict)
        }

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "entryCount": len(self._entries),
            "fingerprint": plan_fingerprint(sorted(self._entries)),
        }

    def lookup(self, normalized_payee: str) -> dict[str, Any] | None:
        entry = self._entries.get(normalize_description(normalized_payee))
        if not entry:
            return None
        # Only scalar facts are forwarded; a research file cannot smuggle a
        # structure the prompt builder was not designed to render.
        return {
            str(key): str(value)
            for key, value in entry.items()
            if isinstance(value, (str, int, float, bool))
        }


# -- taxonomy ---------------------------------------------------------------


def taxonomy_options(
    catalogs: dict[str, list[dict[str, Any]]], taxonomy_id: str
) -> list[dict[str, str]]:
    """Every selectable category in one taxonomy, with its nesting path.

    The returned ids are the *only* values a decision may name. Anything else is
    an invented id and is rejected during validation.
    """
    categories = [
        row for row in catalogs.get(taxonomy_id, []) if isinstance(row, dict)
    ]
    by_id = {str(row.get("id") or ""): row for row in categories if row.get("id")}

    def path(category_id: str, seen: frozenset[str] = frozenset()) -> str:
        row = by_id.get(category_id)
        if row is None or category_id in seen:
            return ""
        name = str(row.get("name") or "")
        parent = str(row.get("parentId") or "")
        if not parent:
            return name
        prefix = path(parent, seen | {category_id})
        return f"{prefix} > {name}" if prefix else name

    options = [
        {
            "categoryId": str(row["id"]),
            "name": str(row.get("name") or ""),
            "parentId": str(row.get("parentId") or ""),
            "path": path(str(row["id"])),
        }
        for row in categories
        if row.get("id") and row.get("name")
    ]
    options.sort(key=lambda row: (row["path"], row["categoryId"]))
    return options


def taxonomy_fingerprint(options: Sequence[dict[str, str]]) -> str:
    return plan_fingerprint(list(options))


# -- tools ------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """One deterministic local evidence source, named in the sealed artifact."""

    name: str
    description: str
    run: Callable[[MerchantCluster], dict[str, Any]]


class EvidenceAssembler:
    """Run every local tool for a cluster and hash the result.

    The assembled document is merchant-redacted, so it is safe to seal into a
    plan, write to the private review, and use as a cache key. The raw payee is
    passed separately, straight into the loopback prompt, and is never part of
    anything this class returns.
    """

    def __init__(
        self,
        *,
        catalogs: dict[str, list[dict[str, Any]]],
        live_history: Any | None = None,
        canonical_history: HistoryIndex | None = None,
        research: MerchantResearch | None = None,
    ) -> None:
        self._catalogs = catalogs
        self._live_history = live_history
        self._canonical_history = canonical_history
        self._research = research or MerchantResearch()
        self._options = {
            taxonomy_id: taxonomy_options(catalogs, taxonomy_id)
            for taxonomy_id in (SPENDING_TAXONOMY, INCOME_TAXONOMY)
        }
        self.tools: tuple[Tool, ...] = (
            Tool(
                "live_taxonomy",
                "categories that exist in the live Wealthfolio taxonomy, with "
                "their parent nesting; the only ids a decision may name",
                self._live_taxonomy,
            ),
            Tool(
                "transaction_shape",
                "cash-flow direction, activity types and account kinds the "
                "merchant was seen on",
                self._transaction_shape,
            ),
            Tool(
                "recurrence",
                "how many activities the merchant covers, over what dates, in "
                "what amount band, and whether the amount repeats exactly",
                self._recurrence,
            ),
            Tool(
                "source_evidence",
                "which importers produced the rows and why the deterministic "
                "planner left them unresolved",
                self._source_evidence,
            ),
            Tool(
                "category_history",
                "what Wealthfolio's own assignments and the reviewed canonical "
                "estate already say about this exact merchant",
                self._category_history,
            ),
            Tool(
                "structural_guard",
                "structural verdict: whether any transfer, card payment, "
                "saving, investment or reconciliation rule forbids a category",
                self._structural_guard,
            ),
            Tool(
                "merchant_research",
                "optional local-only merchant facts; never a web request",
                self._merchant_research,
            ),
        )

    # -- individual tools --------------------------------------------------

    def options_for(self, taxonomy_id: str) -> list[dict[str, str]]:
        return self._options.get(taxonomy_id, [])

    def allowed_category_ids(self, taxonomy_id: str) -> set[str]:
        return {row["categoryId"] for row in self.options_for(taxonomy_id)}

    def category_vocabulary(self, taxonomy_id: str) -> frozenset[str]:
        """Every word the live taxonomy itself publishes for this direction.

        A rationale may reuse these words even when the payee also contains
        them: the plan writes ``categoryName`` next to the rationale, so
        "groceries" or "fuel" reveals nothing that is not already recorded. It
        is the *distinctive* part of a payee that must never be echoed.
        """
        words: set[str] = set()
        for row in self.options_for(taxonomy_id):
            for field in ("name", "path"):
                words.update(
                    token
                    for token in _VOCABULARY_TOKEN.findall(str(row[field]).casefold())
                    if len(token) >= 4
                )
            words.add(str(row["name"]).casefold())
        return frozenset(words)

    def _live_taxonomy(self, cluster: MerchantCluster) -> dict[str, Any]:
        options = self.options_for(cluster.taxonomy_id)
        return {
            "taxonomyId": cluster.taxonomy_id,
            "categoryCount": len(options),
            "taxonomyFingerprint": taxonomy_fingerprint(options),
        }

    def _transaction_shape(self, cluster: MerchantCluster) -> dict[str, Any]:
        return {
            "bucket": cluster.bucket,
            "direction": "credit" if cluster.bucket == "income" else "debit",
            "taxonomyId": cluster.taxonomy_id,
            "activityTypes": sorted(
                {member.activity_type for member in cluster.members}
            ),
            "accountTypes": sorted(
                {member.account_type for member in cluster.members}
            ),
            "accountCount": len({member.account_id for member in cluster.members}),
        }

    def _recurrence(self, cluster: MerchantCluster) -> dict[str, Any]:
        amounts = sorted(cluster.amounts)
        months = sorted({member.date[:7] for member in cluster.members if member.date})
        return {
            "activityCount": len(cluster.members),
            "distinctMonths": len(months),
            "firstSeen": cluster.first_seen,
            "lastSeen": cluster.last_seen,
            "minAmount": f"{amounts[0]:f}" if amounts else "0.00",
            "maxAmount": f"{amounts[-1]:f}" if amounts else "0.00",
            "identicalAmount": len(set(amounts)) == 1 and len(amounts) > 1,
        }

    def _source_evidence(self, cluster: MerchantCluster) -> dict[str, Any]:
        return {
            "sourceSystems": sorted(
                {member.source_system for member in cluster.members if member.source_system}
            ),
            "unresolvedReasons": sorted({member.reason for member in cluster.members}),
        }

    def _category_history(self, cluster: MerchantCluster) -> dict[str, Any]:
        live: dict[str, Any] = {"available": self._live_history is not None}
        if self._live_history is not None:
            reasons: set[str] = set()
            candidates: list[dict[str, Any]] = []
            for account_id in sorted({member.account_id for member in cluster.members}):
                lookup = self._live_history.lookup(
                    account_id, cluster.merchant_hash, cluster.taxonomy_id
                )
                if lookup.matched:
                    # The planner consults live history first, so a match here
                    # means the cluster should never have reached the model.
                    reasons.add("matched-outside-planner")
                    continue
                reasons.add(lookup.reason or "no-live-history")
                if lookup.candidate is not None:
                    candidates.append({
                        "categoryId": lookup.candidate.category_id,
                        "scope": lookup.candidate.scope,
                        "observationCount": lookup.candidate.evidence_count,
                    })
            live["reasons"] = sorted(reasons)
            live["belowThresholdCandidates"] = sorted(
                candidates, key=lambda row: (row["categoryId"], row["scope"])
            )
        canonical: dict[str, Any] = {"available": self._canonical_history is not None}
        if self._canonical_history is not None:
            outcomes: set[str] = set()
            observed: dict[str, int] = {}
            for account_id in sorted(
                {member.canonical_account_id for member in cluster.members}
            ):
                choice = history_choice(
                    self._canonical_history, account_id, cluster.prompt_payee
                )
                if choice is None:
                    outcomes.add("no-canonical-history")
                    continue
                kind, categories = choice
                outcomes.add(kind)
                for name, sources in categories.items():
                    # Canonical *category names* are reviewed vocabulary from the
                    # private estate, not merchant text, and the planner already
                    # failed to map them; naming them is what lets the model do
                    # better than the alias table.
                    observed[str(name)] = max(observed.get(str(name), 0), len(sources))
            canonical["outcomes"] = sorted(outcomes)
            canonical["observedCategories"] = [
                {"category": name, "observationCount": count}
                for name, count in sorted(observed.items())
            ]
        return {"live": live, "canonical": canonical}

    def _structural_guard(self, cluster: MerchantCluster) -> dict[str, Any]:
        return {
            "verdict": "categorizable",
            "checked": [
                "balance-gap-reconciliation",
                "transfer-activity-type",
                "external-reconciliation-flow",
                "structural-subtype",
                "excluded-activity",
                "structural-canonical-kind",
            ],
            "note": (
                "structural exclusions are applied before this point and are "
                "not overridable by a model decision"
            ),
        }

    def _merchant_research(self, cluster: MerchantCluster) -> dict[str, Any]:
        backend = self._research.describe()
        if not self._research.enabled:
            return {**backend, "facts": None}
        facts = self._research.lookup(cluster.prompt_payee)
        return {**backend, "facts": facts or None}

    # -- assembly ----------------------------------------------------------

    def assemble(self, cluster: MerchantCluster) -> dict[str, Any]:
        """Run every tool and return the merchant-redacted evidence document."""
        evidence = {
            "schemaVersion": 1,
            "cluster": cluster.as_document(),
            "tools": {tool.name: tool.run(cluster) for tool in self.tools},
        }
        evidence["evidenceHash"] = plan_fingerprint(evidence)
        return evidence

    def tool_manifest(self) -> list[dict[str, str]]:
        return [
            {"name": tool.name, "description": tool.description}
            for tool in self.tools
        ]
