"""Deterministic graph-based identity resolution for transaction observations.

The resolver is deliberately pure: it never deletes evidence, reads application
state, or writes a projection.  Callers may persist the returned generation or
use it to build an application-specific projection.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import re

from .domain import (
    FinanceState,
    canonical_json,
    content_hash,
    normalize_money,
    normalized_description,
    require_hash,
    utc,
)


# v2 publishes the whole policy document beside its hash so a downstream
# projector can re-derive the hash instead of trusting it, and always emits the
# source-authority block so "no coverage declared" is provable rather than
# indistinguishable from an omitted key.
POLICY_VERSION = "canonical-identity-v5"
AUTO_CONFIDENCE_BASIS_POINTS = 10_000
REVIEW_CONFIDENCE_BASIS_POINTS = 0
CROSS_SOURCE_WEIGHTS = {
    "sameCanonicalAccount": 2500,
    "sameSignedAmountAndCurrency": 2500,
    "sameSourceDay": 1500,
    "descriptionExactOrTokenBoundaryPrefix": 2000,
    "distinctKnownSourceFamilies": 1000,
    "oneToOneComponent": 500,
}


class ConfidenceTier(str, Enum):
    HUMAN_OVERRIDE = "human-override"
    EXACT_SCOPED_IDENTITY = "exact-scoped-identity"
    EXPLICIT_LINEAGE = "explicit-lineage"
    AUTHORITATIVE_SOURCE_COVERAGE = "authoritative-source-coverage"
    UNIQUE_CROSS_SOURCE = "unique-cross-source"
    REVIEW_REQUIRED = "review-required"


class RelationKind(str, Enum):
    DUPLICATE_CANDIDATE = "duplicate-candidate"
    TRANSFER = "transfer"
    TRANSFER_CANDIDATE = "transfer-candidate"
    CORRECTION = "correction"
    REVERSAL = "reversal"
    PENDING_TRANSITION = "pending-transition"
    MIRRORED_PROVIDER_ERROR = "mirrored-provider-error"
    MIRROR_CANDIDATE = "mirror-candidate"
    SOURCE_SUPPRESSED = "source-suppressed"


class DecisionOutcome(str, Enum):
    MERGE_OBSERVATIONS = "merge-observations"
    MERGE_CLAIMS = "merge-claims"
    PRESERVE_DISTINCT = "preserve-distinct"
    LINK_TRANSFER = "link-transfer"
    LINK_CORRECTION = "link-correction"
    LINK_REVERSAL = "link-reversal"
    LINK_PENDING = "link-pending"
    SUPPRESS_MIRROR = "suppress-mirrored-provider-error"
    SOURCE_SUPPRESSED = "source-suppressed"
    UNRESOLVED = "unresolved"
    EXCLUDE_UNTRUSTED = "exclude-untrusted"


#: Residual classes a decision may fall into once the generation is complete.
RESIDUAL_CLASSES = (
    "distinct",
    "source-suppressed",
    "transfer",
    "correction",
    "reversal",
    "unresolved",
)

RESIDUAL_CLASSIFICATION: Mapping[DecisionOutcome, str] = {
    DecisionOutcome.MERGE_OBSERVATIONS: "distinct",
    DecisionOutcome.MERGE_CLAIMS: "distinct",
    DecisionOutcome.PRESERVE_DISTINCT: "distinct",
    DecisionOutcome.LINK_PENDING: "distinct",
    DecisionOutcome.SUPPRESS_MIRROR: "source-suppressed",
    DecisionOutcome.SOURCE_SUPPRESSED: "source-suppressed",
    DecisionOutcome.LINK_TRANSFER: "transfer",
    DecisionOutcome.LINK_CORRECTION: "correction",
    DecisionOutcome.LINK_REVERSAL: "reversal",
    DecisionOutcome.UNRESOLVED: "unresolved",
    # An untrusted observation is not an open question. Trust cutoffs and
    # account exclusions are explicit lineage: the observation is recorded,
    # kept out of the trusted ledger, and its event stays distinct. It was
    # never a duplicate candidate, so it must not inflate the unresolved
    # residual that gates the cutover.
    DecisionOutcome.EXCLUDE_UNTRUSTED: "distinct",
}

#: Rationale codes whose residual class is narrower than their raw outcome.
#: A transfer candidate carries `UNRESOLVED` because the *relationship* is
#: unproven, not because the identity is: both legs are real, separate economic
#: events and neither is a duplicate of the other. It belongs to the `transfer`
#: residual, and stays visible through `relationshipCandidateDecisions` and the
#: `transferCandidates` count.
RESIDUAL_CLASSIFICATION_BY_RATIONALE: Mapping[str, str] = {
    "transfer-candidate": "transfer",
}


SOURCE_AUTHORITY_POLICY_VERSION = "canonical-source-authority-v3"

#: Ordered strongest-to-weakest extraction format strengths.  A rank is only
#: assigned when the recorded evidence explicitly supports the pairing.
FORMAT_STRENGTHS = (
    "stable-provider-id",
    "posted-observation",
    "synthetic-csv",
    "legacy-export",
)

#: Default authority rank: stable OFX/QFX, then current SimpleFIN posted
#: observations, then synthetic CSV, then legacy Monarch exports.
DEFAULT_AUTHORITY_RANK = (
    ("ofx", "stable-provider-id"),
    ("qfx", "stable-provider-id"),
    ("simplefin", "posted-observation"),
    ("extract", "synthetic-csv"),
    ("monarch", "legacy-export"),
)

#: Strengths whose rank claim requires proven stable and replay-stable IDs.
STABLE_IDENTITY_STRENGTHS = ("stable-provider-id", "posted-observation")

COVERAGE_COMPLETENESS = ("complete", "partial", "unknown")

#: Version of the declared duplicate-summary admission contract.  A declared
#: mapping is *explicit operator evidence* that one source account restates
#: another source account's transactions; it is never inferred.
DUPLICATE_SUMMARY_POLICY_VERSION = "canonical-duplicate-summary-v1"

#: Observation attribute names carrying a declared duplicate-summary mapping.
DUPLICATE_SUMMARY_TARGET_ATTRIBUTE = "duplicate_summary_of_account"
DUPLICATE_SUMMARY_DECISION_ATTRIBUTE = "duplicate_summary_decision_hash"
DUPLICATE_SUMMARY_MAP_ATTRIBUTE = "duplicate_summary_map_hash"


@dataclass(frozen=True, slots=True)
class DuplicateSummaryMapping:
    """A durable operator decision that one source account restates another.

    The mapping is the *only* admissible reason to treat two observations in
    different source accounts as one economic event without a per-transaction
    lineage attribute.  It names both sides explicitly, so nothing about the
    relationship is guessed from descriptions, balances, or account names.
    """

    source_family: str
    source_account_id: str
    duplicate_of_source_account_id: str
    decision: str
    decided_at: str
    map_hash: str
    action: str = "exclude"

    def __post_init__(self) -> None:
        if not self.source_family:
            raise ValueError("duplicate summary mapping requires a source family")
        if not self.source_account_id:
            raise ValueError("duplicate summary mapping requires a source account")
        if not self.duplicate_of_source_account_id:
            raise ValueError("duplicate summary mapping requires a target account")
        if self.source_account_id == self.duplicate_of_source_account_id:
            raise ValueError("duplicate summary mapping cannot target itself")
        if not self.decision:
            raise ValueError("duplicate summary mapping requires a decision")
        if not self.decided_at:
            raise ValueError("duplicate summary mapping requires a decision date")
        require_hash(self.map_hash)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source_family.casefold(), self.source_account_id)

    @property
    def target_key(self) -> tuple[str, str]:
        return (self.source_family.casefold(), self.duplicate_of_source_account_id)

    def document(self) -> dict[str, Any]:
        return {
            "kind": "canonical-duplicate-summary-mapping",
            "version": DUPLICATE_SUMMARY_POLICY_VERSION,
            "sourceFamily": self.source_family.casefold(),
            "sourceAccountId": self.source_account_id,
            "duplicateOfSourceAccountId": self.duplicate_of_source_account_id,
            "action": self.action,
            "decision": self.decision,
            "decidedAt": self.decided_at,
            "mapHash": self.map_hash,
        }

    @property
    def decision_hash(self) -> str:
        return content_hash(self.document())


def duplicate_summary_mappings(
    document: Mapping[str, Any],
    *,
    map_hash: str,
    source_family: str = "simplefin",
    decision_kinds: Iterable[str] = ("aggregator-account-summary",),
) -> tuple[DuplicateSummaryMapping, ...]:
    """Read declared duplicate-summary mappings from an account map document.

    Only entries that explicitly declare an exclusion decision *and* name the
    account they duplicate are returned.  Any other entry — including an
    exclusion with a different decision — is ignored, so no relationship is
    ever inferred from a partially specified map.
    """

    accounts = document.get("accounts")
    if not isinstance(accounts, Mapping):
        return ()
    allowed = {str(item) for item in decision_kinds}
    seen: set[str] = set()
    result: list[DuplicateSummaryMapping] = []
    for source_account_id in sorted(str(key) for key in accounts):
        entry = accounts.get(source_account_id)
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("action") or "import") != "exclude":
            continue
        decision = str(entry.get("decision") or "")
        if decision not in allowed:
            continue
        target = str(entry.get("duplicateOfSourceAccountId") or "")
        if not target or target == source_account_id:
            continue
        if source_account_id in seen:
            raise ValueError("duplicate summary mapping declared twice")
        seen.add(source_account_id)
        result.append(
            DuplicateSummaryMapping(
                source_family=source_family,
                source_account_id=source_account_id,
                duplicate_of_source_account_id=target,
                decision=decision,
                decided_at=str(entry.get("decidedAt") or decision),
                map_hash=map_hash,
            )
        )
    targets = {item.duplicate_of_source_account_id for item in result}
    declared = {item.source_account_id for item in result}
    # A duplicate summary may never itself be the authoritative side: chained
    # or mutual declarations are refused outright rather than half-applied.
    if targets & declared:
        raise ValueError("duplicate summary mapping forms a chain")
    return tuple(result)


#: Version of the scoped shared provider-token contract.  A shared token is only
#: lineage when an operator has proven that both namespaces are the *same*
#: scoped account and import provenance.  Token equality is never global.
PROVIDER_TOKEN_SCOPE_POLICY_VERSION = "canonical-provider-token-scope-v1"

#: Observation attribute names carrying a proven scoped provider token.
PROVIDER_TOKEN_ATTRIBUTE = "provider_token"
PROVIDER_TOKEN_SCOPE_ATTRIBUTE = "provider_token_scope_hash"
PROVIDER_TOKEN_SCOPE_MAP_ATTRIBUTE = "provider_token_scope_map_hash"

#: Provider identifier kinds whose token is proven stable and replay stable.
#: A synthetic or absent identifier can never carry a shared-token claim.
SHARED_TOKEN_PROVIDER_KINDS = (
    "ofx-fitid",
    "simplefin-id",
    "scoped-provider-id",
)

#: Hard ceiling on a declared settlement skew between two writers observing the
#: same provider token.  A wider window is a coverage-authority question, not a
#: token question.
MAX_PROVIDER_TOKEN_DAY_SKEW = 3

#: Hard ceiling on a declared posting-date tolerance between an authoritative
#: source and a lower-priority one describing the same economic event.  Two
#: writers can stamp the same settlement on different days — a stable QFX
#: extract records the institution's posting date while an aggregator records
#: when it saw the row — but a wide window would start guessing, so the value an
#: operator may declare is bounded.
MAX_POSTING_DATE_TOLERANCE_DAYS = 5


@dataclass(frozen=True, slots=True)
class ProviderTokenNamespace:
    """One side of a proven shared provider-token namespace.

    ``token_prefix`` is the exact literal prefix a writer stamps in front of the
    provider token in its ``source_id``.  The token is what remains after that
    prefix, so a token containing separators is never mis-split and no family
    prefix is ever guessed.
    """

    source_family: str
    source_account_id: str
    provider_id_kind: str
    token_prefix: str

    def __post_init__(self) -> None:
        if not self.source_family:
            raise ValueError("provider token namespace requires a source family")
        if not self.source_account_id:
            raise ValueError("provider token namespace requires a source account")
        if self.provider_id_kind not in SHARED_TOKEN_PROVIDER_KINDS:
            raise ValueError("provider token namespace requires a stable id kind")
        if not self.token_prefix or not self.token_prefix.endswith(":"):
            raise ValueError("provider token namespace requires a namespace prefix")

    @property
    def key(self) -> tuple[str, str]:
        return (self.source_family.casefold(), self.source_account_id)

    def token(self, source_id: str, provider_id_kind: str) -> str:
        """Return the proven token, or an empty string when nothing is proven."""

        if provider_id_kind != self.provider_id_kind:
            return ""
        if not source_id.startswith(self.token_prefix):
            return ""
        return source_id[len(self.token_prefix) :]

    def document(self) -> dict[str, Any]:
        return {
            "sourceFamily": self.source_family.casefold(),
            "sourceAccountId": self.source_account_id,
            "providerIdKind": self.provider_id_kind,
            "tokenPrefix": self.token_prefix,
        }


@dataclass(frozen=True, slots=True)
class ProviderTokenScope:
    """A durable operator decision that two writers share one token namespace.

    The scope names one canonical account and exactly two source namespaces.  It
    is the only admissible reason to read an identical provider token in two
    source families as the same economic event; without it the resolver keeps
    the observations distinct, because provider tokens are only unique inside
    the account and import provenance that issued them.
    """

    canonical_account_id: str
    left: ProviderTokenNamespace
    right: ProviderTokenNamespace
    decision: str
    decided_at: str
    map_hash: str
    max_day_skew: int = 1

    def __post_init__(self) -> None:
        if not self.canonical_account_id:
            raise ValueError("provider token scope requires a canonical account")
        if self.left.key == self.right.key:
            raise ValueError("provider token scope requires two distinct namespaces")
        if not self.decision:
            raise ValueError("provider token scope requires a decision")
        if not self.decided_at:
            raise ValueError("provider token scope requires a decision date")
        if not 0 <= self.max_day_skew <= MAX_PROVIDER_TOKEN_DAY_SKEW:
            raise ValueError("provider token scope day skew is out of range")
        require_hash(self.map_hash)

    @property
    def sides(self) -> tuple[ProviderTokenNamespace, ProviderTokenNamespace]:
        return (self.left, self.right)

    def document(self) -> dict[str, Any]:
        ordered = sorted(
            (self.left.document(), self.right.document()),
            key=canonical_json,
        )
        return {
            "kind": "canonical-provider-token-scope",
            "version": PROVIDER_TOKEN_SCOPE_POLICY_VERSION,
            "canonicalAccountId": self.canonical_account_id,
            "namespaces": ordered,
            "decision": self.decision,
            "decidedAt": self.decided_at,
            "maxDaySkew": self.max_day_skew,
            "mapHash": self.map_hash,
        }

    @property
    def scope_hash(self) -> str:
        return content_hash(self.document())


def provider_token_scopes(
    document: Mapping[str, Any],
    *,
    map_hash: str,
    decision_kinds: Iterable[str] = ("shared-provider-token-namespace",),
) -> tuple[ProviderTokenScope, ...]:
    """Read declared provider-token scopes from a durable decision document.

    Only fully specified entries are returned.  A scope that omits either
    namespace, names a non-stable identifier kind, or carries an unrecognised
    decision is ignored rather than half-applied, so a partially written map can
    never widen identity.
    """

    entries = document.get("providerTokenScopes")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return ()
    allowed = {str(item) for item in decision_kinds}
    result: list[ProviderTokenScope] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        decision = str(entry.get("decision") or "")
        if decision not in allowed:
            continue
        namespaces = entry.get("namespaces")
        if not isinstance(namespaces, Sequence) or len(namespaces) != 2:
            continue
        parsed = []
        for side in namespaces:
            if not isinstance(side, Mapping):
                parsed = []
                break
            parsed.append(
                ProviderTokenNamespace(
                    source_family=str(side.get("sourceFamily") or ""),
                    source_account_id=str(side.get("sourceAccountId") or ""),
                    provider_id_kind=str(side.get("providerIdKind") or ""),
                    token_prefix=str(side.get("tokenPrefix") or ""),
                )
            )
        if len(parsed) != 2:
            continue
        scope = ProviderTokenScope(
            canonical_account_id=str(entry.get("canonicalAccountId") or ""),
            left=parsed[0],
            right=parsed[1],
            decision=decision,
            decided_at=str(entry.get("decidedAt") or decision),
            map_hash=map_hash,
            max_day_skew=int(entry.get("maxDaySkew", 1)),
        )
        for side in scope.sides:
            if side.key in seen:
                raise ValueError("provider token namespace declared twice")
            seen.add(side.key)
        result.append(scope)
    return tuple(sorted(result, key=lambda item: item.scope_hash))


@dataclass(frozen=True, slots=True)
class SourceCoverageEvidence:
    """Explicit, recorded proof about one source's coverage of one window.

    Nothing here is inferred.  Every field is supplied by the caller from
    extraction metadata so that an authority claim is auditable and replayable.
    """

    source_family: str
    source_connection_id: str
    source_account_id: str
    format_strength: str
    stable_id_support: bool
    replay_stable_ids: bool
    extraction_requested_from: date
    extraction_requested_through: date
    extracted_at: datetime
    freshness_as_of: datetime
    completeness: str
    source_transaction_count: int
    source_hashes: tuple[str, ...]
    trust_cutoff_day: date | None = None
    posting_date_tolerance_days: int = 0

    def __post_init__(self) -> None:
        if not self.source_family or not self.source_account_id:
            raise ValueError("coverage evidence requires a source family and account")
        if not self.source_connection_id:
            raise ValueError("coverage evidence requires a source connection")
        if self.format_strength not in FORMAT_STRENGTHS:
            raise ValueError("unsupported coverage format strength")
        if self.completeness not in COVERAGE_COMPLETENESS:
            raise ValueError("unsupported coverage completeness")
        if self.extraction_requested_from > self.extraction_requested_through:
            raise ValueError("extraction request window is inverted")
        utc(self.extracted_at)
        utc(self.freshness_as_of)
        if self.freshness_as_of < self.extracted_at:
            raise ValueError("freshness cannot precede extraction")
        if self.source_transaction_count < 0:
            raise ValueError("source transaction count cannot be negative")
        if not 0 <= self.posting_date_tolerance_days <= MAX_POSTING_DATE_TOLERANCE_DAYS:
            raise ValueError("posting date tolerance is outside the supported range")
        if self.posting_date_tolerance_days and not (
            self.stable_id_support or self.replay_stable_ids
        ):
            # A posting-date window widens what one authority may explain, so
            # the pairing across that window has to stay provably one-to-one.
            # Provider-stable identifiers prove it directly.  Deterministic
            # replay-stable extract identifiers -- a synthetic CSV whose row IDs
            # are derived from content and occurrence, so the same extract
            # always yields the same IDs in the same order -- prove the same
            # thing for replay: the occurrence multiset is fixed, so the
            # matching cannot drift between runs.  A source with neither kind of
            # identifier may never declare a window, because nothing pins which
            # row is which.
            raise ValueError(
                "a posting date tolerance requires stable or replay-stable "
                "source identifiers"
            )
        if not self.source_hashes:
            raise ValueError("coverage evidence requires source hashes")
        for value in self.source_hashes:
            require_hash(value)
        if tuple(sorted(set(self.source_hashes))) != self.source_hashes:
            raise ValueError("coverage source hashes must be sorted and unique")

    def document(self) -> dict[str, Any]:
        result = {
            "sourceFamily": self.source_family,
            "sourceConnectionHash": content_hash(self.source_connection_id),
            "sourceAccountHash": content_hash(self.source_account_id),
            "formatStrength": self.format_strength,
            "stableIdSupport": self.stable_id_support,
            "replayStableIds": self.replay_stable_ids,
            "extractionRequestedFrom": self.extraction_requested_from.isoformat(),
            "extractionRequestedThrough": (
                self.extraction_requested_through.isoformat()
            ),
            "extractedAt": utc(self.extracted_at).isoformat(),
            "freshnessAsOf": utc(self.freshness_as_of).isoformat(),
            "completeness": self.completeness,
            "sourceTransactionCount": self.source_transaction_count,
            "sourceHashes": list(self.source_hashes),
            "trustCutoffDay": (
                self.trust_cutoff_day.isoformat() if self.trust_cutoff_day else None
            ),
        }
        # Recorded only when an operator actually declared a window, so an
        # interval that expects same-day agreement hashes exactly as it did
        # before the field existed.
        if self.posting_date_tolerance_days:
            result["postingDateToleranceDays"] = self.posting_date_tolerance_days
        return result

    @property
    def evidence_hash(self) -> str:
        return content_hash(self.document())

    def matches(self, observation: "IdentityObservation") -> bool:
        return (
            observation.source_family == self.source_family
            and observation.source_connection_id == self.source_connection_id
            and observation.source_account_id == self.source_account_id
        )

    @property
    def source_scope(self) -> tuple[str, str, str]:
        return (
            self.source_family,
            self.source_connection_id,
            self.source_account_id,
        )


@dataclass(frozen=True, slots=True)
class AuthorityInterval:
    """One canonical account and one closed effective date interval."""

    canonical_account_id: str
    effective_from: date
    effective_through: date
    evidence: SourceCoverageEvidence

    def __post_init__(self) -> None:
        if not self.canonical_account_id:
            raise ValueError("authority interval requires a canonical account")
        if self.effective_from > self.effective_through:
            raise ValueError("authority interval is inverted")
        evidence = self.evidence
        if (
            self.effective_from < evidence.extraction_requested_from
            or self.effective_through > evidence.extraction_requested_through
        ):
            raise ValueError(
                "authority interval must fall inside the extraction request window"
            )
        if (
            evidence.trust_cutoff_day is not None
            and self.effective_through > evidence.trust_cutoff_day
        ):
            raise ValueError("authority interval extends past its trust cutoff")

    def document(self) -> dict[str, Any]:
        return {
            "kind": "canonical-source-authority-interval",
            "canonicalAccountHash": content_hash(self.canonical_account_id),
            "effectiveFrom": self.effective_from.isoformat(),
            "effectiveThrough": self.effective_through.isoformat(),
            "evidence": self.evidence.document(),
        }

    @property
    def interval_id(self) -> str:
        return content_hash(self.document())

    def covers(self, canonical_account_id: str, day: date) -> bool:
        return (
            canonical_account_id == self.canonical_account_id
            and self.effective_from <= day <= self.effective_through
        )

    def matches(self, observation: "IdentityObservation") -> bool:
        return self.covers(
            observation.canonical_account_id, observation.source_day
        ) and self.evidence.matches(observation)

    def overlaps(self, other: "AuthorityInterval") -> bool:
        return (
            self.canonical_account_id == other.canonical_account_id
            and self.effective_from <= other.effective_through
            and other.effective_from <= self.effective_through
        )

    def settlement_days(self) -> int:
        return (self.evidence.freshness_as_of.date() - self.effective_through).days

    @property
    def posting_date_tolerance_days(self) -> int:
        return self.evidence.posting_date_tolerance_days

    def proves_both_days(self, first: date, second: date) -> bool:
        """Whether this interval's proven coverage spans both source days.

        A posting-date window may only be used where *both* the authoritative
        and the lower observation fall inside proven coverage.  Reaching one day
        outside the interval would suppress an observation the authority never
        claimed to cover.
        """

        return (
            self.effective_from <= first <= self.effective_through
            and self.effective_from <= second <= self.effective_through
        )


@dataclass(frozen=True, slots=True)
class SourceAuthorityPolicy:
    """Versioned policy that decides when coverage evidence proves authority."""

    version: str = SOURCE_AUTHORITY_POLICY_VERSION
    default_rank: tuple[tuple[str, str], ...] = DEFAULT_AUTHORITY_RANK
    format_strengths: tuple[str, ...] = FORMAT_STRENGTHS
    stable_identity_strengths: tuple[str, ...] = STABLE_IDENTITY_STRENGTHS
    required_completeness: str = "complete"
    settlement_days: int = 3
    minimum_source_transaction_count: int = 1
    require_posted_status: bool = True
    require_trusted_observations: bool = True
    require_distinct_source_scope: bool = True
    require_reconciled_source_counts: bool = True
    allow_lower_multiplicity_excess: bool = False
    count_based_occurrence_pairing: bool = True

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("source authority policy version is required")
        if self.required_completeness not in COVERAGE_COMPLETENESS:
            raise ValueError("unsupported required completeness")
        if self.settlement_days < 0:
            raise ValueError("settlement days cannot be negative")
        if self.minimum_source_transaction_count < 1:
            raise ValueError("minimum source transaction count must be positive")
        if len(set(self.default_rank)) != len(self.default_rank):
            raise ValueError("authority rank cannot contain duplicates")
        for family, strength in self.default_rank:
            if not family or strength not in self.format_strengths:
                raise ValueError("authority rank entry is not evidence-supported")
        if self.allow_lower_multiplicity_excess:
            raise ValueError(
                "suppressing more low-source occurrences than the authoritative "
                "source proves is never permitted"
            )

    def rank(self, source_family: str, format_strength: str) -> int | None:
        if (source_family, format_strength) not in self.default_rank:
            return None
        return len(self.format_strengths) - self.format_strengths.index(
            format_strength
        )

    def document(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "defaultRank": [list(item) for item in self.default_rank],
            "formatStrengths": list(self.format_strengths),
            "stableIdentityStrengths": list(self.stable_identity_strengths),
            "requiredCompleteness": self.required_completeness,
            "settlementDays": self.settlement_days,
            "minimumSourceTransactionCount": (
                self.minimum_source_transaction_count
            ),
            "requirePostedStatus": self.require_posted_status,
            "requireTrustedObservations": self.require_trusted_observations,
            "requireDistinctSourceScope": self.require_distinct_source_scope,
            "requireReconciledSourceCounts": (
                self.require_reconciled_source_counts
            ),
            "allowLowerMultiplicityExcess": self.allow_lower_multiplicity_excess,
            "countBasedOccurrencePairing": self.count_based_occurrence_pairing,
            "neverSuppress": [
                "opposite-sign-or-cross-account-transfers",
                "same-source-different-provider-ids",
                "outside-overlapping-proven-coverage",
                "non-posted-entries",
                "conflicting-currency-or-amount",
                "ambiguous-lower-source-occurrences",
            ],
            "categoryParticipates": False,
            "descriptionParticipates": (
                (
                    "exact-or-shared-discriminating-token"
                    if self.version == "canonical-source-authority-v2"
                    else "exact-only"
                )
                if self.count_based_occurrence_pairing
                else "required"
            ),
        }

    @property
    def policy_hash(self) -> str:
        return content_hash(self.document())


DEFAULT_SOURCE_AUTHORITY_POLICY = SourceAuthorityPolicy()


@dataclass(frozen=True, slots=True)
class IntervalAuthority:
    """The policy verdict for one interval, with its complete proof."""

    interval: AuthorityInterval
    proven: bool
    rank: int | None
    observed_source_transactions: int
    reconciled: bool
    proof: tuple[tuple[str, str], ...]

    @property
    def authoritative(self) -> bool:
        return self.proven and self.reconciled and self.rank is not None

    @property
    def usable(self) -> bool:
        return self.proven and self.reconciled


@dataclass(frozen=True, slots=True)
class SourceCoverageAuthority:
    """A versioned, deterministic set of per-account coverage intervals."""

    policy: SourceAuthorityPolicy = DEFAULT_SOURCE_AUTHORITY_POLICY
    intervals: tuple[AuthorityInterval, ...] = ()

    def __post_init__(self) -> None:
        identifiers = [item.interval_id for item in self.intervals]
        if sorted(identifiers) != identifiers:
            raise ValueError("authority intervals must be sorted by interval ID")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("authority intervals must be unique")
        for left, right in combinations(self.intervals, 2):
            if (
                left.evidence.source_scope == right.evidence.source_scope
                and left.overlaps(right)
            ):
                raise ValueError(
                    "one source cannot claim two overlapping intervals for an account"
                )

    def document(self) -> dict[str, Any]:
        return {
            "policy": self.policy.document(),
            "policyHash": self.policy.policy_hash,
            "intervals": [item.document() for item in self.intervals],
        }

    @property
    def authority_hash(self) -> str:
        return content_hash(self.document())

    def covering(
        self, observation: "IdentityObservation"
    ) -> tuple[AuthorityInterval, ...]:
        return tuple(item for item in self.intervals if item.matches(observation))


EMPTY_SOURCE_AUTHORITY = SourceCoverageAuthority()


def _coerce_date(value: Any, name: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{name} must be a calendar date, not a timestamp")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return date.fromisoformat(value)
    raise ValueError(f"{name} is required and must be an explicit calendar date")


def _coerce_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, str) and value:
        return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    raise ValueError(f"{name} is required and must be an explicit UTC timestamp")


def _required(record: Mapping[str, Any], name: str) -> Any:
    if name not in record:
        raise ValueError(
            f"authority evidence must state {name} explicitly; "
            "coverage windows are never inferred from observed data"
        )
    return record[name]


# ---------------------------------------------------------------------------
# OFX/QFX statement windows
# ---------------------------------------------------------------------------

# OFX is SGML, not XML: closing tags are optional, so values are read up to the
# next tag or newline.  The transaction-list block is the only place a statement
# window is declared; a DTSTART elsewhere in the document belongs to a different
# scope and must never be mistaken for one.
_OFX_TRANSACTION_LIST = re.compile(
    r"<(BANK|CC|INV)TRANLIST>(?P<body>.*?)</\1TRANLIST>",
    re.IGNORECASE | re.DOTALL,
)
_OFX_STATEMENT = re.compile(
    r"<(BANK|CC|INV)TRANLIST>|<ACCTID>(?P<acctid>[^<\r\n]*)",
    re.IGNORECASE,
)
OFX_STATEMENT_FAMILIES = ("ofx", "qfx")


def _ofx_value(block: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>([^<\r\n]*)", block, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).strip()


def _ofx_date(raw: str | None, name: str) -> date:
    """An OFX timestamp is ``YYYYMMDD`` with an optional time and zone suffix."""

    if not raw:
        raise ValueError(f"ofx statement window is missing {name}")
    digits = raw.strip()[:8]
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"ofx statement window has an unreadable {name}")
    try:
        return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError as exc:
        raise ValueError(f"ofx statement window has an invalid {name}") from exc


@dataclass(frozen=True, slots=True)
class OfxStatementWindow:
    """The statement period an OFX/QFX export declares for one account.

    This is the *file's own* claim about what it covers, read from the
    transaction list's ``DTSTART`` and ``DTEND``.  It is never derived from the
    transactions the file happens to contain: an observed range is evidence of
    what arrived, not proof of what was requested, and a stable export that
    legitimately contains no activity in a month would otherwise silently lose
    coverage over it.
    """

    statement_start: date
    statement_end: date
    account_id: str | None = None

    def __post_init__(self) -> None:
        if self.statement_end < self.statement_start:
            # Observed in the wild: some institutions emit an end date before
            # the start date.  Refuse it rather than repairing it, because a
            # repaired window is a guess about what the institution meant.
            raise ValueError("ofx statement window ends before it starts")

    def covers(self, day: date) -> bool:
        return self.statement_start <= day <= self.statement_end

    def document(self) -> dict[str, Any]:
        return {
            "statementStart": self.statement_start.isoformat(),
            "statementEnd": self.statement_end.isoformat(),
            "accountId": self.account_id,
        }


def read_ofx_statement_windows(text: str) -> tuple[OfxStatementWindow, ...]:
    """Every statement window an OFX/QFX document declares, in file order.

    Raises when a window is present but unusable.  A document with no
    transaction list at all yields an empty tuple, which callers must treat as
    "no declared coverage" rather than as unbounded coverage.
    """

    account_ids: list[str | None] = []
    pending: str | None = None
    for match in _OFX_STATEMENT.finditer(text):
        if match.group("acctid") is not None:
            pending = match.group("acctid").strip() or None
        else:
            account_ids.append(pending)
    windows: list[OfxStatementWindow] = []
    for index, match in enumerate(_OFX_TRANSACTION_LIST.finditer(text)):
        body = match.group("body")
        windows.append(
            OfxStatementWindow(
                statement_start=_ofx_date(_ofx_value(body, "DTSTART"), "DTSTART"),
                statement_end=_ofx_date(_ofx_value(body, "DTEND"), "DTEND"),
                account_id=(
                    account_ids[index] if index < len(account_ids) else None
                ),
            )
        )
    return tuple(windows)


def read_ofx_statement_window(
    text: str, *, account_id: str | None = None
) -> OfxStatementWindow:
    """The one statement window an OFX/QFX document declares for an account.

    ``account_id`` selects among multi-statement documents by exact ``ACCTID``
    or, because institutions mask account numbers differently across their own
    products, by a shared trailing four digits.  Ambiguity is refused, never
    resolved by picking the first or the widest match.
    """

    windows = read_ofx_statement_windows(text)
    if not windows:
        raise ValueError("ofx document declares no statement window")
    if account_id:
        wanted = str(account_id).strip()
        tail = wanted[-4:] if len(wanted) >= 4 else ""
        matched = [
            window
            for window in windows
            if window.account_id
            and (
                window.account_id == wanted
                or (tail and window.account_id.endswith(tail))
            )
        ]
        if not matched:
            raise ValueError("ofx document declares no window for that account")
        if len(matched) > 1:
            raise ValueError("ofx document declares an ambiguous account window")
        return matched[0]
    if len(windows) > 1:
        raise ValueError("ofx document declares more than one statement window")
    return windows[0]


def _statement_window_bounds(
    record: Mapping[str, Any], declared: Any
) -> tuple[date, date, str]:
    """Resolve a record's coverage window from a bound OFX statement window."""

    if isinstance(declared, OfxStatementWindow):
        window = declared
        source_sha256 = str(_required(record, "ofx_statement_sha256"))
    elif isinstance(declared, Mapping):
        source_sha256 = str(_required(declared, "source_sha256"))
        window = OfxStatementWindow(
            statement_start=_coerce_date(
                _required(declared, "statement_start"), "statement_start"
            ),
            statement_end=_coerce_date(
                _required(declared, "statement_end"), "statement_end"
            ),
            account_id=(
                str(declared["account_id"]) if declared.get("account_id") else None
            ),
        )
    else:
        raise ValueError("ofx_statement_window must be a mapping or a parsed window")

    family = str(record.get("source_family") or "").lower()
    if family not in OFX_STATEMENT_FAMILIES:
        raise ValueError(
            "an ofx statement window may only bind an ofx or qfx interval"
        )
    hashes = {str(item) for item in _required(record, "source_hashes")}
    if source_sha256 not in hashes:
        raise ValueError(
            "an ofx statement window must bind a declared source hash"
        )
    for name, value in (
        ("effective_from", window.statement_start),
        ("effective_through", window.statement_end),
    ):
        if record.get(name) is None:
            continue
        if _coerce_date(record[name], name) != value:
            raise ValueError(
                f"{name} conflicts with the bound ofx statement window"
            )
    return window.statement_start, window.statement_end, source_sha256


