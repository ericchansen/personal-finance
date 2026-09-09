"""Durable source-admission decisions for non-transaction and stale evidence.

Two admission problems are solved here, both by the same rule: a blocker is
removed only when the private decision file states a *complete* decision whose
evidence reconciles against what the loader actually observed.  Nothing is
guessed, nothing is silently dropped, and no error history is erased.

1. **Monarch balance-only entities.**  The canonical builder imports Monarch
   *transactions*.  A profile that carries balances and no transactions cannot
   be admitted by a transaction mapping, but it is still real evidence about a
   real asset or liability.  An explicit object decision admits it as observed,
   excluded, or redirected to an alternative existing entity, and its balances
   are preserved as artifacts either way.

2. **SimpleFIN connection freshness.**  Snapshots are immutable, so an
   institution error recorded in an older snapshot is permanent history.  A
   later clean snapshot from the same connection supersedes it.  When the
   newest snapshot for a connection *is* erroring, the last verified clean
   snapshot may be admitted only under an explicit connection-fallback
   decision that binds the current error and the prior snapshot's hash and
   request window, and that declares its own staleness tolerance.  A provider
   *advisory* — a warning about the request that still returns every
   institution — is preserved verbatim as evidence but is not an institution
   error: it never blocks, never makes a connection stale, and never demands a
   fallback decision.

Both surfaces are pure: they take already-parsed documents and observed
aggregates and return decisions, blockers, and artifact payloads.  They import
nothing from ``importers`` so that both the store loader and the canonical
builder can depend on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import hashlib
import json
import re

from .domain import content_hash


SOURCE_ADMISSION_POLICY_VERSION = "source-admission-v2"
DEFAULT_CLEAN_SNAPSHOT_MAX_AGE_DAYS = 1

MONARCH_ACTIONS = (
    "import",
    "observe",
    "exclude",
    "alternative-entity",
)
BALANCE_ONLY_ACTIONS = ("observe", "exclude", "alternative-entity")
TARGETED_ACTIONS = ("import", "observe", "alternative-entity")

CONNECTION_ACTIONS = ("fallback",)

# Canonical entity kinds the normalized builder materializes.  Only some of
# them can carry transactions: an AccountFact and a LoanFact both become
# account rows, while property and vehicle facts are valuation-only entities
# that must never be handed a transaction stream.
TRANSACTION_CAPABLE_ENTITY_KINDS = ("account", "loan")
CANONICAL_ENTITY_KINDS = ("account", "loan", "property", "vehicle")

DEFAULT_CONNECTION_ID = "default"

# SimpleFIN v1 has no connection object: one response carries every
# institution, accounts have no ``conn_id``, and the request sidecar names no
# connection.  Institution-scoped fallback is still required for that shape, so
# the scope is derived from the organization each account belongs to, on
# exactly the basis ``scoped_account_identity`` already uses for account
# identity.  The scope is a hash, never an institution name.
ORGANIZATION_SCOPE_PREFIX = "org:"

# A provider advisory warns about the *request* while still returning complete
# data for every institution; nothing is unavailable and nothing needs a human.
# It is preserved verbatim as evidence but is never an institution blocker and
# never makes a connection stale.  The patterns are matched case-insensitively
# against the whole message and are deliberately narrow: anything unrecognised
# stays actionable, so an unfamiliar provider error can never be downgraded by
# accident.
CONNECTION_ADVISORY_PATTERNS = ("exceeds recommended range",)


def classify_connection_error(error: str) -> str:
    """``"advisory"`` when a provider message is a warning, else ``"actionable"``."""

    folded = str(error).casefold()
    for pattern in CONNECTION_ADVISORY_PATTERNS:
        if pattern in folded:
            return "advisory"
    return "actionable"


def partition_connection_errors(
    errors: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split provider messages into ``(advisories, actionable)`` in order."""

    advisories: list[str] = []
    actionable: list[str] = []
    for error in errors:
        text = str(error)
        if classify_connection_error(text) == "advisory":
            advisories.append(text)
        else:
            actionable.append(text)
    return tuple(advisories), tuple(actionable)


