"""Create property, vehicle and loan records in Wealthfolio.

    python cli.py --data-dir D:/documents/finance-data --dry-run
    python cli.py --data-dir D:/documents/finance-data

Alternative assets sit outside the account/activity model: they are a record
plus a valuation, which is why a house does not need a fake account or a
stream of invented transactions.

A loan's balance is derived by amortizing its origination terms up to today,
so a current figure does not depend on having a recent statement to hand.

Configuration lives in ``<data>/assets/holdings.json``. That file names a
property and a loan, which is personal data, so it belongs in the data
directory rather than in this repository. See holdings.example.json.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monarch"))

import loan as loan_math  # noqa: E402
from wealthfolio_client import WealthfolioClient, WealthfolioError  # noqa: E402


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


def load_config(data_dir: Path) -> dict:
    path = data_dir / "assets" / "holdings.json"
    if not path.exists():
        raise SystemExit(
            f"no config at {path}\n"
            f"copy holdings.example.json there and fill in your own values"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def describe_loan(spec: dict, as_of: date) -> tuple[loan_math.LoanState, Decimal]:
    principal = Decimal(str(spec["principal"]))
    rate = Decimal(str(spec["annualRate"]))
    term = int(spec["termMonths"])
    first = date.fromisoformat(spec["firstPayment"])
    state = loan_math.balance_as_of(principal, rate, term, first, as_of)
    payment = loan_math.monthly_payment(principal, rate, term)
    return state, payment


def normalize_kind(kind: str) -> str:
    """The API serialises kinds in lowercase.

    The frontend constants use SCREAMING_CASE (``PROPERTY``), which is easy to
    copy into a payload and get a 422 for, so accept either.
    """
    lowered = kind.strip().lower()
    aliases = {"precious_metal": "precious", "real_estate": "property"}
    return aliases.get(lowered, lowered)


def loan_value_date(spec: dict, assets: list[dict], as_of: date) -> str:
    """Pick the date a loan's valuation should carry.

    An alternative holding is a single point-in-time valuation, and it enters
    the net worth history on that date. A house dated at its appraisal and its
    mortgage dated today therefore leave a window where the asset is counted
    and the debt is not, materially overstating historical net worth.

    So a loan linked to an asset inherits that asset's valuation date. An
    explicit ``valueDate`` on the loan wins, and an unlinked loan falls back to
    the run date.
    """
    if spec.get("valueDate"):
        return str(spec["valueDate"])
    linked_name = spec.get("linkedTo")
    for asset in assets:
        if asset.get("name") == linked_name and asset.get("valueDate"):
            return str(asset["valueDate"])
    return as_of.isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="D:/documents/finance-data")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    as_of = date.fromisoformat(args.as_of)

    assets = config.get("assets", [])
    loans = config.get("loans", [])

    print(f"as of {as_of}\n")
    for asset in assets:
        print(f"  {asset['kind']:<10} {asset['name'][:38]:<40} "
              f"{Decimal(str(asset['currentValue'])):>14,.2f}")
    for spec in loans:
        state, payment = describe_loan(spec, as_of)
        print(f"  {'LIABILITY':<10} {spec['name'][:38]:<40} {state.balance:>14,.2f}")
        print(f"             payment {payment:,.2f}/mo, {state.payments_made} made, "
              f"interest so far {state.interest_paid:,.2f}")

    if args.dry_run:
        print("\ndry run; nothing sent")
        return 0

    client = WealthfolioClient(
        args.base_url, writer_data_dir=data_dir
    )
    if not client.health():
        raise SystemExit(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(read_password(data_dir))
    client.backup_database()

    existing = {a.get("name"): a for a in (client.get("/alternative-holdings") or [])}
    created: dict[str, str] = {}

    for asset in assets:
        if asset["name"] in existing:
            print(f"exists: {asset['name']}")
            created[asset["name"]] = existing[asset["name"]].get("id", "")
            continue
        payload = {
            "kind": normalize_kind(asset["kind"]),
            "name": asset["name"],
            "currency": asset.get("currency", "USD"),
            "currentValue": str(asset["currentValue"]),
            "valueDate": asset["valueDate"],
        }
        for optional in ("purchasePrice", "purchaseDate", "metadata"):
            if asset.get(optional) is not None:
                payload[optional] = (
                    str(asset[optional]) if optional == "purchasePrice" else asset[optional]
                )
        result = client.post("/alternative-assets", payload)
        created[asset["name"]] = result.get("assetId") or result.get("id") or ""
        print(f"created {asset['kind']}: {asset['name']}")

    for spec in loans:
        if spec["name"] in existing:
            print(f"exists: {spec['name']}")
            continue
        value_date = loan_value_date(spec, assets, as_of)
        # Amortize to the date the valuation will carry, not to today, or the
        # balance and its date describe different moments.
        state, _ = describe_loan(spec, date.fromisoformat(value_date))
        payload = {
            "kind": "liability",
            "name": spec["name"],
            "currency": spec.get("currency", "USD"),
            "currentValue": str(state.balance),
            "valueDate": value_date,
            "metadata": spec.get("metadata") or {"sub_type": "mortgage"},
        }
        linked = created.get(spec.get("linkedTo", ""))
        if linked:
            payload["linkedAssetId"] = linked
        try:
            client.post("/alternative-assets", payload)
            print(f"created LIABILITY: {spec['name']} at {state.balance:,.2f}")
        except WealthfolioError as exc:
            print(f"FAILED {spec['name']}: {exc}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
