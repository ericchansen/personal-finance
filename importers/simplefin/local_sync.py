"""Sync SimpleFIN into a local Wealthfolio."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import replace
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from importers.simplefin.client import (
    DEFAULT_HISTORY_DAYS,
    MAX_HISTORY_DAYS,
    SimpleFinAccount,
    SimpleFinError,
    SimpleFinTransaction,
    fetch_snapshot,
    load_mapping,
    parse_accounts,
    read_access_url,
)
from importers.simplefin.wealthfolio_client import (
    WealthfolioClient,
    WealthfolioError,
    read_password,
    source_day_timestamp,
)

INFLOWS = {"DEPOSIT", "CREDIT", "INTEREST", "DIVIDEND", "TRANSFER_IN"}
OUTFLOWS = {"WITHDRAWAL", "EXPENSE", "FEE", "TAX", "TRANSFER_OUT"}
TRANSFERS = {"TRANSFER_IN", "TRANSFER_OUT"}


class SnapshotObservationError(ValueError):
    pass


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("a financial amount must be finite")
    return result


def read_accounts(payload: dict, *, balance_zone=timezone.utc) -> tuple[list[SimpleFinAccount], list[str]]:
    """Validate source amounts and units before using the legacy parser."""
    seen = set()
    if not isinstance(payload.get("accounts"), list):
        raise ValueError("SimpleFIN response has no accounts array")
    for account in payload["accounts"]:
        if not account.get("id") or account["id"] in seen:
            raise ValueError("SimpleFIN account ID is missing or repeated")
        seen.add(account["id"])
        money(account["balance"])
        transactions = {}
        for txn in account.get("transactions", []):
            pending = txn.get("pending", False)
            if not isinstance(pending, bool):
                raise ValueError("SimpleFIN transaction pending flag must be boolean")
            if pending:
                continue
            if not txn.get("id"):
                raise ValueError("posted SimpleFIN transaction has no ID")
            money(txn["amount"])
            if int(txn["posted"]) <= 0:
                raise ValueError("posted SimpleFIN transaction has no valid date")
            datetime.fromtimestamp(int(txn["posted"]), timezone.utc)
            if txn["id"] in transactions and transactions[txn["id"]] != txn:
                raise ValueError("SimpleFIN returned conflicting versions of one transaction")
            transactions[txn["id"]] = txn
    accounts, errors = parse_accounts(payload)
    accounts = [
        replace(account, balance_date=datetime.fromtimestamp(int(raw["balance-date"]), balance_zone).date())
        for account, raw in zip(accounts, payload["accounts"])
    ]
    errors.extend(str(error) for error in payload.get("errlist", []))
    return accounts, errors


def activity_amount(row: dict, account_type: str | None = None) -> Decimal:
    amount = money(row.get("amount") or 0)
    if row["activityType"] == "INTEREST" and account_type == "CREDIT_CARD":
        return -abs(amount)
    if row["activityType"] in INFLOWS:
        return amount
    if row["activityType"] in OUTFLOWS:
        return -abs(amount)
    raise ValueError("expected a cash activity")



def source_fields(transaction: SimpleFinTransaction) -> dict:
    return {
        "amount": str(transaction.amount), "date": transaction.posted.isoformat(),
        "description": transaction.description,
        **({"utcDate": transaction.posted_utc_date.isoformat()} if transaction.posted_utc_date else {}),
    }


def posted_in(transaction: SimpleFinTransaction, zone) -> SimpleFinTransaction:
    """Date a timestamped posting by its calendar day in `zone`."""
    if transaction.posted_at is None:
        return transaction
    return replace(transaction, posted=transaction.posted_at.astimezone(zone).date())


def cash_type(transaction: SimpleFinTransaction, account_type: str) -> str:
    text = transaction.description.casefold()
    if account_type == "CREDIT_CARD":
        if transaction.amount < 0:
            return "WITHDRAWAL"
        if re.search(r"\b(autopay|automatic payment|payment\W+(?:received|thank))", text):
            return "TRANSFER_IN"
        return "CREDIT"
    if re.search(r"^(interest (paid|payment|earned)|interest)$", text.strip()):
        return "INTEREST" if transaction.amount >= 0 else "WITHDRAWAL"
    if transaction.amount > 0 and re.search(r"\bvenmo\s+cashout\b", text):
        return "TRANSFER_IN"
    internal = re.search(
        r"\btransfer (?:to|from|.* (?:to|from)) .*(?:account|savings|checking|spending)"
        r"|\b(?:citi|chase|amex|credit card|credit crd) .*?(?:autopay|payment)"
        r"|\bbank p2p\b",
        text,
    )
    if internal:
        return "TRANSFER_IN" if transaction.amount >= 0 else "TRANSFER_OUT"
    return "DEPOSIT" if transaction.amount >= 0 else "WITHDRAWAL"


def metadata(row: dict) -> dict:
    value = row.get("metadata")
    result = json.loads(value) if isinstance(value, str) else value or {}
    if not isinstance(result, dict):
        raise ValueError("activity metadata must be an object")
    return result


def update_payload(row: dict, **changes) -> dict:
    result = {
        key: row.get(key)
        for key in (
            "id", "accountId", "activityType", "subtype", "status", "quantity",
            "unitPrice", "amount", "currency", "fee", "tax", "comment",
            "sourceSystem", "sourceRecordId", "sourceGroupId", "idempotencyKey", "metadata",
        )
    }
    if row.get("assetId"):
        result["asset"] = {"id": row["assetId"]}
    result["activityDate"] = row["date"]
    result.update(changes)
    if isinstance(result.get("metadata"), dict):
        result["metadata"] = json.dumps(result["metadata"])
    return result


def plan_transactions(
    source: SimpleFinAccount,
    entry: dict,
    account: dict,
    activities: list[dict],
    zone,
) -> tuple[list[dict], list[dict]]:
    if "historyThrough" not in entry:
        raise ValueError("cash account mapping needs historyThrough (last day owned by old imports)")
    cutoff = date.fromisoformat(entry["historyThrough"])
    try:
        transaction_zone = ZoneInfo(entry.get("transactionTimezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("transactionTimezone must name a valid IANA timezone") from exc
    aliases = set(entry.get("existingActivities", {}).values())
    owned = (f"simplefin:{source.id}:", f"simplefin:{account['id']}:")
    if any(row["accountId"] == account["id"] and row.get("status") == "POSTED"
           and row["date"][:10] > cutoff.isoformat() and row["id"] not in aliases
           and not (row.get("idempotencyKey") or "").startswith(owned) for row in activities):
        raise ValueError(
            "historical activity overlaps the SimpleFIN-owned interval; verify historyThrough "
            "or map existingActivities before importing"
        )
    by_key = defaultdict(list)
    for row in activities:
        if row.get("idempotencyKey"):
            by_key[row["idempotencyKey"]].append(row)
    by_id = {row["id"]: row for row in activities}
    creates, updates = [], []
    seen = set()
    for txn in source.transactions:
        if txn.pending or txn.id in seen:
            continue
        seen.add(txn.id)
        txn = posted_in(txn, transaction_zone)
        key = f"simplefin:{source.id}:{txn.id}"
        old_key = f"simplefin:{account['id']}:{txn.id}"
        alias = entry.get("existingActivities", {}).get(txn.id)
        matches = {row["id"]: row for match_key in (key, old_key) for row in by_key[match_key]}
        if alias:
            row = by_id.get(alias)
            if row is None:
                raise ValueError("an existingActivities mapping points to a missing activity")
            matches[row["id"]] = row
        if len(matches) > 1:
            raise ValueError("one SimpleFIN transaction resolves to multiple activities; correct its mapping")
        row = next(iter(matches.values()), None)
        if row:
            if row.get("status") != "POSTED":
                raise ValueError("existing source activity is not posted; review its native status")
            if row["accountId"] != account["id"]:
                raise ValueError("SimpleFIN transaction is mapped to another account")
            if row["currency"] != source.currency:
                raise ValueError("existing source activity has a different currency")
            previous = metadata(row).get("simplefin")
            if not previous:
                if activity_amount(row, account["accountType"]) != txn.amount or row["date"][:10] != txn.posted.isoformat():
                    raise ValueError("existing source ID has a different amount or date; correct its mapping")
                continue
            current_source = source_fields(txn)
            kind = cash_type(txn, account["accountType"])
            external = kind in TRANSFERS and not row.get("sourceGroupId")
            if activity_amount(row, account["accountType"]) != money(previous["amount"]) or row["date"][:10] != previous["date"]:
                raise ValueError("a source correction conflicts with a manually edited activity")
            if (
                previous == current_source
                and metadata(row).get("flow", {}).get("is_external", False) == external
                and (kind not in TRANSFERS or row["activityType"] == kind)
            ):
                continue
            meta = {**metadata(row), "simplefin": current_source, "flow": {"is_external": external}}
            changes = {
                "amount": str(abs(txn.amount)),
                "activityDate": source_day_timestamp(txn.posted, zone),
                "activityType": kind,
                "status": "POSTED",
                "metadata": json.dumps(meta),
            }
            if row.get("comment") == previous["description"]:
                changes["comment"] = txn.description
            updates.append(update_payload(row, **changes))
            continue
        if txn.posted <= cutoff:
            continue
        kind = cash_type(txn, account["accountType"])
        creates.append({
            "accountId": account["id"],
            "activityType": kind,
            "status": "POSTED",
            "activityDate": source_day_timestamp(txn.posted, zone),
            "amount": str(abs(txn.amount)),
            "currency": source.currency,
            "comment": txn.description,
            "idempotencyKey": key,
            "metadata": json.dumps({
                "simplefin": source_fields(txn),
                "flow": {"is_external": kind in TRANSFERS},
            }),
        })
    return creates, updates


def is_cash_position(position: dict) -> bool:
    return (
        not str(position.get("symbol") or "").strip()
        and str(position.get("description") or "").strip().upper() == "CASH"
    )


def verified_cost(entry: dict, key: str, quantity: Decimal) -> Decimal | None:
    """Average cost from issuer evidence recorded in the private account map, if it still applies."""
    record = (entry.get("verifiedCosts") or {}).get(key)
    if record is None:
        return None
    if "unitCost" in record:
        return money(record["unitCost"])
    if money(record["quantity"]) == quantity:
        return money(record["costBasis"]) / quantity
    return None


HALF_CENT = Decimal("0.005")


def rolled_cost(rules: dict, key: str, transactions, start: date, end: date,
                prices: list[tuple[date, Decimal]], old_quantity: Decimal, old_basis: Decimal,
                quantity: Decimal) -> Decimal | None:
    """Average-cost basis after one quantity step, or None unless the provider's transactions prove it.

    `rules["transactions"]` maps an exact transaction description to the share of its dollars each
    position receives (`{}` for entries that move no shares); `rules["settlementDays"]` is the
    provider's delay between posting a transaction and reporting the quantity that includes it,
    counted in the same timezone as the snapshot dates.
    The step spans from `start`, when the old quantity was first observed, to the new observation
    on `end`; `prices` are the dated prices observed in between. Any share decrease leaves average
    cost unchanged. A purchase adds its dollars and a sale removes average cost for the shares
    sold. Purchases must bridge: their dollars per share bought must fall within the prices
    observed from posting onward, allowing for rounding, and the unknown exact sale price may move
    the result by less than half a cent.
    """
    lag = timedelta(days=int(rules.get("settlementDays", 0)))
    named = rules.get("transactions", {})
    bought = sold = Decimal(0)
    count, first, unattributed = 0, end, set()
    for txn in transactions:
        if txn.pending or txn.amount == 0 or not start - lag < txn.posted <= end - lag:
            continue
        split = named.get(txn.description)
        if split is None:
            unattributed.add("purchase" if txn.amount > 0 else "sale")
            continue
        part = txn.amount * money(split.get(key, 0))
        if part:
            count, first = count + 1, min(first, txn.posted)
            bought, sold = (bought + part, sold) if part > 0 else (bought, sold - part)
    average = old_basis / old_quantity
    change = quantity - old_quantity
    if "purchase" in unattributed:
        return None
    if change < 0 and not bought:
        return average
    if change <= 0 or not bought or unattributed:
        return None
    observed = [price for day, price in prices if day >= first]
    low, high = min(observed), max(observed)
    places = max(-quantity.as_tuple().exponent, -old_quantity.as_tuple().exponent, 0)
    rounding = Decimal(1).scaleb(-places)
    least, most = sold / high, sold / low
    if average * (most - least) >= HALF_CENT:
        return None
    if not ((bought - HALF_CENT * count) / (change + most + rounding) <= high
            and (bought + HALF_CENT * count) / max(change + least - rounding, rounding) >= low):
        return None
    return (old_basis - average * (least + most) / 2 + bought) / quantity


def plan_holdings(source: SimpleFinAccount, account: dict, current: list[dict], entry: dict | None = None,
                  warnings: list[str] | None = None, window=None):
    """`window(asset_id)` returns when the current quantity was first observed and the prices seen since."""
    entry = entry or {}
    per_unit = entry.get("sourceCostBasis", "total") == "perUnit"
    if source.holdings is None or (not source.holdings and source.balance != 0):
        raise ValueError("source positions are unavailable; existing holdings were not replaced")
    holdings, quotes = [], []
    investment_value = Decimal(0)
    cash = Decimal(0)
    seen = set()
    for position in source.holdings or []:
        if not position.get("id") or position["id"] in seen:
            raise ValueError("source position ID is missing or repeated")
        seen.add(position["id"])
        value, quantity = money(position["market_value"]), money(position["shares"])
        if not quantity and not value:
            continue
        symbol = str(position.get("symbol") or "").strip()
        name = str(position.get("description") or symbol)
        currency = position.get("currency") or source.currency
        if currency != source.currency:
            raise ValueError("investment position currency differs from account currency")
        if is_cash_position(position):
            cash += value
            continue
        if quantity <= 0 or value < 0:
            raise ValueError("unsupported negative/quantity-free investment position")
        # Provider share counts are rounded; one global quote can change another
        # account's reported value. Keep each provider position independently priced.
        asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"simplefin:{source.id}:{position['id']}"))
        scoped = next((row for row in current if (row.get("instrument") or {}).get("id") == asset_id), None)
        average_cost = None
        recorded = verified_cost(entry, symbol or name, quantity)
        retained = money(((scoped or {}).get("costBasis") or {}).get("local") or 0)
        if money(position.get("cost_basis") or 0) > 0:
            average_cost = money(position["cost_basis"]) / (1 if per_unit else quantity)
        elif money(position.get("purchase_price") or 0) > 0:
            average_cost = money(position["purchase_price"])
        elif recorded is not None:
            average_cost = recorded
        elif retained > 0 and money(scoped.get("quantity") or 0) == quantity:
            average_cost = retained / quantity
        elif retained > 0 and "rollForward" in entry and window is not None:
            start, prices = window(asset_id)
            average_cost = start and rolled_cost(
                entry["rollForward"], symbol or name, source.transactions, start, source.balance_date,
                [*prices, (source.balance_date, value / quantity)], money(scoped["quantity"]),
                retained, quantity,
            )
            if average_cost is None and warnings is not None:
                warnings.append(
                    f"{account['name']}: the provider's transactions do not prove the cost of the "
                    f"{symbol or name} quantity change since {start or 'its last observation'}; "
                    "cost is unknown until re-anchored"
                )
        elif (symbol or name) in (entry.get("verifiedCosts") or {}) and warnings is not None:
            warnings.append(
                f"{account['name']}: recorded issuer cost for {symbol or name} covers a different quantity "
                "than the source now reports; refresh it from the issuer"
            )
        holding = {
            "assetId": asset_id,
            "symbol": scoped["instrument"]["symbol"] if scoped else f"{symbol or name} @ {account['name']}",
            "name": name, "quantity": str(quantity), "currency": currency,
            "dataSource": "MANUAL", "assetKind": "INVESTMENT",
        }
        if average_cost is not None:
            holding["averageCost"] = str(average_cost)
        holdings.append(holding)
        quotes.append({
            "symbol": asset_id, "date": source.balance_date.isoformat(),
            "close": str(value / quantity), "currency": currency,
        })
        investment_value += value
    if abs(source.balance - investment_value - cash) > Decimal("0.005"):
        raise ValueError(
            "source positions do not reconcile to the account total; "
            "an unexplained balance is not reported cash"
        )
    return {
        "accountId": account["id"], "snapshotDate": source.balance_date.isoformat(),
        "holdings": holdings, "cashBalances": {source.currency: str(cash.quantize(Decimal("0.01")))},
    }, quotes


def save_changes(client, creates, updates):
    if not creates and not updates:
        return
    result = client.save_activities(creates=creates, updates=updates)
    if result.get("errors"):
        raise ValueError("Wealthfolio rejected activity changes: " + json.dumps(result["errors"]))
    if len(result.get("created", [])) < len(creates) or len(result.get("updated", [])) < len(updates):
        raise ValueError("Wealthfolio did not acknowledge all requested activity changes")


def observed_days(client, account_id, query=""):
    """Dates of source (non-calculated) snapshots."""
    return [row["snapshotDate"] for row in client.get(f"/snapshots?accountId={account_id}{query}")
            if row["source"] != "CALCULATED"]


def kept_newer(client, account, day, what, result) -> bool:
    newest = max(observed_days(client, account["id"]), default="")
    if newest > day:
        result["warnings"].append(f"{account['name']}: kept newer {what} dated {newest}")
    return newest > day


def has_source_snapshot(client, snapshot):
    day = snapshot["snapshotDate"]
    return day in observed_days(client, snapshot["accountId"], f"&dateFrom={day}&dateTo={day}")


def publish_holdings(client, snapshot, quotes):
    client.post("/snapshots", snapshot)
    if not has_source_snapshot(client, snapshot):
        raise SnapshotObservationError(
            "Wealthfolio did not retain the source observation; an unchanged calculated "
            "snapshot may be blocking it. Review the native tracking-mode migration."
        )
    if quotes:
        result = client.post("/market-data/quotes/import", {
            "quotes": quotes, "overwriteExisting": True,
        })
        if len(result) != len(quotes) or any(row.get("validationStatus") != "valid" for row in result):
            raise ValueError("Wealthfolio rejected source position quotes")


def latest_readable_snapshot(client, account_id, tracking_mode):
    if tracking_mode not in {"TRANSACTIONS", "HOLDINGS"}:
        raise ValueError("unsupported native tracking mode")
    today = datetime.now(client.display_timezone()).date().isoformat()
    snapshots = [
        row for row in client.get(f"/snapshots?accountId={account_id}")
        if row.get("isDateValid", True) and row["snapshotDate"] <= today
        and (row["source"] == "CALCULATED") == (tracking_mode == "TRANSACTIONS")
    ]
    return max(snapshots, key=lambda row: row["snapshotDate"], default=None)


def inactive_value_warnings(client, accounts):
    warnings = []
    for account in accounts.values():
        if account["isActive"] or account.get("isArchived", False):
            continue
        if account["accountType"] == "CREDIT_CARD":
            snapshot = latest_readable_snapshot(client, account["id"], account["trackingMode"])
            nonzero = snapshot is not None and money(snapshot["cashTotalAccountCurrency"]) != 0
        else:
            nonzero = any(money(row["marketValue"]["local"]) != 0
                          for row in client.get(f"/holdings?accountId={account['id']}"))
        if nonzero:
            warnings.append(
                f"{account['name']}: inactive status does not exclude this account from Wealthfolio net worth; "
                "a retained nonzero value still needs source review. Do not infer a current zero or account closure."
            )
    return warnings


def verify_balances(client, expected, positions, *, attempts=15, sleeper=time.sleep, repair=True):
    """Wait for native calculations, including the tracking-mode transition."""
    stable = 0
    last_differences = []
    for _ in range(attempts):
        differences = []
        for item in expected:
            account_id = item["accountId"]
            holdings = []
            if item["accountType"] == "CREDIT_CARD":
                snapshot = latest_readable_snapshot(
                    client, account_id, item.get("trackingMode", "TRANSACTIONS"),
                )
                if snapshot is None:
                    item["wealthfolioBalance"] = None
                    differences.append(item["name"])
                    continue
                actual = money(snapshot["cashTotalAccountCurrency"])
            else:
                holdings = client.get(f"/holdings?accountId={account_id}")
                actual = sum(
                    (money(row["marketValue"]["local"]) for row in holdings),
                    Decimal(0),
                )
            item["wealthfolioBalance"] = str(actual)
            shape_matches = True
            if account_id in positions:
                snapshot, quotes = positions[account_id]
                prices = {row["symbol"]: money(row["close"]) for row in quotes}
                wanted = {row["assetId"]: money(row["quantity"]) for row in snapshot["holdings"]}
                present = {
                    row["instrument"]["id"]: row for row in holdings
                    if row.get("holdingType") == "security" and money(row.get("quantity") or 0)
                }
                shape_matches = has_source_snapshot(client, snapshot) and set(present) == set(wanted) and all(
                    abs(money(present[id]["quantity"]) - quantity) <= Decimal("0.00000001")
                    and abs(money(present[id]["marketValue"]["local"]) - quantity * prices[id]) <= Decimal("0.005")
                    for id, quantity in wanted.items()
                )
            if not shape_matches or abs(actual - money(item.get("expectedBalance", item["sourceBalance"]))) > Decimal("0.005"):
                differences.append(item["name"])
                if account_id in positions and repair:
                    # An already-running transaction rebuild can erase the first
                    # manual snapshot during a mode switch. Reapply the same snapshot.
                    publish_holdings(client, *positions[account_id])
        if differences:
            last_differences = differences
        stable = stable + 1 if not differences else 0
        if stable >= 2:
            return
        sleeper(1)
    detail = ", ".join(last_differences) or "two consecutive matching reads were not observed"
    raise ValueError("Wealthfolio values did not settle to source balances: " + detail)


def use_holdings(client, account, snapshot, quotes):
    previous = dict(account)
    changed_mode = previous["trackingMode"] != "HOLDINGS"
    if changed_mode:
        client.update_account(account["id"], **{
            k: v for k, v in {**account, "trackingMode": "HOLDINGS"}.items() if k != "id"
        })
    try:
        publish_holdings(client, snapshot, quotes)
    except SnapshotObservationError:
        if changed_mode:
            client.update_account(previous["id"], **{k: v for k, v in previous.items() if k != "id"})
        raise


def unlink(client, rows, row):
    """An economically corrected transfer must be reviewed and relinked in Wealthfolio."""
    group = row["sourceGroupId"]
    counterpart = next(other for other in rows if other.get("sourceGroupId") == group and other["id"] != row["id"])
    client.post("/activities/unlink", {"activityAId": row["id"], "activityBId": counterpart["id"]})
    for member in rows:
        if member.get("sourceGroupId") == group:
            member["sourceGroupId"] = None
            member["metadata"] = {**metadata(member), "flow": {"is_external": True}}


def sync_liability(client, source, current, dry_run) -> int:
    if current["kind"] != "liability" or source.balance > 0:
        raise ValueError("expected a non-positive source liability balance")
    if current["currency"] != source.currency:
        raise ValueError("source and Wealthfolio liability currencies differ")
    day, valued = source.balance_date.isoformat(), current["valuationDate"][:10]
    if valued > day or (valued == day and money(current["marketValue"]) == abs(source.balance)):
        return 0
    if not dry_run:
        client.put(f"/alternative-assets/{current['id']}/valuation", {
            "value": str(abs(source.balance)), "date": day,
            "notes": "SimpleFIN observed outstanding balance",
        })
    return 1


def sync_cash(client, source, entry, account, rows, zone, result, positions, dry_run):
    """Import real transactions; missing card history must not become an activity."""
    if account["accountType"] == "CREDIT_CARD" and account.get("trackingMode", "TRANSACTIONS") != "TRANSACTIONS":
        raise ValueError("credit-card sync requires native transaction tracking; existing settings and activities were retained")
    if account["accountType"] == "CASH" and any(
        not is_cash_position(position)
        or (position.get("currency") or source.currency) != source.currency
        for position in source.holdings or []
    ):
        raise ValueError(
            "cash-only source reports non-cash or different-currency positions; "
            "review the account mapping"
        )
    day = source.balance_date.isoformat()
    creates, updates = plan_transactions(source, entry, account, rows, zone)
    if not dry_run:
        for update in updates:
            previous = next(row for row in rows if row["id"] == update["id"])
            if previous.get("sourceGroupId") and (
                    previous["activityType"] != update["activityType"]
                    or activity_amount(previous, account["accountType"]) != activity_amount(update, account["accountType"])):
                unlink(client, rows, previous)
            if update.get("sourceGroupId") and not previous.get("sourceGroupId"):
                update["sourceGroupId"] = None
                update["metadata"] = json.dumps({**json.loads(update["metadata"]), "flow": {"is_external": True}})
        writes = {update["id"]: update for update in updates}
        for update in updates:
            if group := update.get("sourceGroupId"):
                counterparts = [row for row in rows
                                if row.get("sourceGroupId") == group and row["id"] != update["id"]]
                if len(counterparts) != 1:
                    raise ValueError("a native transfer must have exactly one counterpart")
                counterpart = counterparts[0]
                # Native one-leg updates mirror dates/notes unless both IDs are explicit.
                writes.setdefault(counterpart["id"], update_payload(counterpart))
        save_changes(client, creates, list(writes.values()))
        for row in rows:
            if row["id"] in writes:
                update = writes[row["id"]]
                row.update(update, date=update["activityDate"])
    result["created"] += len(creates)
    result["updated"] += len(updates)
    if account["accountType"] == "CREDIT_CARD" or account["trackingMode"] == "TRANSACTIONS":
        transaction_day = (source.balance_utc_date or source.balance_date).isoformat()
        replaced = {row["id"] for row in updates}
        projected = [row for row in rows if row["id"] not in replaced]
        projected += [{**row, "date": row["activityDate"]} for row in creates + updates]
        expected = source.balance + sum(
            (activity_amount(row, account["accountType"]) for row in projected
             if row["accountId"] == account["id"] and row.get("status") == "POSTED"
             and (metadata(row).get("simplefin") or {}).get("utcDate", row["date"][:10]) > transaction_day),
            Decimal(0),
        )
        if account["accountType"] == "CASH":
            posted = [row for row in projected
                      if row["accountId"] == account["id"] and row.get("status") == "POSTED"]
            if any(row["currency"] != source.currency or row.get("amount") is None for row in posted):
                raise ValueError("transaction-tracked cash needs a cash-only ledger in the source currency")
            actual = sum((activity_amount(row, "CASH") for row in posted), Decimal(0))
            if abs(actual - expected) > Decimal("0.005"):
                raise ValueError(
                    "transaction-tracked cash history does not reconcile with the source; "
                    "review real funding history or native observation tracking"
                )
        return expected
    if kept_newer(client, account, day, "balance", result):
        return None
    snapshot = {
        "accountId": account["id"], "snapshotDate": day,
        "holdings": [], "cashBalances": {source.currency: str(source.balance)},
    }
    positions[account["id"]] = (snapshot, [])
    if not dry_run:
        use_holdings(client, account, snapshot, [])
    result["balances"] += 1
    return source.balance


def quantity_window(client, account_id, asset_id, quantity):
    """First snapshot date with the asset's current quantity, and its source prices since then."""
    start = None
    for day in sorted(observed_days(client, account_id), reverse=True):
        rows = client.get(f"/snapshots/holdings?accountId={account_id}&date={day}")
        held = next((row for row in rows if (row.get("instrument") or {}).get("id") == asset_id), None)
        if held is None or money(held["quantity"]) != quantity:
            break
        start = day
    if start is None:
        return None, []
    prices = [(date.fromisoformat(row["timestamp"][:10]), money(row["close"]))
              for row in client.get(f"/market-data/quotes/history?symbol={asset_id}")
              if row["timestamp"][:10] >= start]
    return date.fromisoformat(start), prices


