"""Close a rolled-over account in Wealthfolio.

    python cli.py --source "Old Employer Plan" --destination "Rollover IRA" \
        --date 2025-01-15 --dry-run

Names are matched case-insensitively against a unique substring of the account
name, so the account's UUID never has to be pasted in by hand. An ambiguous
match is refused rather than guessed at, because moving the wrong retirement
account's balance is not a mistake worth risking to save a prompt.

The destination's opening activity is re-dated to the rollover date so the
money does not disappear between leaving one account and arriving in the other.
Pass --no-redate to leave the destination untouched.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))

from rollover import Rollover, plan_close, plan_redate  # noqa: E402
from wealthfolio_client import WealthfolioClient, WealthfolioError  # noqa: E402


def read_password(data_dir: Path) -> str:
    from_env = os.environ.get("WEALTHFOLIO_PASSWORD")
    if from_env:
        return from_env
    return getpass.getpass("Wealthfolio password: ")


def find_account(accounts: list[dict], needle: str) -> dict:
    matches = [a for a in accounts if needle.lower() in (a.get("name") or "").lower()]
    if not matches:
        raise SystemExit(f"no account matching {needle!r}")
    if len(matches) > 1:
        names = "\n  ".join(a["name"] for a in matches)
        raise SystemExit(f"{needle!r} matches several accounts:\n  {names}")
    return matches[0]


def account_value(client: WealthfolioClient, account_id: str) -> Decimal:
    rows = client.post("/performance/accounts/simple", {"accountIds": [account_id]})
    for row in rows or []:
        if row.get("accountId") == account_id:
            return Decimal(str(row.get("totalValue") or 0))
    return Decimal("0")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="account being closed")
    parser.add_argument("--destination", required=True, help="account that received it")
    parser.add_argument("--date", required=True, help="date the rollover settled")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    parser.add_argument("--no-redate", action="store_true")
    parser.add_argument("--keep-active", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    client = WealthfolioClient(args.base_url)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(Path(args.data_dir)))

    accounts = client.list_accounts()
    source = find_account(accounts, args.source)
    destination = find_account(accounts, args.destination)
    balance = account_value(client, source["id"])

    print(f"source      {source['name']}  {balance:,.2f}")
    print(f"destination {destination['name']}")
    print(f"date        {args.date}")

    creates = plan_close(
        Rollover(
            source_account_id=source["id"],
            source_balance=balance,
            destination_name=destination["name"],
            effective_date=args.date,
        )
    )

    updates = []
    if not args.no_redate:
        for activity in client.iter_activities():
            if activity.get("accountId") != destination["id"]:
                continue
            if activity.get("activityType") not in {"DEPOSIT", "TRANSFER_IN"}:
                continue
            redated = plan_redate(activity, args.date)
            if redated:
                updates.append(redated)

    print(f"\n{len(creates)} activity to create, {len(updates)} to re-date")
    if args.dry_run:
        print("dry run; nothing sent")
        return 0

    client.backup_database()
    if creates or updates:
        result = client.save_activities(creates=creates, updates=updates)
        errors = result.get("errors") or []
        print(f"created {len(result.get('created', []))}, updated {len(result.get('updated', []))}")
        for err in errors:
            print(f"  rejected: {err.get('message', '')[:120]}")

    if not args.keep_active:
        try:
            client.update_account(source["id"], **{**source, "isActive": False})
            print(f"deactivated {source['name']}")
        except WealthfolioError as exc:
            print(f"could not deactivate: {exc}")

    print(f"\nsource now {account_value(client, source['id']):,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
