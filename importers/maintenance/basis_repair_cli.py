"""Create or apply a private, fingerprinted Wealthfolio basis-repair plan."""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extracts"))

try:
    from .basis_repair import (
        BasisRepairError,
        FINGERPRINT_FIELDS,
        activity_fingerprint,
        activity_update_payload,
        build_lot_replacement_plan,
        build_plan,
        file_fingerprint,
        materialize_updates,
        quote_fingerprint,
        validate_lot_deletes,
        verify_evidence_files,
        verify_plan_integrity,
    )
except ImportError:  # pragma: no cover - direct CLI execution
    from basis_repair import (  # type: ignore
        BasisRepairError,
        FINGERPRINT_FIELDS,
        activity_fingerprint,
        activity_update_payload,
        build_lot_replacement_plan,
        build_plan,
        file_fingerprint,
        materialize_updates,
        quote_fingerprint,
        validate_lot_deletes,
        verify_evidence_files,
        verify_plan_integrity,
    )
from fidelity import parse_transactions  # noqa: E402
from wealthfolio_client import WealthfolioClient, WealthfolioError  # noqa: E402

API_ERRORS = (
    WealthfolioError,
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    ConnectionError,
    json.JSONDecodeError,
)


def password(data_dir: Path) -> str:
    value = os.environ.get("WEALTHFOLIO_PASSWORD")
    if value:
        return value
    password_file = data_dir / "wealthfolio" / "ADMIN-PASSWORD.txt"
    if password_file.exists():
        for line in password_file.read_text(encoding="utf-8").splitlines():
            candidate = line.strip()
            if candidate and " " not in candidate and not candidate.startswith(
                ("Wealthfolio", "Move", "Only", "generated")
            ):
                return candidate
    return getpass.getpass("Wealthfolio password: ")


def account_state(client: WealthfolioClient, account_id: str) -> dict:
    holdings = client.get(f"/holdings?accountId={account_id}") or []
    performance = client.post(
        "/performance/accounts/simple", {"accountIds": [account_id]}
    ) or []
    summary = next(
        (row for row in performance if row.get("accountId") == account_id), None
    )
    if summary is None:
        raise BasisRepairError("account performance is unavailable")
    cash = sum(
        (
            Decimal(str(row["marketValue"]["local"]))
            for row in holdings
            if row.get("holdingType") == "cash"
        ),
        Decimal("0"),
    )
    securities = {
        row["instrument"]["symbol"]: {
            "quantity": Decimal(str(row["quantity"])),
            "costBasis": Decimal(str(row["costBasis"]["local"])),
            "unrealizedGain": Decimal(str(row["unrealizedGain"]["local"])),
        }
        for row in holdings
        if row.get("holdingType") == "security"
    }
    return {
        "cash": cash,
        "total": Decimal(str(summary["totalValue"])),
        "gain": Decimal(str(summary["totalGainLossAmount"])),
        "returnPercent": Decimal(str(summary["cumulativeReturnPercent"])),
        "securities": securities,
    }


def assert_close(label: str, actual: Decimal, expected: Decimal) -> None:
    if abs(actual - expected) > Decimal("0.01"):
        raise BasisRepairError(f"{label}: expected {expected}, found {actual}")


def verify_state(plan: dict, state: dict, *, repaired: bool) -> None:
    assert_close("cash", state["cash"], Decimal(plan["guards"]["cash"]))
    assert_close("current total", state["total"], Decimal(plan["guards"]["currentTotal"]))
    for operation in plan["operations"]:
        if operation["kind"] != "BUY_BASIS":
            continue
        holding = state["securities"].get(operation["symbol"])
        if holding is None:
            raise BasisRepairError(f"holding disappeared: {operation['symbol']}")
        if holding["quantity"] != Decimal(operation["after"]["quantity"]):
            raise BasisRepairError(f"quantity changed: {operation['symbol']}")
        if repaired:
            assert_close(
                f"{operation['symbol']} basis",
                holding["costBasis"],
                Decimal(operation["after"]["totalBasis"]),
            )


