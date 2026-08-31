"""Plan private monthly valuations and compare them with Wealthfolio staging."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.rebuild.safety import validate_private_output

from .pipeline import (
    ValuationError,
    build_projection_plan,
    build_refresh_plan,
    validate_refresh_plan,
    validate_projection_target,
    write_immutable_json,
    write_refresh_outputs,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValuationError(f"could not read plan: {exc}") from None
    if not isinstance(payload, dict):
        raise ValuationError("plan must be a JSON object")
    return payload


def _client(base_url: str) -> WealthfolioClient:
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    if not password:
        raise ValuationError("WEALTHFOLIO_PASSWORD is required for staging projection")
    client = WealthfolioClient(base_url)
    if not client.health():
        raise ValuationError("Wealthfolio staging is unreachable")
    client.login(password)
    return client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(r"D:\documents\finance-data")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("plan", help="write a private plan and monthly CSV")
    refresh.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    refresh.add_argument("--snapshot", type=Path)

    project = commands.add_parser(
        "project", help="compare a reviewed refresh plan with staging holdings"
    )
    project.add_argument("--refresh-plan", type=Path, required=True)
    project.add_argument("--base-url", default="http://127.0.0.1:18088")
    args = parser.parse_args(argv)

    try:
        if args.command == "plan":
            plan = build_refresh_plan(
                args.data_dir, as_of=args.as_of, snapshot_path=args.snapshot
            )
            plan_path, csv_path = write_refresh_outputs(args.data_dir, REPO_ROOT, plan)
            print(f"plan: {plan_path}")
            print(f"canonical monthly rows: {csv_path}")
            print(
                f"ready={str(plan['ready']).lower()} "
                f"review-needed={plan['reviewNeeded']} "
                f"rows={len(plan['canonicalRows'])} fingerprint={plan['fingerprint']}"
            )
            return 0 if plan["ready"] else 2

        refresh_plan = _read_json(args.refresh_plan)
        validate_refresh_plan(refresh_plan)
        validate_projection_target(args.base_url)
        client = _client(args.base_url)
        projection = build_projection_plan(client, args.base_url, refresh_plan)
        output = (
            args.data_dir
            / "normalized"
            / "current-valuations"
            / refresh_plan["period"]
            / f"projection-{projection['fingerprint'][:16]}.json"
        )
        validate_private_output(output, args.data_dir, REPO_ROOT)
        output.parent.mkdir(parents=True, exist_ok=True)
        write_immutable_json(output, projection)
        print(f"projection: {output}")
        print(
            f"ready={str(projection['ready']).lower()} "
            f"comparisons={len(projection['comparisons'])} "
            f"blockers={len(projection['blockers'])} "
            f"fingerprint={projection['fingerprint']}"
        )
        return 0 if projection["ready"] else 2
    except ValuationError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