class AdmissionError(ValueError):
    """A decision document is structurally unusable."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _iso_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Monarch balance-only entity decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonarchEntityDecision:
    """One durable ruling about one Monarch source entity."""

    source_account: str
    action: str
    target: str
    decision: str
    decided_at: date | None
    declared_transaction_count: int | None
    declared_balance_count: int | None
    trust_cutoff_day: date | None
    trust_cutoff_unknown: bool
    review_acknowledged: bool
    entity_kind: str
    legacy_string_form: bool
    observed_evidence_hash: str = ""
    target_evidence_hash: str = ""
    all_balances_zero: bool | None = None

    @property
    def balance_only(self) -> bool:
        return self.action in BALANCE_ONLY_ACTIONS

    @property
    def requires_target(self) -> bool:
        return self.action in TARGETED_ACTIONS

    def document(self) -> dict[str, Any]:
        return {
            "policyVersion": SOURCE_ADMISSION_POLICY_VERSION,
            "sourceAccountHash": content_hash(self.source_account),
            "action": self.action,
            "target": self.target,
            "decisionHash": content_hash(self.decision) if self.decision else None,
            "decidedAt": self.decided_at.isoformat() if self.decided_at else None,
            "declaredTransactionCount": self.declared_transaction_count,
            "declaredBalanceCount": self.declared_balance_count,
            "trustCutoffDay": (
                self.trust_cutoff_day.isoformat() if self.trust_cutoff_day else None
            ),
            "trustCutoffUnknown": self.trust_cutoff_unknown,
            "reviewAcknowledged": self.review_acknowledged,
            "entityKind": self.entity_kind,
            "legacyStringForm": self.legacy_string_form,
            "observedEvidenceHash": self.observed_evidence_hash or None,
            "targetEvidenceHash": self.target_evidence_hash or None,
            "allBalancesZero": self.all_balances_zero,
        }

    @property
    def decision_id(self) -> str:
        return content_hash(self.document())


@dataclass(frozen=True)
class MonarchEntityVerdict:
    """What the loader may do with one entity, and why."""

    decision: MonarchEntityDecision | None
    admitted: bool
    canonicalize_transactions: bool
    preserve_balances: bool
    blocker: str | None
    gap: str | None
    proof: dict[str, Any]


@dataclass(frozen=True)
class MonarchObservedEntity:
    """Aggregates the loader actually observed for one source entity.

    ``balance_points`` carries the observed value multiset — the dated balances
    themselves, as canonical decimal strings — and ``source_hashes`` the SHA-256
    of every file those values came from.  Both feed ``evidence_hash`` and
    neither is ever published: a decision binds the hash, so 1,400 values cannot
    be rewritten underneath a decision whose counts and cutoff stayed constant.
    """

    source_account: str
    transaction_count: int
    balance_count: int
    trust_cutoff_day: date | None
    needs_review: bool
    account_type: str = ""
    balance_points: tuple[tuple[str, str], ...] = ()
    source_hashes: tuple[str, ...] = ()

    @property
    def all_balances_zero(self) -> bool:
        return all(_is_zero(value) for _day, value in self.balance_points)

    def evidence_document(self) -> dict[str, Any]:
        """The private evidence a decision binds.  Never published verbatim."""

        return {
            "policyVersion": SOURCE_ADMISSION_POLICY_VERSION,
            "sourceAccountHash": content_hash(self.source_account),
            "accountType": self.account_type,
            "transactionCount": self.transaction_count,
            "balanceCount": self.balance_count,
            "balancePoints": [
                [day, value] for day, value in sorted(self.balance_points)
            ],
            "trustCutoffDay": (
                self.trust_cutoff_day.isoformat() if self.trust_cutoff_day else None
            ),
            "needsReview": self.needs_review,
            "sourceHashes": sorted(set(self.source_hashes)),
        }

    @property
    def evidence_hash(self) -> str:
        return content_hash(self.evidence_document())


def _is_zero(value: str) -> bool:
    try:
        return Decimal(value) == 0
    except (ArithmeticError, TypeError, ValueError):
        # An unparseable value is emphatically not a proven zero.
        return False


def balance_point(day: date | str, value: Any) -> tuple[str, str]:
    """Normalize one observed balance into its canonical evidence pair."""

    day_text = day.isoformat() if isinstance(day, date) else str(day)
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
    else:
        try:
            text = format(Decimal(str(value)).normalize(), "f")
        except (ArithmeticError, TypeError, ValueError):
            text = str(value)
    return (day_text, text)


def parse_monarch_account_map(
    document: Any,
) -> dict[str, MonarchEntityDecision]:
    """Accept the legacy string map and the durable object map together.

    ``{"Name": "acct-id"}`` stays valid and means ``import``.  An object entry
    may additionally name a balance-only action.  Anything that is neither a
    string nor an object is a hard structural failure, never a silent skip.
    """

    if not isinstance(document, Mapping):
        raise AdmissionError("monarch-mapping-invalid")
    decisions: dict[str, MonarchEntityDecision] = {}
    for source_account, entry in document.items():
        if not isinstance(source_account, str) or not source_account:
            raise AdmissionError("monarch-mapping-invalid")
        if isinstance(entry, str):
            decisions[source_account] = MonarchEntityDecision(
                source_account=source_account,
                action="import",
                target=entry,
                decision="",
                decided_at=None,
                declared_transaction_count=None,
                declared_balance_count=None,
                trust_cutoff_day=None,
                trust_cutoff_unknown=False,
                review_acknowledged=False,
                entity_kind="",
                legacy_string_form=True,
            )
            continue
        if not isinstance(entry, Mapping):
            raise AdmissionError("monarch-mapping-invalid")
        counts = entry.get("observedCounts")
        counts = counts if isinstance(counts, Mapping) else {}
        invariant = entry.get("balanceInvariant")
        invariant = invariant if isinstance(invariant, Mapping) else {}
        all_zero = invariant.get("allBalancesZero")
        decisions[source_account] = MonarchEntityDecision(
            source_account=source_account,
            action=_text(entry.get("action")) or "import",
            target=_text(
                entry.get("canonicalAccountId")
                or entry.get("assertionAccountId")
                or entry.get("target")
            ),
            decision=_text(entry.get("decision")),
            decided_at=_iso_date(entry.get("decidedAt")),
            declared_transaction_count=(
                counts.get("transactions")
                if isinstance(counts.get("transactions"), int)
                else None
            ),
            declared_balance_count=(
                counts.get("balances")
                if isinstance(counts.get("balances"), int)
                else None
            ),
            trust_cutoff_day=_iso_date(entry.get("trustCutoffDay")),
            trust_cutoff_unknown=entry.get("trustCutoffUnknown") is True,
            review_acknowledged=entry.get("reviewAcknowledged") is True,
            entity_kind=_text(entry.get("entityKind")),
            legacy_string_form=False,
            observed_evidence_hash=_text(entry.get("observedEvidenceHash")),
            target_evidence_hash=_text(entry.get("targetEvidenceHash")),
            all_balances_zero=all_zero if isinstance(all_zero, bool) else None,
        )
    return decisions


def evaluate_monarch_entity(
    observed: MonarchObservedEntity,
    decision: MonarchEntityDecision | None,
    *,
    known_targets: Iterable[str] | Mapping[str, str],
    target_evidence: Mapping[str, str] | None = None,
) -> MonarchEntityVerdict:
    """Rule on one observed entity against its durable decision.

    ``known_targets`` may be a bare set of canonical ids, or a mapping of id to
    canonical entity kind.  The mapping form lets a decision target a non-account
    canonical entity — a loan, property, or vehicle fact — which is required for
    ``alternative-entity`` and impossible to express with account ids alone.

    ``target_evidence`` maps a canonical id to the hash of the fact behind it, so
    a decision can bind the target it was actually made against.
    """

    if isinstance(known_targets, Mapping):
        targets: dict[str, str] = {
            str(key): str(value or "").strip().lower()
            for key, value in known_targets.items()
        }
    else:
        targets = {str(key): "" for key in known_targets}
    proof: dict[str, Any] = {
        "policyVersion": SOURCE_ADMISSION_POLICY_VERSION,
        "sourceAccountHash": content_hash(observed.source_account),
        "observedTransactionCount": observed.transaction_count,
        "observedBalanceCount": observed.balance_count,
        "observedTrustCutoffDay": (
            observed.trust_cutoff_day.isoformat()
            if observed.trust_cutoff_day
            else None
        ),
        "observedNeedsReview": observed.needs_review,
        "balanceOnly": observed.transaction_count == 0,
        "observedEvidenceHash": observed.evidence_hash,
    }
    if decision is None:
        return MonarchEntityVerdict(
            decision=None,
            admitted=False,
            canonicalize_transactions=False,
            preserve_balances=True,
            blocker="monarch-entity-decision-missing",
            gap=None,
            proof=proof,
        )

    proof["decision"] = decision.document()

    def refuse(code: str) -> MonarchEntityVerdict:
        return MonarchEntityVerdict(
            decision=decision,
            admitted=False,
            canonicalize_transactions=False,
            preserve_balances=True,
            blocker=code,
            gap=None,
            proof=proof,
        )

    if decision.action not in MONARCH_ACTIONS:
        return refuse("monarch-entity-action-invalid")
    if decision.requires_target and not decision.target:
        return refuse("monarch-entity-target-missing")
    if decision.requires_target and decision.target not in targets:
        return refuse("monarch-entity-target-unresolved")

    target_kind = targets.get(decision.target, "")
    proof["targetEntityKind"] = target_kind or None
    if (
        decision.action == "import"
        and target_kind
        and target_kind not in TRANSACTION_CAPABLE_ENTITY_KINDS
    ):
        # A property or vehicle fact is a valuation entity, not a ledger; it can
        # never receive a transaction stream.
        return refuse("monarch-entity-target-not-transaction-capable")
    if (
        decision.action == "alternative-entity"
        and target_kind
        and decision.entity_kind
        and decision.entity_kind.strip().lower() != target_kind
    ):
        # The decision names what the observed entity is; redirecting it to a
        # canonical entity of a different kind would silently change its meaning.
        return refuse("monarch-entity-kind-target-mismatch")

    if decision.legacy_string_form:
        # Backward compatibility: a bare string mapping stays sufficient on its
        # own, for entities with and without transactions alike.
        return MonarchEntityVerdict(
            decision=decision,
            admitted=True,
            canonicalize_transactions=True,
            preserve_balances=True,
            blocker=None,
            gap=None,
            proof=proof,
        )

    if not decision.decision:
        return refuse("monarch-entity-rationale-missing")
    if decision.decided_at is None:
        return refuse("monarch-entity-decided-at-missing")
    if decision.declared_transaction_count is None:
        return refuse("monarch-entity-declared-counts-missing")
    if decision.declared_balance_count is None:
        return refuse("monarch-entity-declared-counts-missing")
    if decision.declared_transaction_count != observed.transaction_count:
        return refuse("monarch-entity-transaction-count-unreconciled")
    if decision.declared_balance_count != observed.balance_count:
        return refuse("monarch-entity-balance-count-unreconciled")
    if decision.balance_only and observed.transaction_count != 0:
        return refuse("monarch-balance-only-action-on-transaction-entity")
    if decision.balance_only and not decision.entity_kind:
        return refuse("monarch-entity-kind-missing")
    if decision.trust_cutoff_unknown == (decision.trust_cutoff_day is not None):
        return refuse("monarch-entity-trust-cutoff-unstated")
    if (
        decision.trust_cutoff_day is not None
        and observed.trust_cutoff_day is not None
        and decision.trust_cutoff_day != observed.trust_cutoff_day
    ):
        return refuse("monarch-entity-trust-cutoff-unreconciled")
    if (
        decision.trust_cutoff_unknown
        and observed.trust_cutoff_day is not None
    ):
        return refuse("monarch-entity-trust-cutoff-unreconciled")
    if observed.needs_review and not decision.review_acknowledged:
        return refuse("monarch-entity-review-unacknowledged")

    # Counts and a cutoff do not pin the values.  An object decision binds the
    # evidence it was made against, so a rewritten balance series that keeps the
    # same count and cutoff invalidates the decision instead of inheriting it.
    if not decision.observed_evidence_hash:
        return refuse("monarch-entity-observed-evidence-hash-missing")
    if decision.observed_evidence_hash != observed.evidence_hash:
        return refuse("monarch-entity-observed-evidence-unreconciled")

    if decision.action == "alternative-entity" and not decision.target_evidence_hash:
        # Redirecting an entity onto another fact is a claim about that fact.
        return refuse("monarch-entity-target-evidence-hash-missing")
    if decision.target_evidence_hash:
        resolved = (target_evidence or {}).get(decision.target)
        if not resolved:
            return refuse("monarch-entity-target-evidence-unavailable")
        proof["targetEvidenceHash"] = resolved
        if resolved != decision.target_evidence_hash:
            return refuse("monarch-entity-target-evidence-unreconciled")

    if decision.action == "exclude":
        if decision.all_balances_zero is None:
            return refuse("monarch-entity-balance-invariant-missing")
        proof["allBalancesZero"] = observed.all_balances_zero
        if decision.all_balances_zero != observed.all_balances_zero:
            return refuse("monarch-entity-balance-invariant-unproven")

    return MonarchEntityVerdict(
        decision=decision,
        admitted=True,
        canonicalize_transactions=decision.action == "import",
        preserve_balances=True,
        blocker=None,
        gap=(
            f"monarch-{decision.action}-observed-not-canonicalized"
            if decision.action != "import"
            else None
        ),
        proof=proof,
    )


# ---------------------------------------------------------------------------
# SimpleFIN connection-scoped freshness and fallback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotEvidence:
    """One immutable snapshot within one connection scope."""

    connection_id: str
    snapshot_sha256: str
    observed_at: datetime
    errors: tuple[str, ...]
    requested_start: date | None
    requested_end: date | None

    @property
    def advisories(self) -> tuple[str, ...]:
        """Provider warnings that leave every institution available."""

        return partition_connection_errors(self.errors)[0]

    @property
    def actionable_errors(self) -> tuple[str, ...]:
        """Provider errors that make an institution unavailable."""

        return partition_connection_errors(self.errors)[1]

    @property
    def clean(self) -> bool:
        # Advisory-only evidence is clean: the pull succeeded for every
        # institution.  The advisory itself is still carried in ``errors`` and
        # hashed into ``error_hash`` so the history is never erased.
        return not self.actionable_errors

    @property
    def error_hash(self) -> str:
        return content_hash(sorted(self.errors))

    @property
    def advisory_hash(self) -> str:
        return content_hash(sorted(self.advisories))

    @property
    def actionable_error_hash(self) -> str:
        return content_hash(sorted(self.actionable_errors))

    def document(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "connectionId": self.connection_id,
            "snapshotSha256": self.snapshot_sha256,
            "observedAt": self.observed_at.isoformat(),
            "errorCount": len(self.errors),
            "errorHash": self.error_hash,
            "requestedStart": (
                self.requested_start.isoformat() if self.requested_start else None
            ),
            "requestedEnd": (
                self.requested_end.isoformat() if self.requested_end else None
            ),
        }
        advisories = self.advisories
        if advisories:
            # Emitted only when advisories exist so evidence observed before
            # this policy revision keeps its exact document hash.
            result["advisoryCount"] = len(advisories)
            result["advisoryHash"] = self.advisory_hash
            result["actionableErrorCount"] = len(self.errors) - len(advisories)
        return result


@dataclass(frozen=True)
class ConnectionFallbackDecision:
    """A durable ruling that stale-but-clean evidence may be admitted."""

    connection_id: str
    action: str
    decision: str
    decided_at: date | None
    current_error_hash: str
    fallback_snapshot_sha256: str
    fallback_requested_start: date | None
    fallback_requested_end: date | None
    max_staleness_days: int | None

    def document(self) -> dict[str, Any]:
        return {
            "policyVersion": SOURCE_ADMISSION_POLICY_VERSION,
            "connectionId": self.connection_id,
            "action": self.action,
            "decisionHash": content_hash(self.decision) if self.decision else None,
            "decidedAt": self.decided_at.isoformat() if self.decided_at else None,
            "currentErrorHash": self.current_error_hash,
            "fallbackSnapshotSha256": self.fallback_snapshot_sha256,
            "fallbackRequestedStart": (
                self.fallback_requested_start.isoformat()
                if self.fallback_requested_start
                else None
            ),
            "fallbackRequestedEnd": (
                self.fallback_requested_end.isoformat()
                if self.fallback_requested_end
                else None
            ),
            "maxStalenessDays": self.max_staleness_days,
        }

    @property
    def decision_id(self) -> str:
        return content_hash(self.document())


@dataclass(frozen=True)
class ConnectionAdmission:
    """The freshness verdict for one connection scope."""

    connection_id: str
    latest_snapshot_sha256: str
    admitted_snapshot_sha256: str | None
    current_errors: tuple[str, ...]
    superseded_error_snapshots: tuple[str, ...]
    fresh: bool
    stale: bool
    staleness_days: int | None
    decision: ConnectionFallbackDecision | None
    blocker: str | None
    gap: str | None
    proof: dict[str, Any]
    advisories: tuple[str, ...] = ()
    admitted_requested_start: date | None = None
    admitted_requested_end: date | None = None

    @property
    def admitted(self) -> bool:
        return self.admitted_snapshot_sha256 is not None


def parse_connection_decisions(
    document: Any,
) -> dict[str, ConnectionFallbackDecision]:
    """Read the optional ``connections`` block of the SimpleFIN account map."""

    if document is None:
        return {}
    if not isinstance(document, Mapping):
        raise AdmissionError("simplefin-connection-decisions-invalid")
    decisions: dict[str, ConnectionFallbackDecision] = {}
    for connection_id, entry in document.items():
        if not isinstance(connection_id, str) or not connection_id:
            raise AdmissionError("simplefin-connection-decisions-invalid")
        if not isinstance(entry, Mapping):
            raise AdmissionError("simplefin-connection-decisions-invalid")
        staleness = entry.get("maxStalenessDays")
        decisions[connection_id] = ConnectionFallbackDecision(
            connection_id=connection_id,
            action=_text(entry.get("action")),
            decision=_text(entry.get("decision")),
            decided_at=_iso_date(entry.get("decidedAt")),
            current_error_hash=_text(entry.get("currentErrorHash")),
            fallback_snapshot_sha256=_text(entry.get("fallbackSnapshotSha256")),
            fallback_requested_start=_iso_date(
                entry.get("fallbackRequestedStart")
            ),
            fallback_requested_end=_iso_date(entry.get("fallbackRequestedEnd")),
            max_staleness_days=(
                staleness if isinstance(staleness, int) and staleness >= 0 else None
            ),
        )
    return decisions


def evaluate_connection(
    snapshots: Sequence[SnapshotEvidence],
    decision: ConnectionFallbackDecision | None,
    *,
    as_of: datetime,
) -> ConnectionAdmission:
    """Decide which snapshot of one connection may be admitted.

    Snapshots must all share one connection scope; accounts are never blended
    across scopes because the caller groups before calling.  Error history is
    reported in full regardless of the verdict.
    """

    if not snapshots:
        raise AdmissionError("simplefin-connection-snapshots-missing")
    connection_ids = {item.connection_id for item in snapshots}
    if len(connection_ids) != 1:
        raise AdmissionError("simplefin-connection-scope-mixed")
    ordered = sorted(
        snapshots, key=lambda item: (item.observed_at, item.snapshot_sha256)
    )
    connection_id = ordered[-1].connection_id
    latest = ordered[-1]
    superseded = tuple(
        item.snapshot_sha256 for item in ordered[:-1] if not item.clean
    )
    proof: dict[str, Any] = {
        "policyVersion": SOURCE_ADMISSION_POLICY_VERSION,
        "connectionId": connection_id,
        "snapshotCount": len(ordered),
        "snapshots": [item.document() for item in ordered],
        "latestSnapshotSha256": latest.snapshot_sha256,
        "supersededErrorSnapshotCount": len(superseded),
    }
    if latest.advisories:
        # Advisories are preserved as evidence and reported, never counted as
        # institution blockers and never treated as staleness.
        proof["latestAdvisoryCount"] = len(latest.advisories)
        proof["latestAdvisoryHash"] = latest.advisory_hash
        proof["latestActionableErrorCount"] = len(latest.actionable_errors)

    if latest.clean:
        # Current clean evidence supersedes historical connection errors.  The
        # errors stay in the proof and in their own immutable snapshots.
        staleness_days = max(
            0, (as_of.date() - latest.observed_at.date()).days
        )
        fresh = staleness_days <= DEFAULT_CLEAN_SNAPSHOT_MAX_AGE_DAYS
        proof["latestObservedAt"] = latest.observed_at.isoformat()
        proof["evaluatedAt"] = as_of.isoformat()
        proof["cleanSnapshotMaxAgeDays"] = (
            DEFAULT_CLEAN_SNAPSHOT_MAX_AGE_DAYS
        )
        proof["stalenessDays"] = staleness_days
        return ConnectionAdmission(
            connection_id=connection_id,
            latest_snapshot_sha256=latest.snapshot_sha256,
            admitted_snapshot_sha256=latest.snapshot_sha256,
            current_errors=(),
            superseded_error_snapshots=superseded,
            fresh=fresh,
            stale=not fresh,
            staleness_days=staleness_days,
            decision=None,
            blocker=None,
            gap=(
                "simplefin-connection-latest-clean-stale"
                if not fresh
                else "simplefin-historical-connection-error-superseded"
                if superseded
                else None
            ),
            proof=proof,
            advisories=latest.advisories,
            admitted_requested_start=latest.requested_start,
            admitted_requested_end=latest.requested_end,
        )

    prior_clean = [item for item in ordered[:-1] if item.clean]
    fallback = prior_clean[-1] if prior_clean else None
    staleness_days = (
        (as_of.date() - fallback.observed_at.date()).days if fallback else None
    )
    proof["currentErrorHash"] = latest.error_hash
    proof["currentErrorCount"] = len(latest.errors)
    proof["currentActionableErrorCount"] = len(latest.actionable_errors)
    proof["fallbackSnapshotSha256"] = (
        fallback.snapshot_sha256 if fallback else None
    )
    proof["fallbackObservedAt"] = (
        fallback.observed_at.isoformat() if fallback else None
    )
    proof["fallbackRequestedStart"] = (
        fallback.requested_start.isoformat()
        if fallback and fallback.requested_start
        else None
    )
    proof["fallbackRequestedEnd"] = (
        fallback.requested_end.isoformat()
        if fallback and fallback.requested_end
        else None
    )
    proof["stalenessDays"] = staleness_days

    def refuse(code: str) -> ConnectionAdmission:
        return ConnectionAdmission(
            connection_id=connection_id,
            latest_snapshot_sha256=latest.snapshot_sha256,
            admitted_snapshot_sha256=None,
            current_errors=latest.actionable_errors,
            superseded_error_snapshots=superseded,
            fresh=False,
            stale=True,
            staleness_days=staleness_days,
            decision=decision,
            blocker=code,
            gap=None,
            proof=proof,
            advisories=latest.advisories,
        )

    if decision is None:
        return refuse("simplefin-connection-error-undecided")
    proof["decision"] = decision.document()
    if decision.action not in CONNECTION_ACTIONS:
        return refuse("simplefin-connection-action-invalid")
    if not decision.decision:
        return refuse("simplefin-connection-rationale-missing")
    if decision.decided_at is None:
        return refuse("simplefin-connection-decided-at-missing")
    if fallback is None:
        return refuse("simplefin-connection-fallback-evidence-missing")
    if decision.current_error_hash != latest.error_hash:
        return refuse("simplefin-connection-current-error-unbound")
    if decision.fallback_snapshot_sha256 != fallback.snapshot_sha256:
        return refuse("simplefin-connection-fallback-snapshot-unbound")
    if (
        decision.fallback_requested_start != fallback.requested_start
        or decision.fallback_requested_end != fallback.requested_end
    ):
        return refuse("simplefin-connection-fallback-window-unbound")
    if decision.max_staleness_days is None:
        return refuse("simplefin-connection-staleness-tolerance-missing")
    if staleness_days is None or staleness_days > decision.max_staleness_days:
        return refuse("simplefin-connection-fallback-too-stale")

    return ConnectionAdmission(
        connection_id=connection_id,
        latest_snapshot_sha256=latest.snapshot_sha256,
        admitted_snapshot_sha256=fallback.snapshot_sha256,
        current_errors=latest.actionable_errors,
        superseded_error_snapshots=superseded,
        fresh=False,
        stale=True,
        staleness_days=staleness_days,
        decision=decision,
        blocker=None,
        gap="simplefin-connection-fallback-admitted-stale",
        proof=proof,
        advisories=latest.advisories,
        admitted_requested_start=fallback.requested_start,
        admitted_requested_end=fallback.requested_end,
    )


@dataclass(frozen=True)
class ConnectionScopeAdmission:
    """Admission verdicts for every connection scope, decided independently.

    Each scope selects its own snapshot: a connection that is still returning
    clean or advisory-only evidence keeps advancing even while a sibling
    connection is failing over to older approved evidence.  No scope is ever
    represented by another scope's snapshot, and one failing connection cannot
    silently freeze a healthy one at a stale snapshot.
    """

    admissions: tuple[ConnectionAdmission, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        """Per-connection blockers, so a reader sees which scope refused."""

        return tuple(
            f"{item.connection_id}: {item.blocker}"
            for item in self.admissions
            if item.blocker
        )

    @property
    def admitted(self) -> tuple[ConnectionAdmission, ...]:
        return tuple(item for item in self.admissions if item.admitted)

    @property
    def stale_connections(self) -> tuple[str, ...]:
        return tuple(item.connection_id for item in self.admitted if item.stale)

    def document(self) -> dict[str, Any]:
        return {
            "policyVersion": SOURCE_ADMISSION_POLICY_VERSION,
            "connectionCount": len(self.admissions),
            "admittedConnectionCount": len(self.admitted),
            "staleConnections": list(self.stale_connections),
            "blockedConnections": [
                item.connection_id for item in self.admissions if item.blocker
            ],
            "connections": [
                {
                    "connectionId": item.connection_id,
                    "latestSnapshotSha256": item.latest_snapshot_sha256,
                    "admittedSnapshotSha256": item.admitted_snapshot_sha256,
                    "fresh": item.fresh,
                    "stale": item.stale,
                    "stalenessDays": item.staleness_days,
                    "advisoryCount": len(item.advisories),
                    "currentErrorCount": len(item.current_errors),
                    "supersededErrorSnapshotCount": len(
                        item.superseded_error_snapshots
                    ),
                    "admittedRequestedStart": (
                        item.admitted_requested_start.isoformat()
                        if item.admitted_requested_start
                        else None
                    ),
                    "admittedRequestedEnd": (
                        item.admitted_requested_end.isoformat()
                        if item.admitted_requested_end
                        else None
                    ),
                    "decisionId": item.decision.decision_id if item.decision else None,
                    "blocker": item.blocker,
                    "gap": item.gap,
                }
                for item in self.admissions
            ],
        }


def evaluate_connection_scopes(
    grouped: Mapping[str, Sequence[SnapshotEvidence]],
    decisions: Mapping[str, ConnectionFallbackDecision],
    *,
    as_of: datetime,
) -> ConnectionScopeAdmission:
    """Evaluate every connection scope on its own evidence and decision.

    Scopes are evaluated independently and in a deterministic order so that a
    replay produces the same selection.  A failing connection yields its own
    blocker rather than suppressing the connections that are still healthy.
    """

    if not grouped:
        raise AdmissionError("simplefin-connection-snapshots-missing")
    for connection_id, decision in decisions.items():
        # A decision approving stale evidence for one connection must never be
        # spent on another; that would admit one scope on another's authority.
        if decision.connection_id != connection_id:
            raise AdmissionError("simplefin-connection-decision-scope-mismatch")
    admissions = tuple(
        evaluate_connection(
            grouped[connection_id], decisions.get(connection_id), as_of=as_of
        )
        for connection_id in sorted(grouped)
    )
    return ConnectionScopeAdmission(admissions=admissions)


def connection_id_for(metadata: Any) -> str:
    """Connection scope declared by request metadata, else the default scope."""

    if isinstance(metadata, Mapping):
        declared = _text(metadata.get("connectionId"))
        if declared:
            return declared
    return DEFAULT_CONNECTION_ID


# ---------------------------------------------------------------------------
# Organization scope (SimpleFIN v1 has no connection object)
# ---------------------------------------------------------------------------


def organization_scope(account: Any, version: str) -> str:
    """The raw scope string an account belongs to.

    This is the same basis ``finance_store.simplefin.scoped_account_identity``
    uses to scope account identity, so an account and its institution scope can
    never disagree about which connection they belong to.
    """

    if not isinstance(account, Mapping):
        return ""
    if str(version) == "2":
        return _text(account.get("conn_id"))
    organization = account.get("org")
    if not isinstance(organization, Mapping):
        return ""
    return (
        _text(organization.get("domain"))
        or (
            f"{_text(organization.get('sfin-url'))}|"
            f"{_text(organization.get('name'))}"
        ).strip("|")
        or _text(organization.get("url"))
        or ""
    )


def organization_scope_id(account: Any, version: str) -> str:
    """Deterministic, name-free scope key for one account's institution."""

    scope = organization_scope(account, version)
    if not scope:
        return ""
    return f"{ORGANIZATION_SCOPE_PREFIX}{content_hash(scope)[:16]}"