def wait_for_repaired_state(
    client: WealthfolioClient, plan: dict, attempts: int = 20
) -> dict:
    """Wait for Wealthfolio's asynchronously refreshed holding cache."""
    error: BasisRepairError | None = None
    for _ in range(attempts):
        state = account_state(client, plan["account"]["id"])
        try:
            verify_state(plan, state, repaired=True)
            return state
        except BasisRepairError as exc:
            error = exc
            time.sleep(0.5)
    raise error or BasisRepairError("repaired state did not become available")


def verify_lot_state(plan: dict, state: dict, *, repaired: bool) -> None:
    guards = plan["guards"]
    assert_close("cash", state["cash"], Decimal(guards["cash"]))
    holding = state["securities"].get(plan["asset"]["symbol"])
    if holding is None:
        raise BasisRepairError(f"holding disappeared: {plan['asset']['symbol']}")
    if holding["quantity"] != Decimal(guards["shares"]):
        raise BasisRepairError("lot replacement changed aggregate shares")
    if repaired:
        assert_close("basis", holding["costBasis"], Decimal(guards["basis"]))
        assert_close("current total", state["total"], Decimal(guards["marketValue"]))
        assert_close("gain", state["gain"], Decimal(guards["gain"]))
        if abs(state["returnPercent"] - Decimal(guards["returnPercent"])) > Decimal(
            "0.0001"
        ):
            raise BasisRepairError("return percent does not match contribution basis")
    else:
        assert_close(
            "pre-repair current total", state["total"], Decimal(guards["beforeTotal"])
        )


def wait_for_lot_state(client: WealthfolioClient, plan: dict, attempts: int = 30) -> dict:
    error: BasisRepairError | None = None
    for _ in range(attempts):
        state = account_state(client, plan["account"]["id"])
        try:
            verify_lot_state(plan, state, repaired=True)
            return state
        except BasisRepairError as exc:
            error = exc
            time.sleep(0.5)
    raise error or BasisRepairError("lot state did not become available")


def wait_for_original_state(
    client: WealthfolioClient, plan: dict, verifier, attempts: int = 30
) -> dict:
    error: BasisRepairError | None = None
    for _ in range(attempts):
        state = account_state(client, plan["account"]["id"])
        try:
            verifier(plan, state, repaired=False)
            return state
        except BasisRepairError as exc:
            error = exc
            time.sleep(0.5)
    raise error or BasisRepairError("original state was not restored")


def _result_ids(rows: list) -> set[str]:
    return {
        str(row.get("id") if isinstance(row, dict) else row)
        for row in rows
    }


def account_activities(client: WealthfolioClient, account_id: str) -> list[dict]:
    return [
        row for row in client.iter_activities() if row.get("accountId") == account_id
    ]


def _same_fingerprint_value(field: str, actual, expected) -> bool:
    if actual in {None, ""} and expected in {None, ""}:
        return True
    if field in {"date", "activityDate"}:
        return str(actual).replace("+00:00", "Z") == str(expected).replace(
            "+00:00", "Z"
        )
    if field in {"quantity", "unitPrice", "amount"}:
        if actual in {None, ""} or expected in {None, ""}:
            return actual in {None, ""} and expected in {None, ""}
        return Decimal(str(actual)) == Decimal(str(expected))
    return actual == expected


def _matches_created_activity(actual: dict, expected: dict) -> bool:
    expected_fields = {
        "accountId": expected.get("accountId"),
        "activityType": expected.get("activityType"),
        "date": expected.get("activityDate"),
        "activityDate": expected.get("activityDate"),
        "assetId": (expected.get("asset") or {}).get("id"),
        "assetSymbol": (expected.get("asset") or {}).get("symbol"),
        "quantity": expected.get("quantity"),
        "unitPrice": expected.get("unitPrice"),
        "amount": expected.get("amount"),
        "idempotencyKey": expected.get("idempotencyKey"),
        "sourceGroupId": expected.get("sourceGroupId"),
        "sourceRecordId": expected.get("sourceRecordId"),
    }
    for field in FINGERPRINT_FIELDS:
        if field == "id":
            continue
        wanted = expected_fields.get(field)
        if field == "date" and actual.get("date") is None:
            continue
        if field == "activityDate" and actual.get("activityDate") is None:
            continue
        if not _same_fingerprint_value(field, actual.get(field), wanted):
            return False
    return True