def build_source_authority(
    records: Iterable[Mapping[str, Any]],
    *,
    policy: SourceAuthorityPolicy = DEFAULT_SOURCE_AUTHORITY_POLICY,
) -> SourceCoverageAuthority:
    """Build coverage authority from explicit evidence metadata.

    Every window boundary must be stated by the caller.  This builder never
    derives an interval from the minimum and maximum dates it happens to see in
    the observation set, because an observed range is not proof of coverage.

    An OFX/QFX record may instead bind ``ofx_statement_window``, in which case
    the boundaries are read from the export's own ``DTSTART`` / ``DTEND`` and
    the window must name a hash the record already declares.  That is still an
    explicit statement — the institution's, not ours — and it is still never
    transaction extrema.
    """

    intervals = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("authority evidence records must be mappings")
        evidence = SourceCoverageEvidence(
            source_family=str(_required(record, "source_family")),
            source_connection_id=str(_required(record, "source_connection_id")),
            source_account_id=str(_required(record, "source_account_id")),
            format_strength=str(_required(record, "format_strength")),
            stable_id_support=bool(_required(record, "stable_id_support")),
            replay_stable_ids=bool(_required(record, "replay_stable_ids")),
            extraction_requested_from=_coerce_date(
                _required(record, "extraction_requested_from"),
                "extraction_requested_from",
            ),
            extraction_requested_through=_coerce_date(
                _required(record, "extraction_requested_through"),
                "extraction_requested_through",
            ),
            extracted_at=_coerce_datetime(
                _required(record, "extracted_at"), "extracted_at"
            ),
            freshness_as_of=_coerce_datetime(
                _required(record, "freshness_as_of"), "freshness_as_of"
            ),
            completeness=str(_required(record, "completeness")),
            source_transaction_count=int(
                _required(record, "source_transaction_count")
            ),
            source_hashes=tuple(
                sorted(set(_required(record, "source_hashes")))
            ),
            trust_cutoff_day=(
                _coerce_date(record["trust_cutoff_day"], "trust_cutoff_day")
                if record.get("trust_cutoff_day")
                else None
            ),
            posting_date_tolerance_days=int(
                record.get("posting_date_tolerance_days") or 0
            ),
        )
        declared_window = record.get("ofx_statement_window")
        if declared_window is not None:
            effective_from, effective_through, _bound_hash = (
                _statement_window_bounds(record, declared_window)
            )
        else:
            effective_from = _coerce_date(
                _required(record, "effective_from"), "effective_from"
            )
            effective_through = _coerce_date(
                _required(record, "effective_through"), "effective_through"
            )
        intervals.append(
            AuthorityInterval(
                canonical_account_id=str(_required(record, "canonical_account_id")),
                effective_from=effective_from,
                effective_through=effective_through,
                evidence=evidence,
            )
        )
    ordered = tuple(sorted(intervals, key=lambda item: item.interval_id))
    return SourceCoverageAuthority(policy=policy, intervals=ordered)



@dataclass(frozen=True, slots=True)
class IdentityPolicy:
    version: str = POLICY_VERSION
    duplicate_candidate_window_days: int = 3
    transfer_window_days: int = 5
    prefix_minimum_characters: int = 4
    source_precedence: tuple[str, ...] = (
        "manual",
        "receipt",
        "ofx",
        "qfx",
        "monarch",
        "simplefin",
        "extract",
        "canonical",
        "wealthfolio",
        "unknown",
    )
    category_precedence: tuple[str, ...] = (
        "manual",
        "receipt",
        "monarch",
        "simplefin",
        "canonical",
        "ofx",
        "qfx",
        "extract",
        "wealthfolio",
        "unknown",
    )
    automatic_source_families: tuple[str, ...] = (
        "canonical",
        "extract",
        "monarch",
        "ofx",
        "qfx",
        "simplefin",
    )
    source_authority: SourceCoverageAuthority = field(
        default=EMPTY_SOURCE_AUTHORITY
    )

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("identity policy version is required")
        if self.duplicate_candidate_window_days < 0:
            raise ValueError("duplicate candidate window cannot be negative")
        if self.transfer_window_days < 0:
            raise ValueError("transfer window cannot be negative")
        if self.prefix_minimum_characters < 1:
            raise ValueError("prefix minimum must be positive")
        if len(set(self.source_precedence)) != len(self.source_precedence):
            raise ValueError("source precedence cannot contain duplicates")
        if len(set(self.category_precedence)) != len(self.category_precedence):
            raise ValueError("category precedence cannot contain duplicates")

    @property
    def authority_policy(self) -> SourceAuthorityPolicy:
        return self.source_authority.policy

    def document(self) -> dict[str, Any]:
        document = {
            "version": self.version,
            "scoreBasisPoints": CROSS_SOURCE_WEIGHTS,
            "automaticThresholdBasisPoints": AUTO_CONFIDENCE_BASIS_POINTS,
            "rules": {
                "exactScopedIdentity": {
                    "scope": [
                        "sourceFamily",
                        "sourceConnection",
                        "sourceAccount",
                        "providerIdKind",
                        "providerTransactionId",
                    ],
                    "requiresProviderIdentity": True,
                },
                "uniqueCrossSource": {
                    "candidateDateWindowDays": (self.duplicate_candidate_window_days),
                    "automaticDateWindowDays": 0,
                    "oneToOneDegrees": [1, 1],
                    "componentSize": 2,
                    "automatic": False,
                    "requiresSourceAuthorityOrScopedProviderIdentity": True,
                    "categoryParticipates": False,
                    "writerTimestampParticipates": False,
                },
                "automaticComponents": {
                    "preserveDistinctIsTransitive": True,
                    "oneOccurrencePerSourceFamilyAndCanonicalAccount": True,
                    "matcherPrecedence": [
                        "explicit-lineage",
                        "authoritative-source-coverage",
                        "unique-cross-source",
                    ],
                    "conflictsRequireReview": True,
                },
                "transfer": {
                    "differentAccounts": True,
                    "oppositeSigns": True,
                    "windowDays": self.transfer_window_days,
                    "neverDuplicateMerge": True,
                },
                "trustCutoff": {
                    "preserveObservation": True,
                    "excludeFromCanonicalProjection": True,
                },
            },
            "sourcePrecedence": list(self.source_precedence),
            "categoryPrecedence": list(self.category_precedence),
            "automaticSourceFamilies": list(self.automatic_source_families),
            "prefixMinimumCharacters": self.prefix_minimum_characters,
            # Always emitted.  An absent key and an empty authority used to hash
            # alike, so a reader could not tell "declared no coverage" from
            # "published by an older writer that could not declare any".
            "sourceAuthority": self.source_authority.document(),
        }
        return document

    @property
    def policy_hash(self) -> str:
        return content_hash(self.document())


DEFAULT_POLICY = IdentityPolicy()


@dataclass(frozen=True, slots=True)
class IdentityObservation:
    observation_id: str
    source_family: str
    source_connection_id: str
    source_account_id: str
    canonical_account_id: str
    provider_transaction_id: str | None
    provider_id_kind: str
    source_hash: str
    source_day: date
    observed_at: datetime
    signed_amount: Decimal
    currency: str
    description: str
    status: str = "posted"
    category: str = ""
    source_group_id: str = ""
    import_lineage_hash: str | None = None
    trust_cutoff_day: date | None = None
    account_status: str = "active"
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.observation_id:
            raise ValueError("observation_id is required")
        if not self.source_family or not self.source_account_id:
            raise ValueError("source family and source account are required")
        if not self.canonical_account_id:
            raise ValueError("canonical account is required")
        if self.provider_id_kind not in {
            "none",
            "synthetic",
            "scoped-provider-id",
            "simplefin-id",
            "ofx-fitid",
        }:
            raise ValueError("unsupported provider identity kind")
        if (
            self.provider_id_kind not in {"none", "synthetic"}
            and not self.provider_transaction_id
        ):
            raise ValueError("provider identity kind requires a provider ID")
        require_hash(self.source_hash)
        if self.import_lineage_hash is not None:
            require_hash(self.import_lineage_hash)
        utc(self.observed_at)
        if self.signed_amount != normalize_money(self.signed_amount):
            raise ValueError("signed amount must be normalized")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("currency must be a three-letter uppercase code")
        if self.status not in {"pending", "posted", "reversed"}:
            raise ValueError("unsupported identity observation status")
        if self.account_status not in {"active", "closed", "excluded", "unknown"}:
            raise ValueError("unsupported account lifecycle status")
        if tuple(sorted(self.attributes)) != self.attributes:
            raise ValueError("attributes must be sorted")

    @property
    def trusted(self) -> bool:
        return self.account_status != "excluded" and (
            self.trust_cutoff_day is None or self.source_day <= self.trust_cutoff_day
        )

    def attribute(self, name: str) -> str:
        return dict(self.attributes).get(name, "")


@dataclass(frozen=True, slots=True)
class SourceClaim:
    claim_id: str
    source_family: str
    canonical_account_hash: str
    source_account_hash: str
    source_connection_hash: str
    provider_identity_hash: str | None
    provider_id_kind: str
    observation_ids: tuple[str, ...]
    selected_observation_id: str
    source_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IdentityEdge:
    edge_hash: str
    kind: RelationKind
    left_claim_id: str
    right_claim_id: str
    feature_vector: tuple[tuple[str, str], ...]
    confidence_basis_points: int
    automatic: bool
    competing_candidate_proof: tuple[tuple[str, int], ...]
    source_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    canonical_event_id: str
    canonical_account_hash: str
    selected_observation_id: str
    member_claim_ids: tuple[str, ...]
    member_observation_ids: tuple[str, ...]
    source_day: date
    signed_amount: Decimal
    currency: str
    description: str
    status: str
    category: str
    source_group_id: str
    trusted: bool


@dataclass(frozen=True, slots=True)
class EventRelationship:
    relationship_hash: str
    kind: RelationKind
    left_canonical_event_id: str
    right_canonical_event_id: str
    source_edge_hash: str


@dataclass(frozen=True, slots=True)
class HumanOverride:
    override_id: str
    version: int
    action: str
    claim_ids: tuple[str, ...]
    rationale_hash: str
    decided_at: datetime
    supersedes_override_id: str | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("override version must be positive")
        if self.action not in {"merge", "preserve-distinct", "transfer"}:
            raise ValueError("unsupported identity override action")
        if len(set(self.claim_ids)) != len(self.claim_ids) or len(self.claim_ids) < 2:
            raise ValueError("override must identify at least two unique claims")
        require_hash(self.rationale_hash)
        utc(self.decided_at)


