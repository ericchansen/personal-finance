"""Private bounded repair diff, restore proof, preparation, execution and recovery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import bounded_promotion as promotion
from .receipt_repair import load_plan


def _key(name: str) -> bytes:
    value = os.environ.get(name, "")
    try:
        key = bytes.fromhex(value)
    except ValueError:
        raise promotion.PromotionError(f"{name} must be a hex signing key") from None
    promotion._require(len(key) >= 32, f"{name} must contain at least 32 bytes")
    return key


def _runtime(target: dict, root: Path) -> promotion.GuardedDockerRuntime:
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    promotion._require(bool(password), "WEALTHFOLIO_PASSWORD is required; auth cannot be disabled")
    return promotion.GuardedDockerRuntime(target, root, password)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("diff", "restore-proof", "review-inputs", "prepare", "inspect", "execute", "recover",
                 "archive", "lineage"):
        command = sub.add_parser(name)
        command.add_argument("--data-dir", type=Path, required=True)
        if name in {"diff", "restore-proof"}:
            command.add_argument("--plan", type=Path, required=True)
        if name == "diff":
            command.add_argument("--original", type=Path, required=True)
            command.add_argument("--candidate", type=Path, required=True)
        if name == "restore-proof":
            command.add_argument("--backup", type=Path, required=True)
            command.add_argument("--target", type=Path, required=True)
            command.add_argument("--expected", choices=("ready", "applied"), required=True)
        if name in {"prepare", "review-inputs"}:
            command.add_argument("--request", type=Path, required=True)
        if name in {"inspect", "execute"}:
            command.add_argument("--preparation", type=Path, required=True)
        if name in {"execute", "archive"}:
            command.add_argument("--authorization", type=Path, required=True)
        if name in {"recover", "archive", "lineage"}:
            command.add_argument("--target", type=Path, required=True)
        if name == "archive":
            command.add_argument("--execution-hash", required=True)
            command.add_argument("--destination", type=Path, required=True)
            command.add_argument("--historical-replay-gap", type=Path)
        if name in {"execute", "recover"}:
            command.add_argument("--preparation-id", required=True)
        if name in {"diff", "restore-proof", "review-inputs", "prepare", "lineage"}:
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = args.data_dir.resolve()
        def path(value: Path) -> Path:
            return promotion.private(value, root)
        if args.command in {"diff", "restore-proof", "review-inputs", "prepare", "lineage"}:
            output = path(args.output)
            promotion._require(not output.exists(), "output already exists")
        if args.command == "diff":
            plan = load_plan(path(args.plan))
            before = promotion.sqlite_state(path(args.original), stopped=True)
            after = promotion.sqlite_state(path(args.candidate), stopped=True)
            changes = promotion.database_diff(before, after)
            try:
                promotion.validate_bounded_diff(before, after, plan)
                supported = True
                blocker = None
            except (promotion.PromotionError, ValueError):
                supported = False
                blocker = "Diff fails the bounded preservation contract; do not authorize."
            result = {
                "kind": promotion.KIND + "-diff", "planHash": plan["planHash"],
                "diff": changes, "diffHash": promotion.plan_fingerprint(changes),
                "supported": supported, "blocker": blocker,
            }
        else:
            evidence_key = _key(promotion.EVIDENCE_KEY_ENV)
            if args.command == "restore-proof":
                result = promotion.restore_proof(
                    root=root, backup=path(args.backup), plan=load_plan(path(args.plan)),
                    runtime=_runtime(promotion.load(path(args.target)), root),
                    evidence_key=evidence_key, expected=args.expected,
                )
            elif args.command == "review-inputs":
                result = promotion.build_review_inputs(
                    root=root, request=promotion.load(path(args.request)), evidence_key=evidence_key
                )
            elif args.command == "prepare":
                request = promotion.load(path(args.request))
                result = promotion.prepare(
                    root=root, request=request, runtime=_runtime(request["target"], root),
                    evidence_key=evidence_key,
                )
            elif args.command == "inspect":
                document = promotion.load(path(args.preparation))
                promotion.inspect_preparation(document, root=root, evidence_key=evidence_key)
                result = document
            elif args.command == "execute":
                document = promotion.load(path(args.preparation))
                preparation = promotion.unseal(
                    document, evidence_key, promotion.KIND + "-preparation"
                )
                result = promotion.execute(
                    root=root, document=document,
                    authorization=promotion.load(path(args.authorization)),
                    supplied_preparation_id=args.preparation_id,
                    runtime=_runtime(preparation["target"], root), evidence_key=evidence_key,
                    operator_key=_key(promotion.OPERATOR_KEY_ENV),
                )
            elif args.command == "recover":
                result = promotion.recover(
                    root=root, runtime=_runtime(promotion.load(path(args.target)), root),
                    evidence_key=evidence_key, operator_key=_key(promotion.OPERATOR_KEY_ENV),
                    expected_preparation_id=args.preparation_id,
                )
            elif args.command == "archive":
                from .repair_lineage import archive_completed
                result = archive_completed(
                    root=root, runtime=promotion.GuardedDockerRuntime(
                        promotion.load(path(args.target)), root, password=""
                    ),
                    destination=path(args.destination), authorization=promotion.load(path(args.authorization)),
                    supplied_execution_hash=args.execution_hash,
                    evidence_key=evidence_key, operator_key=_key(promotion.OPERATOR_KEY_ENV),
                    historical_replay_gap=(
                        promotion.load(path(args.historical_replay_gap)) if args.historical_replay_gap else None
                    ),
                )
            else:
                from .repair_lineage import build_lineage
                result = build_lineage(
                    root=root, target=promotion.load(path(args.target)),
                    evidence_key=evidence_key, operator_key=_key(promotion.OPERATOR_KEY_ENV),
                )
        if args.command in {"diff", "restore-proof", "review-inputs", "prepare", "lineage"}:
            promotion.write(output, result)
        # Do not echo private paths, amounts, rows, credentials or exception payloads.
        print(json.dumps({
            "kind": result["kind"], "documentHash": result.get("documentHash"),
            "phase": result.get("phase"), "supported": result.get("supported"),
            **({"historicalMatchingReplayAvailable": False,
                "missingHistoricalBindingCount": len(
                    result.get("missingBindings") or result.get("missingHistoricalBindings") or []
                )}
               if result.get("historicalMatchingReplayAvailable") is False else {}),
        }, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({
            "error": "Bounded promotion failed closed. Inspect private evidence and execution journal."
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
