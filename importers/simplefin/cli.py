"""Claim a SimpleFIN setup token and pull account data.

    python cli.py claim --token <setup-token>
    python cli.py accounts
    python cli.py accounts --days 90 --json

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
import getpass
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from importers.monarch.wealthfolio_client import WealthfolioClient  # noqa: E402
from importers.simplefin.client import (  # noqa: E402
    SimpleFinError,
    claim_access_url,
    fetch,
    parse_accounts,
)
from importers.simplefin.collection_status import (  # noqa: E402
    build_failure_receipt,
    build_receipt,
    write_receipt,
)
from finance_store.simplefin import detect_protocol_version  # noqa: E402
from importers.simplefin.pipeline import (  # noqa: E402
    DEFAULT_HISTORY_DAYS,
    MAX_HISTORY_DAYS,
    PipelineError,
    balances_from_activities,
    build_plan,
    existing_from_activities,
    fetch_snapshot,
    load_mapping,
    write_plan,
)


def access_path(data_dir: Path) -> Path:
    return data_dir / "simplefin" / "access-url.txt"


def read_access_url(data_dir: Path) -> str:
    path = access_path(data_dir)
    if not path.exists():
        raise SystemExit(
            f"no access URL at {path}\n"
            f"run:  python cli.py claim --token <setup-token>"
        )
    return path.read_text(encoding="utf-8").strip()


def cmd_claim(args) -> int:
    data_dir = Path(args.data_dir)
    path = access_path(data_dir)
    if path.exists() and not args.force:
        raise SystemExit(
            f"an access URL already exists at {path}\n"
            f"claiming again needs a fresh setup token; pass --force to overwrite"
        )
    access_url = claim_access_url(args.token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(access_url + "\n", encoding="utf-8")
    host = access_url.split("@")[-1].split("/")[0]
    print(f"claimed. access URL stored at {path}")
    print(f"host: {host}")
    print("this file grants read access to every connected institution; "
          "it is deliberately outside the repository")
    return 0


def cmd_accounts(args) -> int:
    data_dir = Path(args.data_dir)
    if not 1 <= args.days <= MAX_HISTORY_DAYS:
        raise PipelineError(f"days must be between 1 and {MAX_HISTORY_DAYS}")
    access_url = read_access_url(data_dir)
    start = date.today() - timedelta(days=args.days - 1)
    accounts, errors = fetch(
        access_url, start=start, balances_only=args.balances_only
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
        # A failed institution is the whole point of monitoring this: it means
        # a connection needs re-authenticating and its data is silently stale.
        print(f"{len(errors)} connection error(s):")
        for err in errors:
            print(f"   {err}")
    return 0


def read_wealthfolio_password(data_dir: Path) -> str:
    if os.environ.get("WEALTHFOLIO_PASSWORD"):
        return os.environ["WEALTHFOLIO_PASSWORD"]
    path = data_dir / "wealthfolio" / "ADMIN-PASSWORD.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            candidate = line.strip()
            if candidate and " " not in candidate and not candidate.startswith(
                ("Wealthfolio", "Move", "Only", "generated")
            ):
                return candidate
    if not sys.stdin.isatty():
        raise PipelineError(
            "WEALTHFOLIO_PASSWORD is required for an unattended plan"
        )
    return getpass.getpass("Wealthfolio password: ")


def cmd_pull_plan(args) -> int:
    """Fetch a snapshot and create a read-only Wealthfolio import/drift plan."""
    data_dir = Path(args.data_dir)
    mapping = load_mapping(data_dir)
    snapshot, payload = fetch_snapshot(
        data_dir,
        read_access_url(data_dir),
        days=args.days,
    )
    detected_version = detect_protocol_version(payload)
    if detected_version != getattr(args, "protocol_version", "1"):
        raise PipelineError(
            "SimpleFIN response protocol version does not match the pinned version"
        )
    accounts, errors = parse_accounts(payload)

    client = WealthfolioClient(args.base_url, writer_data_dir=args.data_dir)
    ledger_error = None
    activities = []
    alternative_holdings = []
    known_ids = None
    if not client.health():
        ledger_error = "Wealthfolio is unreachable; overlap and drift checks unavailable"
    else:
        try:
            client.login(read_wealthfolio_password(data_dir))
            activities = list(client.iter_activities())
            known_ids = {str(account["id"]) for account in client.list_accounts()}
            alternative_holdings = client.get("/alternative-holdings") or []
            known_ids.update(str(holding["id"]) for holding in alternative_holdings)
        except Exception:  # noqa: BLE001 - do not leak response/account details
            ledger_error = "Wealthfolio read failed; overlap and drift checks unavailable"

    ledger_balances = balances_from_activities(activities)
    if known_ids is not None:
        for holding in alternative_holdings:
            ledger_balances[str(holding["id"])] = Decimal(
                str(holding.get("marketValue") or 0)
            )
    plan = build_plan(
        accounts,
        errors,
        mapping,
        existing_from_activities(activities),
        ledger_balances,
        known_ids,
    )
    if ledger_error:
        plan["blockers"].append({"code": "ledger-unavailable", "message": ledger_error})
        plan["ready"] = False
    plan_path, assertion_path = write_plan(data_dir, plan, snapshot)

    # Deliberately print counts and external paths only: account and transaction
    # details belong in the private plan, not scheduler logs.
    print(f"snapshot: {snapshot}")
    print(f"plan: {plan_path}")
    print(f"assertions: {assertion_path}")
    print(
        f"planned={plan['totals'].get('planned', 0)} "
        f"skipped={plan['totals'].get('skipped', 0)} "
        f"review={plan['totals'].get('review', 0)} "
        f"blockers={len(plan['blockers'])}"
    )
    return 0 if plan["ready"] else 2


def cmd_pull_snapshot(args) -> int:
    """Fetch immutable source evidence without reading or writing Wealthfolio."""
    data_dir = Path(args.data_dir)
    observed_at = datetime.now(timezone.utc)
    release_commit = os.environ.get("FINANCE_RELEASE_COMMIT", "")
    snapshot = None
    try:
        snapshot, payload = fetch_snapshot(
            data_dir,
            read_access_url(data_dir),
            days=args.days,
        )
        detected_version = detect_protocol_version(payload)
        if detected_version != getattr(args, "protocol_version", "1"):
            raise PipelineError(
                "SimpleFIN response protocol version does not match the pinned version"
            )
        accounts, errors = parse_accounts(payload)
        receipt = build_receipt(
            data_dir,
            snapshot,
            payload,
            requested_days=args.days,
            observed_at=observed_at,
            release_commit=release_commit,
        )
    except (SimpleFinError, PipelineError, ValueError) as exc:
        failure = build_failure_receipt(
            requested_days=args.days,
            observed_at=observed_at,
            release_commit=release_commit,
            error_code=type(exc).__name__,
            snapshot=snapshot,
        )
        receipt_path = write_receipt(data_dir, failure)
        raise PipelineError(
            f"source collection failed; receipt retained at {receipt_path}"
        ) from None
    receipt_path = write_receipt(data_dir, receipt)
    print(
        json.dumps(
            {
                "accountCount": len(accounts),
                "institutionErrorCount": len(errors),
                "inputSetHash": receipt["inputSetHash"],
                "missingExpectedAccountCount": receipt["inventory"][
                    "missingExpectedCount"
                ],
                "receiptHash": receipt["receiptHash"],
                "receiptPath": str(receipt_path),
                "requestWindowDays": args.days,
                "snapshotSha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    sub = parser.add_subparsers(dest="command", required=True)

    claim = sub.add_parser("claim", help="exchange a setup token for an access URL")
    claim.add_argument("--token", required=True)
    claim.add_argument("--force", action="store_true")
    claim.set_defaults(func=cmd_claim)

    accounts = sub.add_parser("accounts", help="list accounts and transactions")
    accounts.add_argument(
        "--days",
        type=int,
        default=DEFAULT_HISTORY_DAYS,
        help="history window; 45 is recommended, 90 is the hard local maximum",
    )
    accounts.add_argument("--balances-only", action="store_true")
    accounts.add_argument("--json", action="store_true")
    accounts.set_defaults(func=cmd_accounts)

    pull = sub.add_parser(
        "pull-plan",
        help="fetch an immutable snapshot and write a dry-run import/drift plan",
    )
    # Also accept this after the subcommand, as shown in the runbook and task.
    pull.add_argument("--data-dir", default=argparse.SUPPRESS)
    pull.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS)
    pull.add_argument("--base-url", default="http://127.0.0.1:8088")
    pull.set_defaults(func=cmd_pull_plan)

    snapshot = sub.add_parser(
        "pull-snapshot",
        help="fetch immutable source evidence without reading Wealthfolio",
    )
    snapshot.add_argument("--data-dir", default=argparse.SUPPRESS)
    snapshot.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS)
    snapshot.add_argument(
        "--protocol-version",
        choices=("1", "2"),
        default="1",
        help="required response protocol version",
    )
    snapshot.set_defaults(func=cmd_pull_snapshot)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SimpleFinError, PipelineError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
