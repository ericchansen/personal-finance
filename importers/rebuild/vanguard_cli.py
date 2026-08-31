"""Plan or apply Vanguard full history during an isolated rebuild."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from importers.extracts import vanguard_history
from importers.monarch.wealthfolio_client import WealthfolioClient

from .vanguard import build_vanguard_plan
from .decisions import DecisionError
from .safety import instance_fingerprint, validate_apply_target, validate_private_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-data-dir", type=Path, required=True)
    parser.add_argument("--resolutions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-fingerprint")
    args = parser.parse_args(argv)
    try:
        output = validate_private_output(
            args.output, args.data_dir, Path(__file__).resolve().parents[2]
        )
    except DecisionError as exc:
        parser.error(str(exc))
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    if not password:
        parser.error("WEALTHFOLIO_PASSWORD is required")
    client = WealthfolioClient(args.base_url)
    client.login(password)
    account_map = json.loads(
        (args.data_dir / "canonical-account-map.json").read_text(encoding="utf-8")
    )
    plan = build_vanguard_plan(
        client, args.source_data_dir, account_map, args.resolutions
    )
    environment = instance_fingerprint(client, args.base_url)
    output.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(
        f"create={len(plan['createActivities'])} update={len(plan['updateActivities'])} "
        f"delete={len(plan['deleteActivities'])} links={len(plan['linkActivities'])} "
        f"blockers={len(plan['blockers'])} fingerprint={plan['planFingerprint']} "
        f"environment={environment}"
    )
    if not args.apply:
        return 0
    try:
        validate_apply_target(
            client,
            args.base_url,
            plan["planFingerprint"],
            args.plan_fingerprint,
        )
    except DecisionError as exc:
        parser.error(str(exc))
    live = list(client.iter_activities())
    vanguard_history.validate_apply_preconditions(
        plan,
        supplied_fingerprint=args.plan_fingerprint,
        current_live_activities=live,
    )
    result = vanguard_history.execute_plan(client, plan, live)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
