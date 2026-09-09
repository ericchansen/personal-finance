"""Read verified external finance artifacts into immutable shadow observations."""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from importers.audit import baseline, forensic
from importers.extracts import correspondence, parsers
from importers.facts.loader import load_facts
from importers.facts.schema import AccountFact, LoanFact, PropertyFact, VehicleFact
from importers.lineage_review import workflow as lineage_workflow
from importers.lineage_review.model import ReviewError
from importers.monarch.monarch import (
    build_profiles,
    detect_trust_cutoff,
    read_balances,
    read_transactions,
)
from importers.normalized import builder as normalized
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import validate_private_output

from .domain import (
    ArtifactObservation,
    DurableDecision,
    IngestionRun,
    ObservationBatch,
    QualityIssue,
    SourceAccount,
    SourceBlob,
    SourceConnection,
    content_hash,
    stable_id,
    utc,
)
from .simplefin import (
    SimpleFinAdapter,
    detect_protocol_version,
    scoped_account_identity,
)
from .source_admission import (
    AdmissionError,
    ConnectionAdmission,
    MonarchEntityVerdict,
    MonarchObservedEntity,
    SnapshotEvidence,
    balance_point,
    connection_id_for,
    evaluate_connection_scopes,
    evaluate_monarch_entity,
    parse_connection_decisions,
    parse_monarch_account_map,
    partition_snapshot,
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SHADOW_OUTPUT = Path("postgres-shadow")
NON_BLOCKING_EMPTY_CAPABILITY_GAPS = frozenset(
    {"budget-period-inventory", "exchange-rate-history"}
)
LINEAGE_OUTCOMES = frozenset(
    {
        "distinct-economic-events",
        "duplicate-economic-event",
        "transfer",
        "insufficient-evidence",
    }
)
_DATE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")
_TIME = re.compile(r"simplefin-(?P<time>\d{6})(?:-(?P<micro>\d{1,6}))?")


class SourceLoadError(RuntimeError):
    """A private source cannot be admitted into a sealed shadow plan."""


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    kind: str
    identity: Any
    payload: dict[str, Any]
    effective_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SourceFile:
    relative_path: str
    path_hash: str
    content_hash: str
    byte_size: int
    source_kind: str
    source_version: str
    parser_hash: str
    record_count: int


@dataclass(frozen=True, slots=True)
class LineageGroup:
    group_id: str
    candidate_hash: str
    audit_graph_hash: str
    evidence_set_hash: str
    member_count: int
    observed_at: datetime
    source_blob_id: str
    quality_issue_id: str
    member_identity_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LineageDecision:
    decision_id: str
    group_id: str
    decision_version: int
    candidate_hash: str
    audit_graph_hash: str
    evidence_set_hash: str
    outcome: str
    survivor_identity_hash: str | None
    rationale_hash: str
    decided_at: datetime
    observed_at: datetime
    source_path: str


@dataclass(frozen=True, slots=True)
class SourceCatalog:
    batches: tuple[ObservationBatch, ...]
    files: tuple[SourceFile, ...]
    lineage_groups: tuple[LineageGroup, ...]
    lineage_decisions: tuple[LineageDecision, ...]
    lineage_review_counts: dict[str, int]
    lineage_readiness_counts: dict[str, int]
    lineage_evidence_gap_counts: dict[str, int]
    blockers: tuple[str, ...]
    gaps: tuple[str, ...]

    @property
    def source_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.source_kind for item in self.files).items()))

    @property
    def observation_count(self) -> int:
        return sum(
            len(batch.transactions)
            + len(batch.balances)
            + len(batch.positions)
            + len(batch.valuations)
            + len(batch.artifacts)
            for batch in self.batches
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser_hash(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted({item.resolve() for item in paths}, key=str):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, set):
        return sorted(_jsonable(child) for child in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SourceLoadError(f"unsupported observation value: {type(value).__name__}")


def _relative(root: Path, path: Path) -> str:
    resolved = path.resolve(strict=True)
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        raise SourceLoadError("source path leaves the external data root") from None


def _observed_from_path(path: Path, fallback: datetime) -> datetime:
    date_match = next(
        (
            _DATE.fullmatch(part)
            for part in reversed(path.parts)
            if _DATE.fullmatch(part)
        ),
        None,
    )
    if date_match is None:
        return fallback
    parsed_date = date.fromisoformat(date_match.group("date"))
    time_match = _TIME.search(path.stem)
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


def _effective_at(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.isdigit()
    ):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    raw = str(value)
    try:
        if len(raw) == 10:
            return datetime.combine(
                date.fromisoformat(raw), time.min, tzinfo=timezone.utc
            )
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return utc(parsed)
    except ValueError:
        return None


def _media_type(path: Path) -> str:
    return {
        ".csv": "text/csv",
        ".json": "application/json",
        ".ofx": "application/x-ofx",
        ".qfx": "application/x-ofx",
    }.get(path.suffix.casefold(), "application/octet-stream")


def _artifacts(
    blob: SourceBlob,
    run: IngestionRun,
    records: Iterable[ArtifactRecord],
) -> tuple[ArtifactObservation, ...]:
    output = []
    for index, record in enumerate(records):
        payload = _jsonable(record.payload)
        identity_hash = content_hash(_jsonable(record.identity))
        observation_hash = content_hash(payload)
        output.append(
            ArtifactObservation(
                id=stable_id(
                    "artifact_observation",
                    blob.id,
                    record.kind,
                    identity_hash,
                    observation_hash,
                    index,
                ),
                ingestion_run_id=run.id,
                source_blob_id=blob.id,
                observation_kind=record.kind,
                source_identity_hash=identity_hash,
                observation_hash=observation_hash,
                record_index=index,
                effective_at=record.effective_at,
                observed_at=run.observed_at,
                processed_at=run.processed_at,
                payload=payload,
            )
        )
    return tuple(output)


def _artifact_batch(
    root: Path,
    path: Path,
    *,
    source_kind: str,
    source_version: str,
    parser_paths: tuple[Path, ...],
    records: Iterable[ArtifactRecord],
    observed_at: datetime,
    source_issues: tuple[QualityIssue, ...] = (),
) -> tuple[ObservationBatch, SourceFile]:
    records = tuple(records)
    relative = _relative(root, path)
    blob_hash = _sha256(path)
    parser_hash = _parser_hash(Path(__file__), *parser_paths)
    blob_id = stable_id("source_blob", relative, blob_hash)
    blob = SourceBlob(
        id=blob_id,
        raw_locator=f"external+sha256://{content_hash(relative)}",
        content_hash=blob_hash,
        observed_at=observed_at,
        processed_at=observed_at,
        byte_size=path.stat().st_size,
        media_type=_media_type(path),
        source_kind=source_kind,
        source_version=source_version,
    )
    run_id = stable_id(
        "ingestion_run", blob_id, source_kind, source_version, parser_hash
    )
    effective_values = [
        item.effective_at for item in records if item.effective_at is not None
    ]
    run = IngestionRun(
        id=run_id,
        source_blob_id=blob_id,
        importer_name=source_kind,
        importer_version=source_version,
        status="succeeded",
        effective_start=min(effective_values) if effective_values else None,
        effective_end=max(effective_values) if effective_values else None,
        observed_at=observed_at,
        processed_at=observed_at,
        records_seen=len(records),
        records_accepted=len(records),
        parser_hash=parser_hash,
        source_protocol=source_kind,
        source_version=source_version,
    )
    connection_key = content_hash(
        {"sourceKind": source_kind, "parserHash": parser_hash}
    )
    connection = SourceConnection(
        id=stable_id("source_connection", source_kind, connection_key),
        source_system=source_kind,
        connection_key=f"sha256:{connection_key}",
        effective_from=observed_at,
        observed_at=observed_at,
        processed_at=observed_at,
    )
    batch = ObservationBatch(
        blob=blob,
        run=run,
        connection=connection,
        accounts=(),
        transactions=(),
        artifacts=_artifacts(blob, run, records),
        source_issues=source_issues,
    )
    source_file = SourceFile(
        relative_path=relative,
        path_hash=content_hash(relative),
        content_hash=blob_hash,
        byte_size=path.stat().st_size,
        source_kind=source_kind,
        source_version=source_version,
        parser_hash=parser_hash,
        record_count=len(records),
    )
    return batch, source_file


def _fact_accounts(root: Path) -> tuple[dict[str, AccountFact], list[Any]]:
    result = load_facts(root / "facts")
    if result.errors:
        raise SourceLoadError("facts-validation-error")
    accounts = {
        parsed.fact.id: parsed.fact
        for parsed in result.facts
        if isinstance(parsed.fact, AccountFact)
    }
    return accounts, list(result.facts)


def _canonical_entity_index(parsed_facts: list[Any]) -> dict[str, str]:
    """Every canonical entity id the normalized builder materializes, by kind.

    Account facts keep their own ids; loan, property, and vehicle facts are
    materialized under a namespaced id, so a durable mapping decision can name
    one.  Restricting targets to account facts alone made it impossible to point
    an observed balance-only entity at the loan or vehicle it actually is.
    """

    index: dict[str, str] = {}
    for parsed in parsed_facts:
        fact = parsed.fact
        if isinstance(fact, AccountFact):
            if fact.id:
                index[fact.id] = "account"
        elif isinstance(fact, (LoanFact, PropertyFact, VehicleFact)):
            # ``fact_id`` is the namespaced canonical id the parser assigns and
            # the normalized builder materializes; never rebuild it here.
            if parsed.fact_id:
                index[parsed.fact_id] = parsed.fact_type
    return index


def _canonical_entity_evidence(parsed_facts: list[Any]) -> dict[str, str]:
    """Hash of the fact behind each canonical entity id.

    A targeted decision binds this so that editing the fact it was made against
    — a renamed lender, a restated principal — invalidates the decision instead
    of silently inheriting it.  Only the hash is ever published.
    """

    evidence: dict[str, str] = {}
    for parsed in parsed_facts:
        fact = parsed.fact
        if isinstance(fact, AccountFact):
            key = fact.id
        elif isinstance(fact, (LoanFact, PropertyFact, VehicleFact)):
            key = parsed.fact_id
        else:
            continue
        if not key:
            continue
        evidence[key] = content_hash(
            {
                "factType": parsed.fact_type,
                "factId": parsed.fact_id,
                "data": _jsonable(parsed.data),
            }
        )
    return evidence


def _observed_entity(
    name: str,
    profile: Any,
    points: dict[str, list[tuple[str, str]]],
    source_hashes: tuple[str, ...],
) -> MonarchObservedEntity:
    return MonarchObservedEntity(
        source_account=name,
        transaction_count=profile.txn_count,
        balance_count=profile.balance_count,
        trust_cutoff_day=profile.trust_cutoff,
        needs_review=profile.needs_review,
        account_type=profile.account_type,
        balance_points=tuple(sorted(points.get(name, ()))),
        source_hashes=source_hashes,
    )


def monarch_observed_entities(root: Path) -> dict[str, MonarchObservedEntity]:
    """Observed evidence for every Monarch profile, keyed by source account.

    This is how a durable decision learns the ``observedEvidenceHash`` it must
    bind: read the entity, copy its hash into the private map, and the evaluator
    will refuse the decision the moment the underlying values change.
    """

    transaction_paths = sorted(
        (root / "legacy" / "monarch").glob("Transactions_*.csv")
    )
    balance_paths = sorted((root / "legacy" / "monarch").glob("Balances_*.csv"))
    if not transaction_paths or not balance_paths:
        raise SourceLoadError("monarch-export-incomplete")
    transactions = [
        transaction
        for path in transaction_paths
        for transaction in read_transactions(path)
    ]
    balances = [point for path in balance_paths for point in read_balances(path)]
    points: dict[str, list[tuple[str, str]]] = {}
    for point in balances:
        points.setdefault(point.account, []).append(
            balance_point(point.date, point.balance)
        )
    source_hashes = tuple(
        sorted(_sha256(path) for path in (*transaction_paths, *balance_paths))
    )
    return {
        name: _observed_entity(name, profile, points, source_hashes)
        for name, profile in sorted(
            build_profiles(transactions, balances).items()
        )
    }


def canonical_entity_evidence(root: Path) -> dict[str, str]:
    """Target-fact hashes a durable decision may bind, keyed by canonical id."""

    _accounts, parsed_facts = _fact_accounts(root)
    return _canonical_entity_evidence(parsed_facts)


def _mapping_target(entry: dict[str, Any]) -> str:
    action = str(entry.get("action") or "import")
    if action == "import":
        return str(
            entry.get("assertionAccountId")
            or entry.get("wealthfolioAccountId")
            or ""
        )
    if action == "monitor":
        return str(
            entry.get("assertionAccountId")
            or entry.get("wealthfolioAlternativeAssetId")
            or ""
        )
    return str(entry.get("assertionAccountId") or "")


def _simplefin_batches(
    root: Path,
    generated_at: datetime,
    facts: dict[str, AccountFact],
) -> tuple[list[ObservationBatch], list[SourceFile], list[str], list[str]]:
    mapping_path = root / "simplefin" / "account-map.json"
    try:
        mapping_document = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceLoadError("simplefin-mapping-invalid") from exc
    mapping = mapping_document.get("accounts")
    if mapping_document.get("version") != 1 or not isinstance(mapping, dict):
        raise SourceLoadError("simplefin-mapping-invalid")
    admission_hash = content_hash(
        {
            "mapping": mapping_document,
            "accountFacts": {
                key: _jsonable(value) for key, value in sorted(facts.items())
            },
            "loaderHash": _parser_hash(Path(__file__)),
        }
    )

    batches: list[ObservationBatch] = []
    files: list[SourceFile] = []
    gaps: list[str] = []
    blockers: list[str] = []
    mapping_records = [
        ArtifactRecord(
            "simplefin-account-mapping",
            source_id,
            {"sourceAccountId": source_id, **_jsonable(entry)},
        )
        for source_id, entry in sorted(mapping.items())
        if isinstance(entry, dict)
    ]

    snapshots = sorted((root / "raw" / "simplefin").rglob("simplefin-*.json"))
    if not snapshots:
        raise SourceLoadError("simplefin-snapshots-missing")
    try:
        connection_decisions = parse_connection_decisions(
            mapping_document.get("connections")
        )
    except AdmissionError as exc:
        raise SourceLoadError(str(exc)) from exc
    evidence_by_connection: dict[str, list[SnapshotEvidence]] = {}
    adapter = SimpleFinAdapter("external-snapshot-collector")
    for path in snapshots:
        raw = path.read_bytes()
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceLoadError("simplefin-snapshot-invalid") from exc
        protocol_version = detect_protocol_version(document)
        observed_at = _observed_from_path(path, generated_at)
        request_path = path.with_name(
            f"request-{path.stem.removeprefix('simplefin-')}.json"
        )
        overlap_start = None
        overlap_end = None
        request_metadata: dict[str, Any] = {}
        if request_path.is_file():
            try:
                request_metadata = json.loads(
                    request_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise SourceLoadError(
                    "simplefin-request-metadata-invalid"
                ) from exc
            if (
                request_metadata.get("schemaVersion") != 1
                or str(request_metadata.get("protocolVersion"))
                != protocol_version
                or request_metadata.get("snapshotSha256")
                != hashlib.sha256(raw).hexdigest()
                or request_metadata.get("pendingIncluded") is not True
            ):
                raise SourceLoadError(
                    "simplefin-request-metadata-binding-mismatch"
                )
            request_start = _effective_at(
                request_metadata.get("requestedStart")
            )
            request_end = _effective_at(request_metadata.get("requestedEnd"))
            if request_start is None or request_end is None:
                raise SourceLoadError(
                    "simplefin-request-window-invalid"
                )
            overlap_start = request_start
            overlap_end = request_end + timedelta(days=1) - timedelta(
                microseconds=1
            )
            request_batch, request_file = _artifact_batch(
                root,
                request_path,
                source_kind="simplefin-request-metadata",
                source_version="v1",
                parser_paths=(Path(__file__),),
                records=[
                    ArtifactRecord(
                        "simplefin-request-window",
                        hashlib.sha256(raw).hexdigest(),
                        request_metadata,
                        effective_at=overlap_end,
                    )
                ],
                observed_at=observed_at,
            )
            batches.append(request_batch)
            files.append(request_file)
        else:
            gaps.append("simplefin-overlap-window-missing")
        batch = adapter.parse(
            raw,
            raw_locator=f"external+sha256://{content_hash(_relative(root, path))}",
            observed_at=observed_at,
            processed_at=max(observed_at, generated_at),
            protocol_version=protocol_version,
            overlap_start=overlap_start,
            overlap_end=overlap_end,
        )
        # Snapshots are immutable, so an institution error is permanent history
        # for its own snapshot. Whether it blocks admission is a connection-
        # scoped freshness question, decided once after every snapshot is read.
        payload = (
            document.get("data")
            if protocol_version == "2"
            and isinstance(document.get("data"), dict)
            else document
        )
        account_rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(account_rows, list):
            raise SourceLoadError("simplefin-accounts-invalid")
        partition = partition_snapshot(
            snapshot_sha256=hashlib.sha256(raw).hexdigest(),
            observed_at=observed_at,
            version=protocol_version,
            accounts=account_rows,
            errors=batch.errors,
            declared_connection=connection_id_for(request_metadata),
            requested_start=overlap_start.date() if overlap_start else None,
            requested_end=(
                _effective_at(request_metadata.get("requestedEnd")).date()
                if request_metadata.get("requestedEnd")
                else None
            ),
        )
        for item in partition.evidence:
            evidence_by_connection.setdefault(item.connection_id, []).append(item)

        rows_by_id = {
            scoped_account_identity(row, protocol_version): row
            for row in account_rows
            if isinstance(row, dict)
        }
        provider_id_counts = Counter(
            str(row.get("id") or row.get("account_id") or "")
            for row in account_rows
            if isinstance(row, dict)
        )
        admitted_accounts: list[SourceAccount] = []
        admitted_ids: set[str] = set()
        artifacts: list[ArtifactRecord] = []
        for account in batch.accounts:
            row = rows_by_id[account.external_id]
            provider_account_id = str(
                row.get("id") or row.get("account_id") or ""
            )
            entry = mapping.get(account.external_id)
            if entry is None and provider_id_counts[provider_account_id] == 1:
                entry = mapping.get(provider_account_id)
            if not isinstance(entry, dict):
                raise SourceLoadError("simplefin-account-unmapped")
            action = str(entry.get("action") or "import")
            target = _mapping_target(entry)
            fact = facts.get(target)
            mapping_problem = None
            if action not in {"import", "monitor", "observe", "exclude"}:
                mapping_problem = "simplefin-mapping-action-invalid"
            elif action == "import" and fact is None:
                mapping_problem = "simplefin-canonical-account-unresolved"
            elif action in {"monitor", "observe"} and not target:
                mapping_problem = f"simplefin-{action}-target-missing"
            elif action == "exclude" and not str(
                entry.get("decision") or ""
            ).strip():
                mapping_problem = "simplefin-exclusion-decision-missing"
            organization = row.get("org")
            if (
                protocol_version == "1"
                and (
                    not isinstance(organization, dict)
                    or not any(
                        organization.get(key)
                        for key in ("domain", "sfin-url", "url", "name")
                    )
                )
            ):
                mapping_problem = "simplefin-v1-organization-scope-missing"
            if mapping_problem:
                blockers.append(mapping_problem)
            account_payload = {
                key: value for key, value in row.items() if key != "transactions"
            }
            artifacts.append(
                ArtifactRecord(
                    "simplefin-account",
                    {"protocol": protocol_version, "account": account.external_id},
                    {
                        "protocolVersion": protocol_version,
                        "mappingAction": action,
                        "overlapStart": (
                            overlap_start.isoformat() if overlap_start else None
                        ),
                        "overlapEnd": (
                            overlap_end.isoformat() if overlap_end else None
                        ),
                        "account": account_payload,
                    },
                    effective_at=account.effective_from,
                )
            )
            for transaction in row.get("transactions") or []:
                if isinstance(transaction, dict):
                    artifacts.append(
                        ArtifactRecord(
                            "simplefin-transaction",
                            {
                                "account": account.external_id,
                                "transaction": transaction.get("id")
                                or transaction.get("transaction_id"),
                            },
                            {
                                "protocolVersion": protocol_version,
                                "mappingAction": action,
                                "accountId": account.external_id,
                                "transaction": transaction,
                            },
                            effective_at=_effective_at(
                                transaction.get("posted")
                                or transaction.get("posted_at")
                                or transaction.get("transacted_at")
                            ),
                        )
                    )
            if mapping_problem:
                continue
            if action != "import":
                gaps.append(f"simplefin-{action}-observed-not-canonicalized")
                continue
            cutoff_value = entry.get("trustCutoffAt")
            trust_cutoff = _effective_at(cutoff_value)
            if cutoff_value and trust_cutoff is None:
                raise SourceLoadError("simplefin-trust-cutoff-invalid")
            if trust_cutoff is None and fact and fact.closed:
                trust_cutoff = datetime.combine(
                    fact.closed, time.max, tzinfo=timezone.utc
                )
            admitted_ids.add(account.id)
            admitted_accounts.append(
                replace(
                    account,
                    name=fact.display_name if fact else account.name,
                    account_type=fact.kind if fact else account.account_type,
                    canonical_key=target,
                    trust_cutoff_at=trust_cutoff,
                    trust_cutoff_provided=trust_cutoff is not None,
                    status=(
                        "excluded"
                        if fact and fact.excluded
                        else "closed"
                        if fact and fact.closed
                        else "active"
                    ),
                    status_provided=True,
                )
            )
        run_id = stable_id(
            "ingestion_run_admission",
            batch.run.id,
            batch.run.parser_hash,
            admission_hash,
        )
        run = replace(
            batch.run,
            id=run_id,
            records_seen=len(artifacts),
            records_accepted=sum(
                item.source_account_id in admitted_ids
                for item in (*batch.transactions, *batch.balances)
            ),
            admission_hash=admission_hash,
        )
        admitted_transactions = tuple(
            replace(
                item,
                ingestion_run_id=run_id,
                last_seen_run_id=run_id,
            )
            for item in batch.transactions
            if item.source_account_id in admitted_ids
        )
        admitted_balances = tuple(
            replace(item, ingestion_run_id=run_id)
            for item in batch.balances
            if item.source_account_id in admitted_ids
        )
        batch = replace(
            batch,
            run=run,
            accounts=tuple(admitted_accounts),
            transactions=admitted_transactions,
            balances=admitted_balances,
            artifacts=_artifacts(batch.blob, run, artifacts),
        )
        batches.append(batch)
        files.append(
            SourceFile(
                relative_path=_relative(root, path),
                path_hash=content_hash(_relative(root, path)),
                content_hash=batch.blob.content_hash,
                byte_size=batch.blob.byte_size,
                source_kind="simplefin-snapshot",
                source_version=f"v{protocol_version}",
                parser_hash=batch.run.parser_hash or "",
                record_count=len(artifacts),
            )
        )
    admissions: list[ConnectionAdmission] = []
    try:
        scopes = evaluate_connection_scopes(
            evidence_by_connection, connection_decisions, as_of=generated_at
        )
    except AdmissionError as exc:
        raise SourceLoadError(str(exc)) from exc
    for admission in scopes.admissions:
        admissions.append(admission)
        if admission.blocker:
            # Named per connection so a failing scope is never mistaken for a
            # global failure, and a healthy sibling is never implicated.
            blockers.append(admission.blocker)
            blockers.append(
                f"simplefin-connection-blocked:{admission.connection_id}"
            )
            blockers.append(
                "simplefin-institution-error-count:"
                f"{len(admission.current_errors)}"
            )
        if admission.gap:
            gaps.append(admission.gap)
        if admission.advisories:
            # Reported so the advisory is never invisible, but as a gap and not
            # a blocker: every institution answered.
            gaps.append(
                "simplefin-connection-advisory-count:"
                f"{len(admission.advisories)}"
            )
        if admission.stale and admission.staleness_days is not None:
            gaps.append(
                "simplefin-connection-staleness-days:"
                f"{admission.staleness_days}"
            )
    admission_batch, admission_file = _artifact_batch(
        root,
        mapping_path,
        source_kind="simplefin-account-map",
        source_version="v1",
        parser_paths=(Path(__file__),),
        records=[
            *mapping_records,
            *(
                ArtifactRecord(
                    "simplefin-connection-admission",
                    admission.connection_id,
                    {
                        "connectionId": admission.connection_id,
                        "latestSnapshotSha256": admission.latest_snapshot_sha256,
                        "admittedSnapshotSha256": (
                            admission.admitted_snapshot_sha256
                        ),
                        "currentErrorCount": len(admission.current_errors),
                        "advisoryCount": len(admission.advisories),
                        "admittedRequestedStart": (
                            admission.admitted_requested_start.isoformat()
                            if admission.admitted_requested_start
                            else None
                        ),
                        "admittedRequestedEnd": (
                            admission.admitted_requested_end.isoformat()
                            if admission.admitted_requested_end
                            else None
                        ),
                        "supersededErrorSnapshotCount": len(
                            admission.superseded_error_snapshots
                        ),
                        "fresh": admission.fresh,
                        "stale": admission.stale,
                        "stalenessDays": admission.staleness_days,
                        "blocker": admission.blocker,
                        "gap": admission.gap,
                        "proof": admission.proof,
                    },
                )
                for admission in admissions
            ),
        ],
        observed_at=generated_at,
    )
    batches.append(admission_batch)
    files.append(admission_file)
    return batches, files, gaps, blockers


def _fact_batches(
    root: Path,
    generated_at: datetime,
    parsed_facts: list[Any],
) -> tuple[list[ObservationBatch], list[SourceFile]]:
    batches = []
    files = []
    by_path: dict[Path, list[Any]] = {}
    for parsed in parsed_facts:
        by_path.setdefault(parsed.path, []).append(parsed)
    for path, values in sorted(by_path.items(), key=lambda item: str(item[0])):
        records = [
            ArtifactRecord(
                f"fact-{parsed.fact_type}",
                parsed.fact_id,
                parsed.data,
                effective_at=_effective_at(
                    parsed.data.get("date")
                    or parsed.data.get("decidedOn")
                    or parsed.data.get("opened")
                ),
            )
            for parsed in values
        ]
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind="fact",
            source_version="facts-v1",
            parser_paths=(
                Path(__import__("importers.facts.loader", fromlist=["x"]).__file__),
                Path(__import__("importers.facts.schema", fromlist=["x"]).__file__),
            ),
            records=records,
            observed_at=generated_at,
        )
        batches.append(batch)
        files.append(source_file)
    return batches, files


def _extract_batches(
    root: Path,
    generated_at: datetime,
    facts: dict[str, AccountFact],
) -> tuple[list[ObservationBatch], list[SourceFile]]:
    mapping_path = root / "extracts" / "mapping.json"
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceLoadError("extract-mapping-invalid") from exc
    entries = mapping.get("files")
    if not isinstance(entries, list):
        raise SourceLoadError("extract-mapping-invalid")
    names: dict[str, list[str]] = {}
    for fact in facts.values():
        if fact.display_name:
            names.setdefault(fact.display_name.casefold(), []).append(fact.id)
    batches: list[ObservationBatch] = []
    files: list[SourceFile] = []
    mapping_batch, mapping_file = _artifact_batch(
        root,
        mapping_path,
        source_kind="extract-map",
        source_version="v1",
        parser_paths=(Path(parsers.__file__),),
        records=[
            ArtifactRecord("extract-mapping", index, entry)
            for index, entry in enumerate(entries)
            if isinstance(entry, dict)
        ],
        observed_at=generated_at,
    )
    batches.append(mapping_batch)
    files.append(mapping_file)
    specifications = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceLoadError("extract-mapping-entry-invalid")
        relative = Path(str(entry.get("file") or ""))
        if relative.is_absolute() or not relative.parts:
            raise SourceLoadError("extract-mapping-path-invalid")
        path = (root / "extracts" / relative).resolve()
        _relative(root, path)
        account_reference = str(entry.get("account") or "")
        if account_reference in facts:
            target = account_reference
        else:
            matches = names.get(account_reference.casefold(), [])
            if len(matches) > 1:
                raise SourceLoadError("extract-account-ambiguous")
            target = matches[0] if matches else ""
        if not target:
            raise SourceLoadError("extract-account-unresolved")
        specifications.append((path, entry, target))
    try:
        parsed = correspondence.parse_mapped_extracts(specifications, data_root=root)
    except ValueError as exc:
        raise SourceLoadError("extract-correspondence-invalid") from exc
    supporting_paths: set[Path] = set()
    for (path, _entry, target), corroborated in zip(
        specifications, parsed, strict=True
    ):
        extract = corroborated.extract
        records = [
            ArtifactRecord(
                "extract-transaction",
                {
                    "account": target,
                    "sourceId": transaction.source_id,
                    "format": extract.format,
                },
                {
                    "accountId": target,
                    "format": extract.format,
                    "transaction": _jsonable(transaction),
                },
                effective_at=_effective_at(transaction.date),
            )
            for transaction in extract.transactions
        ]
        records.extend(
            ArtifactRecord("extract-description-evidence", item["sourceId"], item)
            for item in corroborated.description_evidence
        )
        for supporting_path in corroborated.supporting_files:
            if supporting_path in supporting_paths:
                continue
            supporting_paths.add(supporting_path)
            supporting_batch, supporting_file = _artifact_batch(
                root,
                supporting_path,
                source_kind="paired-bank-export",
                source_version="citi-csv",
                parser_paths=(Path(parsers.__file__), Path(correspondence.__file__)),
                records=(),
                observed_at=generated_at,
            )
            batches.append(supporting_batch)
            files.append(supporting_file)
        if extract.balance is not None:
            records.append(
                ArtifactRecord(
                    "extract-balance",
                    {"account": target, "date": extract.balance_date},
                    {
                        "accountId": target,
                        "balance": extract.balance,
                        "balanceDate": extract.balance_date,
                    },
                    effective_at=_effective_at(extract.balance_date),
                )
            )
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind=f"normalized-{extract.format}",
            source_version="parser-v1",
            parser_paths=(Path(parsers.__file__), Path(correspondence.__file__)),
            records=records,
            observed_at=generated_at,
        )
        batches.append(batch)
        files.append(source_file)
    return batches, files


def _monarch_batches(
    root: Path,
    generated_at: datetime,
    facts: dict[str, AccountFact],
    entities: dict[str, str] | None = None,
    target_evidence: dict[str, str] | None = None,
) -> tuple[list[ObservationBatch], list[SourceFile], list[str], list[str]]:
    # Monarch decisions may target any canonical entity the builder materializes;
    # SimpleFIN account validation deliberately stays account-fact-only.
    known_entities = {**{key: "account" for key in facts}, **(entities or {})}
    mapping_path = root / "normalized" / "monarch-account-map.json"
    try:
        mapping_document = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceLoadError("monarch-mapping-invalid") from exc
    try:
        decisions = parse_monarch_account_map(mapping_document)
    except AdmissionError as exc:
        raise SourceLoadError(str(exc)) from exc
    for entity in decisions.values():
        if entity.legacy_string_form and entity.target not in known_entities:
            raise SourceLoadError("monarch-account-unresolved")

    batches: list[ObservationBatch] = []
    files: list[SourceFile] = []
    gaps: list[str] = []
    blockers: list[str] = []
    mapping_records = [
        ArtifactRecord(
            "monarch-account-mapping",
            source_name,
            {"sourceAccount": source_name, "canonicalAccountId": entity.target}
            if entity.legacy_string_form
            else {
                "sourceAccount": source_name,
                "canonicalAccountId": entity.target,
                "action": entity.action,
                "decision": entity.document(),
            },
        )
        for source_name, entity in sorted(decisions.items())
    ]

    transaction_paths = sorted(
        (root / "legacy" / "monarch").glob("Transactions_*.csv")
    )
    balance_paths = sorted((root / "legacy" / "monarch").glob("Balances_*.csv"))
    if not transaction_paths or not balance_paths:
        raise SourceLoadError("monarch-export-incomplete")

    all_transactions = [
        transaction for path in transaction_paths for transaction in read_transactions(path)
    ]
    all_balances = [
        point for path in balance_paths for point in read_balances(path)
    ]
    profiles = build_profiles(all_transactions, all_balances)
    # The value multiset behind every profile, plus the files it came from, so a
    # decision binds what it actually saw rather than only how many rows it saw.
    observed_points: dict[str, list[tuple[str, str]]] = {}
    for point in all_balances:
        observed_points.setdefault(point.account, []).append(
            balance_point(point.date, point.balance)
        )
    monarch_source_hashes = tuple(
        sorted(_sha256(path) for path in (*transaction_paths, *balance_paths))
    )
    verdicts: dict[str, MonarchEntityVerdict] = {}
    admission_records: list[ArtifactRecord] = []
    unmapped = 0
    for name, profile in sorted(profiles.items()):
        verdict = evaluate_monarch_entity(
            _observed_entity(
                name, profile, observed_points, monarch_source_hashes
            ),
            decisions.get(name),
            known_targets=known_entities,
            target_evidence=target_evidence or {},
        )
        verdicts[name] = verdict
        admission_records.append(
            ArtifactRecord(
                "monarch-entity-admission",
                content_hash(name),
                {
                    "admitted": verdict.admitted,
                    "canonicalizeTransactions": verdict.canonicalize_transactions,
                    "preserveBalances": verdict.preserve_balances,
                    "blocker": verdict.blocker,
                    "gap": verdict.gap,
                    "proof": verdict.proof,
                },
            )
        )
        if verdict.blocker:
            if verdict.decision is None:
                unmapped += 1
            else:
                blockers.append(verdict.blocker)
        if verdict.gap:
            gaps.append(verdict.gap)
        if profile.needs_review and not (
            verdict.decision and verdict.decision.review_acknowledged
        ):
            gaps.append("monarch-account-status-review-required")
    if unmapped:
        blockers.append(f"monarch-account-unmapped-count:{unmapped}")
    mapping_batch, mapping_file = _artifact_batch(
        root,
        mapping_path,
        source_kind="monarch-account-map",
        source_version="v1",
        parser_paths=(
            Path(__import__("importers.monarch.monarch", fromlist=["x"]).__file__),
        ),
        records=[*mapping_records, *admission_records],
        observed_at=generated_at,
    )
    batches.append(mapping_batch)
    files.append(mapping_file)

    def _target_for(source_account: str) -> str:
        verdict = verdicts.get(source_account)
        if (
            verdict is not None
            and verdict.admitted
            and verdict.decision is not None
            and verdict.decision.target
        ):
            return verdict.decision.target
        return f"unmapped:{content_hash(source_account)}"

    parser_path = Path(
        __import__("importers.monarch.monarch", fromlist=["x"]).__file__
    )
    for path in transaction_paths:
        fallback_counts: Counter[str] = Counter()
        records = []
        for transaction in read_transactions(path):
            verdict = verdicts.get(transaction.account)
            target = (
                _target_for(transaction.account)
                if verdict is not None and verdict.canonicalize_transactions
                else f"unmapped:{content_hash(transaction.account)}"
            )
            source_id = transaction.source_id
            if not source_id:
                base = content_hash(
                    _jsonable({
                        "account": target,
                        "date": transaction.date,
                        "amount": transaction.amount,
                        "merchant": transaction.merchant,
                        "category": transaction.category,
                    })
                )
                fallback_counts[base] += 1
                source_id = f"{base}:{fallback_counts[base]}"
            records.append(
                ArtifactRecord(
                    "monarch-transaction",
                    {"account": target, "sourceId": source_id},
                    {
                        "canonicalAccountId": target,
                        "transaction": _jsonable(transaction),
                    },
                    effective_at=_effective_at(transaction.date),
                )
            )
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind="monarch-legacy-transactions",
            source_version="csv-v1",
            parser_paths=(parser_path,),
            records=records,
            observed_at=generated_at,
        )
        batches.append(batch)
        files.append(source_file)
    for path in balance_paths:
        records = []
        points = read_balances(path)
        by_account: dict[str, list[Any]] = {}
        for point in points:
            by_account.setdefault(point.account, []).append(point)
        cutoffs = {
            account: detect_trust_cutoff(values)[0]
            for account, values in by_account.items()
        }
        for point in points:
            verdict = verdicts.get(point.account)
            target = _target_for(point.account)
            payload: dict[str, Any] = {
                "canonicalAccountId": target,
                "balance": point.balance,
                "date": point.date,
                "trustCutoff": cutoffs[point.account],
                "trusted": (
                    cutoffs[point.account] is None
                    or point.date <= cutoffs[point.account]
                ),
            }
            if (
                verdict is not None
                and verdict.decision is not None
                and not verdict.decision.legacy_string_form
            ):
                payload["admissionAction"] = verdict.decision.action
                payload["admissionDecisionId"] = verdict.decision.decision_id
                payload["canonicalized"] = verdict.canonicalize_transactions
            records.append(
                ArtifactRecord(
                    "monarch-balance",
                    {"account": target, "date": point.date},
                    payload,
                    effective_at=_effective_at(point.date),
                )
            )
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind="monarch-legacy-balances",
            source_version="csv-v1",
            parser_paths=(parser_path,),
            records=records,
            observed_at=generated_at,
        )
        batches.append(batch)
        files.append(source_file)
    return batches, files, gaps, blockers


