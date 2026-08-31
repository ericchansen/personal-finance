"""Reconcile staging accounts with canonical account facts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from importers.monarch.wealthfolio_client import WealthfolioClient

from .accounts import apply_account_plan, build_account_plan, build_aliases, load_canonical_accounts
from .decisions import DecisionError
from .safety import instance_fingerprint, plan_fingerprint, validate_apply_target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-data-dir", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-fingerprint")
    args = parser.parse_args(argv)
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    if not password:
        parser.error("WEALTHFOLIO_PASSWORD is required")
    client = WealthfolioClient(args.base_url)
    client.login(password)
    try:
        canonical = load_canonical_accounts(
            args.source_data_dir / "normalized" / "canonical" / "accounts.csv"
        )
        aliases = build_aliases(args.source_data_dir, canonical)
        plan = build_account_plan(client.list_accounts(), canonical, aliases)
        fingerprint = plan_fingerprint(plan)
        environment = instance_fingerprint(client, args.base_url)
        print(
            f"update={len(plan.updates)} create={len(plan.creates)} "
            f"fingerprint={fingerprint} environment={environment}"
        )
        if not args.apply:
            return 0
        validate_apply_target(
            client, args.base_url, fingerprint, args.plan_fingerprint
        )
        mapping = apply_account_plan(client, plan)
        output = args.data_dir / "canonical-account-map.json"
        output.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        print(f"canonical account map: {output}")
    except DecisionError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
