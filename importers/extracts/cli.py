"""Load institution extracts into Wealthfolio.

    python cli.py plan  --data-dir D:/documents/finance-data
    python cli.py apply --data-dir D:/documents/finance-data

Extracts overlap history that is already loaded from an aggregator, and the two
sources use different transaction ids, so the server cannot recognise the same
transaction twice. Importing an extract wholesale would therefore duplicate
every overlapping row.

The importer only loads transactions dated **after** the newest activity already
present in the target account. That boundary is read from Wealthfolio at run
time rather than configured, so it stays correct as more data is loaded.

Mapping lives in ``<data>/extracts/mapping.json`` because account names are
personal data. See mapping.example.json.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))

import parsers  # noqa: E402
from wealthfolio_client import WealthfolioClient, WealthfolioError  # noqa: E402

BATCH_SIZE = 200
KEY_PREFIX = "extract"

# Wealthfolio rejects these on a credit card; see the Monarch importer for the
# full matrix. Repeated here so an extract cannot silently lose rows.
CREDIT_CARD_SUBSTITUTIONS = {"DEPOSIT": "CREDIT", "TRANSFER_OUT": "WITHDRAWAL", "TAX": "WITHDRAWAL"}


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


def load_mapping(data_dir: Path) -> dict:
    path = data_dir / "extracts" / "mapping.json"
    if not path.exists():
        raise SystemExit(
            f"no mapping at {path}\n"
            f"copy mapping.example.json there and fill in your own accounts"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def activity_type(amount: Decimal, wf_account_type: str) -> str:
    base = "DEPOSIT" if amount > 0 else "WITHDRAWAL"
    if wf_account_type == "CREDIT_CARD":
        return CREDIT_CARD_SUBSTITUTIONS.get(base, base)
    return base


def latest_dates(client: WealthfolioClient) -> dict[str, date]:
    """Newest activity date already loaded, per account.

    This is the cutoff that prevents an extract from re-importing history the
    aggregator already supplied under different ids.
    """
    newest: dict[str, date] = {}
    for row in client.iter_activities():
        raw = (row.get("date") or "")[:10]
        if not raw:
            continue
        try:
            when = date.fromisoformat(raw)
        except ValueError:
            continue
        account = row["accountId"]
        if account not in newest or when > newest[account]:
            newest[account] = when
    return newest


def resolve(data_dir: Path, client: WealthfolioClient | None = None):
    """Pair each extract file with its target account and cutoff."""
    mapping = load_mapping(data_dir)
    by_name = {}
    cutoffs: dict[str, date] = {}
    if client:
        by_name = {a["name"]: a for a in client.list_accounts()}
        by_id = latest_dates(client)
        cutoffs = {name: by_id.get(a["id"]) for name, a in by_name.items()}

    resolved = []
    for entry in mapping.get("files", []):
        path = Path(entry["file"])
        if not path.is_absolute():
            path = data_dir / "extracts" / entry["file"]
        if not path.exists():
            print(f"missing: {path}")
            continue
        account = by_name.get(entry["account"]) if by_name else None
        resolved.append({
            "path": path,
            "accountName": entry["account"],
            "account": account,
            "cutoff": cutoffs.get(entry["account"]),
            "preferOfx": entry.get("preferOfx", True),
        })
    return resolved


def choose_files(resolved: list[dict]) -> list[dict]:
    """Drop a CSV when the same account and period is also available as OFX.

    OFX carries FITID, so its ids are stable; a CSV id has to be synthesized and
    cannot survive the institution rewording a description.
    """
    seen_ofx = {
        (r["accountName"], r["path"].stem)
        for r in resolved
        if r["path"].suffix.lower() == ".ofx"
    }
    kept = []
    for r in resolved:
        if r["path"].suffix.lower() == ".csv" and (r["accountName"], r["path"].stem) in seen_ofx:
            continue
        kept.append(r)
    return kept


def cmd_plan(args) -> int:
    data_dir = Path(args.data_dir)
    client = None
    if not args.offline:
        client = WealthfolioClient(
            args.base_url, writer_data_dir=args.data_dir
        )
        if client.health():
            client.login(read_password(data_dir))
        else:
            client = None
            print("(Wealthfolio unreachable; showing file contents only)\n")

    resolved = choose_files(resolve(data_dir, client))
    total_new = 0
    print(f"{'FILE':<52} {'ACCOUNT':<34} {'ALL':>6} {'CUTOFF':<12} {'NEW':>6}")
    print("-" * 116)
    for r in resolved:
        extract = parsers.parse_file(r["path"])
        cutoff = r["cutoff"]
        new = [t for t in extract.transactions if cutoff is None or t.date > cutoff]
        total_new += len(new)
        print(
            f"{r['path'].name[:50]:<52} {r['accountName'][:32]:<34} "
            f"{len(extract.transactions):>6} {str(cutoff or '-'):<12} {len(new):>6}"
        )
    print(f"\n{total_new} transactions would be imported")
    return 0


def cmd_apply(args) -> int:
    data_dir = Path(args.data_dir)
    client = WealthfolioClient(args.base_url, writer_data_dir=args.data_dir)
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(data_dir))
    client.backup_database()

    resolved = choose_files(resolve(data_dir, client))
    missing = [r for r in resolved if not r["account"]]
    if missing:
        for r in missing:
            print(f"unknown account: {r['accountName']}")
        raise SystemExit("fix mapping.json before importing")

    payloads: list[dict] = []
    for r in resolved:
        extract = parsers.parse_file(r["path"])
        cutoff = r["cutoff"]
        wf_type = r["account"]["accountType"]
        for t in extract.transactions:
            if cutoff is not None and t.date <= cutoff:
                continue
            payloads.append({
                "accountId": r["account"]["id"],
                "activityType": activity_type(t.amount, wf_type),
                "activityDate": f"{t.date.isoformat()}T00:00:00Z",
                "amount": abs(float(t.amount)),
                "currency": "USD",
                "isDraft": False,
                "comment": t.description[:200] or None,
                "idempotencyKey": f"{KEY_PREFIX}:{r['account']['id']}:{t.source_id}",
            })

    print(f"importing {len(payloads)} transactions")
    created = skipped = 0
    errors: list[str] = []
    for start in range(0, len(payloads), BATCH_SIZE):
        batch = payloads[start:start + BATCH_SIZE]
        try:
            result = client.save_activities(creates=batch)
            created += len(result.get("created", []))
            errors.extend(e.get("message", "?") for e in (result.get("errors") or []))
        except WealthfolioError as exc:
            if "uplicate" not in exc.body:
                raise
            for item in batch:
                try:
                    result = client.save_activities(creates=[item])
                    created += len(result.get("created", []))
                    errors.extend(e.get("message", "?") for e in (result.get("errors") or []))
                except WealthfolioError as inner:
                    if "uplicate" in inner.body:
                        skipped += 1
                    else:
                        raise
        print(f"  {min(start + BATCH_SIZE, len(payloads))}/{len(payloads)}"
              f"  created={created} skipped={skipped}", end="\r")

    print(f"\nimported {created}, already present {skipped}")
    if errors:
        print(f"{len(errors)} rejected:")
        for reason, count in Counter(errors).most_common(5):
            print(f"  {count:>5}  {reason[:110]}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply"])
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--offline", action="store_true",
                        help="plan without contacting Wealthfolio")
    args = parser.parse_args()
    return cmd_plan(args) if args.command == "plan" else cmd_apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