def _canonical_batches(
    root: Path,
    generated_at: datetime,
) -> tuple[
    list[ObservationBatch],
    list[SourceFile],
    list[str],
    list[str],
    dict[str, Any] | None,
]:
    verified = normalized.verify_publication(root)
    canonical = root / "normalized" / "canonical"
    manifest_path = canonical / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["schemaVersion"] == normalized.SCHEMA_VERSION:
        try:
            verified = normalized.verify(root)
        except normalized.BuildError as exc:
            raise SourceLoadError(
                "canonical-publication-verification-failed"
            ) from exc
    files = []
    batches = []
    gaps = [
        "canonical-publication-warning"
        for _warning in verified.get("warnings", [])
    ]
    blockers = []
    lineage_binding = None
    if manifest["schemaVersion"] == normalized.SCHEMA_VERSION:
        if isinstance(manifest.get("lineageReview"), dict):
            lineage_binding = manifest["lineageReview"]
        else:
            blockers.append("canonical-lineage-binding-missing")
    else:
        blockers.append("canonical-lineage-publication-required")
    manifest_batch, manifest_file = _artifact_batch(
        root,
        manifest_path,
        source_kind="canonical-publication-manifest",
        source_version=f"v{manifest['schemaVersion']}",
        parser_paths=(Path(normalized.__file__),),
        records=[
            ArtifactRecord(
                "canonical-publication-manifest",
                manifest["schemaVersion"],
                manifest,
                effective_at=_effective_at(manifest.get("buildTimestamp")),
            )
        ],
        observed_at=_effective_at(manifest.get("buildTimestamp")) or generated_at,
    )
    batches.append(manifest_batch)
    files.append(manifest_file)
    for name in normalized.CSV_OUTPUT_NAMES:
        path = canonical / name
        with path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        kind = name.removesuffix(".csv")
        records = []
        for index, row in enumerate(rows):
            identity = (
                row.get("source_id")
                or row.get("account_id")
                or row.get("entity_id")
                or f"{kind}:{index}"
            )
            effective = (
                row.get("date") or row.get("as_of") or row.get("opened")
            )
            records.append(
                ArtifactRecord(
                    f"canonical-{kind}",
                    identity,
                    row,
                    effective_at=_effective_at(effective),
                )
            )
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind=f"canonical-{kind}",
            source_version=f"v{manifest['schemaVersion']}",
            parser_paths=(Path(normalized.__file__),),
            records=records,
            observed_at=_effective_at(manifest.get("buildTimestamp"))
            or generated_at,
        )
        batches.append(batch)
        files.append(source_file)
    for name in normalized.LINEAGE_OUTPUT_NAMES:
        if name not in manifest["dataFiles"]:
            continue
        path = canonical / name
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceLoadError("canonical-lineage-publication-invalid") from exc
        if not isinstance(document, dict):
            raise SourceLoadError("canonical-lineage-publication-invalid")
        kind = name.removesuffix(".json")
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind=f"canonical-{kind}",
            source_version=f"v{manifest['schemaVersion']}",
            parser_paths=(Path(normalized.__file__),),
            records=[
                ArtifactRecord(
                    f"canonical-{kind}",
                    {
                        "kind": document.get("kind"),
                        "schemaVersion": document.get("schemaVersion"),
                    },
                    document,
                    effective_at=_effective_at(manifest.get("buildTimestamp")),
                )
            ],
            observed_at=_effective_at(manifest.get("buildTimestamp"))
            or generated_at,
        )
        batches.append(batch)
        files.append(source_file)
    return batches, files, gaps, blockers, lineage_binding


