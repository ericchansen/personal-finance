"""Read Ledger Live's local cache without modifying it.

The normalized records deliberately omit extended public keys, addresses,
seed identifiers, and Ledger account IDs (which can contain an xpub).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SATOSHIS_PER_BTC = 100_000_000
DEFAULT_APP_JSON = (
    Path.home() / "AppData" / "Roaming" / "Ledger Live" / "app.json"
)


class LedgerLiveError(RuntimeError):
    """Ledger Live cache data is unavailable or malformed."""


@dataclass(frozen=True)
class Operation:
    txid: str
    occurred_at: str
    direction: str
    value_sat: int
    fee_sat: int
    block_height: int | None
    failed: bool

    @property
    def signed_value_sat(self) -> int:
        return self.value_sat if self.direction == "inflow" else -self.value_sat


@dataclass(frozen=True)
class Account:
    account_ref: str
    name: str
    currency: str
    derivation_mode: str
    index: int
    created_at: str
    current_balance_sat: int
    spendable_balance_sat: int
    block_height: int | None
    operations: tuple[Operation, ...]

    @property
    def inflow_sat(self) -> int:
        return sum(o.value_sat for o in self.operations if not o.failed and o.direction == "inflow")

    @property
    def outflow_sat(self) -> int:
        return sum(o.value_sat for o in self.operations if not o.failed and o.direction == "outflow")

    @property
    def reconciled_balance_sat(self) -> int:
        return self.inflow_sat - self.outflow_sat


def satoshis_to_btc(satoshis: int) -> Decimal:
    """Convert the base unit exactly: 1 BTC = 100,000,000 satoshis."""
    return Decimal(satoshis) / Decimal(SATOSHIS_PER_BTC)


def load_ledger_live(path: Path = DEFAULT_APP_JSON) -> list[Account]:
    """Load account balances and cached operations from Ledger Live app.json."""
    try:
        with path.open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerLiveError(f"could not read Ledger Live app data: {type(exc).__name__}") from None

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise LedgerLiveError("Ledger Live app data must contain a data object")
    raw_accounts = payload["data"].get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise LedgerLiveError("Ledger Live data.accounts must be a list")

    return [_parse_account(raw, index) for index, raw in enumerate(raw_accounts)]


def _parse_account(wrapper: Any, position: int) -> Account:
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("data"), dict):
        raise LedgerLiveError(f"account {position} must contain a data object")
    raw = wrapper["data"]
    operations = raw.get("operations", [])
    if not isinstance(operations, list):
        raise LedgerLiveError(f"account {position} operations must be a list")

    deduped: dict[str, Operation] = {}
    for operation_position, operation in enumerate(operations):
        parsed = _parse_operation(operation, position, operation_position)
        previous = deduped.get(parsed.txid)
        if previous is not None and previous != parsed:
            raise LedgerLiveError(
                f"account {position} has conflicting cached operations for one txid"
            )
        deduped[parsed.txid] = parsed

    raw_id = _required_string(raw, "id", f"account {position}")
    created_at = _timestamp(raw.get("creationDate"), f"account {position} creationDate")
    return Account(
        account_ref="ledger-live:" + hashlib.sha256(raw_id.encode()).hexdigest()[:16],
        name=_required_string(raw, "name", f"account {position}"),
        currency=_required_string(raw, "currencyId", f"account {position}"),
        derivation_mode=str(raw.get("derivationMode") or ""),
        index=_integer(raw.get("index", 0), f"account {position} index"),
        created_at=created_at,
        current_balance_sat=_satoshis(raw.get("balance"), f"account {position} balance"),
        spendable_balance_sat=_satoshis(
            raw.get("spendableBalance", raw.get("balance")),
            f"account {position} spendableBalance",
        ),
        block_height=_optional_integer(raw.get("blockHeight"), f"account {position} blockHeight"),
        operations=tuple(sorted(deduped.values(), key=lambda item: (item.occurred_at, item.txid))),
    )


def _parse_operation(raw: Any, account_position: int, position: int) -> Operation:
    context = f"account {account_position} operation {position}"
    if not isinstance(raw, dict):
        raise LedgerLiveError(f"{context} must be an object")
    raw_direction = raw.get("type")
    directions = {"IN": "inflow", "OUT": "outflow"}
    if raw_direction not in directions:
        raise LedgerLiveError(f"{context} has an unsupported direction")
    return Operation(
        txid=_required_string(raw, "hash", context),
        occurred_at=_timestamp(raw.get("date"), f"{context} date"),
        direction=directions[raw_direction],
        value_sat=_satoshis(raw.get("value"), f"{context} value"),
        fee_sat=_satoshis(raw.get("fee", 0), f"{context} fee"),
        block_height=_optional_integer(raw.get("blockHeight"), f"{context} blockHeight"),
        failed=bool(raw.get("hasFailed", False)),
    )


def _required_string(raw: dict[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise LedgerLiveError(f"{context} {key} must be a non-empty string")
    return value


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool):
        raise LedgerLiveError(f"{context} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LedgerLiveError(f"{context} must be an integer") from None


def _optional_integer(value: Any, context: str) -> int | None:
    return None if value in (None, "") else _integer(value, context)


def _satoshis(value: Any, context: str) -> int:
    amount = _integer(value, context)
    if amount < 0:
        raise LedgerLiveError(f"{context} must not be negative")
    return amount


def _timestamp(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise LedgerLiveError(f"{context} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LedgerLiveError(f"{context} must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise LedgerLiveError(f"{context} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def assert_current_balances(accounts: list[Account]) -> None:
    """Assert cached inflows minus outflows equal each current app.json balance."""
    failures = [
        account.account_ref
        for account in accounts
        if account.reconciled_balance_sat != account.current_balance_sat
    ]
    if failures:
        raise LedgerLiveError(
            f"{len(failures)} account(s) do not reconcile cached operations to current balance"
        )


def account_to_dict(account: Account) -> dict[str, Any]:
    """Return a portable JSON record containing no xpub or address material."""
    record = asdict(account)
    record["operations"] = [asdict(operation) for operation in account.operations]
    record["inflow_sat"] = account.inflow_sat
    record["outflow_sat"] = account.outflow_sat
    record["reconciled_balance_sat"] = account.reconciled_balance_sat
    return record


def export_payload(accounts: list[Account]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "ledger-live-local-cache",
        "unit": "satoshi",
        "satoshis_per_btc": SATOSHIS_PER_BTC,
        "accounts": [account_to_dict(account) for account in accounts],
    }
