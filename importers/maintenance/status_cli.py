"""Report which accounts have gone stale, and what that costs.

    python status_cli.py --data-dir D:/documents/finance-data

Every source in this project is a manual export, so the ledger decays silently:
an account stops being refreshed and simply keeps reporting the last figure it
saw. Nothing surfaces that. The balance still looks like a balance.

Two kinds of staleness matter differently, so they are reported separately.

**Cash and credit accounts feed spending reports.** A gap there does not just
misstate a balance, it removes real income and spending from the cash flow, and
the remaining months look better or worse than they were. This is the more
damaging kind and is listed first.

**Investment accounts misstate net worth** but leave cash flow alone, because
they are excluded from spending reports by design.

Credit cards need their balances derived from activity: Wealthfolio's account
valuation endpoint omits them entirely, returning no rows even when asked for
one directly, so a report that trusts it silently drops the debt.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))

from wealthfolio_client import WealthfolioClient  # noqa: E402

INFLOW = {"DEPOSIT", "CREDIT", "TRANSFER_IN", "INTEREST", "DIVIDEND"}
OUTFLOW = {"WITHDRAWAL", "TRANSFER_OUT", "FEE", "TAX", "EXPENSE"}
SPENDING_TYPES = {"CASH", "CREDIT_CARD"}


def read_password(data_dir: Path) -> str:
    from_env = os.environ.get("WEALTHFOLIO_PASSWORD")
    if from_env:
        return from_env
    return getpass.getpass("Wealthfolio password: ")


def days_since(day: str | None, today: date) -> int | None:
    if not day:
        return None
    try:
        return (today - datetime.fromisoformat(day[:10]).date()).days
    except ValueError:
        return None


def collect(client: WealthfolioClient) -> list[dict]:
    accounts = {a["id"]: a for a in client.list_accounts() if a.get("isActive")}
    # Closed accounts still count toward net worth, so a stale balance on one
    # is real money in the reported figure.
    closed = {a["id"]: a for a in client.list_accounts() if not a.get("isActive")}
    valued = {
        v["accountId"]: Decimal(str(v.get("totalValue") or 0))
        for v in client.post(
            "/performance/accounts/simple", {"accountIds": list(accounts)}
        )
        or []
    }

    last_seen: dict[str, str] = {}
    counts: dict[str, int] = defaultdict(int)
    derived: dict[str, Decimal] = defaultdict(Decimal)
    for row in client.iter_activities():
        aid = row.get("accountId")
        if aid not in accounts and aid not in closed:
            continue
        counts[aid] += 1
        when = str(row.get("date") or row.get("activityDate") or "")[:10]
        if when and when > last_seen.get(aid, ""):
            last_seen[aid] = when
        if aid in valued:
            continue
        try:
            amount = Decimal(str(row.get("amount") or 0))
        except Exception:
            continue
        kind = row.get("activityType")
        if kind in INFLOW:
            derived[aid] += amount
        elif kind in OUTFLOW:
            derived[aid] -= amount

    today = date.today()
    out = []
    for aid, account in {**accounts, **closed}.items():
        out.append({
            "name": account.get("name") or "",
            "type": account.get("accountType") or "",
            "balance": valued.get(aid, derived.get(aid, Decimal("0"))),
            "activities": counts[aid],
            "last": last_seen.get(aid),
            "stale": days_since(last_seen.get(aid), today),
            "spending": account.get("accountType") in SPENDING_TYPES,
            "closed": aid in closed,
        })
    return out


def partition(rows: list[dict], threshold: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Split accounts into cash-flow risk, net-worth-only risk, and fresh.

    An account that has never held an activity counts as stale rather than
    fresh: a shell account created by an aggregator and never populated is
    exactly the kind of gap this report exists to surface.

    Closed accounts are excluded here. They are reported separately, because
    the question they raise is not "refresh this" but "is this balance real".
    """
    live = [r for r in rows if not r.get("closed")]
    aging = [r for r in live if r["stale"] is None or r["stale"] > threshold]
    fresh = [r for r in live if r not in aging]
    return (
        [r for r in aging if r["spending"]],
        [r for r in aging if not r["spending"]],
        fresh,
    )


def phantom_balances(rows: list[dict]) -> list[dict]:
    """Closed accounts still carrying a balance.

    Wealthfolio counts closed accounts toward net worth, so a stale balance on
    one is real money in the reported figure. A credit card is the common case
    and the suspicious one: an issuer will not close a card while it owes
    money, so a closed card showing a balance usually means the final payoff
    was never recorded — the aggregator simply stopped watching.
    """
    return [
        r for r in rows
        if r.get("closed") and abs(r["balance"]) >= Decimal("0.01")
    ]


def render(rows: list[dict], threshold: int) -> None:
    def show(group: list[dict], title: str, why: str) -> None:
        if not group:
            return
        print(f"\n{title}")
        print(f"  {why}\n")
        print(f"  {'ACCOUNT':<44} {'BALANCE':>12} {'LAST SEEN':>11} {'STALE':>8}")
        for row in sorted(group, key=lambda r: -(r["stale"] or 10**6)):
            stale = "never" if row["stale"] is None else f"{row['stale']}d"
            last = row["last"] or "-"
            print(f"  {row['name'][:42]:<44} {row['balance']:>12,.2f} "
                  f"{last:>11} {stale:>8}")

    cash_flow, net_worth, fresh = partition(rows, threshold)
    show(
        cash_flow,
        "CASH FLOW AT RISK",
        "These feed spending reports, so a gap removes real income and "
        "spending,\n  not just balance accuracy.",
    )
    show(
        net_worth,
        "NET WORTH ONLY",
        "Excluded from spending reports, so a gap misstates net worth but\n"
        "  leaves cash flow intact.",
    )
    show(
        phantom_balances(rows),
        "CLOSED BUT STILL CARRYING A BALANCE",
        "Closed accounts still count toward net worth. An issuer will not\n"
        "  close a card while it owes money, so a balance here usually means\n"
        "  the final payoff was never recorded.",
    )

    live = [r for r in rows if not r.get("closed")]
    print(f"\n{len(fresh)} of {len(live)} open accounts refreshed within "
          f"{threshold} days")
    empty = [r for r in live if r["activities"] == 0]
    if empty:
        print(f"{len(empty)} account(s) have never held any activity:")
        for row in empty:
            print(f"  {row['name'][:60]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--threshold", type=int, default=45,
                        help="days before an account counts as stale")
    args = parser.parse_args(argv)

    client = WealthfolioClient(args.base_url)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(Path(args.data_dir)))

    # Read net worth from the app rather than re-deriving it. Summing accounts
    # by hand is easy to get subtly wrong -- closed accounts still count, and
    # credit cards are missing from the valuation endpoint entirely.
    net = client.get("/net-worth")
    assets = Decimal(str(net["assets"]["total"]))
    liabilities = Decimal(str(net["liabilities"]["total"]))
    print(f"NET WORTH   {assets - liabilities:>16,.2f}   as of {net['date']}")
    print(f"  assets    {assets:>16,.2f}")
    print(f"  liabilities {liabilities:>14,.2f}")

    render(collect(client), args.threshold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