def _baseline_batches(
    root: Path,
    generated_at: datetime,
) -> tuple[list[ObservationBatch], list[SourceFile], list[str]]:
    blockers: list[str] = []
    try:
        baseline.verify(root, repo_root=REPOSITORY_ROOT)
    except (baseline.BaselineError, OSError):
        blockers.append("wealthfolio-baseline-verification-failed")
    publication, pointer = baseline._current(root)
    manifest_path = publication / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    observed_at = _effective_at(manifest.get("generatedAt")) or generated_at
    blockers.extend(_baseline_capability_blockers(manifest))
    batches = []
    files = []
    manifest_batch, manifest_file = _artifact_batch(
        root,
        manifest_path,
        source_kind="wealthfolio-baseline-manifest",
        source_version=f"v{manifest['schemaVersion']}",
        parser_paths=(Path(baseline.__file__),),
        records=[
            ArtifactRecord(
                "wealthfolio-baseline-manifest",
                pointer["publicationId"],
                manifest,
                effective_at=observed_at,
            )
        ],
        observed_at=observed_at,
    )
    batches.append(manifest_batch)
    files.append(manifest_file)
    for name in sorted(manifest["domainFiles"]):
        path = publication / "domains" / name
        if _sha256(path) != manifest["domainFiles"][name].get("sha256"):
            raise SourceLoadError("baseline-publication-hash-mismatch")
        payload = json.loads(path.read_text(encoding="utf-8"))
        domain_name = name.removesuffix(".json")
        raw_records = payload.get("records")
        records: list[ArtifactRecord] = []
        if isinstance(raw_records, list):
            for index, row in enumerate(raw_records):
                normalized_row = (
                    row if isinstance(row, dict) else {"value": _jsonable(row)}
                )
                records.append(
                    ArtifactRecord(
                        f"wealthfolio-{domain_name}",
                        normalized_row.get("id") or index,
                        {"domain": domain_name, "record": normalized_row},
                        effective_at=_effective_at(
                            normalized_row.get("date")
                            or normalized_row.get("asOf")
                            or normalized_row.get("snapshotDate")
                        ),
                    )
                )
        elif isinstance(raw_records, dict):
            for key, value in sorted(raw_records.items()):
                children = value if isinstance(value, list) else [value]
                for index, child in enumerate(children):
                    normalized_child = (
                        child
                        if isinstance(child, dict)
                        else {"value": _jsonable(child)}
                    )
                    records.append(
                        ArtifactRecord(
                            f"wealthfolio-{domain_name}",
                            {
                                "collection": key,
                                "id": normalized_child.get("id") or index,
                            },
                            {
                                "domain": domain_name,
                                "collection": key,
                                "record": normalized_child,
                            },
                            effective_at=_effective_at(
                                normalized_child.get("date")
                                or normalized_child.get("asOf")
                                or normalized_child.get("snapshotDate")
                            ),
                        )
                    )
        else:
            records.append(
                ArtifactRecord(
                    f"wealthfolio-{domain_name}",
                    name,
                    payload,
                    effective_at=observed_at,
                )
            )
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind="wealthfolio-baseline-domain",
            source_version=f"v{manifest['schemaVersion']}",
            parser_paths=(Path(baseline.__file__),),
            records=records,
            observed_at=observed_at,
        )
        batches.append(batch)
        files.append(source_file)
    return batches, files, blockers


