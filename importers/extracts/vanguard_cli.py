"""Load a Vanguard export into Wealthfolio as real holdings.

    python vanguard_cli.py --data-dir D:/documents/finance-data [--dry-run]

Every other source in this project supplies a balance, which leaves a
retirement account modelled as undifferentiated cash. Vanguard supplies
positions, so these accounts can hold what they actually hold — and a
projection can reason about allocation rather than a single number.

The approach per account:

1. Reconcile the account's cash to the export's total value, since the existing
   figure came from an aggregator months ago and is stale.
2. Buy each position at its exported price, which converts that cash into
   holdings.
3. Leave the settlement fund (a money-market fund) as cash, because that is
   what it is.

Mapping account numbers to names lives in
``<data>/extracts/vanguard/mapping.json`` — the export does not name its own
accounts, and those names are personal data.
"""

from __future__ import annotations

import argparse
import getpass
import glob
import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))

import vanguard  # noqa: E402
from wealthfolio_client import WealthfolioClient, WealthfolioError  # noqa: E402

KEY_PREFIX = "vanguard"


def read_password(data_dir: Path) -> str:
    from_env = os.environ.get("WEALTHFOLIO_PASSWORD")
    if from_env:
        return from_env
    pw_file = data_dir / "wealthfolio" / "ADMIN-PASSWORD.txt"
    if pw_file.exists():
        for line in pw_file.read_text(encoding="utf-8").splitlines():
            candidate = line.strip()
            if candidate and " " not in candidate and not candidate.startswith(
                ("Wealthfolio", "Move", "Only", "generated")
            ):
                return candidate
    return getpass.getpass("Wealthfolio password: ")


def account_value(client: WealthfolioClient, account_id: str) -> Decimal:
    """Net cash effect of everything already recorded in an account."""
    inflow = {"DEPOSIT", "CREDIT", "TRANSFER_IN", "INTEREST", "DIVIDEND"}
    outflow = {"WITHDRAWAL", "TRANSFER_OUT", "FEE", "TAX"}
    total = Decimal("0")
    for row in client.iter_activities():
        if row.get("accountId") != account_id:
            continue
        try:
            amount = Decimal(str(row.get("amount") or 0))
        except Exception:
            continue
        kind = row.get("activityType")
        if kind in inflow:
            total += amount
        elif kind in outflow:
            total -= amount
        elif kind == "BUY":
            total -= amount
        elif kind == "SELL":
            total += amount
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    folder = data_dir / "extracts" / "vanguard"
    config = json.loads((folder / "mapping.json").read_text(encoding="utf-8"))
    files = sorted(glob.glob(str(folder / "*.csv")))
    if not files:
        raise SystemExit(f"no Vanguard export in {folder}")

    export = vanguard.parse_file(Path(files[-1]))
    as_of = config.get("asOf")
    cash_symbols = set(config.get("cashSymbols", ["VMFXX"]))

    print(f"{'ACCOUNT':<36} {'EXPORT VALUE':>14} {'POSITIONS':>10}")
    for number in export.account_numbers:
        spec = config["accounts"].get(number)
        name = spec["name"] if spec else f"(unmapped {number[-4:]})"
        positions = [h for h in export.holdings
                     if h.account_number == number and h.symbol not in cash_symbols]
        print(f"{name[:34]:<36} {export.value_of(number):>14,.2f} {len(positions):>10}")

    if args.dry_run:
        print("\ndry run; nothing sent")
        return 0

    client = WealthfolioClient(args.base_url)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(data_dir))
    client.backup_database()

    existing = {a["name"]: a for a in client.list_accounts()}
    created_accounts = 0
    for number, spec in config["accounts"].items():
        if spec["name"] in existing or not spec.get("create"):
            continue
        account = client.create_account(
            name=spec["name"],
            account_type=spec.get("accountType", "SECURITIES"),
            group=spec.get("group", "Retirement"),
        )
        existing[spec["name"]] = account
        created_accounts += 1
        print(f"created account: {spec['name']}")

    total_buys = 0
    for number in export.account_numbers:
        spec = config["accounts"].get(number)
        if not spec:
            print(f"skipping unmapped account ***{number[-4:]}")
            continue
        account = existing.get(spec["name"])
        if not account:
            print(f"missing account: {spec['name']}")
            continue

        target = export.funding_needed(number, cash_symbols)
        current = account_value(client, account["id"])
        delta = target - current

        payloads = []
        if abs(delta) >= Decimal("0.01"):
            # Bring stale cash up to the value the export actually reports.
            # Dated the day *before* the export: activities sharing a date have
            # no guaranteed order, so funding an account and buying its
            # positions on one day can be evaluated buy-first and leave it
            # transiently overdrawn. An account with prior cash absorbs that; a
            # newly created one starts at zero and goes straight negative.
            funded_on = (date.fromisoformat(as_of) - timedelta(days=1)).isoformat()
            payloads.append({
                "accountId": account["id"],
                "activityType": "DEPOSIT" if delta > 0 else "WITHDRAWAL",
                "activityDate": f"{funded_on}T00:00:00Z",
                "amount": abs(float(delta)),
                "currency": "USD",
                "isDraft": False,
                "comment": "Vanguard reconciliation to exported value",
                "idempotencyKey": f"{KEY_PREFIX}:reconcile:{number}:{as_of}",
            })

        for holding in export.holdings:
            if holding.account_number != number or holding.symbol in cash_symbols:
                continue
            payloads.append({
                "accountId": account["id"],
                "activityType": "BUY",
                "activityDate": f"{as_of}T00:00:00Z",
                # `asset` is an AssetResolutionInput struct, not a bare string.
                # Supplying name and quote mode lets Wealthfolio create the
                # asset if it has never seen the fund before.
                "asset": {
                    "symbol": holding.symbol,
                    "name": holding.name[:120] or holding.symbol,
                    "kind": "SECURITY",
                    "quoteMode": "MARKET",
                    # Required when the asset is created here rather than
                    # resolved from a provider search.
                    "quoteCcy": "USD",
                    "instrumentType": "MUTUALFUND",
                },
                "quantity": float(holding.shares),
                "unitPrice": float(holding.share_price),
                "currency": "USD",
                "isDraft": False,
                "comment": holding.name[:200],
                "idempotencyKey": f"{KEY_PREFIX}:{number}:{holding.symbol}:{as_of}",
            })

        try:
            result = client.save_activities(creates=payloads)
            made = len(result.get("created", []))
            total_buys += made
            errors = result.get("errors") or []
            status = f"{made} activities"
            if errors:
                status += f", {len(errors)} rejected: {errors[0].get('message','')[:70]}"
            print(f"{spec['name'][:34]:<36} {status}")
        except WealthfolioError as exc:
            if "uplicate" in exc.body:
                print(f"{spec['name'][:34]:<36} already loaded")
            else:
                print(f"{spec['name'][:34]:<36} FAILED: {exc}")
                return 1

    print(f"\naccounts created {created_accounts}, activities written {total_buys}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
