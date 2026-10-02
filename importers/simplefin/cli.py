"""Claim a SimpleFIN setup token and inspect connected accounts.

    python -m importers.simplefin.cli claim --token <setup-token>
    python -m importers.simplefin.cli accounts
    python -m importers.simplefin.cli accounts --days 90 --json

The access URL is written to ``<data>/simplefin/access-url.txt`` with no copy
in this repository, because it embeds HTTP Basic credentials that grant read
access to every connected institution. Treat it like a password: it is
revocable from the Bridge dashboard, so if it leaks, revoke rather than
panic.

Claiming consumes the setup token. If the claim succeeds but writing the file
fails, the token is spent and a new one must be generated -- so the file is
written before anything else is attempted.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

from importers.simplefin.client import (
    DEFAULT_HISTORY_DAYS,
    MAX_HISTORY_DAYS,
    SimpleFinError,
    access_path,
    claim_access_url,
    fetch,
    read_access_url,
)


def cmd_claim(args) -> int:
    path = access_path(Path(args.data_dir))
    if path.exists() and not args.force:
        raise SimpleFinError(
            f"an access URL already exists at {path}; "
            "claiming again needs a fresh setup token; pass --force to overwrite"
        )
    access_url = claim_access_url(args.token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(access_url + "\n", encoding="utf-8")
    print(f"claimed. access URL stored at {path}")
    print(f"host: {access_url.split('@')[-1].split('/')[0]}")
    print("this file grants read access to every connected institution; "
          "it is deliberately outside the repository")
    return 0


def cmd_accounts(args) -> int:
    if not 1 <= args.days <= MAX_HISTORY_DAYS:
        raise SimpleFinError(f"days must be between 1 and {MAX_HISTORY_DAYS}")
    start = date.today() - timedelta(days=args.days - 1)
    accounts, errors = fetch(
        read_access_url(Path(args.data_dir)), start=start, balances_only=args.balances_only
    )

    if args.json:
        print(json.dumps({
            "accounts": [
                {
                    "id": a.id, "org": a.org, "name": a.name,
                    "currency": a.currency, "balance": str(a.balance),
                    "balanceDate": a.balance_date.isoformat() if a.balance_date else None,
                    "transactions": [
                        {"id": t.id, "posted": t.posted.isoformat(),
                         "amount": str(t.amount), "description": t.description,
                         "pending": t.pending}
                        for t in a.transactions
                    ],
                }
                for a in accounts
            ],
            "errors": errors,
        }, indent=1))
        return 0

    by_org: dict[str, list] = {}
    for account in accounts:
        by_org.setdefault(account.org or "(unknown)", []).append(account)

    print(f"{len(accounts)} accounts across {len(by_org)} institutions\n")
    for org in sorted(by_org):
        print(org)
        for a in sorted(by_org[org], key=lambda x: x.name):
            unit = "" if a.is_currency else "  [non-currency unit]"
            seen = a.balance_date.isoformat() if a.balance_date else "unknown"
            print(f"   {a.name[:44]:46} {a.balance:>14,.2f}  "
                  f"{len(a.transactions):>4} txns  as of {seen}{unit}")
        print()

    if errors:
        # A failed institution means a connection needs re-authenticating and
        # its data is silently stale.
        print(f"{len(errors)} connection error(s):")
        for err in errors:
            print(f"   {err}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", default=os.environ.get("FINANCE_DATA"),
        required=not os.environ.get("FINANCE_DATA"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    claim = sub.add_parser("claim", help="exchange a setup token for an access URL")
    claim.add_argument("--token", required=True)
    claim.add_argument("--force", action="store_true")
    claim.set_defaults(func=cmd_claim)

    accounts = sub.add_parser("accounts", help="list accounts and transactions")
    accounts.add_argument(
        "--days", type=int, default=DEFAULT_HISTORY_DAYS,
        help=f"history window; at most {MAX_HISTORY_DAYS}",
    )
    accounts.add_argument("--balances-only", action="store_true")
    accounts.add_argument("--json", action="store_true")
    accounts.set_defaults(func=cmd_accounts)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SimpleFinError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