def sync_securities(client, source, account, result, positions, dry_run, entry=None):
    if kept_newer(client, account, source.balance_date.isoformat(), "holdings", result):
        return None
    current = client.get(f"/holdings?accountId={account['id']}")
    quantities = {(row.get("instrument") or {}).get("id"): money(row.get("quantity") or 0) for row in current}
    # Balance and snapshot dates are display-zone days; date postings the same way so the
    # roll-forward settlement delay compares like with like.
    zone = client.display_timezone()
    source = replace(source, transactions=[posted_in(txn, zone) for txn in source.transactions])
    snapshot, quotes = plan_holdings(
        source, account, current, entry, result["warnings"],
        lambda asset_id: quantity_window(client, account["id"], asset_id, quantities[asset_id]),
    )
    missing_basis = sum("averageCost" not in holding for holding in snapshot["holdings"])
    if missing_basis:
        result["warnings"].append(
            f"{account['name']}: acquisition cost unavailable for {missing_basis} position(s); "
            "Wealthfolio's zero cost is not verified tax basis, and cost-based gains are unverified"
        )
    positions[account["id"]] = (snapshot, quotes)
    if not dry_run:
        use_holdings(client, account, snapshot, quotes)
    result["positions"] += len(snapshot["holdings"])
    result["balances"] += 1
    return source.balance


