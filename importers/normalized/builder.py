"""Build app-independent canonical CSV files from private financial sources."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from importers.extracts import parsers
from importers.extracts import vanguard, vanguard_activity, vanguard_history
from importers.facts.loader import load_facts
from importers.facts.schema import (
    AccountFact,
    AssertionFact,
    CryptoFact,
    DecisionFact,
    LoanFact,
    PropertyFact,
    VehicleFact,
)
from importers.monarch.monarch import read_transactions
from importers.simplefin.pipeline import read_snapshot

SCHEMA_VERSION = 2
OUTPUT_NAMES = ("accounts.csv", "transactions.csv", "positions.csv", "valuations.csv")
ACCOUNT_COLUMNS = (
    "account_id", "institution", "name", "kind", "currency", "opened", "closed",
    "excluded", "exclusion_reason",
)
TRANSACTION_COLUMNS = (
    "date", "account_id", "amount", "description", "source_id", "source_file",
    "category", "transfer_group", "symbol", "quantity", "price", "external_flow",
    "excluded", "exclusion_reason",
)
POSITION_COLUMNS = (
    "as_of", "account_id", "symbol", "quantity", "price", "market_value",
    "basis_per_unit", "source_file",
)
VALUATION_COLUMNS = (
    "date", "entity_id", "value", "currency", "source_file", "observed_or_derived",
)


class BuildError(RuntimeError):
    """The inputs cannot safely produce a complete canonical estate."""


@dataclass(frozen=True)
class AccountRow:
    account_id: str
    institution: str
    name: str
    kind: str
    currency: str = "USD"
    opened: str = ""
    closed: str = ""
    excluded: bool = False
    exclusion_reason: str = ""


@dataclass(frozen=True)
class TransactionRow:
    date: str
    account_id: str
    amount: str
    description: str
    source_id: str
    source_file: str
    category: str = ""
    transfer_group: str = ""
    symbol: str = ""
    quantity: str = ""
    price: str = ""
    external_flow: bool = False
    excluded: bool = False
    exclusion_reason: str = ""


@dataclass(frozen=True)
class PositionRow:
    as_of: str
    account_id: str
    symbol: str
    quantity: str
    price: str
    market_value: str
    basis_per_unit: str
    source_file: str


@dataclass(frozen=True)
class ValuationRow:
    date: str
    entity_id: str
    value: str
    currency: str
    source_file: str
    observed_or_derived: str


@dataclass
class Estate:
    accounts: list[AccountRow]
    transactions: list[TransactionRow]
    positions: list[PositionRow]
    valuations: list[ValuationRow]
    source_files: set[Path]
    warnings: list[str]
    source_stats: dict[str, int]


def _money(value: Decimal | int | str | None) -> str:
    if value is None or value == "":
        return ""
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise BuildError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise BuildError(f"non-finite decimal value: {value!r}")
    return format(result, "f")


def _norm(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


class IdentityMap:
    """The one boundary between source identities and canonical account IDs."""

    def __init__(self, accounts: Iterable[AccountRow]):
        self.accounts = {account.account_id: account for account in accounts}
        self.by_name: dict[str, list[str]] = {}
        for account in accounts:
            for value in (account.name, account.institution):
                self.by_name.setdefault(_norm(value), []).append(account.account_id)

    def id(self, account_id: str, context: str) -> str:
        if account_id not in self.accounts:
            raise BuildError(f"{context}: unknown canonical account id {account_id!r}")
        return account_id

    def name(self, value: str, context: str) -> str:
        matches = sorted(set(self.by_name.get(_norm(value), [])))
        if len(matches) != 1:
            state = "no" if not matches else "multiple"
            raise BuildError(f"{context}: {state} account facts match mapped name {value!r}")
        return matches[0]

    def excluded_source(self, name: str, institution: str, context: str) -> str:
        tokens = set(_norm(f"{name} {institution}").split())
        matches = [
            account.account_id
            for account in self.accounts.values()
            if account.excluded
            and len(tokens & set(_norm(f"{account.name} {account.institution}").split())) >= 2
        ]
        if len(matches) != 1:
            raise BuildError(f"{context}: excluded source cannot be mapped to one account fact")
        return matches[0]

    def ledger(self, name: str, context: str) -> str:
        exact = sorted(set(self.by_name.get(_norm(name), [])))
        if len(exact) == 1:
            return exact[0]
        matches = [
            row.account_id for row in self.accounts.values()
            if "ledger" in _norm(f"{row.institution} {row.name}")
            and row.kind.casefold() in {"cryptocurrency", "crypto"}
        ]
        if len(matches) != 1:
            raise BuildError(f"{context}: Ledger account cannot be mapped to one account fact")
        return matches[0]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read {path}: {exc}") from None


def _fact_source(parsed: Any, root: Path) -> str:
    return _source_path(parsed.path, root)


def _load_fact_rows(root: Path) -> tuple[Estate, IdentityMap, list[Any]]:
    facts_dir = root / "facts"
    result = load_facts(facts_dir)
    if result.errors:
        raise BuildError("invalid facts:\n" + "\n".join(str(issue) for issue in result.errors))
    warnings = [str(issue) for issue in result.warnings]
    accounts: list[AccountRow] = []
    positions: list[PositionRow] = []
    valuations: list[ValuationRow] = []
    sources = {parsed.path for parsed in result.facts}
    for parsed in result.facts:
        fact = parsed.fact
        source = _fact_source(parsed, root)
        if isinstance(fact, AccountFact):
            accounts.append(AccountRow(
                fact.id, fact.institution, fact.display_name, fact.kind, "USD",
                fact.opened.isoformat() if fact.opened else "",
                fact.closed.isoformat() if fact.closed else "",
                fact.excluded, fact.reason or "",
            ))
        elif isinstance(fact, AssertionFact) and fact.on and fact.balance is not None:
            valuations.append(ValuationRow(
                fact.on.isoformat(), fact.account_id, _money(fact.balance), "USD",
                source, "observed",
            ))
        elif isinstance(fact, PropertyFact):
            entity = f"property:{fact.name}"
            if fact.purchase_date and fact.purchase_price is not None:
                valuations.append(ValuationRow(
                    fact.purchase_date.isoformat(), entity, _money(fact.purchase_price),
                    "USD", source, "observed",
                ))
            for appraisal in fact.appraisals:
                valuations.append(ValuationRow(
                    appraisal.on.isoformat(), entity, _money(appraisal.value), "USD",
                    source, "observed",
                ))
            if fact.sale_date and fact.sale_price is not None:
                valuations.append(ValuationRow(
                    fact.sale_date.isoformat(), entity, _money(fact.sale_price), "USD",
                    source, "observed",
                ))
        elif isinstance(fact, VehicleFact):
            entity = f"vehicle:{fact.name}"
            if fact.purchase_date and fact.purchase_price is not None:
                valuations.append(ValuationRow(
                    fact.purchase_date.isoformat(), entity, _money(fact.purchase_price),
                    "USD", source, "observed",
                ))
            if fact.current_value_date and fact.current_value is not None:
                valuations.append(ValuationRow(
                    fact.current_value_date.isoformat(), entity, _money(fact.current_value),
                    "USD", source, "observed",
                ))
        elif isinstance(fact, LoanFact):
            entity = f"loan:{fact.name}"
            accounts.append(
                AccountRow(
                    entity,
                    fact.lender,
                    fact.name,
                    "liability",
                    "USD",
                    fact.origination_date.isoformat() if fact.origination_date else "",
                    fact.payoff_date.isoformat() if fact.payoff_date else "",
                )
            )
            if fact.origination_date and fact.principal is not None:
                valuations.append(ValuationRow(
                    fact.origination_date.isoformat(), entity, _money(-fact.principal),
                    "USD", source, "observed",
                ))
            if fact.payoff_date and fact.payoff_amount is not None:
                valuations.append(ValuationRow(
                    fact.payoff_date.isoformat(), entity, _money(-fact.payoff_amount),
                    "USD", source, "observed",
                ))
        elif (
            isinstance(fact, CryptoFact)
            and fact.quantity is not None
            and fact.account_id
            and fact.as_of
        ):
            positions.append(PositionRow(
                fact.as_of.isoformat(),
                fact.account_id,
                fact.unit,
                _money(fact.quantity),
                "",
                "",
                "",
                source,
            ))
    if len({row.account_id for row in accounts}) != len(accounts):
        raise BuildError("facts contain duplicate canonical account ids")
    estate = Estate(accounts, [], positions, valuations, sources, warnings, {})
    return estate, IdentityMap(accounts), list(result.facts)


def _excluded(identity: IdentityMap, account_id: str) -> tuple[bool, str]:
    account = identity.accounts[account_id]
    return account.excluded, account.exclusion_reason if account.excluded else ""


def _transaction(
    identity: IdentityMap, when: date | str, account_id: str, amount: Decimal,
    description: str, source_id: str, source_file: str, category: str = "",
    *, excluded: bool = False, reason: str = "", transfer_group: str = "",
) -> TransactionRow:
    inherited, inherited_reason = _excluded(identity, account_id)
    return TransactionRow(
        when.isoformat() if isinstance(when, date) else when,
        account_id, _money(amount), description, source_id, source_file, category,
        transfer_group, "", "", "", False,
        excluded or inherited, reason or inherited_reason,
    )


def _load_monarch(root: Path, estate: Estate, identity: IdentityMap) -> None:
    folder = root / "legacy" / "monarch"
    paths = sorted(folder.glob("Transactions_*.csv"))
    if not paths:
        raise BuildError(f"no Monarch Transactions_*.csv files under {folder}")
    map_path = root / "normalized" / "monarch-account-map.json"
    mapping = _load_json(map_path)
    if not isinstance(mapping, dict):
        raise BuildError("monarch-account-map.json must be an object")
    estate.source_files.add(map_path)
    for path in paths:
        estate.source_files.add(path)
        source = _source_path(path, root)
        fallback_counts: dict[str, int] = {}
        for txn in read_transactions(path):
            target = mapping.get(txn.account)
            if not isinstance(target, str):
                raise BuildError(f"{source}: Monarch account {txn.account!r} is unmapped")
            account_id = identity.id(target, source)
            source_id = txn.source_id
            if not source_id:
                base = hashlib.sha256(
                    f"{account_id}|{txn.date}|{txn.amount}|{txn.merchant}|{txn.category}".encode()
                ).hexdigest()
                fallback_counts[base] = fallback_counts.get(base, 0) + 1
                source_id = f"monarch:{base}:{fallback_counts[base]}"
            else:
                source_id = f"monarch:{source_id}"
            estate.transactions.append(_transaction(
                identity, txn.date, account_id, txn.amount, txn.merchant, source_id,
                source, txn.category,
            ))


def _load_extracts(root: Path, estate: Estate, identity: IdentityMap) -> None:
    map_path = root / "extracts" / "mapping.json"
    mapping = _load_json(map_path)
    entries = mapping.get("files") if isinstance(mapping, dict) else None
    if not isinstance(entries, list):
        raise BuildError("extracts/mapping.json must contain a files list")
    estate.source_files.add(map_path)
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("file") or not entry.get("account"):
            raise BuildError("each extracts mapping entry requires file and account")
        path = Path(str(entry["file"]))
        if not path.is_absolute():
            path = root / "extracts" / path
        if not path.exists():
            raise BuildError(f"mapped extract is missing: {path}")
        if path.suffix.casefold() not in {".csv", ".ofx", ".qfx"}:
            raise BuildError(f"unsupported mapped extract shape: {path}")
        account_id = identity.name(str(entry["account"]), str(path))
        try:
            extract = parsers.parse_file(path, account_id=account_id)
        except ValueError as exc:
            raise BuildError(str(exc)) from None
        estate.source_files.add(path)
        source = _source_path(path, root)
        for txn in extract.transactions:
            prefix = "synthetic" if txn.id_is_synthetic else "stable"
            estate.transactions.append(_transaction(
                identity, txn.date, account_id, txn.amount, txn.description,
                f"extract:{prefix}:{txn.source_id}", source, txn.kind or "",
            ))
        if extract.balance is not None and extract.balance_date is not None:
            estate.valuations.append(ValuationRow(
                extract.balance_date.isoformat(), account_id, _money(extract.balance),
                identity.accounts[account_id].currency, source, "observed",
            ))


def _load_simplefin(root: Path, estate: Estate, identity: IdentityMap) -> None:
    candidates = sorted((root / "raw" / "simplefin").rglob("simplefin-*.json"))
    if not candidates:
        raise BuildError("no immutable SimpleFIN snapshot found under raw/simplefin")
    snapshot = candidates[-1]
    mapping_path = root / "simplefin" / "account-map.json"
    mapping_doc = _load_json(mapping_path)
    if mapping_doc.get("version") != 1 or not isinstance(mapping_doc.get("accounts"), dict):
        raise BuildError("simplefin/account-map.json must have version 1 and accounts")
    accounts, errors = read_snapshot(snapshot)
    if errors:
        raise BuildError("latest SimpleFIN snapshot has institution errors: " + "; ".join(errors))
    mapping = mapping_doc["accounts"]
    estate.source_files.update({snapshot, mapping_path})
    source = _source_path(snapshot, root)
    for account in accounts:
        entry = mapping.get(account.id)
        if not isinstance(entry, dict):
            raise BuildError(f"{source}: SimpleFIN source account is unmapped")
        action = entry.get("action", "import")
        if action == "import":
            # assertionAccountId is the facts-layer identity. The Wealthfolio
            # target is only an app routing detail and can outlive its account fact.
            account_id = identity.id(
                str(entry.get("assertionAccountId") or entry.get("wealthfolioAccountId") or ""),
                source,
            )
        elif action == "monitor":
            account_id = identity.id(
                str(entry.get("assertionAccountId") or entry.get("wealthfolioAlternativeAssetId") or ""),
                source,
            )
        elif action == "observe":
            account_id = identity.id(str(entry.get("assertionAccountId") or ""), source)
        elif action == "exclude":
            try:
                account_id = identity.name(account.name, source)
            except BuildError:
                account_id = identity.excluded_source(account.name, account.org, source)
        else:
            raise BuildError(f"{source}: unsupported SimpleFIN mapping action {action!r}")
        if account.balance_date:
            estate.valuations.append(ValuationRow(
                account.balance_date.isoformat(), account_id, _money(account.balance),
                account.currency, source, "observed",
            ))
        if action not in {"import", "exclude"}:
            if account.transactions:
                estate.warnings.append(
                    f"{source}: {len(account.transactions)} transactions omitted for {action} account "
                    f"{account_id}"
                )
            continue
        for txn in account.transactions:
            estate.transactions.append(_transaction(
                identity, txn.posted, account_id, txn.amount, txn.description,
                f"simplefin:{account.id}:{txn.id}", source, "",
                excluded=txn.pending or action == "exclude",
                reason="pending transaction" if txn.pending else (
                    identity.accounts[account_id].exclusion_reason if action == "exclude" else ""
                ),
            ))


def _load_ledger(root: Path, estate: Estate, identity: IdentityMap) -> None:
    path = root / "raw" / "ledger-live-normalized.json"
    payload = _load_json(path)
    if payload.get("schema_version") != 1 or not isinstance(payload.get("accounts"), list):
        raise BuildError("raw/ledger-live-normalized.json has an unsupported schema")
    estate.source_files.add(path)
    source = _source_path(path, root)
    divisor = Decimal(str(payload.get("satoshis_per_btc") or 100_000_000))
    for raw in payload["accounts"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("operations"), list):
            raise BuildError(f"{source}: malformed Ledger account")
        account_id = identity.ledger(str(raw.get("name") or ""), source)
        operations = raw["operations"]
        dates = [str(op.get("occurred_at") or "")[:10] for op in operations if op.get("occurred_at")]
        as_of = max(dates) if dates else str(raw.get("created_at") or "")[:10]
        estate.positions.append(PositionRow(
            as_of, account_id, str(payload.get("unit") or "BTC").upper(),
            _money(Decimal(str(raw.get("current_balance_sat"))) / divisor), "", "", "", source,
        ))
        for operation in operations:
            direction = operation.get("direction")
            if direction not in {"inflow", "outflow"}:
                raise BuildError(f"{source}: unsupported Ledger operation direction")
            amount = Decimal(str(operation.get("value_sat"))) / divisor
            if direction == "outflow":
                amount = -amount
            estate.transactions.append(_transaction(
                identity, str(operation.get("occurred_at"))[:10], account_id, amount,
                f"{str(payload.get('unit') or 'BTC').upper()} {direction}",
                f"ledger:{operation.get('txid')}", source, direction,
                excluded=bool(operation.get("failed")),
                reason="failed operation" if operation.get("failed") else "",
            ))


def _load_vanguard(root: Path, estate: Estate, identity: IdentityMap) -> None:
    folder = root / "extracts" / "vanguard"
    config_path = folder / "mapping.json"
    config = _load_json(config_path)
    candidates = sorted(
        path for path in folder.glob("*.csv")
        if vanguard.looks_like_vanguard(path.read_text(encoding="utf-8-sig", errors="replace"))
    )
    if not candidates:
        raise BuildError(f"no supported Vanguard holdings/transactions CSV under {folder}")
    path = candidates[-1]
    export = vanguard.parse_file(path)
    if not export.account_numbers:
        raise BuildError(f"{path}: Vanguard CSV contains no accounts")
    as_of = str(config.get("asOf") or "")
    try:
        date.fromisoformat(as_of)
    except ValueError:
        raise BuildError("Vanguard mapping requires ISO asOf") from None
    specs = config.get("accounts")
    if not isinstance(specs, dict):
        raise BuildError("Vanguard mapping requires accounts")
    exclusions = config.get("excludedAccounts")
    if not isinstance(exclusions, dict):
        raise BuildError("Vanguard mapping requires durable excludedAccounts decisions")
    resolution_path = root / "plans" / "vanguard-in-kind-prices.json"
    resolution_doc = _load_json(resolution_path)
    resolutions = resolution_doc.get("events")
    if not isinstance(resolutions, list):
        raise BuildError(f"{resolution_path}: resolutions require an events list")
    workbook_paths = sorted(
        item for item in folder.glob("*.xlsx") if vanguard_activity.sniff_workbook(item)
    )
    if not workbook_paths:
        raise BuildError(f"no Vanguard full-history XLSX workbooks under {folder}")
    reports = vanguard_activity.parse_files(workbook_paths)
    estate.source_files.update({config_path, path, resolution_path, *workbook_paths})
    combined_source = _source_path(path, root)
    holdings: dict[str, list[vanguard.Holding]] = defaultdict(list)
    for holding in export.holdings:
        holdings[holding.account_number].append(holding)

    mapped_rows = 0
    excluded_rows = 0
    history: dict[str, tuple[list[vanguard_history.PlannedEvent], str]] = {}
    for report in reports:
        number = report.account.account_number
        if number not in specs:
            decision = exclusions.get(number)
            if not isinstance(decision, dict) or not all(
                decision.get(key) for key in ("decision", "reason")
            ):
                raise BuildError(
                    f"{report.source}: unmapped workbook requires an excludedAccounts decision"
                )
            excluded_rows += len(report.transactions)
            continue
        spec = specs[number]
        if not isinstance(spec, dict) or not spec.get("name"):
            raise BuildError(f"{report.source}: Vanguard account is unmapped")
        account_id = identity.name(str(spec["name"]), report.source)
        events = vanguard_history.classify_transactions(
            number, account_id, report.transactions
        )
        events, issues = vanguard_history.apply_resolutions(
            events, account_id, resolutions
        )
        blocked = [event for event in events if event.status == "blocked"]
        if issues or blocked:
            details = issues or [
                f"{event.event_date} {event.symbol or 'cash'}: {event.reason}"
                for event in blocked
            ]
            raise BuildError(
                f"{report.source}: unresolved Vanguard history: {'; '.join(details)}"
            )
        vanguard_history.assign_idempotency_keys(events)
        history[number] = (events, _source_path(folder / report.source, root))
        mapped_rows += len(report.transactions)
    if set(history) != set(specs):
        raise BuildError("Vanguard mapping and full-history workbooks do not match")

    inflows = {"DEPOSIT", "CREDIT", "TRANSFER_IN", "INTEREST", "DIVIDEND", "SELL"}
    canonical_history_rows: list[TransactionRow] = []
    for number, (events, source) in sorted(history.items()):
        account_id = identity.name(str(specs[number]["name"]), source)
        expected_shares = {
            row.symbol: row.shares
            for row in holdings[number]
            if row.symbol != vanguard_history.CASH_SYMBOL
        }
        actual_shares = {
            symbol: quantity
            for symbol, quantity in vanguard_history.share_totals(events).items()
            if quantity
        }
        if actual_shares != expected_shares:
            raise BuildError(f"{source}: Vanguard share reconciliation failed")
        expected_cash = sum(
            (row.shares for row in holdings[number] if row.symbol == vanguard_history.CASH_SYMBOL),
            Decimal("0"),
        )
        actual_cash = vanguard_history.cash_total(events)
        if actual_cash != expected_cash:
            raise BuildError(
                f"{source}: Vanguard cash reconciliation failed "
                f"({_money(actual_cash)} != {_money(expected_cash)})"
            )
        for event in events:
            for activity in event.activities:
                kind = str(activity["activityType"])
                asset = activity.get("asset") or {}
                quantity = Decimal(str(activity.get("quantity") or 0))
                if kind in {"SELL", "TRANSFER_OUT"}:
                    quantity = -abs(quantity)
                metadata = json.loads(activity.get("metadata") or "{}")
                asset_transfer = kind in {"TRANSFER_IN", "TRANSFER_OUT"} and (
                    activity.get("quantity") is not None
                )
                amount = Decimal("0") if asset_transfer else Decimal(
                    str(activity.get("amount") or 0)
                )
                if kind not in inflows:
                    amount = -abs(amount)
                row = TransactionRow(
                    activity["activityDate"][:10], account_id, _money(amount),
                    str(activity.get("comment") or f"Vanguard history: {kind}"),
                    str(activity["idempotencyKey"]), source, kind,
                    str(event.reason or "") if str(event.reason or "").startswith("20") else "",
                    str(asset.get("symbol") or ""),
                    _money(quantity) if activity.get("quantity") is not None else "",
                    _money(activity.get("unitPrice")) if activity.get("unitPrice") is not None else "",
                    bool(metadata.get("flow", {}).get("is_external")),
                )
                estate.transactions.append(row)
                canonical_history_rows.append(row)

    basis_state: dict[tuple[str, str], tuple[Decimal, Decimal, bool]] = {}
    transferred_basis: dict[str, Decimal] = {}
    for row in sorted(
        canonical_history_rows,
        key=lambda item: (
            item.date,
            0 if item.category == "TRANSFER_OUT" else 1,
            item.account_id,
            item.source_id,
        ),
    ):
        if not row.symbol or not row.quantity:
            continue
        key = (row.account_id, row.symbol)
        held, cost, known = basis_state.get(
            key, (Decimal("0"), Decimal("0"), True)
        )
        quantity = Decimal(row.quantity)
        average = cost / held if known and held else Decimal("0")
        if row.category == "BUY":
            if not row.price:
                known = False
            elif known:
                cost += abs(quantity) * Decimal(row.price)
            held += quantity
        elif row.category == "SELL":
            if known:
                cost += quantity * average
            held += quantity
        elif row.category == "TRANSFER_OUT":
            if row.transfer_group and known:
                transferred_basis[row.transfer_group] = average
                cost += quantity * average
            else:
                known = False
            held += quantity
        elif row.category == "TRANSFER_IN":
            pair_basis = transferred_basis.get(row.transfer_group)
            if row.external_flow or pair_basis is None:
                known = False
            elif known:
                cost += quantity * pair_basis
            held += quantity
        basis_state[key] = (held, cost, known)

    for number in sorted(history):
        source = history[number][1]
        account_id = identity.name(str(specs[number]["name"]), source)
        for holding in holdings[number]:
            held, cost, known = basis_state.get(
                (account_id, holding.symbol), (Decimal("0"), Decimal("0"), False)
            )
            basis = (
                Decimal("1")
                if holding.symbol == vanguard_history.CASH_SYMBOL
                else cost / held
                if known and held == holding.shares and held
                else None
            )
            estate.positions.append(PositionRow(
                as_of, account_id, holding.symbol, _money(holding.shares),
                _money(holding.share_price), _money(holding.total_value), _money(basis),
                combined_source,
            ))
        estate.valuations.append(ValuationRow(
            as_of, account_id, _money(export.value_of(number)), "USD",
            combined_source, "observed",
        ))
    estate.source_stats.update({
        "vanguardMappedWorkbookRows": mapped_rows,
        "vanguardExcludedWorkbookRows": excluded_rows,
        "vanguardCanonicalActivities": sum(
            len(event.activities) for events, _ in history.values() for event in events
        ),
    })
    estate.warnings.append(
        "vanguard-combined-csv-transactions-ignored: full-history XLSX workbooks "
        "take precedence; combined CSV retained for current positions and valuation"
    )


def _apply_decisions(estate: Estate, facts: list[Any], identity: IdentityMap) -> None:
    handoffs: list[tuple[str, date, str]] = []
    account_exclusions: dict[str, str] = {}
    transfer_groups: dict[str, str] = {}
    category_overrides: dict[str, str] = {}
    for parsed in facts:
        fact = parsed.fact
        if not isinstance(fact, DecisionFact):
            continue
        if fact.kind == "duplicate-account" and not fact.resolution.upper().startswith("DEFERRED"):
            match = re.search(r"handoff\s+(\d{4}-\d{2}-\d{2})", fact.resolution, re.I)
            if not match:
                raise BuildError(f"{parsed.path}: active duplicate-account decision lacks handoff date")
            inactive = [
                account_id for account_id in fact.affects
                if account_id in identity.accounts
                and "inactive" in identity.accounts[account_id].exclusion_reason.casefold()
            ]
            if len(inactive) != 1:
                raise BuildError(f"{parsed.path}: duplicate-account decision has no unique inactive account")
            handoffs.append((inactive[0], date.fromisoformat(match.group(1)), fact.id))
        if fact.kind == "account-exclusion":
            for account_id in fact.affects:
                identity.id(account_id, str(parsed.path))
                account_exclusions[account_id] = f"account-exclusion decision: {fact.id}"
        group = parsed.data.get("transferGroup")
        source_ids = parsed.data.get("sourceIds") or parsed.data.get("affectsSourceIds")
        if group and isinstance(source_ids, list):
            for source_id in source_ids:
                transfer_groups[str(source_id)] = str(group)
        if fact.kind == "category":
            category = parsed.data.get("category")
            if not isinstance(category, str) or not category.strip():
                raise BuildError(f"{parsed.path}: category decision has no category")
            if not isinstance(source_ids, list) or not source_ids:
                raise BuildError(f"{parsed.path}: category decision has no sourceIds")
            for source_id in source_ids:
                category_overrides[str(source_id)] = category.strip()

    estate.accounts = [
        AccountRow(**{
            **asdict(row),
            "excluded": True,
            "exclusion_reason": row.exclusion_reason or account_exclusions[row.account_id],
        }) if row.account_id in account_exclusions else row
        for row in estate.accounts
    ]
    changed: list[TransactionRow] = []
    for row in estate.transactions:
        data = asdict(row)
        if row.account_id in account_exclusions:
            data["excluded"] = True
            data["exclusion_reason"] = (
                row.exclusion_reason or account_exclusions[row.account_id]
            )
        for account_id, cutoff, decision_id in handoffs:
            if row.account_id == account_id and date.fromisoformat(row.date) >= cutoff:
                data["excluded"] = True
                data["exclusion_reason"] = f"duplicate-account handoff: {decision_id}"
        transfer = transfer_groups.get(row.source_id)
        if transfer:
            data["transfer_group"] = transfer
        category = category_overrides.get(row.source_id)
        if category:
            data["category"] = category
        changed.append(TransactionRow(**data))
    estate.transactions = changed


def _deduplicate(estate: Estate) -> None:
    grouped: dict[tuple[str, str], list[TransactionRow]] = {}
    for row in sorted(estate.transactions, key=_transaction_sort):
        grouped.setdefault((row.account_id, row.source_id), []).append(row)
    selected: list[TransactionRow] = []
    for (account_id, source_id), rows in grouped.items():
        by_content: dict[tuple[Any, ...], list[TransactionRow]] = {}
        for row in rows:
            key = (
                row.date, row.amount, row.description, row.category, row.transfer_group,
                row.symbol, row.quantity, row.price, row.external_flow,
                row.excluded, row.exclusion_reason,
            )
            by_content.setdefault(key, []).append(row)
        collision = len(by_content) > 1
        if collision:
            estate.warnings.append(
                f"conflicting-source-id: {account_id} {source_id}; retained "
                f"{len(by_content)} distinct rows with deterministic suffixes"
            )
        for content, copies in sorted(by_content.items(), key=lambda item: item[0]):
            kept = copies[0]
            if collision:
                suffix = hashlib.sha256(
                    json.dumps(content, separators=(",", ":"), default=str).encode()
                ).hexdigest()[:16]
                kept = TransactionRow(**{
                    **asdict(kept), "source_id": f"{source_id}:collision:{suffix}"
                })
            selected.append(kept)
            for duplicate in copies[1:]:
                estate.warnings.append(
                    f"duplicate-source-id: {account_id} {source_id}; "
                    f"kept {kept.source_file}, also seen in {duplicate.source_file}"
                )
    estate.transactions = selected

    fingerprints: dict[tuple[str, ...], list[TransactionRow]] = {}
    for row in estate.transactions:
        key = (
            row.account_id, row.date, _money(row.amount), _norm(row.description),
            row.symbol, row.quantity, row.price,
        )
        fingerprints.setdefault(key, []).append(row)
    canonical: list[TransactionRow] = []
    for rows in fingerprints.values():
        if len(rows) == 1:
            canonical.extend(rows)
            continue

        by_file: dict[str, list[TransactionRow]] = defaultdict(list)
        for row in rows:
            by_file[row.source_file].append(row)
        # Two identical rows inside one source may be two real same-day
        # purchases. Only collapse when every source contributed at most one
        # row, so the cross-source match is one-to-one rather than guessed.
        if len(by_file) < 2 or any(len(copies) > 1 for copies in by_file.values()):
            sample = sorted(rows, key=_transaction_sort)[0]
            estate.warnings.append(
                f"ambiguous-cross-source-duplicate: {sample.account_id} {sample.date} "
                f"{sample.amount}; retained {len(rows)} rows"
            )
            canonical.extend(rows)
            continue

        preferred = min(rows, key=lambda row: (_source_priority(row), _transaction_sort(row)))
        monarch_category = next(
            (
                row.category
                for row in rows
                if row.source_id.startswith("monarch:") and row.category
            ),
            "",
        )
        transfer_group = next((row.transfer_group for row in rows if row.transfer_group), "")
        kept = TransactionRow(
            **{
                **asdict(preferred),
                # Direct files and SimpleFIN have stronger identity but often
                # no spending category. Preserve Monarch's category as
                # enrichment without retaining its duplicate money movement.
                "category": monarch_category or preferred.category,
                "transfer_group": transfer_group or preferred.transfer_group,
            }
        )
        canonical.append(kept)
        estate.warnings.append(
            f"cross-source-deduplicated: {kept.account_id} {kept.date} "
            f"{kept.amount}; kept {kept.source_file}, matched "
            f"{','.join(sorted(path for path in by_file if path != kept.source_file))}"
        )
    estate.transactions = canonical


def _source_priority(row: TransactionRow) -> int:
    """Prefer the strongest identity when one transaction appears in sources."""
    if row.source_id.startswith("extract:stable:"):
        return 0
    if row.source_id.startswith("simplefin:"):
        return 1
    if row.source_id.startswith("extract:synthetic:"):
        return 2
    if row.source_id.startswith("ledger:"):
        return 2
    if row.source_id.startswith("vanguard-history:"):
        return 0
    if row.source_id.startswith("monarch:"):
        return 3
    return 4


def _verify_assertions(estate: Estate, facts: list[Any], root: Path) -> None:
    assertions = {
        (fact.fact.account_id, fact.fact.on.isoformat()): fact.fact.balance
        for fact in facts
        if isinstance(fact.fact, AssertionFact) and fact.fact.on and fact.fact.balance is not None
    }
    fact_files = {_source_path(fact.path, root) for fact in facts}
    observed: dict[tuple[str, str], set[Decimal]] = {}
    for row in estate.valuations:
        if row.source_file not in fact_files and row.entity_id in {a.account_id for a in estate.accounts}:
            observed.setdefault((row.entity_id, row.date), set()).add(Decimal(row.value))
    for key, expected in assertions.items():
        actual = observed.get(key)
        if actual is not None and actual != {expected}:
            raise BuildError(
                f"assertion failed for account {key[0]} on {key[1]}: "
                f"expected {_money(expected)}, observed {','.join(sorted(_money(v) for v in actual))}"
            )


def _transaction_sort(row: TransactionRow) -> tuple[str, ...]:
    return (
        row.date, row.account_id, row.source_id, row.source_file, row.amount,
        row.description, row.category,
    )


def _finish(estate: Estate, facts: list[Any], root: Path) -> Estate:
    _apply_decisions(estate, facts, IdentityMap(estate.accounts))
    _deduplicate(estate)
    _verify_assertions(estate, facts, root)
    estate.accounts.sort(key=lambda row: row.account_id)
    estate.transactions.sort(key=_transaction_sort)
    estate.positions.sort(key=lambda row: (
        row.as_of, row.account_id, row.symbol, row.source_file, row.quantity,
    ))
    estate.valuations.sort(key=lambda row: (
        row.date, row.entity_id, row.source_file, row.value, row.observed_or_derived,
    ))
    estate.warnings = sorted(set(estate.warnings))
    return estate


def collect(data_dir: str | Path) -> Estate:
    root = Path(data_dir)
    estate, identity, facts = _load_fact_rows(root)
    _load_monarch(root, estate, identity)
    _load_extracts(root, estate, identity)
    _load_simplefin(root, estate, identity)
    _load_ledger(root, estate, identity)
    _load_vanguard(root, estate, identity)
    return _finish(estate, facts, root)


def _csv_bytes(rows: list[Any], columns: tuple[str, ...]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        data = asdict(row)
        writer.writerow({
            key: ("true" if value is True else "false" if value is False else value)
            for key, value in data.items()
        })
    return output.getvalue().encode("utf-8")


def _data_documents(estate: Estate) -> dict[str, bytes]:
    return {
        "accounts.csv": _csv_bytes(estate.accounts, ACCOUNT_COLUMNS),
        "transactions.csv": _csv_bytes(estate.transactions, TRANSACTION_COLUMNS),
        "positions.csv": _csv_bytes(estate.positions, POSITION_COLUMNS),
        "valuations.csv": _csv_bytes(estate.valuations, VALUATION_COLUMNS),
    }


def _summary(estate: Estate, root: Path) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "rowCounts": {
            "accounts": len(estate.accounts),
            "transactions": len(estate.transactions),
            "positions": len(estate.positions),
            "valuations": len(estate.valuations),
        },
        "sourceFiles": [
            {"path": _source_path(path, root), "sha256": _sha256(path)}
            for path in sorted(estate.source_files, key=lambda item: _source_path(item, root))
        ],
        "sourceStats": dict(sorted(estate.source_stats.items())),
        "warnings": estate.warnings,
    }


def plan(data_dir: str | Path) -> dict[str, Any]:
    root = Path(data_dir)
    return _summary(collect(root), root)


def build(
    data_dir: str | Path, *, now: datetime | None = None,
    before_swap: Any | None = None,
) -> dict[str, Any]:
    root = Path(data_dir)
    estate = collect(root)
    documents = _data_documents(estate)
    canonical = root / "normalized" / "canonical"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    staging = canonical.parent / f".canonical-staging-{uuid.uuid4().hex}"
    backup = canonical.parent / f".canonical-backup-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, content in documents.items():
            (staging / name).write_bytes(content)
        manifest = {
            **_summary(estate, root),
            "buildTimestamp": (now or datetime.now(timezone.utc)).isoformat(),
            "dataFiles": {
                name: hashlib.sha256(content).hexdigest()
                for name, content in sorted(documents.items())
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        if before_swap:
            before_swap(staging)
        moved_old = False
        try:
            if canonical.exists():
                os.replace(canonical, backup)
                moved_old = True
            os.replace(staging, canonical)
        except Exception:
            if moved_old and backup.exists() and not canonical.exists():
                os.replace(backup, canonical)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists() and canonical.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _read_csv(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != columns:
            raise BuildError(f"{path.name}: columns do not match schema")
        return list(reader)


def verify_publication(data_dir: str | Path) -> dict[str, Any]:
    """Verify the published canonical manifest, sources, and data files."""
    root = Path(data_dir)
    canonical = root / "normalized" / "canonical"
    manifest_path = canonical / "manifest.json"
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise BuildError("manifest must be an object")
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise BuildError("manifest schema version is unsupported")
    source_files = manifest.get("sourceFiles")
    if not isinstance(source_files, list):
        raise BuildError("manifest sourceFiles must be a list")
    for source in source_files:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("path"), str)
            or not isinstance(source.get("sha256"), str)
        ):
            raise BuildError("manifest sourceFiles contains an invalid entry")
        path = (root / source["path"]).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            raise BuildError(f"source path leaves data directory: {source['path']}") from None
        if not path.is_file() or _sha256(path) != source["sha256"]:
            raise BuildError(f"source hash mismatch: {source['path']}")
    schemas = {
        "accounts.csv": ACCOUNT_COLUMNS,
        "transactions.csv": TRANSACTION_COLUMNS,
        "positions.csv": POSITION_COLUMNS,
        "valuations.csv": VALUATION_COLUMNS,
    }
    data_files = manifest.get("dataFiles")
    if not isinstance(data_files, dict) or set(data_files) != set(schemas):
        raise BuildError("manifest dataFiles does not match the canonical schema")
    rows: dict[str, list[dict[str, str]]] = {}
    for name, columns in schemas.items():
        path = canonical / name
        expected = data_files.get(name)
        if not path.exists() or _sha256(path) != expected:
            raise BuildError(f"canonical data hash mismatch: {name}")
        rows[name] = _read_csv(path, columns)
    expected_counts = manifest.get("rowCounts")
    if not isinstance(expected_counts, dict) or set(expected_counts) != {
        name.removesuffix(".csv") for name in OUTPUT_NAMES
    }:
        raise BuildError("manifest rowCounts does not match the canonical schema")
    for name in OUTPUT_NAMES:
        key = name.removesuffix(".csv")
        if len(rows[name]) != expected_counts.get(key):
            raise BuildError(f"row count mismatch: {name}")
    account_ids = {row["account_id"] for row in rows["accounts.csv"]}
    if len(account_ids) != len(rows["accounts.csv"]):
        raise BuildError("accounts.csv contains duplicate account_id")
    for row in rows["transactions.csv"]:
        if row["account_id"] not in account_ids:
            raise BuildError("transactions.csv references an unknown account")
        _money(row["amount"])
        for key in ("quantity", "price"):
            if row[key]:
                _money(row[key])
        if row["external_flow"] not in {"true", "false"}:
            raise BuildError("transactions.csv contains invalid external_flow")
        date.fromisoformat(row["date"])
    for row in rows["positions.csv"]:
        if row["account_id"] not in account_ids:
            raise BuildError("positions.csv references an unknown account")
        date.fromisoformat(row["as_of"])
        for key in ("quantity", "price", "market_value", "basis_per_unit"):
            if row[key]:
                _money(row[key])
    for row in rows["valuations.csv"]:
        date.fromisoformat(row["date"])
        _money(row["value"])
    return {
        "verified": True,
        "schemaVersion": SCHEMA_VERSION,
        "rowCounts": expected_counts,
        "warnings": manifest.get("warnings", []),
    }


def verify(data_dir: str | Path) -> dict[str, Any]:
    root = Path(data_dir)
    canonical = root / "normalized" / "canonical"
    result = verify_publication(root)
    recomputed = collect(root)
    documents = _data_documents(recomputed)
    for name, content in documents.items():
        if (canonical / name).read_bytes() != content:
            raise BuildError(f"canonical data differs from recomputed data: {name}")
    return result