def verify_original_activities(
    plan: dict,
    originals: list[dict],
    live: list[dict],
    *,
    reject_create_keys: bool,
) -> None:
    by_id = {row["id"]: row for row in live}
    expected = {
        operation["activityId"]: operation["expectedFingerprint"]
        for operation in (
            plan["deletes"] if plan["purpose"] == "dated-contribution-lot-replacement"
            else plan["operations"]
        )
    }
    original_ids = {row["id"] for row in originals}
    for activity_id, fingerprint in expected.items():
        if activity_id not in original_ids:
            raise BasisRepairError("rollback verification lacks an original activity")
        row = by_id.get(activity_id)
        if row is None or activity_fingerprint(row) != fingerprint:
            raise BasisRepairError(
                f"original activity fingerprint was not restored: {activity_id}"
            )
    if reject_create_keys:
        create_keys = {row["idempotencyKey"] for row in plan["creates"]}
        if any(row.get("idempotencyKey") in create_keys for row in live):
            raise BasisRepairError("replacement activity remains after rollback")


def rollback_ambiguous_lot_mutation(
    client: WealthfolioClient,
    plan: dict,
    originals: list[dict],
    rollback_verify,
) -> None:
    live = account_activities(client, plan["account"]["id"])
    by_key: dict[str, list[dict]] = {}
    for row in live:
        if row.get("idempotencyKey"):
            by_key.setdefault(row["idempotencyKey"], []).append(row)
    created_ids: set[str] = set()
    for expected in plan["creates"]:
        matches = by_key.get(expected["idempotencyKey"], [])
        if len(matches) > 1 or (matches and not _matches_created_activity(matches[0], expected)):
            raise BasisRepairError("ambiguous replacement activity state")
        if matches:
            created_ids.add(matches[0]["id"])
    live_ids = {row["id"] for row in live}
    delete_ids = {
        operation["activityId"]
        for operation in plan["deletes"]
        if operation["activityId"] not in live_ids
    }
    if created_ids or delete_ids:
        rollback_lot_bulk(client, plan, created_ids, delete_ids)
    restored = account_activities(client, plan["account"]["id"])
    verify_original_activities(plan, originals, restored, reject_create_keys=True)
    if rollback_verify:
        rollback_verify()


def rollback_ambiguous_aggregate_mutation(
    client: WealthfolioClient,
    plan: dict,
    originals: list[dict],
    rollback_verify,
) -> None:
    live = account_activities(client, plan["account"]["id"])
    by_id = {row["id"]: row for row in live}
    original_by_id = {row["id"]: row for row in originals}
    updated_ids: set[str] = set()
    for operation in plan["operations"]:
        activity_id = operation["activityId"]
        row = by_id.get(activity_id)
        original = original_by_id.get(activity_id)
        if row is None or original is None:
            raise BasisRepairError("ambiguous aggregate activity state")
        if activity_fingerprint(row) == operation["expectedFingerprint"]:
            continue
        changed_field = "unitPrice" if operation["kind"] == "BUY_BASIS" else "amount"
        target = operation["after"][changed_field]
        if not _same_fingerprint_value(changed_field, row.get(changed_field), target):
            raise BasisRepairError("aggregate activity has an unexpected value")
        for field in FINGERPRINT_FIELDS:
            if field == changed_field:
                continue
            if not _same_fingerprint_value(field, row.get(field), original.get(field)):
                raise BasisRepairError("aggregate activity changed unexpectedly")
        updated_ids.add(activity_id)
    if updated_ids:
        rollback_aggregate_updates(client, originals, updated_ids)
    restored = account_activities(client, plan["account"]["id"])
    verify_original_activities(plan, originals, restored, reject_create_keys=False)
    if rollback_verify:
        rollback_verify()


def _quote_payload(row: dict | None) -> dict | None:
    if row is None:
        return None
    on = str(row.get("date") or row.get("day") or row.get("timestamp") or "")[:10]
    if not on:
        raise BasisRepairError("quote history row has no date")
    return {
        "_id": row.get("id"),
        "symbol": str(row.get("symbol") or row.get("assetId") or ""),
        "date": on,
        "open": str(row.get("open")),
        "high": str(row.get("high")),
        "low": str(row.get("low")),
        "close": str(row.get("close")),
        "volume": row.get("volume") or 0,
        "currency": row.get("currency") or "USD",
        "dataSource": row.get("dataSource") or row.get("source") or "MANUAL",
    }