def _baseline_capability_blockers(manifest: Mapping[str, Any]) -> list[str]:
    """Block unknown/unrepresented API gaps, but not proven-empty known gaps."""
    capabilities = manifest.get("capabilityResults")
    counts = manifest.get("recordCounts")
    if not isinstance(capabilities, Mapping):
        return ["baseline-capability-manifest-invalid"]
    if not isinstance(counts, Mapping):
        return (
            []
            if all(
                isinstance(details, Mapping)
                and details.get("status") == "available"
                for details in capabilities.values()
            )
            else ["baseline-capability-manifest-invalid"]
        )
    blockers = []
    for name, details in sorted(capabilities.items()):
        if not isinstance(details, Mapping):
            blockers.append(f"baseline-capability-invalid:{name}")
            continue
        status = details.get("status")
        if status == "available":
            continue
        known_empty = (
            name in NON_BLOCKING_EMPTY_CAPABILITY_GAPS
            and status == "unavailable"
            and details.get("reasonType") == "known-api-gap"
            and counts.get(name) == 0
        )
        if not known_empty:
            blockers.append(f"baseline-capability-{status}:{name}")
    return blockers


def _forensic_batches(
    root: Path,
    generated_at: datetime,
) -> tuple[
    list[ObservationBatch],
    list[SourceFile],
    list[LineageGroup],
    list[LineageDecision],
    list[str],
    list[str],
]:
    blockers: list[str] = []
    try:
        forensic.verify(root, repo_root=REPOSITORY_ROOT)
    except (forensic.ForensicAuditError, baseline.BaselineError, OSError):
        blockers.append("forensic-publication-verification-failed")
    publication, pointer = forensic._current(root)
    manifest = json.loads((publication / "manifest.json").read_text(encoding="utf-8"))
    detail = json.loads(
        (publication / "private-audit.json").read_text(encoding="utf-8")
    )
    review = json.loads(
        (publication / "review-decisions.json").read_text(encoding="utf-8")
    )
    observed_at = _effective_at(manifest.get("generatedAt")) or generated_at
    group_details = {
        str(group["groupId"]): group for group in detail["candidateGroups"]
    }
    issues = []
    groups = []
    for candidate in review["decisions"]:
        group_id = str(candidate["candidateGroupId"])
        group = group_details[group_id]
        activity_refs = group.get("activityRefs")
        if (
            not isinstance(activity_refs, list)
            or len(activity_refs) < 2
            or len(set(activity_refs)) != len(activity_refs)
        ):
            raise SourceLoadError("forensic-lineage-members-invalid")
        issue_id = stable_id("quality_issue", "lineage_review_required", group_id)
        evidence_set_hash = content_hash(sorted(candidate["evidenceHashes"]))
        issues.append(
            QualityIssue(
                id=issue_id,
                issue_type="unresolved_duplicate",
                severity="warning",
                subject_type="lineage_group",
                subject_key=group_id,
                details={
                    "candidateHash": candidate["candidateHash"],
                    "auditGraphHash": review["auditGraphSha256"],
                    "evidenceSetHash": evidence_set_hash,
                    "memberCount": len(activity_refs),
                    "policy": "retain_all_until_evidence_bound_decision",
                },
                effective_at=observed_at,
                observed_at=observed_at,
                processed_at=observed_at,
            )
        )
        groups.append(
            LineageGroup(
                group_id=group_id,
                candidate_hash=candidate["candidateHash"],
                audit_graph_hash=review["auditGraphSha256"],
                evidence_set_hash=evidence_set_hash,
                member_count=len(activity_refs),
                observed_at=observed_at,
                source_blob_id="",
                quality_issue_id=issue_id,
                member_identity_hashes=tuple(
                    sorted(content_hash(ref) for ref in activity_refs)
                ),
            )
        )

    batches: list[ObservationBatch] = []
    files: list[SourceFile] = []
    review_blob_id = ""
    for name in sorted({"manifest.json", *manifest["files"]}):
        path = publication / name
        if name != "manifest.json" and (
            _sha256(path) != manifest["files"][name].get("sha256")
            or path.stat().st_size != manifest["files"][name].get("size")
        ):
            raise SourceLoadError("forensic-publication-hash-mismatch")
        if path.suffix.casefold() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = []
            if name == "private-audit.json":
                collection_names = (
                    "activities",
                    "candidateEdges",
                    "candidateGroups",
                    "duplicateEconomicEffects",
                    "receiptProvenReconciliationEffects",
                    "unresolvedEffects",
                )
                for collection in collection_names:
                    values = payload.get(collection) or []
                    if not isinstance(values, list):
                        raise SourceLoadError(
                            "forensic-publication-collection-invalid"
                        )
                    for index, row in enumerate(values):
                        normalized_row = (
                            row
                            if isinstance(row, dict)
                            else {"value": _jsonable(row)}
                        )
                        records.append(
                            ArtifactRecord(
                                f"forensic-{collection}",
                                normalized_row.get("activityRef")
                                or normalized_row.get("candidateId")
                                or normalized_row.get("groupId")
                                or index,
                                {
                                    "collection": collection,
                                    "record": normalized_row,
                                },
                                effective_at=_effective_at(
                                    normalized_row.get("sourceDateUtc")
                                    or normalized_row.get("sourceDate")
                                ),
                            )
                        )
            elif name == "review-decisions.json":
                records = [
                    ArtifactRecord(
                        "forensic-review-template",
                        row.get("candidateGroupId") or index,
                        row,
                        effective_at=observed_at,
                    )
                    for index, row in enumerate(payload.get("decisions") or [])
                    if isinstance(row, dict)
                ]
            if not records:
                records = [
                    ArtifactRecord(
                        f"forensic-{path.stem}",
                        path.stem,
                        payload,
                        effective_at=observed_at,
                    )
                ]
        else:
            records = [
                ArtifactRecord(
                    f"forensic-{path.stem}",
                    {"sha256": _sha256(path)},
                    {
                        "sha256": _sha256(path),
                        "byteSize": path.stat().st_size,
                        "rowCount": sum(1 for _line in path.open(encoding="utf-8"))
                        - 1,
                    },
                    effective_at=observed_at,
                )
            ]
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind="forensic-publication",
            source_version=f"v{manifest['schemaVersion']}",
            parser_paths=(Path(forensic.__file__),),
            records=records,
            observed_at=observed_at,
            source_issues=tuple(issues) if name == "review-decisions.json" else (),
        )
        if name == "review-decisions.json":
            review_blob_id = batch.blob.id
        batches.append(batch)
        files.append(source_file)
    groups = [replace(group, source_blob_id=review_blob_id) for group in groups]
    decisions = _lineage_decisions(root, review, groups, generated_at)
    by_source: dict[str, list[LineageDecision]] = {}
    for decision in decisions:
        by_source.setdefault(decision.source_path, []).append(decision)
    for relative, source_decisions in sorted(by_source.items()):
        path = (root / relative).resolve(strict=True)
        durable = tuple(
            DurableDecision(
                id=decision.decision_id,
                decision_type="duplicate_resolution",
                subject_type="lineage_group",
                subject_key=decision.group_id,
                action=decision.outcome,
                rationale=(
                    "Evidence-bound lineage review; rationale sha256:"
                    f"{decision.rationale_hash}"
                ),
                decided_by="lineage-review",
                effective_at=decision.decided_at,
                observed_at=decision.observed_at,
                processed_at=decision.observed_at,
            )
            for decision in source_decisions
        )
        batch, source_file = _artifact_batch(
            root,
            path,
            source_kind="lineage-review-decisions",
            source_version="v1",
            parser_paths=(Path(forensic.__file__),),
            records=[
                ArtifactRecord(
                    "lineage-review-decision",
                    {
                        "groupId": decision.group_id,
                        "version": decision.decision_version,
                    },
                    {
                        key: value
                        for key, value in asdict(decision).items()
                        if key != "source_path"
                    },
                    effective_at=decision.decided_at,
                )
                for decision in source_decisions
            ],
            observed_at=generated_at,
        )
        batches.append(replace(batch, source_decisions=durable))
        files.append(source_file)
    gaps = [
        "lineage-review-decision-missing"
        for group in groups
        if group.group_id not in {decision.group_id for decision in decisions}
    ]
    return batches, files, groups, decisions, gaps, blockers