def verify_import(client, accounts, result, positions, *, read_only=False):
    unpaired = []
    for row in client.iter_activities():
        if not metadata(row).get("simplefin") or row["activityType"] not in TRANSFERS or row.get("sourceGroupId"):
            continue
        account = accounts.get(row["accountId"])
        if account is None:
            result["warnings"].append("An unlinked source activity belongs to an account outside the current account scope")
        elif account["accountType"] == "CASH":
            unpaired.append(row)
    result["unmatchedTransferCount"] = len(unpaired)
    if unpaired:
        result["warnings"].append(
            f"{len(unpaired)} cash transfer legs lack a counterpart; Wealthfolio includes unlinked legs in cash-flow totals"
        )
    if not read_only:
        client.post("/portfolio/recalculate", {"marketSyncMode": {"type": "incremental"}})
    try:
        if read_only:
            verify_balances(client, result["accounts"], positions, attempts=2, sleeper=lambda _: None, repair=False)
        else:
            verify_balances(client, result["accounts"], positions)
    except ValueError as exc:
        result["errors"].append(str(exc))


def sync(client, sources, mapping, *, dry_run=False, check=False):
    dry_run = dry_run or check
    accounts = {row["id"]: row for row in client.list_accounts()}
    alternatives = {row["id"]: row for row in client.get("/alternative-holdings")}
    rows = list(client.iter_activities())
    zone = client.display_timezone()
    result = {"created": 0, "updated": 0, "balances": 0, "positions": 0, "accounts": [], "warnings": [], "errors": []}
    today = datetime.now(timezone.utc).date()
    positions = {}
    for source in sources:
        entry = mapping.get(source.id)
        if entry is None:
            result["errors"].append(f"Unmapped SimpleFIN account: {source.name}")
            continue
        if entry.get("action") == "exclude" or entry.get("localSync") is False:
            continue
        try:
            if not source.currency.strip():
                raise ValueError("SimpleFIN account currency is missing; refusing to assume USD")
            if not source.is_currency:
                raise ValueError("non-fiat SimpleFIN account requires a unit-specific importer")
            if (today - source.balance_date).days > 3:
                result["warnings"].append(f"{source.name}: balance is dated {source.balance_date}")
            if entry.get("wealthfolioAlternativeAssetId"):
                current = alternatives[entry["wealthfolioAlternativeAssetId"]]
                changed = sync_liability(client, source, current, dry_run)
                result["balances"] += changed
                if dry_run and changed:
                    result["errors"].append(f"{source.name}: saved source requires a native liability valuation update")
                continue
            account = accounts.get(entry.get("wealthfolioAccountId"))
            if account is None:
                raise ValueError("map wealthfolioAccountId to the intended app account")
            if not account["isActive"]:
                continue
            if account["currency"] != source.currency:
                raise ValueError("source and Wealthfolio account currencies differ")
            if entry.get("sourceScope", "account") != "account":
                raise ValueError(
                    "sourceScope is not a full account; additional-card activity cannot stand "
                    "in for the primary billing account"
                )
            if account["accountType"] in {"CASH", "CREDIT_CARD"}:
                expected = sync_cash(client, source, entry, account, rows, zone, result, positions, dry_run)
            elif account["accountType"] == "SECURITIES":
                expected = sync_securities(client, source, account, result, positions, dry_run, entry)
            else:
                raise ValueError("unsupported account type for SimpleFIN sync")
            if expected is None:
                continue
            result["accounts"].append({
                "accountId": account["id"], "name": account["name"],
                "sourceBalance": str(source.balance), "balanceDate": str(source.balance_date),
                "expectedBalance": str(expected),
                "accountType": account["accountType"],
                "trackingMode": "HOLDINGS" if account["id"] in positions else "TRANSACTIONS",
            })
        except (ValueError, KeyError, InvalidOperation, WealthfolioError) as exc:
            result["errors"].append(f"{source.name}: {exc}")
    result["warnings"].extend(inactive_value_warnings(client, accounts))
    if check or not dry_run:
        verify_import(client, accounts, result, positions, read_only=check)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("FINANCE_DATA"), required=not os.environ.get("FINANCE_DATA"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--snapshot", type=Path, help="Replay an existing SimpleFIN JSON response")
    parser.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS,
                        choices=range(1, MAX_HISTORY_DAYS + 1), metavar=f"1..{MAX_HISTORY_DAYS}")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check the last saved source against the app without fetching or changing data")
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve()
    mapping = load_mapping(root)
    last_path = root / "simplefin" / "local-last-sync.json"
    if args.check:
        args.dry_run = True
        if args.snapshot is None:
            args.snapshot = root / json.loads(last_path.read_text(encoding="utf-8"))["snapshotPath"]
    # Fail before spending a SimpleFIN request when Wealthfolio is unavailable.
    client = WealthfolioClient(args.base_url)
    client.login(read_password())
    if args.snapshot:
        snapshot_path = args.snapshot.resolve()
        payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        snapshot_path, payload = fetch_snapshot(root, read_access_url(root), days=args.days)
    if not args.dry_run and last_path.exists():
        previous = json.loads(last_path.read_text(encoding="utf-8")).get("snapshotPath")
        previous = root / previous if previous else None
        if previous and previous.exists() and snapshot_path.stat().st_mtime < previous.stat().st_mtime:
            raise ValueError("snapshot is older than the last sync; use --dry-run to inspect it")
    sources, warnings = read_accounts(payload, balance_zone=client.display_timezone())
    result = sync(client, sources, mapping, dry_run=args.dry_run, check=args.check)
    if args.check and (result["created"] or result["updated"]):
        result["errors"].append("Saved source requires real activity changes; preview and sync before declaring it reconciled")
    result["warnings"] = warnings + result["warnings"]
    # Relative to the data directory, so host and container runs agree.
    result["snapshotPath"] = (
        snapshot_path.relative_to(root).as_posix() if snapshot_path.is_relative_to(root) else str(snapshot_path)
    )
    output = root / "simplefin" / ("local-preview.json" if args.dry_run else "local-last-sync.json")
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "accounts"}))
    print(f"Details: {output}")
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, InvalidOperation, OSError, SimpleFinError, WealthfolioError) as exc:
        print(f"Local sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
