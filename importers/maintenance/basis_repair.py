"""Plan and apply fingerprint-guarded aggregate cost-basis repairs."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Iterable


class BasisRepairError(RuntimeError):
    pass


FINGERPRINT_FIELDS = (
    "id",
    "accountId",
    "activityType",
    "date",
    "activityDate",
    "assetId",
    "assetSymbol",
    "quantity",
    "unitPrice",
    "amount",
    "idempotencyKey",
    "sourceGroupId",
    "sourceRecordId",
)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def activity_fingerprint(activity: dict[str, Any]) -> str:
    snapshot = {key: activity.get(key) for key in FINGERPRINT_FIELDS}
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_plan_fingerprint(plan: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "planSha256"}
    encoded = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_plan(plan: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(plan)
    sealed["planSha256"] = canonical_plan_fingerprint(sealed)
    return sealed


def verify_plan_integrity(plan: dict[str, Any]) -> str:
    expected = plan.get("planSha256")
    actual = canonical_plan_fingerprint(plan)
    if not expected or expected != actual:
        raise BasisRepairError("plan integrity check failed")
    return actual


def verify_evidence_files(plan: dict[str, Any], data_dir: Path) -> None:
    root = data_dir.resolve()
    for evidence in plan.get("evidence", []):
        path = (root / evidence["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise BasisRepairError("evidence path escapes private data directory") from None
        if not path.is_file():
            raise BasisRepairError(f"evidence file is missing: {evidence['path']}")
        if file_fingerprint(path.read_bytes()) != evidence.get("sha256"):
            raise BasisRepairError(f"evidence hash changed: {evidence['path']}")


def quote_fingerprint(asset_id: str, on: str, quote: dict[str, Any] | None) -> str:
    snapshot = {"assetId": asset_id, "date": on, "quote": quote}
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def exact_unit_price(total_basis: Decimal, quantity: Decimal) -> Decimal:
    if quantity <= 0 or total_basis < 0:
        raise BasisRepairError("basis quantity must be positive and total non-negative")
    with localcontext() as context:
        context.prec = 34
        return total_basis / quantity


def activity_cash_effect(activity: dict[str, Any]) -> Decimal:
    kind = activity.get("activityType")
    if kind == "DEPOSIT":
        return Decimal(str(activity["amount"]))
    if kind == "BUY":
        amount = activity.get("amount")
        cost = (
            Decimal(str(amount))
            if amount not in {None, ""}
            else Decimal(str(activity["quantity"]))
            * Decimal(str(activity["unitPrice"]))
        )
        return -cost
    raise BasisRepairError(f"unsupported cash-effect activity: {kind}")


def _one(rows: Iterable[dict[str, Any]], description: str) -> dict[str, Any]:
    found = list(rows)
    if len(found) != 1:
        raise BasisRepairError(f"expected one {description}; found {len(found)}")
    return found[0]


def build_plan(
    *,
    account: dict[str, Any],
    activities: Iterable[dict[str, Any]],
    targets: dict[str, dict[str, str]],
    funding_key: str,
    expected_cash: Decimal,
    expected_total: Decimal,
    evidence: list[dict[str, str]],
) -> dict[str, Any]:
    """Build updates whose net cash effect is zero.

    ``totalBasis`` is authoritative. Per-share prices are derived at Decimal128
    precision, allowing repeating averages to round back to the exact cent total.
    """
    rows = list(activities)
    buys: list[tuple[dict[str, Any], Decimal, Decimal]] = []
    for symbol, target in sorted(targets.items()):
        activity = _one(
            (
                row
                for row in rows
                if row.get("activityType") == "BUY"
                and row.get("assetSymbol") == symbol
            ),
            f"BUY for {symbol}",
        )
        quantity = Decimal(str(activity.get("quantity")))
        expected_quantity = Decimal(target["quantity"])
        if quantity != expected_quantity:
            raise BasisRepairError(
                f"{symbol} quantity changed: expected {expected_quantity}, found {quantity}"
            )
        total_basis = Decimal(target["totalBasis"])
        buys.append((activity, total_basis, exact_unit_price(total_basis, quantity)))

    funding = _one(
        (row for row in rows if row.get("idempotencyKey") == funding_key),
        f"funding activity {funding_key!r}",
    )
    if funding.get("activityType") != "DEPOSIT":
        raise BasisRepairError("paired funding activity must be a DEPOSIT")

    old_basis = sum(
        (
            Decimal(str(activity["quantity"]))
            * Decimal(str(activity["unitPrice"]))
            for activity, _, _ in buys
        ),
        Decimal("0"),
    )
    new_basis = sum((total for _, total, _ in buys), Decimal("0"))
    old_funding = Decimal(str(funding["amount"]))
    new_funding = old_funding + new_basis - old_basis
    if new_funding < 0:
        raise BasisRepairError("paired funding would become negative")

    operations = []
    for activity, total_basis, unit_price in buys:
        operations.append(
            {
                "kind": "BUY_BASIS",
                "activityId": activity["id"],
                "symbol": activity["assetSymbol"],
                "expectedFingerprint": activity_fingerprint(activity),
                "before": {
                    "quantity": str(activity["quantity"]),
                    "unitPrice": str(activity["unitPrice"]),
                    "date": str(activity.get("date") or activity.get("activityDate")),
                },
                "after": {
                    "quantity": str(activity["quantity"]),
                    "unitPrice": decimal_text(unit_price),
                    "totalBasis": decimal_text(total_basis),
                    "date": str(activity.get("date") or activity.get("activityDate")),
                },
            }
        )
    operations.append(
        {
            "kind": "FUNDING",
            "activityId": funding["id"],
            "expectedFingerprint": activity_fingerprint(funding),
            "before": {
                "amount": str(funding["amount"]),
                "date": str(funding.get("date") or funding.get("activityDate")),
            },
            "after": {
                "amount": decimal_text(new_funding),
                "date": str(funding.get("date") or funding.get("activityDate")),
            },
        }
    )

    return seal_plan({
        "schemaVersion": 1,
        "purpose": "aggregate-unrealized-basis-repair",
        "account": {"id": account["id"], "name": account["name"]},
        "evidence": evidence,
        "guards": {
            "cash": decimal_text(expected_cash),
            "currentTotal": decimal_text(expected_total),
            "oldBasis": decimal_text(old_basis),
            "newBasis": decimal_text(new_basis),
            "netCashEffect": decimal_text(
                (new_funding - old_funding) - (new_basis - old_basis)
            ),
            "datesMustNotChange": True,
        },
        "operations": operations,
    })


def materialize_updates(
    plan: dict[str, Any], current: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Recheck optimistic fingerprints and produce Wealthfolio update payloads."""
    by_id = {row["id"]: row for row in current}
    updates = []
    for operation in plan["operations"]:
        activity = by_id.get(operation["activityId"])
        if activity is None:
            raise BasisRepairError(f"activity disappeared: {operation['activityId']}")
        if activity_fingerprint(activity) != operation["expectedFingerprint"]:
            raise BasisRepairError(f"activity changed: {operation['activityId']}")
        updated = activity_update_payload(activity)
        original_date = str(updated["activityDate"])
        if operation["kind"] == "BUY_BASIS":
            updated["unitPrice"] = operation["after"]["unitPrice"]
        elif operation["kind"] == "FUNDING":
            updated["amount"] = operation["after"]["amount"]
        else:
            raise BasisRepairError(f"unknown operation kind: {operation['kind']}")
        if original_date != operation["after"]["date"]:
            raise BasisRepairError("repair would change an activity date")
        updates.append(updated)
    return updates