def account_scope_ids(accounts: Sequence[Any], version: str) -> dict[str, str]:
    """Map each provider account id to its institution scope id.

    Accounts whose organization cannot be determined fall back to the default
    scope rather than being dropped or silently merged into a sibling.
    """

    scopes: dict[str, str] = {}
    for account in accounts:
        if not isinstance(account, Mapping):
            continue
        provider_id = _text(account.get("id")) or _text(account.get("account_id"))
        if not provider_id:
            continue
        scopes[provider_id] = (
            organization_scope_id(account, version) or DEFAULT_CONNECTION_ID
        )
    return scopes


_NON_ALPHANUMERIC = re.compile(r"[^0-9a-z]+")


def normalize_institution_name(value: Any) -> str:
    """Case-folded, punctuation-collapsed institution name.

    Normalization exists so that ``Example Bank, N.A.`` and ``example bank
    n a`` are the same *name*, not so that similar names can be guessed at.
    Matching against it is exact and boundary-delimited; there is deliberately
    no edit distance, prefix, or token-subset rule anywhere in this module.
    """

    if not isinstance(value, str):
        return ""
    return _NON_ALPHANUMERIC.sub(" ", value.casefold()).strip()


def _scope_names(accounts: Sequence[Any], version: str) -> dict[str, set[str]]:
    """Normalized institution names, per scope, taken from the payload itself."""

    names: dict[str, set[str]] = {}
    for account in accounts:
        if not isinstance(account, Mapping):
            continue
        scope = organization_scope_id(account, version)
        if not scope:
            continue
        organization = account.get("org")
        if not isinstance(organization, Mapping):
            continue
        bucket = names.setdefault(scope, set())
        for key in ("name", "domain"):
            normalized = normalize_institution_name(organization.get(key))
            if len(normalized) >= 2:
                bucket.add(normalized)
    return names