@dataclass(frozen=True, slots=True)
class IdentityDecision:
    decision_hash: str
    generation_hash: str
    policy_version: str
    policy_hash: str
    outcome: DecisionOutcome
    confidence_tier: ConfidenceTier
    confidence_basis_points: int
    rationale_code: str
    claim_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    canonical_event_ids: tuple[str, ...]
    feature_vector: tuple[tuple[str, str], ...]
    competing_candidate_proof: tuple[tuple[str, int], ...]
    source_hashes: tuple[str, ...]
    human_override_id: str | None = None

    @property
    def residual_classification(self) -> str:
        override = RESIDUAL_CLASSIFICATION_BY_RATIONALE.get(self.rationale_code)
        if override is not None:
            return override
        return RESIDUAL_CLASSIFICATION[self.outcome]

    @property
    def source_authority_policy_hash(self) -> str | None:
        return dict(self.feature_vector).get("authorityPolicyHash")


@dataclass(frozen=True, slots=True)
class ApplicationProjection:
    projection_id: str
    target_system: str
    canonical_event_id: str
    projected_hash: str
    active: bool


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    policy: IdentityPolicy
    input_hash: str
    generation_hash: str
    canonical_state_hash: str
    observations: tuple[IdentityObservation, ...]
    claims: tuple[SourceClaim, ...]
    edges: tuple[IdentityEdge, ...]
    canonical_events: tuple[CanonicalEvent, ...]
    relationships: tuple[EventRelationship, ...]
    decisions: tuple[IdentityDecision, ...]
    overrides: tuple[HumanOverride, ...] = ()
    interval_authorities: tuple[IntervalAuthority, ...] = ()
    token_scopes: tuple[ProviderTokenScope, ...] = ()

    @property
    def observation_to_canonical(self) -> dict[str, str]:
        return {
            observation_id: event.canonical_event_id
            for event in self.canonical_events
            for observation_id in event.member_observation_ids
        }

    def provider_token_scope_document(self) -> dict[str, Any]:
        """Safe aggregate scoped-token report: counts and hashes only.

        A provider token is source data, so nothing here exposes one.  The
        report answers how many scopes an operator declared, how many links each
        one actually settled, and how many buckets stayed ambiguous.
        """

        settled: dict[str, int] = {scope.scope_hash: 0 for scope in self.token_scopes}
        for decision in self.decisions:
            features = dict(decision.feature_vector)
            if features.get("scopedSharedProviderToken") != "true":
                continue
            scope_hash = features.get("providerTokenScopeHash", "")
            if scope_hash in settled:
                settled[scope_hash] += 1
        ambiguous = sum(
            1
            for decision in self.decisions
            if decision.rationale_code.startswith("ambiguous-shared-token-")
        )
        return {
            "declared": bool(self.token_scopes),
            "policyVersion": PROVIDER_TOKEN_SCOPE_POLICY_VERSION,
            "scopeCount": len(self.token_scopes),
            "linkedDecisionCount": sum(settled.values()),
            "ambiguousGroupCount": ambiguous,
            "scopes": [
                {
                    "scopeHash": scope.scope_hash,
                    "mapHash": scope.map_hash,
                    "canonicalAccountHash": content_hash(scope.canonical_account_id),
                    "leftNamespaceHash": content_hash(scope.left.key),
                    "rightNamespaceHash": content_hash(scope.right.key),
                    "leftProviderIdKind": scope.left.provider_id_kind,
                    "rightProviderIdKind": scope.right.provider_id_kind,
                    "maxDaySkew": scope.max_day_skew,
                    "decision": scope.decision,
                    "decidedAt": scope.decided_at,
                    "linkedDecisionCount": settled[scope.scope_hash],
                }
                for scope in self.token_scopes
            ],
        }

    def source_authority_document(self) -> dict[str, Any]:
        """Safe aggregate coverage report: counts, intervals, hashed accounts."""

        authority_policy = self.policy.authority_policy
        return {
            "declared": bool(self.policy.source_authority.intervals),
            "policyVersion": authority_policy.version,
            "policyHash": authority_policy.policy_hash,
            "authorityHash": self.policy.source_authority.authority_hash,
            "intervalCount": len(self.interval_authorities),
            "earliestEffectiveFrom": min(
                (
                    item.interval.effective_from.isoformat()
                    for item in self.interval_authorities
                ),
                default=None,
            ),
            "latestEffectiveThrough": max(
                (
                    item.interval.effective_through.isoformat()
                    for item in self.interval_authorities
                ),
                default=None,
            ),
            "provenIntervalCount": sum(
                item.proven for item in self.interval_authorities
            ),
            "reconciledIntervalCount": sum(
                item.reconciled for item in self.interval_authorities
            ),
            "authoritativeIntervalCount": sum(
                item.authoritative for item in self.interval_authorities
            ),
            "intervals": [
                {
                    "intervalId": item.interval.interval_id,
                    "canonicalAccountHash": content_hash(
                        item.interval.canonical_account_id
                    ),
                    "sourceFamily": item.interval.evidence.source_family,
                    "formatStrength": item.interval.evidence.format_strength,
                    "effectiveFrom": item.interval.effective_from.isoformat(),
                    "effectiveThrough": item.interval.effective_through.isoformat(),
                    "rank": item.rank,
                    "proven": item.proven,
                    "reconciled": item.reconciled,
                    "authoritative": item.authoritative,
                    "completeness": item.interval.evidence.completeness,
                    "stableIdSupport": item.interval.evidence.stable_id_support,
                    "replayStableIds": item.interval.evidence.replay_stable_ids,
                    "declaredSourceTransactionCount": (
                        item.interval.evidence.source_transaction_count
                    ),
                    "observedSourceTransactionCount": (
                        item.observed_source_transactions
                    ),
                    "settlementDays": item.interval.settlement_days(),
                    "postingDateToleranceDays": (
                        item.interval.posting_date_tolerance_days
                    ),
                    "evidenceHash": item.interval.evidence.evidence_hash,
                    "authorityProof": dict(item.proof),
                }
                for item in self.interval_authorities
            ],
        }

    def report_document(self) -> dict[str, Any]:
        automatic_decisions = [
            decision
            for decision in self.decisions
            if decision.confidence_tier
            not in {
                ConfidenceTier.REVIEW_REQUIRED,
                ConfidenceTier.HUMAN_OVERRIDE,
            }
        ]
        automatic = [
            decision
            for decision in automatic_decisions
            if decision.outcome
            in {
                DecisionOutcome.MERGE_CLAIMS,
                DecisionOutcome.MERGE_OBSERVATIONS,
                DecisionOutcome.SUPPRESS_MIRROR,
                DecisionOutcome.SOURCE_SUPPRESSED,
            }
        ]
        # `unresolvedDecisions` is built from the residual classification, not
        # the raw outcome: a decision is only an open duplicate question when
        # nothing has classified it. Transfer candidates and untrusted
        # exclusions are decided outcomes that happen to carry a non-merging
        # outcome value.
        unresolved = [
            decision
            for decision in self.decisions
            if decision.residual_classification == "unresolved"
        ]
        # Graph observations that need a human eye but are not duplicate
        # questions. They keep their own audit surface so reclassifying them out
        # of `unresolvedDecisions` never makes them invisible.
        relationship_candidates = [
            decision
            for decision in self.decisions
            if decision.confidence_tier is ConfidenceTier.REVIEW_REQUIRED
            and decision.residual_classification != "unresolved"
        ]
        by_class = Counter(decision.rationale_code for decision in automatic)
        by_confidence = Counter(
            decision.confidence_tier.value for decision in automatic
        )
        by_pair = Counter(
            dict(decision.feature_vector).get("sourceFamilyPair", "single-source")
            for decision in automatic
        )
        residual = Counter(
            decision.residual_classification for decision in self.decisions
        )

        def decision_document(decision: IdentityDecision) -> dict[str, Any]:
            return {
                "decisionHash": decision.decision_hash,
                "generationHash": decision.generation_hash,
                "policyVersion": decision.policy_version,
                "policyHash": decision.policy_hash,
                "outcome": decision.outcome.value,
                "residualClassification": decision.residual_classification,
                "confidenceTier": decision.confidence_tier.value,
                "confidenceBasisPoints": decision.confidence_basis_points,
                "rationaleCode": decision.rationale_code,
                "claimIds": list(decision.claim_ids),
                "observationIds": list(decision.observation_ids),
                "canonicalEventIds": list(decision.canonical_event_ids),
                "featureVector": dict(decision.feature_vector),
                "competingCandidateProof": dict(decision.competing_candidate_proof),
                "sourceHashes": list(decision.source_hashes),
                "humanOverrideId": decision.human_override_id,
            }

        return {
            "schemaVersion": 1,
            "kind": "canonical-identity-shadow-report",
            "private": True,
            "readOnly": True,
            "policyVersion": self.policy.version,
            "policyHash": self.policy.policy_hash,
            "policyDocument": self.policy.document(),
            "inputHash": self.input_hash,
            "generationHash": self.generation_hash,
            "canonicalStateHash": self.canonical_state_hash,
            "counts": {
                "observations": len(self.observations),
                "sourceClaims": len(self.claims),
                "canonicalEventsBefore": len(self.claims),
                "canonicalEventsAfter": len(self.canonical_events),
                "safeAutomaticResolutions": len(automatic),
                "automaticDecisionCount": len(automatic_decisions),
                "safeCanonicalReduction": (
                    len(self.claims) - len(self.canonical_events)
                ),
                "unresolvedDuplicateGroups": len(unresolved),
                "transferLinks": sum(
                    decision.outcome is DecisionOutcome.LINK_TRANSFER
                    for decision in self.decisions
                ),
                "transferCandidates": sum(
                    decision.rationale_code == "transfer-candidate"
                    for decision in self.decisions
                ),
                # Both populations left `unresolvedDecisions` when residual
                # classification became rationale-aware, so they are counted
                # here and republished in `relationshipCandidateDecisions`.
                "reviewRequiredRelationshipCandidates": len(relationship_candidates),
                "excludedUntrustedObservations": sum(
                    decision.outcome is DecisionOutcome.EXCLUDE_UNTRUSTED
                    for decision in self.decisions
                ),
                "crossAccountMirrorCandidates": sum(
                    decision.rationale_code == "cross-account-mirror-candidate"
                    for decision in self.decisions
                ),
                "sourceSuppressedClaims": sum(
                    decision.outcome is DecisionOutcome.SOURCE_SUPPRESSED
                    for decision in self.decisions
                ),
                "authorityAmbiguousGroups": sum(
                    decision.rationale_code
                    in {
                        "ambiguous-automatic-component-collapse",
                        "ambiguous-lower-source-multiplicity",
                        "ambiguous-authoritative-multiplicity",
                        "ambiguous-transfer-endpoint-collapse",
                        "ambiguous-authority-description-mapping",
                        "ambiguous-authority-posting-window",
                    }
                    for decision in self.decisions
                ),
                "authorityPostingWindowSuppressions": sum(
                    decision.outcome is DecisionOutcome.SOURCE_SUPPRESSED
                    and dict(decision.feature_vector).get("sameSourceDay") == "false"
                    for decision in self.decisions
                ),
                "declaredDuplicateSuppressions": sum(
                    decision.rationale_code
                    == "declared-duplicate-summary-suppression"
                    for decision in self.decisions
                ),
                "declaredDuplicateAmbiguousGroups": sum(
                    decision.rationale_code
                    in {
                        "ambiguous-declared-duplicate-multiplicity",
                        "ambiguous-declared-duplicate-description-mapping",
                    }
                    for decision in self.decisions
                ),
                "scopedSharedTokenLinks": sum(
                    decision.rationale_code == "scoped-shared-provider-token-lineage"
                    for decision in self.decisions
                ),
                "scopedSharedTokenAmbiguousGroups": sum(
                    decision.rationale_code
                    in {
                        "ambiguous-shared-token-scope-membership",
                        "ambiguous-shared-token-multiplicity",
                        "ambiguous-shared-token-economic-conflict",
                        "ambiguous-shared-token-date-window",
                    }
                    for decision in self.decisions
                ),
                "authorityCoveredClaims": sum(
                    item.observed_source_transactions
                    for item in self.interval_authorities
                    if item.usable
                ),
            },
            "residualByClass": {
                name: residual.get(name, 0) for name in RESIDUAL_CLASSES
            },
            "sourceAuthority": self.source_authority_document(),
            "automaticByClass": dict(sorted(by_class.items())),
            "automaticByConfidence": dict(sorted(by_confidence.items())),
            "automaticBySourceFamily": dict(sorted(by_pair.items())),
            "automaticDecisions": [
                decision_document(decision) for decision in automatic_decisions
            ],
            "unresolvedDecisions": [
                decision_document(decision) for decision in unresolved
            ],
            "relationshipCandidateDecisions": [
                decision_document(decision) for decision in relationship_candidates
            ],
            "decisionHashes": sorted(
                decision.decision_hash for decision in self.decisions
            ),
        }


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}
        self._members = {value: {value} for value in self.parent}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        self.parent[second] = first
        self._members[first].update(self._members.pop(second))

    def members(self, value: str) -> frozenset[str]:
        return frozenset(self._members[self.find(value)])


_AUTOMATIC_MERGE_KINDS = frozenset(
    {
        RelationKind.DUPLICATE_CANDIDATE,
        RelationKind.PENDING_TRANSITION,
        RelationKind.MIRRORED_PROVIDER_ERROR,
        RelationKind.SOURCE_SUPPRESSED,
    }
)


def _automatic_merge_priority(edge: IdentityEdge) -> tuple[int, str]:
    features = dict(edge.feature_vector)
    if (
        edge.kind
        in {
            RelationKind.PENDING_TRANSITION,
            RelationKind.MIRRORED_PROVIDER_ERROR,
        }
        or features.get("scopedSharedProviderToken") == "true"
    ):
        return (0, edge.edge_hash)
    if edge.kind is RelationKind.SOURCE_SUPPRESSED:
        return (1, edge.edge_hash)
    return (2, edge.edge_hash)


