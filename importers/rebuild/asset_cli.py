"""Create current alternative assets from facts and canonical valuations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from importers.monarch.wealthfolio_client import WealthfolioClient

from .assets import apply_asset_plan, build_asset_plan
from .decisions import DecisionError
from .safety import instance_fingerprint, plan_fingerprint, validate_apply_target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-fingerprint")
    args = parser.parse_args(argv)
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    if not password:
        parser.error("WEALTHFOLIO_PASSWORD is required")
    client = WealthfolioClient(args.base_url)
    if not client.health():
        parser.error(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(password)
    try:
        plan = build_asset_plan(
            client,
            args.data_dir / "facts",
            args.data_dir / "normalized" / "canonical" / "valuations.csv",
        )
        fingerprint = plan_fingerprint(plan)
        environment = instance_fingerprint(client, args.base_url)
        print(
            f"create={len(plan.creates)} existing={len(plan.existing)} "
            f"fingerprint={fingerprint} environment={environment}"
        )
        if not args.apply:
            print("plan only; nothing sent")
            return 0
        validate_apply_target(
            client, args.base_url, fingerprint, args.plan_fingerprint
        )
        apply_asset_plan(client, plan)
    except DecisionError as exc:
        parser.error(str(exc))
    print("canonical alternative holdings applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