def _structured_error_scope(error: Any, version: str) -> str:
    """Scope declared by a structured provider error, if it declares one."""

    if not isinstance(error, Mapping):
        return ""
    scope = organization_scope_id(error, version)
    if scope:
        return scope
    return ""


def _matching_scopes(text: str, names: Mapping[str, set[str]]) -> set[str]:
    """Scopes whose exact normalized name occurs at token boundaries."""

    padded = f" {text} "
    return {
        scope
        for scope, candidates in names.items()
        if any(f" {name} " in padded for name in candidates)
    }


@dataclass(frozen=True)
class ScopedErrors:
    """Provider errors split into institution-scoped and unscopable groups."""

    by_scope: Mapping[str, tuple[str, ...]]
    unscopable: tuple[str, ...]

    def for_scope(self, scope: str) -> tuple[str, ...]:
        """Errors that apply to one scope: its own plus every global error.

        An error nobody could attribute to a single institution is a global
        blocker.  Attributing it to one scope would clear the other scopes on
        no evidence, so it is applied to all of them.
        """

        return tuple(sorted({*self.by_scope.get(scope, ()), *self.unscopable}))


def scope_provider_errors(
    errors: Sequence[Any],
    accounts: Sequence[Any],
    version: str,
) -> ScopedErrors:
    """Attribute each provider error to one institution scope, or to none.

    An error is scoped only when it is structured with an organization, or
    when exactly one organization present in the same payload has its **exact**
    normalized name inside the error text at token boundaries.  Zero matches or
    two or more matches leave the error unscopable, and an unscopable error
    blocks every scope rather than being guessed onto one.
    """

    names = _scope_names(accounts, version)
    by_scope: dict[str, list[str]] = {}
    unscopable: list[str] = []
    for error in errors:
        scope = _structured_error_scope(error, version)
        text = (
            _text(error.get("message") or error.get("description"))
            if isinstance(error, Mapping)
            else str(error)
        )
        if not scope:
            matched = _matching_scopes(normalize_institution_name(text), names)
            scope = next(iter(matched)) if len(matched) == 1 else ""
        if scope and scope in names:
            by_scope.setdefault(scope, []).append(text)
        else:
            unscopable.append(text)
    return ScopedErrors(
        by_scope={scope: tuple(items) for scope, items in by_scope.items()},
        unscopable=tuple(unscopable),
    )