def quote_states(
    client: WealthfolioClient, asset_id: str, dates: set[str]
) -> dict[str, dict | None]:
    path = "/market-data/quotes/history?symbol=" + urllib.parse.quote(asset_id)
    response = client.get(path) or []
    rows = response.get("data", []) if isinstance(response, dict) else response
    found = {}
    for row in rows:
        normalized = _quote_payload(row)
        if normalized and normalized["date"] in dates:
            normalized["symbol"] = asset_id
            found[normalized["date"]] = normalized
    return {on: found.get(on) for on in dates}


def validate_quote_guards(
    client: WealthfolioClient, plan: dict
) -> dict[str, dict | None]:
    dates = {guard["date"] for guard in plan["quoteGuards"]}
    current = quote_states(client, plan["asset"]["id"], dates)
    for guard in plan["quoteGuards"]:
        actual = quote_fingerprint(
            plan["asset"]["id"], guard["date"], current[guard["date"]]
        )
        if actual != guard["expectedFingerprint"]:
            raise BasisRepairError(f"quote changed after planning: {guard['date']}")
    return current


def _public_quote(quote: dict) -> dict:
    return {
        key: value
        for key, value in quote.items()
        if key not in {"_id", "purpose", "exactNav"}
    }


def restore_quotes(
    client: WealthfolioClient,
    plan: dict,
    current: dict[str, dict | None],
) -> None:
    restore = [
        _public_quote(guard["rollbackQuote"])
        for guard in plan["quoteGuards"]
        if guard["rollbackQuote"] is not None
    ]
    if restore:
        result = client.post(
            "/market-data/quotes/import",
            {"quotes": restore, "overwriteExisting": True},
        )
        if isinstance(result, dict) and result.get("errors"):
            raise BasisRepairError(f"quote rollback rejected: {result['errors']}")
    after = quote_states(client, plan["asset"]["id"], set(current))
    for guard in plan["quoteGuards"]:
        if guard["rollbackQuote"] is None:
            added = after[guard["date"]]
            if added and added.get("_id"):
                client._request(
                    "DELETE",
                    f"/market-data/quotes/id/{urllib.parse.quote(str(added['_id']))}",
                )
    restored = quote_states(client, plan["asset"]["id"], set(current))
    for guard in plan["quoteGuards"]:
        actual = quote_fingerprint(
            plan["asset"]["id"], guard["date"], restored[guard["date"]]
        )
        if actual != guard["expectedFingerprint"]:
            raise BasisRepairError(
                f"quote rollback could not be verified: {guard['date']}"
            )


def rollback_lot_bulk(
    client: WealthfolioClient,
    plan: dict,
    created_ids: set[str],
    deleted_ids: set[str],
) -> None:
    originals = {
        operation["activityId"]: operation["rollbackCreate"]
        for operation in plan["deletes"]
    }
    rollback = client.save_activities(
        creates=[originals[activity_id] for activity_id in sorted(deleted_ids)],
        delete_ids=sorted(created_ids),
    )
    restored = len(rollback.get("created", []))
    removed = len(rollback.get("deleted", []))
    if (
        rollback.get("errors")
        or restored != len(deleted_ids)
        or removed != len(created_ids)
    ):
        raise BasisRepairError(
            "automatic activity rollback incomplete; restore the database backup"
        )