def _lineage_decisions(
    root: Path,
    review: dict[str, Any],
    groups: list[LineageGroup],
    generated_at: datetime,
) -> list[LineageDecision]:
    group_map = {group.group_id: group for group in groups}
    candidates = [
        *(
            sorted((root / "decisions").rglob("*.json"))
            if (root / "decisions").is_dir()
            else ()
        ),
        *(
            sorted(
                (root / "audit" / "duplicates" / "decisions").rglob("*.json")
            )
            if (root / "audit" / "duplicates" / "decisions").is_dir()
            else ()
        ),
    ]
    decisions: list[LineageDecision] = []
    for path in candidates:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceLoadError("lineage-decision-file-invalid") from exc
        rows = document.get("decisions") if isinstance(document, dict) else None
        if not isinstance(rows, list):
            continue
        audit_graph = str(
            document.get("auditGraphSha256")
            or document.get("auditGraphHash")
            or ""
        )
        if audit_graph != review["auditGraphSha256"]:
            raise SourceLoadError("lineage-decision-audit-binding-mismatch")
        for row in rows:
            if not isinstance(row, dict):
                raise SourceLoadError("lineage-decision-invalid")
            outcome = str(row.get("outcome") or row.get("status") or "")
            if outcome not in LINEAGE_OUTCOMES:
                continue
            group_id = str(
                row.get("candidateGroupId") or row.get("lineageGroupId") or ""
            )
            group = group_map.get(group_id)
            evidence_hashes = row.get("evidenceHashes")
            if (
                group is None
                or row.get("candidateHash") != group.candidate_hash
                or not isinstance(evidence_hashes, list)
                or content_hash(sorted(evidence_hashes)) != group.evidence_set_hash
            ):
                raise SourceLoadError("lineage-decision-evidence-binding-mismatch")
            version = row.get("decisionVersion", 1)
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise SourceLoadError("lineage-decision-version-invalid")
            survivor = row.get("survivorIdentityHash")
            if survivor is None and row.get("survivorActivityRef") is not None:
                survivor = content_hash(str(row["survivorActivityRef"]))
            if outcome == "duplicate-economic-event" and (
                not isinstance(survivor, str)
                or not re.fullmatch(r"[0-9a-f]{64}", survivor)
                or survivor not in group.member_identity_hashes
            ):
                raise SourceLoadError("lineage-decision-survivor-required")
            rationale = str(row.get("rationale") or "").strip()
            if not rationale:
                raise SourceLoadError("lineage-decision-rationale-required")
            decided_at = _effective_at(row.get("decidedAt") or row.get("decidedOn"))
            if decided_at is None:
                raise SourceLoadError("lineage-decision-time-invalid")
            rationale_hash = content_hash(rationale)
            decisions.append(
                LineageDecision(
                    decision_id=stable_id(
                        "lineage_decision",
                        group_id,
                        version,
                        group.candidate_hash,
                        group.audit_graph_hash,
                        group.evidence_set_hash,
                        outcome,
                        survivor or "",
                        rationale_hash,
                        decided_at.isoformat(),
                    ),
                    group_id=group_id,
                    decision_version=version,
                    candidate_hash=group.candidate_hash,
                    audit_graph_hash=group.audit_graph_hash,
                    evidence_set_hash=group.evidence_set_hash,
                    outcome=outcome,
                    survivor_identity_hash=survivor,
                    rationale_hash=rationale_hash,
                    decided_at=decided_at,
                    observed_at=generated_at,
                    source_path=_relative(root, path),
                )
            )
    unique: dict[tuple[str, int], LineageDecision] = {}
    for decision in decisions:
        key = (decision.group_id, decision.decision_version)
        previous = unique.get(key)
        semantics = (
            decision.candidate_hash,
            decision.audit_graph_hash,
            decision.evidence_set_hash,
            decision.outcome,
            decision.survivor_identity_hash,
            decision.rationale_hash,
            decision.decided_at,
        )
        if previous is not None:
            previous_semantics = (
                previous.candidate_hash,
                previous.audit_graph_hash,
                previous.evidence_set_hash,
                previous.outcome,
                previous.survivor_identity_hash,
                previous.rationale_hash,
                previous.decided_at,
            )
            if previous_semantics != semantics:
                raise SourceLoadError("lineage-decision-version-conflict")
            continue
        unique[key] = decision
    return sorted(
        unique.values(), key=lambda item: (item.group_id, item.decision_version)
    )