@dataclass(frozen=True)
class SnapshotPartition:
    """One immutable snapshot split into per-institution evidence."""

    snapshot_sha256: str
    observed_at: datetime
    evidence: tuple[SnapshotEvidence, ...]
    accounts_by_scope: Mapping[str, tuple[str, ...]]
    unscopable_errors: tuple[str, ...]


def partition_snapshot(
    *,
    snapshot_sha256: str,
    observed_at: datetime,
    version: str,
    accounts: Sequence[Any],
    errors: Sequence[Any],
    declared_connection: str = DEFAULT_CONNECTION_ID,
    requested_start: date | None = None,
    requested_end: date | None = None,
) -> SnapshotPartition:
    """Split one snapshot into the connection scopes it actually contains.

    An explicitly declared ``connectionId`` always wins: a human naming the
    scope is stronger evidence than anything inferred.  Otherwise accounts are
    partitioned by their organization, which is the only scope SimpleFIN v1
    carries, and each scope gets only its own accounts and its own errors.
    """

    if declared_connection and declared_connection != DEFAULT_CONNECTION_ID:
        scopes = {declared_connection: tuple(
            _text(account.get("id")) or _text(account.get("account_id"))
            for account in accounts
            if isinstance(account, Mapping)
        )}
        scoped = ScopedErrors(by_scope={}, unscopable=tuple(
            _text(error.get("message") or error.get("description"))
            if isinstance(error, Mapping)
            else str(error)
            for error in errors
        ))
    else:
        by_account = account_scope_ids(accounts, version)
        grouped: dict[str, list[str]] = {}
        for provider_id, scope in by_account.items():
            grouped.setdefault(scope, []).append(provider_id)
        scopes = {scope: tuple(sorted(ids)) for scope, ids in grouped.items()}
        # The default scope is always present and always evaluated.  It is the
        # global scope: it carries accounts whose institution could not be
        # determined and every error that could not be attributed to exactly
        # one institution.  Without it an errors-only snapshot would create a
        # scope no later snapshot could ever supersede.
        scopes.setdefault(DEFAULT_CONNECTION_ID, ())
        scoped = scope_provider_errors(errors, accounts, version)
    if declared_connection and declared_connection != DEFAULT_CONNECTION_ID:
        errors_for_scope = {declared_connection: scoped.unscopable}
    else:
        errors_for_scope = {
            scope: (
                scoped.unscopable
                if scope == DEFAULT_CONNECTION_ID
                else scoped.by_scope.get(scope, ())
            )
            for scope in scopes
        }
    evidence = tuple(
        SnapshotEvidence(
            connection_id=scope,
            snapshot_sha256=snapshot_sha256,
            observed_at=observed_at,
            errors=errors_for_scope.get(scope, ()),
            requested_start=requested_start,
            requested_end=requested_end,
        )
        for scope in sorted(scopes)
    )
    return SnapshotPartition(
        snapshot_sha256=snapshot_sha256,
        observed_at=observed_at,
        evidence=evidence,
        accounts_by_scope=scopes,
        unscopable_errors=scoped.unscopable,
    )