def apply_lot_plan(
    client: WealthfolioClient,
    plan: dict,
    activities: list[dict],
    *,
    post_verify=None,
    rollback_verify=None,
):
    delete_ids = validate_lot_deletes(plan, activities)
    previous_quotes = validate_quote_guards(client, plan)
    client.backup_database()
    try:
        result = client.save_activities(creates=plan["creates"], delete_ids=delete_ids)
    except API_ERRORS as exc:
        try:
            rollback_ambiguous_lot_mutation(
                client, plan, activities, rollback_verify
            )
        except API_ERRORS + (BasisRepairError,):
            raise BasisRepairError(
                "lot mutation transport failed with ambiguous state; "
                "database backup restore required"
            ) from None
        raise BasisRepairError(
            f"lot mutation transport failed after possible commit; "
            f"live changes were rolled back and verified: {exc}"
        ) from None
    created_ids = _result_ids(result.get("created", []))
    deleted_ids = _result_ids(result.get("deleted", []))
    if (
        result.get("errors")
        or len(created_ids) != len(plan["creates"])
        or len(deleted_ids) != len(delete_ids)
    ):
        rollback_lot_bulk(client, plan, created_ids, deleted_ids)
        raise BasisRepairError(
            "atomic lot replacement was incomplete; all reported partial changes "
            "were rolled back"
        )
    quote_error = None
    try:
        quote_result = client.post(
            "/market-data/quotes/import",
            {
                "quotes": [_public_quote(quote) for quote in plan["quotes"]],
                "overwriteExisting": True,
            },
        )
        if isinstance(quote_result, dict) and quote_result.get("errors"):
            quote_error = str(quote_result["errors"])
    except API_ERRORS as exc:
        quote_error = str(exc)
    if quote_error is not None:
        rollback_lot_bulk(client, plan, created_ids, set(delete_ids))
        try:
            restore_quotes(client, plan, previous_quotes)
            if rollback_verify:
                rollback_verify()
        except API_ERRORS + (BasisRepairError,):
            raise BasisRepairError(
                "quote import and rollback failed; restore the database backup"
            ) from None
        raise BasisRepairError(
            "quote import failed; activities and previous quote state were restored: "
            f"{quote_error}"
        ) from None
    if post_verify:
        try:
            return post_verify()
        except API_ERRORS + (BasisRepairError,) as exc:
            try:
                rollback_lot_bulk(client, plan, created_ids, set(delete_ids))
                restore_quotes(client, plan, previous_quotes)
                if rollback_verify:
                    rollback_verify()
            except API_ERRORS + (BasisRepairError,):
                raise BasisRepairError(
                    "postcondition failed and rollback is unverified; "
                    "restore the database backup"
                ) from None
            raise BasisRepairError(
                f"postcondition failed; activity and quote changes rolled back: {exc}"
            ) from None
    return None


def rollback_aggregate_updates(
    client: WealthfolioClient, activities: list[dict], updated_ids: set[str]
) -> None:
    originals = [
        activity_update_payload(row) for row in activities if row["id"] in updated_ids
    ]
    result = client.save_activities(updates=originals) if originals else {}
    if (
        result.get("errors")
        or len(result.get("updated", [])) != len(updated_ids)
    ):
        raise BasisRepairError(
            "aggregate rollback incomplete; restore the database backup"
        )


def apply_aggregate_plan(
    client: WealthfolioClient,
    plan: dict,
    activities: list[dict],
    *,
    post_verify,
    rollback_verify,
):
    updates = materialize_updates(plan, activities)
    client.backup_database()
    try:
        result = client.save_activities(updates=updates)
    except API_ERRORS as exc:
        try:
            rollback_ambiguous_aggregate_mutation(
                client, plan, activities, rollback_verify
            )
        except API_ERRORS + (BasisRepairError,):
            raise BasisRepairError(
                "aggregate mutation transport failed with ambiguous state; "
                "database backup restore required"
            ) from None
        raise BasisRepairError(
            f"aggregate mutation transport failed after possible commit; "
            f"live changes were rolled back and verified: {exc}"
        ) from None
    updated_ids = _result_ids(result.get("updated", []))
    if result.get("errors") or len(updated_ids) != len(updates):
        rollback_aggregate_updates(client, activities, updated_ids)
        raise BasisRepairError(
            "aggregate update count mismatch; reported updates were rolled back, "
            "but database restore is required because completeness cannot be proven"
        )
    try:
        return post_verify(), len(updates)
    except API_ERRORS + (BasisRepairError,) as exc:
        try:
            rollback_aggregate_updates(client, activities, updated_ids)
            rollback_verify()
        except API_ERRORS + (BasisRepairError,):
            raise BasisRepairError(
                "aggregate postcondition failed and rollback is unverified; "
                "restore the database backup"
            ) from None
        raise BasisRepairError(
            f"aggregate postcondition failed; updates rolled back: {exc}"
        ) from None


