"""Load a Fidelity export into Wealthfolio as real holdings.

    python fidelity_cli.py --data-dir D:/documents/finance-data [--dry-run]

Fidelity spreads one household across three unrelated shapes, and all three
have to be handled differently:

**Brokerage.** Ordinary tickers plus a money-market sweep. The sweep is cash,
so it is reconciled as cash rather than bought as a position.

**Employer plan.** Holds a collective investment trust identified by CUSIP.
No quote provider can price it, so it is created with a manual quote at the
exported price. Buying it as a market asset instead produces a holding that is
permanently flagged stale — which is exactly how the existing "price update
needed" warning arose.

**Stock plan (ESPP).** The balance is *cash withheld from pay*, not stock: it
buys shares at a discount when the offering period closes. Recording it as a
position would invent shares that do not exist yet and would book the discount
as a gain months early.

It is nonetheless created as a SECURITIES account, not a CASH one. Wealthfolio's
CASH type means "a bank account that feeds spending reports", which this is not:
the money never passes through a tracked checking account, so reporting it as
income would invent earnings. SECURITIES accounts are excluded from spending by
design, and the account will hold real shares once the offering period closes.
An investment account holding only cash is an ordinary thing; a bank account
that quietly fabricates income is not.

Mapping account numbers to names lives in
``<data>/extracts/fidelity/mapping.json``, because account names are personal
data and the positions export does not always name every account.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))

import fidelity  # noqa: E402
from wealthfolio_client import WealthfolioClient, WealthfolioError  # noqa: E402

KEY_PREFIX = "fidelity"


def read_password(data_dir: Path) -> str:
    from_env = os.environ.get("WEALTHFOLIO_PASSWORD")
    if from_env:
        return from_env
    return getpass.getpass("Wealthfolio password: ")


def account_value(client: WealthfolioClient, account_id: str) -> Decimal:
    rows = client.post("/performance/accounts/simple", {"accountIds": [account_id]})
    for row in rows or []:
        if row.get("accountId") == account_id:
            return Decimal(str(row.get("totalValue") or 0))
    return Decimal("0")


def reconcile(account_id: str, delta: Decimal, as_of: str, key: str) -> list[dict]:
    """Move an account's cash by ``delta``, dated the day before the export.

    Dating it near the export rather than today keeps earlier periods
    untouched, so refreshing a stale balance does not rewrite last year's net
    worth.

    The day *before* matters: activities on the same date have no guaranteed
    order, so funding an account and buying its positions on one day can be
    evaluated buy-first and leave the account transiently overdrawn. Existing
    accounts usually have enough prior cash to absorb that, but a newly created
    one starts at zero and goes straight negative.
    """
    if abs(delta) < Decimal("0.01"):
        return []
    funded_on = (date.fromisoformat(as_of) - timedelta(days=1)).isoformat()
    return [{
        "accountId": account_id,
        "activityType": "DEPOSIT" if delta > 0 else "WITHDRAWAL",
        "activityDate": f"{funded_on}T00:00:00Z",
        "amount": abs(float(delta)),
        "currency": "USD",
        "isDraft": False,
        "comment": "Fidelity reconciliation to exported value",
        "idempotencyKey": f"{KEY_PREFIX}:reconcile:{key}:{as_of}",
    }]


def buy(account_id: str, position, as_of: str) -> dict:
    manual = position.needs_manual_price
    return {
        "accountId": account_id,
        "activityType": "BUY",
        "activityDate": f"{as_of}T00:00:00Z",
        "asset": {
            "symbol": position.symbol,
            "name": position.description[:120] or position.symbol,
            "kind": "SECURITY",
            # A CUSIP has no public quote; asking for MARKET leaves the
            # holding stuck at its import price and flagged as stale forever.
            "quoteMode": "MANUAL" if manual else "MARKET",
            "quoteCcy": "USD",
            "instrumentType": "MUTUALFUND" if manual else "EQUITY",
        },
        "quantity": float(position.quantity),
        "unitPrice": float(position.last_price),
        "currency": "USD",
        "isDraft": False,
        "comment": position.description[:200],
        "idempotencyKey": (
            f"{KEY_PREFIX}:{position.account_number}:{position.symbol}:{as_of}"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    folder = data_dir / "extracts" / "fidelity"
    mapping_path = folder / "mapping.json"
    if not mapping_path.exists():
        raise SystemExit(
            f"no mapping at {mapping_path}\n"
            f"copy fidelity-mapping.example.json there and fill in your own values"
        )
    config = json.loads(mapping_path.read_text(encoding="utf-8"))
    as_of = config["asOf"]

    files = sorted(folder.glob("*.csv"))
    if not files:
        raise SystemExit(f"no Fidelity export in {folder}")
    export = fidelity.parse_files(files)

    espp_total = sum((e.amount for e in export.espp), Decimal("0"))

    print(f"{'ACCOUNT':<34} {'EXPORT VALUE':>14} {'POSITIONS':>10}")
    for number, name in export.accounts.items():
        spec = config["accounts"].get(number)
        label = spec["name"] if spec else f"(unmapped {number})"
        held = [p for p in export.positions
                if p.account_number == number and not p.is_cash]
        print(f"{label[:32]:<34} {export.value_of(number):>14,.2f} {len(held):>10}")
    if espp_total:
        espp_spec = config.get("espp", {})
        print(f"{espp_spec.get('name', 'ESPP (unmapped)')[:32]:<34} "
              f"{espp_total:>14,.2f} {'cash':>10}")
    for number in config.get("zeroOut", []):
        print(f"{'-> zero out ' + number:<34} {0:>14,.2f}")

    if args.dry_run:
        print("\ndry run; nothing sent")
        return 0

    client = WealthfolioClient(args.base_url, writer_data_dir=args.data_dir)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(data_dir))
    client.backup_database()

    existing = {a["name"]: a for a in client.list_accounts()}
    for spec in list(config["accounts"].values()) + [config.get("espp") or {}]:
        name = spec.get("name")
        if not name or name in existing or not spec.get("create"):
            continue
        account = client.create_account(
            name=name,
            account_type=spec.get("accountType", "SECURITIES"),
            group=spec.get("group", "Retirement"),
        )
        existing[name] = account
        print(f"created account: {name}")

    total_created = 0
    zero_out = set(config.get("zeroOut", []))
    for number, spec in config["accounts"].items():
        # An account named in zeroOut is handled below. Reconciling it here as
        # well would apply the same correction twice, because Wealthfolio
        # recalculates valuations asynchronously and the second read still
        # returns the pre-correction figure.
        if number in zero_out:
            continue
        account = existing.get(spec["name"])
        if not account:
            print(f"missing account: {spec['name']}")
            continue
        if number not in export.accounts:
            # Absent from the export is not the same as empty: a download that
            # did not cover this account would otherwise silently zero it.
            print(f"{spec['name'][:32]:<34} not in export; left alone")
            continue

        target = export.funding_needed(number)
        current = account_value(client, account["id"])
        payloads = reconcile(account["id"], target - current, as_of, number)
        payloads += [
            buy(account["id"], p, as_of)
            for p in export.positions
            if p.account_number == number and not p.is_cash
        ]
        total_created += send(client, spec["name"], payloads)

    espp_spec = config.get("espp")
    if espp_spec and espp_total:
        account = existing.get(espp_spec["name"])
        if account:
            delta = espp_total - account_value(client, account["id"])
            payloads = reconcile(account["id"], delta, as_of, "espp")
            total_created += send(client, espp_spec["name"], payloads)

    for number in sorted(zero_out):
        spec = config["accounts"].get(number) or {"name": number}
        account = existing.get(spec["name"])
        if not account:
            continue
        delta = -account_value(client, account["id"])
        payloads = reconcile(account["id"], delta, as_of, f"zero:{number}")
        total_created += send(client, spec["name"], payloads)

    print(f"\n{total_created} activities created")
    return 0


def send(client: WealthfolioClient, label: str, payloads: list[dict]) -> int:
    if not payloads:
        print(f"{label[:32]:<34} up to date")
        return 0
    try:
        result = client.save_activities(creates=payloads)
    except WealthfolioError as exc:
        # A re-run resends the same positions, and Wealthfolio rejects the
        # whole batch as duplicates. That is the loader being idempotent, not
        # a failure -- but it has to be told apart from a real rejection so a
        # genuine error is not silently swallowed.
        if "Duplicate activity" in str(exc):
            print(f"{label[:32]:<34} already loaded")
            return 0
        print(f"{label[:32]:<34} failed: {exc}")
        return 0
    made = len(result.get("created", []))
    # Rejections come back in the response body rather than as an error, so a
    # run that imported almost nothing can still look like a success.
    errors = result.get("errors") or []
    status = f"{made} activities"
    if errors:
        status += f", {len(errors)} rejected: {errors[0].get('message', '')[:60]}"
    print(f"{label[:32]:<34} {status}")
    return made


if __name__ == "__main__":
    raise SystemExit(main())