_SNAPSHOT_DATE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")
_SNAPSHOT_TIME = re.compile(r"simplefin-(?P<time>\d{6})(?:-(?P<micro>\d{1,6}))?")


def snapshot_observed_at(path: Path, fallback: datetime) -> datetime:
    """Deterministic observation instant encoded in an immutable snapshot path."""

    date_match = next(
        (
            _SNAPSHOT_DATE.fullmatch(part)
            for part in reversed(path.parts)
            if _SNAPSHOT_DATE.fullmatch(part)
        ),
        None,
    )
    if date_match is None:
        return fallback
    parsed_date = date.fromisoformat(date_match.group("date"))
    time_match = _SNAPSHOT_TIME.search(path.stem)
    if time_match is None:
        parsed_time = time.min
    else:
        raw_time = time_match.group("time")
        parsed_time = time(
            int(raw_time[0:2]),
            int(raw_time[2:4]),
            int(raw_time[4:6]),
            int((time_match.group("micro") or "0").ljust(6, "0")),
        )
    return datetime.combine(parsed_date, parsed_time, tzinfo=timezone.utc)


def snapshot_scope_for_paths(paths: Sequence[Path]) -> dict[Path, str]:
    """Connection scope declared by each snapshot's request sidecar.

    Two connections can legitimately produce byte-identical snapshots, so a
    caller resolving evidence back to files must key on scope as well as the
    content hash or it will hand one connection another's file.
    """

    scopes: dict[Path, str] = {}
    for path in paths:
        request_path = path.with_name(
            f"request-{path.stem.removeprefix('simplefin-')}.json"
        )
        metadata: Mapping[str, Any] = {}
        if request_path.is_file():
            try:
                loaded = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AdmissionError("simplefin-request-metadata-invalid") from exc
            if isinstance(loaded, Mapping):
                metadata = loaded
        scopes[path] = connection_id_for(metadata)
    return scopes


