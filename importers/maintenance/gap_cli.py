"""Fill a known gap in a cash account, without inventing income.

    python gap_cli.py --account "Example Checking" --to 100.00 \
        --date 2026-01-31 --note "Synthetic gap example" --dry-run

An export that starts after the ledger's last known activity leaves a hole. The
balance can still be made correct by adding the difference, but *how* it is
added matters: a DEPOSIT into a cash account is reported as income, so plugging
a gap that way inflates earnings by the size of the plug.

TRANSFER_IN and TRANSFER_OUT are used instead. They move the balance without
claiming money was earned or spent.

This is a stopgap. The honest fix is to download the missing period, so the
entry is labelled with what is missing and dated to the day before the export
begins, which keeps it out of the months that do have real data.
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

from wealthfolio_client import WealthfolioClient  # noqa: E402

INFLOW = {"DEPOSIT", "CREDIT", "TRANSFER_IN", "INTEREST", "DIVIDEND"}
OUTFLOW = {"WITHDRAWAL", "TRANSFER_OUT", "FEE", "TAX", "EXPENSE"}


def plan_gap(
    account_id: str, current: Decimal, target: Decimal, day: str, note: str,
    account_type: str = "CASH",
) -> list[dict]:
    """Return the single entry that moves ``current`` to ``target``.

    Returns nothing when the balance already matches, which makes a re-run a
    no-op.

    For a cash account the correction is a transfer, so it never appears as
    income or spending -- money that entered or left outside the tracked period
    was not earned or spent, it was simply unobserved. The entry is marked
    ``external_transfer`` because there is no second leg to match; without that
    subtype Wealthfolio reports it as a broken transfer, and a genuinely broken
    one becomes indistinguishable from a deliberate fill.

    A credit card growing more negative is different, and the API's refusal to
    accept ``TRANSFER_OUT`` on one is a useful accident. A card balance only
    increases because it was **used**, so the gap is unrecorded spending.
    Cards take ``WITHDRAWAL`` for a charge -- that is what every imported card
    transaction uses -- while ``EXPENSE`` and ``TRANSFER_OUT`` are both
    rejected. The reverse, a card balance shrinking, is a payment from a
    tracked account and stays a transfer.
    """
    delta = target - current
    if abs(delta) < Decimal("0.01"):
        return []

    is_card = account_type == "CREDIT_CARD"
    if delta > 0:
        kind = "TRANSFER_IN"
    elif is_card:
        kind = "WITHDRAWAL"
    else:
        kind = "TRANSFER_OUT"

    entry = {
        "accountId": account_id,
        "activityType": kind,
        "activityDate": f"{day}T00:00:00Z",
        "amount": abs(float(delta)),
        "currency": "USD",
        "isDraft": False,
        "comment": f"Unexported gap: {note}",
        "idempotencyKey": f"gap:{account_id}:{day}",
    }
    if kind in {"TRANSFER_IN", "TRANSFER_OUT"}:
        entry["subtype"] = "external_transfer"
    return [entry]


def balance_from_activity(client: WealthfolioClient, account_id: str) -> Decimal:
    """Derive a balance from activity.

    Credit cards are absent from the valuation endpoint entirely, so summing
    activity is the only method that works for every account type.
    """
    total = Decimal("0")
    for row in client.iter_activities():
        if row.get("accountId") != account_id:
            continue
        try:
            amount = Decimal(str(row.get("amount") or 0))
        except Exception:
            continue
        kind = row.get("activityType")
        if kind in INFLOW:
            total += amount
        elif kind in OUTFLOW:
            total -= amount
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="substring of the name")
    parser.add_argument("--to", required=True, type=Decimal, help="true balance")
    parser.add_argument("--date", required=True, help="date to place the entry")
    parser.add_argument("--note", default="period not covered by export")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    client = WealthfolioClient(
        args.base_url, writer_data_dir=args.data_dir
    )
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(
        os.environ.get("WEALTHFOLIO_PASSWORD") or getpass.getpass("Wealthfolio password: ")
    )

    matches = [
        a for a in client.list_accounts()
        if args.account.lower() in (a.get("name") or "").lower()
    ]
    if len(matches) != 1:
        names = "\n  ".join(a["name"] for a in matches) or "(none)"
        raise SystemExit(f"{args.account!r} matched {len(matches)} accounts:\n  {names}")
    account = matches[0]

    current = balance_from_activity(client, account["id"])
    payloads = plan_gap(account["id"], current, args.to, args.date, args.note,
                        account.get("accountType", "CASH"))
    print(f"{account['name']}")
    print(f"  ledger  {current:>12,.2f}")
    print(f"  actual  {args.to:>12,.2f}")
    print(f"  gap     {args.to - current:>12,.2f}")

    if not payloads:
        print("  already reconciled")
        return 0
    if args.dry_run:
        print(f"  would add {payloads[0]['activityType']} on {args.date}")
        return 0

    client.backup_database()
    result = client.save_activities(creates=payloads)
    print(f"  created {len(result.get('created', []))}, "
          f"errors {result.get('errors') or []}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
