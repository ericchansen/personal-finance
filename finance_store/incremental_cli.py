"""Executable, opt-in incremental cash worker. Never collects or schedules."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import incremental
from .incremental_inputs import IncrementalHold, load_scope
from importers.monarch.wealthfolio_client import WealthfolioClient


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "run", "status"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--scope", action="append", required=True,
                        help="External, hash-bound account scope configuration; repeat for independent runs")
    parser.add_argument("--run-hash", help="Exact stored plan for apply")
    parser.add_argument("--dsn-env", default="FINANCE_INCREMENTAL_DSN")
    parser.add_argument("--password-env", default="WEALTHFOLIO_PASSWORD")
    args = parser.parse_args(argv)
    if args.command == "apply" and (not args.run_hash or len(args.scope) != 1):
        parser.error("apply requires --run-hash and exactly one --scope")
    import psycopg
    failed = False
    for config_path in args.scope:
        try:
            scope = load_scope(args.data_dir, config_path)
            with psycopg.connect(os.environ[args.dsn_env], autocommit=True) as connection:
                if args.command == "status":
                    result = incremental.status(connection, scope.scope_id, root=scope.root)
                else:
                    client = WealthfolioClient(scope.config["origin"], writer_data_dir=scope.root)
                    client.login(os.environ[args.password_env])
                    if args.command == "apply":
                        result = incremental.apply(connection, scope, client, args.run_hash)
                    else:
                        result = getattr(incremental, args.command)(connection, scope, client)
            safe = {key: result[key] for key in (
                "runHash", "run_hash", "scopeId", "state", "reason", "heldCount", "replay",
                "historicalBackfillCount", "cashTransitionCount",
                "proposedOperationCount", "journaledOperationCount", "appliedOperationCount",
                "plannedBalanceMatchesSource", "newHistoricalReviewCount", "frontierReviewCount",
            ) if key in result}
            safe["scopeId"] = scope.scope_id
            evidence = result.get("evidence") or {}
            safe["operationCount"] = (len(result["operations"]) if "operations" in result
                                      else evidence.get("operationCount"))
            if "reason" not in safe and evidence.get("reason"):
                safe["reason"] = evidence["reason"]
            for key in ("heldCount", "eligibleBatchOnly", "historicalBackfillCount", "cashTransitionCount", "sourceCheckpointHash",
                        "proposedOperationCount", "journaledOperationCount", "appliedOperationCount",
                        "plannedBalanceMatchesSource", "newHistoricalReviewCount", "frontierReviewCount"):
                if key in evidence:
                    safe[key] = evidence[key]
            print(json.dumps(safe, sort_keys=True))
            failed |= result.get("state") in {"held", "uncertain"}
        except Exception as error:
            # Exception strings from HTTP/database clients may contain private
            # rows or credentials. Only our fixed reason codes go to stdout.
            reason = str(error) if isinstance(error, IncrementalHold) else type(error).__name__
            print(json.dumps({"state": "held", "reason": reason}, sort_keys=True))
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
