"""Minimal SimpleFIN v1/v2 adapter that never persists raw payloads in PostgreSQL."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import (
    BalanceObservation,
    IngestionRun,
    ObservationBatch,
    SourceAccount,
    SourceBlob,
    SourceConnection,
    TransactionObservation,
    content_hash,
    normalize_money,
    stable_id,
    utc,
)
from .source_admission import organization_scope


class SimpleFinAdapterError(ValueError):
    pass


def detect_protocol_version(payload: dict[str, Any]) -> str:
    declared = payload.get("version") or payload.get("schema_version")
    if declared is not None:
        return str(declared)
    if "errlist" in payload or "connections" in payload or any(
        isinstance(account, dict) and account.get("conn_id")
        for account in (payload.get("accounts") or [])
    ):
        return "2"
    return "1"


def scoped_account_identity(account: dict[str, Any], version: str) -> str:
    provider_id = str(account.get("id") or account.get("account_id") or "")
    if not provider_id:
        return ""
    scope = organization_scope(account, version)
    if not scope:
        return provider_id
    return f"{content_hash(scope)[:16]}:{provider_id}"


def _timestamp(value: Any, field: str) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and not value.isdigit():
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError) as exc:
        raise SimpleFinAdapterError(f"{field} must be an ISO timestamp or epoch seconds") from exc
    return utc(parsed)


def _money(value: Any, field: str) -> Decimal:
    try:
        return normalize_money(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SimpleFinAdapterError(f"{field} must be decimal money") from exc


class SimpleFinAdapter:
    """Parse compatible synthetic v1 or v2 snapshots into neutral observations."""

    name = "simplefin"
    version = "2.0"
    parser_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def __init__(self, connection_key: str, *, source_system: str = "simplefin") -> None:
        if not connection_key:
            raise ValueError("connection_key is required")
        self.connection_key = connection_key
        self.source_system = source_system

    def parse(
        self,
        raw: bytes,
        *,
        raw_locator: str,
        observed_at: datetime,
        processed_at: datetime | None = None,
        trust_cutoffs: dict[str, datetime] | None = None,
        protocol_version: str | None = None,
        overlap_start: datetime | None = None,
        overlap_end: datetime | None = None,
        sealed_plan_hash: str | None = None,
    ) -> ObservationBatch:
        observed_at = utc(observed_at)
        processed_at = utc(processed_at or observed_at)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SimpleFinAdapterError("SimpleFIN payload must be UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise SimpleFinAdapterError("SimpleFIN payload must be an object")

        detected_version = detect_protocol_version(payload)
        version = protocol_version or detected_version
        if version not in {"1", "2"}:
            raise SimpleFinAdapterError(f"unsupported SimpleFIN version: {version}")
        if protocol_version is not None and protocol_version != detected_version:
            raise SimpleFinAdapterError(
                "declared SimpleFIN protocol version does not match the snapshot"
            )
        envelope = (
            payload.get("data")
            if version == "2" and isinstance(payload.get("data"), dict)
            else payload
        )
        if not isinstance(envelope, dict):
            raise SimpleFinAdapterError("SimpleFIN data envelope must be an object")

        blob_hash = __import__("hashlib").sha256(raw).hexdigest()
        blob_id = stable_id("source_blob", raw_locator, blob_hash)
        blob = SourceBlob(
            id=blob_id,
            raw_locator=raw_locator,
            content_hash=blob_hash,
            observed_at=observed_at,
            processed_at=processed_at,
            byte_size=len(raw),
            source_kind="simplefin-snapshot",
            source_version=f"v{version}",
        )
        connection_id = stable_id("source_connection", self.source_system, self.connection_key)
        connection = SourceConnection(
            id=connection_id,
            source_system=self.source_system,
            connection_key=self.connection_key,
            effective_from=observed_at,
            observed_at=observed_at,
            processed_at=processed_at,
        )
        account_rows = envelope.get("accounts") or []
        if not isinstance(account_rows, list):
            raise SimpleFinAdapterError("accounts must be an array")

        account_records: list[SourceAccount] = []
        transactions: list[TransactionObservation] = []
        transaction_order = 0
        balances: list[BalanceObservation] = []
        effective_values: list[datetime] = []
        trust_cutoffs = trust_cutoffs or {}

        for account_row in account_rows:
            if not isinstance(account_row, dict):
                raise SimpleFinAdapterError("account records must be objects")
            provider_account_id = str(
                account_row.get("id") or account_row.get("account_id") or ""
            )
            external_id = scoped_account_identity(account_row, version)
            if not external_id:
                raise SimpleFinAdapterError("account ID is required")
            if (
                version == "2"
                and ("errlist" in payload or "connections" in payload)
                and not account_row.get("conn_id")
            ):
                raise SimpleFinAdapterError(
                    "SimpleFIN v2 account is missing conn_id"
                )
            account_id = stable_id("source_account", connection_id, external_id)
            currency = str(account_row.get("currency") or "USD").upper()
            status_provided = (
                "status" in account_row or "account_status" in account_row
            )
            account_status = str(
                account_row.get("status")
                or account_row.get("account_status")
                or "unknown"
            ).lower()
            if account_status not in {"active", "closed", "unknown"}:
                raise SimpleFinAdapterError(
                    f"unsupported source account status: {account_status}"
                )
            account_records.append(
                SourceAccount(
                    id=account_id,
                    connection_id=connection_id,
                    external_id=external_id,
                    name=str(account_row.get("name") or account_row.get("display_name") or ""),
                    account_type=account_row.get("account_type") or account_row.get("type"),
                    currency=currency,
                    effective_from=observed_at,
                    observed_at=observed_at,
                    processed_at=processed_at,
                    canonical_key=str(
                        account_row.get("canonical_key")
                        or f"{self.source_system}:{self.connection_key}:{external_id}"
                    ),
                    trust_cutoff_at=(
                        trust_cutoffs.get(external_id)
                        if external_id in trust_cutoffs
                        else trust_cutoffs.get(provider_account_id)
                    ),
                    mapping_effective_from=observed_at,
                    trust_cutoff_provided=(
                        external_id in trust_cutoffs
                        or provider_account_id in trust_cutoffs
                    ),
                    status=account_status,
                    status_provided=status_provided,
                )
            )
            transaction_rows = account_row.get("transactions") or []
            for transaction_row in transaction_rows:
                if not isinstance(transaction_row, dict):
                    raise SimpleFinAdapterError("transaction records must be objects")
                source_id = str(
                    transaction_row.get("id")
                    or transaction_row.get("transaction_id")
                    or ""
                )
                if not source_id:
                    raise SimpleFinAdapterError("transaction ID is required")
                posted_value = (
                    transaction_row.get("posted")
                    if "posted" in transaction_row
                    else transaction_row.get("posted_at")
                )
                pending = bool(transaction_row.get("pending")) or posted_value in {
                    0,
                    "0",
                }
                effective_value = (
                    transaction_row.get("transacted_at") or observed_at
                    if pending and posted_value in {0, "0", None}
                    else posted_value or transaction_row.get("transacted_at")
                )
                effective_at = _timestamp(
                    effective_value, "transaction effective time"
                )
                semantics = {
                    "sourceAccountId": external_id,
                    "sourceTransactionId": source_id,
                    "effectiveAt": effective_at.isoformat(),
                    "amount": str(_money(transaction_row.get("amount"), "transaction amount")),
                    "currency": str(transaction_row.get("currency") or currency).upper(),
                    "description": str(
                        transaction_row.get("description")
                        or transaction_row.get("payee")
                        or ""
                    ),
                    "status": (
                        str(transaction_row.get("status")).lower()
                        if transaction_row.get("status")
                        else ("pending" if pending else "posted")
                    ),
                }
                observation_hash = content_hash(semantics)
                transactions.append(
                    TransactionObservation(
                        id=stable_id("transaction_observation", account_id, source_id, observation_hash),
                        source_account_id=account_id,
                        source_transaction_id=source_id,
                        observation_hash=observation_hash,
                        effective_at=effective_at,
                        observed_at=observed_at,
                        processed_at=processed_at,
                        amount=Decimal(semantics["amount"]),
                        currency=semantics["currency"],
                        description=semantics["description"],
                        status=semantics["status"],
                        source_blob_id=blob_id,
                        ingestion_run_id="",
                        last_seen_at=observed_at,
                        sighting_order=transaction_order,
                        last_seen_run_id="",
                        last_seen_order=transaction_order,
                    )
                )
                transaction_order += 1
                effective_values.append(effective_at)

            balance_value = (
                account_row.get("balance")
                if "balance" in account_row
                else account_row.get("current_balance")
            )
            balance_time = (
                account_row.get("balance-date")
                or account_row.get("balance_date")
                or account_row.get("balance_at")
            )
            if balance_value is not None and balance_time is not None:
                effective_at = _timestamp(balance_time, "balance effective time")
                amount = _money(balance_value, "account balance")
                semantics = {
                    "sourceAccountId": external_id,
                    "balanceType": "current",
                    "effectiveAt": effective_at.isoformat(),
                    "amount": str(amount),
                    "currency": currency,
                }
                observation_hash = content_hash(semantics)
                balances.append(
                    BalanceObservation(
                        id=stable_id("balance_observation", account_id, observation_hash),
                        source_account_id=account_id,
                        observation_hash=observation_hash,
                        balance_type="current",
                        effective_at=effective_at,
                        observed_at=observed_at,
                        processed_at=processed_at,
                        amount=amount,
                        currency=currency,
                        source_blob_id=blob_id,
                        ingestion_run_id="",
                    )
                )
                effective_values.append(effective_at)

        run_id = stable_id(
            "ingestion_run",
            blob_id,
            self.name,
            self.version,
            self.parser_hash,
            version,
            observed_at.isoformat(),
        )
        transactions = [
            replace(
                transaction,
                ingestion_run_id=run_id,
                last_seen_run_id=run_id,
            )
            for transaction in transactions
        ]
        balances = [
            replace(balance, ingestion_run_id=run_id)
            for balance in balances
        ]
        records = len(transactions) + len(balances)
        run = IngestionRun(
            id=run_id,
            source_blob_id=blob_id,
            importer_name=self.name,
            importer_version=f"{self.version}/v{version}",
            status="succeeded",
            effective_start=min(effective_values) if effective_values else None,
            effective_end=max(effective_values) if effective_values else None,
            observed_at=observed_at,
            processed_at=processed_at,
            records_seen=records,
            records_accepted=records,
            parser_hash=self.parser_hash,
            source_protocol="simplefin",
            source_version=version,
            overlap_start=utc(overlap_start) if overlap_start else None,
            overlap_end=utc(overlap_end) if overlap_end else None,
            sealed_plan_hash=sealed_plan_hash,
        )
        raw_errors = (
            envelope.get("errlist")
            or envelope.get("errors")
            or payload.get("errlist")
            or payload.get("errors")
            or []
        )
        errors = tuple(
            (
                f"{item.get('code') or 'unknown'}:"
                f"{content_hash(str(item.get('conn_id') or 'global'))[:16]}"
                if isinstance(item, dict)
                else str(item)
            )
            for item in raw_errors
        )
        return ObservationBatch(
            blob=blob,
            run=run,
            connection=connection,
            accounts=tuple(account_records),
            transactions=tuple(transactions),
            balances=tuple(balances),
            errors=errors,
        )