def activity_update_payload(activity: dict[str, Any]) -> dict[str, Any]:
    """Convert a search result into the richer shape required by bulk updates."""
    updated = dict(activity)
    updated["activityDate"] = str(
        updated.pop("date", updated.get("activityDate", ""))
    )
    if updated.get("activityType") in {
        "BUY",
        "SELL",
        "DIVIDEND",
        "SPLIT",
        "FEE",
        "TAX",
    }:
        updated["asset"] = {
            "id": updated.get("assetId"),
            "symbol": updated.get("assetSymbol"),
            "name": updated.get("assetName"),
            "kind": "SECURITY",
            "quoteMode": updated.get("assetPricingMode") or "MARKET",
            "quoteCcy": updated.get("currency"),
            "instrumentType": updated.get("instrumentType"),
        }
    return updated


def activity_create_payload(activity: dict[str, Any]) -> dict[str, Any]:
    """Convert an existing activity to a create payload for guarded rollback."""
    updated = activity_update_payload(activity)
    keep = {
        "accountId",
        "activityType",
        "activityDate",
        "asset",
        "quantity",
        "unitPrice",
        "amount",
        "fee",
        "tax",
        "currency",
        "fxRate",
        "status",
        "subtype",
        "metadata",
        "comment",
        "idempotencyKey",
        "sourceSystem",
        "sourceRecordId",
        "sourceGroupId",
        "importRunId",
        "needsReview",
    }
    return {key: value for key, value in updated.items() if key in keep and value is not None}


