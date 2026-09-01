"""A private, read-only index of Wealthfolio's *own* categorized history.

The canonical estate is not the only place a reviewed category decision lives.
Wealthfolio itself holds thousands of them: assignments created by its official
presets, by its categorization rules, and by a human sitting in the Spending UI
saying "this merchant is groceries". A plan that ignores those decisions asks
the operator to re-make every one of them by hand, which is exactly what a
244-activity window that auto-categorized six of them was doing.

This module reads that signal back out. It enumerates already-categorized cash
activities over a configurable lookback -- long enough by default to have seen
an annually recurring merchant twice -- and reduces them to an exact-merchant
consensus at two scopes:

* **account** -- the merchant means one thing on one Wealthfolio account.
* **global**  -- the merchant means the same thing on every account that saw it.

Three properties matter more than coverage:

1. **Nothing here is a guess.** A merchant whose category conflicts at *either*
   scope produces no consensus at all; it abstains and is reported.
2. **Nothing structural is trained on.** Transfers, card and loan payments,
   saving and investment movements, balance-gap reconciliation, excluded
   activities and Wealthfolio's synthetic "uncategorized" identity are all
   removed *before* a merchant is ever counted, so a money movement can never
   teach the index that a merchant is a purchase.
3. **No merchant text exists here at all.** A payee is normalized and then
   immediately replaced by the keyed HMAC-SHA256 digest the sealed plan already
   uses. The index is keyed by digest, so no artifact, log line or test can leak
   a payee even by accident.

The index is read-only: it issues no writes, contacts no model, and leaves no
state behind.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from importers.categorize.identity import parse_source_identity
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint
from importers.simplefin.categorization import (
    ALL_SOURCE_SYSTEMS,
    STRUCTURAL_CANONICAL_KINDS,
    TRANSFER_TYPES,
    activity_metadata,
    cash_bucket,
    in_source_scope,
    is_external_flow,
    merchant_hash,
    normalize_source_systems,
)
from importers.simplefin.pipeline import normalize_description
from importers.simplefin.spending_adapter import (
    INCOME_TAXONOMY,
    SPENDING_TAXONOMY,
    UNCATEGORIZED_CATEGORY_IDS,
)

#: Long enough that a merchant billed once a year has been seen twice. Anything
#: shorter silently drops annual insurance, tax preparation, memberships and
#: registrations -- exactly the rows a human least wants to re-decide.
DEFAULT_LOOKBACK_MONTHS = 24

#: Distinct already-categorized activities required before a consensus may be
#: applied. One observation is an anecdote; it stays visible as an abstention.
MIN_LIVE_EVIDENCE = 2

#: Confidence recorded for each scope. Deliberately a notch below the canonical
#: equivalents (0.98/0.95) so a reviewer can tell at a glance whether a decision
#: came from the reviewed canonical estate or from Wealthfolio's own history.
LIVE_ACCOUNT_CONFIDENCE = "0.97"
LIVE_GLOBAL_CONFIDENCE = "0.94"

#: How many contributing activity ids are sealed per evidence set. The evidence
#: hash covers the *whole* set, so the sample cannot be gamed; it exists to keep
#: the drift re-read at promotion time bounded.
MAX_SEALED_EVIDENCE_IDS = 64

#: Live activity subtypes that name a movement of money rather than a purchase.
#: These mirror the canonical structural kinds for installs whose activities
#: carry a subtype but no canonical row to join to.
STRUCTURAL_SUBTYPES = frozenset({
    "cc_payment",
    "card_payment",
    "credit_card_payment",
    "external_transfer",
    "internal_transfer",
    "investment",
    "loan_payment",
    "reconciliation",
    "saving",
    "transfer",
})

#: Category identities that mean "not categorized yet". Wealthfolio reports a
#: synthetic bucket for these, and training on it would teach the index that
#: every unreviewed merchant is legitimately uncategorized.
UNCATEGORIZED_TOKENS = frozenset({"uncategorized", "uncategorised", "unknown"})

_TAXONOMIES = frozenset({SPENDING_TAXONOMY, INCOME_TAXONOMY})


def taxonomy_for_bucket(bucket: str) -> str:
    """Map a cash-flow direction onto the taxonomy that may describe it."""
    return INCOME_TAXONOMY if bucket == "income" else SPENDING_TAXONOMY


def lookback_start(end_date: str, months: int) -> str:
    """The first day of the lookback window ending on ``end_date``.

    Calendar months, not 30-day blocks, and inclusive of the end month, so a
    24-month lookback ending in August 2026 starts in September 2024 and has
    therefore seen an annually recurring merchant twice.
    """
    if months < 1:
        raise DecisionError("live history lookback must be at least one month")
    try:
        anchor = date.fromisoformat(str(end_date)[:10])
    except ValueError:
        raise DecisionError("live history lookback needs an ISO end date") from None
    total = anchor.year * 12 + (anchor.month - 1) - (months - 1)
    year, month = divmod(total, 12)
    return date(year, month + 1, 1).isoformat()


def _uncategorized_identity(taxonomy_id: str, category_id: str) -> bool:
    token = str(category_id or "").strip().strip("_").casefold()
    return (
        taxonomy_id not in _TAXONOMIES
        or str(category_id or "") in UNCATEGORIZED_CATEGORY_IDS
        or not token
        or token in UNCATEGORIZED_TOKENS
    )


def _assignment_provenance(rows: Sequence[dict[str, Any]]) -> str:
    """Name how Wealthfolio says an assignment was made, without asserting it.

    The pinned build does not promise a provenance field. When one is present
    it is preserved verbatim so a reviewer can see that a consensus rests on,
    say, preset rules rather than hand review; when absent the observation is
    recorded as ``unstated`` rather than invented.
    """
    for row in rows:
        for key in ("source", "assignmentSource", "assignedBy", "origin"):
            value = str(row.get(key) or "").strip().casefold()
            if value:
                return value
    return "unstated"


@dataclass(frozen=True)
class LiveHistoryScope:
    """Exactly which slice of Wealthfolio history an index was built from."""

    lookback_months: int
    start_date: str
    end_date: str
    account_ids: tuple[str, ...]
    source_systems: tuple[str, ...]
    min_evidence: int = MIN_LIVE_EVIDENCE

    def as_document(self) -> dict[str, Any]:
        return {
            "lookbackMonths": self.lookback_months,
            "startDate": self.start_date,
            "endDate": self.end_date,
            "accountIds": list(self.account_ids),
            "sourceSystems": list(self.source_systems),
            "minEvidenceCount": self.min_evidence,
        }

    def contains(self, when: str) -> bool:
        return self.start_date <= str(when or "")[:10] <= self.end_date


def build_live_history_scope(
    *,
    end_date: str,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    account_ids: Iterable[str] = (),
    source_systems: Sequence[str] | None = None,
    min_evidence: int = MIN_LIVE_EVIDENCE,
) -> LiveHistoryScope:
    if min_evidence < 1:
        raise DecisionError("live history evidence threshold must be at least one")
    systems = normalize_source_systems(source_systems or [ALL_SOURCE_SYSTEMS])
    return LiveHistoryScope(
        lookback_months=lookback_months,
        start_date=lookback_start(end_date, lookback_months),
        end_date=str(end_date)[:10],
        account_ids=tuple(sorted({str(value) for value in account_ids if value})),
        source_systems=systems,
        min_evidence=min_evidence,
    )


@dataclass(frozen=True)
class LiveHistoryConsensus:
    """One unanimous (scope, merchant, direction) decision and its provenance."""

    scope: str
    account_id: str
    merchant_hash: str
    taxonomy_id: str
    category_id: str
    evidence_count: int
    activity_ids: tuple[str, ...]
    source_systems: tuple[str, ...]
    assignment_provenance: tuple[str, ...]
    first_seen: str
    last_seen: str

    @property
    def evidence_kind(self) -> str:
        return f"live-{self.scope}-history"

    @property
    def confidence(self) -> str:
        return (
            LIVE_ACCOUNT_CONFIDENCE
            if self.scope == "account"
            else LIVE_GLOBAL_CONFIDENCE
        )

    @property
    def evidence_hash(self) -> str:
        return plan_fingerprint({
            "scope": self.scope,
            "accountId": self.account_id,
            "merchantHash": self.merchant_hash,
            "taxonomyId": self.taxonomy_id,
            "categoryId": self.category_id,
            "evidenceCount": self.evidence_count,
            "activityIdsFingerprint": plan_fingerprint(list(self.activity_ids)),
        })

    def as_candidate_fields(self) -> dict[str, Any]:
        """The plan-candidate fields naming the evidence this decision used.

        The planner merges whatever the rescuing evidence source returns, so a
        live-history decision and a local-model decision can share one code path
        while still sealing entirely different provenance.
        """
        return {
            "liveHistoryEvidenceHash": self.evidence_hash,
            "liveHistoryScope": self.scope,
            "liveHistoryFirstSeen": self.first_seen,
            "liveHistoryLastSeen": self.last_seen,
            "liveHistorySourceSystems": list(self.source_systems),
            "liveHistoryProvenance": list(self.assignment_provenance),
        }

    def as_evidence_document(self) -> dict[str, Any]:
        """The merchant-redacted evidence row sealed into a plan."""
        return {
            "evidenceHash": self.evidence_hash,
            "scope": self.scope,
            "accountId": self.account_id,
            "merchantHash": self.merchant_hash,
            "taxonomyId": self.taxonomy_id,
            "categoryId": self.category_id,
            "evidenceCount": self.evidence_count,
            "sampleActivityIds": list(self.activity_ids[:MAX_SEALED_EVIDENCE_IDS]),
            "sourceSystems": list(self.source_systems),
            "assignmentProvenance": list(self.assignment_provenance),
            "firstSeen": self.first_seen,
            "lastSeen": self.last_seen,
        }


@dataclass(frozen=True)
class LiveHistoryLookup:
    """What the index has to say about one uncategorized activity."""

    consensus: LiveHistoryConsensus | None = None
    reason: str = ""
    candidate: LiveHistoryConsensus | None = None

    @property
    def matched(self) -> bool:
        return self.consensus is not None


@dataclass
class LiveHistoryIndex:
    """Exact-merchant consensus over Wealthfolio's existing assignments."""

    scope: LiveHistoryScope
    by_account: dict[tuple[str, str], dict[tuple[str, str], set[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )
    by_merchant: dict[str, dict[tuple[str, str], set[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )
    provenance: dict[tuple[str, str, str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    excluded_counts: Counter[str] = field(default_factory=Counter)
    observed_activity_ids: set[str] = field(default_factory=set)

    # -- construction ------------------------------------------------------

    def observe(
        self,
        *,
        activity_id: str,
        account_id: str,
        merchant_digest: str,
        taxonomy_id: str,
        category_id: str,
        when: str = "",
        source_system: str = "",
        assignment_provenance: str = "unstated",
    ) -> None:
        """Record one already-categorized activity.

        Observing the same ``activity_id`` twice is idempotent at every scope:
        evidence is counted over *distinct activities*, so an activity listed
        twice by a paginated API can never manufacture a consensus on its own.
        """
        activity_id = str(activity_id or "")
        if not activity_id or not merchant_digest or not category_id:
            return
        identity = (taxonomy_id, category_id)
        self.observed_activity_ids.add(activity_id)
        self.by_account[(account_id, merchant_digest)][identity].add(activity_id)
        self.by_merchant[merchant_digest][identity].add(activity_id)
        day = str(when or "")[:10]
        for scope_account in (account_id, ""):
            record = self.provenance.setdefault(
                (scope_account, merchant_digest, taxonomy_id, category_id),
                {
                    "activityIds": set(),
                    "sourceSystems": set(),
                    "assignmentProvenance": set(),
                    "first": "",
                    "last": "",
                },
            )
            record["activityIds"].add(activity_id)
            if source_system:
                record["sourceSystems"].add(source_system)
            if assignment_provenance:
                record["assignmentProvenance"].add(assignment_provenance)
            if day:
                record["first"] = min(record["first"] or day, day)
                record["last"] = max(record["last"], day)

    # -- consensus ---------------------------------------------------------

    def _consensus(
        self,
        scope: str,
        account_id: str,
        merchant_digest: str,
        identity: tuple[str, str],
        activity_ids: set[str],
    ) -> LiveHistoryConsensus:
        taxonomy_id, category_id = identity
        record = self.provenance.get(
            (account_id if scope == "account" else "", merchant_digest, taxonomy_id, category_id),
            {
                "sourceSystems": set(),
                "assignmentProvenance": set(),
                "first": "",
                "last": "",
            },
        )
        return LiveHistoryConsensus(
            scope=scope,
            account_id=account_id if scope == "account" else "",
            merchant_hash=merchant_digest,
            taxonomy_id=taxonomy_id,
            category_id=category_id,
            evidence_count=len(activity_ids),
            activity_ids=tuple(sorted(activity_ids)),
            source_systems=tuple(sorted(record["sourceSystems"])),
            assignment_provenance=tuple(sorted(record["assignmentProvenance"])),
            first_seen=record["first"],
            last_seen=record["last"],
        )

    def lookup(
        self,
        account_id: str,
        merchant_digest: str,
        taxonomy_id: str,
        *,
        min_evidence: int | None = None,
    ) -> LiveHistoryLookup:
        """Resolve one merchant, or name exactly why it abstained.

        Account scope outranks global scope, but a conflict at *either* scope
        abstains: a merchant that means two things somewhere in the library is
        precisely the case where a confident guess does damage.
        """
        threshold = self.scope.min_evidence if min_evidence is None else min_evidence
        merchant = self.by_merchant.get(merchant_digest) or {}
        if not merchant:
            return LiveHistoryLookup(reason="no-live-history")
        directed = {
            identity: activity_ids
            for identity, activity_ids in merchant.items()
            if identity[0] == taxonomy_id
        }
        if not directed:
            return LiveHistoryLookup(reason="live-history-direction-mismatch")
        if len(directed) > 1:
            return LiveHistoryLookup(reason="conflicting-live-history")
        account = {
            identity: activity_ids
            for identity, activity_ids in (
                self.by_account.get((account_id, merchant_digest)) or {}
            ).items()
            if identity[0] == taxonomy_id
        }
        if len(account) > 1:
            return LiveHistoryLookup(reason="conflicting-live-history")
        if account:
            identity, activity_ids = next(iter(account.items()))
            consensus = self._consensus(
                "account", account_id, merchant_digest, identity, activity_ids
            )
        else:
            identity, activity_ids = next(iter(directed.items()))
            consensus = self._consensus(
                "global", "", merchant_digest, identity, activity_ids
            )
        if consensus.evidence_count < threshold:
            return LiveHistoryLookup(
                reason="insufficient-live-history", candidate=consensus
            )
        return LiveHistoryLookup(consensus=consensus)

    # -- reporting ---------------------------------------------------------

    @property
    def merchant_count(self) -> int:
        return len(self.by_merchant)

    def conflict_count(self, taxonomy_id: str | None = None) -> int:
        """Merchants that mean two things *within one direction*.

        A merchant that is a purchase on the spending side and a payment on the
        income side is not a conflict -- it is two different questions. Only a
        disagreement inside a single taxonomy blocks a decision, so only that is
        counted here.
        """

        def conflicted(identities: dict[tuple[str, str], set[str]]) -> bool:
            taxonomies: dict[str, set[str]] = defaultdict(set)
            for taxonomy, category in identities:
                if taxonomy_id is None or taxonomy == taxonomy_id:
                    taxonomies[taxonomy].add(category)
            return any(len(categories) > 1 for categories in taxonomies.values())

        return sum(
            conflicted(identities) for identities in self.by_merchant.values()
        )

    def seal(self, used: Iterable[LiveHistoryConsensus] = ()) -> dict[str, Any]:
        """Seal the scope and the evidence a plan actually relied on.

        Only the evidence sets that produced a candidate are sealed. That keeps
        the document bounded, and it makes the drift check at promotion time
        re-read exactly the assignments the decision rested on rather than the
        whole library.
        """
        evidence: dict[str, dict[str, Any]] = {}
        for consensus in used:
            evidence.setdefault(
                consensus.evidence_hash, consensus.as_evidence_document()
            )
        document = {
            "schemaVersion": 1,
            "scope": self.scope.as_document(),
            "metrics": {
                "observedActivityCount": len(self.observed_activity_ids),
                "merchantCount": self.merchant_count,
                "conflictingMerchantCount": self.conflict_count(),
                "excludedCounts": dict(sorted(self.excluded_counts.items())),
                "sealedEvidenceCount": len(evidence),
            },
            "evidence": sorted(evidence.values(), key=lambda row: row["evidenceHash"]),
        }
        document["indexFingerprint"] = plan_fingerprint(document)
        return document


def _structural_exclusion(
    activity: dict[str, Any],
    *,
    structural_activity_ids: set[str],
    activity_id: str,
) -> str:
    """Name the structural reason this activity may not train the index."""
    if activity_id in structural_activity_ids:
        return "structural-canonical-kind"
    key = str(activity.get("idempotencyKey") or "")
    identity = parse_source_identity(key)
    if identity is None:
        return "unknown-source-identity"
    if identity.structural:
        return "reconciliation-source"
    if str(activity.get("activityType") or "") in TRANSFER_TYPES:
        return "transfer"
    if is_external_flow(activity):
        return "external-reconciliation"
    if str(activity.get("subtype") or "").strip().casefold() in STRUCTURAL_SUBTYPES:
        return "structural-subtype"
    metadata = activity_metadata(activity.get("metadata"))
    if (
        activity.get("excluded") is True
        or activity.get("isExcluded") is True
        or metadata.get("excluded") is True
    ):
        return "excluded-activity"
    if str(metadata.get("transactionKind") or "") in STRUCTURAL_CANONICAL_KINDS:
        return "structural-canonical-kind"
    return ""


#: Public, source-agnostic name for the structural filter above. Aliased rather
#: than renamed so every existing caller keeps working unchanged.
structural_exclusion = _structural_exclusion


def build_live_history_index(
    activities: Iterable[dict[str, Any]],
    assignments: dict[str, list[dict[str, Any]]],
    accounts: Iterable[dict[str, Any]],
    merchant_hash_key: bytes,
    *,
    scope: LiveHistoryScope,
    spending_account_ids: set[str] | None = None,
    structural_activity_ids: set[str] | None = None,
) -> LiveHistoryIndex:
    """Index every already-categorized activity inside ``scope``.

    Read-only and merchant-free: a payee is normalized and hashed before it is
    stored, so the returned index -- and everything derived from it -- contains
    digests only.
    """
    index = LiveHistoryIndex(scope=scope)
    by_account = {str(row.get("id") or ""): row for row in accounts}
    allowed_accounts = (
        set(by_account) if spending_account_ids is None else set(spending_account_ids)
    )
    if scope.account_ids:
        allowed_accounts &= set(scope.account_ids)
    structural_ids = set(structural_activity_ids or ())

    for activity in activities:
        activity_id = str(activity.get("id") or "")
        if not activity_id:
            index.excluded_counts["missing-activity-id"] += 1
            continue
        if not scope.contains(str(activity.get("date") or "")):
            continue
        account_id = str(activity.get("accountId") or "")
        if account_id not in allowed_accounts:
            index.excluded_counts["account-not-spending-enabled"] += 1
            continue
        structural = _structural_exclusion(
            activity,
            structural_activity_ids=structural_ids,
            activity_id=activity_id,
        )
        if structural:
            index.excluded_counts[structural] += 1
            continue
        if not in_source_scope(activity, scope.source_systems):
            index.excluded_counts["out-of-source-scope"] += 1
            continue
        rows = assignments.get(activity_id)
        if not rows:
            index.excluded_counts["uncategorized"] += 1
            continue
        assigned = [
            (str(row.get("taxonomyId") or ""), str(row.get("categoryId") or ""))
            for row in rows
            if isinstance(row, dict) and row.get("taxonomyId") in _TAXONOMIES
        ]
        if len(assigned) > 1:
            index.excluded_counts["multiple-assignments"] += 1
            continue
        if not assigned:
            index.excluded_counts["uncategorized"] += 1
            continue
        taxonomy_id, category_id = assigned[0]
        if _uncategorized_identity(taxonomy_id, category_id):
            index.excluded_counts["synthetic-uncategorized"] += 1
            continue
        account_type = str(by_account.get(account_id, {}).get("accountType") or "")
        bucket = cash_bucket(account_type, str(activity.get("activityType") or ""))
        if not bucket:
            index.excluded_counts["not-a-cash-flow"] += 1
            continue
        if taxonomy_id != taxonomy_for_bucket(bucket):
            index.excluded_counts["direction-mismatch"] += 1
            continue
        description = str(
            activity.get("comment")
            or activity.get("notes")
            or activity.get("description")
            or ""
        )
        normalized = normalize_description(description)
        if not normalized:
            index.excluded_counts["missing-description"] += 1
            continue
        identity = parse_source_identity(activity.get("idempotencyKey"))
        index.observe(
            activity_id=activity_id,
            account_id=account_id,
            merchant_digest=merchant_hash(normalized, merchant_hash_key),
            taxonomy_id=taxonomy_id,
            category_id=category_id,
            when=str(activity.get("date") or ""),
            source_system=identity.source_system if identity else "",
            assignment_provenance=_assignment_provenance(rows),
        )
    return index


def summarize_live_history(seal: dict[str, Any]) -> str:
    """A PII-free one-line summary safe to print to a terminal."""
    scope = seal["scope"]
    metrics = seal["metrics"]
    return (
        f"liveHistory lookbackMonths={scope['lookbackMonths']} "
        f"accounts={len(scope['accountIds'])} "
        f"observed={metrics['observedActivityCount']} "
        f"merchants={metrics['merchantCount']} "
        f"conflicts={metrics['conflictingMerchantCount']} "
        f"sealedEvidence={metrics['sealedEvidenceCount']}"
    )
