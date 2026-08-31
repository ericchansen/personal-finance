"""Build a read-only, fingerprinted Vanguard history migration plan.

The planner deliberately separates source classification from Wealthfolio
mutation.  Some Vanguard rows describe in-kind transfers without a price or
cost basis.  Those rows participate in share reconciliation, but remain
blocked plan items rather than having a price invented for them.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from .vanguard_activity import VanguardActivity, VanguardActivityReport
except ImportError:  # pragma: no cover - direct script execution
    from vanguard_activity import VanguardActivity, VanguardActivityReport


CASH_SYMBOL = "VMFXX"
PLAN_SCHEMA = 1
OBSERVED_TYPES = {
    "Buy",
    "Buy (exchange)",
    "Capital gain (LT)",
    "Capital gain (ST)",
    "Contribution",
    "Distribution",
    "Dividend",
    "Recharacterization (incoming)",
    "Recharacterization (outgoing)",
    "Reinvestment",
    "Reinvestment (LT gain)",
    "Reinvestment (ST gain)",
    "Rollover (incoming)",
    "Sell",
    "Sell (exchange)",
    "Sweep in",
    "Sweep out",
    "Transfer (incoming)",
    "Transfer (outgoing)",
}


class VanguardHistoryError(ValueError):
    pass


class ApplyRefused(VanguardHistoryError):
    pass


@dataclass(frozen=True)
class AccountSpec:
    account_number: str
    wealthfolio_account_id: str
    wealthfolio_name: str
    expected_shares: Mapping[str, Decimal]
    expected_cash: Decimal


@dataclass
class PlannedEvent:
    account_number: str
    event_date: date
    source_types: list[str]
    symbol: str | None
    share_delta: Decimal = Decimal("0")
    cash_delta: Decimal = Decimal("0")
    activities: list[dict] = field(default_factory=list)
    status: str = "ready"
    reason: str | None = None


def effective_date(row: VanguardActivity) -> date:
    return row.settlement_date or row.transaction_date


def _asset(row: VanguardActivity) -> dict:
    return {
        "symbol": row.symbol,
        "name": (row.holding or row.symbol or "")[:120],
        "kind": "SECURITY",
        "quoteMode": "MARKET",
        "quoteCcy": "USD",
        "instrumentType": "MUTUALFUND",
    }


def _activity(
    account_id: str,
    kind: str,
    row: VanguardActivity,
    *,
    amount: Decimal | None = None,
    quantity: Decimal | None = None,
    unit_price: Decimal | None = None,
) -> dict:
    payload = {
        "accountId": account_id,
        "activityType": kind,
        "activityDate": f"{effective_date(row).isoformat()}T00:00:00Z",
        "currency": "USD",
        "isDraft": False,
        "comment": f"Vanguard history: {row.transaction_type}"[:200],
    }
    if amount is not None:
        payload["amount"] = float(abs(amount))
    if quantity is not None or (kind == "DIVIDEND" and row.symbol):
        payload["asset"] = _asset(row)
    if quantity is not None:
        payload["quantity"] = float(abs(quantity))
    if unit_price is not None:
        payload["unitPrice"] = float(abs(unit_price))
    return payload


def _pair_key(row: VanguardActivity) -> tuple[date, str | None, Decimal]:
    return effective_date(row), row.symbol, abs(row.cash_amount)


def _pair_rows(rows: Sequence[VanguardActivity]) -> tuple[list[tuple[VanguardActivity, VanguardActivity]], list[VanguardActivity]]:
    """Pair income declarations with their reinvestment cash leg.

    Most pairs have the same symbol. A real cross-fund case declares a VBTLX
    dividend and reinvests the exact amount into VFIAX on the same day. After
    exact-symbol matching, pair a cross-symbol remainder only when the
    date/amount identifies exactly one declaration and one purchase; anything
    ambiguous remains separate rather than guessed.
    """
    income: dict[tuple[date, str | None, Decimal], list[VanguardActivity]] = defaultdict(list)
    reinvest: dict[tuple[date, str | None, Decimal], list[VanguardActivity]] = defaultdict(list)
    income_types = {"Dividend", "Capital gain (LT)", "Capital gain (ST)"}
    reinvest_types = {"Reinvestment", "Reinvestment (LT gain)", "Reinvestment (ST gain)"}
    other: list[VanguardActivity] = []
    for row in rows:
        if row.transaction_type in income_types and row.cash_amount > 0:
            income[_pair_key(row)].append(row)
        elif row.transaction_type in reinvest_types and row.cash_amount < 0:
            reinvest[_pair_key(row)].append(row)
        else:
            other.append(row)
    pairs: list[tuple[VanguardActivity, VanguardActivity]] = []
    for key in sorted(set(income) | set(reinvest), key=lambda value: (value[0], value[1] or "", value[2])):
        declarations = income[key]
        purchases = reinvest[key]
        count = min(len(declarations), len(purchases))
        pairs.extend(zip(declarations[:count], purchases[:count]))
        other.extend(declarations[count:])
        other.extend(purchases[count:])
    remaining_income: dict[tuple[date, Decimal], list[VanguardActivity]] = defaultdict(list)
    remaining_reinvest: dict[tuple[date, Decimal], list[VanguardActivity]] = defaultdict(list)
    untouched: list[VanguardActivity] = []
    for row in other:
        broad_key = effective_date(row), abs(row.cash_amount)
        if row.transaction_type in income_types and row.cash_amount > 0:
            remaining_income[broad_key].append(row)
        elif row.transaction_type in reinvest_types and row.cash_amount < 0:
            remaining_reinvest[broad_key].append(row)
        else:
            untouched.append(row)
    for key in sorted(set(remaining_income) | set(remaining_reinvest)):
        declarations = remaining_income[key]
        purchases = remaining_reinvest[key]
        if len(declarations) == len(purchases) == 1:
            pairs.append((declarations[0], purchases[0]))
        else:
            untouched.extend(declarations)
            untouched.extend(purchases)
    return pairs, untouched


def _blocked(account_number: str, row: VanguardActivity, reason: str) -> PlannedEvent:
    return PlannedEvent(
        account_number=account_number,
        event_date=effective_date(row),
        source_types=[row.transaction_type],
        symbol=row.symbol,
        share_delta=row.shares or Decimal("0"),
        status="blocked",
        reason=reason,
    )


def _classify_single(account_number: str, account_id: str, row: VanguardActivity) -> PlannedEvent:
    kind = row.transaction_type
    shares = row.shares or Decimal("0")
    event = PlannedEvent(
        account_number=account_number,
        event_date=effective_date(row),
        source_types=[kind],
        symbol=row.symbol,
        share_delta=shares,
    )

    if kind in {"Sweep in", "Sweep out"}:
        event.status = "omitted"
        event.reason = "VMFXX is modeled as cash; sweep trades are internal"
        return event
    if kind == "Contribution":
        event.cash_delta = abs(row.cash_amount)
        event.activities = [_activity(account_id, "DEPOSIT", row, amount=row.cash_amount)]
        return event
    if kind == "Distribution":
        event.cash_delta = -abs(row.cash_amount)
        event.activities = [_activity(account_id, "WITHDRAWAL", row, amount=row.cash_amount)]
        return event
    if kind == "Rollover (incoming)":
        event.cash_delta = abs(row.cash_amount)
        activity = _activity(account_id, "TRANSFER_IN", row, amount=row.cash_amount)
        activity["subtype"] = "external_transfer"
        activity["metadata"] = json.dumps({"flow": {"is_external": True}})
        event.activities = [activity]
        return event
    if kind in {"Transfer (incoming)", "Transfer (outgoing)"}:
        if shares:
            return _blocked(
                account_number,
                row,
                "in-kind transfer has no acquisition price or original basis",
            )
        activity_type = "TRANSFER_IN" if kind.endswith("(incoming)") else "TRANSFER_OUT"
        event.cash_delta = abs(row.cash_amount) if activity_type == "TRANSFER_IN" else -abs(row.cash_amount)
        activity = _activity(account_id, activity_type, row, amount=row.cash_amount)
        activity["subtype"] = "external_transfer"
        activity["metadata"] = json.dumps({"flow": {"is_external": True}})
        event.activities = [activity]
        return event
    if kind.startswith("TRANSFER FROM ") or kind.startswith("TRANSFER TO "):
        return _blocked(
            account_number,
            row,
            "legacy in-kind account transfer has no acquisition price or original basis",
        )
    if kind.startswith("Recharacterization "):
        return _blocked(
            account_number,
            row,
            "in-kind recharacterization must preserve original lots and basis",
        )
    if kind in {"Buy", "Buy (exchange)"}:
        if not shares or row.share_price is None or row.cash_amount >= 0:
            return _blocked(
                account_number,
                row,
                "purchase-shaped in-kind row lacks trustworthy cash/price semantics",
            )
        event.cash_delta = -abs(row.cash_amount)
        event.activities = [
            _activity(
                account_id,
                "BUY",
                row,
                amount=row.cash_amount,
                quantity=shares,
                unit_price=row.share_price,
            )
        ]
        return event
    if kind in {"Sell", "Sell (exchange)"}:
        if not shares or row.share_price is None or row.cash_amount <= 0:
            return _blocked(account_number, row, "sale row lacks trustworthy quantity, price, or proceeds")
        event.cash_delta = abs(row.cash_amount)
        event.activities = [
            _activity(
                account_id,
                "SELL",
                row,
                amount=row.cash_amount,
                quantity=shares,
                unit_price=row.share_price,
            )
        ]
        return event
    if kind in {"Dividend", "Capital gain (LT)", "Capital gain (ST)"}:
        if shares:
            return _blocked(
                account_number,
                row,
                "quantity-bearing legacy income row has no price; acquisition date is retained",
            )
        if row.symbol == CASH_SYMBOL:
            event.cash_delta = abs(row.cash_amount)
            event.activities = [_activity(account_id, "DIVIDEND", row, amount=row.cash_amount)]
            return event
        event.cash_delta = abs(row.cash_amount)
        event.activities = [_activity(account_id, "DIVIDEND", row, amount=row.cash_amount)]
        return event
    if kind in {"Reinvestment", "Reinvestment (LT gain)", "Reinvestment (ST gain)"}:
        if row.symbol == CASH_SYMBOL:
            event.status = "omitted"
            event.reason = "VMFXX reinvestment remains cash"
            return event
        if not shares:
            return _blocked(account_number, row, "reinvestment has no trustworthy quantity")
        unit_price = row.share_price
        if unit_price is None and row.cash_amount:
            unit_price = abs(row.cash_amount) / abs(shares)
        if unit_price is None:
            return _blocked(account_number, row, "reinvestment has no trustworthy price")
        event.cash_delta = -abs(row.cash_amount)
        event.activities = [
            _activity(
                account_id,
                "BUY",
                row,
                amount=row.cash_amount,
                quantity=shares,
                unit_price=unit_price,
            )
        ]
        return event
    return _blocked(account_number, row, "unrecognized Vanguard transaction type")


def classify_transactions(
    account_number: str,
    account_id: str,
    rows: Sequence[VanguardActivity],
) -> list[PlannedEvent]:
    """Classify all rows, collapsing exact income/reinvestment source pairs.

    A collapsed pair emits both a DIVIDEND and a BUY.  Wealthfolio treats the
    former as a cash inflow and the latter as a cash outflow; emitting only the
    BUY, as the early design suggested, would create negative phantom cash.
    """
    pairs, singles = _pair_rows(rows)
    events: list[PlannedEvent] = []
    for declaration, purchase in pairs:
        shares = purchase.shares or Decimal("0")
        event = PlannedEvent(
            account_number=account_number,
            event_date=effective_date(purchase),
            source_types=[declaration.transaction_type, purchase.transaction_type],
            symbol=purchase.symbol,
            share_delta=shares,
        )
        if purchase.symbol == CASH_SYMBOL:
            # VMFXX itself is represented as cash. The dividend increases that
            # cash; the reinvestment merely converts cash to the same
            # cash-equivalent fund and is therefore omitted.
            event.activities = [
                _activity(
                    account_id,
                    "DIVIDEND",
                    declaration,
                    amount=declaration.cash_amount,
                )
            ]
            event.cash_delta = declaration.cash_amount
            event.reason = "VMFXX dividend retained; reinvestment is internal cash movement"
        elif not shares:
            event.status = "blocked"
            event.reason = "paired reinvestment has no trustworthy quantity"
        else:
            unit_price = purchase.share_price
            if unit_price is None and purchase.cash_amount:
                unit_price = abs(purchase.cash_amount) / abs(shares)
            if unit_price is None:
                event.status = "blocked"
                event.reason = "paired reinvestment has no trustworthy price"
                events.append(event)
                continue
            event.activities = [
                _activity(account_id, "DIVIDEND", declaration, amount=declaration.cash_amount),
                _activity(
                    account_id,
                    "BUY",
                    purchase,
                    amount=purchase.cash_amount,
                    quantity=shares,
                    unit_price=unit_price,
                ),
            ]
            event.cash_delta = declaration.cash_amount + purchase.cash_amount
        events.append(event)
    events.extend(_classify_single(account_number, account_id, row) for row in singles)
    return sorted(events, key=lambda event: (event.event_date, event.symbol or "", event.source_types))


def assign_idempotency_keys(events: Sequence[PlannedEvent]) -> None:
    """Assign deterministic keys and retain legitimate identical activities."""
    collisions: Counter[str] = Counter()
    for event in events:
        for activity in event.activities:
            symbol = ((activity.get("asset") or {}).get("symbol") or "cash").casefold()
            amount = Decimal(str(activity.get("amount") or 0)).quantize(Decimal("0.01"))
            quantity = Decimal(str(activity.get("quantity") or 0)).normalize()
            raw = ":".join(
                [
                    "vanguard-history",
                    event.account_number,
                    activity["activityDate"][:10],
                    activity["activityType"].casefold(),
                    symbol,
                    format(amount, "f"),
                    format(quantity, "f"),
                ]
            )
            ordinal = collisions[raw]
            collisions[raw] += 1
            activity["idempotencyKey"] = raw if ordinal == 0 else f"{raw}:repeat-{ordinal + 1}"


def share_totals(events: Iterable[PlannedEvent]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for event in events:
        if event.symbol and event.symbol != CASH_SYMBOL:
            result[event.symbol] += event.share_delta
    return dict(sorted(result.items()))


def cash_total(events: Iterable[PlannedEvent]) -> Decimal:
    return sum((event.cash_delta for event in events), Decimal("0"))


def activity_cash(rows: Iterable[Mapping], account_id: str) -> Decimal:
    """Derive current cash synchronously from activity rows."""
    inflow = {"DEPOSIT", "CREDIT", "TRANSFER_IN", "INTEREST", "DIVIDEND", "SELL"}
    outflow = {"WITHDRAWAL", "TRANSFER_OUT", "FEE", "TAX", "EXPENSE", "BUY"}
    total = Decimal("0")
    for row in rows:
        if row.get("accountId") != account_id:
            continue
        amount = Decimal(str(row.get("amount") or 0))
        kind = row.get("activityType")
        if (
            kind in {"BUY", "SELL"}
            and row.get("quantity") is not None
            and row.get("unitPrice") is not None
        ):
            # Holdings valuation recomputes trade cash from quantity × price;
            # the API still echoes the source `amount`, which may differ after
            # Vanguard rounds shares and NAVs independently.
            amount = abs(
                Decimal(str(row["quantity"]))
                * Decimal(str(row["unitPrice"]))
            )
        asset_transfer = kind in {"TRANSFER_IN", "TRANSFER_OUT"} and (
            row.get("assetId")
            or row.get("assetSymbol")
            or row.get("quantity") is not None
        )
        if asset_transfer:
            # API responses expose the transferred security's market value in
            # `amount`, but asset transfers alter holdings, not cash.
            continue
        if kind in inflow:
            total += amount
        elif kind in outflow:
            total -= amount
    return total


def live_activity_fingerprint(rows: Iterable[Mapping]) -> str:
    fields = (
        "id",
        "accountId",
        "activityType",
        "date",
        "activityDate",
        "amount",
        "quantity",
        "unitPrice",
        "idempotencyKey",
        "sourceGroupId",
    )
    canonical = [{key: row.get(key) for key in fields} for row in rows]
    canonical.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _jsonable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _resolved_asset(event: PlannedEvent, resolution: Mapping) -> dict:
    symbol = event.symbol or str(resolution.get("symbol") or "")
    return {
        "symbol": symbol,
        "name": symbol,
        "kind": "SECURITY",
        "quoteMode": "MARKET",
        "quoteCcy": "USD",
        "instrumentType": "MUTUALFUND",
    }


def _resolved_activity(
    event: PlannedEvent,
    account_id: str,
    kind: str,
    resolution: Mapping,
    *,
    external: bool,
) -> dict:
    payload = {
        "accountId": account_id,
        "activityType": kind,
        "activityDate": f"{event.event_date.isoformat()}T00:00:00Z",
        "asset": _resolved_asset(event, resolution),
        "quantity": float(abs(event.share_delta)),
        "unitPrice": float(Decimal(str(resolution["unitPrice"]))),
        "currency": "USD",
        "isDraft": False,
        "metadata": json.dumps({"flow": {"is_external": external}}),
        "comment": str(resolution.get("comment") or "Resolved Vanguard in-kind event")[:200],
    }
    if external:
        payload["subtype"] = "external_transfer"
    return payload


def apply_resolutions(
    events: Sequence[PlannedEvent],
    account_id: str,
    resolutions: Sequence[Mapping],
) -> tuple[list[PlannedEvent], list[str]]:
    """Resolve blocked events using externally researched, cited NAV evidence.

    Matching uses source facts, never positional event ids: account, date,
    symbol, source type, and share delta must identify exactly one blocked
    event. The resolution artifact's ordinal is merely presentation metadata.
    """
    candidates: dict[tuple[str, str, str | None, str, Decimal], list[Mapping]] = (
        defaultdict(list)
    )
    for resolution in resolutions:
        key = (
            str(resolution.get("account") or ""),
            str(resolution.get("date") or ""),
            resolution.get("symbol"),
            str(resolution.get("sourceType") or ""),
            Decimal(str(resolution.get("shareDelta") or 0)),
        )
        candidates[key].append(resolution)

    issues: list[str] = []
    for event in events:
        if event.status != "blocked" or len(event.source_types) != 1:
            continue
        key = (
            event.account_number,
            event.event_date.isoformat(),
            event.symbol,
            event.source_types[0],
            event.share_delta,
        )
        matches = candidates.get(key, [])
        if len(matches) != 1:
            if len(matches) > 1:
                issues.append(
                    f"{event.account_number} {event.event_date} {event.symbol}: "
                    "multiple in-kind resolutions match"
                )
            continue
        resolution = matches[0]
        if str(resolution.get("confidence") or "").upper() != "HIGH":
            issues.append(
                f"{event.account_number} {event.event_date} {event.symbol}: "
                "resolution confidence is not HIGH"
            )
            continue
        action = str(resolution.get("activityType") or "")
        price = resolution.get("unitPrice")
        if action == "OMIT":
            event.status = "omitted"
            event.reason = str(resolution.get("comment") or "resolved as no economic event")
            event.cash_delta = Decimal("0")
            event.activities = []
        elif action == "DIVIDEND+BUY":
            if price is None:
                issues.append(f"{event.account_number} {event.event_date}: missing NAV")
                continue
            amount = Decimal(str(resolution["amount"]))
            base = {
                "accountId": account_id,
                "activityDate": f"{event.event_date.isoformat()}T00:00:00Z",
                "currency": "USD",
                "isDraft": False,
                "comment": str(resolution.get("comment") or "")[:200],
            }
            event.activities = [
                {
                    **base,
                    "activityType": "DIVIDEND",
                    "asset": _resolved_asset(event, resolution),
                    "amount": float(amount),
                },
                {
                    **base,
                    "activityType": "BUY",
                    "asset": _resolved_asset(event, resolution),
                    "quantity": float(abs(event.share_delta)),
                    "unitPrice": float(Decimal(str(price))),
                    "amount": float(amount),
                },
            ]
            event.cash_delta = Decimal("0")
            event.status = "ready"
            event.reason = "resolved from cited historical NAV"
        elif action in {"BUY", "TRANSFER_IN"}:
            if price is None:
                issues.append(f"{event.account_number} {event.event_date}: missing NAV")
                continue
            # A zero-cash, quantity-bearing arrival is an asset transfer, not a
            # BUY: BUY would consume cash that never existed in the report.
            pair_id = str(resolution.get("pairId") or "")
            internal = bool(pair_id)
            event.activities = [
                _resolved_activity(
                    event,
                    account_id,
                    "TRANSFER_IN",
                    resolution,
                    external=not internal,
                )
            ]
            event.cash_delta = Decimal("0")
            event.status = "ready"
            event.reason = (
                pair_id
                or "external in-kind arrival at cited transfer-date NAV"
            )
        elif action in {"TRANSFER_OUT"}:
            if price is None:
                issues.append(f"{event.account_number} {event.event_date}: missing NAV")
                continue
            event.activities = [
                _resolved_activity(
                    event, account_id, action, resolution, external=False
                )
            ]
            event.cash_delta = Decimal("0")
            event.status = "ready"
            event.reason = str(resolution.get("pairId") or "internal in-kind transfer")
        else:
            issues.append(
                f"{event.account_number} {event.event_date} {event.symbol}: "
                f"unsupported resolution action {action!r}"
            )
    return list(events), issues


def plan_fingerprint(plan: Mapping) -> str:
    body = {key: value for key, value in plan.items() if key != "planFingerprint"}
    return hashlib.sha256(
        json.dumps(_jsonable(body), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _synthetic_deletions(rows: Sequence[Mapping], mapped_account_ids: set[str]) -> tuple[list[dict], list[str]]:
    deletions: list[dict] = []
    blockers: list[str] = []
    for row in rows:
        if row.get("accountId") not in mapped_account_ids:
            continue
        key = str(row.get("idempotencyKey") or "")
        activity_date = str(row.get("date") or row.get("activityDate") or "")
        key_date = None
        activity_day = None
        try:
            key_date = date.fromisoformat(key.rsplit(":", 1)[-1])
            activity_day = date.fromisoformat(activity_date[:10])
        except ValueError:
            pass
        is_synthetic = key.startswith("monarch:opening:") or (
            key.startswith("vanguard:reconcile:")
            and key_date is not None
            and activity_day == key_date - timedelta(days=1)
        ) or (
            key.startswith("vanguard:")
            and not key.startswith("vanguard:reconcile:")
            and activity_day == key_date
        )
        if not is_synthetic:
            continue
        if row.get("sourceGroupId"):
            blockers.append(
                f"refusing linked deletion {row.get('id')}: sourceGroupId={row.get('sourceGroupId')}"
            )
        deletions.append(
            {
                "id": row.get("id"),
                "accountId": row.get("accountId"),
                "activityType": row.get("activityType"),
                "activityDate": row.get("date") or row.get("activityDate"),
                "idempotencyKey": key,
                "sourceGroupId": row.get("sourceGroupId"),
            }
        )
    return deletions, blockers


def _reuse_linked_rollover(
    account_number: str,
    account_id: str,
    events: Sequence[PlannedEvent],
    live_activities: Sequence[Mapping],
) -> list[dict]:
    """Reuse a preserved employer-plan transfer inside full Vanguard history.

    The current ledger may already link a stale employer-plan balance to part of the
    first incoming rollover. Deleting it would erase real source-account
    history and can cascade to its counterpart. Keep that linked leg, reduce
    the separate residual deposit so both sum to Vanguard's observed rollover,
    and suppress creation of a second full rollover activity.
    """
    linked = [
        row
        for row in live_activities
        if row.get("accountId") == account_id
        and row.get("activityType") == "TRANSFER_IN"
        and row.get("sourceGroupId")
        and str(row.get("idempotencyKey") or "").startswith("vanguard:reconcile:")
    ]
    residuals = [
        row
        for row in live_activities
        if row.get("accountId") == account_id
        and str(row.get("idempotencyKey") or "").startswith(
            "rollover:unrecorded:"
        )
    ]
    if not linked and not residuals:
        return []
    if len(linked) != 1 or len(residuals) != 1:
        raise VanguardHistoryError(
            f"{account_number}: preserved rollover funding is ambiguous"
        )
    leg = linked[0]
    residual = residuals[0]
    event = next(
        (
            candidate
            for candidate in events
            if candidate.source_types == ["Rollover (incoming)"]
            and candidate.event_date
            == effective_date_from_mapping(leg)
        ),
        None,
    )
    if event is None:
        raise VanguardHistoryError(
            f"{account_number}: no source rollover matches preserved linked leg"
        )
    observed = event.cash_delta
    linked_amount = Decimal(str(leg.get("amount") or 0))
    new_residual = observed - linked_amount
    if new_residual < 0:
        raise VanguardHistoryError(
            f"{account_number}: linked rollover exceeds source rollover"
        )
    event.activities = []
    event.status = "reused"
    event.reason = "preserved linked employer-plan transfer plus residual funding update"
    return [
        {
            "id": residual.get("id"),
            "fingerprint": {
                "accountId": residual.get("accountId"),
                "activityType": residual.get("activityType"),
                "activityDate": residual.get("date")
                or residual.get("activityDate"),
                "amount": str(residual.get("amount") or 0),
                "idempotencyKey": residual.get("idempotencyKey"),
                "sourceGroupId": residual.get("sourceGroupId"),
            },
            "after": {
                "activityType": "DEPOSIT",
                "amount": format(new_residual, "f"),
                "comment": (
                    "Rollover amount above the balance preserved in the "
                    "linked employer plan"
                ),
            },
        }
    ]


def effective_date_from_mapping(row: Mapping) -> date:
    raw = str(row.get("date") or row.get("activityDate") or "")[:10]
    return date.fromisoformat(raw)


def build_plan(
    reports: Sequence[VanguardActivityReport],
    accounts: Mapping[str, AccountSpec],
    live_activities: Sequence[Mapping],
    workbook_hashes: Mapping[str, str],
    resolutions: Sequence[Mapping] = (),
) -> dict:
    events_by_account: dict[str, list[PlannedEvent]] = {}
    exclusions: list[dict] = []
    blockers: list[str] = []
    all_activities: list[dict] = []
    update_activities: list[dict] = []
    reconciliation: dict[str, dict] = {}

    for report in reports:
        number = report.account.account_number
        spec = accounts.get(number)
        if spec is None:
            exclusions.append(
                {
                    "accountNumber": number,
                    "source": report.source,
                    "transactionCount": len(report.transactions),
                    "reason": "unmapped account; preserved in source workbook and excluded",
                }
            )
            continue
        events = classify_transactions(number, spec.wealthfolio_account_id, report.transactions)
        events, resolution_issues = apply_resolutions(
            events, spec.wealthfolio_account_id, resolutions
        )
        blockers.extend(resolution_issues)
        try:
            update_activities.extend(
                _reuse_linked_rollover(
                    number, spec.wealthfolio_account_id, events, live_activities
                )
            )
        except VanguardHistoryError as exc:
            blockers.append(str(exc))
        assign_idempotency_keys(events)
        events_by_account[number] = events
        calculated = share_totals(events)
        expected = {
            symbol: shares
            for symbol, shares in spec.expected_shares.items()
            if symbol != CASH_SYMBOL
        }
        share_differences = {
            symbol: calculated.get(symbol, Decimal("0")) - expected.get(symbol, Decimal("0"))
            for symbol in sorted(set(calculated) | set(expected))
            if calculated.get(symbol, Decimal("0")) != expected.get(symbol, Decimal("0"))
        }
        calculated_cash = cash_total(events)
        cash_difference = calculated_cash - spec.expected_cash
        blocked = [event for event in events if event.status == "blocked"]
        if share_differences:
            blockers.append(f"{number}: share reconciliation failed")
        if abs(cash_difference) > Decimal("0.01"):
            blockers.append(f"{number}: cash reconciliation failed")
        if blocked:
            blockers.append(f"{number}: {len(blocked)} unpriced or ambiguous event(s)")
        reconciliation[number] = {
            "wealthfolioAccountId": spec.wealthfolio_account_id,
            "wealthfolioName": spec.wealthfolio_name,
            "calculatedShares": calculated,
            "expectedShares": expected,
            "shareDifferences": share_differences,
            "calculatedCash": calculated_cash,
            "expectedCash": spec.expected_cash,
            "cashDifference": cash_difference,
            "blockedEventCount": len(blocked),
            "sourceAsOf": max(event.event_date for event in events),
        }
        all_activities.extend(activity for event in events for activity in event.activities)

    mapped_ids = {spec.wealthfolio_account_id for spec in accounts.values()}
    deletions, linked_blockers = _synthetic_deletions(live_activities, mapped_ids)
    blockers.extend(linked_blockers)
    pair_members: dict[str, list[dict]] = defaultdict(list)
    for events in events_by_account.values():
        for event in events:
            if not event.reason or not event.reason.startswith("20"):
                continue
            for activity in event.activities:
                if activity["activityType"] in {"TRANSFER_IN", "TRANSFER_OUT"}:
                    pair_members[event.reason].append(
                        {
                            "idempotencyKey": activity["idempotencyKey"],
                            "activityType": activity["activityType"],
                            "accountId": activity["accountId"],
                        }
                    )
    link_activities = []
    for pair_id, members in sorted(pair_members.items()):
        if len(members) != 2 or {
            member["activityType"] for member in members
        } != {"TRANSFER_IN", "TRANSFER_OUT"}:
            blockers.append(f"{pair_id}: internal transfer pair is incomplete")
            continue
        link_activities.append({"pairId": pair_id, "members": members})

    plan = {
        "schemaVersion": PLAN_SCHEMA,
        "mode": "PLAN_ONLY",
        "workbookFingerprints": dict(sorted(workbook_hashes.items())),
        "liveActivityFingerprint": live_activity_fingerprint(live_activities),
        "mappedAccounts": sorted(accounts),
        "sourceTransactionCounts": {
            report.account.account_number: len(report.transactions) for report in reports
        },
        "excludedAccounts": exclusions,
        "events": {
            number: [_jsonable(asdict(event)) for event in events]
            for number, events in sorted(events_by_account.items())
        },
        "createActivities": all_activities,
        "deleteActivities": deletions,
        "updateActivities": update_activities,
        "linkActivities": link_activities,
        "protectedLinkedActivities": [
            {
                "id": row.get("id"),
                "accountId": row.get("accountId"),
                "activityType": row.get("activityType"),
                "activityDate": row.get("date") or row.get("activityDate"),
                "idempotencyKey": row.get("idempotencyKey"),
                "sourceGroupId": row.get("sourceGroupId"),
            }
            for row in live_activities
            if row.get("sourceGroupId") and row.get("accountId") in mapped_ids
        ],
        "reconciliation": reconciliation,
        "blockers": blockers,
        "applyAllowed": not blockers,
        "rollbackInstructions": [
            "Create and verify a Wealthfolio database backup before any deletion.",
            "If any mutation or post-check fails, stop and restore that backup.",
            "Never delete a sourceGroupId-linked activity; unlink/relink it explicitly first.",
            "Verify the pre-existing employer-plan-to-rollover transfer pair after restoration.",
        ],
    }
    plan = _jsonable(plan)
    plan["planFingerprint"] = plan_fingerprint(plan)
    return plan


def validate_apply_preconditions(
    plan: Mapping,
    *,
    supplied_fingerprint: str,
    current_live_activities: Sequence[Mapping],
) -> None:
    if plan_fingerprint(plan) != plan.get("planFingerprint"):
        raise ApplyRefused("plan content does not match its embedded fingerprint")
    if supplied_fingerprint != plan.get("planFingerprint"):
        raise ApplyRefused("exact plan fingerprint was not supplied")
    if live_activity_fingerprint(current_live_activities) != plan.get("liveActivityFingerprint"):
        raise ApplyRefused("live activities changed after planning")
    linked = [row for row in plan.get("deleteActivities", []) if row.get("sourceGroupId")]
    if linked:
        raise ApplyRefused("linked activities cannot be deleted without explicit unlink/relink")
    if plan.get("blockers"):
        raise ApplyRefused("plan has unresolved confidence gates")
    if not plan.get("applyAllowed"):
        raise ApplyRefused("plan is not approved for apply")


def execute_plan(client, plan: Mapping, live_activities: Sequence[Mapping]) -> dict:
    """Apply one fully validated plan in one bulk mutation, then link transfers."""
    by_id = {str(row.get("id")): row for row in live_activities}
    updates = []
    for planned in plan.get("updateActivities", []):
        current = by_id.get(str(planned.get("id")))
        if current is None:
            raise ApplyRefused(f"update activity disappeared: {planned.get('id')}")
        expected = planned["fingerprint"]
        actual = {
            "accountId": current.get("accountId"),
            "activityType": current.get("activityType"),
            "activityDate": current.get("date") or current.get("activityDate"),
            "amount": str(current.get("amount") or 0),
            "idempotencyKey": current.get("idempotencyKey"),
            "sourceGroupId": current.get("sourceGroupId"),
        }
        if actual != expected:
            raise ApplyRefused(f"update activity changed: {planned.get('id')}")
        payload = {key: value for key, value in current.items() if key != "date"}
        payload["activityDate"] = actual["activityDate"]
        payload.update(planned["after"])
        updates.append(payload)

    client.backup_database()
    result = client.save_activities(
        creates=list(plan.get("createActivities", [])),
        updates=updates,
        delete_ids=[
            str(row["id"]) for row in plan.get("deleteActivities", [])
        ],
    )
    errors = result.get("errors") or []
    if errors:
        raise ApplyRefused(f"bulk mutation returned {len(errors)} error(s): {errors[0]}")
    expected_counts = {
        "created": len(plan.get("createActivities", [])),
        "updated": len(updates),
        "deleted": len(plan.get("deleteActivities", [])),
    }
    for key, expected in expected_counts.items():
        actual = len(result.get(key, []))
        if actual != expected:
            raise ApplyRefused(
                f"bulk mutation {key} count mismatch: expected {expected}, got {actual}"
            )

    created_by_key = {
        row.get("idempotencyKey"): row
        for row in result.get("created", [])
        if row.get("idempotencyKey")
    }
    linked = 0
    for pair in plan.get("linkActivities", []):
        members = pair.get("members") or []
        if len(members) != 2:
            raise ApplyRefused(f"incomplete link group: {pair.get('pairId')}")
        rows = [created_by_key.get(member["idempotencyKey"]) for member in members]
        if any(row is None for row in rows):
            raise ApplyRefused(f"created transfer missing: {pair.get('pairId')}")
        client.post(
            "/activities/link",
            {"activityAId": rows[0]["id"], "activityBId": rows[1]["id"]},
        )
        linked += 1

    post_bulk_activities = list(client.iter_activities())
    rounding_corrections = []
    for reconciliation in plan.get("reconciliation", {}).values():
        account_id = reconciliation["wealthfolioAccountId"]
        cash = activity_cash(post_bulk_activities, account_id)
        expected = Decimal(str(reconciliation["expectedCash"]))
        difference = expected - cash
        if abs(difference) > Decimal("2.00"):
            raise ApplyRefused(
                f"post-import cash differs by {difference}; restore backup"
            )
        if abs(difference) < Decimal("0.01"):
            continue
        kind = "TRANSFER_IN" if difference > 0 else "TRANSFER_OUT"
        rounding_corrections.append(
            {
                "accountId": account_id,
                "activityType": kind,
                "activityDate": (
                    f"{reconciliation['sourceAsOf']}T00:00:00Z"
                ),
                "amount": float(abs(difference)),
                "currency": "USD",
                "isDraft": False,
                "subtype": "external_transfer",
                "metadata": json.dumps({"flow": {"is_external": True}}),
                "comment": (
                    "Vanguard history rounding reconciliation: source cash "
                    "versus quantity times reported prices"
                ),
                "idempotencyKey": (
                    f"vanguard-history:cash-rounding:{account_id}:"
                    f"{reconciliation['sourceAsOf']}"
                ),
            }
        )
    if rounding_corrections:
        correction_result = client.save_activities(creates=rounding_corrections)
        correction_errors = correction_result.get("errors") or []
        if correction_errors or len(correction_result.get("created", [])) != len(
            rounding_corrections
        ):
            raise ApplyRefused(
                "cash rounding reconciliation failed; restore backup"
            )
    final_activities = list(client.iter_activities())
    for reconciliation in plan.get("reconciliation", {}).values():
        account_id = reconciliation["wealthfolioAccountId"]
        actual = activity_cash(final_activities, account_id)
        expected = Decimal(str(reconciliation["expectedCash"]))
        if abs(actual - expected) >= Decimal("0.01"):
            raise ApplyRefused(
                f"final cash differs by {actual - expected}; restore backup"
            )
    client.post("/portfolio/recalculate", {})
    return {
        **expected_counts,
        "linked": linked,
        "roundingCorrections": len(rounding_corrections),
    }
