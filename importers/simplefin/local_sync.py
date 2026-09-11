"""SimpleFIN -> local Wealthfolio, without the canonical/release pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from importers.monarch.wealthfolio_client import (
    WealthfolioClient,
    WealthfolioError,
    source_day_timestamp,
)
from importers.simplefin.cli import read_access_url, read_wealthfolio_password
from importers.simplefin.client import SimpleFinAccount, SimpleFinTransaction, parse_accounts
from importers.simplefin.pipeline import fetch_snapshot, load_mapping

INFLOWS = {"DEPOSIT", "CREDIT", "INTEREST", "DIVIDEND", "TRANSFER_IN"}
OUTFLOWS = {"WITHDRAWAL", "EXPENSE", "FEE", "TAX", "TRANSFER_OUT"}


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("a financial amount must be finite")
    return result


def read_accounts(payload: dict) -> tuple[list[SimpleFinAccount], list[str]]:
    """Do not let the legacy parser turn malformed source amounts into zero."""
    seen = set()
    if not isinstance(payload.get("accounts"), list):
        raise ValueError("SimpleFIN response has no accounts array")
    for account in payload["accounts"]:
        if not account.get("id") or account["id"] in seen:
            raise ValueError("SimpleFIN account ID is missing or repeated")
        seen.add(account["id"])
        money(account["balance"])
        datetime.fromtimestamp(int(account["balance-date"]), timezone.utc)
        transactions = {}
        for txn in account.get("transactions", []):
            if txn.get("pending"):
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
    errors.extend(str(error) for error in payload.get("errlist", []))
    return accounts, errors


def activity_amount(row: dict) -> Decimal:
    amount = money(row.get("amount") or 0)
    if row["activityType"] in INFLOWS:
        return amount
    if row["activityType"] in OUTFLOWS:
        return -abs(amount)
    raise ValueError("expected a cash activity")


def cash_type(transaction: SimpleFinTransaction, account_type: str) -> str:
    text = transaction.description.casefold()
    if account_type == "CREDIT_CARD":
        if transaction.amount < 0:
            return "WITHDRAWAL"
        if re.search(r"\b(autopay|automatic payment|payment received|payment thank)", text):
            return "TRANSFER_IN"
        return "CREDIT"
    if re.search(r"^(interest (paid|payment|earned)|interest)$", text.strip()):
        return "INTEREST" if transaction.amount >= 0 else "EXPENSE"
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
            "id", "accountId", "activityType", "subtype", "quantity",
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
    by_key = {row.get("idempotencyKey"): row for row in activities}
    by_id = {row["id"]: row for row in activities}
    creates, updates = [], []
    seen = set()
    for txn in source.transactions:
        if txn.pending or txn.id in seen:
            continue
        seen.add(txn.id)
        key = f"simplefin:{source.id}:{txn.id}"
        old_key = f"simplefin:{account['id']}:{txn.id}"
        alias = entry.get("existingActivities", {}).get(txn.id)
        row = by_key.get(key) or by_key.get(old_key)
        if alias:
            row = by_id.get(alias)
            if row is None:
                raise ValueError("an existingActivities mapping points to a missing activity")
        if row:
            if row["accountId"] != account["id"]:
                raise ValueError("SimpleFIN transaction is mapped to another account")
            previous = metadata(row).get("simplefin")
            if not previous:
                if activity_amount(row) != txn.amount:
                    raise ValueError("existing source ID has a different amount; correct its mapping")
                continue
            current_source = {
                "amount": str(txn.amount), "date": txn.posted.isoformat(),
                "description": txn.description,
            }
            kind = cash_type(txn, account["accountType"])
            external = kind in {"TRANSFER_IN", "TRANSFER_OUT"} and not row.get("sourceGroupId")
            if previous == current_source and metadata(row).get("flow", {}).get("is_external", False) == external:
                continue
            if activity_amount(row) != money(previous["amount"]) or row["date"][:10] != previous["date"]:
                raise ValueError("a source correction conflicts with a manually edited activity")
            meta = {**metadata(row), "simplefin": current_source, "flow": {"is_external": external}}
            changes = {
                "amount": float(abs(txn.amount)),
                "activityDate": source_day_timestamp(txn.posted, zone),
                "activityType": kind,
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
            "activityDate": source_day_timestamp(txn.posted, zone),
            "amount": float(abs(txn.amount)),
            "currency": source.currency,
            "comment": txn.description,
            "idempotencyKey": key,
            "metadata": json.dumps({
                "simplefin": {
                    "amount": str(txn.amount), "date": txn.posted.isoformat(),
                    "description": txn.description,
                },
                "flow": {"is_external": kind in {"TRANSFER_IN", "TRANSFER_OUT"}},
            }),
        })
    return creates, updates


def plan_balance(source: SimpleFinAccount, account: dict, activities: list[dict], zone):
    if account["accountType"] != "CREDIT_CARD":
        raise ValueError("cash balances use native holdings snapshots")
    key = f"simplefin-balance:{source.id}"
    existing = next((row for row in activities if row.get("idempotencyKey") == key), None)
    if existing and existing["date"][:10] > source.balance_date.isoformat():
        return [], []
    balance = sum(
        (activity_amount(row) - money(row.get("fee") or 0) - money(row.get("tax") or 0)
         for row in activities
         if row["accountId"] == account["id"]
         and row.get("idempotencyKey") != key
         and row["date"][:10] <= source.balance_date.isoformat()),
        Decimal(0),
    )
    difference = (source.balance - balance).quantize(Decimal("0.01"))
    if difference < 0:
        raise ValueError("card balance needs missing charges or a historical correction; refusing to invent spending")
    if not difference and not existing:
        return [], []
    fields = {
        "activityType": "TRANSFER_IN",
        "activityDate": source_day_timestamp(source.balance_date, zone),
        "amount": float(abs(difference)),
        "subtype": "external_transfer",
        "comment": "SimpleFIN balance reconciliation (not income or spending)",
        "metadata": json.dumps({"flow": {"is_external": True}}),
    }
    if existing:
        if (activity_amount(existing) == difference
                and existing["date"][:10] == source.balance_date.isoformat()):
            return [], []
        return [], [update_payload(existing, **fields)]
    return [{
        "accountId": account["id"], "currency": source.currency,
        "idempotencyKey": key, **fields,
    }], []


def plan_holdings(source: SimpleFinAccount, account: dict, current: list[dict]):
    holdings, quotes = [], []
    investment_value = Decimal(0)
    for position in source.holdings or []:
        value, quantity = money(position["market_value"]), money(position["shares"])
        if not quantity and not value:
            continue
        symbol = str(position.get("symbol") or "").strip()
        name = str(position.get("description") or symbol)
        if name.upper() == "CASH" and not symbol:
            continue
        if quantity <= 0 or value < 0:
            raise ValueError("unsupported negative/quantity-free investment position")
        currency = position.get("currency") or source.currency
        if currency != source.currency:
            raise ValueError("investment position currency differs from account currency")
        # Provider share counts are rounded; one global quote can change another
        # account's reported value. Keep each provider position independently priced.
        asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"simplefin:{source.id}:{position['id']}"))
        scoped = next((row for row in current if (row.get("instrument") or {}).get("id") == asset_id), None)
        existing = scoped
        if existing is None:
            existing = next((row for row in current if (
                symbol and (row.get("instrument") or {}).get("symbol") == symbol
                or (row.get("instrument") or {}).get("name") == name
            )), None)
        average_cost = None
        if money(position.get("purchase_price") or 0) > 0:
            average_cost = money(position["purchase_price"])
        elif existing and money(existing.get("quantity") or 0) > 0 and existing.get("costBasis"):
            average_cost = money(existing["costBasis"]["local"]) / money(existing["quantity"])
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
    if not source.holdings and source.balance > 0:
        asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "simplefin-total:" + source.id))
        holdings.append({
            "assetId": asset_id, "symbol": "SF-" + asset_id[:8],
            "name": account["name"] + " - reported value (positions unavailable)",
            "quantity": "1", "currency": source.currency, "dataSource": "MANUAL",
            "assetKind": "INVESTMENT",
        })
        quotes.append({
            "symbol": asset_id, "date": source.balance_date.isoformat(),
            "close": str(source.balance), "currency": source.currency,
        })
        investment_value = source.balance
    cash = (source.balance - investment_value).quantize(Decimal("0.01"))
    return {
        "accountId": account["id"], "snapshotDate": source.balance_date.isoformat(),
        "holdings": holdings, "cashBalances": {source.currency: str(cash)},
    }, quotes


def save_changes(client, creates, updates):
    if not creates and not updates:
        return
    result = client.save_activities(creates=creates, updates=updates)
    if result.get("errors"):
        raise ValueError("Wealthfolio rejected activity changes: " + json.dumps(result["errors"]))
    if len(result.get("created", [])) < len(creates) or len(result.get("updated", [])) < len(updates):
        raise ValueError("Wealthfolio did not acknowledge all requested activity changes")


def publish_holdings(client, snapshot, quotes):
    client.post("/snapshots", snapshot)
    if quotes:
        result = client.post("/market-data/quotes/import", {
            "quotes": quotes, "overwriteExisting": True,
        })
        if len(result) != len(quotes) or any(row.get("validationStatus") != "valid" for row in result):
            raise ValueError("Wealthfolio rejected source position quotes")


def routed_to(left, right):
    suffixes = set(re.findall(r"\baccount\s+[xX*]*(\d{4})\b", left.get("comment") or ""))
    own = set(re.findall(r"\b\d{4}\b", left.get("accountName") or ""))
    suffixes -= own
    if not suffixes:
        return None
    return bool(suffixes & set(re.findall(r"\b\d{4}\b", right.get("accountName") or "")))


def transfer_pairs(rows):
    candidates = [
        row for row in rows if metadata(row).get("simplefin")
        and row["activityType"] in {"TRANSFER_IN", "TRANSFER_OUT"}
        and not row.get("sourceGroupId")
    ]
    matches = {}
    for left in candidates:
        choices = []
        for right in candidates:
            days = abs((date.fromisoformat(left["date"][:10]) - date.fromisoformat(right["date"][:10])).days)
            routing = (routed_to(left, right), routed_to(right, left))
            if (left["accountId"] != right["accountId"] and left["currency"] == right["currency"]
                    and left["activityType"] != right["activityType"]
                    and activity_amount(left) == -activity_amount(right) and days <= 5
                    and False not in routing and (days == 0 or True in routing)):
                choices.append((days, right["id"]))
        if choices:
            nearest = min(days for days, _ in choices)
            closest = [id for days, id in choices if days == nearest]
            if len(closest) == 1:
                matches[left["id"]] = closest[0]
    return [
        (left, right) for left, right in matches.items()
        if left < right and matches.get(right) == left
    ]


def verify_balances(client, expected, positions, *, attempts=15, sleeper=time.sleep):
    """Wait for native calculations, including the tracking-mode transition."""
    stable = 0
    differences = []
    for _ in range(attempts):
        differences = []
        for item in expected:
            account_id = item["accountId"]
            holdings = []
            if item["accountType"] == "CREDIT_CARD":
                snapshots = client.get(f"/snapshots?accountId={account_id}")
                actual = money(max(snapshots, key=lambda x: x["snapshotDate"])["cashTotalAccountCurrency"]) if snapshots else Decimal(0)
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
                shape_matches = set(present) == set(wanted) and all(
                    abs(money(present[id]["quantity"]) - quantity) <= Decimal("0.00000001")
                    and abs(money(present[id]["marketValue"]["local"]) - quantity * prices[id]) <= Decimal("0.005")
                    for id, quantity in wanted.items()
                )
            if not shape_matches or abs(actual - money(item.get("expectedBalance", item["sourceBalance"]))) > Decimal("0.005"):
                differences.append(item["name"])
                if account_id in positions:
                    # An already-running transaction rebuild can erase the first
                    # manual snapshot during a mode switch. Reapply the same snapshot.
                    publish_holdings(client, *positions[account_id])
        stable = stable + 1 if not differences else 0
        if stable >= 2:
            return
        sleeper(1)
    raise ValueError("Wealthfolio values did not settle to source balances: " + ", ".join(differences))


def sync(client, sources, mapping, *, dry_run=False):
    accounts = {row["id"]: row for row in client.list_accounts()}
    alternatives = {row["id"]: row for row in client.get("/alternative-holdings")}
    rows = list(client.iter_activities())
    zone = client.display_timezone()
    result = {"created": 0, "updated": 0, "balances": 0, "positions": 0, "linkedTransfers": 0, "accounts": [], "warnings": [], "errors": []}
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
            if not source.is_currency:
                raise ValueError("non-fiat SimpleFIN account requires a unit-specific importer")
            if source.balance_date is None:
                raise ValueError("source balance has no observation date")
            if (today - source.balance_date).days > 3:
                result["warnings"].append(f"{source.name}: balance is dated {source.balance_date}")
            alt_id = entry.get("wealthfolioAlternativeAssetId")
            if alt_id:
                current = alternatives[alt_id]
                if current["kind"] != "liability" or source.balance > 0:
                    raise ValueError("expected a non-positive source liability balance")
                if current["currency"] != source.currency:
                    raise ValueError("source and Wealthfolio liability currencies differ")
                if current["valuationDate"][:10] <= source.balance_date.isoformat() and (
                    money(current["marketValue"]) != abs(source.balance)
                    or current["valuationDate"][:10] != source.balance_date.isoformat()
                ):
                    if not dry_run:
                        client.put(f"/alternative-assets/{alt_id}/valuation", {
                            "value": str(abs(source.balance)), "date": source.balance_date.isoformat(),
                            "notes": "SimpleFIN observed outstanding balance",
                        })
                    result["balances"] += 1
                continue
            account = accounts.get(entry.get("wealthfolioAccountId"))
            if account is None:
                raise ValueError("map wealthfolioAccountId to the intended app account")
            if not account["isActive"]:
                continue
            if account["currency"] != source.currency:
                raise ValueError("source and Wealthfolio account currencies differ")
            expected_balance = source.balance
            if account["accountType"] in {"CASH", "CREDIT_CARD"}:
                balance_row = next((row for row in rows if row.get("idempotencyKey") == f"simplefin-balance:{source.id}"), None)
                if balance_row and balance_row["date"][:10] > source.balance_date.isoformat():
                    result["warnings"].append(f"{account['name']}: kept newer balance dated {balance_row['date'][:10]}")
                    continue
                creates, updates = plan_transactions(source, entry, account, rows, zone)
                if not dry_run:
                    for update in updates:
                        group = update.get("sourceGroupId")
                        if group:
                            counterpart = next(row for row in rows if row.get("sourceGroupId") == group and row["id"] != update["id"])
                            client.post("/activities/unlink", {"activityAId": update["id"], "activityBId": counterpart["id"]})
                            for member in rows:
                                if member.get("sourceGroupId") == group:
                                    member["sourceGroupId"] = None
                                    member["metadata"] = {**metadata(member), "flow": {"is_external": True}}
                            update["sourceGroupId"] = None
                            update["metadata"] = json.dumps({**json.loads(update["metadata"]), "flow": {"is_external": True}})
                    save_changes(client, creates, updates)
                result["created"] += len(creates)
                result["updated"] += len(updates)
                projected = [row for row in rows if row["id"] not in {x["id"] for x in updates}]
                projected += [{**row, "date": row["activityDate"]} for row in creates + updates]
                expected_balance += sum(
                    (activity_amount(row) - money(row.get("fee") or 0) - money(row.get("tax") or 0)
                     for row in projected if row["accountId"] == account["id"]
                     and row["date"][:10] > source.balance_date.isoformat()),
                    Decimal(0),
                )
                if account["accountType"] == "CASH":
                    snapshot = {
                        "accountId": account["id"], "snapshotDate": source.balance_date.isoformat(),
                        "holdings": [], "cashBalances": {source.currency: str(source.balance)},
                    }
                    expected_balance = source.balance
                    if not dry_run:
                        if account["trackingMode"] != "HOLDINGS":
                            client.update_account(account["id"], **{
                                k: v for k, v in {**account, "trackingMode": "HOLDINGS"}.items() if k != "id"
                            })
                        publish_holdings(client, snapshot, [])
                        positions[account["id"]] = (snapshot, [])
                    result["balances"] += 1
                else:
                    balance_creates, balance_updates = plan_balance(source, account, projected, zone)
                    if not dry_run:
                        save_changes(client, balance_creates, balance_updates)
                    result["balances"] += len(balance_creates) + len(balance_updates)
            elif account["accountType"] == "SECURITIES":
                snapshots = client.get(f"/snapshots?accountId={account['id']}")
                newest = max((row["snapshotDate"] for row in snapshots if row["source"] == "MANUAL_ENTRY"), default="")
                if newest > source.balance_date.isoformat():
                    result["warnings"].append(f"{account['name']}: kept newer holdings dated {newest}")
                    continue
                current = client.get(f"/holdings?accountId={account['id']}")
                snapshot, quotes = plan_holdings(source, account, current)
                if not dry_run:
                    if account["trackingMode"] != "HOLDINGS":
                        client.update_account(account["id"], **{
                            k: v for k, v in {**account, "trackingMode": "HOLDINGS"}.items() if k != "id"
                        })
                    publish_holdings(client, snapshot, quotes)
                    positions[account["id"]] = (snapshot, quotes)
                result["positions"] += len(snapshot["holdings"])
                result["balances"] += 1
                if not source.holdings and source.balance:
                    result["warnings"].append(f"{account['name']}: source supplies a total but no positions")
            else:
                raise ValueError("unsupported account type for SimpleFIN sync")
            result["accounts"].append({
                "accountId": account["id"], "name": account["name"],
                "sourceBalance": str(source.balance), "balanceDate": str(source.balance_date),
                "expectedBalance": str(expected_balance),
                "accountType": account["accountType"],
            })
        except (ValueError, KeyError, InvalidOperation, WealthfolioError) as exc:
            result["errors"].append(f"{source.name}: {exc}")
    if not dry_run:
        for left, right in transfer_pairs(list(client.iter_activities())):
            client.post("/activities/link", {"activityAId": left, "activityBId": right})
            result["linkedTransfers"] += 1
        unpaired = [
            row for row in client.iter_activities()
            if metadata(row).get("simplefin")
            and row["activityType"] in {"TRANSFER_IN", "TRANSFER_OUT"}
            and not row.get("sourceGroupId")
            and accounts[row["accountId"]]["accountType"] == "CASH"
        ]
        result["unmatchedTransferCount"] = len(unpaired)
        if unpaired:
            result["warnings"].append(
                f"{len(unpaired)} cash transfer legs lack a counterpart; Wealthfolio includes unlinked legs in cash-flow totals"
            )
        client.post("/portfolio/recalculate", {"marketSyncMode": {"type": "incremental", "asset_ids": []}})
        try:
            verify_balances(client, result["accounts"], positions)
        except ValueError as exc:
            result["errors"].append(str(exc))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("FINANCE_DATA"), required=not os.environ.get("FINANCE_DATA"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--snapshot", type=Path, help="Replay an existing SimpleFIN JSON response")
    parser.add_argument("--days", type=int, default=45, choices=range(1, 91), metavar="1..90")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve()
    mapping = load_mapping(root)
    if args.snapshot:
        snapshot_path = args.snapshot.resolve()
        payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        snapshot_path, payload = fetch_snapshot(root, read_access_url(root), days=args.days)
    last_path = root / "simplefin" / "local-last-sync.json"
    if not args.dry_run and last_path.exists():
        previous = json.loads(last_path.read_text(encoding="utf-8")).get("snapshotPath")
        if previous and Path(previous).exists() and snapshot_path.stat().st_mtime < Path(previous).stat().st_mtime:
            raise ValueError("snapshot is older than the last sync; use --dry-run to inspect it")
    sources, warnings = read_accounts(payload)
    client = WealthfolioClient(args.base_url, local_sync=True)
    client.login(read_wealthfolio_password(root))
    result = sync(client, sources, mapping, dry_run=args.dry_run)
    result["warnings"] = warnings + result["warnings"]
    result["snapshotPath"] = str(snapshot_path)
    output = root / "simplefin" / ("local-preview.json" if args.dry_run else "local-last-sync.json")
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "accounts"}))
    print(f"Details: {output}")
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, InvalidOperation, OSError) as exc:
        print(f"Local sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