def _lineage_readiness(
    root: Path,
    canonical_lineage_binding: dict[str, Any] | None,
) -> tuple[
    dict[str, int],
    dict[str, int],
    dict[str, int],
    list[str],
    list[str],
]:
    output = root / lineage_workflow.OUTPUT_RELATIVE
    if not (output / "current.json").is_file():
        return {}, {}, {}, ["lineage-review-publication-missing"], []
    try:
        summary = lineage_workflow.verify(root, repo_root=REPOSITORY_ROOT)
    except (ReviewError, DecisionError, OSError):
        return {}, {}, {}, ["lineage-review-verification-failed"], []
    counts = dict(summary["counts"])
    readiness = dict(summary["readinessCounts"])
    evidence_gaps = dict(summary["evidenceGapCounts"])
    blockers = []
    if (
        not isinstance(canonical_lineage_binding, dict)
        or canonical_lineage_binding.get("queuePublicationId")
        != summary["queuePublicationId"]
        or canonical_lineage_binding.get("decisionPublicationId")
        != summary["decisionPublicationId"]
    ):
        blockers.append("canonical-lineage-binding-mismatch")
    queue_incomplete = (
        counts["unresolved-groups"] != 0
        or readiness["ready-groups"] != counts["queue-groups"]
    )
    canonical_identity_complete = _canonical_identity_supersedes_review_queue(
        canonical_lineage_binding
    )
    if queue_incomplete and not canonical_identity_complete:
        blockers.append("lineage-remediation-readiness-incomplete")
    if counts["reviewed-decisions"] != 0:
        blockers.append("lineage-v5-persistence-not-implemented")
    gaps = [
        f"lineage-readiness-{code}-count:{count}"
        for code, count in sorted(evidence_gaps.items())
        if count
    ]
    if queue_incomplete and canonical_identity_complete:
        gaps.append("lineage-review-superseded-by-canonical-identity-v5")
    return counts, readiness, evidence_gaps, blockers, gaps