def snapshot_evidence_from_paths(
    paths: Sequence[Path],
    *,
    fallback: datetime,
    errors_for: Any,
) -> dict[str, list[SnapshotEvidence]]:
    """Group immutable snapshot files into connection-scoped evidence.

    ``errors_for`` is called with each path and returns its institution errors.
    The sibling ``request-*.json`` supplies the connection scope and request
    window when present; snapshots without one fall back to the default scope
    with an unknown window.
    """

    scopes = snapshot_scope_for_paths(paths)
    grouped: dict[str, list[SnapshotEvidence]] = {}
    for path in paths:
        raw = path.read_bytes()
        request_path = path.with_name(
            f"request-{path.stem.removeprefix('simplefin-')}.json"
        )
        metadata: Mapping[str, Any] = {}
        if request_path.is_file():
            loaded = json.loads(request_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                metadata = loaded
        connection_id = scopes[path]
        grouped.setdefault(connection_id, []).append(
            SnapshotEvidence(
                connection_id=connection_id,
                snapshot_sha256=hashlib.sha256(raw).hexdigest(),
                observed_at=snapshot_observed_at(path, fallback),
                errors=tuple(errors_for(path)),
                requested_start=_iso_date(metadata.get("requestedStart")),
                requested_end=_iso_date(metadata.get("requestedEnd")),
            )
        )
    return grouped


@dataclass(frozen=True)
class PartitionedEvidence:
    """Per-institution evidence resolved back to the files it came from."""

    grouped: Mapping[str, tuple[SnapshotEvidence, ...]]
    accounts: Mapping[tuple[str, str], tuple[str, ...]]
    paths: Mapping[tuple[str, str], Path]
    unscopable_errors: Mapping[str, tuple[str, ...]]


def partitioned_evidence_from_paths(
    paths: Sequence[Path],
    *,
    fallback: datetime,
    view_for: Any,
) -> PartitionedEvidence:
    """Group immutable snapshots into institution-scoped evidence.

    ``view_for`` is called with each path and returns
    ``(version, account_rows, errors)`` so this module never has to know how a
    protocol version nests its payload.  The sibling ``request-*.json`` still
    supplies an explicitly declared scope and the request window.

    A single SimpleFIN v1 file holding thirty institutions therefore yields
    thirty independently selectable scopes, each carrying only its own accounts
    — which is what lets one institution fail over while the rest advance.
    """

    declared = snapshot_scope_for_paths(paths)
    grouped: dict[str, list[SnapshotEvidence]] = {}
    accounts: dict[tuple[str, str], tuple[str, ...]] = {}
    files: dict[tuple[str, str], Path] = {}
    unscopable: dict[str, tuple[str, ...]] = {}
    for path in paths:
        raw = path.read_bytes()
        request_path = path.with_name(
            f"request-{path.stem.removeprefix('simplefin-')}.json"
        )
        metadata: Mapping[str, Any] = {}
        if request_path.is_file():
            loaded = json.loads(request_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                metadata = loaded
        version, account_rows, errors = view_for(path)
        snapshot_sha256 = hashlib.sha256(raw).hexdigest()
        partition = partition_snapshot(
            snapshot_sha256=snapshot_sha256,
            observed_at=snapshot_observed_at(path, fallback),
            version=version,
            accounts=account_rows,
            errors=errors,
            declared_connection=declared[path],
            requested_start=_iso_date(metadata.get("requestedStart")),
            requested_end=_iso_date(metadata.get("requestedEnd")),
        )
        unscopable[snapshot_sha256] = partition.unscopable_errors
        for item in partition.evidence:
            grouped.setdefault(item.connection_id, []).append(item)
            key = (item.connection_id, snapshot_sha256)
            accounts[key] = partition.accounts_by_scope.get(
                item.connection_id, ()
            )
            files[key] = path
    return PartitionedEvidence(
        grouped={scope: tuple(items) for scope, items in grouped.items()},
        accounts=accounts,
        paths=files,
        unscopable_errors=unscopable,
    )