def connect(base_url: str, data_dir: Path) -> WealthfolioClient:
    client = WealthfolioClient(base_url)
    if not client.health():
        raise BasisRepairError(f"Wealthfolio is unhealthy at {base_url}")
    client.login(password(data_dir))
    return client


def private_path(path: str, data_dir: Path, label: str) -> Path:
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(data_dir.resolve())
    except ValueError:
        raise SystemExit(f"{label} must be inside the private data directory") from None
    return resolved


def write_receipt(
    path: Path,
    *,
    plan: dict,
    base_url: str,
    environment_id: str,
    before: dict,
    after: dict,
) -> None:
    receipt = {
        "schemaVersion": 1,
        "status": "success",
        "planSha256": plan["planSha256"],
        "baseUrl": base_url.rstrip("/"),
        "environmentId": environment_id,
        "accountId": plan["account"]["id"],
        "before": {
            "cash": str(before["cash"]),
            "total": str(before["total"]),
            "gain": str(before["gain"]),
            "returnPercent": str(before["returnPercent"]),
        },
        "after": {
            "cash": str(after["cash"]),
            "total": str(after["total"]),
            "gain": str(after["gain"]),
            "returnPercent": str(after["returnPercent"]),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def verify_production_receipt(
    receipt_path: Path,
    *,
    plan_sha256: str,
    base_url: str,
    environment_id: str,
) -> None:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BasisRepairError(f"invalid rehearsal receipt: {exc}") from None
    if receipt.get("status") != "success":
        raise BasisRepairError("rehearsal receipt is not successful")
    if receipt.get("planSha256") != plan_sha256:
        raise BasisRepairError("rehearsal receipt is for a different plan")
    if receipt.get("baseUrl") == base_url.rstrip("/"):
        raise BasisRepairError("rehearsal receipt must come from a different base URL")
    if receipt.get("environmentId") == environment_id:
        raise BasisRepairError("rehearsal and production environment IDs must differ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--spec")
    parser.add_argument("--account")
    parser.add_argument("--funding-key")
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--receipt")
    parser.add_argument("--environment-id")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    plan_path = Path(args.plan).resolve()
    try:
        plan_path.relative_to(data_dir)
    except ValueError:
        raise SystemExit("--plan must be inside the private data directory") from None

    if args.apply:
        if not args.receipt or not args.environment_id:
            raise SystemExit("apply requires --receipt and --environment-id")
        receipt_path = private_path(args.receipt, data_dir, "receipt")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan_sha256 = verify_plan_integrity(plan)
        verify_evidence_files(plan, data_dir)
        production = urlparse(args.base_url).port == 8088
        if production:
            if not args.allow_production:
                raise SystemExit("refusing production apply without --allow-production")
            if args.expected_plan_sha256 != plan_sha256:
                raise SystemExit(
                    "production apply requires the exact --expected-plan-sha256"
                )
            verify_production_receipt(
                receipt_path,
                plan_sha256=plan_sha256,
                base_url=args.base_url,
                environment_id=args.environment_id,
            )
        elif args.expected_plan_sha256 and args.expected_plan_sha256 != plan_sha256:
            raise SystemExit("--expected-plan-sha256 does not match the plan")
        client = connect(args.base_url, data_dir)
        account_id = plan["account"]["id"]
        activities = [
            row for row in client.iter_activities() if row.get("accountId") == account_id
        ]
        before = account_state(client, account_id)
        if plan["purpose"] == "dated-contribution-lot-replacement":
            verify_lot_state(plan, before, repaired=False)
            after = apply_lot_plan(
                client,
                plan,
                activities,
                post_verify=lambda: wait_for_lot_state(client, plan),
                rollback_verify=lambda: wait_for_original_state(
                    client, plan, verify_lot_state
                ),
            )
            write_receipt(
                receipt_path,
                plan=plan,
                base_url=args.base_url,
                environment_id=args.environment_id,
                before=before,
                after=after,
            )
            print(
                f"replaced 3 synthetic activities with {len(plan['creates']) // 2} "
                f"dated lots; shares {plan['guards']['shares']}; "
                f"basis {after['securities'][plan['asset']['symbol']]['costBasis']:.2f}; "
                f"gain {after['gain']:.2f}; return {after['returnPercent']:.4f}"
            )
            return 0
        verify_state(plan, before, repaired=False)
        after, update_count = apply_aggregate_plan(
            client,
            plan,
            activities,
            post_verify=lambda: wait_for_repaired_state(client, plan),
            rollback_verify=lambda: wait_for_original_state(
                client, plan, verify_state
            ),
        )
        write_receipt(
            receipt_path,
            plan=plan,
            base_url=args.base_url,
            environment_id=args.environment_id,
            before=before,
            after=after,
        )
        print(
            f"repaired {update_count - 1} BUYs; cash and current total unchanged; "
            f"gain {before['gain']:.2f} -> {after['gain']:.2f}; "
            f"return {before['returnPercent']:.4f} -> {after['returnPercent']:.4f}"
        )
        return 0

    client = connect(args.base_url, data_dir)
    if not args.spec or not args.evidence:
        raise SystemExit("planning requires --spec and --evidence")
    spec_path = Path(args.spec).resolve()
    evidence_paths = [Path(path).resolve() for path in args.evidence]
    for path in [spec_path, *evidence_paths]:
        try:
            path.relative_to(data_dir)
        except ValueError:
            raise SystemExit("private inputs must be inside --data-dir") from None
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    account_name = spec.get("account") if spec.get("mode") == "lot-replacement" else args.account
    if not account_name:
        raise SystemExit("aggregate basis planning requires --account")
    accounts = [row for row in client.list_accounts() if row["name"] == account_name]
    if len(accounts) != 1:
        raise BasisRepairError(f"expected one exact account match; found {len(accounts)}")
    account = accounts[0]
    activities = [
        row for row in client.iter_activities() if row.get("accountId") == account["id"]
    ]
    state = account_state(client, account["id"])
    evidence = [
        {
            "path": str(path.relative_to(data_dir)),
            "sha256": file_fingerprint(path.read_bytes()),
        }
        for path in evidence_paths
    ]
    if spec.get("mode") == "lot-replacement":
        source_path = (data_dir / spec["source"]).resolve()
        if source_path not in evidence_paths:
            raise BasisRepairError("lot source must also be passed as --evidence")
        transactions = parse_transactions(
            source_path.read_text(encoding="utf-8-sig"), str(source_path)
        )
        lots = [
            {
                "date": row.run_date.isoformat(),
                "quantity": str(row.quantity),
                "amount": str(row.amount),
            }
            for row in transactions
            if row.account_name == spec["sourceAccount"]
            and row.action == spec["sourceAction"]
        ]
        first_date = min(row["date"] for row in lots)
        quote_dates = {
            (date.fromisoformat(first_date) - timedelta(days=1)).isoformat(),
            spec["quoteDate"],
        }
        plan = build_lot_replacement_plan(
            account=account,
            activities=activities,
            delete_expectations=spec["syntheticDeletes"],
            expected_deleted_cash_effect=Decimal(spec["deletedCashEffect"]),
            lots=lots,
            asset=spec["asset"],
            quote_date=spec["quoteDate"],
            market_value=Decimal(spec["marketValue"]),
            expected_before_total=state["total"],
            expected_shares=Decimal(spec["shares"]),
            expected_basis=Decimal(spec["basis"]),
            expected_cash=Decimal(spec["cash"]),
            evidence=evidence,
            current_quotes=quote_states(client, spec["asset"]["id"], quote_dates),
        )
        verify_lot_state(plan, state, repaired=False)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        print(
            f"wrote fingerprinted plan for {len(lots)} dated lots and "
            f"{len(plan['deletes'])} deletes; sha256 {plan['planSha256']}"
        )
        return 0
    if not args.funding_key:
        raise SystemExit("aggregate basis planning requires --funding-key")
    plan = build_plan(
        account=account,
        activities=activities,
        targets=spec["symbols"],
        funding_key=args.funding_key,
        expected_cash=Decimal(spec["cash"]),
        expected_total=state["total"],
        evidence=evidence,
    )
    verify_state(plan, state, repaired=False)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote fingerprinted plan with {len(plan['operations'])} updates; "
        f"sha256 {plan['planSha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