def build_lot_replacement_plan(
    *,
    account: dict[str, Any],
    activities: Iterable[dict[str, Any]],
    delete_expectations: list[dict[str, str]],
    expected_deleted_cash_effect: Decimal,
    lots: Iterable[dict[str, str]],
    asset: dict[str, Any],
    quote_date: str,
    market_value: Decimal,
    expected_before_total: Decimal,
    expected_shares: Decimal,
    expected_basis: Decimal,
    expected_cash: Decimal,
    evidence: list[dict[str, str]],
    current_quotes: dict[str, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Replace one synthetic position with exact dated contribution lots."""
    rows = list(activities)
    by_id = {row["id"]: row for row in rows}
    delete_ids = [expected["id"] for expected in delete_expectations]
    if len(set(delete_ids)) != len(delete_ids):
        raise BasisRepairError("delete activity ids must be unique")
    delete_rows = []
    for activity_id in delete_ids:
        if activity_id not in by_id:
            raise BasisRepairError(f"delete activity disappeared: {activity_id}")
        delete_rows.append(by_id[activity_id])
    linked = [row["id"] for row in delete_rows if row.get("sourceGroupId")]
    if linked:
        raise BasisRepairError("refusing to delete linked synthetic activities")
    if len(delete_rows) != 3:
        raise BasisRepairError("lot replacement requires exactly three synthetic deletes")
    if sorted(row["activityType"] for row in delete_rows) != [
        "BUY",
        "DEPOSIT",
        "DEPOSIT",
    ]:
        raise BasisRepairError("synthetic deletes must be one BUY and two DEPOSITs")
    expectations = {expected["id"]: expected for expected in delete_expectations}
    for row in delete_rows:
        expected = expectations[row["id"]]
        if row["activityType"] != expected["activityType"]:
            raise BasisRepairError("synthetic delete type does not match target spec")
        for field in ("assetId", "assetSymbol"):
            if field in expected and str(row.get(field) or "") != expected[field]:
                raise BasisRepairError(f"synthetic delete {field} does not match target")
        for field in ("quantity", "unitPrice", "amount"):
            if field in expected and Decimal(str(row.get(field) or "0")) != Decimal(
                expected[field]
            ):
                raise BasisRepairError(f"synthetic delete {field} does not match target")

    deleted_cash_effect = sum(
        (activity_cash_effect(row) for row in delete_rows), Decimal("0")
    )
    if deleted_cash_effect != expected_deleted_cash_effect:
        raise BasisRepairError(
            f"deleted cash effect {deleted_cash_effect} does not match target spec"
        )

    lot_rows = sorted(list(lots), key=lambda row: row["date"])
    if len({row["date"] for row in lot_rows}) != len(lot_rows):
        raise BasisRepairError("contribution dates must be unique")
    quantity_total = sum(
        (Decimal(row["quantity"]) for row in lot_rows), Decimal("0")
    )
    basis_total = sum((Decimal(row["amount"]) for row in lot_rows), Decimal("0"))
    if quantity_total != expected_shares:
        raise BasisRepairError(
            f"lot shares total {quantity_total}, expected {expected_shares}"
        )
    if basis_total != expected_basis:
        raise BasisRepairError(
            f"lot amounts total {basis_total}, expected {expected_basis}"
        )

    creates = []
    for lot in lot_rows:
        on = date.fromisoformat(lot["date"])
        quantity = Decimal(lot["quantity"])
        amount = Decimal(lot["amount"])
        unit_price = exact_unit_price(amount, quantity)
        key = f"basis-lot:{account['id']}:{on.isoformat()}"
        creates.extend(
            [
                {
                    "accountId": account["id"],
                    "activityType": "DEPOSIT",
                    "activityDate": f"{on - timedelta(days=1)}T00:00:00Z",
                    "amount": decimal_text(amount),
                    "currency": "USD",
                    "isDraft": False,
                    "comment": "Employer retirement contribution funding",
                    "idempotencyKey": f"{key}:funding",
                },
                {
                    "accountId": account["id"],
                    "activityType": "BUY",
                    "activityDate": f"{on.isoformat()}T00:00:00Z",
                    "asset": dict(asset),
                    "quantity": decimal_text(quantity),
                    "unitPrice": decimal_text(unit_price),
                    "amount": decimal_text(amount),
                    "currency": "USD",
                    "isDraft": False,
                    "comment": "Employer retirement contribution",
                    "idempotencyKey": f"{key}:buy",
                },
            ]
        )
    replacement_cash_effect = sum(
        (activity_cash_effect(row) for row in creates), Decimal("0")
    )
    if replacement_cash_effect != deleted_cash_effect:
        raise BasisRepairError(
            "replacement activities do not preserve exact deleted cash effect"
        )

    nav = exact_unit_price(market_value, expected_shares)
    first_lot = lot_rows[0]
    anchor_date = date.fromisoformat(first_lot["date"]) - timedelta(days=1)
    anchor_nav = exact_unit_price(
        Decimal(first_lot["amount"]), Decimal(first_lot["quantity"])
    )

    def quote(on: str, value: Decimal) -> dict[str, Any]:
        return {
            "symbol": asset["id"],
            "date": on,
            "open": decimal_text(value),
            "high": decimal_text(value),
            "low": decimal_text(value),
            "close": decimal_text(value),
            "volume": 0,
            "currency": "USD",
            "dataSource": "MANUAL",
        }

    current_quotes = current_quotes or {}
    replacement_quotes = [
        {
            **quote(anchor_date.isoformat(), anchor_nav),
            "purpose": "first-funding-day-anchor-from-first-lot",
            "exactNav": decimal_text(anchor_nav),
        },
        {
            **quote(quote_date, nav),
            "purpose": "current-simplefin-market-value",
            "exactNav": decimal_text(nav),
        },
    ]
    quote_guards = []
    for replacement in replacement_quotes:
        on = replacement["date"]
        previous = current_quotes.get(on)
        quote_guards.append(
            {
                "date": on,
                "expectedFingerprint": quote_fingerprint(asset["id"], on, previous),
                "rollbackQuote": previous,
            }
        )

    return seal_plan({
        "schemaVersion": 1,
        "purpose": "dated-contribution-lot-replacement",
        "account": {"id": account["id"], "name": account["name"]},
        "asset": {"id": asset["id"], "symbol": asset["symbol"]},
        "evidence": evidence,
        "guards": {
            "beforeTotal": decimal_text(expected_before_total),
            "cash": decimal_text(expected_cash),
            "shares": decimal_text(expected_shares),
            "basis": decimal_text(expected_basis),
            "marketValue": decimal_text(market_value),
            "gain": decimal_text(market_value - expected_basis),
            "returnPercent": decimal_text(
                (market_value - expected_basis) / expected_basis
            ),
            "deletedCashEffect": decimal_text(deleted_cash_effect),
            "replacementCashEffect": decimal_text(replacement_cash_effect),
            "datesAreSourceDates": True,
        },
        "deletes": [
            {
                "activityId": row["id"],
                "expectedFingerprint": activity_fingerprint(row),
                "targetExpectation": expectations[row["id"]],
                "rollbackCreate": activity_create_payload(row),
            }
            for row in delete_rows
        ],
        "creates": creates,
        "quotes": replacement_quotes,
        "quoteGuards": quote_guards,
    })


def validate_lot_deletes(
    plan: dict[str, Any], current: Iterable[dict[str, Any]]
) -> list[str]:
    by_id = {row["id"]: row for row in current}
    delete_ids = []
    for operation in plan["deletes"]:
        activity = by_id.get(operation["activityId"])
        if activity is None:
            raise BasisRepairError(f"activity disappeared: {operation['activityId']}")
        if activity.get("sourceGroupId"):
            raise BasisRepairError("refusing to delete a linked activity")
        expected = operation["targetExpectation"]
        if activity.get("activityType") != expected["activityType"]:
            raise BasisRepairError("delete activity no longer matches target type")
        for field in ("assetId", "assetSymbol"):
            if field in expected and str(activity.get(field) or "") != expected[field]:
                raise BasisRepairError("delete activity no longer matches target asset")
        for field in ("quantity", "unitPrice", "amount"):
            if field in expected and Decimal(
                str(activity.get(field) or "0")
            ) != Decimal(expected[field]):
                raise BasisRepairError(
                    f"delete activity no longer matches target {field}"
                )
        if activity_fingerprint(activity) != operation["expectedFingerprint"]:
            raise BasisRepairError(f"activity changed: {operation['activityId']}")
        delete_ids.append(activity["id"])
    current_effect = sum(
        (activity_cash_effect(by_id[activity_id]) for activity_id in delete_ids),
        Decimal("0"),
    )
    if current_effect != Decimal(plan["guards"]["deletedCashEffect"]):
        raise BasisRepairError("delete cash effect changed after planning")
    return delete_ids
