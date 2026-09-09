"""Replay canonical exclusions and transfer links into an isolated rebuild."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from importers.monarch.wealthfolio_client import WealthfolioClient

from .decisions import DecisionError, apply_plan, build_plan
from .safety import instance_fingerprint, plan_fingerprint, validate_apply_target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-fingerprint")
    parser.add_argument("--max-deletes", type=int, default=500)
    args = parser.parse_args(argv)

    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    if not password:
        parser.error("WEALTHFOLIO_PASSWORD is required")
    client = WealthfolioClient(args.base_url, writer_data_dir=args.data_dir)
    if not client.health():
        parser.error(f"Wealthfolio is not reachable at {args.base_url}")
    client.login(password)

    try:
        plan = build_plan(
            client, args.data_dir / "normalized" / "canonical" / "transactions.csv"
        )
        fingerprint = plan_fingerprint(plan)
        environment = instance_fingerprint(client, args.base_url)
        print(
            f"delete={len(plan.delete_ids)} unlink={len(plan.unlink_pairs)} "
            f"unresolved-linked={len(plan.unresolved_linked_exclusions)} "
            f"retype={len(plan.type_updates)} link={len(plan.link_pairs)} "
            f"missing-exclusions={len(plan.missing_exclusions)} "
            f"missing-transfer-legs={len(plan.missing_transfer_legs)} "
            f"fingerprint={fingerprint} environment={environment}"
        )
        if not args.apply:
            print("plan only; nothing sent")
            return 0
        validate_apply_target(
            client, args.base_url, fingerprint, args.plan_fingerprint
        )
        apply_plan(client, plan, max_deletes=args.max_deletes)
    except DecisionError as exc:
        parser.error(str(exc))
    print("canonical decisions applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