def _merge_barrier_conflicts(
    union: _UnionFind,
    left_claim_id: str,
    right_claim_id: str,
    barriers: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    left_root = union.find(left_claim_id)
    right_root = union.find(right_claim_id)
    if left_root == right_root:
        return ()
    merging = {left_root, right_root}
    return tuple(
        sorted(
            pair
            for pair in barriers
            if {
                union.find(pair[0]),
                union.find(pair[1]),
            }
            == merging
        )
    )


def _source_occurrence_conflicts(
    union: _UnionFind,
    left_claim_id: str,
    right_claim_id: str,
    selected_by_claim: Mapping[str, IdentityObservation],
    explicit_same_scope_pairs: set[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Protect distinct occurrences reported by one source family.

    Stable source claims with different provider identities represent distinct
    occurrences unless explicit lifecycle lineage or a human merge says
    otherwise. This constraint is checked against whole components, preventing
    two authoritative purchases from being joined transitively through lower
    priority observations. The check is evaluated only across the two small
    components being joined rather than materializing every same-source pair
    in a full account history.
    """

    left_root = union.find(left_claim_id)
    right_root = union.find(right_claim_id)
    if left_root == right_root:
        return ()
    right_by_scope: dict[tuple[str, str], list[str]] = defaultdict(list)
    for claim_id in union.members(right_root):
        selected = selected_by_claim[claim_id]
        right_by_scope[
            (
                selected.source_family.casefold(),
                selected.canonical_account_id,
            )
        ].append(claim_id)
    conflicts = []
    for left_id in union.members(left_root):
        selected = selected_by_claim[left_id]
        scope = (
            selected.source_family.casefold(),
            selected.canonical_account_id,
        )
        for right_id in right_by_scope.get(scope, ()):
            pair = _pair_key(left_id, right_id)
            if pair not in explicit_same_scope_pairs:
                conflicts.append(pair)
    return tuple(sorted(conflicts))


def _feature_vector(**values: object) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                key,
                (
                    "true"
                    if value is True
                    else "false"
                    if value is False
                    else str(value)
                ),
            )
            for key, value in values.items()
        )
    )


def _claim_scope(observation: IdentityObservation) -> dict[str, str]:
    if observation.provider_transaction_id and observation.provider_id_kind not in {
        "none",
        "synthetic",
    }:
        return {
            "sourceFamily": observation.source_family,
            "sourceConnection": observation.source_connection_id,
            "sourceAccount": observation.source_account_id,
            "providerIdKind": observation.provider_id_kind,
            "providerTransactionId": observation.provider_transaction_id,
        }
    return {"observationId": observation.observation_id}


def _source_rank(policy: IdentityPolicy, source_family: str) -> int:
    try:
        return len(policy.source_precedence) - policy.source_precedence.index(
            source_family
        )
    except ValueError:
        return 0


def _category_rank(policy: IdentityPolicy, source_family: str) -> int:
    try:
        return len(policy.category_precedence) - policy.category_precedence.index(
            source_family
        )
    except ValueError:
        return 0


def _claim_selection_key(observation: IdentityObservation) -> tuple[object, ...]:
    return (
        observation.observed_at,
        {"pending": 0, "posted": 1, "reversed": 2}[observation.status],
        observation.source_hash,
        observation.observation_id,
    )


def _event_selection_key(
    observation: IdentityObservation,
    policy: IdentityPolicy,
    preferred: frozenset[str] = frozenset(),
) -> tuple[object, ...]:
    return (
        observation.trusted,
        observation.observation_id in preferred,
        _source_rank(policy, observation.source_family),
        observation.status != "pending",
        observation.observed_at,
        {"pending": 0, "posted": 1, "reversed": 2}[observation.status],
        observation.source_hash,
        observation.observation_id,
    )


def _description_selection_key(
    observation: IdentityObservation, policy: IdentityPolicy
) -> tuple[object, ...]:
    normalized = normalized_description(observation.description)
    return (
        len(normalized),
        _source_rank(policy, observation.source_family),
        observation.source_hash,
    )


def _description_relation(left: str, right: str, policy: IdentityPolicy) -> str | None:
    left_normalized = normalized_description(left)
    right_normalized = normalized_description(right)
    if not left_normalized or not right_normalized:
        return None
    if left_normalized == right_normalized:
        return "exact"
    shorter, longer = sorted(
        (left_normalized, right_normalized), key=lambda value: (len(value), value)
    )
    if len(shorter) < policy.prefix_minimum_characters:
        return None
    shorter_tokens = shorter.split()
    longer_tokens = longer.split()
    if longer_tokens[: len(shorter_tokens)] == shorter_tokens:
        return "token-boundary-prefix"
    return None


def _cross_source_score(*, same_day: bool, one_to_one: bool) -> int:
    score = sum(
        CROSS_SOURCE_WEIGHTS[name]
        for name in {
            "sameCanonicalAccount",
            "sameSignedAmountAndCurrency",
            "descriptionExactOrTokenBoundaryPrefix",
            "distinctKnownSourceFamilies",
        }
    )
    if same_day:
        score += CROSS_SOURCE_WEIGHTS["sameSourceDay"]
    if one_to_one:
        score += CROSS_SOURCE_WEIGHTS["oneToOneComponent"]
    return score


def _days(left: IdentityObservation, right: IdentityObservation) -> int:
    return abs((left.source_day - right.source_day).days)


def _pair_key(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right)))  # type: ignore[return-value]


def _edge(
    kind: RelationKind,
    left: SourceClaim,
    right: SourceClaim,
    features: tuple[tuple[str, str], ...],
    source_hashes: Iterable[str],
    *,
    confidence: int = REVIEW_CONFIDENCE_BASIS_POINTS,
    automatic: bool = False,
    proof: tuple[tuple[str, int], ...] = (),
) -> IdentityEdge:
    left_id, right_id = _pair_key(left.claim_id, right.claim_id)
    body = {
        "kind": kind.value,
        "leftClaimId": left_id,
        "rightClaimId": right_id,
        "features": dict(features),
        "sourceHashes": sorted(set(source_hashes)),
    }
    return IdentityEdge(
        edge_hash=content_hash(body),
        kind=kind,
        left_claim_id=left_id,
        right_claim_id=right_id,
        feature_vector=features,
        confidence_basis_points=confidence,
        automatic=automatic,
        competing_candidate_proof=proof,
        source_hashes=tuple(sorted(set(source_hashes))),
    )


def _latest_overrides(
    overrides: Iterable[HumanOverride],
) -> tuple[HumanOverride, ...]:
    latest: dict[str, HumanOverride] = {}
    by_id = {item.override_id: item for item in overrides}
    for override in sorted(
        overrides, key=lambda item: (item.decided_at, item.version, item.override_id)
    ):
        if (
            override.supersedes_override_id is not None
            and override.supersedes_override_id not in by_id
        ):
            raise ValueError("override supersedes an unknown decision")
        key = content_hash(sorted(override.claim_ids))
        previous = latest.get(key)
        if previous and override.version <= previous.version:
            raise ValueError("override versions must increase for the same claim set")
        latest[key] = override
    return tuple(sorted(latest.values(), key=lambda item: item.override_id))


def _make_decision(
    *,
    generation_hash: str,
    policy: IdentityPolicy,
    outcome: DecisionOutcome,
    confidence_tier: ConfidenceTier,
    confidence_basis_points: int,
    rationale_code: str,
    claim_ids: Iterable[str],
    observation_ids: Iterable[str],
    canonical_event_ids: Iterable[str],
    feature_vector: tuple[tuple[str, str], ...] = (),
    competing_candidate_proof: tuple[tuple[str, int], ...] = (),
    source_hashes: Iterable[str] = (),
    human_override_id: str | None = None,
) -> IdentityDecision:
    body = {
        "generationHash": generation_hash,
        "policyVersion": policy.version,
        "policyHash": policy.policy_hash,
        "outcome": outcome.value,
        "confidenceTier": confidence_tier.value,
        "confidenceBasisPoints": confidence_basis_points,
        "rationaleCode": rationale_code,
        "claimIds": sorted(set(claim_ids)),
        "observationIds": sorted(set(observation_ids)),
        "canonicalEventIds": sorted(set(canonical_event_ids)),
        "featureVector": dict(feature_vector),
        "competingCandidateProof": dict(competing_candidate_proof),
        "sourceHashes": sorted(set(source_hashes)),
        "humanOverrideId": human_override_id,
    }
    return IdentityDecision(
        decision_hash=content_hash(body),
        generation_hash=generation_hash,
        policy_version=policy.version,
        policy_hash=policy.policy_hash,
        outcome=outcome,
        confidence_tier=confidence_tier,
        confidence_basis_points=confidence_basis_points,
        rationale_code=rationale_code,
        claim_ids=tuple(body["claimIds"]),
        observation_ids=tuple(body["observationIds"]),
        canonical_event_ids=tuple(body["canonicalEventIds"]),
        feature_vector=feature_vector,
        competing_candidate_proof=competing_candidate_proof,
        source_hashes=tuple(body["sourceHashes"]),
        human_override_id=human_override_id,
    )


def _connected_components(
    claim_ids: Iterable[str], edges: Iterable[IdentityEdge]
) -> tuple[tuple[str, ...], ...]:
    neighbors = {claim_id: set() for claim_id in claim_ids}
    for edge in edges:
        neighbors.setdefault(edge.left_claim_id, set()).add(edge.right_claim_id)
        neighbors.setdefault(edge.right_claim_id, set()).add(edge.left_claim_id)
    remaining = {key for key, value in neighbors.items() if value}
    components = []
    while remaining:
        stack = [min(remaining)]
        members: set[str] = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            stack.extend(neighbors[current] - members)
        remaining -= members
        components.append(tuple(sorted(members)))
    return tuple(sorted(components))


def _observation_identity_document(
    observation: IdentityObservation,
) -> dict[str, Any]:
    return {
        "observationId": observation.observation_id,
        "sourceFamily": observation.source_family,
        "sourceConnectionHash": content_hash(observation.source_connection_id),
        "sourceAccountHash": content_hash(observation.source_account_id),
        "canonicalAccountHash": content_hash(observation.canonical_account_id),
        "providerTransactionHash": (
            content_hash(observation.provider_transaction_id)
            if observation.provider_transaction_id
            else None
        ),
        "providerIdKind": observation.provider_id_kind,
        "sourceHash": observation.source_hash,
        "sourceDay": observation.source_day.isoformat(),
        "observedAt": observation.observed_at.isoformat(),
        "signedAmount": format(observation.signed_amount, "f"),
        "currency": observation.currency,
        "descriptionHash": content_hash(observation.description),
        "status": observation.status,
        "categoryHash": content_hash(observation.category),
        "sourceGroupHash": (
            content_hash(observation.source_group_id)
            if observation.source_group_id
            else None
        ),
        "importLineageHash": observation.import_lineage_hash,
        "trustCutoffDay": (
            observation.trust_cutoff_day.isoformat()
            if observation.trust_cutoff_day
            else None
        ),
        "accountStatus": observation.account_status,
        "attributesHash": content_hash(observation.attributes),
    }


#: Relation kinds that structurally disqualify a claim from source-coverage
#: suppression. Each one asserts explicit lineage about the economics of the
#: claim itself, which coverage evidence is not allowed to reinterpret.
#: Transfer and mirror *candidates* -- and explicit transfers -- are deliberately
#: absent: they describe the relationship between two distinct cross-account
#: legs, not whether one leg was reported twice inside one account.
_AUTHORITY_BLOCKING_RELATIONS = frozenset(
    {
        RelationKind.CORRECTION,
        RelationKind.REVERSAL,
        RelationKind.PENDING_TRANSITION,
        RelationKind.MIRRORED_PROVIDER_ERROR,
        RelationKind.SOURCE_SUPPRESSED,
    }
)


def _preserve_transfer_endpoints(
    suppressions: list["_AuthoritySuppression"],
    transfer_pairs: set[tuple[str, str]],
) -> tuple[list["_AuthoritySuppression"], list["_AuthoritySuppression"]]:
    """Drop any suppression that would merge a transfer's two endpoints.

    A single suppression can never do this on its own -- the direct pair is
    refused earlier -- but a chain can: with an explicit same-account transfer
    ``(A, B)``, suppressing both ``A`` and ``B`` into a third copy ``C`` would
    quietly collapse the transfer into one event. Suppressions are applied in
    stable sorted order and one is refused the moment it would close such a
    chain, so the outcome does not depend on input order.
    """

    ordered = sorted(
        suppressions,
        key=lambda item: (item.suppressed_claim_id, item.authoritative_claim_id),
    )
    union = _UnionFind(
        {item.suppressed_claim_id for item in ordered}
        | {item.authoritative_claim_id for item in ordered}
        | {claim_id for pair in transfer_pairs for claim_id in pair}
    )
    kept: list[_AuthoritySuppression] = []
    dropped: list[_AuthoritySuppression] = []
    for item in ordered:
        left = union.find(item.suppressed_claim_id)
        right = union.find(item.authoritative_claim_id)
        merging = {left, right}
        if any(
            {union.find(first), union.find(second)} == merging
            for first, second in transfer_pairs
        ):
            dropped.append(item)
            continue
        union.union(item.suppressed_claim_id, item.authoritative_claim_id)
        kept.append(item)
    return kept, dropped


def _occurrence_pairs(
    authoritative: Sequence[str],
    lower: Sequence[str],
    description: Any,
    policy: IdentityPolicy,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Map lower occurrences onto authoritative ones, one to one.

    Inside a bucket both sources have already proven complete, reconciled,
    overlapping coverage of the same account, day, signed amount and currency.
    Coverage alone still cannot prove which same-value observation corresponds
    to which authoritative occurrence. Current coverage matching requires exact
    normalized descriptions. A shared merchant phone number, store code, or
    unclassified reference is not transaction identity.

    Multiplicity is preserved in both directions.  An authoritative occurrence
    is claimed at most once, so three authoritative purchases stay three
    events; and a lower occurrence with nothing left to pair against is
    returned as excess rather than suppressed, so a genuine extra repeat the
    authority never saw survives for review.
    """

    available: dict[str, list[str]] = defaultdict(list)
    for claim_id in sorted(authoritative):
        available[description(claim_id)].append(claim_id)
    pairs: list[tuple[str, str]] = []
    unmatched: list[str] = []
    for claim_id in sorted(lower):
        queue = available.get(description(claim_id))
        if queue:
            pairs.append((claim_id, queue.pop(0)))
        else:
            unmatched.append(claim_id)
    if policy.authority_policy.version != "canonical-source-authority-v2":
        return sorted(pairs), sorted(unmatched)
    # Preserve the old document's behavior only for explicit historical replay.
    still_unmatched: list[str] = []
    for claim_id in unmatched:
        related = [
            candidate
            for candidate, queue in sorted(available.items())
            if queue
            and _description_relation(
                description(claim_id),
                candidate,
                policy,
            )
            and _shared_discriminating_token(
                description(claim_id),
                candidate,
            )
        ]
        if len(related) == 1:
            pairs.append((claim_id, available[related[0]].pop(0)))
        else:
            still_unmatched.append(claim_id)
    return sorted(pairs), sorted(still_unmatched)


def _shared_discriminating_token(left: str, right: str) -> bool:
    left_tokens = set(re.findall(r"[a-z0-9]+", left))
    right_tokens = set(re.findall(r"[a-z0-9]+", right))
    return any(
        len(token) >= 7 and any(character.isdigit() for character in token)
        for token in left_tokens & right_tokens
    )


def _description_mapped_pairs(
    authoritative: Sequence[str],
    lower: Sequence[str],
    description: Any,
    policy: IdentityPolicy,
) -> tuple[list[tuple[str, str]], list[str], bool]:
    """The pre-``countBasedOccurrencePairing`` mapping, kept for replay.

    Every lower description had to relate to exactly one authoritative
    description, and no authoritative description could be claimed twice.  A
    policy that still declares ``count_based_occurrence_pairing`` false replays
    on this path.
    """

    authoritative_by_description: dict[str, list[str]] = defaultdict(list)
    for claim_id in sorted(authoritative):
        authoritative_by_description[description(claim_id)].append(claim_id)
    lower_by_description: dict[str, list[str]] = defaultdict(list)
    for claim_id in sorted(lower):
        lower_by_description[description(claim_id)].append(claim_id)

    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    for lower_description in sorted(lower_by_description):
        related = [
            candidate
            for candidate in sorted(authoritative_by_description)
            if _description_relation(lower_description, candidate, policy)
        ]
        if len(related) != 1 or related[0] in claimed:
            return [], [], True
        mapping[lower_description] = related[0]
        claimed.add(related[0])

    pairs: list[tuple[str, str]] = []
    excess: list[str] = []
    for lower_description, target in sorted(mapping.items()):
        lower_group = lower_by_description[lower_description]
        authoritative_group = authoritative_by_description[target]
        if len(lower_group) > len(authoritative_group):
            excess.extend(lower_group)
            continue
        pairs.extend(zip(lower_group, authoritative_group))
    return sorted(pairs), sorted(excess), False


@dataclass(frozen=True, slots=True)
class _AuthoritySuppression:
    suppressed_claim_id: str
    authoritative_claim_id: str
    feature_vector: tuple[tuple[str, str], ...]
    proof: tuple[tuple[str, int], ...]
    source_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AuthorityAmbiguity:
    claim_ids: tuple[str, ...]
    rationale_code: str
    feature_vector: tuple[tuple[str, str], ...]
    proof: tuple[tuple[str, int], ...]
    source_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AuthorityPlan:
    interval_authorities: tuple[IntervalAuthority, ...] = ()
    suppressions: tuple[_AuthoritySuppression, ...] = ()
    ambiguities: tuple[_AuthorityAmbiguity, ...] = ()
    resolved_claims: frozenset[str] = frozenset()
    preferred_observations: frozenset[str] = frozenset()


def _interval_authorities(
    policy: IdentityPolicy,
    claims: Iterable[SourceClaim],
    observation_by_id: Mapping[str, IdentityObservation],
) -> tuple[IntervalAuthority, ...]:
    authority = policy.source_authority
    authority_policy = authority.policy
    ordered_claims = tuple(claims)
    verdicts = []
    for interval in authority.intervals:
        evidence = interval.evidence
        matching_claims = tuple(
            claim
            for claim in ordered_claims
            for selected in (
                observation_by_id[claim.selected_observation_id],
            )
            if interval.matches(selected)
            and (
                not dict(selected.attributes).get("sourceArtifactSha256")
                or dict(selected.attributes)["sourceArtifactSha256"]
                in evidence.source_hashes
            )
        )
        membership = sorted(
            (
                {
                    "sourceFamily": observation.source_family,
                    "canonicalAccountHash": content_hash(
                        observation.canonical_account_id
                    ),
                    "sourceAccountHash": content_hash(
                        observation.source_account_id
                    ),
                    "providerIdentityHash": (
                        content_hash(observation.provider_transaction_id)
                        if observation.provider_transaction_id
                        else None
                    ),
                    "providerIdKind": observation.provider_id_kind,
                    "sourceDay": observation.source_day.isoformat(),
                    "signedAmount": format(
                        observation.signed_amount, "f"
                    ),
                    "currency": observation.currency,
                    "status": observation.status,
                    "descriptionHash": content_hash(
                        normalized_description(observation.description)
                    ),
                    "sourceArtifactSha256": dict(
                        observation.attributes
                    ).get("sourceArtifactSha256"),
                }
                for claim in matching_claims
                for observation in (
                    observation_by_id[claim.selected_observation_id],
                )
            ),
            key=canonical_json,
        )
        observed = len(matching_claims)
        settlement = interval.settlement_days()
        complete = evidence.completeness == authority_policy.required_completeness
        fresh = settlement >= authority_policy.settlement_days
        sufficient = (
            evidence.source_transaction_count
            >= authority_policy.minimum_source_transaction_count
        )
        hashed = bool(evidence.source_hashes)
        proven = complete and fresh and sufficient and hashed
        reconciled = (
            observed == evidence.source_transaction_count
            if authority_policy.require_reconciled_source_counts
            else True
        )
        stable_required = (
            evidence.format_strength in authority_policy.stable_identity_strengths
        )
        stable_proven = evidence.stable_id_support and evidence.replay_stable_ids
        rank = authority_policy.rank(evidence.source_family, evidence.format_strength)
        if rank is not None and stable_required and not stable_proven:
            rank = None
        verdicts.append(
            IntervalAuthority(
                interval=interval,
                proven=proven,
                rank=rank,
                observed_source_transactions=observed,
                reconciled=reconciled,
                proof=_feature_vector(
                    completenessProven=complete,
                    freshnessProven=fresh,
                    settlementDays=settlement,
                    requiredSettlementDays=authority_policy.settlement_days,
                    withinExtractionWindow=True,
                    trustCutoffRespected=True,
                    sourceCountProven=sufficient,
                    sourceHashesRecorded=hashed,
                    stableIdRequired=stable_required,
                    stableIdProven=stable_proven,
                    countsReconciled=reconciled,
                    declaredSourceTransactionCount=(
                        evidence.source_transaction_count
                    ),
                    observedSourceTransactionCount=observed,
                    observedClaimMembershipHash=content_hash(
                        membership
                    ),
                    sourceArtifactBound=all(
                        bool(
                            dict(
                                observation_by_id[
                                    claim.selected_observation_id
                                ].attributes
                            ).get("sourceArtifactSha256")
                        )
                        for claim in matching_claims
                    ),
                    formatStrength=evidence.format_strength,
                    rank="none" if rank is None else rank,
                ),
            )
        )
    return tuple(verdicts)


def _source_authority_plan(
    *,
    policy: IdentityPolicy,
    claims: tuple[SourceClaim, ...],
    claim_by_id: Mapping[str, SourceClaim],
    observation_by_id: Mapping[str, IdentityObservation],
    edges: Mapping[tuple[RelationKind, str, str], IdentityEdge],
    blocked_pairs: set[tuple[str, str]],
    declared_transfer_pairs: set[tuple[str, str]],
    observations: tuple[IdentityObservation, ...],
) -> _AuthorityPlan:
    """Classify lower-priority overlapping observations as source-suppressed.

    Suppression is only ever applied inside an interval whose authority is
    proven by recorded evidence, and only when a deterministic one-to-one
    occurrence mapping preserves multiplicity.  Everything else is left alone.
    """

    verdicts = _interval_authorities(policy, claims, observation_by_id)
    if not verdicts:
        return _AuthorityPlan()
    authority_policy = policy.authority_policy
    usable = [item for item in verdicts if item.usable]
    if not usable:
        return _AuthorityPlan(interval_authorities=verdicts)

    # A claim touched by an explicit *lineage* relation is structurally
    # excluded: corrections, reversals, pending transitions, and mirrored
    # provider errors assert something about the economics that source coverage
    # is not allowed to reinterpret.
    #
    # Transfer and mirror *candidates*, and explicit transfers, are different in
    # kind. They are cross-account statements about two distinct economic legs.
    # They say nothing about whether one leg was reported twice by two writers
    # in the same account, so they must not disqualify same-account coverage
    # suppression -- otherwise an account with many real transfers can never be
    # deduplicated at all. Both legs are preserved either way, and the relation
    # is remapped onto the surviving canonical events after canonicalization.
    linked_claims: set[str] = set()
    for edge in edges.values():
        if edge.kind not in _AUTHORITY_BLOCKING_RELATIONS:
            continue
        linked_claims.add(edge.left_claim_id)
        linked_claims.add(edge.right_claim_id)

    # Explicit transfers still get one guarantee: collapsing same-account source
    # copies may never make a transfer's two opposite-sign endpoints land on the
    # same canonical event.  Operator ``transfer`` overrides count here too --
    # they assert the same relationship without producing an inferred edge.
    transfer_pairs: set[tuple[str, str]] = {
        _pair_key(edge.left_claim_id, edge.right_claim_id)
        for edge in edges.values()
        if edge.kind is RelationKind.TRANSFER
    } | set(declared_transfer_pairs)

    covering: dict[str, IntervalAuthority] = {}
    for claim in claims:
        selected = observation_by_id[claim.selected_observation_id]
        source_artifact_sha256 = dict(selected.attributes).get(
            "sourceArtifactSha256"
        )
        matched = [
            item
            for item in usable
            if item.interval.matches(selected)
            and (
                not source_artifact_sha256
                or source_artifact_sha256
                in item.interval.evidence.source_hashes
            )
        ]
        if len(matched) != 1:
            continue
        covering[claim.claim_id] = matched[0]

    buckets: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    economic: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    day_by_claim: dict[str, date] = {}
    for claim_id, verdict in sorted(covering.items()):
        claim = claim_by_id[claim_id]
        members = [
            observation_by_id[observation_id]
            for observation_id in claim.observation_ids
        ]
        selected = observation_by_id[claim.selected_observation_id]
        if authority_policy.require_trusted_observations and not all(
            member.trusted for member in members
        ):
            continue
        if authority_policy.require_posted_status and not all(
            member.status == "posted" for member in members
        ):
            continue
        if selected.signed_amount == 0:
            continue
        if claim_id in linked_claims:
            continue
        day_by_claim[claim_id] = selected.source_day
        economic[
            (
                selected.canonical_account_id,
                str(selected.signed_amount),
                selected.currency,
            )
        ].append(claim_id)

    suppressions: list[_AuthoritySuppression] = []
    ambiguities: list[_AuthorityAmbiguity] = []
    resolved: set[str] = set()
    preferred: set[str] = set()

    # Two writers can stamp one settlement on different days.  Where an
    # authoritative interval explicitly declares a posting-date tolerance, a
    # lower observation is drawn onto the authoritative day it can be proven to
    # belong to -- but only when exactly one such day exists.  Without a
    # declared tolerance every claim keeps its own day, which is the historical
    # same-day behaviour.
    for economic_key, group_claim_ids in sorted(economic.items()):

        def place(claim_id: str, day: date) -> None:
            buckets[(*economic_key, day.isoformat())].append(claim_id)

        group_ranks = {
            claim_id: covering[claim_id].rank for claim_id in group_claim_ids
        }
        if any(value is None for value in group_ranks.values()):
            for claim_id in sorted(group_claim_ids):
                place(claim_id, day_by_claim[claim_id])
            continue
        group_top_rank = max(group_ranks.values())
        anchors = sorted(
            claim_id
            for claim_id in group_claim_ids
            if group_ranks[claim_id] == group_top_rank
        )
        anchor_days = sorted({day_by_claim[claim_id] for claim_id in anchors})
        for claim_id in sorted(group_claim_ids):
            day = day_by_claim[claim_id]
            if group_ranks[claim_id] == group_top_rank or day in anchor_days:
                place(claim_id, day)
                continue
            lower_verdict = covering[claim_id]
            reachable: set[date] = set()
            for anchor_id in anchors:
                anchor_day = day_by_claim[anchor_id]
                anchor_interval = covering[anchor_id].interval
                tolerance = anchor_interval.posting_date_tolerance_days
                if not tolerance:
                    continue
                if abs((day - anchor_day).days) > tolerance:
                    continue
                if not anchor_interval.proves_both_days(day, anchor_day):
                    continue
                if not lower_verdict.interval.proves_both_days(day, anchor_day):
                    continue
                reachable.add(anchor_day)
            if not reachable:
                place(claim_id, day)
            elif len(reachable) == 1:
                place(claim_id, next(iter(reachable)))
            else:
                # More than one authoritative day could explain this row.  A
                # tolerance must never let two candidates compete.
                competing = sorted(
                    anchor_id
                    for anchor_id in anchors
                    if day_by_claim[anchor_id] in reachable
                )
                ambiguities.append(
                    _AuthorityAmbiguity(
                        claim_ids=tuple(sorted([claim_id, *competing])),
                        rationale_code="ambiguous-authority-posting-window",
                        feature_vector=_feature_vector(
                            authorityPolicyHash=authority_policy.policy_hash,
                            authorityPolicyVersion=authority_policy.version,
                            sameCanonicalAccount=True,
                            sameSourceDay=False,
                            sameSignedAmountAndCurrency=True,
                            sourceDay=day.isoformat(),
                            authoritativeRank=group_top_rank,
                            reachableAuthoritativeDayCount=len(reachable),
                        ),
                        proof=tuple(
                            sorted(
                                {
                                    "authoritativeCount": len(competing),
                                    "lowerCount": 1,
                                    "bucketParticipantCount": len(group_claim_ids),
                                    "reachableAuthoritativeDayCount": len(reachable),
                                    "maxPostingDateToleranceDays": max(
                                        covering[
                                            anchor_id
                                        ].interval.posting_date_tolerance_days
                                        for anchor_id in competing
                                    ),
                                }.items()
                            )
                        ),
                        source_hashes=tuple(
                            sorted(
                                {
                                    value
                                    for member_id in [claim_id, *competing]
                                    for value in claim_by_id[member_id].source_hashes
                                }
                            )
                        ),
                    )
                )

    for bucket_key, claim_ids in sorted(buckets.items()):
        if len(claim_ids) < 2:
            continue
        ordered_claims = sorted(claim_ids)
        ranks = {
            claim_id: covering[claim_id].rank for claim_id in ordered_claims
        }
        # An unranked participant means the policy cannot order this bucket at
        # all, so nothing in it may be suppressed.
        if any(value is None for value in ranks.values()):
            continue
        top_rank = max(ranks.values())
        authoritative = sorted(
            claim_id for claim_id in ordered_claims if ranks.get(claim_id) == top_rank
        )
        lower = sorted(set(ordered_claims) - set(authoritative))
        if not authoritative or not lower:
            continue

        def description(claim_id: str) -> str:
            return normalized_description(
                observation_by_id[
                    claim_by_id[claim_id].selected_observation_id
                ].description
            )

        authoritative_scope_counts = Counter(
            covering[claim_id].interval.evidence.source_scope
            for claim_id in authoritative
        )

        bucket_source_hashes = sorted(
            {
                value
                for claim_id in ordered_claims
                for value in claim_by_id[claim_id].source_hashes
            }
        )
        bucket_days = sorted({day_by_claim[claim_id] for claim_id in ordered_claims})
        bucket_features = _feature_vector(
            **{
                "authorityPolicyHash": authority_policy.policy_hash,
                "authorityPolicyVersion": authority_policy.version,
                "sameCanonicalAccount": True,
                "sameSourceDay": len(bucket_days) == 1,
                "sameSignedAmountAndCurrency": True,
                "sourceDay": bucket_key[3],
                "authoritativeRank": top_rank,
                # Recorded only where the bucket actually spans days, so a
                # same-day bucket keeps the feature vector it always had.
                **(
                    {}
                    if len(bucket_days) == 1
                    else {
                        "sourceDaySpanDays": (
                            max(bucket_days) - min(bucket_days)
                        ).days
                    }
                ),
            }
        )

        # Two top-rank sources that disagree about how many events happened
        # cannot both be right, and nothing here can decide which is.  The
        # whole bucket stays open rather than picking a winner.
        if len(set(authoritative_scope_counts.values())) > 1:
            ambiguities.append(
                _AuthorityAmbiguity(
                    claim_ids=tuple(ordered_claims),
                    rationale_code="ambiguous-authoritative-multiplicity",
                    feature_vector=bucket_features,
                    proof=tuple(
                        sorted(
                            {
                                "authoritativeCount": len(authoritative),
                                "lowerCount": len(lower),
                                "bucketParticipantCount": len(ordered_claims),
                                "authoritativeSourceScopeCount": len(
                                    authoritative_scope_counts
                                ),
                                "maxAuthoritativeScopeCount": max(
                                    authoritative_scope_counts.values()
                                ),
                                "minAuthoritativeScopeCount": min(
                                    authoritative_scope_counts.values()
                                ),
                            }.items()
                        )
                    ),
                    source_hashes=tuple(bucket_source_hashes),
                )
            )
            continue

        lower_by_scope: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for claim_id in lower:
            lower_by_scope[
                covering[claim_id].interval.evidence.source_scope
            ].append(claim_id)
        pair_records: list[tuple[str, str, int, int, int, int]] = []
        for _lower_scope, scoped_lower in sorted(lower_by_scope.items()):
            scoped_lower = sorted(scoped_lower)
            if authority_policy.count_based_occurrence_pairing:
                pairs, excess = _occurrence_pairs(
                    authoritative,
                    scoped_lower,
                    description,
                    policy,
                )
                unmappable = False
            else:
                pairs, excess, unmappable = _description_mapped_pairs(
                    authoritative,
                    scoped_lower,
                    description,
                    policy,
                )
            scoped_claim_ids = tuple(sorted([*authoritative, *scoped_lower]))
            scoped_source_hashes = tuple(
                sorted(
                    {
                        source_hash
                        for claim_id in scoped_claim_ids
                        for source_hash in claim_by_id[claim_id].source_hashes
                    }
                )
            )
            if unmappable:
                ambiguities.append(
                    _AuthorityAmbiguity(
                        claim_ids=scoped_claim_ids,
                        rationale_code="ambiguous-authority-description-mapping",
                        feature_vector=bucket_features,
                        proof=tuple(
                            sorted(
                                {
                                    "authoritativeCount": len(authoritative),
                                    "lowerCount": len(scoped_lower),
                                    "bucketParticipantCount": len(scoped_claim_ids),
                                    "authoritativeDescriptionCount": len(
                                        {
                                            description(item)
                                            for item in authoritative
                                        }
                                    ),
                                    "lowerDescriptionCount": len(
                                        {
                                            description(item)
                                            for item in scoped_lower
                                        }
                                    ),
                                    "reportingSourceScopeCount": 1,
                                }.items()
                            )
                        ),
                        source_hashes=scoped_source_hashes,
                    )
                )
                continue

            # Occurrence capacity is independent per reporting source scope.
            # OFX, SimpleFIN, and a legacy export may all corroborate the same
            # authoritative occurrence without consuming one another's slot.
            if excess:
                ambiguities.append(
                    _AuthorityAmbiguity(
                        claim_ids=tuple(sorted(excess)),
                        rationale_code="ambiguous-lower-source-multiplicity",
                        feature_vector=bucket_features,
                        proof=tuple(
                            sorted(
                                {
                                    "authoritativeCount": len(authoritative),
                                    "lowerCount": len(scoped_lower),
                                    "bucketParticipantCount": len(scoped_claim_ids),
                                    "pairedOccurrenceCount": len(pairs),
                                    "excessLowerOccurrenceCount": len(excess),
                                    "reportingSourceScopeCount": 1,
                                }.items()
                            )
                        ),
                        source_hashes=scoped_source_hashes,
                    )
                )
            pair_records.extend(
                (
                    lower_claim_id,
                    authoritative_claim_id,
                    index,
                    len(scoped_lower),
                    len(pairs),
                    len(excess),
                )
                for index, (lower_claim_id, authoritative_claim_id) in enumerate(
                    pairs
                )
            )

        planned: list[_AuthoritySuppression] = []
        blocked_group = False
        for (
            lower_claim_id,
            authoritative_claim_id,
            index,
            lower_count,
            paired_count,
            excess_count,
        ) in pair_records:
            lower_description = description(lower_claim_id)
            target = description(authoritative_claim_id)
            lower_verdict = covering[lower_claim_id]
            authoritative_verdict = covering[authoritative_claim_id]
            lower_scope = lower_verdict.interval.evidence.source_scope
            authoritative_scope = (
                authoritative_verdict.interval.evidence.source_scope
            )
            if (
                authority_policy.require_distinct_source_scope
                and lower_scope == authoritative_scope
            ):
                blocked_group = True
                continue
            if not lower_verdict.interval.overlaps(
                authoritative_verdict.interval
            ):
                blocked_group = True
                continue
            if (
                _pair_key(lower_claim_id, authoritative_claim_id)
                in blocked_pairs
            ):
                blocked_group = True
                continue
            if (
                _pair_key(lower_claim_id, authoritative_claim_id)
                in transfer_pairs
            ):
                # Suppressing here would merge a transfer's two endpoints into
                # one canonical event and erase the leg.
                blocked_group = True
                continue
            lower_observation = observation_by_id[
                claim_by_id[lower_claim_id].selected_observation_id
            ]
            authoritative_observation = observation_by_id[
                claim_by_id[authoritative_claim_id].selected_observation_id
            ]
            if (
                lower_observation.currency != authoritative_observation.currency
                or lower_observation.signed_amount
                != authoritative_observation.signed_amount
                or lower_observation.canonical_account_id
                != authoritative_observation.canonical_account_id
            ):
                blocked_group = True
                continue
            day_distance = abs(
                (
                    lower_observation.source_day
                    - authoritative_observation.source_day
                ).days
            )
            tolerance = (
                authoritative_verdict.interval.posting_date_tolerance_days
            )
            if day_distance:
                # Re-prove the window on the final pairing rather than
                # trusting the bucket anchor: the occurrence mapping may
                # have paired this row with a different authoritative claim
                # than the one that drew it onto this day.
                if day_distance > tolerance:
                    blocked_group = True
                    continue
                if not authoritative_verdict.interval.proves_both_days(
                    lower_observation.source_day,
                    authoritative_observation.source_day,
                ) or not lower_verdict.interval.proves_both_days(
                    lower_observation.source_day,
                    authoritative_observation.source_day,
                ):
                    blocked_group = True
                    continue
            # A day distance is recorded only where one exists, so a
            # same-day suppression keeps the decision it always produced.
            window_features: dict[str, object] = (
                {}
                if not day_distance
                else {
                    "sourceDayDistanceDays": day_distance,
                    "postingDateToleranceDays": tolerance,
                    "authoritativeSourceDay": (
                        authoritative_observation.source_day.isoformat()
                    ),
                    "suppressedSourceDay": (
                        lower_observation.source_day.isoformat()
                    ),
                }
            )
            window_proof: dict[str, int] = (
                {}
                if not day_distance
                else {
                    "sourceDayDistanceDays": day_distance,
                    "maxPostingDateToleranceDays": tolerance,
                }
            )
            planned.append(
                _AuthoritySuppression(
                    suppressed_claim_id=lower_claim_id,
                    authoritative_claim_id=authoritative_claim_id,
                    feature_vector=_feature_vector(
                        **{
                            "authorityPolicyHash": authority_policy.policy_hash,
                            "authorityPolicyVersion": authority_policy.version,
                            "suppressedClaimId": lower_claim_id,
                            "authoritativeClaimId": authoritative_claim_id,
                            "authoritativeIntervalId": (
                                authoritative_verdict.interval.interval_id
                            ),
                            "suppressedIntervalId": (
                                lower_verdict.interval.interval_id
                            ),
                            "authoritativeEvidenceHash": (
                                authoritative_verdict.interval.evidence.evidence_hash
                            ),
                            "suppressedEvidenceHash": (
                                lower_verdict.interval.evidence.evidence_hash
                            ),
                            "authoritativeClaimMembershipHash": dict(
                                authoritative_verdict.proof
                            )["observedClaimMembershipHash"],
                            "suppressedClaimMembershipHash": dict(
                                lower_verdict.proof
                            )["observedClaimMembershipHash"],
                            "authoritativeSourceFamily": authoritative_scope[0],
                            "suppressedSourceFamily": lower_scope[0],
                            "authoritativeFormatStrength": (
                                authoritative_verdict.interval.evidence.format_strength
                            ),
                            "suppressedFormatStrength": (
                                lower_verdict.interval.evidence.format_strength
                            ),
                            "sameCanonicalAccount": True,
                            "sameSourceDay": not day_distance,
                            "sameSignedAmountAndCurrency": True,
                            "distinctSourceScope": True,
                            "intervalsOverlap": True,
                            "descriptionRelation": (
                                _description_relation(
                                    lower_description, target, policy
                                )
                                or "none"
                            ),
                            "occurrenceIndex": index,
                            "categoryParticipates": False,
                            "writerTimestampParticipates": False,
                            **window_features,
                        }
                    ),
                    proof=tuple(
                        sorted(
                            {
                                "authoritativeCount": len(authoritative),
                                "lowerCount": lower_count,
                                "bucketParticipantCount": (
                                    len(authoritative) + lower_count
                                ),
                                "pairedOccurrenceCount": paired_count,
                                "excessLowerOccurrenceCount": excess_count,
                                "reportingSourceScopeCount": 1,
                                "occurrenceIndex": index,
                                "authoritativeRank": top_rank,
                                "suppressedRank": (
                                    lower_verdict.rank
                                    if lower_verdict.rank is not None
                                    else 0
                                ),
                                "authoritativeIntervalSourceCount": (
                                    authoritative_verdict.interval.evidence.source_transaction_count
                                ),
                                "suppressedIntervalSourceCount": (
                                    lower_verdict.interval.evidence.source_transaction_count
                                ),
                                **window_proof,
                            }.items()
                        )
                    ),
                    source_hashes=tuple(
                        sorted(
                            set(claim_by_id[lower_claim_id].source_hashes)
                            | set(
                                claim_by_id[
                                    authoritative_claim_id
                                ].source_hashes
                            )
                        )
                    ),
                )
            )
        if blocked_group and not planned:
            continue
        suppressions.extend(planned)
        for item in planned:
            resolved.add(item.suppressed_claim_id)
            resolved.add(item.authoritative_claim_id)
            preferred.update(
                claim_by_id[item.authoritative_claim_id].observation_ids
            )

    if transfer_pairs and suppressions:
        suppressions, dropped = _preserve_transfer_endpoints(
            suppressions, transfer_pairs
        )
        for item in dropped:
            resolved.discard(item.suppressed_claim_id)
            resolved.discard(item.authoritative_claim_id)
            preferred.difference_update(
                claim_by_id[item.authoritative_claim_id].observation_ids
            )
            ambiguities.append(
                _AuthorityAmbiguity(
                    rationale_code="ambiguous-transfer-endpoint-collapse",
                    claim_ids=_pair_key(
                        item.suppressed_claim_id, item.authoritative_claim_id
                    ),
                    feature_vector=item.feature_vector,
                    proof=item.proof,
                    source_hashes=item.source_hashes,
                )
            )

    return _AuthorityPlan(
        interval_authorities=verdicts,
        suppressions=tuple(
            sorted(
                suppressions,
                key=lambda item: (
                    item.suppressed_claim_id,
                    item.authoritative_claim_id,
                ),
            )
        ),
        ambiguities=tuple(
            sorted(ambiguities, key=lambda item: (item.rationale_code, item.claim_ids))
        ),
        resolved_claims=frozenset(resolved),
        preferred_observations=frozenset(preferred),
    )


@dataclass(frozen=True, slots=True)
class _DeclaredDuplicate:
    duplicate_claim_id: str
    authoritative_claim_id: str
    feature_vector: tuple[tuple[str, str], ...]
    proof: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class _DeclaredDuplicatePlan:
    suppressions: tuple[_DeclaredDuplicate, ...] = ()
    ambiguities: tuple[_AuthorityAmbiguity, ...] = ()
    #: Every duplicate/authoritative pair inside a bucket the declaration fully
    #: resolved.  Those pairs are explained by the recorded one-to-one proof, so
    #: the generic cross-account mirror heuristic must not re-raise them.
    covered_pairs: frozenset[tuple[str, str]] = frozenset()

    @property
    def by_pair(self) -> dict[tuple[str, str], _DeclaredDuplicate]:
        return {
            _pair_key(item.duplicate_claim_id, item.authoritative_claim_id): item
            for item in self.suppressions
        }


def _declared_duplicate_summary(
    claim: SourceClaim,
    observation_by_id: Mapping[str, IdentityObservation],
) -> tuple[str, str, str] | None:
    """The declared duplicate-summary target for a claim, or ``None``.

    Every observation backing the claim must carry the *same* declaration.  A
    claim whose members disagree is left alone rather than resolved on the
    strength of one member's attributes.
    """

    declarations = {
        (
            observation_by_id[observation_id].attribute(
                DUPLICATE_SUMMARY_TARGET_ATTRIBUTE
            ),
            observation_by_id[observation_id].attribute(
                DUPLICATE_SUMMARY_DECISION_ATTRIBUTE
            ),
            observation_by_id[observation_id].attribute(
                DUPLICATE_SUMMARY_MAP_ATTRIBUTE
            ),
        )
        for observation_id in claim.observation_ids
    }
    if len(declarations) != 1:
        return None
    target, decision_hash, map_hash = next(iter(declarations))
    if not target or not decision_hash or not map_hash:
        return None
    return (target, decision_hash, map_hash)


def _declared_duplicate_plan(
    *,
    policy: IdentityPolicy,
    claims: tuple[SourceClaim, ...],
    claim_by_id: Mapping[str, SourceClaim],
    observation_by_id: Mapping[str, IdentityObservation],
    selected_by_claim: Mapping[str, IdentityObservation],
    blocked_pairs: set[tuple[str, str]],
    lineage_claims: set[str],
) -> _DeclaredDuplicatePlan:
    """Resolve declared duplicate-summary accounts by one-to-one multiset match.

    A declared mapping says *account A restates account B*.  Only that explicit
    declaration admits a cross-account suppression here; nothing is inferred
    from descriptions, balances, or names.  Even with the declaration in hand a
    suppression is only planned when the two source multisets permit a unique
    occurrence mapping, so a legitimate extra repeat on either side keeps the
    whole bucket unresolved instead of silently dropping an event.
    """

    duplicates: dict[str, tuple[str, str, str]] = {}
    for claim in claims:
        declared = _declared_duplicate_summary(claim, observation_by_id)
        if declared is not None:
            duplicates[claim.claim_id] = declared
    if not duplicates:
        return _DeclaredDuplicatePlan()

    def usable(claim_id: str, *, authoritative: bool) -> bool:
        claim = claim_by_id[claim_id]
        members = [
            observation_by_id[observation_id]
            for observation_id in claim.observation_ids
        ]
        selected = selected_by_claim[claim_id]
        if claim_id in lineage_claims:
            return False
        if selected.signed_amount == 0:
            return False
        if any(member.status != "posted" for member in members):
            return False
        if any(
            member.trust_cutoff_day is not None
            and member.source_day > member.trust_cutoff_day
            for member in members
        ):
            return False
        # A transfer group is an explicit multi-leg economic statement; never
        # collapse one of its legs on the strength of an account-level map.
        if any(member.source_group_id for member in members):
            return False
        if authoritative and any(
            member.account_status == "excluded" for member in members
        ):
            return False
        return True

    authoritative_by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    for claim in claims:
        if claim.claim_id in duplicates:
            continue
        selected = selected_by_claim[claim.claim_id]
        if not selected.source_account_id:
            continue
        authoritative_by_key[
            (selected.source_family.casefold(), selected.source_account_id)
        ].append(claim.claim_id)

    buckets: dict[tuple[str, ...], tuple[list[str], list[str]]] = {}
    for duplicate_claim_id in sorted(duplicates):
        target, decision_hash, map_hash = duplicates[duplicate_claim_id]
        selected = selected_by_claim[duplicate_claim_id]
        if not usable(duplicate_claim_id, authoritative=False):
            continue
        family = selected.source_family.casefold()
        if selected.source_account_id == target:
            continue
        bucket_key = (
            family,
            selected.source_account_id,
            target,
            decision_hash,
            map_hash,
            selected.source_day.isoformat(),
            str(selected.signed_amount),
            selected.currency,
        )
        if bucket_key not in buckets:
            candidates = [
                claim_id
                for claim_id in sorted(authoritative_by_key.get((family, target), ()))
                if usable(claim_id, authoritative=True)
                and selected_by_claim[claim_id].source_day == selected.source_day
                and selected_by_claim[claim_id].signed_amount
                == selected.signed_amount
                and selected_by_claim[claim_id].currency == selected.currency
            ]
            buckets[bucket_key] = ([], candidates)
        buckets[bucket_key][0].append(duplicate_claim_id)

    suppressions: list[_DeclaredDuplicate] = []
    ambiguities: list[_AuthorityAmbiguity] = []
    covered_pairs: set[tuple[str, str]] = set()
    for bucket_key, (duplicate_ids, authoritative_ids) in sorted(buckets.items()):
        duplicate_ids = sorted(duplicate_ids)
        if not duplicate_ids or not authoritative_ids:
            continue
        participants = tuple(sorted(set(duplicate_ids) | set(authoritative_ids)))
        bucket_source_hashes = tuple(
            sorted(
                {
                    value
                    for claim_id in participants
                    for value in claim_by_id[claim_id].source_hashes
                }
            )
        )
        bucket_features = _feature_vector(
            declaredDuplicateSummary=True,
            duplicateSummaryPolicyVersion=DUPLICATE_SUMMARY_POLICY_VERSION,
            duplicateSummaryDecisionHash=bucket_key[3],
            duplicateSummaryMapHash=bucket_key[4],
            sourceDay=bucket_key[5],
            sameSignedAmountAndCurrency=True,
        )

        def description(claim_id: str) -> str:
            return normalized_description(selected_by_claim[claim_id].description)

        duplicate_by_description: dict[str, list[str]] = defaultdict(list)
        for claim_id in duplicate_ids:
            duplicate_by_description[description(claim_id)].append(claim_id)
        authoritative_by_description: dict[str, list[str]] = defaultdict(list)
        for claim_id in authoritative_ids:
            authoritative_by_description[description(claim_id)].append(claim_id)

        mapping: dict[str, str] = {}
        claimed: dict[str, str] = {}
        ambiguous = False
        for duplicate_description in sorted(duplicate_by_description):
            related = [
                candidate
                for candidate in sorted(authoritative_by_description)
                if _description_relation(duplicate_description, candidate, policy)
            ]
            if len(related) != 1 or related[0] in claimed:
                ambiguous = True
                break
            mapping[duplicate_description] = related[0]
            claimed[related[0]] = duplicate_description
        if ambiguous:
            ambiguities.append(
                _AuthorityAmbiguity(
                    claim_ids=participants,
                    rationale_code="ambiguous-declared-duplicate-description-mapping",
                    feature_vector=bucket_features,
                    proof=tuple(
                        sorted(
                            {
                                "authoritativeCount": len(authoritative_ids),
                                "lowerCount": len(duplicate_ids),
                                "bucketParticipantCount": len(participants),
                            }.items()
                        )
                    ),
                    source_hashes=bucket_source_hashes,
                )
            )
            continue

        planned: list[_DeclaredDuplicate] = []
        blocked = False
        for duplicate_description, target_description in sorted(mapping.items()):
            duplicate_group = sorted(duplicate_by_description[duplicate_description])
            authoritative_group = sorted(
                authoritative_by_description[target_description]
            )
            if len(duplicate_group) > len(authoritative_group):
                # A legitimate extra repeat only the duplicate side recorded:
                # multiplicity wins, so nothing in this group is suppressed.
                ambiguities.append(
                    _AuthorityAmbiguity(
                        claim_ids=tuple(
                            sorted(duplicate_group + authoritative_group)
                        ),
                        rationale_code="ambiguous-declared-duplicate-multiplicity",
                        feature_vector=bucket_features,
                        proof=tuple(
                            sorted(
                                {
                                    "authoritativeCount": len(authoritative_group),
                                    "lowerCount": len(duplicate_group),
                                    "bucketParticipantCount": len(participants),
                                }.items()
                            )
                        ),
                        source_hashes=bucket_source_hashes,
                    )
                )
                blocked = True
                continue
            for index, duplicate_claim_id in enumerate(duplicate_group):
                authoritative_claim_id = authoritative_group[index]
                if (
                    _pair_key(duplicate_claim_id, authoritative_claim_id)
                    in blocked_pairs
                ):
                    blocked = True
                    continue
                duplicate_observation = selected_by_claim[duplicate_claim_id]
                authoritative_observation = selected_by_claim[authoritative_claim_id]
                if (
                    duplicate_observation.currency
                    != authoritative_observation.currency
                    or duplicate_observation.signed_amount
                    != authoritative_observation.signed_amount
                    or duplicate_observation.source_day
                    != authoritative_observation.source_day
                ):
                    blocked = True
                    continue
                planned.append(
                    _DeclaredDuplicate(
                        duplicate_claim_id=duplicate_claim_id,
                        authoritative_claim_id=authoritative_claim_id,
                        feature_vector=_feature_vector(
                            declaredDuplicateSummary=True,
                            explicitDuplicateSummaryMapping=True,
                            duplicateSummaryPolicyVersion=(
                                DUPLICATE_SUMMARY_POLICY_VERSION
                            ),
                            duplicateSummaryDecisionHash=bucket_key[3],
                            duplicateSummaryMapHash=bucket_key[4],
                            duplicateSourceAccountHash=content_hash(bucket_key[1]),
                            authoritativeSourceAccountHash=content_hash(
                                bucket_key[2]
                            ),
                            suppressedClaimId=duplicate_claim_id,
                            authoritativeClaimId=authoritative_claim_id,
                            sameSignedAmountAndCurrency=True,
                            sameSourceDay=True,
                            sourceDay=bucket_key[5],
                            descriptionRelation=_description_relation(
                                duplicate_description, target_description, policy
                            ),
                            occurrenceIndex=index,
                            sourceFamilyPair="-".join(
                                sorted(
                                    (
                                        duplicate_observation.source_family,
                                        authoritative_observation.source_family,
                                    )
                                )
                            ),
                            categoryParticipates=False,
                            writerTimestampParticipates=False,
                        ),
                        proof=tuple(
                            sorted(
                                {
                                    "authoritativeCount": len(authoritative_group),
                                    "lowerCount": len(duplicate_group),
                                    "bucketParticipantCount": len(participants),
                                    "occurrenceIndex": index,
                                }.items()
                            )
                        ),
                    )
                )
        if blocked and not planned:
            continue
        suppressions.extend(planned)
        if not blocked and len(planned) == len(duplicate_ids):
            covered_pairs.update(
                _pair_key(duplicate_claim_id, authoritative_claim_id)
                for duplicate_claim_id in duplicate_ids
                for authoritative_claim_id in authoritative_ids
            )

    return _DeclaredDuplicatePlan(
        suppressions=tuple(
            sorted(
                suppressions,
                key=lambda item: (
                    item.duplicate_claim_id,
                    item.authoritative_claim_id,
                ),
            )
        ),
        ambiguities=tuple(
            sorted(ambiguities, key=lambda item: (item.rationale_code, item.claim_ids))
        ),
        covered_pairs=frozenset(covered_pairs),
    )


@dataclass(frozen=True, slots=True)
class _SharedTokenLink:
    left_claim_id: str
    right_claim_id: str
    feature_vector: tuple[tuple[str, str], ...]
    proof: tuple[tuple[str, int], ...]
    source_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SharedTokenPlan:
    links: tuple[_SharedTokenLink, ...] = ()
    ambiguities: tuple[_AuthorityAmbiguity, ...] = ()

    @property
    def by_pair(self) -> dict[tuple[str, str], _SharedTokenLink]:
        return {
            _pair_key(item.left_claim_id, item.right_claim_id): item
            for item in self.links
        }


def _scoped_provider_token(
    claim: SourceClaim,
    observation_by_id: Mapping[str, IdentityObservation],
) -> tuple[str, str, str] | None:
    """Return ``(token, scope hash, map hash)`` when every member agrees."""

    values: set[tuple[str, str, str]] = set()
    for observation_id in claim.observation_ids:
        observation = observation_by_id[observation_id]
        token = observation.attribute(PROVIDER_TOKEN_ATTRIBUTE) or ""
        scope_hash = observation.attribute(PROVIDER_TOKEN_SCOPE_ATTRIBUTE) or ""
        map_hash = observation.attribute(PROVIDER_TOKEN_SCOPE_MAP_ATTRIBUTE) or ""
        values.add((token, scope_hash, map_hash))
    if len(values) != 1:
        return None
    token, scope_hash, map_hash = next(iter(values))
    if not token or not scope_hash or not map_hash:
        return None
    return (token, scope_hash, map_hash)


def _shared_token_plan(
    *,
    claims: tuple[SourceClaim, ...],
    claim_by_id: Mapping[str, SourceClaim],
    observation_by_id: Mapping[str, IdentityObservation],
    selected_by_claim: Mapping[str, IdentityObservation],
    scopes: Mapping[str, ProviderTokenScope],
    blocked_pairs: set[tuple[str, str]],
    lineage_claims: set[str],
) -> _SharedTokenPlan:
    """Link two writers that recorded the *same* proven scoped provider token.

    The token alone proves nothing.  A link is only planned when a durable
    operator scope names both namespaces and the canonical account they belong
    to, both sides carry a proven stable identifier kind, the token is unique
    inside each namespace, and the economic tuple agrees within the declared
    settlement skew.  Token equality is never applied across providers globally.
    """

    if not scopes:
        return _SharedTokenPlan()

    tokens: dict[str, tuple[str, str, str]] = {}
    for claim in claims:
        scoped = _scoped_provider_token(claim, observation_by_id)
        if scoped is not None:
            tokens[claim.claim_id] = scoped
    if not tokens:
        return _SharedTokenPlan()

    def usable(claim_id: str) -> bool:
        claim = claim_by_id[claim_id]
        members = [
            observation_by_id[observation_id]
            for observation_id in claim.observation_ids
        ]
        selected = selected_by_claim[claim_id]
        if claim_id in lineage_claims:
            return False
        if selected.signed_amount == 0:
            return False
        if any(member.status != "posted" for member in members):
            return False
        if any(
            member.trust_cutoff_day is not None
            and member.source_day > member.trust_cutoff_day
            for member in members
        ):
            return False
        if any(member.source_group_id for member in members):
            return False
        if any(member.account_status == "excluded" for member in members):
            return False
        return True

    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for claim_id in sorted(tokens):
        token, scope_hash, _map_hash = tokens[claim_id]
        if scope_hash not in scopes:
            continue
        if not usable(claim_id):
            continue
        buckets[(scope_hash, token)].append(claim_id)

    links: list[_SharedTokenLink] = []
    ambiguities: list[_AuthorityAmbiguity] = []
    for (scope_hash, token), members in sorted(buckets.items()):
        scope = scopes[scope_hash]
        participants = tuple(sorted(members))
        source_hashes = tuple(
            sorted(
                {
                    value
                    for claim_id in participants
                    for value in claim_by_id[claim_id].source_hashes
                }
            )
        )
        by_side: dict[tuple[str, str], list[str]] = {
            scope.left.key: [],
            scope.right.key: [],
        }
        misplaced = False
        for claim_id in participants:
            selected = selected_by_claim[claim_id]
            side = (selected.source_family.casefold(), selected.source_account_id)
            if side not in by_side:
                misplaced = True
                break
            if selected.canonical_account_id != scope.canonical_account_id:
                misplaced = True
                break
            by_side[side].append(claim_id)
        left_ids = sorted(by_side[scope.left.key])
        right_ids = sorted(by_side[scope.right.key])
        features = _feature_vector(
            scopedSharedProviderToken=True,
            providerTokenScopePolicyVersion=PROVIDER_TOKEN_SCOPE_POLICY_VERSION,
            providerTokenScopeHash=scope_hash,
            providerTokenScopeMapHash=scope.map_hash,
            providerTokenHash=content_hash(token),
            canonicalAccountHash=content_hash(scope.canonical_account_id),
            maxDaySkewDays=scope.max_day_skew,
        )
        proof = tuple(
            sorted(
                {
                    "leftNamespaceCount": len(left_ids),
                    "rightNamespaceCount": len(right_ids),
                    "sharedTokenBucketCount": len(participants),
                    "dateDistanceDays": (
                        _days(
                            selected_by_claim[left_ids[0]],
                            selected_by_claim[right_ids[0]],
                        )
                        if len(left_ids) == 1 and len(right_ids) == 1
                        else 0
                    ),
                    "maxDaySkewDays": scope.max_day_skew,
                }.items()
            )
        )
        if misplaced:
            ambiguities.append(
                _AuthorityAmbiguity(
                    claim_ids=participants,
                    rationale_code="ambiguous-shared-token-scope-membership",
                    feature_vector=features,
                    proof=proof,
                    source_hashes=source_hashes,
                )
            )
            continue
        if not left_ids or not right_ids:
            continue
        if len(left_ids) != 1 or len(right_ids) != 1:
            # A stable token must be unique inside its own namespace.  A repeat
            # means the namespace was mis-declared, so nothing is linked.
            ambiguities.append(
                _AuthorityAmbiguity(
                    claim_ids=participants,
                    rationale_code="ambiguous-shared-token-multiplicity",
                    feature_vector=features,
                    proof=proof,
                    source_hashes=source_hashes,
                )
            )
            continue
        left_id, right_id = left_ids[0], right_ids[0]
        left = selected_by_claim[left_id]
        right = selected_by_claim[right_id]
        if _pair_key(left_id, right_id) in blocked_pairs:
            continue
        if left.currency != right.currency or left.signed_amount != right.signed_amount:
            ambiguities.append(
                _AuthorityAmbiguity(
                    claim_ids=participants,
                    rationale_code="ambiguous-shared-token-economic-conflict",
                    feature_vector=features,
                    proof=proof,
                    source_hashes=source_hashes,
                )
            )
            continue
        distance = _days(left, right)
        if distance > scope.max_day_skew:
            ambiguities.append(
                _AuthorityAmbiguity(
                    claim_ids=participants,
                    rationale_code="ambiguous-shared-token-date-window",
                    feature_vector=features,
                    proof=proof,
                    source_hashes=source_hashes,
                )
            )
            continue
        links.append(
            _SharedTokenLink(
                left_claim_id=left_id,
                right_claim_id=right_id,
                feature_vector=_feature_vector(
                    scopedSharedProviderToken=True,
                    providerTokenScopePolicyVersion=(
                        PROVIDER_TOKEN_SCOPE_POLICY_VERSION
                    ),
                    providerTokenScopeHash=scope_hash,
                    providerTokenScopeMapHash=scope.map_hash,
                    providerTokenHash=content_hash(token),
                    canonicalAccountHash=content_hash(scope.canonical_account_id),
                    sameCanonicalAccount=True,
                    sameSignedAmountAndCurrency=True,
                    sameSourceDay=left.source_day == right.source_day,
                    dateDistanceDays=distance,
                    maxDaySkewDays=scope.max_day_skew,
                    sourceFamilyPair="-".join(
                        sorted((left.source_family, right.source_family))
                    ),
                    categoryParticipates=False,
                    writerTimestampParticipates=False,
                ),
                proof=proof,
                source_hashes=source_hashes,
            )
        )

    return _SharedTokenPlan(
        links=tuple(
            sorted(links, key=lambda item: (item.left_claim_id, item.right_claim_id))
        ),
        ambiguities=tuple(
            sorted(ambiguities, key=lambda item: (item.rationale_code, item.claim_ids))
        ),
    )


def resolve_identity(
    observations: Iterable[IdentityObservation],
    *,
    policy: IdentityPolicy = DEFAULT_POLICY,
    overrides: Iterable[HumanOverride] = (),
    token_scopes: Iterable[ProviderTokenScope] = (),
) -> IdentityResolution:
    ordered_observations = tuple(
        sorted(observations, key=lambda item: item.observation_id)
    )
    if len({item.observation_id for item in ordered_observations}) != len(
        ordered_observations
    ):
        raise ValueError("observation IDs must be unique")

    scope_by_hash: dict[str, ProviderTokenScope] = {}
    for scope in token_scopes:
        if scope.scope_hash in scope_by_hash:
            raise ValueError("provider token scope declared twice")
        scope_by_hash[scope.scope_hash] = scope

    input_hash = content_hash(
        [_observation_identity_document(item) for item in ordered_observations]
    )
    all_overrides = tuple(
        sorted(
            overrides,
            key=lambda item: (
                item.decided_at,
                item.version,
                item.override_id,
            ),
        )
    )
    current_overrides = _latest_overrides(all_overrides)
    generation_hash = content_hash(
        {
            "policyHash": policy.policy_hash,
            "inputHash": input_hash,
            "overrides": [
                {
                    "overrideId": item.override_id,
                    "version": item.version,
                    "action": item.action,
                    "claimIds": list(item.claim_ids),
                    "rationaleHash": item.rationale_hash,
                    "supersedes": item.supersedes_override_id,
                }
                for item in current_overrides
            ],
            **(
                {"providerTokenScopes": sorted(scope_by_hash)}
                if scope_by_hash
                else {}
            ),
        }
    )

    observations_by_scope: dict[str, list[IdentityObservation]] = defaultdict(list)
    for observation in ordered_observations:
        observations_by_scope[content_hash(_claim_scope(observation))].append(
            observation
        )

    claims = []
    observation_by_id = {item.observation_id: item for item in ordered_observations}
    for claim_id, members in sorted(observations_by_scope.items()):
        selected = max(members, key=_claim_selection_key)
        claims.append(
            SourceClaim(
                claim_id=claim_id,
                source_family=selected.source_family,
                canonical_account_hash=content_hash(selected.canonical_account_id),
                source_account_hash=content_hash(selected.source_account_id),
                source_connection_hash=content_hash(selected.source_connection_id),
                provider_identity_hash=(
                    content_hash(selected.provider_transaction_id)
                    if selected.provider_transaction_id
                    else None
                ),
                provider_id_kind=selected.provider_id_kind,
                observation_ids=tuple(sorted(item.observation_id for item in members)),
                selected_observation_id=selected.observation_id,
                source_hashes=tuple(sorted({item.source_hash for item in members})),
            )
        )
    claims_tuple = tuple(claims)
    claim_by_id = {item.claim_id: item for item in claims_tuple}
    selected_by_claim = {
        claim.claim_id: observation_by_id[claim.selected_observation_id]
        for claim in claims_tuple
    }
    claim_by_observation = {
        observation_id: claim.claim_id
        for claim in claims_tuple
        for observation_id in claim.observation_ids
    }
    provider_claims: dict[str, set[str]] = defaultdict(set)
    for claim in claims_tuple:
        selected = selected_by_claim[claim.claim_id]
        if selected.provider_transaction_id:
            provider_claims[selected.provider_transaction_id].add(claim.claim_id)

    def referenced_claim(observation: IdentityObservation, name: str) -> str | None:
        reference = observation.attribute(name)
        if not reference:
            return None
        if reference in claim_by_observation:
            return claim_by_observation[reference]
        matches = provider_claims.get(reference, set())
        return next(iter(matches)) if len(matches) == 1 else None

    pair_ids: set[tuple[str, str]] = set()
    lineage_claims: set[str] = set()
    duplicate_buckets: dict[tuple[object, ...], list[str]] = defaultdict(list)
    absolute_amount_buckets: dict[tuple[str, Decimal], list[str]] = defaultdict(list)
    source_group_buckets: dict[str, list[str]] = defaultdict(list)
    for claim in claims_tuple:
        selected = selected_by_claim[claim.claim_id]
        duplicate_buckets[
            (
                selected.canonical_account_id,
                selected.currency,
                selected.signed_amount,
            )
        ].append(claim.claim_id)
        if selected.signed_amount:
            absolute_amount_buckets[
                (selected.currency, abs(selected.signed_amount))
            ].append(claim.claim_id)
        if selected.source_group_id:
            source_group_buckets[selected.source_group_id].append(claim.claim_id)
        for attribute in (
            "correction_of",
            "counterpart_id",
            "pending_of",
            "provider_error_of",
            "reversal_of",
        ):
            target = referenced_claim(selected, attribute)
            if target and target != claim.claim_id:
                pair_ids.add(_pair_key(claim.claim_id, target))
                lineage_claims.add(claim.claim_id)
                lineage_claims.add(target)
    for bucket in duplicate_buckets.values():
        for left_id, right_id in combinations(bucket, 2):
            if (
                _days(
                    selected_by_claim[left_id],
                    selected_by_claim[right_id],
                )
                <= policy.duplicate_candidate_window_days
            ):
                pair_ids.add(_pair_key(left_id, right_id))
    for bucket in absolute_amount_buckets.values():
        for left_id, right_id in combinations(bucket, 2):
            left = selected_by_claim[left_id]
            right = selected_by_claim[right_id]
            if (
                left.canonical_account_id != right.canonical_account_id
                and _days(left, right) <= policy.transfer_window_days
            ):
                pair_ids.add(_pair_key(left_id, right_id))
    for bucket in source_group_buckets.values():
        pair_ids.update(_pair_key(*pair) for pair in combinations(bucket, 2))

    override_blocked_pairs: set[tuple[str, str]] = set()
    for override in current_overrides:
        if override.action == "merge":
            continue
        for left_id, right_id in combinations(override.claim_ids, 2):
            if left_id in claim_by_id and right_id in claim_by_id:
                override_blocked_pairs.add(_pair_key(left_id, right_id))

    declared_plan = _declared_duplicate_plan(
        policy=policy,
        claims=claims_tuple,
        claim_by_id=claim_by_id,
        observation_by_id=observation_by_id,
        selected_by_claim=selected_by_claim,
        blocked_pairs=override_blocked_pairs,
        lineage_claims=lineage_claims,
    )
    declared_by_pair = declared_plan.by_pair
    pair_ids.update(declared_by_pair)

    token_plan = _shared_token_plan(
        claims=claims_tuple,
        claim_by_id=claim_by_id,
        observation_by_id=observation_by_id,
        selected_by_claim=selected_by_claim,
        scopes=scope_by_hash,
        blocked_pairs=override_blocked_pairs,
        lineage_claims=lineage_claims,
    )
    token_by_pair = token_plan.by_pair
    pair_ids.update(token_by_pair)

    edges: dict[tuple[RelationKind, str, str], IdentityEdge] = {}
    automatic_families = set(policy.automatic_source_families)
    for left_id, right_id in sorted(pair_ids):
        left_claim = claim_by_id[left_id]
        right_claim = claim_by_id[right_id]
        left = selected_by_claim[left_claim.claim_id]
        right = selected_by_claim[right_claim.claim_id]
        source_hashes = (*left_claim.source_hashes, *right_claim.source_hashes)
        description_relation = _description_relation(
            left.description, right.description, policy
        )
        same_amount = (
            left.signed_amount == right.signed_amount
            and left.currency == right.currency
        )
        opposite_amount = (
            left.signed_amount != 0
            and right.signed_amount != 0
            and left.signed_amount == -right.signed_amount
            and left.currency == right.currency
        )
        same_day = left.source_day == right.source_day
        same_account = left.canonical_account_id == right.canonical_account_id
        distinct_families = left.source_family != right.source_family
        family_pair = "-".join(sorted((left.source_family, right.source_family)))
        explicit_group = bool(
            left.source_group_id and left.source_group_id == right.source_group_id
        )
        compatible_source_group = not (
            left.source_group_id
            and right.source_group_id
            and left.source_group_id != right.source_group_id
        )
        explicit_counterpart = (
            referenced_claim(left, "counterpart_id") == right_claim.claim_id
            or referenced_claim(right, "counterpart_id") == left_claim.claim_id
        )
        explicit_pending = (
            referenced_claim(left, "pending_of") == right_claim.claim_id
            or referenced_claim(right, "pending_of") == left_claim.claim_id
        )
        explicit_correction = (
            referenced_claim(left, "correction_of") == right_claim.claim_id
            or referenced_claim(right, "correction_of") == left_claim.claim_id
        )
        explicit_reversal = (
            referenced_claim(left, "reversal_of") == right_claim.claim_id
            or referenced_claim(right, "reversal_of") == left_claim.claim_id
        )
        explicit_provider_error = (
            referenced_claim(left, "provider_error_of") == right_claim.claim_id
            or referenced_claim(right, "provider_error_of") == left_claim.claim_id
        )

        if (
            not same_account
            and opposite_amount
            and _days(left, right) <= policy.transfer_window_days
        ):
            kind = (
                RelationKind.TRANSFER
                if explicit_group or explicit_counterpart
                else RelationKind.TRANSFER_CANDIDATE
            )
            edge = _edge(
                kind,
                left_claim,
                right_claim,
                _feature_vector(
                    differentAccounts=True,
                    oppositeSignedAmount=True,
                    dateDistanceDays=_days(left, right),
                    explicitSourceGroup=explicit_group,
                    explicitCounterpart=explicit_counterpart,
                    sourceFamilyPair=family_pair,
                ),
                source_hashes,
                confidence=(
                    AUTO_CONFIDENCE_BASIS_POINTS
                    if kind is RelationKind.TRANSFER
                    else REVIEW_CONFIDENCE_BASIS_POINTS
                ),
                automatic=kind is RelationKind.TRANSFER,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        if explicit_group and not same_account:
            edge = _edge(
                RelationKind.MIRROR_CANDIDATE,
                left_claim,
                right_claim,
                _feature_vector(
                    explicitSourceGroup=True,
                    sourceGroupConflict=True,
                    sourceFamilyPair=family_pair,
                ),
                source_hashes,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        if explicit_reversal:
            edge = _edge(
                RelationKind.REVERSAL,
                left_claim,
                right_claim,
                _feature_vector(
                    explicitReversal=True,
                    sourceFamilyPair=family_pair,
                ),
                source_hashes,
                confidence=AUTO_CONFIDENCE_BASIS_POINTS,
                automatic=True,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        if explicit_correction:
            edge = _edge(
                RelationKind.CORRECTION,
                left_claim,
                right_claim,
                _feature_vector(
                    explicitCorrection=True,
                    sourceFamilyPair=family_pair,
                ),
                source_hashes,
                confidence=AUTO_CONFIDENCE_BASIS_POINTS,
                automatic=True,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        if (
            explicit_pending
            and same_account
            and same_amount
            and {left.status, right.status} == {"pending", "posted"}
        ):
            edge = _edge(
                RelationKind.PENDING_TRANSITION,
                left_claim,
                right_claim,
                _feature_vector(
                    explicitPendingLineage=True,
                    sameCanonicalAccount=True,
                    sameSignedAmountAndCurrency=True,
                    sourceFamilyPair=family_pair,
                ),
                source_hashes,
                confidence=AUTO_CONFIDENCE_BASIS_POINTS,
                automatic=True,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        if (
            explicit_provider_error
            and not same_account
            and same_amount
            and _days(left, right) <= policy.duplicate_candidate_window_days
            and description_relation is not None
        ):
            edge = _edge(
                RelationKind.MIRRORED_PROVIDER_ERROR,
                left_claim,
                right_claim,
                _feature_vector(
                    explicitProviderError=True,
                    sameSignedAmountAndCurrency=True,
                    sameSourceDay=True,
                    dateDistanceDays=_days(left, right),
                    descriptionRelation=description_relation,
                    sourceFamilyPair=family_pair,
                ),
                source_hashes,
                confidence=AUTO_CONFIDENCE_BASIS_POINTS,
                automatic=True,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        shared_token = token_by_pair.get(_pair_key(left_id, right_id))
        if (
            shared_token is not None
            and same_account
            and same_amount
            and not opposite_amount
        ):
            edge = _edge(
                RelationKind.DUPLICATE_CANDIDATE,
                left_claim,
                right_claim,
                shared_token.feature_vector,
                source_hashes,
                confidence=AUTO_CONFIDENCE_BASIS_POINTS,
                automatic=True,
                proof=shared_token.proof,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        declared = declared_by_pair.get(_pair_key(left_id, right_id))
        if declared is not None and same_amount and same_day and not opposite_amount:
            edge = _edge(
                RelationKind.MIRRORED_PROVIDER_ERROR,
                left_claim,
                right_claim,
                declared.feature_vector,
                source_hashes,
                confidence=AUTO_CONFIDENCE_BASIS_POINTS,
                automatic=True,
                proof=declared.proof,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        if _pair_key(left_id, right_id) in declared_plan.covered_pairs:
            # Already accounted for by the declared one-to-one occurrence proof
            # for this account pair, day, amount and currency.
            continue

        if (
            not same_account
            and same_amount
            and same_day
            and description_relation is not None
            and left.source_connection_id
            and left.source_connection_id == right.source_connection_id
        ):
            edge = _edge(
                RelationKind.MIRROR_CANDIDATE,
                left_claim,
                right_claim,
                _feature_vector(
                    sameConnection=True,
                    sameSignedAmountAndCurrency=True,
                    sameSourceDay=True,
                    descriptionRelation=description_relation,
                    sourceFamilyPair=family_pair,
                ),
                source_hashes,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge
            continue

        known_distinct_families = (
            distinct_families
            and left.source_family in automatic_families
            and right.source_family in automatic_families
        )
        if (
            same_account
            and same_amount
            and _days(left, right) <= policy.duplicate_candidate_window_days
            and description_relation is not None
            and known_distinct_families
            and compatible_source_group
            and left.trusted
            and right.trusted
        ):
            features = _feature_vector(
                sameCanonicalAccount=True,
                sameSignedAmountAndCurrency=True,
                sameSourceDay=same_day,
                dateDistanceDays=_days(left, right),
                descriptionRelation=description_relation,
                distinctKnownSourceFamilies=True,
                sourceFamilyPair=family_pair,
                sourceConnectionsDiffer=(
                    left.source_connection_id != right.source_connection_id
                ),
                distinctImportLineage=(
                    left.import_lineage_hash is not None
                    and right.import_lineage_hash is not None
                    and left.import_lineage_hash != right.import_lineage_hash
                ),
                compatibleSourceGroup=True,
                categoryParticipates=False,
                writerTimestampParticipates=False,
            )
            edge = _edge(
                RelationKind.DUPLICATE_CANDIDATE,
                left_claim,
                right_claim,
                features,
                source_hashes,
            )
            edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge

    duplicate_edges = [
        edge for edge in edges.values() if edge.kind is RelationKind.DUPLICATE_CANDIDATE
    ]
    degrees: Counter[str] = Counter()
    for edge in duplicate_edges:
        degrees[edge.left_claim_id] += 1
        degrees[edge.right_claim_id] += 1
    components = _connected_components(
        claim_by_id,
        duplicate_edges,
    )
    component_size = {
        claim_id: len(component) for component in components for claim_id in component
    }
    for edge in duplicate_edges:
        if dict(edge.feature_vector).get("scopedSharedProviderToken") == "true":
            # Explicit scoped lineage: the token scope already proved a unique
            # occurrence inside both namespaces, so a graph-degree heuristic
            # must not downgrade or re-score it.
            continue
        proof = tuple(
            sorted(
                {
                    "leftDegree": degrees[edge.left_claim_id],
                    "rightDegree": degrees[edge.right_claim_id],
                    "componentSize": component_size[edge.left_claim_id],
                    "competingCandidateCount": (
                        degrees[edge.left_claim_id] + degrees[edge.right_claim_id] - 2
                    ),
                }.items()
            )
        )
        cardinality_is_unique = dict(proof) == {
            "competingCandidateCount": 0,
            "componentSize": 2,
            "leftDegree": 1,
            "rightDegree": 1,
        }
        same_day = dict(edge.feature_vector).get("sameSourceDay") == "true"
        confidence = _cross_source_score(
            same_day=same_day,
            one_to_one=cardinality_is_unique,
        )
        updated = replace(
            edge,
            confidence_basis_points=confidence,
            # Description agreement is candidate evidence, not source lineage.
            # Only scoped provider identity or proven source coverage may merge
            # cross-source claims automatically.
            automatic=False,
            competing_candidate_proof=proof,
        )
        edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = updated
    duplicate_edges = [
        edge for edge in edges.values() if edge.kind is RelationKind.DUPLICATE_CANDIDATE
    ]

    blocked_pairs: set[tuple[str, str]] = set()
    declared_transfer_pairs: set[tuple[str, str]] = set()
    union = _UnionFind(claim_by_id)
    for override in current_overrides:
        unknown = set(override.claim_ids) - set(claim_by_id)
        if unknown:
            raise ValueError("override references an unknown source claim")
        if override.action == "merge":
            first, *rest = override.claim_ids
            for claim_id in rest:
                union.union(first, claim_id)
    human_roots = {claim_id: union.find(claim_id) for claim_id in claim_by_id}
    for override in current_overrides:
        if override.action == "merge":
            continue
        for left_id, right_id in combinations(override.claim_ids, 2):
            pair = _pair_key(left_id, right_id)
            blocked_pairs.add(pair)
            if override.action == "transfer":
                declared_transfer_pairs.add(pair)
    if any(union.find(left) == union.find(right) for left, right in blocked_pairs):
        raise ValueError("human identity overrides contain a transitive conflict")

    authority_plan = _source_authority_plan(
        policy=policy,
        claims=claims_tuple,
        claim_by_id=claim_by_id,
        observation_by_id=observation_by_id,
        edges=edges,
        blocked_pairs=blocked_pairs,
        declared_transfer_pairs=declared_transfer_pairs,
        observations=ordered_observations,
    )
    for suppression in authority_plan.suppressions:
        left_claim = claim_by_id[suppression.suppressed_claim_id]
        right_claim = claim_by_id[suppression.authoritative_claim_id]
        edge = _edge(
            RelationKind.SOURCE_SUPPRESSED,
            left_claim,
            right_claim,
            suppression.feature_vector,
            suppression.source_hashes,
            confidence=AUTO_CONFIDENCE_BASIS_POINTS,
            automatic=True,
            proof=suppression.proof,
        )
        edges[(edge.kind, edge.left_claim_id, edge.right_claim_id)] = edge

    explicit_same_scope_pairs = {
        _pair_key(edge.left_claim_id, edge.right_claim_id)
        for edge in edges.values()
        if edge.automatic
        and edge.kind
        in {
            RelationKind.PENDING_TRANSITION,
            RelationKind.MIRRORED_PROVIDER_ERROR,
        }
    }
    relationship_barriers = {
        _pair_key(edge.left_claim_id, edge.right_claim_id)
        for edge in edges.values()
        if edge.kind
        in {
            RelationKind.TRANSFER,
            RelationKind.TRANSFER_CANDIDATE,
            RelationKind.CORRECTION,
            RelationKind.REVERSAL,
            RelationKind.MIRROR_CANDIDATE,
        }
    } | declared_transfer_pairs

    accepted_automatic_edge_hashes: set[str] = set()
    rejected_automatic_edge_hashes: set[str] = set()
    human_rejected_edge_hashes: set[str] = set()
    component_conflicts: list[_AuthorityAmbiguity] = []
    automatic_edges = sorted(
        (
            edge
            for edge in edges.values()
            if edge.automatic and edge.kind in _AUTOMATIC_MERGE_KINDS
        ),
        key=_automatic_merge_priority,
    )
    for edge in automatic_edges:
        pair = _pair_key(edge.left_claim_id, edge.right_claim_id)
        if (
            pair in blocked_pairs
            or human_roots[edge.left_claim_id]
            == human_roots[edge.right_claim_id]
            or _merge_barrier_conflicts(
                union,
                edge.left_claim_id,
                edge.right_claim_id,
                blocked_pairs,
            )
        ):
            rejected_automatic_edge_hashes.add(edge.edge_hash)
            human_rejected_edge_hashes.add(edge.edge_hash)
            continue
        relationship_conflicts = _merge_barrier_conflicts(
            union,
            edge.left_claim_id,
            edge.right_claim_id,
            relationship_barriers,
        )
        source_occurrence_conflicts = _source_occurrence_conflicts(
            union,
            edge.left_claim_id,
            edge.right_claim_id,
            selected_by_claim,
            explicit_same_scope_pairs,
        )
        structural_conflicts = tuple(
            sorted(
                {
                    *relationship_conflicts,
                    *source_occurrence_conflicts,
                }
            )
        )
        if structural_conflicts:
            rejected_automatic_edge_hashes.add(edge.edge_hash)
            conflict_claim_ids = tuple(
                sorted(
                    {
                        edge.left_claim_id,
                        edge.right_claim_id,
                        *(
                            claim_id
                            for conflict in structural_conflicts
                            for claim_id in conflict
                        ),
                    }
                )
            )
            component_conflicts.append(
                _AuthorityAmbiguity(
                    claim_ids=conflict_claim_ids,
                    rationale_code="ambiguous-automatic-component-collapse",
                    feature_vector=_feature_vector(
                        rejectedRelationKind=edge.kind.value,
                        preserveDistinctConflict=False,
                        sourceOccurrenceConflict=bool(
                            source_occurrence_conflicts
                        ),
                        relationshipConflict=bool(relationship_conflicts),
                        componentConstraintPolicyVersion=POLICY_VERSION,
                    ),
                    proof=tuple(
                        sorted(
                            {
                                "conflictingBarrierCount": len(
                                    structural_conflicts
                                ),
                                "componentClaimCount": len(conflict_claim_ids),
                            }.items()
                        )
                    ),
                    source_hashes=tuple(
                        sorted(
                            {
                                source_hash
                                for claim_id in conflict_claim_ids
                                for source_hash in claim_by_id[
                                    claim_id
                                ].source_hashes
                            }
                        )
                    ),
                )
            )
            continue
        union.union(edge.left_claim_id, edge.right_claim_id)
        accepted_automatic_edge_hashes.add(edge.edge_hash)

    if rejected_automatic_edge_hashes:
        edges = {
            key: (
                replace(
                    edge,
                    confidence_basis_points=REVIEW_CONFIDENCE_BASIS_POINTS,
                    automatic=False,
                )
                if edge.edge_hash in rejected_automatic_edge_hashes
                else edge
            )
            for key, edge in edges.items()
        }

    accepted_suppression_pairs = {
        _pair_key(edge.left_claim_id, edge.right_claim_id)
        for edge in automatic_edges
        if edge.kind is RelationKind.SOURCE_SUPPRESSED
        and edge.edge_hash in accepted_automatic_edge_hashes
    }
    kept_suppressions = tuple(
        item
        for item in authority_plan.suppressions
        if _pair_key(item.suppressed_claim_id, item.authoritative_claim_id)
        in accepted_suppression_pairs
    )
    authority_plan = replace(
        authority_plan,
        suppressions=kept_suppressions,
        ambiguities=tuple(
            sorted(
                (*authority_plan.ambiguities, *component_conflicts),
                key=lambda item: (item.rationale_code, item.claim_ids),
            )
        ),
        resolved_claims=frozenset(
            claim_id
            for item in kept_suppressions
            for claim_id in (
                item.suppressed_claim_id,
                item.authoritative_claim_id,
            )
        ),
        preferred_observations=frozenset(
            observation_id
            for item in kept_suppressions
            for observation_id in claim_by_id[
                item.authoritative_claim_id
            ].observation_ids
        ),
    )
    if any(
        union.find(left_id) == union.find(right_id)
        for left_id, right_id in blocked_pairs
    ):
        raise ValueError("identity component violated a human cannot-link decision")
    if any(
        union.find(left_id) == union.find(right_id)
        for left_id, right_id in relationship_barriers
    ):
        raise ValueError("identity component violated a relationship constraint")
    final_components: dict[str, list[str]] = defaultdict(list)
    for claim_id in claim_by_id:
        final_components[union.find(claim_id)].append(claim_id)
    for component in final_components.values():
        by_scope: dict[tuple[str, str], list[str]] = defaultdict(list)
        for claim_id in component:
            selected = selected_by_claim[claim_id]
            by_scope[
                (
                    selected.source_family.casefold(),
                    selected.canonical_account_id,
                )
            ].append(claim_id)
        for same_scope_claims in by_scope.values():
            for left_id, right_id in combinations(same_scope_claims, 2):
                if (
                    human_roots[left_id] != human_roots[right_id]
                    and _pair_key(left_id, right_id)
                    not in explicit_same_scope_pairs
                ):
                    raise ValueError(
                        "identity component violated occurrence conservation"
                    )
    duplicate_edges = [
        edge
        for edge in edges.values()
        if edge.kind is RelationKind.DUPLICATE_CANDIDATE
    ]

    event_claims: dict[str, list[str]] = defaultdict(list)
    for claim_id in claim_by_id:
        event_claims[union.find(claim_id)].append(claim_id)
    canonical_events = []
    canonical_by_claim: dict[str, str] = {}
    for member_claim_ids in sorted(
        (tuple(sorted(values)) for values in event_claims.values())
    ):
        member_observations = tuple(
            sorted(
                observation_id
                for claim_id in member_claim_ids
                for observation_id in claim_by_id[claim_id].observation_ids
            )
        )
        event_observations = [
            observation_by_id[observation_id] for observation_id in member_observations
        ]
        selected = max(
            event_observations,
            key=lambda item: _event_selection_key(
                item, policy, authority_plan.preferred_observations
            ),
        )
        description_source = max(
            event_observations,
            key=lambda item: _description_selection_key(item, policy),
        )
        category_candidates = [
            item for item in event_observations if item.category.strip()
        ]
        category = (
            max(
                category_candidates,
                key=lambda item: (
                    _category_rank(policy, item.source_family),
                    item.source_hash,
                ),
            ).category
            if category_candidates
            else ""
        )
        source_groups = sorted(
            {
                item.source_group_id
                for item in event_observations
                if item.source_group_id
            }
        )
        event_id = content_hash(
            {
                "kind": "canonical-identity-event",
                "memberClaimIds": member_claim_ids,
            }
        )
        event = CanonicalEvent(
            canonical_event_id=event_id,
            canonical_account_hash=content_hash(selected.canonical_account_id),
            selected_observation_id=selected.observation_id,
            member_claim_ids=member_claim_ids,
            member_observation_ids=member_observations,
            source_day=selected.source_day,
            signed_amount=selected.signed_amount,
            currency=selected.currency,
            description=description_source.description,
            status=selected.status if selected.trusted else "excluded",
            category=category,
            source_group_id=(source_groups[0] if len(source_groups) == 1 else ""),
            trusted=selected.trusted,
        )
        canonical_events.append(event)
        for claim_id in member_claim_ids:
            canonical_by_claim[claim_id] = event_id
    canonical_events_tuple = tuple(
        sorted(canonical_events, key=lambda item: item.canonical_event_id)
    )

    relationships = []
    for edge in sorted(edges.values(), key=lambda item: item.edge_hash):
        if edge.kind not in {
            RelationKind.TRANSFER,
            RelationKind.TRANSFER_CANDIDATE,
            RelationKind.CORRECTION,
            RelationKind.REVERSAL,
            RelationKind.MIRROR_CANDIDATE,
        }:
            continue
        left_event = canonical_by_claim[edge.left_claim_id]
        right_event = canonical_by_claim[edge.right_claim_id]
        if left_event == right_event:
            continue
        relationships.append(
            EventRelationship(
                relationship_hash=content_hash(
                    {
                        "kind": edge.kind.value,
                        "canonicalEventIds": sorted((left_event, right_event)),
                        "sourceEdgeHash": edge.edge_hash,
                    }
                ),
                kind=edge.kind,
                left_canonical_event_id=min(left_event, right_event),
                right_canonical_event_id=max(left_event, right_event),
                source_edge_hash=edge.edge_hash,
            )
        )
    for override in current_overrides:
        if override.action != "transfer":
            continue
        event_ids = tuple(
            sorted({canonical_by_claim[claim_id] for claim_id in override.claim_ids})
        )
        for left_event, right_event in combinations(event_ids, 2):
            relationships.append(
                EventRelationship(
                    relationship_hash=content_hash(
                        {
                            "kind": RelationKind.TRANSFER.value,
                            "canonicalEventIds": [left_event, right_event],
                            "humanOverrideId": override.override_id,
                        }
                    ),
                    kind=RelationKind.TRANSFER,
                    left_canonical_event_id=left_event,
                    right_canonical_event_id=right_event,
                    source_edge_hash=content_hash(
                        {
                            "kind": "human-override",
                            "overrideId": override.override_id,
                        }
                    ),
                )
            )

    decisions = []
    for claim in claims_tuple:
        members = [
            observation_by_id[observation_id]
            for observation_id in claim.observation_ids
        ]
        if len(members) < 2:
            continue
        statuses = {item.status for item in members}
        economics = {
            (
                item.source_day,
                item.signed_amount,
                item.currency,
                normalized_description(item.description),
            )
            for item in members
        }
        lifecycle_outcomes = []
        if {"pending", "posted"} <= statuses:
            lifecycle_outcomes.append(
                ("same-id-pending-posted", DecisionOutcome.LINK_PENDING)
            )
        if len(economics) > 1:
            lifecycle_outcomes.append(
                ("same-id-correction", DecisionOutcome.LINK_CORRECTION)
            )
        if not lifecycle_outcomes:
            lifecycle_outcomes.append(
                (
                    "exact-scoped-source-identity",
                    DecisionOutcome.MERGE_OBSERVATIONS,
                )
            )
        for rationale, outcome in lifecycle_outcomes:
            decisions.append(
                _make_decision(
                    generation_hash=generation_hash,
                    policy=policy,
                    outcome=outcome,
                    confidence_tier=ConfidenceTier.EXACT_SCOPED_IDENTITY,
                    confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                    rationale_code=rationale,
                    claim_ids=(claim.claim_id,),
                    observation_ids=claim.observation_ids,
                    canonical_event_ids=(canonical_by_claim[claim.claim_id],),
                    feature_vector=_feature_vector(
                        exactScopedProviderIdentity=True,
                        providerIdKind=claim.provider_id_kind,
                        sourceFamily=claim.source_family,
                    ),
                    competing_candidate_proof=(
                        ("competingCandidateCount", 0),
                        ("sourceClaimCount", 1),
                    ),
                    source_hashes=claim.source_hashes,
                )
            )

    for edge in sorted(edges.values(), key=lambda item: item.edge_hash):
        claim_ids = (edge.left_claim_id, edge.right_claim_id)
        observation_ids = tuple(
            sorted(
                observation_id
                for claim_id in claim_ids
                for observation_id in claim_by_id[claim_id].observation_ids
            )
        )
        canonical_ids = tuple(
            sorted({canonical_by_claim[claim_id] for claim_id in claim_ids})
        )
        if (
            edge.kind is RelationKind.DUPLICATE_CANDIDATE
            and edge.automatic
            and _pair_key(edge.left_claim_id, edge.right_claim_id) not in blocked_pairs
        ):
            scoped_token = (
                dict(edge.feature_vector).get("scopedSharedProviderToken") == "true"
            )
            decisions.append(
                _make_decision(
                    generation_hash=generation_hash,
                    policy=policy,
                    outcome=DecisionOutcome.MERGE_CLAIMS,
                    confidence_tier=(
                        ConfidenceTier.EXPLICIT_LINEAGE
                        if scoped_token
                        else ConfidenceTier.UNIQUE_CROSS_SOURCE
                    ),
                    confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                    rationale_code=(
                        "scoped-shared-provider-token-lineage"
                        if scoped_token
                        else "unique-cross-source-economic-tuple"
                    ),
                    claim_ids=claim_ids,
                    observation_ids=observation_ids,
                    canonical_event_ids=canonical_ids,
                    feature_vector=edge.feature_vector,
                    competing_candidate_proof=edge.competing_candidate_proof,
                    source_hashes=edge.source_hashes,
                )
            )
        elif edge.kind is RelationKind.TRANSFER:
            decisions.append(
                _make_decision(
                    generation_hash=generation_hash,
                    policy=policy,
                    outcome=DecisionOutcome.LINK_TRANSFER,
                    confidence_tier=ConfidenceTier.EXPLICIT_LINEAGE,
                    confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                    rationale_code="explicit-transfer-lineage",
                    claim_ids=claim_ids,
                    observation_ids=observation_ids,
                    canonical_event_ids=canonical_ids,
                    feature_vector=edge.feature_vector,
                    source_hashes=edge.source_hashes,
                )
            )
        elif edge.kind is RelationKind.PENDING_TRANSITION and edge.automatic:
            decisions.append(
                _make_decision(
                    generation_hash=generation_hash,
                    policy=policy,
                    outcome=DecisionOutcome.LINK_PENDING,
                    confidence_tier=ConfidenceTier.EXPLICIT_LINEAGE,
                    confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                    rationale_code="explicit-pending-lineage",
                    claim_ids=claim_ids,
                    observation_ids=observation_ids,
                    canonical_event_ids=canonical_ids,
                    feature_vector=edge.feature_vector,
                    source_hashes=edge.source_hashes,
                )
            )
        elif edge.kind is RelationKind.CORRECTION:
            decisions.append(
                _make_decision(
                    generation_hash=generation_hash,
                    policy=policy,
                    outcome=DecisionOutcome.LINK_CORRECTION,
                    confidence_tier=ConfidenceTier.EXPLICIT_LINEAGE,
                    confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                    rationale_code="explicit-correction-lineage",
                    claim_ids=claim_ids,
                    observation_ids=observation_ids,
                    canonical_event_ids=canonical_ids,
                    feature_vector=edge.feature_vector,
                    source_hashes=edge.source_hashes,
                )
            )
        elif edge.kind is RelationKind.REVERSAL:
            decisions.append(
                _make_decision(
                    generation_hash=generation_hash,
                    policy=policy,
                    outcome=DecisionOutcome.LINK_REVERSAL,
                    confidence_tier=ConfidenceTier.EXPLICIT_LINEAGE,
                    confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                    rationale_code="explicit-reversal-lineage",
                    claim_ids=claim_ids,
                    observation_ids=observation_ids,
                    canonical_event_ids=canonical_ids,
                    feature_vector=edge.feature_vector,
                    source_hashes=edge.source_hashes,
                )
            )
        elif (
            edge.kind is RelationKind.MIRRORED_PROVIDER_ERROR
            and edge.automatic
        ):
            declared_mirror = (
                dict(edge.feature_vector).get("declaredDuplicateSummary") == "true"
            )
            decisions.append(
                _make_decision(
                    generation_hash=generation_hash,
                    policy=policy,
                    outcome=DecisionOutcome.SUPPRESS_MIRROR,
                    confidence_tier=ConfidenceTier.EXPLICIT_LINEAGE,
                    confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                    rationale_code=(
                        "declared-duplicate-summary-suppression"
                        if declared_mirror
                        else "explicit-mirrored-provider-error"
                    ),
                    claim_ids=claim_ids,
                    observation_ids=observation_ids,
                    canonical_event_ids=canonical_ids,
                    feature_vector=edge.feature_vector,
                    competing_candidate_proof=edge.competing_candidate_proof,
                    source_hashes=edge.source_hashes,
                )
            )

    for suppression in authority_plan.suppressions:
        claim_ids = (
            suppression.suppressed_claim_id,
            suppression.authoritative_claim_id,
        )
        decisions.append(
            _make_decision(
                generation_hash=generation_hash,
                policy=policy,
                outcome=DecisionOutcome.SOURCE_SUPPRESSED,
                confidence_tier=ConfidenceTier.AUTHORITATIVE_SOURCE_COVERAGE,
                confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                rationale_code="authoritative-source-coverage-suppression",
                claim_ids=claim_ids,
                observation_ids=(
                    observation_id
                    for claim_id in claim_ids
                    for observation_id in claim_by_id[claim_id].observation_ids
                ),
                canonical_event_ids=(
                    canonical_by_claim[claim_id] for claim_id in claim_ids
                ),
                feature_vector=suppression.feature_vector,
                competing_candidate_proof=suppression.proof,
                source_hashes=suppression.source_hashes,
            )
        )

    for ambiguity in authority_plan.ambiguities:
        decisions.append(
            _make_decision(
                generation_hash=generation_hash,
                policy=policy,
                outcome=DecisionOutcome.UNRESOLVED,
                confidence_tier=ConfidenceTier.REVIEW_REQUIRED,
                confidence_basis_points=REVIEW_CONFIDENCE_BASIS_POINTS,
                rationale_code=ambiguity.rationale_code,
                claim_ids=ambiguity.claim_ids,
                observation_ids=(
                    observation_id
                    for claim_id in ambiguity.claim_ids
                    for observation_id in claim_by_id[claim_id].observation_ids
                ),
                canonical_event_ids=(
                    canonical_by_claim[claim_id] for claim_id in ambiguity.claim_ids
                ),
                feature_vector=ambiguity.feature_vector,
                competing_candidate_proof=ambiguity.proof,
                source_hashes=ambiguity.source_hashes,
            )
        )

    for ambiguity in declared_plan.ambiguities:
        decisions.append(
            _make_decision(
                generation_hash=generation_hash,
                policy=policy,
                outcome=DecisionOutcome.UNRESOLVED,
                confidence_tier=ConfidenceTier.REVIEW_REQUIRED,
                confidence_basis_points=REVIEW_CONFIDENCE_BASIS_POINTS,
                rationale_code=ambiguity.rationale_code,
                claim_ids=ambiguity.claim_ids,
                observation_ids=(
                    observation_id
                    for claim_id in ambiguity.claim_ids
                    for observation_id in claim_by_id[claim_id].observation_ids
                ),
                canonical_event_ids=(
                    canonical_by_claim[claim_id] for claim_id in ambiguity.claim_ids
                ),
                feature_vector=ambiguity.feature_vector,
                competing_candidate_proof=ambiguity.proof,
                source_hashes=ambiguity.source_hashes,
            )
        )

    for ambiguity in token_plan.ambiguities:
        decisions.append(
            _make_decision(
                generation_hash=generation_hash,
                policy=policy,
                outcome=DecisionOutcome.UNRESOLVED,
                confidence_tier=ConfidenceTier.REVIEW_REQUIRED,
                confidence_basis_points=REVIEW_CONFIDENCE_BASIS_POINTS,
                rationale_code=ambiguity.rationale_code,
                claim_ids=ambiguity.claim_ids,
                observation_ids=(
                    observation_id
                    for claim_id in ambiguity.claim_ids
                    for observation_id in claim_by_id[claim_id].observation_ids
                ),
                canonical_event_ids=(
                    canonical_by_claim[claim_id] for claim_id in ambiguity.claim_ids
                ),
                feature_vector=ambiguity.feature_vector,
                competing_candidate_proof=ambiguity.proof,
                source_hashes=ambiguity.source_hashes,
            )
        )

    for component in components:
        if component and set(component) <= authority_plan.resolved_claims:
            continue
        component_edges = [
            edge
            for edge in duplicate_edges
            if edge.left_claim_id in component and edge.right_claim_id in component
            and edge.edge_hash not in human_rejected_edge_hashes
        ]
        if component_edges and not any(edge.automatic for edge in component_edges):
            decisions.append(
                _make_decision(
                    generation_hash=generation_hash,
                    policy=policy,
                    outcome=DecisionOutcome.UNRESOLVED,
                    confidence_tier=ConfidenceTier.REVIEW_REQUIRED,
                    confidence_basis_points=max(
                        edge.confidence_basis_points for edge in component_edges
                    ),
                    rationale_code="ambiguous-cross-source-cardinality",
                    claim_ids=component,
                    observation_ids=(
                        observation_id
                        for claim_id in component
                        for observation_id in claim_by_id[claim_id].observation_ids
                    ),
                    canonical_event_ids=(
                        canonical_by_claim[claim_id] for claim_id in component
                    ),
                    feature_vector=_feature_vector(
                        componentSize=len(component),
                        edgeCount=len(component_edges),
                        cardinality=(
                            "one-to-one"
                            if len(component) == 2
                            else (
                                "many-to-many"
                                if sum(degrees[item] > 1 for item in component) > 1
                                else "one-to-many"
                            )
                        ),
                    ),
                    competing_candidate_proof=tuple(
                        sorted(
                            (
                                f"degree:{claim_id}",
                                degrees[claim_id],
                            )
                            for claim_id in component
                        )
                    ),
                    source_hashes=(
                        source_hash
                        for claim_id in component
                        for source_hash in claim_by_id[claim_id].source_hashes
                    ),
                )
            )

    for edge in sorted(edges.values(), key=lambda item: item.edge_hash):
        if edge.kind not in {
            RelationKind.TRANSFER_CANDIDATE,
            RelationKind.MIRROR_CANDIDATE,
        }:
            continue
        mirror = edge.kind is RelationKind.MIRROR_CANDIDATE
        claim_ids = (edge.left_claim_id, edge.right_claim_id)
        decisions.append(
            _make_decision(
                generation_hash=generation_hash,
                policy=policy,
                # An unproven same-sign cross-account mirror is a relationship
                # observation, not a duplicate question. Both legs are real until
                # an explicit provider-error mapping says otherwise, so the
                # residual is `distinct` and the pair never becomes an
                # unresolved duplicate group. The edge stays queryable and is
                # counted separately for audit.
                outcome=(
                    DecisionOutcome.PRESERVE_DISTINCT
                    if mirror
                    else DecisionOutcome.UNRESOLVED
                ),
                confidence_tier=ConfidenceTier.REVIEW_REQUIRED,
                confidence_basis_points=REVIEW_CONFIDENCE_BASIS_POINTS,
                rationale_code=(
                    "cross-account-mirror-candidate"
                    if mirror
                    else "transfer-candidate"
                ),
                claim_ids=claim_ids,
                observation_ids=(
                    observation_id
                    for claim_id in claim_ids
                    for observation_id in claim_by_id[claim_id].observation_ids
                ),
                canonical_event_ids=(
                    canonical_by_claim[claim_id] for claim_id in claim_ids
                ),
                feature_vector=edge.feature_vector,
                competing_candidate_proof=edge.competing_candidate_proof,
                source_hashes=edge.source_hashes,
            )
        )

    for observation in ordered_observations:
        if observation.trusted:
            continue
        claim_id = claim_by_observation[observation.observation_id]
        decisions.append(
            _make_decision(
                generation_hash=generation_hash,
                policy=policy,
                outcome=DecisionOutcome.EXCLUDE_UNTRUSTED,
                confidence_tier=ConfidenceTier.EXPLICIT_LINEAGE,
                confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                rationale_code="trust-cutoff-or-account-exclusion",
                claim_ids=(claim_id,),
                observation_ids=(observation.observation_id,),
                canonical_event_ids=(canonical_by_claim[claim_id],),
                feature_vector=_feature_vector(
                    accountExcluded=observation.account_status == "excluded",
                    afterTrustCutoff=(
                        observation.trust_cutoff_day is not None
                        and observation.source_day > observation.trust_cutoff_day
                    ),
                ),
                source_hashes=(observation.source_hash,),
            )
        )

    for override in current_overrides:
        canonical_ids = tuple(
            sorted({canonical_by_claim[claim_id] for claim_id in override.claim_ids})
        )
        outcome = {
            "merge": DecisionOutcome.MERGE_CLAIMS,
            "preserve-distinct": DecisionOutcome.PRESERVE_DISTINCT,
            "transfer": DecisionOutcome.LINK_TRANSFER,
        }[override.action]
        decisions.append(
            _make_decision(
                generation_hash=generation_hash,
                policy=policy,
                outcome=outcome,
                confidence_tier=ConfidenceTier.HUMAN_OVERRIDE,
                confidence_basis_points=AUTO_CONFIDENCE_BASIS_POINTS,
                rationale_code=f"human-{override.action}",
                claim_ids=override.claim_ids,
                observation_ids=(
                    observation_id
                    for claim_id in override.claim_ids
                    for observation_id in claim_by_id[claim_id].observation_ids
                ),
                canonical_event_ids=canonical_ids,
                feature_vector=_feature_vector(
                    overrideVersion=override.version,
                    humanAuthority=True,
                ),
                source_hashes=(
                    source_hash
                    for claim_id in override.claim_ids
                    for source_hash in claim_by_id[claim_id].source_hashes
                ),
                human_override_id=override.override_id,
            )
        )

    canonical_state_hash = content_hash(
        {
            "canonicalEvents": [
                {
                    "canonicalEventId": item.canonical_event_id,
                    "canonicalAccountHash": item.canonical_account_hash,
                    "selectedObservationId": item.selected_observation_id,
                    "memberClaimIds": list(item.member_claim_ids),
                    "memberObservationIds": list(item.member_observation_ids),
                    "sourceDay": item.source_day.isoformat(),
                    "signedAmount": format(item.signed_amount, "f"),
                    "currency": item.currency,
                    "descriptionHash": content_hash(item.description),
                    "status": item.status,
                    "categoryHash": content_hash(item.category),
                    "sourceGroupHash": (
                        content_hash(item.source_group_id)
                        if item.source_group_id
                        else None
                    ),
                    "trusted": item.trusted,
                }
                for item in canonical_events_tuple
            ],
            "relationships": [
                {
                    "relationshipHash": item.relationship_hash,
                    "kind": item.kind.value,
                    "leftCanonicalEventId": item.left_canonical_event_id,
                    "rightCanonicalEventId": item.right_canonical_event_id,
                    "sourceEdgeHash": item.source_edge_hash,
                }
                for item in sorted(
                    relationships, key=lambda value: value.relationship_hash
                )
            ],
        }
    )
    for suppression in authority_plan.suppressions:
        if canonical_by_claim[suppression.suppressed_claim_id] != canonical_by_claim[
            suppression.authoritative_claim_id
        ]:
            raise ValueError(
                "source-suppressed claim did not attach to its authoritative event"
            )

    return IdentityResolution(
        policy=policy,
        input_hash=input_hash,
        generation_hash=generation_hash,
        canonical_state_hash=canonical_state_hash,
        observations=ordered_observations,
        claims=claims_tuple,
        edges=tuple(sorted(edges.values(), key=lambda item: item.edge_hash)),
        canonical_events=canonical_events_tuple,
        relationships=tuple(
            sorted(relationships, key=lambda item: item.relationship_hash)
        ),
        decisions=tuple(
            sorted(
                {item.decision_hash: item for item in decisions}.values(),
                key=lambda item: item.decision_hash,
            )
        ),
        overrides=all_overrides,
        interval_authorities=authority_plan.interval_authorities,
        token_scopes=tuple(
            scope_by_hash[scope_hash] for scope_hash in sorted(scope_by_hash)
        ),
    )


def application_projections(
    resolution: IdentityResolution, target_system: str = "wealthfolio"
) -> tuple[ApplicationProjection, ...]:
    if not target_system:
        raise ValueError("projection target is required")
    return tuple(
        ApplicationProjection(
            projection_id=content_hash(
                {
                    "targetSystem": target_system,
                    "canonicalEventId": event.canonical_event_id,
                    "canonicalStateHash": resolution.canonical_state_hash,
                }
            ),
            target_system=target_system,
            canonical_event_id=event.canonical_event_id,
            projected_hash=content_hash(
                {
                    "canonicalEventId": event.canonical_event_id,
                    "sourceDay": event.source_day.isoformat(),
                    "signedAmount": format(event.signed_amount, "f"),
                    "currency": event.currency,
                    "description": event.description,
                    "status": event.status,
                    "category": event.category,
                    "sourceGroupId": event.source_group_id,
                }
            ),
            active=event.trusted and event.status == "posted",
        )
        for event in resolution.canonical_events
    )


def _row_source_family(source_id: str, source_file: str) -> str:
    prefix = source_id.partition(":")[0].casefold()
    if prefix in {
        "canonical",
        "extract",
        "ledger",
        "manual",
        "monarch",
        "rebuild",
        "simplefin",
        "vanguard",
        "vanguard-history",
    }:
        if prefix == "extract" and source_file.casefold().endswith(".ofx"):
            return "ofx"
        if prefix == "extract" and source_file.casefold().endswith(".qfx"):
            return "qfx"
        return prefix
    return "unknown"


def provider_id_kind(
    source_family: str,
    source_id: str,
    *,
    has_connection_scope: bool,
) -> str:
    """How strong a provider transaction identifier is, for one observation.

    Both producers classify identifier strength here.  A shadow that called an
    OFX identifier weak while canonical called it ``ofx-fitid`` would refuse a
    scoped-token lineage canonical had already accepted.
    """

    if not source_id:
        return "none"
    if source_id.startswith("extract:synthetic:"):
        return "synthetic"
    if source_family == "simplefin":
        return "simplefin-id"
    if source_family in {"ofx", "qfx"}:
        return "ofx-fitid"
    if source_family != "unknown" and has_connection_scope:
        return "scoped-provider-id"
    return "none"


def _row_day_and_signature(value: object) -> tuple[date, str]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("transaction row date is required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    signature = ""
    if "T" in raw:
        signature = f"T{parsed.hour:02d}"
    return parsed.date(), signature


_SOURCE_PATH_DATE = re.compile(r"(?:^|[\\/])(\d{4}-\d{2}-\d{2})(?:[\\/])")
_SOURCE_FILENAME_DATE = re.compile(
    r"(?:^|[\s_-])(\d{4}-\d{2}-\d{2})(?=$|[\s_.-])"
)
_SIMPLEFIN_FILE_TIME = re.compile(
    r"simplefin-(\d{2})(\d{2})(\d{2})(?:-(\d{1,6}))?"
)


def _row_observed_at(
    row: Mapping[str, Any],
    source_file: str,
    source_day: date,
) -> datetime:
    for name in (
        "observed_at",
        "observedAt",
        "extracted_at",
        "extractedAt",
    ):
        value = str(row.get(name) or "").strip()
        if not value:
            continue
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return utc(
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    date_match = (
        _SOURCE_PATH_DATE.search(source_file)
        or _SOURCE_FILENAME_DATE.search(Path(source_file).name)
    )
    time_match = _SIMPLEFIN_FILE_TIME.search(source_file)
    if date_match:
        microseconds = (
            int((time_match.group(4) or "").ljust(6, "0") or "0")
            if time_match
            else 0
        )
        parsed_day = date.fromisoformat(date_match.group(1))
        return datetime(
            parsed_day.year,
            parsed_day.month,
            parsed_day.day,
            int(time_match.group(1)) if time_match else 0,
            int(time_match.group(2)) if time_match else 0,
            int(time_match.group(3)) if time_match else 0,
            microseconds,
            tzinfo=timezone.utc,
        )
    return datetime.combine(source_day, time.min, tzinfo=timezone.utc)


def source_account_scope(
    source_family: str,
    source_id: str,
    *,
    canonical_account_id: str,
    declared_source_account_id: str = "",
    explicit_connection_id: str = "",
) -> tuple[str, str]:
    """The provider source account and connection scope for one observation.

    Declaration keys are ``(source family, provider source account)``, so every
    producer has to derive that pair the same way or a durable decision recorded
    against one pipeline would silently miss in the other.  This is the single
    derivation both the canonical projection and the private shadow use.
    """

    if source_family == "simplefin":
        parts = source_id.split(":", 2)
        source_account_id = (
            parts[1] if len(parts) == 3 and parts[1] else canonical_account_id
        )
        default_scope = "simplefin-account-scoped"
    elif source_family in {"ofx", "qfx"}:
        source_account_id = canonical_account_id
        default_scope = f"{source_family}-account-scoped"
    else:
        source_account_id = declared_source_account_id or canonical_account_id
        default_scope = f"unscoped:{source_family}"
    return source_account_id, (explicit_connection_id or default_scope)


class DeclarationIndex:
    """Durable duplicate-summary and provider-token declarations, indexed once.

    Both producers attach exactly these attributes, from exactly these keys, so
    a declaration cannot resolve a group in one pipeline and leave it unresolved
    in the other.
    """

    __slots__ = ("summaries", "namespaces")

    def __init__(
        self,
        duplicate_summaries: Iterable[DuplicateSummaryMapping] = (),
        token_scopes: Iterable[ProviderTokenScope] = (),
    ) -> None:
        summaries: dict[tuple[str, str], DuplicateSummaryMapping] = {}
        for mapping in duplicate_summaries:
            if mapping.key in summaries:
                raise ValueError("duplicate summary mapping declared twice")
            summaries[mapping.key] = mapping
        namespaces: dict[
            tuple[str, str], tuple[ProviderTokenScope, ProviderTokenNamespace]
        ] = {}
        for scope in token_scopes:
            for side in scope.sides:
                if side.key in namespaces:
                    raise ValueError("provider token namespace declared twice")
                namespaces[side.key] = (scope, side)
        self.summaries = summaries
        self.namespaces = namespaces

    def attributes(
        self,
        *,
        source_family: str,
        source_account_id: str,
        canonical_account_id: str,
        source_id: str,
        provider_id_kind: str,
    ) -> dict[str, str]:
        key = (source_family.casefold(), source_account_id)
        attributes: dict[str, str] = {}
        summary = self.summaries.get(key)
        if summary is not None:
            attributes[DUPLICATE_SUMMARY_TARGET_ATTRIBUTE] = (
                summary.duplicate_of_source_account_id
            )
            attributes[DUPLICATE_SUMMARY_DECISION_ATTRIBUTE] = summary.decision_hash
            attributes[DUPLICATE_SUMMARY_MAP_ATTRIBUTE] = summary.map_hash
        scoped = self.namespaces.get(key)
        if scoped is not None:
            scope, namespace = scoped
            token = namespace.token(source_id, provider_id_kind)
            # The token is only proven when the declared namespace *and* the
            # declared canonical account both match.  An observation that lands
            # in the same source account under a different canonical account is
            # left untouched rather than force-fitted into the scope.
            if token and canonical_account_id == scope.canonical_account_id:
                attributes[PROVIDER_TOKEN_ATTRIBUTE] = token
                attributes[PROVIDER_TOKEN_SCOPE_ATTRIBUTE] = scope.scope_hash
                attributes[PROVIDER_TOKEN_SCOPE_MAP_ATTRIBUTE] = scope.map_hash
        return attributes


def apply_declarations(
    observations: Iterable[IdentityObservation],
    *,
    duplicate_summaries: Iterable[DuplicateSummaryMapping] = (),
    token_scopes: Iterable[ProviderTokenScope] = (),
) -> tuple[IdentityObservation, ...]:
    """Attach durable declaration attributes to already-built observations.

    Producers that do not read canonical transaction rows -- the private shadow
    reads sealed forensic activities -- still have to see the same declarations,
    or the shadow would report unresolved groups that canonical has resolved.
    Attributes already present are preserved; declarations only ever add.
    """

    index = DeclarationIndex(duplicate_summaries, token_scopes)
    result = []
    for observation in observations:
        declared = index.attributes(
            source_family=observation.source_family,
            source_account_id=observation.source_account_id,
            canonical_account_id=observation.canonical_account_id,
            source_id=observation.provider_transaction_id or "",
            provider_id_kind=observation.provider_id_kind,
        )
        if not declared:
            result.append(observation)
            continue
        merged = dict(observation.attributes)
        merged.update(declared)
        result.append(
            replace(observation, attributes=tuple(sorted(merged.items())))
        )
    return tuple(result)


def observations_from_transaction_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    duplicate_summaries: Iterable[DuplicateSummaryMapping] = (),
    token_scopes: Iterable[ProviderTokenScope] = (),
    source_artifact_hashes: Mapping[str, str] | None = None,
) -> tuple[IdentityObservation, ...]:
    values = [dict(row) for row in rows]
    index = DeclarationIndex(duplicate_summaries, token_scopes)
    fingerprints = [content_hash(row) for row in values]
    occurrences: Counter[str] = Counter()
    result = []
    for row, fingerprint in sorted(
        zip(values, fingerprints, strict=True),
        key=lambda item: (item[1], canonical_json(item[0])),
    ):
        occurrences[fingerprint] += 1
        source_id = str(row.get("source_id") or "")
        source_file = str(row.get("source_file") or "")
        source_family = _row_source_family(source_id, source_file)
        explicit_connection = str(row.get("source_connection_id") or "")
        source_account_id, connection_scope = source_account_scope(
            source_family,
            source_id,
            canonical_account_id=str(row.get("account_id") or ""),
            declared_source_account_id=str(row.get("source_account_id") or ""),
            explicit_connection_id=explicit_connection,
        )
        source_day, writer_signature = _row_day_and_signature(row.get("date"))
        attributes = {
            name: str(row.get(name) or "")
            for name in (
                "correction_of",
                "counterpart_id",
                "pending_of",
                "provider_error_of",
                "reversal_of",
            )
            if row.get(name)
        }
        if writer_signature:
            attributes["writer_timestamp_signature"] = writer_signature
        if source_artifact_hashes is not None:
            source_artifact_hash = source_artifact_hashes.get(source_file)
            if source_file and source_artifact_hash is None:
                raise ValueError(
                    "transaction row source file has no artifact hash"
                )
            if source_artifact_hash is not None:
                require_hash(source_artifact_hash)
                attributes["sourceArtifactSha256"] = source_artifact_hash
        provider_kind = provider_id_kind(
            source_family,
            source_id,
            has_connection_scope=bool(explicit_connection),
        )
        attributes.update(
            index.attributes(
                source_family=source_family,
                source_account_id=source_account_id,
                canonical_account_id=str(row.get("account_id") or ""),
                source_id=source_id,
                provider_id_kind=provider_kind,
            )
        )
        reason = str(row.get("exclusion_reason") or "").casefold()
        status = str(row.get("status") or "").casefold()
        if not status:
            status = "pending" if "pending" in reason else "posted"
        result.append(
            IdentityObservation(
                observation_id=content_hash(
                    {
                        "kind": "canonical-row-identity-observation",
                        "fingerprint": fingerprint,
                        "occurrence": occurrences[fingerprint],
                    }
                ),
                source_family=source_family,
                source_connection_id=connection_scope,
                source_account_id=source_account_id,
                canonical_account_id=str(row.get("account_id") or ""),
                provider_transaction_id=source_id or None,
                provider_id_kind=provider_kind,
                source_hash=fingerprint,
                source_day=source_day,
                observed_at=_row_observed_at(row, source_file, source_day),
                signed_amount=normalize_money(Decimal(str(row.get("amount") or "0"))),
                currency=str(row.get("currency") or "USD").upper(),
                description=str(row.get("description") or ""),
                status=status,
                category=str(row.get("category") or ""),
                source_group_id=str(row.get("transfer_group") or ""),
                import_lineage_hash=(
                    content_hash(source_file) if source_file else None
                ),
                account_status=(
                    "excluded" if row.get("excluded") is True else "active"
                ),
                attributes=tuple(sorted(attributes.items())),
            )
        )
    return tuple(result)


def observations_from_finance_state(
    state: FinanceState,
) -> tuple[IdentityObservation, ...]:
    accounts = {item.id: item for item in state.source_accounts}
    connections = {item.id: item for item in state.connections}
    source_blobs = {item.id: item for item in state.source_blobs}
    observations = []
    for item in state.transaction_observations:
        account = accounts.get(item.source_account_id)
        if account is None:
            raise ValueError("transaction observation has no source account")
        connection = connections.get(account.connection_id)
        if connection is None:
            raise ValueError("source account has no source connection")
        source_blob = source_blobs.get(item.source_blob_id)
        if source_blob is None:
            raise ValueError("transaction observation has no source blob")
        family = connection.source_system.casefold()
        provider_kind = (
            "simplefin-id"
            if family == "simplefin"
            else "ofx-fitid"
            if family in {"ofx", "qfx"}
            else "scoped-provider-id"
        )
        observations.append(
            IdentityObservation(
                observation_id=item.id,
                source_family=family,
                source_connection_id=connection.id,
                source_account_id=account.id,
                canonical_account_id=account.canonical_key,
                provider_transaction_id=item.source_transaction_id,
                provider_id_kind=provider_kind,
                source_hash=item.observation_hash,
                source_day=item.effective_at.date(),
                observed_at=item.observed_at,
                signed_amount=item.amount,
                currency=item.currency,
                description=item.description,
                status=item.status,
                trust_cutoff_day=(
                    account.trust_cutoff_at.date()
                    if account.trust_cutoff_at is not None
                    else None
                ),
                account_status=account.status,
                attributes=(
                    ("sourceArtifactSha256", source_blob.content_hash),
                ),
            )
        )
    return tuple(observations)
