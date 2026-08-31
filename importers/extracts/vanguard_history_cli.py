"""Generate a private Vanguard full-history migration plan; never mutate live data.

The audit file supplies the private account mapping and current holdings.  The
result contains account identifiers and must be written outside this repository.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))

import vanguard_activity  # noqa: E402
import vanguard_history  # noqa: E402
from wealthfolio_client import WealthfolioClient  # noqa: E402


def _password(data_dir: Path | None) -> str:
    if os.environ.get("WEALTHFOLIO_PASSWORD"):
        return os.environ["WEALTHFOLIO_PASSWORD"]
    if data_dir:
        password_file = data_dir / "wealthfolio" / "ADMIN-PASSWORD.txt"
        if password_file.exists():
            for line in password_file.read_text(encoding="utf-8").splitlines():
                candidate = line.strip()
                if candidate and " " not in candidate:
                    return candidate
    return getpass.getpass("Wealthfolio password: ")


def _audit_accounts(audit: dict) -> dict[str, vanguard_history.AccountSpec]:
    simplefin = {row["id"]: row for row in audit["simplefin"]["vanguard_accounts"]}
    wealthfolio = {row["name"]: row for row in audit["wealthfolio_vanguard_accounts"]}
    result = {}
    for workbook in audit["workbooks"]:
        match = workbook.get("simplefin_match")
        name = workbook["identity"].get("wealthfolio_name")
        if not match or not name or name not in wealthfolio:
            continue
        holdings = simplefin[match["id"]]["holdings"]
        shares = {row["symbol"]: Decimal(row["shares"]) for row in holdings}
        result[Path(workbook["file"]).stem.rsplit(" ", 1)[-1]] = vanguard_history.AccountSpec(
            account_number=Path(workbook["file"]).stem.rsplit(" ", 1)[-1],
            wealthfolio_account_id=wealthfolio[name]["id"],
            wealthfolio_name=name,
            expected_shares=shares,
            expected_cash=shares.get(vanguard_history.CASH_SYMBOL, Decimal("0")),
        )
    return result


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _audited_reports(audit: dict) -> tuple[tuple, dict[str, str]]:
    """Rehydrate rows when audited source files were archived after inspection."""
    reports = []
    hashes = {}
    for workbook in audit["workbooks"]:
        transactions = []
        account_types = set()
        for sheet in workbook["sheets"]:
            for source in sheet["transactions"]:
                account_types.add(source.get("account_type_column") or "")
                transactions.append(
                    vanguard_activity.VanguardActivity(
                        transaction_date=date.fromisoformat(source["trade_date"]),
                        settlement_date=(
                            date.fromisoformat(source["settlement_date"])
                            if source.get("settlement_date")
                            else None
                        ),
                        holding=source["name"],
                        symbol=source.get("symbol"),
                        transaction_type=source["vanguard_type"],
                        shares=(
                            Decimal(source["quantity"])
                            if source.get("quantity") is not None
                            else None
                        ),
                        share_price=(
                            Decimal(source["price"]) if source.get("price") is not None else None
                        ),
                        cash_amount=Decimal(source["amount"]),
                        fees=(
                            Decimal("0")
                            if str(source.get("commission_and_fees") or "").casefold()
                            in {"", "free"}
                            else Decimal(source["commission_and_fees"])
                        ),
                    )
                )
        number = Path(workbook["file"]).stem.rsplit(" ", 1)[-1]
        reports.append(
            vanguard_activity.VanguardActivityReport(
                source=workbook["file_name"],
                account=vanguard_activity.VanguardAccountIdentity(
                    number, tuple(sorted(value for value in account_types if value))
                ),
                transactions=tuple(transactions),
            )
        )
        hashes[workbook["file_name"]] = workbook["sha256"]
    return tuple(reports), hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook-dir", required=True, type=Path)
    parser.add_argument("--audit-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--live-json", type=Path, help="offline activity snapshot for tests/review")
    parser.add_argument(
        "--resolutions",
        type=Path,
        help="private cited NAV resolutions for in-kind events",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-fingerprint")
    args = parser.parse_args(argv)

    audit = json.loads(args.audit_json.read_text(encoding="utf-8"))
    # Archived reports are renamed descriptively after download. Identify them
    # by workbook headers rather than requiring Vanguard's original filename.
    paths = [
        path
        for path in sorted(args.workbook_dir.glob("*.xlsx"))
        if vanguard_activity.sniff_workbook(path)
    ]
    if paths:
        reports = vanguard_activity.parse_files(paths)
        workbook_hashes = {path.name: _hash(path) for path in paths}
    else:
        reports, workbook_hashes = _audited_reports(audit)
        print(
            "warning: audited source workbooks are no longer present; "
            "planning from their preserved audit rows and recorded hashes",
            file=sys.stderr,
        )
    if args.live_json:
        live = json.loads(args.live_json.read_text(encoding="utf-8"))
    else:
        client = WealthfolioClient(args.base_url)
        if not client.health():
            parser.error(f"Wealthfolio is not reachable at {args.base_url}")
        client.login(_password(args.data_dir))
        live = list(client.iter_activities())

    resolutions = []
    if args.resolutions:
        resolution_doc = json.loads(args.resolutions.read_text(encoding="utf-8"))
        resolutions = resolution_doc.get("events") or []
        if not isinstance(resolutions, list):
            parser.error("resolution file must contain an events list")

    plan = vanguard_history.build_plan(
        reports,
        _audit_accounts(audit),
        live,
        workbook_hashes,
        resolutions,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(
        f"planned {len(plan['createActivities'])} creates and "
        f"{len(plan['deleteActivities'])} deletions; "
        f"{len(plan['blockers'])} blocker(s)"
    )
    print(f"plan fingerprint: {plan['planFingerprint']}")
    print(f"private plan: {args.output}")
    if args.apply:
        vanguard_history.validate_apply_preconditions(
            plan,
            supplied_fingerprint=args.plan_fingerprint or "",
            current_live_activities=live,
        )
        result = vanguard_history.execute_plan(client, plan, live)
        print(
            f"applied: created={result['created']} updated={result['updated']} "
            f"deleted={result['deleted']} linked={result['linked']} "
            f"rounding={result['roundingCorrections']}"
        )
        return 0
    print("PLAN ONLY: no Wealthfolio mutation attempted")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except vanguard_history.ApplyRefused as exc:
        print(f"APPLY REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
