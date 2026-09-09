"""Database-neutral domain records for the finance evidence store."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

NAMESPACE = uuid.UUID("a571595f-e4ad-5b07-91c3-e21f18c93e5d")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MONEY_QUANTUM = Decimal("0.00000001")
MONEY_LIMIT = Decimal("10000000000000000")


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def stable_id(kind: str, *parts: object) -> str:
    value = "\x1f".join((kind, *(str(part) for part in parts)))
    return str(uuid.uuid5(NAMESPACE, value))


def normalized_description(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_hash(value: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise ValueError("expected a lowercase SHA-256 digest")
    return value


def normalize_money(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) >= MONEY_LIMIT:
        raise ValueError("money must be finite and fit numeric(24,8)")
    try:
        normalized = value.quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_EVEN
        ).normalize()
        return Decimal(0) if normalized.is_zero() else normalized
    except InvalidOperation as exc:
        raise ValueError("money must fit numeric(24,8)") from exc


@dataclass(frozen=True, slots=True)
class SourceBlob:
    id: str
    raw_locator: str
    content_hash: str
    observed_at: datetime
    processed_at: datetime
    byte_size: int
    media_type: str = "application/json"
    source_kind: str = "unknown"
    source_version: str = "unknown"

    def __post_init__(self) -> None:
        if not self.raw_locator or "://" not in self.raw_locator:
            raise ValueError("raw payload locator must identify external storage")
        require_hash(self.content_hash)
        if self.byte_size < 0:
            raise ValueError("byte_size cannot be negative")
        if not self.source_kind or not self.source_version:
            raise ValueError("source kind and version are required")
        utc(self.observed_at)
        if utc(self.processed_at) < utc(self.observed_at):
            raise ValueError("processed_at cannot precede observed_at")


@dataclass(frozen=True, slots=True)
class IngestionRun:
    id: str
    source_blob_id: str
    importer_name: str
    importer_version: str
    status: str
    effective_start: datetime | None
    effective_end: datetime | None
    observed_at: datetime
    processed_at: datetime
    records_seen: int = 0
    records_accepted: int = 0
    parser_hash: str | None = None
    source_protocol: str | None = None
    source_version: str | None = None
    overlap_start: datetime | None = None
    overlap_end: datetime | None = None
    sealed_plan_hash: str | None = None
    admission_hash: str | None = None

    def __post_init__(self) -> None:
        if self.parser_hash is not None:
            require_hash(self.parser_hash)
        if self.sealed_plan_hash is not None:
            require_hash(self.sealed_plan_hash)
        if self.admission_hash is not None:
            require_hash(self.admission_hash)
        if self.overlap_start is not None:
            utc(self.overlap_start)
        if self.overlap_end is not None:
            utc(self.overlap_end)
        if (
            self.overlap_start is not None
            and self.overlap_end is not None
            and self.overlap_end < self.overlap_start
        ):
            raise ValueError("overlap_end cannot precede overlap_start")


@dataclass(frozen=True, slots=True)
class SourceConnection:
    id: str
    source_system: str
    connection_key: str
    effective_from: datetime
    observed_at: datetime
    processed_at: datetime
    status: str = "active"


@dataclass(frozen=True, slots=True)
class SourceAccount:
    id: str
    connection_id: str
    external_id: str
    name: str
    account_type: str | None
    currency: str
    effective_from: datetime
    observed_at: datetime
    processed_at: datetime
    canonical_key: str
    trust_cutoff_at: datetime | None = None
    mapping_effective_from: datetime | None = None
    trust_cutoff_provided: bool = False
    status: str = "unknown"
    status_provided: bool = False


@dataclass(frozen=True, slots=True)
class TransactionObservation:
    id: str
    source_account_id: str
    source_transaction_id: str
    observation_hash: str
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    amount: Decimal
    currency: str
    description: str
    status: str
    source_blob_id: str
    ingestion_run_id: str
    last_seen_at: datetime
    sighting_order: int
    last_seen_run_id: str
    last_seen_order: int

    def __post_init__(self) -> None:
        require_hash(self.observation_hash)
        if not self.source_transaction_id:
            raise ValueError("source transaction ID is required")
        utc(self.effective_at)
        utc(self.observed_at)
        if utc(self.processed_at) < utc(self.observed_at):
            raise ValueError("processed_at cannot precede observed_at")
        if self.status not in {"pending", "posted"}:
            raise ValueError(f"unsupported transaction status: {self.status}")
        if self.amount != normalize_money(self.amount):
            raise ValueError("transaction amount must be normalized to numeric(24,8)")
        if self.sighting_order < 0:
            raise ValueError("sighting_order cannot be negative")
        if self.last_seen_order < 0:
            raise ValueError("last_seen_order cannot be negative")

    @property
    def semantic_key(self) -> tuple[str, Decimal, str]:
        return (
            self.effective_at.date().isoformat(),
            self.amount.normalize(),
            normalized_description(self.description),
        )


@dataclass(frozen=True, slots=True)
class BalanceObservation:
    id: str
    source_account_id: str
    observation_hash: str
    balance_type: str
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    amount: Decimal
    currency: str
    source_blob_id: str
    ingestion_run_id: str

    def __post_init__(self) -> None:
        if self.amount != normalize_money(self.amount):
            raise ValueError("balance amount must be normalized to numeric(24,8)")


@dataclass(frozen=True, slots=True)
class PositionObservation:
    id: str
    source_account_id: str
    external_position_id: str
    observation_hash: str
    instrument_key: str
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    quantity: Decimal
    cost_basis: Decimal | None
    currency: str | None
    source_blob_id: str
    ingestion_run_id: str


@dataclass(frozen=True, slots=True)
class ValuationObservation:
    id: str
    source_account_id: str
    observation_hash: str
    valuation_type: str
    instrument_key: str | None
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    value: Decimal
    currency: str
    source_blob_id: str
    ingestion_run_id: str


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    id: str
    ingestion_run_id: str
    source_blob_id: str
    observation_kind: str
    source_identity_hash: str
    observation_hash: str
    record_index: int
    effective_at: datetime | None
    observed_at: datetime
    processed_at: datetime
    payload: dict[str, Any]
    issue_id: str | None = None

    def __post_init__(self) -> None:
        require_hash(self.source_identity_hash)
        require_hash(self.observation_hash)
        if not self.observation_kind:
            raise ValueError("observation_kind is required")
        if self.record_index < 0:
            raise ValueError("record_index cannot be negative")
        if self.effective_at is not None:
            utc(self.effective_at)
        utc(self.observed_at)
        if utc(self.processed_at) < utc(self.observed_at):
            raise ValueError("processed_at cannot precede observed_at")
        canonical_json(self.payload)


@dataclass(frozen=True, slots=True)
class CanonicalAccount:
    id: str
    key: str
    display_name: str
    account_type: str | None
    currency: str
    effective_from: datetime
    observed_at: datetime
    processed_at: datetime
    status: str = "active"


@dataclass(frozen=True, slots=True)
class CanonicalTransaction:
    id: str
    account_id: str
    key: str
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    amount: Decimal
    currency: str
    description: str
    status: str
    correction_of_id: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationLink:
    id: str
    observation_id: str
    canonical_transaction_id: str
    method: str
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    issue_id: str | None = None
    decision_id: str | None = None
    current: bool = True


@dataclass(frozen=True, slots=True)
class DurableDecision:
    id: str
    decision_type: str
    subject_type: str
    subject_key: str
    action: str
    rationale: str
    decided_by: str
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    supersedes_id: str | None = None


@dataclass(frozen=True, slots=True)
class QualityIssue:
    id: str
    issue_type: str
    severity: str
    subject_type: str
    subject_key: str
    details: dict[str, Any]
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    status: str = "open"


@dataclass(frozen=True, slots=True)
class AppStateSnapshot:
    id: str
    app_name: str
    state_kind: str
    external_locator: str
    state_hash: str
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectionRun:
    id: str
    target_system: str
    version: str
    status: str
    cutoff_effective_at: datetime
    observed_at: datetime
    processed_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    id: str
    run_id: str
    canonical_transaction_id: str
    target_system: str
    target_record_key: str
    projected_hash: str
    status: str
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime
    current: bool = True


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    event_type: str
    actor: str
    subject_type: str
    subject_key: str
    data: dict[str, Any]
    effective_at: datetime
    observed_at: datetime
    processed_at: datetime


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    blob: SourceBlob
    run: IngestionRun
    connection: SourceConnection
    accounts: tuple[SourceAccount, ...]
    transactions: tuple[TransactionObservation, ...]
    balances: tuple[BalanceObservation, ...] = ()
    positions: tuple[PositionObservation, ...] = ()
    valuations: tuple[ValuationObservation, ...] = ()
    artifacts: tuple[ArtifactObservation, ...] = ()
    source_decisions: tuple[DurableDecision, ...] = ()
    source_issues: tuple[QualityIssue, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FinanceState:
    blobs: tuple[SourceBlob, ...] = ()
    runs: tuple[IngestionRun, ...] = ()
    connections: tuple[SourceConnection, ...] = ()
    source_accounts: tuple[SourceAccount, ...] = ()
    transaction_observations: tuple[TransactionObservation, ...] = ()
    balance_observations: tuple[BalanceObservation, ...] = ()
    position_observations: tuple[PositionObservation, ...] = ()
    valuation_observations: tuple[ValuationObservation, ...] = ()
    artifact_observations: tuple[ArtifactObservation, ...] = ()
    canonical_accounts: tuple[CanonicalAccount, ...] = ()
    canonical_transactions: tuple[CanonicalTransaction, ...] = ()
    links: tuple[ObservationLink, ...] = ()
    decisions: tuple[DurableDecision, ...] = ()
    issues: tuple[QualityIssue, ...] = ()
    app_state_snapshots: tuple[AppStateSnapshot, ...] = ()
    projection_runs: tuple[ProjectionRun, ...] = ()
    projection_records: tuple[ProjectionRecord, ...] = ()
    audit_events: tuple[AuditEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class CanonicalMutation:
    transaction_id: str
    effective_at: datetime
    amount: Decimal
    currency: str
    description: str
    status: str


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    canonical_accounts: tuple[CanonicalAccount, ...] = ()
    canonical_transactions: tuple[CanonicalTransaction, ...] = ()
    canonical_mutations: tuple[CanonicalMutation, ...] = ()
    links: tuple[ObservationLink, ...] = ()
    decisions: tuple[DurableDecision, ...] = ()
    issues: tuple[QualityIssue, ...] = ()
    projection_run: ProjectionRun | None = None
    projection_records: tuple[ProjectionRecord, ...] = ()
    projection_withdrawals: tuple[str, ...] = ()
    audit_events: tuple[AuditEvent, ...] = ()
    replayed_observation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IngestionResult:
    run_id: str
    accepted_observations: int
    replayed_observations: int
    canonical_transactions_created: int
    issues_created: int
    state_hash: str