def _canonical_identity_supersedes_review_queue(
    canonical_lineage_binding: Mapping[str, Any] | None,
) -> bool:
    """Whether verified canonical v2 has fully classified the review queue."""
    if not isinstance(canonical_lineage_binding, Mapping):
        return False
    identity = canonical_lineage_binding.get("identityPolicy")
    if not isinstance(identity, Mapping):
        return False
    residual = identity.get("residualByClass")
    return (
        str(identity.get("policyVersion") or "").startswith("canonical-identity-v")
        and identity.get("unresolvedDuplicateGroups") == 0
        and identity.get("authorityAmbiguousGroups") == 0
        and isinstance(residual, Mapping)
        and residual.get("unresolved") == 0
    )


def load_source_catalog(
    data_dir: str | Path,
    *,
    generated_at: datetime,
) -> SourceCatalog:
    """Verify all selected private evidence before returning any ingest batches."""
    root = Path(data_dir).resolve(strict=True)
    validate_private_output(root / SHADOW_OUTPUT, root, REPOSITORY_ROOT)
    generated_at = utc(generated_at)
    facts, parsed_facts = _fact_accounts(root)
    batches: list[ObservationBatch] = []
    files: list[SourceFile] = []
    blockers: list[str] = []
    gaps: list[str] = []

    fact_batches, fact_files = _fact_batches(root, generated_at, parsed_facts)
    batches.extend(fact_batches)
    files.extend(fact_files)

    (
        simplefin_batches,
        simplefin_files,
        simplefin_gaps,
        simplefin_blockers,
    ) = _simplefin_batches(root, generated_at, facts)
    batches.extend(simplefin_batches)
    files.extend(simplefin_files)
    gaps.extend(simplefin_gaps)
    blockers.extend(simplefin_blockers)

    extract_batches, extract_files = _extract_batches(root, generated_at, facts)
    batches.extend(extract_batches)
    files.extend(extract_files)

    (
        monarch_batches,
        monarch_files,
        monarch_gaps,
        monarch_blockers,
    ) = _monarch_batches(
        root,
        generated_at,
        facts,
        _canonical_entity_index(parsed_facts),
        _canonical_entity_evidence(parsed_facts),
    )
    batches.extend(monarch_batches)
    files.extend(monarch_files)
    gaps.extend(monarch_gaps)
    blockers.extend(monarch_blockers)

    (
        canonical_batches,
        canonical_files,
        canonical_gaps,
        canonical_blockers,
        canonical_lineage_binding,
    ) = _canonical_batches(root, generated_at)
    batches.extend(canonical_batches)
    files.extend(canonical_files)
    gaps.extend(canonical_gaps)
    blockers.extend(canonical_blockers)

    baseline_batches, baseline_files, baseline_blockers = _baseline_batches(
        root, generated_at
    )
    batches.extend(baseline_batches)
    files.extend(baseline_files)
    blockers.extend(baseline_blockers)

    (
        forensic_batches,
        forensic_files,
        lineage_groups,
        lineage_decisions,
        lineage_gaps,
        forensic_blockers,
    ) = _forensic_batches(root, generated_at)
    batches.extend(forensic_batches)
    files.extend(forensic_files)
    gaps.extend(lineage_gaps)
    blockers.extend(forensic_blockers)

    (
        lineage_review_counts,
        lineage_readiness_counts,
        lineage_evidence_gap_counts,
        lineage_readiness_blockers,
        lineage_readiness_gaps,
    ) = _lineage_readiness(root, canonical_lineage_binding)
    blockers.extend(lineage_readiness_blockers)
    gaps.extend(lineage_readiness_gaps)

    duplicate_paths = [
        path
        for path, count in Counter(item.relative_path for item in files).items()
        if count > 1
    ]
    if duplicate_paths:
        raise SourceLoadError("source-catalog-contains-duplicate-files")
    summarized_gaps = []
    for code, count in sorted(Counter(gaps).items()):
        summarized_gaps.append(
            f"{code}-count:{count}" if count > 1 else code
        )
    return SourceCatalog(
        batches=tuple(
            sorted(
                batches,
                key=lambda item: (
                    item.run.observed_at,
                    item.blob.source_kind,
                    item.blob.content_hash,
                ),
            )
        ),
        files=tuple(sorted(files, key=lambda item: item.relative_path)),
        lineage_groups=tuple(lineage_groups),
        lineage_decisions=tuple(lineage_decisions),
        lineage_review_counts=lineage_review_counts,
        lineage_readiness_counts=lineage_readiness_counts,
        lineage_evidence_gap_counts=lineage_evidence_gap_counts,
        blockers=tuple(sorted(set(blockers))),
        gaps=tuple(summarized_gaps),
    )
